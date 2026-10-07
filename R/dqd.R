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

# ------------------------------------------------------------------------------------------
# sanitize_measurements: physiologic-range and outlier sanitization (RFC 6.1)
# ------------------------------------------------------------------------------------------
.SANITIZE_METHODS <- c("dqd_biologic_limits", "winsorize_iqr", "z_score_cutoff")
.SANITIZE_ACTIONS <- c("nullify", "clamp", "drop_row")
# Statistical methods need at least this many finite values in a (concept, unit) group.
.SANITIZE_MIN_GROUP_ROWS <- 3L
# z_score_cutoff computes its statistics on values divided by the group's largest magnitude when that
# magnitude exceeds this, so that squaring a ~1e154+ artifact cannot overflow the variance. Below it the
# divisor is exactly 1.0 and the arithmetic is unchanged.
.SANITIZE_Z_RESCALE_ABOVE <- "1e100"
.SANITIZE_REQUIRED_MEASUREMENT_COLS <- c(
  "measurement_id", "person_id", "measurement_concept_id", "measurement_date",
  "unit_concept_id", "value_as_number"
)
# Passed through when the measurement table has them (needed to aggregate per visit downstream).
.SANITIZE_OPTIONAL_MEASUREMENT_COLS <- c("measurement_datetime", "visit_occurrence_id")

#' @keywords internal
#' @noRd
.sanitize_ident <- function(x) paste0('"', gsub('"', '""', x, fixed = TRUE), '"')

# Quote a (possibly schema.table / catalog.schema.table) table name.
#' @keywords internal
#' @noRd
.sanitize_table_ref <- function(name, arg) {
  if (!is.character(name) || length(name) != 1L || is.na(name) || !nzchar(trimws(name))) {
    stop(sprintf("%s must be a non-empty table or view name; got %s", arg, deparse(name)), call. = FALSE)
  }
  name <- trimws(name)
  parts <- strsplit(name, ".", fixed = TRUE)[[1]]
  if (grepl("^\\.|\\.$|\\.\\.", name) || length(parts) > 3L) {
    stop(sprintf("%s must look like 'table', 'schema.table' or 'catalog.schema.table'; got %s", arg, deparse(name)),
         call. = FALSE)
  }
  paste(vapply(parts, .sanitize_ident, character(1)), collapse = ".")
}

# Round-trip-exact decimal text for a finite number (identical to the Python implementation).
#' @keywords internal
#' @noRd
.sanitize_num <- function(x) sprintf("%.17g", as.numeric(x))

#' @keywords internal
#' @noRd
.sanitize_positive <- function(value, arg) {
  if (!is.numeric(value) || length(value) != 1L || !is.finite(value) || value <= 0) {
    stop(sprintf("%s must be a single positive, finite number; got %s", arg, deparse(value)), call. = FALSE)
  }
  as.numeric(value)
}

# Strict whole number: integers and integral doubles are accepted; logical, text, NA and fractions are not.
#' @keywords internal
#' @noRd
.sanitize_int <- function(value, what) {
  if (!is.numeric(value) || length(value) != 1L || !is.finite(value) || value != floor(value)) {
    stop(sprintf("%s must be a whole number; got %s", what, deparse(value)), call. = FALSE)
  }
  as.numeric(value)
}

#' @keywords internal
#' @noRd
.sanitize_concept_ids <- function(values) {
  if (!is.numeric(values)) {
    stop(sprintf("measurement_concept_ids must be a vector of integer concept ids; got %s", deparse(values)),
         call. = FALSE)
  }
  if (length(values) == 0L) {
    stop("measurement_concept_ids is empty; pass NULL to sanitize every concept", call. = FALSE)
  }
  ids <- vapply(as.list(values), .sanitize_int, numeric(1), what = "measurement_concept_ids entries")
  sort(unique(ids))
}

# A limit bound: a finite number, or missing (NA/NaN) for 'no bound on this side'.
#' @keywords internal
#' @noRd
.sanitize_bound <- function(value, what) {
  if (length(value) != 1L || is.na(value)) return(NA_real_)
  num <- if (is.numeric(value) || is.logical(value)) as.numeric(value) else suppressWarnings(as.numeric(value))
  if (is.na(num)) stop(sprintf("%s must be numeric or missing; got %s", what, deparse(value)), call. = FALSE)
  if (!is.finite(num)) {
    stop(sprintf("%s must be finite (use a missing value for an open bound); got %s", what, deparse(value)),
         call. = FALSE)
  }
  num
}

