#' Run Native DuckDB DataQualityDashboard (DQD) Engine
#'
#' Executes vectorized pure-SQL OHDSI DQD checks (conformance, completeness, plausibility,
#' and temporal logic) directly in DuckDB and optionally writes standard OHDSI DQD JSON.
#'
#' @param con Active DuckDB DBI connection.
#' @param output_json Optional file path to write standard OHDSI DQD JSON.
#' @param check_levels Character vector of check categories: `"TABLE"`, `"FIELD"`, `"CONCEPT"`, `"TEMPORAL"`.
#' @param cdm_version Character CDM version (default `"5.4"`).
#' @return A list with `Metadata`, `Overview`, and `CheckResults`.
#' @export
run_dqd <- function(con,
                    output_json = NULL,
                    check_levels = c("TABLE", "FIELD", "CONCEPT", "TEMPORAL"),
                    cdm_version = "5.4") {
  check_levels <- toupper(check_levels)
  results <- list()

  has_table <- function(tbl) {
    cnt <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM information_schema.tables WHERE lower(table_name) = '%s'", tolower(tbl)))$n[1]
    cnt > 0
  }

  get_row_count <- function(tbl) {
    if (!has_table(tbl)) return(0L)
    DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s", tbl))$n[1]
  }

  tables <- c(
    "person", "observation_period", "visit_occurrence", "condition_occurrence",
    "procedure_occurrence", "drug_exposure", "measurement", "death", "care_site",
    "provider", "location", "cdm_source", "concept"
  )

  # 1. TABLE CHECKS
  if ("TABLE" %in% check_levels) {
    for (tbl in tables) {
      exists <- has_table(tbl)
      row_cnt <- if (exists) get_row_count(tbl) else 0L
      is_required <- tbl %in% c("person", "observation_period", "visit_occurrence", "cdm_source")
      failed <- if (is_required) !exists else FALSE

      results[[length(results) + 1]] <- list(
        check_id = sprintf("TABLE_EXISTS_%s", toupper(tbl)),
        check_name = "cdmTable",
        check_level = "TABLE",
        cdm_table_name = tbl,
        cdm_field_name = NA_character_,
        check_description = sprintf("Verifies that required table %s is present in the CDM.", tbl),
        num_violated_rows = if (exists) 0L else 1L,
        pct_violated_rows = if (exists) 0.0 else 100.0,
        num_denominator_rows = 1L,
        execution_status = if (!failed) "PASSED" else "FAILED",
        failed = failed
      )

      if (exists && is_required) {
        empty_failed <- (row_cnt == 0L)
        results[[length(results) + 1]] <- list(
          check_id = sprintf("TABLE_NOT_EMPTY_%s", toupper(tbl)),
          check_name = "measurePersonCompleteness",
          check_level = "TABLE",
          cdm_table_name = tbl,
          cdm_field_name = NA_character_,
          check_description = sprintf("Verifies that required table %s contains clinical rows.", tbl),
          num_violated_rows = if (empty_failed) 1L else 0L,
          pct_violated_rows = if (empty_failed) 100.0 else 0.0,
          num_denominator_rows = 1L,
          execution_status = if (!empty_failed) "PASSED" else "FAILED",
          failed = empty_failed
        )
      }
    }
  }

  # 2. FIELD CHECKS
  if ("FIELD" %in% check_levels) {
    required_fields <- list(
      c("person", "person_id"),
      c("person", "gender_concept_id"),
      c("person", "year_of_birth"),
      c("observation_period", "observation_period_id"),
      c("observation_period", "person_id"),
      c("observation_period", "observation_period_start_date"),
      c("observation_period", "observation_period_end_date"),
      c("visit_occurrence", "visit_occurrence_id"),
      c("visit_occurrence", "person_id"),
      c("visit_occurrence", "visit_concept_id"),
      c("visit_occurrence", "visit_start_date"),
      c("condition_occurrence", "condition_occurrence_id"),
      c("condition_occurrence", "person_id"),
      c("condition_occurrence", "condition_concept_id"),
      c("condition_occurrence", "condition_start_date"),
      c("drug_exposure", "drug_exposure_id"),
      c("drug_exposure", "person_id"),
      c("drug_exposure", "drug_concept_id"),
      c("drug_exposure", "drug_exposure_start_date"),
      c("procedure_occurrence", "procedure_occurrence_id"),
      c("procedure_occurrence", "person_id"),
      c("procedure_occurrence", "procedure_concept_id"),
      c("procedure_occurrence", "procedure_date"),
      c("measurement", "measurement_id"),
      c("measurement", "person_id"),
      c("measurement", "measurement_concept_id"),
      c("measurement", "measurement_date")
    )

    for (item in required_fields) {
      tbl <- item[1]
      fld <- item[2]
      if (!has_table(tbl)) next
      total <- get_row_count(tbl)
      if (total == 0L) next

      null_count <- as.integer(DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s WHERE %s IS NULL", tbl, fld))$n[1])
      pct <- round((null_count / total * 100.0), 2)
      results[[length(results) + 1]] <- list(
        check_id = sprintf("FIELD_NOT_NULL_%s_%s", toupper(tbl), toupper(fld)),
        check_name = "cdmField",
        check_level = "FIELD",
        cdm_table_name = tbl,
        cdm_field_name = fld,
        check_description = sprintf("The number and percent of records with a NULL value in the %s field of the %s table.", fld, tbl),
        num_violated_rows = null_count,
        pct_violated_rows = pct,
        num_denominator_rows = total,
        execution_status = if (null_count == 0L) "PASSED" else "FAILED",
        failed = null_count > 0L
      )
    }

    # Primary Key Uniqueness
    pk_fields <- list(
      c("person", "person_id"),
      c("observation_period", "observation_period_id"),
      c("visit_occurrence", "visit_occurrence_id"),
      c("condition_occurrence", "condition_occurrence_id"),
      c("drug_exposure", "drug_exposure_id"),
      c("procedure_occurrence", "procedure_occurrence_id"),
      c("measurement", "measurement_id"),
      c("care_site", "care_site_id"),
      c("provider", "provider_id"),
      c("location", "location_id")
    )
    for (item in pk_fields) {
      tbl <- item[1]
      pk <- item[2]
      if (!has_table(tbl)) next
      total <- get_row_count(tbl)
      if (total == 0L) next

      dup_count <- as.integer(DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) - COUNT(DISTINCT %s) AS n FROM %s", pk, tbl))$n[1])
      pct <- round((dup_count / total * 100.0), 2)
      results[[length(results) + 1]] <- list(
        check_id = sprintf("FIELD_PK_UNIQUE_%s_%s", toupper(tbl), toupper(pk)),
        check_name = "isPrimaryKey",
        check_level = "FIELD",
        cdm_table_name = tbl,
        cdm_field_name = pk,
        check_description = sprintf("Verifies that primary key %s in table %s is unique.", pk, tbl),
        num_violated_rows = dup_count,
        pct_violated_rows = pct,
        num_denominator_rows = total,
        execution_status = if (dup_count == 0L) "PASSED" else "FAILED",
        failed = dup_count > 0L
      )
    }

    # Foreign Key Integrity: person_id in occurrence tables
    fk_tables <- c("observation_period", "visit_occurrence", "condition_occurrence",
                   "drug_exposure", "procedure_occurrence", "measurement")
    if (has_table("person")) {
      for (tbl in fk_tables) {
        if (!has_table(tbl)) next
        total <- get_row_count(tbl)
        if (total == 0L) next
        orphan_count <- as.integer(DBI::dbGetQuery(con, sprintf("
          SELECT COUNT(*) AS n FROM %s t
          LEFT JOIN person p ON t.person_id = p.person_id
          WHERE p.person_id IS NULL
        ", tbl))$n[1])
        pct <- round((orphan_count / total * 100.0), 2)
        results[[length(results) + 1]] <- list(
          check_id = sprintf("FIELD_FK_PERSON_%s", toupper(tbl)),
          check_name = "fkPerson",
          check_level = "FIELD",
          cdm_table_name = tbl,
          cdm_field_name = "person_id",
          check_description = sprintf("The number and percent of records in %s with a person_id that does not exist in person.", tbl),
          num_violated_rows = orphan_count,
          pct_violated_rows = pct,
          num_denominator_rows = total,
          execution_status = if (orphan_count == 0L) "PASSED" else "FAILED",
          failed = orphan_count > 0L
        )
      }
    }
  }

  # 3. CONCEPT CHECKS
  if ("CONCEPT" %in% check_levels && has_table("concept") && get_row_count("concept") > 0L) {
    concept_fields <- list(
      c("condition_occurrence", "condition_concept_id"),
      c("drug_exposure", "drug_concept_id"),
      c("procedure_occurrence", "procedure_concept_id"),
      c("measurement", "measurement_concept_id")
    )
    for (item in concept_fields) {
      tbl <- item[1]
      fld <- item[2]
      if (!has_table(tbl)) next
      total <- get_row_count(tbl)
      if (total == 0L) next

      non_std_count <- as.integer(DBI::dbGetQuery(con, sprintf("
        SELECT COUNT(*) AS n FROM %s t
        JOIN concept c ON t.%s = c.concept_id
        WHERE t.%s != 0 AND (c.standard_concept != 'S' OR c.standard_concept IS NULL)
      ", tbl, fld, fld))$n[1])
      pct <- round((non_std_count / total * 100.0), 2)
      results[[length(results) + 1]] <- list(
        check_id = sprintf("CONCEPT_STANDARD_%s_%s", toupper(tbl), toupper(fld)),
        check_name = "standardConceptRecordCompleteness",
        check_level = "CONCEPT",
        cdm_table_name = tbl,
        cdm_field_name = fld,
        check_description = sprintf("The number and percent of non-zero concept records in %s.%s that map to a non-standard concept.", tbl, fld),
        num_violated_rows = non_std_count,
        pct_violated_rows = pct,
        num_denominator_rows = total,
        execution_status = if (non_std_count == 0L) "PASSED" else "FAILED",
        failed = non_std_count > 0L
      )
    }
  }

  # 4. TEMPORAL CHECKS
  if ("TEMPORAL" %in% check_levels) {
    span_tables <- list(
      c("observation_period", "observation_period_start_date", "observation_period_end_date"),
      c("visit_occurrence", "visit_start_date", "visit_end_date"),
      c("condition_occurrence", "condition_start_date", "condition_end_date"),
      c("drug_exposure", "drug_exposure_start_date", "drug_exposure_end_date")
    )
    for (item in span_tables) {
      tbl <- item[1]
      start_f <- item[2]
      end_f <- item[3]
      if (!has_table(tbl)) next
      total <- get_row_count(tbl)
      if (total == 0L) next

      viol_cnt <- as.integer(DBI::dbGetQuery(con, sprintf("
        SELECT COUNT(*) AS n FROM %s
        WHERE %s IS NOT NULL AND %s IS NOT NULL AND %s > %s
      ", tbl, start_f, end_f, start_f, end_f))$n[1])
      pct <- round((viol_cnt / total * 100.0), 2)
      results[[length(results) + 1]] <- list(
        check_id = sprintf("TEMPORAL_START_BEFORE_END_%s", toupper(tbl)),
        check_name = "plausibleTemporalAfter",
        check_level = "TEMPORAL",
        cdm_table_name = tbl,
        cdm_field_name = sprintf("%s, %s", start_f, end_f),
        check_description = sprintf("The number and percent of records in %s where %s occurs after %s.", tbl, start_f, end_f),
        num_violated_rows = viol_cnt,
        pct_violated_rows = pct,
        num_denominator_rows = total,
        execution_status = if (viol_cnt == 0L) "PASSED" else "FAILED",
        failed = viol_cnt > 0L
      )
    }

    if (has_table("person")) {
      event_tables <- list(
        c("condition_occurrence", "condition_start_date"),
        c("drug_exposure", "drug_exposure_start_date"),
        c("visit_occurrence", "visit_start_date"),
        c("procedure_occurrence", "procedure_date"),
        c("measurement", "measurement_date")
      )
      for (item in event_tables) {
        tbl <- item[1]
        dt_f <- item[2]
        if (!has_table(tbl)) next
        total <- get_row_count(tbl)
        if (total == 0L) next

        pre_birth_cnt <- as.integer(DBI::dbGetQuery(con, sprintf("
          SELECT COUNT(*) AS n FROM %s t
          JOIN person p ON t.person_id = p.person_id
          WHERE t.%s < make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1))
        ", tbl, dt_f))$n[1])
        pct <- round((pre_birth_cnt / total * 100.0), 2)
        results[[length(results) + 1]] <- list(
          check_id = sprintf("TEMPORAL_BIRTH_BEFORE_EVENT_%s", toupper(tbl)),
          check_name = "plausibleTemporalBirth",
          check_level = "TEMPORAL",
          cdm_table_name = tbl,
          cdm_field_name = dt_f,
          check_description = sprintf("The number and percent of records in %s with an event date preceding patient birth date.", tbl),
          num_violated_rows = pre_birth_cnt,
          pct_violated_rows = pct,
          num_denominator_rows = total,
          execution_status = if (pre_birth_cnt == 0L) "PASSED" else "FAILED",
          failed = pre_birth_cnt > 0L
        )
      }
    }
  }

  total_checks <- length(results)
  failed_checks <- sum(vapply(results, function(r) isTRUE(r$failed), logical(1)))
  passed_checks <- total_checks - failed_checks
  pct_passed <- if (total_checks > 0) round((passed_checks / total_checks * 100.0), 2) else 100.0

  dqd_res <- list(
    Metadata = list(
      cdm_version = cdm_version,
      dqa_tool = "omop-duck-db native DQD engine",
      execution_timestamp = format(as.POSIXlt(Sys.time(), "UTC"), "%Y-%m-%dT%H:%M:%SZ")
    ),
    Overview = list(
      count_total = as.integer(total_checks),
      count_passed = as.integer(passed_checks),
      count_failed = as.integer(failed_checks),
      percent_passed = pct_passed
    ),
    CheckResults = results
  )

  if (!is.null(output_json)) {
    if (requireNamespace("jsonlite", quietly = TRUE)) {
      dir.create(dirname(normalizePath(output_json, mustWork = FALSE)), showWarnings = FALSE, recursive = TRUE)
      jsonlite::write_json(dqd_res, output_json, pretty = TRUE, auto_unbox = TRUE)
    }
  }

  dqd_res
}