# Validate a user `limits` frame -> data.frame(concept_id, unit_concept_id [NA = any unit], min_value, max_value).
#' @keywords internal
#' @noRd
.sanitize_limit_rows <- function(limits) {
  if (!is.data.frame(limits)) {
    if (!is.list(limits)) {
      stop("limits must be a data.frame with columns concept_id, min_value, max_value (and optionally unit_concept_id)",
           call. = FALSE)
    }
    limits <- as.data.frame(limits, stringsAsFactors = FALSE)
  }
  cols <- stats::setNames(names(limits), tolower(names(limits)))
  missing_cols <- setdiff(c("concept_id", "min_value", "max_value"), names(cols))
  if (length(missing_cols) > 0L) {
    stop(sprintf("limits is missing required column(s) %s; got columns %s",
                 paste(sprintf("'%s'", missing_cols), collapse = ", "),
                 paste(sprintf("'%s'", names(limits)), collapse = ", ")), call. = FALSE)
  }
  n <- nrow(limits)
  units <- if ("unit_concept_id" %in% names(cols)) limits[[cols[["unit_concept_id"]]]] else rep(NA_real_, n)
  out <- data.frame(concept_id = numeric(n), unit_concept_id = numeric(n),
                    min_value = numeric(n), max_value = numeric(n))
  for (i in seq_len(n)) {
    where <- sprintf("limits row %d", i)
    cid <- limits[[cols[["concept_id"]]]][i]
    if (is.na(cid)) stop(sprintf("%s: concept_id is missing", where), call. = FALSE)
    out$concept_id[i] <- .sanitize_int(cid, sprintf("%s: concept_id", where))
    uid <- units[i]
    out$unit_concept_id[i] <- if (is.na(uid)) NA_real_ else .sanitize_int(uid, sprintf("%s: unit_concept_id", where))
    lo <- .sanitize_bound(limits[[cols[["min_value"]]]][i], sprintf("%s: min_value", where))
    hi <- .sanitize_bound(limits[[cols[["max_value"]]]][i], sprintf("%s: max_value", where))
    if (is.na(lo) && is.na(hi)) {
      stop(sprintf("%s: min_value and max_value are both missing, so it would limit nothing", where), call. = FALSE)
    }
    if (!is.na(lo) && !is.na(hi) && lo > hi) {
      stop(sprintf("%s: min_value (%s) is greater than max_value (%s)", where, format(lo), format(hi)), call. = FALSE)
    }
    key <- paste(out$concept_id[seq_len(i - 1L)], out$unit_concept_id[seq_len(i - 1L)])
    if (paste(out$concept_id[i], out$unit_concept_id[i]) %in% key) {
      stop(sprintf("%s: duplicate (concept_id, unit_concept_id) = (%s, %s)", where, format(out$concept_id[i]),
                   format(out$unit_concept_id[i])), call. = FALSE)
    }
    out$min_value[i] <- lo
    out$max_value[i] <- hi
  }
  out
}

# The bundled table inst/extdata/physiologic_limits.csv -> data.frame(concept_id, unit_concept_id, min_value, max_value).
#' @keywords internal
#' @noRd
.sanitize_bundled_limits <- function(path = NULL) {
  # `path` overrides the bundled file location (used by the tests to feed a malformed table)
  if (is.null(path)) {
    path <- system.file("extdata", "physiologic_limits.csv", package = "omopduckdb")
    if (!nzchar(path) || !file.exists(path)) {
      path <- file.path(getwd(), "inst", "extdata", "physiologic_limits.csv")
    }
    if (!file.exists(path)) {
      stop("Bundled physiologic limits table not found: expected inst/extdata/physiologic_limits.csv in the ",
           "omopduckdb package (or in the working directory's inst/ when running from a source checkout).",
           call. = FALSE)
    }
  } else if (!file.exists(path)) {
    stop(sprintf("Physiologic limits table not found: %s", path), call. = FALSE)
  }
  # read.csv, not read.table: names contain commas and '#'. Everything is read as text and parsed strictly.
  raw <- utils::read.csv(path, stringsAsFactors = FALSE, colClasses = "character", check.names = FALSE,
                         comment.char = "", encoding = "UTF-8")
  need <- c("concept_id", "unit_concept_id", "min_value", "max_value")
  absent <- setdiff(need, names(raw))
  if (length(absent) > 0L) {
    stop(sprintf("%s is missing column(s) %s", path, paste(sprintf("'%s'", absent), collapse = ", ")), call. = FALSE)
  }
  if (nrow(raw) == 0L) stop(sprintf("%s contains no limits", path), call. = FALSE)
  num <- lapply(raw[need], function(x) suppressWarnings(as.numeric(x)))
  where <- sprintf("%s line %d", path, seq_len(nrow(raw)) + 1L)
  bad <- Reduce(`|`, lapply(num, is.na))
  if (any(bad)) {
    stop(sprintf("%s: non-numeric concept_id/unit_concept_id/min_value/max_value", where[which(bad)[1]]), call. = FALSE)
  }
  out <- data.frame(concept_id = num$concept_id, unit_concept_id = num$unit_concept_id,
                    min_value = num$min_value, max_value = num$max_value)
  bad <- !(is.finite(out$min_value) & is.finite(out$max_value) & out$min_value < out$max_value)
  if (any(bad)) {
    stop(sprintf("%s: min_value must be finite and less than max_value", where[which(bad)[1]]), call. = FALSE)
  }
  dup <- duplicated(out[c("concept_id", "unit_concept_id")])
  if (any(dup)) {
    stop(sprintf("%s: duplicate (concept_id, unit_concept_id) = (%s, %s)", where[which(dup)[1]],
                 format(out$concept_id[which(dup)[1]]), format(out$unit_concept_id[which(dup)[1]])), call. = FALSE)
  }
  out
}

# Bundled table with the user's rows applied on top. A user row naming a unit replaces the bundled row for
# that (concept, unit); a user row without a unit is an any-unit limit that replaces every bundled row of
# its concept.
#' @keywords internal
#' @noRd
.sanitize_effective_limits <- function(user_rows) {
  bundled <- .sanitize_bundled_limits()
  if (is.null(user_rows) || nrow(user_rows) == 0L) return(bundled)
  any_unit <- user_rows$concept_id[is.na(user_rows$unit_concept_id)]
  with_unit <- !is.na(user_rows$unit_concept_id)
  by_unit <- paste(user_rows$concept_id[with_unit], user_rows$unit_concept_id[with_unit])
  keep <- !(bundled$concept_id %in% any_unit) & !(paste(bundled$concept_id, bundled$unit_concept_id) %in% by_unit)
  rbind(bundled[keep, , drop = FALSE], user_rows)
}

# `lim` CTE: the limits as an inline literal (no temporary table, nothing registered).
#' @keywords internal
#' @noRd
.sanitize_limits_cte <- function(rows) {
  int_lit <- function(v) ifelse(is.na(v), "NULL", sprintf("'%.0f'", v))
  num_lit <- function(v) ifelse(is.na(v), "NULL", paste0("'", vapply(v, .sanitize_num, character(1)), "'"))
  values <- paste0("(", int_lit(rows$concept_id), ", ", int_lit(rows$unit_concept_id), ", ",
                   num_lit(rows$min_value), ", ", num_lit(rows$max_value), ")", collapse = ",\n      ")
  paste0(
    "lim AS (\n",
    "  SELECT CAST(concept_id AS BIGINT) AS concept_id, CAST(unit_concept_id AS BIGINT) AS unit_concept_id,\n",
    "         CAST(min_value AS DOUBLE) AS min_value, CAST(max_value AS DOUBLE) AS max_value\n",
    "  FROM (VALUES\n      ", values, "\n  ) AS v(concept_id, unit_concept_id, min_value, max_value)\n",
    ")"
  )
}

# Compose list(rows_sql, report_sql); both share one classification CTE chain. Every row of the in-scope
# measurements gets a sanitize_status and a cleaned value in SQL, so the rows and the per-concept report are
# computed by the same text and always reconcile.
#' @keywords internal
#' @noRd
.sanitize_build_sql <- function(method, action, extras, cohort_ref, person_ref, concept_ids, limit_rows,
                                iqr_multiplier, z_threshold) {
  where <- "m.value_as_number IS NOT NULL"
  if (!is.null(cohort_ref)) {
    where <- c(where, sprintf("m.person_id IN (SELECT TRY_CAST(c.%s AS BIGINT) FROM %s AS c)", person_ref, cohort_ref))
  }
  if (!is.null(concept_ids)) {
    where <- c(where, paste0("m.measurement_concept_id IN (", paste(sprintf("%.0f", concept_ids), collapse = ", "), ")"))
  }
  extra_select <- paste0(sprintf(", m.%s", vapply(extras, .sanitize_ident, character(1))), collapse = "")
  ctes <- c(paste0(
    "base AS (\n",
    "  SELECT m.measurement_id, m.person_id, m.measurement_concept_id, m.measurement_date,\n",
    "         m.unit_concept_id, CAST(m.value_as_number AS DOUBLE) AS value_raw", extra_select, "\n",
    "  FROM measurement AS m\n",
    "  WHERE ", paste(where, collapse = " AND "), "\n",
    ")"
  ))

  if (method == "dqd_biologic_limits") {
    ctes <- c(
      ctes,
      .sanitize_limits_cte(limit_rows),
      "lim_unit AS (SELECT * FROM lim WHERE unit_concept_id IS NOT NULL)",
      "lim_any AS (SELECT * FROM lim WHERE unit_concept_id IS NULL)",
      "lim_concept AS (SELECT DISTINCT concept_id FROM lim)",
      # a unit-specific row wins over an any-unit row; a concept with limits only in other units is
      # 'unit_skipped', a concept with no limit row at all is 'no_limit'
      paste0(
        "joined AS (\n",
        "  SELECT b.*,\n",
        "         CASE WHEN lu.concept_id IS NOT NULL THEN lu.min_value ELSE la.min_value END AS lo,\n",
        "         CASE WHEN lu.concept_id IS NOT NULL THEN lu.max_value ELSE la.max_value END AS hi,\n",
        "         (lu.concept_id IS NOT NULL OR la.concept_id IS NOT NULL) AS limit_applies,\n",
        "         (lc.concept_id IS NOT NULL) AS concept_has_limit\n",
        "  FROM base AS b\n",
        "  LEFT JOIN lim_unit AS lu ON lu.concept_id = b.measurement_concept_id",
        " AND lu.unit_concept_id = b.unit_concept_id\n",
        "  LEFT JOIN lim_any AS la ON la.concept_id = b.measurement_concept_id\n",
        "  LEFT JOIN lim_concept AS lc ON lc.concept_id = b.measurement_concept_id\n",
        ")"
      ),
      paste0(
        "cls AS (\n",
        "  SELECT joined.*, CASE\n",
        "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n",
        "    WHEN NOT limit_applies THEN CASE WHEN concept_has_limit THEN 'unit_skipped' ELSE 'no_limit' END\n",
        "    WHEN lo IS NOT NULL AND value_raw < lo THEN 'below_min'\n",
        "    WHEN hi IS NOT NULL AND value_raw > hi THEN 'above_max'\n",
        "    ELSE 'ok' END AS sanitize_status\n",
        "  FROM joined\n",
        ")"
      )
    )
  } else if (method == "winsorize_iqr") {
    k <- sprintf("CAST('%s' AS DOUBLE)", .sanitize_num(iqr_multiplier))
    usable <- sprintf("s.s_n >= %d AND s.s_q3 > s.s_q1", .SANITIZE_MIN_GROUP_ROWS)
    ctes <- c(
      ctes,
      # statistics come from the finite in-scope values only; concept 0 (unmapped) is never pooled
      paste0(
        "stats AS (\n",
        "  SELECT measurement_concept_id AS s_concept_id, unit_concept_id AS s_unit_concept_id,\n",
        "         COUNT(*) AS s_n, quantile_cont(value_raw, 0.25) AS s_q1, quantile_cont(value_raw, 0.75) AS s_q3\n",
        "  FROM base\n",
        "  WHERE isfinite(value_raw) AND measurement_concept_id <> 0\n",
        "  GROUP BY measurement_concept_id, unit_concept_id\n",
        ")"
      ),
      paste0(
        "joined AS (\n",
        "  SELECT b.*, CASE WHEN ", usable, " THEN s.s_q1 - ", k, " * (s.s_q3 - s.s_q1) END AS lo,\n",
        "         CASE WHEN ", usable, " THEN s.s_q3 + ", k, " * (s.s_q3 - s.s_q1) END AS hi\n",
        "  FROM base AS b\n",
        "  LEFT JOIN stats AS s ON s.s_concept_id = b.measurement_concept_id",
        " AND s.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n",
        ")"
      ),
      paste0(
        "cls AS (\n",
        "  SELECT joined.*, CASE\n",
        "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n",
        "    WHEN measurement_concept_id = 0 THEN 'no_limit'\n",
        "    WHEN lo IS NULL THEN 'insufficient_data'\n",
        "    WHEN value_raw < lo THEN 'below_min'\n",
        "    WHEN value_raw > hi THEN 'above_max'\n",
        "    ELSE 'ok' END AS sanitize_status\n",
        "  FROM joined\n",
        ")"
      )
    )
  } else {
    k <- sprintf("CAST('%s' AS DOUBLE)", .sanitize_num(z_threshold))
    usable <- sprintf("s.s_n >= %d AND s.s_sd > 0", .SANITIZE_MIN_GROUP_ROWS)
    ctes <- c(
      ctes,
      # statistics come from the finite in-scope values only; concept 0 (unmapped) is never pooled.
      # Mean and sd are taken on value / s_scale (s_scale is 1.0 unless the group holds a value above
      # 1e100) because DuckDB raises on STDDEV_SAMP overflow, and a single 1e160 artifact would abort
      # the whole call. The z test is made on the scaled value against the scaled fences; lo/hi are the
      # unscaled fences, only used to clamp (a flagged value's fence is always finite).
      paste0(
        "zscale AS (\n",
        "  SELECT measurement_concept_id AS s_concept_id, unit_concept_id AS s_unit_concept_id,\n",
        "         COUNT(*) AS s_n,\n",
        "         CASE WHEN MAX(abs(value_raw)) > ", .SANITIZE_Z_RESCALE_ABOVE, " THEN MAX(abs(value_raw)) ",
        "ELSE 1.0 END AS s_scale\n",
        "  FROM base\n",
        "  WHERE isfinite(value_raw) AND measurement_concept_id <> 0\n",
        "  GROUP BY measurement_concept_id, unit_concept_id\n",
        ")"
      ),
      paste0(
        "stats AS (\n",
        "  SELECT z.s_concept_id, z.s_unit_concept_id, z.s_n, z.s_scale,\n",
        "         avg(b.value_raw / z.s_scale) AS s_mean, stddev_samp(b.value_raw / z.s_scale) AS s_sd\n",
        "  FROM base AS b\n",
        "  JOIN zscale AS z ON z.s_concept_id = b.measurement_concept_id",
        " AND z.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n",
        "  WHERE isfinite(b.value_raw)\n",
        "  GROUP BY z.s_concept_id, z.s_unit_concept_id, z.s_n, z.s_scale\n",
        ")"
      ),
      paste0(
        "joined AS (\n",
        "  SELECT b.*, s.s_scale,\n",
        "         CASE WHEN ", usable, " THEN s.s_mean - ", k, " * s.s_sd END AS lo_s,\n",
        "         CASE WHEN ", usable, " THEN s.s_mean + ", k, " * s.s_sd END AS hi_s\n",
        "  FROM base AS b\n",
        "  LEFT JOIN stats AS s ON s.s_concept_id = b.measurement_concept_id",
        " AND s.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n",
        ")"
      ),
      paste0(
        "cls AS (\n",
        "  SELECT joined.*, lo_s * s_scale AS lo, hi_s * s_scale AS hi, CASE\n",
        "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n",
        "    WHEN measurement_concept_id = 0 THEN 'no_limit'\n",
        "    WHEN lo_s IS NULL THEN 'insufficient_data'\n",
        "    WHEN value_raw / s_scale < lo_s THEN 'below_min'\n",
        "    WHEN value_raw / s_scale > hi_s THEN 'above_max'\n",
        "    ELSE 'ok' END AS sanitize_status\n",
        "  FROM joined\n",
        ")"
      )
    )
  }

  if (action == "clamp") {
    out_of_range <- "CASE WHEN sanitize_status = 'below_min' THEN lo ELSE hi END"
    # +/-Inf go to the nearest finite bound when there is one; NaN (and any value with no finite bound,
    # e.g. an IQR fence that overflowed) is nullified
    invalid <- paste0("CASE WHEN isinf(value_raw) AND value_raw > 0 AND isfinite(hi) THEN hi ",
                      "WHEN isinf(value_raw) AND value_raw < 0 AND isfinite(lo) THEN lo ELSE NULL END")
  } else {
    out_of_range <- invalid <- "NULL"
  }
  ctes <- c(ctes, paste0(
    "fin AS (\n",
    "  SELECT cls.*,\n",
    "         (sanitize_status IN ('below_min', 'above_max', 'invalid_number')) AS flagged,\n",
    "         CASE WHEN sanitize_status IN ('below_min', 'above_max') THEN ", out_of_range, "\n",
    "              WHEN sanitize_status = 'invalid_number' THEN ", invalid, "\n",
    "              ELSE value_raw END AS value_clean\n",
    "  FROM cls\n",
    ")"
  ))
  with_clause <- paste0("WITH ", paste(ctes, collapse = ",\n"))

  out_cols <- paste0(
    "measurement_id, person_id, measurement_concept_id, measurement_date, unit_concept_id, ",
    "value_clean AS value_as_number, value_raw AS value_as_number_raw, sanitize_status",
    paste0(sprintf(", %s", vapply(extras, .sanitize_ident, character(1))), collapse = "")
  )
  rows_sql <- paste0(
    with_clause, "\nSELECT ", out_cols, "\nFROM fin\n",
    if (action == "drop_row") "WHERE NOT flagged\n" else "",
    "ORDER BY measurement_id, person_id, measurement_concept_id, measurement_date"
  )

  n_of <- function(status) sprintf("COUNT(*) FILTER (WHERE sanitize_status = '%s')", status)
  if (action == "drop_row") {
    changed <- "CAST(0 AS BIGINT)"
    dropped <- "COUNT(*) FILTER (WHERE flagged)"
  } else {
    changed <- "COUNT(*) FILTER (WHERE flagged)"
    dropped <- "CAST(0 AS BIGINT)"
  }
  report_sql <- paste0(
    with_clause, "\n",
    "SELECT measurement_concept_id, unit_concept_id, COUNT(*) AS n,\n",
    "       ", n_of("ok"), " AS n_ok, ", n_of("below_min"), " AS n_below, ", n_of("above_max"), " AS n_above,\n",
    "       ", n_of("invalid_number"), " AS n_invalid, ", changed, " AS n_changed, ", dropped, " AS n_dropped,\n",
    "       ", n_of("no_limit"), " AS n_no_limit, ", n_of("unit_skipped"), " AS n_unit_skipped,\n",
    "       ", n_of("insufficient_data"), " AS n_insufficient_data\n",
    "FROM fin\n",
    "GROUP BY measurement_concept_id, unit_concept_id\n",
    "ORDER BY measurement_concept_id, unit_concept_id NULLS LAST"
  )
  list(rows_sql = rows_sql, report_sql = report_sql)
}

# Warn when most of a limit-bearing concept's rows lack a unit and so were not screened at all. The package's
# own ETL writes unit_concept_id = 0 for lab results, and "dqd_biologic_limits" never guesses a unit, so such
# labs come back as "unit_skipped" with their artifacts intact.
#' @keywords internal
#' @noRd
.sanitize_warn_unscreened <- function(report) {
  if (nrow(report) == 0L) return(invisible(NULL))
  no_unit <- is.na(report$unit_concept_id) | report$unit_concept_id == 0L
  by_concept <- split(seq_len(nrow(report)), report$measurement_concept_id)
  unscreened <- vapply(by_concept, function(i) {
    2 * sum(report$n_unit_skipped[i][no_unit[i]]) > sum(report$n[i])
  }, logical(1))
  bad <- sort(as.numeric(names(by_concept))[unscreened])
  if (length(bad) == 0L) return(invisible(NULL))
  shown <- paste0(paste(sprintf("%.0f", utils::head(bad, 5L)), collapse = ", "),
                  if (length(bad) > 5L) sprintf(", ... (%d in all)", length(bad)) else "")
  warning(sprintf(paste0(
    "sanitize_measurements: more than half of the measurements of %d concept(s) with physiologic limits have ",
    "no unit (unit_concept_id is NULL or 0) and were NOT screened (sanitize_status 'unit_skipped'): concept_id ",
    "%s. Map the units in the ETL, or pass `limits` rows without unit_concept_id to apply a limit whatever the unit."),
    length(bad), shown), call. = FALSE)
  invisible(NULL)
}

#' Sanitize implausible numeric measurements (physiologic limits or statistical outliers)
#'
#' Screens `measurement.value_as_number` for artifacts such as a systolic blood pressure of 0 or 999,
#' negative lab values, or NaN/Inf, and returns the numeric measurements with a sanitized value, the raw
#' value and a per-row status (RFC 6.1). Everything is computed in DuckDB SQL (no temporary or persistent
#' objects are created and the database is only read), so it works on a read-only connection and gives the
#' same result as `omop_etl.sanitize_measurements()` in Python.
#'
#' @details
#' **Methods.** `"dqd_biologic_limits"` compares each value with the bundled per-concept, per-unit
#' plausibility limits (`inst/extdata/physiologic_limits.csv`, see
#' `system.file("extdata", "physiologic_limits.csv", package = "omopduckdb")`) or with `limits`.
#' `"winsorize_iqr"` flags values outside the Tukey fences `Q1 - k*IQR .. Q3 + k*IQR` of their
#' (concept, unit) group (`k = iqr_multiplier`). `"z_score_cutoff"` flags values with `|z| > z_threshold`
#' using the group mean and sample standard deviation.
#'
#' **Actions.** `"nullify"` sets a flagged value to `NA`; `"clamp"` moves it to the violated bound (for
#' `"winsorize_iqr"` the fence, for `"z_score_cutoff"` `mean +/- z_threshold * sd`); `"drop_row"` removes the
#' row from the result.
#'
#' **Invalid numbers.** `NaN` and `+/-Inf` are invalid under every method, whether or not a limit exists
#' (`sanitize_status = "invalid_number"`). `"nullify"` and `"drop_row"` behave as usual. `"clamp"` moves
#' `+Inf`/`-Inf` to the group's upper/lower bound when there is one, and falls back to `NA` for `NaN` and
#' whenever no finite bound applies.
#'
#' **Inclusive bounds.** A value equal to a bound is `"ok"`.
#'
#' **Units (`"dqd_biologic_limits"`).** A limit row applies only when the measurement's `unit_concept_id`
#' equals the row's. A concept that has limits only in other units, or a measurement with a `NULL`/0 unit,
#' is left untouched and reported as `"unit_skipped"` (a creatinine in umol/L is never compared with the
#' mg/dL bounds). A concept with no limit row at all is `"no_limit"`.
#'
#' **Custom limits.** A `limits` row with a `unit_concept_id` replaces the bundled row for that concept
#' and unit (or adds one). A row without a unit (`NA`, or no such column) is an any-unit limit: it replaces
#' every bundled row of the concept and applies whatever unit the measurement has, including `NULL`/0. A
#' missing `min_value` or `max_value` leaves that side open. A unit-specific row wins over an any-unit row
#' of the same concept. Passing `limits` with another method warns and is ignored.
#'
#' **Statistical methods.** Statistics are computed per `(measurement_concept_id, unit_concept_id)` (a
#' `NULL` unit is its own group) over the finite, in-scope values, i.e. after the cohort and concept
#' restrictions. A group with fewer than 3 finite values, a zero IQR (`"winsorize_iqr"`) or a zero or
#' undefined standard deviation (`"z_score_cutoff"`) has no usable spread: its values are kept and reported
#' as `"insufficient_data"`. `measurement_concept_id = 0` (unmapped) is never pooled into a group and is
#' reported as `"no_limit"`. Quartiles use linear interpolation (DuckDB `quantile_cont`, the same as R's
#' `quantile(type = 7)` and numpy's default).
#'
#' The default `"dqd_biologic_limits"` never guesses a unit, so an ETL that leaves `unit_concept_id` at 0 (this
#' package's PCORnet ETL does for lab results: the text stays in `unit_source_value`) gets no screening of
#' those rows. A warning is raised when more than half of the measurements of a concept with limits are skipped
#' for that reason: map the units in the ETL, or pass `limits` rows without `unit_concept_id` (an any-unit
#' limit) for the concepts you want screened.
#'
#' **Choosing an action.** `"nullify"` (or `"drop_row"`) is the right action for the artifacts
#' `"dqd_biologic_limits"` finds: a blood pressure of 0 or 999 is a sentinel, not an extreme measurement, and
#' `"clamp"` would turn it into 40 or 300, a plausible-looking value that downstream models treat as real. Use
#' `"clamp"` (winsorization) only for values that are extreme but real.
#'
#' **Skewed data and masking.** `"winsorize_iqr"` and `"z_score_cutoff"` assume a roughly symmetric
#' distribution. Right-skewed labs (creatinine, ALT, CRP, ...) have a long valid tail, so clinically important
#' values (an AKI creatinine of 3-6 mg/dL) can be nullified or clamped: prefer `"dqd_biologic_limits"` for
#' such labs, or transform them first. `"z_score_cutoff"` is also not robust: the outliers themselves inflate
#' the mean and standard deviation, and with a sample standard deviation no value of a group of `n` can have
#' `|z|` above `(n - 1) / sqrt(n)`, so the default `z_threshold = 4` cannot flag anything in a group of fewer
#' than 18 values.
#'
#' **Report.** `attr(x, "sanitization_report")` is a data.frame with one row per `measurement_concept_id`
#' and `unit_concept_id` and columns `measurement_concept_id`, `unit_concept_id`, `n`, `n_ok`, `n_below`,
#' `n_above`, `n_invalid`, `n_changed`, `n_dropped`, `n_no_limit`, `n_unit_skipped` and
#' `n_insufficient_data`. It counts every in-scope row, including dropped ones, and reconciles exactly:
#' `n = n_ok + n_below + n_above + n_invalid + n_no_limit + n_unit_skipped + n_insufficient_data` and
#' `n_changed + n_dropped = n_below + n_above + n_invalid`.
#'
#' @param con Active DuckDB DBI connection to the OMOP CDM database (read-only is fine).
#' @param cohort_table Optional table or view (`"name"` or `"schema.name"`) restricting the scope to the
#'   persons listed in its `person_col`. Duplicate cohort rows do not duplicate measurements. `NULL` =
#'   everyone.
#' @param method One of `"dqd_biologic_limits"` (default), `"winsorize_iqr"` or `"z_score_cutoff"`.
#' @param action One of `"nullify"` (default), `"clamp"` or `"drop_row"`.
#' @param measurement_concept_ids Optional integer vector: only sanitize these `measurement_concept_id`
#'   values. `NULL` = all concepts.
#' @param limits Optional data.frame of custom limits for `"dqd_biologic_limits"` with columns
#'   `concept_id`, `min_value`, `max_value` and optionally `unit_concept_id` (names are case-insensitive,
#'   extra columns are ignored). See Details.
#' @param iqr_multiplier Fence width `k` for `"winsorize_iqr"` (default 3; 1.5 is the classic Tukey fence).
#'   Must be positive and finite.
#' @param z_threshold Cut-off for `"z_score_cutoff"` (default 4). Must be positive and finite.
#' @param person_col Person id column of `cohort_table` (default `"subject_id"`). It must exist; there is no
#'   fallback to another column.
#' @return A `data.frame` with one row per in-scope measurement (rows whose `value_as_number` is `NULL` are
#'   not part of the result), ordered by `measurement_id`, with columns `measurement_id`, `person_id`,
#'   `measurement_concept_id`, `measurement_date`, `unit_concept_id`, `value_as_number` (sanitized),
#'   `value_as_number_raw`, `sanitize_status` and, when the measurement table has them,
#'   `measurement_datetime` and `visit_occurrence_id`. `sanitize_status` is one of `"ok"`, `"below_min"`,
#'   `"above_max"`, `"invalid_number"`, `"no_limit"`, `"unit_skipped"` or `"insufficient_data"`. The
#'   per-concept report is attached as `attr(x, "sanitization_report")`.
#' @examples
#' con <- DBI::dbConnect(duckdb::duckdb())
#' DBI::dbExecute(con, "
#'   CREATE TABLE measurement AS SELECT * FROM (VALUES
#'     (1, 1, 3004249, DATE '2021-01-01', 8876, 120.0),
#'     (2, 1, 3004249, DATE '2021-01-02', 8876, 999.0),
#'     (3, 2, 3004249, DATE '2021-01-03', 8876, 0.0),
#'     (4, 2, 3016723, DATE '2021-01-04', 8749, 88.0)
#'   ) AS t(measurement_id, person_id, measurement_concept_id, measurement_date, unit_concept_id,
#'          value_as_number)")
#' clean <- sanitize_measurements(con)
#' clean[, c("measurement_id", "value_as_number", "value_as_number_raw", "sanitize_status")]
#' attr(clean, "sanitization_report")
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' @export
sanitize_measurements <- function(con,
                                  cohort_table = NULL,
                                  method = "dqd_biologic_limits",
                                  action = "nullify",
                                  measurement_concept_ids = NULL,
                                  limits = NULL,
                                  iqr_multiplier = 3.0,
                                  z_threshold = 4.0,
                                  person_col = "subject_id") {
  choice <- function(value, allowed, arg) {
    v <- if (is.character(value) && length(value) == 1L && !is.na(value)) tolower(trimws(value)) else NA_character_
    if (is.na(v) || !v %in% allowed) {
      stop(sprintf("%s must be one of %s; got %s", arg, paste0("[", paste(sprintf("'%s'", allowed), collapse = ", "), "]"),
                   deparse(value)), call. = FALSE)
    }
    v
  }
  method_k <- choice(method, .SANITIZE_METHODS, "method")
  action_k <- choice(action, .SANITIZE_ACTIONS, "action")
  k_iqr <- .sanitize_positive(iqr_multiplier, "iqr_multiplier")
  k_z <- .sanitize_positive(z_threshold, "z_threshold")
  concept_ids <- if (is.null(measurement_concept_ids)) NULL else .sanitize_concept_ids(measurement_concept_ids)

  limit_rows <- NULL
  if (method_k == "dqd_biologic_limits") {
    user_rows <- if (is.null(limits)) NULL else .sanitize_limit_rows(limits)
    limit_rows <- .sanitize_effective_limits(user_rows)
  } else if (!is.null(limits)) {
    warning(sprintf("limits is only used by method='dqd_biologic_limits' and is ignored for method='%s'.", method_k),
            call. = FALSE)
  }

  describe_cols <- function(table_sql) {
    DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", table_sql))$column_name
  }
  meas_cols <- tryCatch(
    describe_cols("measurement"),
    error = function(e) {
      stop("sanitize_measurements() needs a CDM 'measurement' table on this connection: ", conditionMessage(e),
           call. = FALSE)
    }
  )
  meas_map <- stats::setNames(meas_cols, tolower(meas_cols))
  absent <- setdiff(.SANITIZE_REQUIRED_MEASUREMENT_COLS, names(meas_map))
  if (length(absent) > 0L) {
    stop(sprintf("The measurement table is missing required column(s) %s",
                 paste0("[", paste(sprintf("'%s'", absent), collapse = ", "), "]")), call. = FALSE)
  }
  extras <- unname(meas_map[intersect(.SANITIZE_OPTIONAL_MEASUREMENT_COLS, names(meas_map))])

  cohort_ref <- person_ref <- NULL
  if (!is.null(cohort_table)) {
    cohort_ref <- .sanitize_table_ref(cohort_table, "cohort_table")
    if (!is.character(person_col) || length(person_col) != 1L || is.na(person_col) || !nzchar(trimws(person_col))) {
      stop(sprintf("person_col must be a non-empty column name; got %s", deparse(person_col)), call. = FALSE)
    }
    cohort_cols <- tryCatch(
      describe_cols(cohort_ref),
      error = function(e) {
        stop(sprintf("cohort_table '%s' could not be read: %s", cohort_table, conditionMessage(e)), call. = FALSE)
      }
    )
    cohort_map <- stats::setNames(cohort_cols, tolower(cohort_cols))
    key <- tolower(trimws(person_col))
    if (!key %in% names(cohort_map)) {
      stop(sprintf("person_col '%s' not found in cohort_table '%s'. Available columns: %s. Pass person_col = ... explicitly.",
                   person_col, cohort_table, paste0("[", paste(sprintf("'%s'", cohort_cols), collapse = ", "), "]")),
           call. = FALSE)
    }
    person_ref <- .sanitize_ident(cohort_map[[key]])
  }

  sql <- .sanitize_build_sql(
    method = method_k, action = action_k, extras = extras, cohort_ref = cohort_ref, person_ref = person_ref,
    concept_ids = concept_ids, limit_rows = limit_rows, iqr_multiplier = k_iqr, z_threshold = k_z
  )
  out <- DBI::dbGetQuery(con, sql$rows_sql)
  report <- DBI::dbGetQuery(con, sql$report_sql)
  count_cols <- setdiff(names(report), c("measurement_concept_id", "unit_concept_id"))
  report[count_cols] <- lapply(report[count_cols], as.integer)
  if (method_k == "dqd_biologic_limits") .sanitize_warn_unscreened(report)
  attr(out, "sanitization_report") <- report
  out
}
