#' Ensure Standard OMOP Cohort Tables Exist
#'
#' Creates `cohort` and `cohort_definition` tables in the active DuckDB connection
#' if they are not already present.
#'
#' @param con Active DuckDB DBI connection.
#' @return Invisibly NULL.
#' @importFrom stats setNames
#' @export
ensure_cohort_tables <- function(con) {
  DBI::dbExecute(con, "
    CREATE TABLE IF NOT EXISTS cohort (
        cohort_definition_id BIGINT NOT NULL,
        subject_id BIGINT NOT NULL,
        cohort_start_date DATE NOT NULL,
        cohort_end_date DATE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cohort_definition (
        cohort_definition_id BIGINT NOT NULL,
        cohort_definition_name VARCHAR(255) NOT NULL,
        cohort_definition_description VARCHAR,
        definition_type_concept_id INTEGER,
        cohort_definition_syntax VARCHAR,
        subject_concept_id INTEGER,
        cohort_initiation_date DATE
    );
  ")
  invisible(NULL)
}

#' Resolve Concept Descendants from Hierarchy Transitive Closure
#'
#' Queries `concept_ancestor` to resolve all standard descendants of the specified
#' ancestor concept(s).
#'
#' @param con Active DuckDB DBI connection.
#' @param ancestor_concept_ids Integer vector of ancestor concept IDs.
#' @param include_self Logical. If `TRUE` (default), includes 0-level self mappings.
#' @param min_levels_of_separation Integer. Minimum hierarchy separation level (default 0).
#' @param max_levels_of_separation Integer or NULL. Maximum hierarchy separation level.
#' @return A data.frame containing descendant concept details.
#' @export
get_concept_descendants <- function(con,
                                    ancestor_concept_ids,
                                    include_self = TRUE,
                                    min_levels_of_separation = 0,
                                    max_levels_of_separation = NULL) {
  ancestor_concept_ids <- as.integer(ancestor_concept_ids)
  if (length(ancestor_concept_ids) == 0) {
    return(data.frame(
      ancestor_concept_id = integer(0),
      descendant_concept_id = integer(0),
      min_levels_of_separation = integer(0),
      max_levels_of_separation = integer(0),
      concept_name = character(0),
      vocabulary_id = character(0),
      concept_code = character(0),
      standard_concept = character(0)
    ))
  }

  in_clause <- paste(ancestor_concept_ids, collapse = ", ")
  max_filter <- if (!is.null(max_levels_of_separation)) {
    sprintf("AND ca.max_levels_of_separation <= %d", as.integer(max_levels_of_separation))
  } else {
    ""
  }
  self_filter <- if (isTRUE(include_self)) "" else "AND ca.ancestor_concept_id != ca.descendant_concept_id"

  query <- sprintf("
    SELECT 
        ca.ancestor_concept_id,
        ca.descendant_concept_id,
        ca.min_levels_of_separation,
        ca.max_levels_of_separation,
        c.concept_name,
        c.vocabulary_id,
        c.concept_code,
        c.standard_concept
    FROM concept_ancestor ca
    JOIN concept c ON ca.descendant_concept_id = c.concept_id
    WHERE ca.ancestor_concept_id IN (%s)
      AND ca.min_levels_of_separation >= %d
      %s
      %s
    ORDER BY ca.ancestor_concept_id, ca.min_levels_of_separation, ca.descendant_concept_id;
  ", in_clause, as.integer(min_levels_of_separation), max_filter, self_filter)

  DBI::dbGetQuery(con, query)
}

#' Resolve Concept Ancestors from Hierarchy Transitive Closure
#'
#' Navigates upward through polyhierarchies in `concept_ancestor` to find broader concept classes.
#'
#' @param con Active DuckDB DBI connection.
#' @param descendant_concept_ids Integer vector of descendant concept IDs.
#' @param include_self Logical. If `TRUE` (default), includes 0-level self mappings.
#' @param min_levels_of_separation Integer. Minimum hierarchy separation level (default 0).
#' @param max_levels_of_separation Integer or NULL. Maximum hierarchy separation level.
#' @return A data.frame containing ancestor concept details.
#' @export
get_concept_ancestors <- function(con,
                                  descendant_concept_ids,
                                  include_self = TRUE,
                                  min_levels_of_separation = 0,
                                  max_levels_of_separation = NULL) {
  descendant_concept_ids <- as.integer(descendant_concept_ids)
  if (length(descendant_concept_ids) == 0) {
    return(data.frame(
      descendant_concept_id = integer(0),
      ancestor_concept_id = integer(0),
      min_levels_of_separation = integer(0),
      max_levels_of_separation = integer(0),
      concept_name = character(0),
      vocabulary_id = character(0),
      concept_code = character(0),
      standard_concept = character(0)
    ))
  }

  in_clause <- paste(descendant_concept_ids, collapse = ", ")
  max_filter <- if (!is.null(max_levels_of_separation)) {
    sprintf("AND ca.max_levels_of_separation <= %d", as.integer(max_levels_of_separation))
  } else {
    ""
  }
  self_filter <- if (isTRUE(include_self)) "" else "AND ca.ancestor_concept_id != ca.descendant_concept_id"

  query <- sprintf("
    SELECT 
        ca.descendant_concept_id,
        ca.ancestor_concept_id,
        ca.min_levels_of_separation,
        ca.max_levels_of_separation,
        c.concept_name,
        c.vocabulary_id,
        c.concept_code,
        c.standard_concept
    FROM concept_ancestor ca
    JOIN concept c ON ca.ancestor_concept_id = c.concept_id
    WHERE ca.descendant_concept_id IN (%s)
      AND ca.min_levels_of_separation >= %d
      %s
      %s
    ORDER BY ca.descendant_concept_id, ca.min_levels_of_separation, ca.ancestor_concept_id;
  ", in_clause, as.integer(min_levels_of_separation), max_filter, self_filter)

  DBI::dbGetQuery(con, query)
}

#' Resolve Concept Set Algebra (ATLAS-Compatible)
#'
#' Evaluates concept set expressions with inclusive and exclusive lists and optional descendant expansion.
#'
#' @param con Active DuckDB DBI connection.
#' @param include_concepts Integer vector of concept IDs to include.
#' @param exclude_concepts Optional integer vector of concept IDs to exclude.
#' @param include_descendants Logical. If `TRUE` (default), expands included concepts to their descendants.
#' @param exclude_descendants Logical. If `TRUE` (default), expands excluded concepts to their descendants.
#' @return Sorted integer vector of resolved standard concept IDs.
#' @export
resolve_concept_set <- function(con,
                                include_concepts,
                                exclude_concepts = NULL,
                                include_descendants = TRUE,
                                exclude_descendants = TRUE) {
  include_concepts <- as.integer(include_concepts)
  if (length(include_concepts) == 0) return(integer(0))

  inc_clause <- paste(include_concepts, collapse = ", ")

  inc_query <- if (isTRUE(include_descendants)) {
    sprintf("
      SELECT descendant_concept_id AS concept_id 
      FROM concept_ancestor 
      WHERE ancestor_concept_id IN (%s)
      UNION
      SELECT concept_id FROM concept WHERE concept_id IN (%s)
    ", inc_clause, inc_clause)
  } else {
    sprintf("SELECT concept_id FROM concept WHERE concept_id IN (%s)", inc_clause)
  }

  if (!is.null(exclude_concepts) && length(exclude_concepts) > 0) {
    exclude_concepts <- as.integer(exclude_concepts)
    exc_clause <- paste(exclude_concepts, collapse = ", ")
    exc_query <- if (isTRUE(exclude_descendants)) {
      sprintf("
        SELECT descendant_concept_id AS concept_id 
        FROM concept_ancestor 
        WHERE ancestor_concept_id IN (%s)
        UNION
        SELECT concept_id FROM concept WHERE concept_id IN (%s)
      ", exc_clause, exc_clause)
    } else {
      sprintf("SELECT concept_id FROM concept WHERE concept_id IN (%s)", exc_clause)
    }
    final_query <- sprintf("(%s) EXCEPT (%s) ORDER BY 1;", inc_query, exc_query)
  } else {
    final_query <- sprintf("(%s) ORDER BY 1;", inc_query)
  }

  res <- DBI::dbGetQuery(con, final_query)
  if (nrow(res) == 0) integer(0) else as.integer(res[[1]])
}

#' Materialize an OMOP Cohort
#'
#' Registers a cohort definition in `cohort_definition` and inserts qualifying subject
#' spans into `cohort`.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Integer unique identifier for this cohort.
#' @param cohort_name Character display name for the cohort.
#' @param cohort_description Optional character description.
#' @param entry_sql SQL string returning candidate events with `person_id` (or `subject_id`)
#'   and `start_date` (or `cohort_start_date`), and optionally `end_date`.
#' @param exit_rule Character, either `"fixed_days"` (default) or `"from_query"`.
#' @param exit_offset_days Integer days added to `cohort_start_date` when `exit_rule = "fixed_days"`.
#' @return Integer count of inserted cohort records.
#' @export
create_cohort <- function(con,
                          cohort_id,
                          cohort_name,
                          cohort_description = NULL,
                          entry_sql = NULL,
                          exit_rule = "fixed_days",
                          exit_offset_days = 0) {
  ensure_cohort_tables(con)
  cohort_id <- as.integer(cohort_id)
  name_esc <- gsub("'", "''", cohort_name)
  desc_esc <- gsub("'", "''", cohort_description %||% "")

  DBI::dbExecute(con, sprintf("DELETE FROM cohort_definition WHERE cohort_definition_id = %d;", cohort_id))
  DBI::dbExecute(con, sprintf("
    INSERT INTO cohort_definition (
        cohort_definition_id,
        cohort_definition_name,
        cohort_definition_description,
        definition_type_concept_id,
        cohort_definition_syntax,
        subject_concept_id,
        cohort_initiation_date
    ) VALUES (
        %d, '%s', '%s', 0, NULL, 0, DATE '%s'
    );
  ", cohort_id, name_esc, desc_esc, as.character(Sys.Date())))

  DBI::dbExecute(con, sprintf("DELETE FROM cohort WHERE cohort_definition_id = %d;", cohort_id))

  if (is.null(entry_sql) || nchar(trimws(entry_sql)) == 0) {
    return(0L)
  }

  cols_info <- DBI::dbGetQuery(con, sprintf("DESCRIBE (%s)", entry_sql))
  actual_cols <- setNames(cols_info$column_name, toupper(cols_info$column_name))

  subj_col <- NULL
  for (cand in c("SUBJECT_ID", "PERSON_ID", "PATID")) {
    if (cand %in% names(actual_cols)) {
      subj_col <- actual_cols[[cand]]
      break
    }
  }
  if (is.null(subj_col)) subj_col <- cols_info$column_name[1]

  start_col <- NULL
  for (cand in c("COHORT_START_DATE", "START_DATE", "INDEX_DATE", "CONDITION_START_DATE", "DRUG_EXPOSURE_START_DATE", "PROCEDURE_DATE", "VISIT_START_DATE")) {
    if (cand %in% names(actual_cols)) {
      start_col <- actual_cols[[cand]]
      break
    }
  }
  if (is.null(start_col)) start_col <- if (nrow(cols_info) > 1) cols_info$column_name[2] else cols_info$column_name[1]

  end_col <- NULL
  for (cand in c("COHORT_END_DATE", "END_DATE", "CONDITION_END_DATE", "DRUG_EXPOSURE_END_DATE", "VISIT_END_DATE")) {
    if (cand %in% names(actual_cols)) {
      end_col <- actual_cols[[cand]]
      break
    }
  }

  end_date_expr <- if (exit_rule == "fixed_days") {
    sprintf("CAST(\"%s\" AS DATE) + %d", start_col, as.integer(exit_offset_days))
  } else if (!is.null(end_col)) {
    sprintf("COALESCE(CAST(\"%s\" AS DATE), CAST(\"%s\" AS DATE))", end_col, start_col)
  } else {
    sprintf("CAST(\"%s\" AS DATE)", start_col)
  }

  insert_sql <- sprintf("
    INSERT INTO cohort (
        cohort_definition_id,
        subject_id,
        cohort_start_date,
        cohort_end_date
    )
    SELECT 
        %d AS cohort_definition_id,
        CAST(\"%s\" AS BIGINT) AS subject_id,
        CAST(\"%s\" AS DATE) AS cohort_start_date,
        %s AS cohort_end_date
    FROM (%s) raw_entry
    WHERE \"%s\" IS NOT NULL AND \"%s\" IS NOT NULL;
  ", cohort_id, subj_col, start_col, end_date_expr, entry_sql, subj_col, start_col)

  n <- DBI::dbExecute(con, insert_sql)
  as.integer(n)
}

#' Track Patient Attrition Across Phenotyping Rules (CONSORT Flowchart)
#'
#' Applies sequential filtering criteria step-by-step and computes patient retention metrics.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Integer cohort ID being evaluated.
#' @param steps A list of named steps, each containing `step_name` (character) and `step_query` (character SQL).
#'   Each query after step 1 can query `_step_current` to filter down from the previous step.
#' @return A data.frame with columns: `step_number`, `step_name`, `subjects_retained`, `subjects_dropped`, `percent_retained`.
#' @export
compute_attrition <- function(con, cohort_id, steps) {
  tryCatch(DBI::dbExecute(con, "DROP TABLE IF EXISTS _step_current;"), error = function(e) NULL)

  step_numbers <- integer(length(steps))
  step_names <- character(length(steps))
  subjects_retained <- integer(length(steps))
  subjects_dropped <- integer(length(steps))
  percent_retained <- numeric(length(steps))

  initial_count <- NULL
  prev_count <- NULL

  for (i in seq_along(steps)) {
    step_item <- steps[[i]]
    s_name <- if (is.list(step_item)) step_item[[1]] else names(steps)[i]
    s_query <- if (is.list(step_item)) step_item[[2]] else step_item

    if (i == 1) {
      DBI::dbExecute(con, sprintf("CREATE TEMPORARY TABLE _step_current AS %s;", s_query))
    } else {
      DBI::dbExecute(con, sprintf("CREATE TEMPORARY TABLE _step_next AS %s;", s_query))
      DBI::dbExecute(con, "DROP TABLE _step_current;")
      DBI::dbExecute(con, "ALTER TABLE _step_next RENAME TO _step_current;")
    }

    step_cols_info <- DBI::dbGetQuery(con, "DESCRIBE _step_current")
    step_actual_cols <- setNames(step_cols_info$column_name, toupper(step_cols_info$column_name))
    step_subj <- step_actual_cols[["SUBJECT_ID"]] %||% step_actual_cols[["PERSON_ID"]] %||% step_actual_cols[["PATID"]] %||% step_cols_info$column_name[1]

    cnt_res <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(DISTINCT \"%s\") AS cnt FROM _step_current", step_subj))
    cur_count <- as.integer(cnt_res$cnt[1])

    if (i == 1) {
      initial_count <- cur_count
      prev_count <- cur_count
      dropped <- 0L
      pct <- if (initial_count > 0) 100.0 else 0.0
    } else {
      dropped <- max(0L, prev_count - cur_count)
      pct <- if (!is.null(initial_count) && initial_count > 0) round((cur_count / initial_count) * 100, 2) else 0.0
      prev_count <- cur_count
    }

    step_numbers[i] <- as.integer(i)
    step_names[i] <- as.character(s_name)
    subjects_retained[i] <- cur_count
    subjects_dropped[i] <- dropped
    percent_retained[i] <- pct
  }

  tryCatch(DBI::dbExecute(con, "DROP TABLE IF EXISTS _step_current;"), error = function(e) NULL)

  data.frame(
    step_number = step_numbers,
    step_name = step_names,
    subjects_retained = subjects_retained,
    subjects_dropped = subjects_dropped,
    percent_retained = percent_retained,
    stringsAsFactors = FALSE
  )
}

#' Combine Cohorts via Set Operations
#'
#' Combines two cohorts using `UNION`, `INTERSECT`, or `DIFFERENCE`.
#'
#' @param con Active DuckDB DBI connection.
#' @param new_cohort_id Target cohort ID.
#' @param cohort_id_a First source cohort ID.
#' @param cohort_id_b Second source cohort ID.
#' @param operation Character: `"UNION"`, `"INTERSECT"`, or `"DIFFERENCE"`.
#' @param name Optional display name.
#' @param description Optional description.
#' @return Integer count of inserted records.
#' @export
combine_cohorts <- function(con,
                            new_cohort_id,
                            cohort_id_a,
                            cohort_id_b,
                            operation = "UNION",
                            name = NULL,
                            description = NULL) {
  ensure_cohort_tables(con)
  op <- toupper(trimws(operation))
  if (!op %in% c("UNION", "INTERSECT", "DIFFERENCE", "EXCEPT")) {
    stop("Unsupported operation '", operation, "'. Use UNION, INTERSECT, or DIFFERENCE.")
  }
  sql_op <- if (op == "DIFFERENCE") "EXCEPT" else op
  name <- name %||% sprintf("Cohort %d (%s of %d and %d)", as.integer(new_cohort_id), op, as.integer(cohort_id_a), as.integer(cohort_id_b))

  entry_sql <- sprintf("
    SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = %d
    %s
    SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = %d
  ", as.integer(cohort_id_a), sql_op, as.integer(cohort_id_b))

  create_cohort(
    con,
    cohort_id = new_cohort_id,
    cohort_name = name,
    cohort_description = description,
    entry_sql = entry_sql,
    exit_rule = "from_query"
  )
}

#' Get Cohort Summary Statistics
#'
#' Returns summary metrics (total subjects, total spans, min/max start dates, mean duration).
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Optional integer cohort definition ID to filter.
#' @return A data.frame with summary metrics.
#' @export
get_cohort_summary <- function(con, cohort_id = NULL) {
  ensure_cohort_tables(con)
  filter_clause <- if (!is.null(cohort_id)) sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

  query <- sprintf("
    SELECT 
        c.cohort_definition_id,
        COALESCE(cd.cohort_definition_name, 'Cohort ' || c.cohort_definition_id) AS cohort_name,
        COUNT(DISTINCT c.subject_id) AS total_subjects,
        COUNT(*) AS total_records,
        MIN(c.cohort_start_date) AS min_start_date,
        MAX(c.cohort_start_date) AS max_start_date,
        ROUND(AVG(c.cohort_end_date - c.cohort_start_date), 1) AS mean_duration_days
    FROM cohort c
    LEFT JOIN cohort_definition cd ON c.cohort_definition_id = cd.cohort_definition_id
    %s
    GROUP BY c.cohort_definition_id, cd.cohort_definition_name
    ORDER BY c.cohort_definition_id;
  ", filter_clause)

  DBI::dbGetQuery(con, query)
}

# ------------------------------------------------------------------------------------------------------
# Study-cohort engine
#
# One parameterised SQL generator shared by define_study_cohort(), build_readmission_cohort() and
# build_end_of_life_cohort(). The two legacy builders are thin wrappers that pin the conventions they have
# always used (see .STUDY_READMISSION_CONVENTIONS / .STUDY_END_OF_LIFE_CONVENTIONS); the golden-file tests in
# tests/testthat/test-define-study-cohort.R guarantee their results did not change. Mirrors the Python engine
# in python/omop_etl/cohort.py function for function.
# ------------------------------------------------------------------------------------------------------

.STUDY_VISIT_TYPES <- c(inpatient = 9201, emergency = 9203, outpatient = 9202)
.STUDY_SAMPLING_RULES <- c("first", "last", "random")
.STUDY_AGE_METHODS <- c("year_difference", "completed_years")
.STUDY_DEATH_SOURCES <- c("death_table", "discharge_disposition")
.STUDY_FOLLOWUP_EVIDENCE <- c("observation_period", "visit", "measurement", "condition", "drug", "death")
.STUDY_MORTALITY_TYPES <- c("in_hospital", "post_discharge", "fixed_window", "composite_readmit_or_death")
.STUDY_MORTALITY_FAMILIES <- c("in_hospital", "post_discharge", "fixed_window", "composite")
.STUDY_TMP_TABLE <- "_study_cohort_tmp"

# Conventions of build_readmission_cohort(): only the index stay's own discharge disposition ('Patient died')
# and the death table count as death; follow-up is evidenced by later encounters or clinical records.
.STUDY_READMISSION_CONVENTIONS <- list(
  death_ids = 4216643,
  death_sources = "death_table",
  evidence = c("visit", "measurement", "condition", "drug")
)
# Conventions of build_end_of_life_cohort(): death is the earliest date across the death table and every visit
# discharged to a death concept; follow-up is also evidenced by observation periods and by a death at/after the
# horizon. NOTE: 4155309 is kept for backwards compatibility only. In the Athena vocabulary it is 'Ileal part'
# (an anatomic site), while this package's PCORnet ETL writes it for 'discharged against medical advice'. Pass
# death_discharge_concept_ids = 4216643 to count only 'Patient died'.
.STUDY_END_OF_LIFE_CONVENTIONS <- list(
  death_ids = c(4216643, 4155309),
  death_sources = .STUDY_DEATH_SOURCES,
  evidence = .STUDY_FOLLOWUP_EVIDENCE
)
# outcome family -> min_los_days (NULL = no restriction) and exclude_in_hospital_death used when "auto" is passed
.STUDY_AUTO_MIN_LOS <- list(none = 1, readmission = 1, post_discharge = 1, composite = 1, in_hospital = 0, fixed_window = NULL)
.STUDY_AUTO_EXCLUDE <- list(none = TRUE, readmission = TRUE, post_discharge = TRUE, composite = TRUE,
                            in_hospital = FALSE, fixed_window = FALSE)

# Fully resolved, validated definition handed to the SQL generators. NULL means "not applied" for visit_ids,
# study_start, study_end, min_age, min_los_days, window_days and schema.
#' @keywords internal
#' @noRd
.new_study_spec <- function(cohort_id, outcome, label, schema = NULL, visit_ids = 9201,
                            outcome_visit_ids = 9201, study_start = NULL, study_end = NULL, min_age = 18,
                            age_method = "year_difference", min_los_days = 1, washin_days = 365,
                            exclude_death = TRUE, death_ids = 4216643, death_sources = "death_table",
                            window_days = 30, gap_days = 0, verified = TRUE,
                            evidence = .STUDY_FOLLOWUP_EVIDENCE, rule = "random", seed = 42) {
  list(cohort_id = cohort_id, outcome = outcome, label = label, schema = schema, visit_ids = visit_ids,
       outcome_visit_ids = outcome_visit_ids, study_start = study_start, study_end = study_end,
       min_age = min_age, age_method = age_method, min_los_days = min_los_days, washin_days = washin_days,
       exclude_death = exclude_death, death_ids = death_ids, death_sources = death_sources,
       window_days = window_days, gap_days = gap_days, verified = verified, evidence = evidence,
       rule = rule, seed = seed)
}

# ---- identifier / literal helpers --------------------------------------------------------------------

#' @keywords internal
#' @noRd
.sql_str <- function(x) paste0("'", gsub("'", "''", as.character(x), fixed = TRUE), "'")

#' @keywords internal
#' @noRd
.sql_int <- function(x) sprintf("%.0f", as.numeric(x))

#' @keywords internal
#' @noRd
.int_list <- function(ids) paste(.sql_int(ids), collapse = ", ")

#' Validates dot-separated plain identifiers and returns them double-quoted.
#' @keywords internal
#' @noRd
.quote_ident_path <- function(name, what, max_parts) {
  if (!is.character(name) || length(name) != 1L || is.na(name) || !nzchar(name)) {
    stop(sprintf("%s must be a non-empty string, got %s.", what, paste(format(name), collapse = " ")), call. = FALSE)
  }
  parts <- strsplit(name, ".", fixed = TRUE)[[1]]
  if (length(parts) > max_parts || length(parts) == 0L || !all(grepl("^[A-Za-z_][A-Za-z0-9_]*$", parts))) {
    stop(sprintf("Invalid %s '%s': use up to %d dot-separated names made of letters, digits and underscores.",
                 what, name, max_parts), call. = FALSE)
  }
  paste0('"', parts, '"', collapse = ".")
}

#' @keywords internal
#' @noRd
.study_tbl <- function(spec, name) {
  if (is.null(spec$schema)) name else paste0(.quote_ident_path(spec$schema, "schema", 2L), ".", name)
}

# ---- SQL generators ----------------------------------------------------------------------------------

#' @keywords internal
#' @noRd
.study_order_by <- function(rule, seed) {
  if (rule == "random") {
    sprintf("hash(f.visit_occurrence_id, %s), f.visit_occurrence_id", .sql_int(seed))
  } else if (rule == "first") {
    "f.visit_start_date ASC, f.visit_occurrence_id ASC"
  } else if (rule == "last") {
    "f.visit_start_date DESC, f.visit_occurrence_id DESC"
  } else {
    stop(sprintf("Unsupported sampling rule '%s'. Use 'first', 'last', or 'random'.", rule), call. = FALSE)
  }
}

#' @keywords internal
#' @noRd
.study_age_expr <- function(spec) {
  dob <- "make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1))"
  fn <- if (spec$age_method == "year_difference") "date_diff" else "date_sub"
  sprintf("%s('year', %s, v.visit_start_date)", fn, dob)
}

# Date the outcome / follow-up windows are measured from: index start (fixed window) or discharge.
#' @keywords internal
#' @noRd
.study_anchor <- function(spec) if (spec$outcome == "fixed_window") "e.visit_start_date" else "e.visit_end_date"

# Verified follow-up needs a horizon and is not defined for in-hospital outcomes.
#' @keywords internal
#' @noRd
.study_followup_applicable <- function(spec) spec$outcome != "in_hospital" && !is.null(spec$window_days)

# Index-stay eligibility criteria as list(label, sql) pairs (predicates on v / p / pd), in attrition order.
#' @keywords internal
#' @noRd
.study_conditions <- function(spec) {
  conds <- list()
  add <- function(label, sql) conds[[length(conds) + 1L]] <<- list(label = label, sql = sql)

  if (is.null(spec$visit_ids)) {
    add("Any visit type", "1=1")
  } else {
    ids <- .int_list(spec$visit_ids)
    add(sprintf("Index visit type in (%s)", ids), sprintf("v.visit_concept_id IN (%s)", ids))
  }

  if (!is.null(spec$study_start) || !is.null(spec$study_end)) {
    parts <- character(0)
    if (!is.null(spec$study_start)) parts <- c(parts, sprintf("v.visit_start_date >= DATE '%s'", spec$study_start))
    if (!is.null(spec$study_end)) parts <- c(parts, sprintf("v.visit_start_date <= DATE '%s'", spec$study_end))
    label <- if (!is.null(spec$study_start) && !is.null(spec$study_end)) {
      sprintf("Index visit start between %s and %s (inclusive)", spec$study_start, spec$study_end)
    } else if (!is.null(spec$study_start)) {
      sprintf("Index visit start on or after %s", spec$study_start)
    } else {
      sprintf("Index visit start on or before %s", spec$study_end)
    }
    add(label, paste(parts, collapse = " AND "))
  }

  if (!is.null(spec$min_age)) {
    add(sprintf("Age >= %s years at index", .sql_int(spec$min_age)),
        sprintf("%s >= %s", .study_age_expr(spec), .sql_int(spec$min_age)))
  }

  if (!is.null(spec$min_los_days)) {
    n <- .sql_int(spec$min_los_days)
    add(sprintf("Length of stay >= %s day(s)", n),
        sprintf("date_diff('day', v.visit_start_date, v.visit_end_date) >= %s", n))
  }

  if (!is.null(spec$washin_days) && spec$washin_days > 0) {
    w <- .sql_int(spec$washin_days)
    add(sprintf("Prior observation >= %s days (wash-in)", w), sprintf("(
                EXISTS (
                    SELECT 1 FROM %s op
                    WHERE op.person_id = v.person_id
                      AND op.observation_period_start_date <= (v.visit_start_date - %s)
                      AND op.observation_period_end_date >= v.visit_start_date
                )
                OR EXISTS (
                    SELECT 1 FROM %s pv
                    WHERE pv.person_id = v.person_id
                      AND pv.visit_occurrence_id != v.visit_occurrence_id
                      AND pv.visit_start_date <= (v.visit_start_date - %s)
                )
            )", .study_tbl(spec, "observation_period"), w, .study_tbl(spec, "visit_occurrence"), w))
  }

  if (isTRUE(spec$exclude_death)) {
    add("No in-hospital death at index stay",
        sprintf("COALESCE(v.discharged_to_concept_id, 0) NOT IN (%s) AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)",
                .int_list(spec$death_ids)))
  } else {
    add("Alive at index admission", "(pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)")
  }
  conds
}

#' @keywords internal
#' @noRd
.study_death_ctes <- function(spec) {
  selects <- character(0)
  if ("death_table" %in% spec$death_sources) {
    selects <- c(selects, sprintf(
      "SELECT person_id, CAST(death_date AS DATE) AS death_date FROM %s WHERE death_date IS NOT NULL",
      .study_tbl(spec, "death")))
  }
  if ("discharge_disposition" %in% spec$death_sources) {
    selects <- c(selects, sprintf(
      "SELECT person_id, CAST(visit_end_date AS DATE) AS death_date FROM %s WHERE discharged_to_concept_id IN (%s) AND visit_end_date IS NOT NULL",
      .study_tbl(spec, "visit_occurrence"), .int_list(spec$death_ids)))
  }
  sprintf("all_deaths AS (
        %s
    ),
    patient_death AS (
        SELECT person_id, MIN(death_date) AS death_date
        FROM all_deaths
        GROUP BY person_id
    )", paste(selects, collapse = "\n        UNION ALL\n        "))
}

#' @keywords internal
#' @noRd
.study_eligible_select <- function(spec, conditions) {
  where <- paste(vapply(conditions, function(x) x$sql, character(1)), collapse = "\n          AND ")
  sprintf("SELECT
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_concept_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            pd.death_date,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            %s AS age_at_index
        FROM %s v
        JOIN %s p ON v.person_id = p.person_id
        LEFT JOIN patient_death pd ON v.person_id = pd.person_id
        WHERE %s", .study_age_expr(spec), .study_tbl(spec, "visit_occurrence"), .study_tbl(spec, "person"), where)
}

#' @keywords internal
#' @noRd
.study_readmit_predicate <- function(spec) {
  paste0(
    "ro.person_id = e.subject_id ",
    "AND ro.visit_occurrence_id != e.visit_occurrence_id ",
    "AND ro.visit_concept_id IN (", .int_list(spec$outcome_visit_ids), ") ",
    "AND ro.visit_start_date > (e.visit_end_date + ", .sql_int(spec$gap_days), ") ",
    "AND ro.visit_start_date <= (e.visit_end_date + ", .sql_int(spec$window_days), ")"
  )
}

# The outcome_flag / outcome_date select-list items, evaluated per eligible stay (alias e).
#' @keywords internal
#' @noRd
.study_outcome_sql <- function(spec) {
  gap <- .sql_int(spec$gap_days)
  vo <- .study_tbl(spec, "visit_occurrence")
  if (spec$outcome == "none") {
    return("CAST(NULL AS INTEGER) AS outcome_flag, CAST(NULL AS DATE) AS outcome_date")
  }
  if (spec$outcome == "readmission") {
    pred <- .study_readmit_predicate(spec)
    return(paste0(
      sprintf("CASE WHEN EXISTS (SELECT 1 FROM %s ro WHERE %s) THEN 1 ELSE 0 END AS outcome_flag, ", vo, pred),
      sprintf("CAST((SELECT MIN(ro.visit_start_date) FROM %s ro WHERE %s) AS DATE) AS outcome_date", vo, pred)
    ))
  }
  d <- .int_list(spec$death_ids)
  if (spec$outcome == "in_hospital") {
    cond <- sprintf(paste0("(COALESCE(e.discharged_to_concept_id, 0) IN (%s) ",
                           "OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date))"), d)
    return(paste0(
      sprintf("CASE WHEN %s THEN 1 ELSE 0 END AS outcome_flag, ", cond),
      sprintf("CASE WHEN %s THEN CAST(COALESCE(e.death_date, e.visit_end_date) AS DATE) ELSE NULL END AS outcome_date", cond)
    ))
  }
  anchor <- .study_anchor(spec)
  death_in_window <- sprintf(
    "(e.death_date IS NOT NULL AND e.death_date > (%s + %s) AND e.death_date <= (%s + %s))",
    anchor, gap, anchor, .sql_int(spec$window_days))
  if (spec$outcome %in% c("post_discharge", "fixed_window")) {
    return(paste0(
      sprintf("CASE WHEN %s THEN 1 ELSE 0 END AS outcome_flag, ", death_in_window),
      sprintf("CASE WHEN %s THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date", death_in_window)
    ))
  }
  if (spec$outcome == "composite") {
    pred <- .study_readmit_predicate(spec)
    return(paste0(
      sprintf("CASE WHEN (%s OR EXISTS (SELECT 1 FROM %s ro WHERE %s)) THEN 1 ELSE 0 END AS outcome_flag, ",
              death_in_window, vo, pred),
      sprintf("CAST(LEAST(CASE WHEN %s THEN e.death_date ELSE NULL END, (SELECT MIN(ro.visit_start_date) FROM %s ro WHERE %s)) AS DATE) AS outcome_date",
              death_in_window, vo, pred)
    ))
  }
  stop(sprintf("Unsupported outcome family '%s'.", spec$outcome), call. = FALSE)
}

# The has_subsequent_event select-list item: evidence of observation at / after anchor + horizon.
#' @keywords internal
#' @noRd
.study_followup_sql <- function(spec) {
  if (!.study_followup_applicable(spec)) return("CAST(NULL AS INTEGER) AS has_subsequent_event")
  h <- .sql_int(spec$window_days)
  anchor <- .study_anchor(spec)
  t <- function(name) .study_tbl(spec, name)
  clauses <- list(
    observation_period = sprintf(
      "EXISTS (SELECT 1 FROM %s op WHERE op.person_id = e.subject_id AND op.observation_period_end_date >= (%s + %s))",
      t("observation_period"), anchor, h),
    visit = sprintf(
      "EXISTS (SELECT 1 FROM %s sv WHERE sv.person_id = e.subject_id AND sv.visit_occurrence_id != e.visit_occurrence_id AND sv.visit_start_date >= (%s + %s))",
      t("visit_occurrence"), anchor, h),
    measurement = sprintf(
      "EXISTS (SELECT 1 FROM %s sm WHERE sm.person_id = e.subject_id AND sm.measurement_date >= (%s + %s))",
      t("measurement"), anchor, h),
    condition = sprintf(
      "EXISTS (SELECT 1 FROM %s sc WHERE sc.person_id = e.subject_id AND sc.condition_start_date >= (%s + %s))",
      t("condition_occurrence"), anchor, h),
    drug = sprintf(
      "EXISTS (SELECT 1 FROM %s sd WHERE sd.person_id = e.subject_id AND sd.drug_exposure_start_date >= (%s + %s))",
      t("drug_exposure"), anchor, h),
    death = sprintf("(e.death_date IS NOT NULL AND e.death_date >= (%s + %s))", anchor, h)
  )
  keep <- .STUDY_FOLLOWUP_EVIDENCE[.STUDY_FOLLOWUP_EVIDENCE %in% spec$evidence]
  sprintf("CASE WHEN (
                %s
            ) THEN 1 ELSE 0 END AS has_subsequent_event", paste(unlist(clauses[keep]), collapse = "\n                OR "))
}

#' @keywords internal
#' @noRd
.study_verified_sql <- function(spec) {
  if (!.study_followup_applicable(spec)) return("CAST(NULL AS INTEGER)")
  if (spec$outcome == "none") return("CASE WHEN o.has_subsequent_event = 1 THEN 1 ELSE 0 END")
  "CASE WHEN (o.outcome_flag = 1 OR o.has_subsequent_event = 1) THEN 1 ELSE 0 END"
}

# The WITH clause, ending at CTE `through` ("verified_stays" for attrition, "ranked_stays" for the cohort).
#' @keywords internal
#' @noRd
.study_ctes <- function(spec, through = "ranked_stays") {
  ctes <- c(
    .study_death_ctes(spec),
    sprintf("eligible_stays AS (\n        %s\n    )", .study_eligible_select(spec, .study_conditions(spec))),
    sprintf("outcomes_and_followup AS (
        SELECT
            e.*,
            %s,
            %s
        FROM eligible_stays e
    )", .study_outcome_sql(spec), .study_followup_sql(spec)),
    sprintf("verified_stays AS (\n        SELECT o.*, %s AS followup_verified\n        FROM outcomes_and_followup o\n    )",
            .study_verified_sql(spec))
  )
  if (through != "verified_stays") {
    keep <- if (isTRUE(spec$verified) && .study_followup_applicable(spec)) "WHERE followup_verified = 1" else ""
    ctes <- c(
      ctes,
      sprintf("filtered_stays AS (\n        SELECT *\n        FROM verified_stays\n        %s\n    )", keep),
      sprintf("ranked_stays AS (
        SELECT
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id
                ORDER BY %s
            ) AS stay_rank
        FROM filtered_stays f
    )", .study_order_by(spec$rule, spec$seed))
    )
  }
  paste0("WITH ", paste(ctes, collapse = ",\n    "))
}

#' @keywords internal
#' @noRd
.study_query_sql <- function(spec) {
  # paste0() rather than sprintf() for the big WITH clause: sprintf limits the length of substituted strings
  final_select <- sprintf("SELECT
        %s AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        visit_concept_id,
        outcome_flag,
        outcome_date,
        followup_verified,
        age_at_index,
        los_days,
        discharged_to_concept_id,
        %s AS target_outcome
    FROM ranked_stays
    WHERE stay_rank = 1
    ORDER BY subject_id, cohort_start_date", .sql_int(spec$cohort_id), .sql_str(spec$label))
  paste0(.study_ctes(spec), "
    ", final_select)
}

# Cumulative CONSORT steps for compute_attrition(), built from the same predicates as the cohort query.
#' @keywords internal
#' @noRd
.study_attrition_steps <- function(spec) {
  conds <- .study_conditions(spec)
  head <- .study_death_ctes(spec)
  steps <- list()
  for (k in seq_along(conds)) {
    sql <- paste0("WITH ", head, "\n    SELECT subject_id, visit_occurrence_id FROM (\n        ",
                  .study_eligible_select(spec, conds[seq_len(k)]), "\n    ) s")
    steps[[length(steps) + 1L]] <- list(conds[[k]]$label, sql)
  }
  if (isTRUE(spec$verified) && .study_followup_applicable(spec)) {
    anchor_label <- if (spec$outcome == "fixed_window") "index start" else "discharge"
    steps[[length(steps) + 1L]] <- list(
      sprintf("Verified follow-up (evidence >= %s days after %s)", .sql_int(spec$window_days), anchor_label),
      paste0(.study_ctes(spec, through = "verified_stays"),
             "\n    SELECT subject_id, visit_occurrence_id FROM verified_stays WHERE followup_verified = 1")
    )
  }
  steps
}

# ---- materialisation ---------------------------------------------------------------------------------

# Writes the cohort (and optional outcome cohort) held in temp table `tmp` to the OMOP cohort tables.
# outcome_style = "visits" writes one row per qualifying readmission visit (its start/end dates); "event_date"
# writes one point event per patient on outcome_date.
#' @keywords internal
#' @noRd
.materialize_study_cohort <- function(con, spec, tmp, name, description, outcome_cohort_id = NULL,
                                      outcome_name = NULL, outcome_description = NULL,
                                      outcome_style = "visits", overwrite = TRUE) {
  ensure_cohort_tables(con)
  t_cohort <- .study_tbl(spec, "cohort")
  t_def <- .study_tbl(spec, "cohort_definition")
  today <- as.character(Sys.Date())
  cid <- .sql_int(spec$cohort_id)
  ids <- c(cid, if (!is.null(outcome_cohort_id)) .sql_int(outcome_cohort_id))

  if (isTRUE(overwrite)) {
    for (i in ids) {
      DBI::dbExecute(con, sprintf("DELETE FROM %s WHERE cohort_definition_id = %s;", t_cohort, i))
      DBI::dbExecute(con, sprintf("DELETE FROM %s WHERE cohort_definition_id = %s;", t_def, i))
    }
  }

  define <- function(i, nm, desc) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO %s (
          cohort_definition_id, cohort_definition_name, cohort_definition_description,
          definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
      ) VALUES (
          %s, %s, %s, 0, NULL, 0, DATE '%s'
      );
    ", t_def, i, .sql_str(nm), .sql_str(desc), today))
  }

  define(cid, name, description)
  DBI::dbExecute(con, sprintf("
    INSERT INTO %s (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT DISTINCT cohort_definition_id, subject_id, cohort_start_date, cohort_end_date
    FROM %s;
  ", t_cohort, tmp))

  if (!is.null(outcome_cohort_id)) {
    oid <- .sql_int(outcome_cohort_id)
    define(oid, outcome_name, outcome_description)
    if (outcome_style == "visits") {
      DBI::dbExecute(con, sprintf("
        INSERT INTO %s (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT DISTINCT
            %s AS cohort_definition_id,
            tc.subject_id,
            ro.visit_start_date AS cohort_start_date,
            ro.visit_end_date AS cohort_end_date
        FROM %s tc
        JOIN %s ro ON tc.subject_id = ro.person_id
        WHERE tc.outcome_flag = 1
          AND ro.visit_occurrence_id != tc.visit_occurrence_id
          AND ro.visit_concept_id IN (%s)
          AND ro.visit_start_date > (tc.cohort_end_date + %s)
          AND ro.visit_start_date <= (tc.cohort_end_date + %s);
      ", t_cohort, oid, tmp, .study_tbl(spec, "visit_occurrence"), .int_list(spec$outcome_visit_ids),
                                   .sql_int(spec$gap_days), .sql_int(spec$window_days)))
    } else {
      DBI::dbExecute(con, sprintf("
        INSERT INTO %s (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT DISTINCT
            %s AS cohort_definition_id,
            subject_id,
            COALESCE(outcome_date, cohort_end_date) AS cohort_start_date,
            COALESCE(outcome_date, cohort_end_date) AS cohort_end_date
        FROM %s
        WHERE outcome_flag = 1;
      ", t_cohort, oid, tmp))
    }
  }
  invisible(NULL)
}

.STUDY_COLUMNS <- paste0(
  "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, visit_concept_id, ",
  "outcome_flag, outcome_date, followup_verified, age_at_index, los_days, discharged_to_concept_id, target_outcome"
)

# Builds the cohort in a temp table, optionally materialises it, and returns `projection` as a data.frame.
#' @keywords internal
#' @noRd
.run_study_engine <- function(con, spec, projection, materialise, table_name = NULL, materialize_args = list()) {
  table_sql <- if (!is.null(table_name)) .quote_ident_path(table_name, "table_name", 3L) else NULL
  tmp <- .STUDY_TMP_TABLE
  DBI::dbExecute(con, sprintf("CREATE OR REPLACE TEMP TABLE %s AS %s", tmp, .study_query_sql(spec)))
  on.exit({
    if (DBI::dbIsValid(con)) DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s;", tmp))
  }, add = TRUE)

  tryCatch({
    if (isTRUE(materialise)) {
      do.call(.materialize_study_cohort, c(list(con = con, spec = spec, tmp = tmp), materialize_args))
    }
    if (!is.null(table_sql)) {
      DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s;", table_sql))
      DBI::dbExecute(con, sprintf("CREATE TABLE %s AS SELECT %s FROM %s ORDER BY subject_id;", table_sql, projection, tmp))
    }
  }, error = function(e) {
    if (grepl("read-only", conditionMessage(e), ignore.case = TRUE)) {
      stop("Cannot write the cohort: the connection is read-only. Pass materialise = FALSE (and no table_name) ",
           "to build the cohort without writing to the database.", call. = FALSE)
    }
    stop(e)
  })
  DBI::dbGetQuery(con, sprintf("SELECT %s FROM %s ORDER BY subject_id;", projection, tmp))
}

# ---- argument validation (define_study_cohort) -------------------------------------------------------

#' @keywords internal
#' @noRd
.whole_number <- function(value, name, minimum = NULL, allow_null = FALSE) {
  if (is.null(value) || (length(value) == 1L && !is.character(value) && is.na(value))) {
    if (allow_null) return(NULL)
    stop(sprintf("%s must not be NULL.", name), call. = FALSE)
  }
  if (!is.numeric(value) || length(value) != 1L || !is.finite(value) || value != round(value)) {
    stop(sprintf("%s must be a whole number, got %s.", name, paste(format(value), collapse = " ")), call. = FALSE)
  }
  if (!is.null(minimum) && value < minimum) {
    stop(sprintf("%s must be >= %s, got %s.", name, format(minimum), format(value)), call. = FALSE)
  }
  as.numeric(value)
}

#' @keywords internal
#' @noRd
.flag <- function(value, name) {
  if (is.logical(value) && length(value) == 1L && !is.na(value)) return(value)
  stop(sprintf("%s must be TRUE or FALSE, got %s.", name, paste(format(value), collapse = " ")), call. = FALSE)
}

#' @keywords internal
#' @noRd
.materialise_mode <- function(value) {
  if (is.character(value) && length(value) == 1L && !is.na(value) && tolower(trimws(value)) == "auto") return("auto")
  if (is.logical(value) && length(value) == 1L && !is.na(value)) return(value)
  stop(sprintf("materialise must be TRUE, FALSE or 'auto', got %s.", paste(format(value), collapse = " ")), call. = FALSE)
}

# TRUE when the database that holds the cohort tables was opened read-only.
#' @keywords internal
#' @noRd
.study_db_read_only <- function(con, schema) {
  catalog <- if (!is.null(schema) && grepl(".", schema, fixed = TRUE)) strsplit(schema, ".", fixed = TRUE)[[1]][1] else NULL
  res <- if (is.null(catalog)) {
    DBI::dbGetQuery(con, "SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()")
  } else {
    DBI::dbGetQuery(con, sprintf("SELECT readonly FROM duckdb_databases() WHERE database_name = %s", .sql_str(catalog)))
  }
  nrow(res) > 0L && isTRUE(as.logical(res$readonly[[1]]))
}

#' @keywords internal
#' @noRd
.iso_date <- function(value, name) {
  if (is.null(value) || (length(value) == 1L && is.na(value))) return(NULL)
  if (inherits(value, "POSIXt")) value <- as.Date(value)
  if (inherits(value, "Date") && length(value) == 1L) return(format(value, "%Y-%m-%d"))
  if (is.character(value) && length(value) == 1L) {
    v <- trimws(value)
    if (grepl("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", v)) {
      d <- as.Date(v, format = "%Y-%m-%d")
      if (!is.na(d) && format(d, "%Y-%m-%d") == v) return(v)
    }
  }
  stop(sprintf("%s must be a valid date ('YYYY-MM-DD', a Date, or NULL), got %s.", name, paste(format(value), collapse = " ")),
       call. = FALSE)
}

#' @keywords internal
#' @noRd
.resolve_study_window <- function(study_window) {
  if (is.null(study_window)) return(list(start = NULL, end = NULL))
  if (length(study_window) != 2L) {
    stop("study_window must be NULL or a (start, end) pair; either element may be NULL.", call. = FALSE)
  }
  start <- .iso_date(study_window[[1]], "study_window start")
  end <- .iso_date(study_window[[2]], "study_window end")
  if (!is.null(start) && !is.null(end) && as.Date(start) > as.Date(end)) {
    stop(sprintf("study_window start (%s) must not be after its end (%s).", start, end), call. = FALSE)
  }
  list(start = start, end = end)
}

# 'inpatient' / 'emergency' / 'outpatient', visit_concept_ids, or a mix of both.
#' @keywords internal
#' @noRd
.resolve_visit_types <- function(value, name, allow_null) {
  if (is.null(value)) {
    if (allow_null) return(NULL)
    stop(sprintf("%s must not be NULL.", name), call. = FALSE)
  }
  items <- if (is.list(value)) value else as.list(value)
  if (length(items) == 0L) stop(sprintf("%s must not be empty.", name), call. = FALSE)
  out <- numeric(0)
  for (item in items) {
    if (is.character(item) && length(item) == 1L && !is.na(item)) {
      key <- tolower(trimws(item))
      if (key %in% names(.STUDY_VISIT_TYPES)) {
        out <- c(out, unname(.STUDY_VISIT_TYPES[[key]]))
      } else if (grepl("^[0-9]+$", key)) {
        out <- c(out, as.numeric(key))
      } else {
        stop(sprintf("Unknown %s '%s'. Use 'inpatient', 'emergency', 'outpatient', or explicit visit_concept_ids.",
                     name, item), call. = FALSE)
      }
    } else {
      out <- c(out, .whole_number(item, paste(name, "concept id")))
    }
  }
  sort(unique(out))
}

#' @keywords internal
#' @noRd
.resolve_choices <- function(value, allowed, name) {
  cleaned <- tolower(trimws(as.character(unlist(value))))
  if (length(cleaned) == 0L || any(is.na(cleaned)) || !all(cleaned %in% allowed)) {
    stop(sprintf("%s must be a non-empty subset of (%s); got %s.", name, paste(allowed, collapse = ", "),
                 paste(format(unlist(value)), collapse = ", ")), call. = FALSE)
  }
  allowed[allowed %in% cleaned]
}

#' @keywords internal
#' @noRd
.resolve_death_ids <- function(value, name) {
  items <- as.list(unlist(value))
  if (length(items) == 0L) stop(sprintf("%s must not be empty.", name), call. = FALSE)
  sort(unique(vapply(items, function(x) .whole_number(x, paste(name, "concept id")), numeric(1))))
}

# Maps target_outcome (+ mortality_type) to list(outcome = engine family, label = target_outcome value).
#' @keywords internal
#' @noRd
.resolve_outcome <- function(target_outcome, mortality_type, followup_days) {
  if (!is.character(target_outcome) || length(target_outcome) != 1L || is.na(target_outcome)) {
    stop(sprintf("target_outcome must be a string, got %s.", paste(format(target_outcome), collapse = " ")), call. = FALSE)
  }
  t <- tolower(trimws(target_outcome))
  m <- regmatches(t, regexec("^all_cause_readmission(_([0-9]+)d)?$", t))[[1]]
  if (length(m) > 0L) {
    if (nzchar(m[3]) && !is.null(followup_days) && as.numeric(m[3]) != followup_days) {
      stop(sprintf("target_outcome '%s' implies a %s-day window but followup_days = %s; make them agree or use 'all_cause_readmission'.",
                   target_outcome, m[3], .sql_int(followup_days)), call. = FALSE)
    }
    return(list(outcome = "readmission", label = "all_cause_readmission"))
  }
  if (t == "none") return(list(outcome = "none", label = "none"))
  if (t %in% c("readmission_or_death", "composite_readmit_or_death")) {
    return(list(outcome = "composite", label = "readmission_or_death"))
  }
  if (t == "mortality") {
    mt <- tolower(trimws(as.character(mortality_type)))
    if (identical(mt, "composite_readmit_or_death")) return(list(outcome = "composite", label = "readmission_or_death"))
    if (length(mt) != 1L || !mt %in% c("in_hospital", "post_discharge", "fixed_window")) {
      stop(sprintf("Unsupported mortality_type '%s'. Must be one of 'in_hospital', 'post_discharge', 'fixed_window' (or 'composite_readmit_or_death').",
                   paste(mortality_type, collapse = " ")), call. = FALSE)
    }
    return(list(outcome = mt, label = paste0("mortality_", mt)))
  }
  stop(sprintf("Unsupported target_outcome '%s'. Use 'none', 'all_cause_readmission' (alias 'all_cause_readmission_<N>d'), 'mortality', or 'readmission_or_death'.",
               target_outcome), call. = FALSE)
}

#' @keywords internal
#' @noRd
.describe_study <- function(spec) {
  bits <- sprintf("index visit concept ids %s",
                  if (is.null(spec$visit_ids)) "any" else paste0("(", .int_list(spec$visit_ids), ")"))
  if (!is.null(spec$study_start) || !is.null(spec$study_end)) {
    bits <- c(bits, sprintf("index start %s to %s", spec$study_start %||% "*", spec$study_end %||% "*"))
  }
  if (!is.null(spec$min_age)) bits <- c(bits, sprintf("age >= %s (%s)", .sql_int(spec$min_age), spec$age_method))
  if (!is.null(spec$min_los_days)) bits <- c(bits, sprintf("LOS >= %s d", .sql_int(spec$min_los_days)))
  if (!is.null(spec$washin_days) && spec$washin_days > 0) bits <- c(bits, sprintf("wash-in %s d", .sql_int(spec$washin_days)))
  bits <- c(bits, if (isTRUE(spec$exclude_death)) "exclude in-hospital death" else "in-hospital death retained")
  bits <- c(bits, sprintf("outcome %s", spec$label))
  if (!is.null(spec$window_days) && spec$outcome != "in_hospital") {
    bits <- c(bits, sprintf("window (%s, %s] d", .sql_int(spec$gap_days), .sql_int(spec$window_days)))
  }
  bits <- c(bits, if (isTRUE(spec$verified) && .study_followup_applicable(spec)) "verified follow-up required" else "verified follow-up not required")
  bits <- c(bits, paste0("sampling ", spec$rule, if (spec$rule == "random") sprintf(" (seed %s)", .sql_int(spec$seed)) else ""))
  paste0("Study cohort: ", paste(bits, collapse = "; "))
}

#' Define an Index-Stay Study Cohort
#'
#' Defines a leak-free index-stay study cohort (one stay per patient) from OMOP visits, with an optional outcome.
#' This is a single parameterised template that generalises [build_readmission_cohort()] and
#' [build_end_of_life_cohort()]: both now run on the same engine, so a cohort defined here and one built by a
#' legacy builder with equivalent settings are identical row for row. It adds emergency / outpatient / custom
#' index visit types, a study window on the index date, and a cohort-only mode (`target_outcome = "none"`).
#'
#' @section Definitions:
#' With `s` the index visit start date and `e` its end date (DATE values; `+ n` adds `n` days), `F` =
#' `followup_days` and `G` = `gap_days`:
#' \itemize{
#'   \item **Index stay**: `visit_concept_id` in the visit type, `study_start <= s <= study_end` (both inclusive),
#'     age at `s` >= `min_age`, `e - s >= min_los_days`, wash-in satisfied, and not an in-hospital death.
#'   \item **Wash-in**: an `observation_period` with `start <= s - washin_days` and `end >= s` exists, or, as
#'     fallback evidence when observation periods are missing, another visit of the patient started on or before
#'     `s - washin_days` (inclusive).
#'   \item **Outcome windows** (open on the left, closed on the right): readmission `e + G < start <= e + F`;
#'     post-discharge death `e + G < death <= e + F`; fixed-window death `s + G < death <= s + F`; in-hospital
#'     death `s <= death <= e` or a discharge disposition in `death_discharge_concept_ids`.
#'   \item **Verified follow-up**: an outcome event, or evidence (see `followup_evidence`) dated `>= anchor + F`,
#'     where the anchor is `e` (`s` for fixed-window mortality).
#'   \item **Leak-free scoping**: history lies at or before `s` and outcomes strictly after the anchor, so lookback
#'     and outcome periods are disjoint. `study_window` restricts only the index date; follow-up and outcome
#'     ascertainment may use records after `study_end`.
#' }
#'
#' @section Reproducing the legacy builders:
#' Every mapping below is asserted against stored golden results in the test suite.
#' \itemize{
#'   \item `build_readmission_cohort(target_visit_concept_ids = T, outcome_visit_concept_ids = O,
#'     followup_window_days = F, grace_days = G, washin_days = W, index_selection_rule = R, random_state = S,
#'     require_verified_followup = V)` equals `define_study_cohort(visit_type = T, outcome_visit_type = O,
#'     followup_days = F, gap_days = G, washin_days = W, sampling_rule = R, random_state = S,
#'     require_verified_followup = V, min_age = 18)`.
#'   \item `build_end_of_life_cohort(mortality_type = M, mortality_window_days = F, gap_days = G, min_age = A, ...)`
#'     equals `define_study_cohort(target_outcome = "mortality", mortality_type = M, followup_days = F,
#'     gap_days = G, min_age = A, ...)` (for `"composite_readmit_or_death"` use
#'     `target_outcome = "readmission_or_death"`).
#' }
#'
#' @param con Active DuckDB DBI connection to an OMOP CDM v5.4 database. May be read-only (see
#'   `materialise`).
#' @param visit_type Index visit type: `"inpatient"` (visit_concept_id 9201), `"emergency"` (9203),
#'   `"outpatient"` (9202), explicit visit_concept_ids, or a list/vector mixing both
#'   (`c("inpatient", "emergency")`). `NULL` accepts any visit. Site-specific concepts are passed as ids.
#' @param study_window `NULL`, or a length-2 vector/list `c(start, end)` of inclusive bounds on the index visit
#'   **start date** (`"YYYY-MM-DD"` strings or Dates); either element may be `NULL`/`NA`.
#' @param min_age Minimum age at the index visit start (see `age_method`); `NULL` disables the rule.
#' @param min_los_days Minimum length of stay in days (`visit_end_date - visit_start_date`); `NULL` disables the
#'   rule. `"auto"` (default) follows the legacy builders: 1 for readmission, post-discharge mortality, composite
#'   and cohort-only; 0 for in-hospital mortality; no restriction for fixed-window mortality.
#' @param washin_days Required prior observation in days (see Definitions); `0`/`NULL` disables the rule.
#' @param followup_days Length `F` of the follow-up / outcome horizon in days. Required for every outcome except
#'   `"none"` (where `NULL` also disables follow-up verification) and in-hospital mortality (ignored).
#' @param exclude_in_hospital_death `TRUE`/`FALSE`/`"auto"`. When `TRUE`, drops index stays that ended in death:
#'   the stay's discharge disposition is one of `death_discharge_concept_ids` **or** the patient's earliest known
#'   death date (sources: `death_sources`) is on/before the stay's end. When `FALSE`, only stays starting after
#'   the patient's death date are dropped. `"auto"` (default): `TRUE` except for in-hospital and fixed-window
#'   mortality. `TRUE` is rejected for in-hospital mortality and `FALSE` for post-discharge / composite
#'   outcomes, because either would mislabel deaths.
#' @param target_outcome `"all_cause_readmission"` (alias `"all_cause_readmission_<N>d"` with `N` equal to
#'   `followup_days`): another `outcome_visit_type` visit starts in `(e + G, e + F]`. `"mortality"`: see
#'   `mortality_type`. `"readmission_or_death"` (alias `"composite_readmit_or_death"`): readmission or death in
#'   `(e + G, e + F]`. `"none"`: cohort only; `outcome_flag` and `outcome_date` are `NA`.
#' @param mortality_type Used when `target_outcome = "mortality"`: `"post_discharge"` (death in `(e + G, e + F]`),
#'   `"fixed_window"` (death in `(s + G, s + F]`, SARD-style) or `"in_hospital"`.
#' @param outcome_visit_type Visit types that count as readmissions (default `"inpatient"`).
#' @param gap_days Gap `G` between the window anchor and the start of the outcome window (`grace_days` in
#'   [build_readmission_cohort()], `gap_days` in [build_end_of_life_cohort()]). Must be smaller than
#'   `followup_days`.
#' @param sampling_rule `"random"` (default), `"first"` or `"last"`: which eligible stay represents a patient --
#'   the stay minimising `hash(visit_occurrence_id, random_state)` (ties by id), the earliest start, or the latest
#'   start.
#' @param random_state Seed of the `"random"` rule (default 42).
#' @param require_verified_followup Keep a stay only if it has an outcome event or verified follow-up
#'   (default `TRUE`). Not applicable to in-hospital mortality or when `followup_days` is `NULL`. The result
#'   column `followup_verified` is reported either way.
#' @param death_discharge_concept_ids Discharge-disposition concepts meaning "died". Default `4216643` for
#'   readmission / cohort-only, `c(4216643, 4155309)` for mortality outcomes (the legacy end-of-life convention;
#'   4155309 is 'Ileal part' in Athena and is written by this package's PCORnet ETL for 'discharged against
#'   medical advice', so consider passing `4216643`).
#' @param death_sources Where a patient's death date comes from: `"death_table"` and/or
#'   `"discharge_disposition"` (the end date of any visit discharged to a death concept). Default `"death_table"`
#'   for readmission / cohort-only, both for mortality outcomes.
#' @param followup_evidence Records that verify follow-up when dated on/after anchor + `F`: any of
#'   `"observation_period"` (period end), `"visit"` (another visit's start), `"measurement"`, `"condition"`,
#'   `"drug"` (record dates) and `"death"`. Default: the four record types for readmission / cohort-only, all
#'   six for mortality outcomes.
#' @param age_method `"year_difference"` (default; legacy, needed for equivalence) is
#'   `date_diff('year', birth, index_start)`, the number of calendar-year boundaries crossed, which overstates
#'   age by one year before the birthday. `"completed_years"` is the exact age in whole years. Missing birth
#'   month / day count as 1.
#' @param cohort_definition_id Id written to `cohort` / `cohort_definition` (default 1).
#' @param outcome_cohort_id Optional id under which outcome events are written (when `materialise = TRUE`).
#'   Readmission outcomes write each qualifying readmission visit (start, end); mortality and composite outcomes
#'   write a one-day event on `outcome_date`.
#' @param cohort_name,cohort_description Metadata for `cohort_definition`; the description defaults to a summary
#'   of all settings.
#' @param materialise `TRUE` writes the cohort to the OMOP `cohort` / `cohort_definition` tables (created if
#'   missing), exactly as the legacy builders do; rows for the same ids are replaced, so re-running is
#'   idempotent. It raises an error on a read-only connection. `FALSE` leaves the database untouched. `"auto"`
#'   (default) writes unless the connection is read-only, in which case it warns and behaves like `FALSE` (so
#'   the same call works on read-only connections).
#' @param table_name Optional table name (up to three dot-separated plain identifiers) to persist the full result.
#' @param schema Optional schema (or `catalog.schema`) holding the CDM tables; default resolved via the search
#'   path.
#' @param attrition Compute the stepwise patient attrition via [compute_attrition()] (default `TRUE`).
#'
#' @return A `data.frame`, one row per patient ordered by `subject_id`, with columns `cohort_definition_id`,
#'   `subject_id`, `cohort_start_date` (index visit start), `cohort_end_date` (index visit end),
#'   `visit_occurrence_id`, `visit_concept_id`, `outcome_flag`, `outcome_date`, `followup_verified`,
#'   `age_at_index`, `los_days`, `discharged_to_concept_id` and `target_outcome`. The attributes `attrition`
#'   (the [compute_attrition()] table: visit type, study window, age, length of stay, wash-in, in-hospital death,
#'   verified follow-up), `summary` (the [get_cohort_summary()] row, when materialised into the default schema)
#'   and `definition` (a list of the resolved settings) carry the audit trail.
#' @examples
#' db <- tempfile(fileext = ".duckdb")
#' build_schema(db_path = db)
#' con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
#' DBI::dbExecute(con, "
#'   INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
#'   VALUES (1, 8507, 1960, 8527, 38003564), (2, 8532, 1975, 8527, 38003564)")
#' DBI::dbExecute(con, "
#'   INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,
#'                                 visit_end_date, visit_type_concept_id)
#'   VALUES (1, 1, 9201, DATE '2019-03-01', DATE '2019-03-05', 32827),
#'          (2, 1, 9201, DATE '2019-03-20', DATE '2019-03-22', 32827),
#'          (3, 2, 9201, DATE '2019-05-01', DATE '2019-05-04', 32827),
#'          (4, 2, 9202, DATE '2019-07-01', DATE '2019-07-01', 32827)")
#' cohort <- define_study_cohort(
#'   con,
#'   visit_type = "inpatient",
#'   study_window = c("2017-01-01", "2020-12-31"),
#'   min_age = 18,
#'   min_los_days = 1,
#'   washin_days = 0,
#'   followup_days = 30,
#'   exclude_in_hospital_death = TRUE,
#'   target_outcome = "all_cause_readmission_30d",
#'   sampling_rule = "first",
#'   materialise = FALSE
#' )
#' cohort[, c("subject_id", "visit_occurrence_id", "outcome_flag")]
#' attr(cohort, "attrition")
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' unlink(db)
#' @export
define_study_cohort <- function(con,
                                visit_type = "inpatient",
                                study_window = NULL,
                                min_age = 18,
                                min_los_days = "auto",
                                washin_days = 365,
                                followup_days = 30,
                                exclude_in_hospital_death = "auto",
                                target_outcome = "all_cause_readmission",
                                mortality_type = "post_discharge",
                                outcome_visit_type = "inpatient",
                                gap_days = 0,
                                sampling_rule = "random",
                                random_state = 42,
                                require_verified_followup = TRUE,
                                death_discharge_concept_ids = NULL,
                                death_sources = NULL,
                                followup_evidence = NULL,
                                age_method = "year_difference",
                                cohort_definition_id = 1,
                                outcome_cohort_id = NULL,
                                cohort_name = NULL,
                                cohort_description = NULL,
                                materialise = "auto",
                                table_name = NULL,
                                schema = NULL,
                                attrition = TRUE) {
  visit_ids <- .resolve_visit_types(visit_type, "visit_type", allow_null = TRUE)
  win <- .resolve_study_window(study_window)
  min_age_n <- .whole_number(min_age, "min_age", minimum = 0, allow_null = TRUE)
  washin_n <- .whole_number(washin_days, "washin_days", minimum = 0, allow_null = TRUE) %||% 0
  followup_n <- .whole_number(followup_days, "followup_days", minimum = 1, allow_null = TRUE)
  gap_n <- .whole_number(gap_days, "gap_days", minimum = 0)
  seed_n <- .whole_number(random_state, "random_state")
  cohort_id <- .whole_number(cohort_definition_id, "cohort_definition_id")
  verified <- .flag(require_verified_followup, "require_verified_followup")
  materialise <- .materialise_mode(materialise)
  attrition <- .flag(attrition, "attrition")

  rule <- tolower(trimws(as.character(sampling_rule)))
  if (length(rule) != 1L || !rule %in% .STUDY_SAMPLING_RULES) {
    stop(sprintf("Unsupported sampling_rule '%s'. Use 'first', 'last', or 'random'.",
                 paste(sampling_rule, collapse = " ")), call. = FALSE)
  }
  method <- tolower(trimws(as.character(age_method)))
  if (length(method) != 1L || !method %in% .STUDY_AGE_METHODS) {
    stop(sprintf("Unsupported age_method '%s'. Use one of (%s).", paste(age_method, collapse = " "),
                 paste(.STUDY_AGE_METHODS, collapse = ", ")), call. = FALSE)
  }
  if (!is.null(schema)) .quote_ident_path(schema, "schema", 2L)
  if (!is.null(table_name)) .quote_ident_path(table_name, "table_name", 3L)

  res <- .resolve_outcome(target_outcome, mortality_type, followup_n)
  outcome <- res$outcome
  label <- res$label

  min_los <- if (identical(min_los_days, "auto")) {
    .STUDY_AUTO_MIN_LOS[[outcome]]
  } else {
    .whole_number(min_los_days, "min_los_days", minimum = 0, allow_null = TRUE)
  }
  exclude <- if (identical(exclude_in_hospital_death, "auto")) {
    .STUDY_AUTO_EXCLUDE[[outcome]]
  } else {
    .flag(exclude_in_hospital_death, "exclude_in_hospital_death")
  }
  if (outcome == "in_hospital" && exclude) {
    stop("exclude_in_hospital_death = TRUE cannot be combined with in-hospital mortality: it would remove every event.",
         call. = FALSE)
  }
  if (outcome %in% c("post_discharge", "composite") && !exclude) {
    stop("Post-discharge mortality and readmission_or_death need exclude_in_hospital_death = TRUE: ",
         "stays that ended in death would otherwise be labelled as survivors.", call. = FALSE)
  }

  needs_window <- outcome %in% c("readmission", "post_discharge", "fixed_window", "composite")
  if (needs_window) {
    if (is.null(followup_n)) stop(sprintf("followup_days is required for target_outcome '%s'.", label), call. = FALSE)
    if (gap_n >= followup_n) {
      stop(sprintf("Inconsistent windows: gap_days (%s) must be smaller than followup_days (%s); the outcome window would be empty.",
                   .sql_int(gap_n), .sql_int(followup_n)), call. = FALSE)
    }
  } else {
    if (gap_n != 0) {
      stop(sprintf("gap_days has no effect for target_outcome '%s'; leave it at 0.", label), call. = FALSE)
    }
    if (outcome == "none" && verified && is.null(followup_n)) {
      stop("require_verified_followup = TRUE needs followup_days when target_outcome = 'none'; ",
           "set require_verified_followup = FALSE or provide followup_days.", call. = FALSE)
    }
  }
  window <- if (outcome != "in_hospital") followup_n else NULL

  outcome_ids <- .resolve_visit_types(outcome_visit_type %||% "inpatient", "outcome_visit_type", allow_null = FALSE)

  conv <- if (outcome %in% .STUDY_MORTALITY_FAMILIES) .STUDY_END_OF_LIFE_CONVENTIONS else .STUDY_READMISSION_CONVENTIONS
  death_ids <- if (is.null(death_discharge_concept_ids)) conv$death_ids else
    .resolve_death_ids(death_discharge_concept_ids, "death_discharge_concept_ids")
  sources <- if (is.null(death_sources)) conv$death_sources else
    .resolve_choices(death_sources, .STUDY_DEATH_SOURCES, "death_sources")
  evidence <- if (is.null(followup_evidence)) conv$evidence else
    .resolve_choices(followup_evidence, .STUDY_FOLLOWUP_EVIDENCE, "followup_evidence")

  oid <- .whole_number(outcome_cohort_id, "outcome_cohort_id", allow_null = TRUE)
  if (!is.null(oid)) {
    if (outcome == "none") {
      stop("outcome_cohort_id cannot be used with target_outcome = 'none' (there are no outcome events).", call. = FALSE)
    }
    if (oid == cohort_id) stop("outcome_cohort_id must differ from cohort_definition_id.", call. = FALSE)
  }

  spec <- .new_study_spec(
    cohort_id = cohort_id, outcome = outcome, label = label, schema = schema, visit_ids = visit_ids,
    outcome_visit_ids = outcome_ids, study_start = win$start, study_end = win$end, min_age = min_age_n,
    age_method = method, min_los_days = min_los, washin_days = washin_n, exclude_death = exclude,
    death_ids = death_ids, death_sources = sources, window_days = window, gap_days = gap_n, verified = verified,
    evidence = evidence, rule = rule, seed = seed_n
  )

  if (identical(materialise, "auto")) {
    if (.study_db_read_only(con, schema)) {
      warning("The connection is read-only, so the cohort was returned but not written to the cohort / ",
              "cohort_definition tables. Pass materialise = FALSE to silence this warning.", call. = FALSE)
      materialise <- FALSE
    } else {
      materialise <- TRUE
    }
  }

  name <- cohort_name %||% "Study Cohort"
  df <- .run_study_engine(
    con, spec, .STUDY_COLUMNS, materialise = materialise, table_name = table_name,
    materialize_args = list(
      name = name,
      description = cohort_description %||% .describe_study(spec),
      outcome_cohort_id = oid,
      outcome_name = sprintf("%s - Outcome (%s)", name, label),
      outcome_description = sprintf("Outcome events (%s) for %s", label, name),
      outcome_style = if (outcome == "readmission") "visits" else "event_date",
      overwrite = TRUE
    )
  )

  if (attrition) {
    attr(df, "attrition") <- compute_attrition(con, cohort_id, .study_attrition_steps(spec))
  }
  if (materialise && is.null(schema)) {
    attr(df, "summary") <- get_cohort_summary(con, cohort_id)
  }
  attr(df, "definition") <- list(
    visit_concept_ids = visit_ids,
    study_window = c(win$start, win$end),
    min_age = min_age_n,
    age_method = method,
    min_los_days = min_los,
    washin_days = washin_n,
    followup_days = window,
    gap_days = gap_n,
    exclude_in_hospital_death = exclude,
    target_outcome = label,
    outcome_visit_concept_ids = outcome_ids,
    sampling_rule = rule,
    random_state = seed_n,
    require_verified_followup = verified && .study_followup_applicable(spec),
    death_discharge_concept_ids = death_ids,
    death_sources = sources,
    followup_evidence = evidence,
    schema = schema
  )
  df
}

# ---- legacy builders: thin wrappers over the engine --------------------------------------------------

#' @keywords internal
#' @noRd
.legacy_ids <- function(value) if (is.null(value)) NULL else as.numeric(unlist(value))

#' Build 30-Day Readmission Cohort
#'
#' Constructs a leak-free 30-day readmission cohort directly in DuckDB SQL.
#' Enforces adult inpatient stay, LOS >= 1 day, discharged alive, verified follow-up window
#' (filtering right-censored lost-to-follow-up stays), reproducible index admission sampling,
#' and disjoint lookback vs post-discharge outcome scoping.
#'
#' This is a thin wrapper over the shared study-cohort engine (see [define_study_cohort()], which generalises
#' it): the legacy conventions (death from the death table and the index stay's own discharge disposition
#' 4216643; follow-up evidenced by later visits, measurements, conditions or drug exposures; age as
#' calendar-year difference) are pinned here and covered by golden-file tests.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Integer ID for target index cohort in OMOP `cohort` table (default 1).
#' @param outcome_cohort_id Optional integer ID for materialized readmission outcome cohort.
#' @param cohort_name Display name for cohort_definition table.
#' @param cohort_description Description for cohort_definition table.
#' @param target_visit_concept_ids Integer vector of visit concepts defining index stay (default 9201).
#' @param outcome_visit_concept_ids Integer vector of visit concepts defining readmission (default 9201).
#' @param followup_window_days Follow-up window in days (default 30).
#' @param grace_days Grace days immediately post-discharge before outcome window begins (default 0).
#' @param washin_days Prior observation lookback requirement in days (default 365).
#' @param index_selection_rule "random" (reproducible seed), "first", or "last".
#' @param random_state Integer seed for reproducible random index sampling (default 42).
#' @param require_verified_followup Logical. Whether to filter out lost-to-follow-up patients (default TRUE).
#' @param table_name Optional character string to materialize the index cohort table with metadata.
#' @return A `data.frame` containing qualifying index cohort records with metadata.
#' @export
build_readmission_cohort <- function(con,
                                     cohort_id = 1,
                                     outcome_cohort_id = NULL,
                                     cohort_name = "Inpatient 30-Day Readmission Cohort",
                                     cohort_description = NULL,
                                     target_visit_concept_ids = 9201,
                                     outcome_visit_concept_ids = 9201,
                                     followup_window_days = 30,
                                     grace_days = 0,
                                     washin_days = 365,
                                     index_selection_rule = "random",
                                     random_state = 42,
                                     require_verified_followup = TRUE,
                                     table_name = NULL) {
  rule <- tolower(trimws(index_selection_rule))
  if (!rule %in% .STUDY_SAMPLING_RULES) {
    stop(sprintf("Unsupported index_selection_rule '%s'. Use 'random', 'first', or 'last'.", index_selection_rule))
  }

  conv <- .STUDY_READMISSION_CONVENTIONS
  spec <- .new_study_spec(
    cohort_id = as.numeric(cohort_id), outcome = "readmission", label = "all_cause_readmission",
    visit_ids = .legacy_ids(target_visit_concept_ids), outcome_visit_ids = .legacy_ids(outcome_visit_concept_ids),
    min_age = 18, age_method = "year_difference", min_los_days = 1,
    washin_days = if (!is.null(washin_days) && washin_days > 0) as.numeric(washin_days) else 0,
    exclude_death = TRUE, death_ids = conv$death_ids, death_sources = conv$death_sources,
    window_days = as.numeric(followup_window_days), gap_days = as.numeric(grace_days),
    verified = isTRUE(require_verified_followup), evidence = conv$evidence, rule = rule,
    seed = as.numeric(random_state)
  )
  projection <- paste0(
    "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag, ",
    "los_days, age_at_index AS age_at_admission, discharged_to_concept_id"
  )
  .run_study_engine(
    con, spec, projection, materialise = TRUE, table_name = table_name,
    materialize_args = list(
      name = cohort_name,
      description = cohort_description %||% "Inpatient 30-day readmission index cohort",
      outcome_cohort_id = outcome_cohort_id,
      outcome_name = paste(cohort_name, "- Readmission Outcome"),
      outcome_description = "Patients experiencing 30-day readmission outcome",
      outcome_style = "visits",
      overwrite = TRUE
    )
  )
}

#' Build End of Life / Mortality Cohort
#'
#' Constructs a standardized End of Life / Mortality cohort in DuckDB SQL.
#' Supports four mortality ascertainment paradigms:
#' \itemize{
#'   \item \code{"in_hospital"}: Death during index stay (expired/died in hospital).
#'   \item \code{"post_discharge"}: Death within (t_discharge + gap_days, t_discharge + mortality_window_days].
#'   \item \code{"fixed_window"}: SARD-style EOL prediction within (t_index + gap_days, t_index + mortality_window_days].
#'   \item \code{"composite_readmit_or_death"}: Clinical composite of acute readmission OR all-cause death post-discharge.
#' }
#' Resolves dual OMOP death sources:
#' \itemize{
#'   \item \code{death} table: \code{death_date} linked to \code{person_id}.
#'   \item \code{visit_occurrence} table: \code{discharged_to_concept_id IN (4216643, 4155309)} (Expired / Hospice).
#' }
#' This is a thin wrapper over the shared study-cohort engine (see [define_study_cohort()], which generalises
#' it); its legacy conventions are pinned here and covered by golden-file tests.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Target cohort definition ID in OMOP `cohort` table (default 1).
#' @param outcome_cohort_id Optional outcome cohort definition ID (default 2). Set NULL to skip outcome cohort table population.
#' @param cohort_name Display name for cohort_definition table.
#' @param cohort_description Detailed description for cohort_definition table.
#' @param mortality_type Mode of mortality ascertainment: \code{"in_hospital"}, \code{"post_discharge"}, \code{"fixed_window"}, or \code{"composite_readmit_or_death"}.
#' @param target_visit_concept_ids Visit concept IDs defining qualifying index encounters (default 9201: Inpatient). Set NULL for any visit.
#' @param outcome_visit_concept_ids Visit concept IDs defining readmissions for composite mode (default 9201).
#' @param mortality_window_days Number of days for mortality outcome window (default 30).
#' @param gap_days Days between index date / discharge and start of outcome window (default 0).
#' @param min_age Minimum patient age at index encounter (default 18).
#' @param washin_days Baseline continuous observation lookback requirement before index stay (default 365).
#' @param require_verified_followup Logical. If TRUE, excludes lost-to-follow-up patients without confirmed survival follow-up (default TRUE).
#' @param index_selection_rule \code{"random"} (reproducible seed), \code{"first"}, or \code{"last"}.
#' @param random_state Seed for reproducible random sampling (default 42).
#' @param table_name Optional custom table name in DuckDB to materialize cohort records with metadata.
#' @param schema CDM schema containing tables (default "main").
#' @param overwrite Logical. If TRUE, cleans existing cohort and cohort_definition entries for cohort_id and outcome_cohort_id (default TRUE).
#' @return A \code{data.frame} containing qualifying cohort records with metadata.
#' @export
build_end_of_life_cohort <- function(con,
                                    cohort_id = 1,
                                    outcome_cohort_id = 2,
                                    cohort_name = "End of Life Cohort",
                                    cohort_description = NULL,
                                    mortality_type = "post_discharge",
                                    target_visit_concept_ids = 9201,
                                    outcome_visit_concept_ids = 9201,
                                    mortality_window_days = 30,
                                    gap_days = 0,
                                    min_age = 18,
                                    washin_days = 365,
                                    require_verified_followup = TRUE,
                                    index_selection_rule = "random",
                                    random_state = 42,
                                    table_name = NULL,
                                    schema = "main",
                                    overwrite = TRUE) {
  m_type <- tolower(trimws(mortality_type))
  if (!m_type %in% .STUDY_MORTALITY_TYPES) {
    stop(sprintf("Unsupported mortality_type '%s'. Must be one of: %s", mortality_type,
                 paste(.STUDY_MORTALITY_TYPES, collapse = ", ")))
  }

  rule <- tolower(trimws(index_selection_rule))
  if (!rule %in% .STUDY_SAMPLING_RULES) {
    stop(sprintf("Unsupported index_selection_rule '%s'. Use 'random', 'first', or 'last'.", index_selection_rule))
  }

  outcome <- if (m_type == "composite_readmit_or_death") "composite" else m_type
  conv <- .STUDY_END_OF_LIFE_CONVENTIONS
  spec <- .new_study_spec(
    cohort_id = as.numeric(cohort_id), outcome = outcome, label = m_type, schema = schema,
    visit_ids = .legacy_ids(target_visit_concept_ids), outcome_visit_ids = .legacy_ids(outcome_visit_concept_ids),
    min_age = as.numeric(min_age), age_method = "year_difference", min_los_days = .STUDY_AUTO_MIN_LOS[[outcome]],
    washin_days = if (!is.null(washin_days) && washin_days > 0) as.numeric(washin_days) else 0,
    exclude_death = .STUDY_AUTO_EXCLUDE[[outcome]], death_ids = conv$death_ids,
    death_sources = conv$death_sources,
    window_days = if (m_type != "in_hospital") as.numeric(mortality_window_days) else NULL,
    gap_days = as.numeric(gap_days), verified = isTRUE(require_verified_followup), evidence = conv$evidence,
    rule = rule, seed = as.numeric(random_state)
  )
  projection <- paste0(
    "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag, ",
    "outcome_date, target_outcome AS mortality_type, age_at_index, los_days, discharged_to_concept_id"
  )
  .run_study_engine(
    con, spec, projection, materialise = TRUE, table_name = table_name,
    materialize_args = list(
      name = cohort_name,
      description = cohort_description %||% sprintf("%s (%s)", cohort_name, m_type),
      outcome_cohort_id = outcome_cohort_id,
      outcome_name = paste(cohort_name, "Outcome"),
      outcome_description = sprintf("Outcome events for %s", cohort_name),
      outcome_style = "event_date",
      overwrite = isTRUE(overwrite)
    )
  )
}

#' @rdname build_end_of_life_cohort
#' @export
build_mortality_cohort <- build_end_of_life_cohort


#' Prepare Competing Risk Survival Data for Readmission vs. Mortality
#'
#' Under CMS HRRP and traditional binary readmission models, patients who die post-discharge
#' without an acute readmission are often either censored or mislabeled as non-events (survivors),
#' introducing survivor bias into hospital benchmarking.
#'
#' This function formats index stays into a 3-state competing risk survival framework:
#' \itemize{
#'   \item Status 0: Censored (event-free throughout the entire follow-up window)
#'   \item Status 1: Primary event of interest (acute hospital readmission)
#'   \item Status 2: Competing event (all-cause mortality post-discharge without readmission)
#' }
#'
#' @param con Active DuckDB connection.
#' @param cohort_table Source cohort table name containing index stays (default `"cohort"`).
#' @param cohort_id Optional `cohort_definition_id` to filter `cohort_table`.
#' @param followup_window_days Observation horizon in days (default 30).
#' @param grace_days Days post-discharge before outcome evaluation begins (default 0).
#' @param outcome_visit_concept_ids Visit concept IDs for readmission (default `9201`).
#' @return A list with two elements:
#' \itemize{
#'   \item `data`: A `data.frame` with columns `subject_id`, `visit_occurrence_id`, `cohort_start_date`,
#'     `cohort_end_date`, `time_days`, `status`, `event_type`, and `composite_event`.
#'   \item `summary`: A list of summary metrics (total patients, counts and crude rates for readmission,
#'     competing mortality, censoring, and composite outcomes).
#' }
#' @export
prepare_competing_risks_data <- function(con,
                                         cohort_table = "cohort",
                                         cohort_id = NULL,
                                         followup_window_days = 30,
                                         grace_days = 0,
                                         outcome_visit_concept_ids = c(9201)) {
  out_ids <- paste(as.integer(outcome_visit_concept_ids), collapse = ", ")
  cohort_filter <- if (!is.null(cohort_id)) sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", cohort_table))$column_name)
  vid_col <- if ("visit_occurrence_id" %in% cols) "c.visit_occurrence_id" else "v.visit_occurrence_id"

  query <- sprintf("
    WITH cohort_src AS (
        SELECT 
            c.subject_id,
            c.cohort_start_date,
            c.cohort_end_date,
            %s AS visit_occurrence_id
        FROM %s c
        LEFT JOIN visit_occurrence v 
          ON v.person_id = c.subject_id 
         AND v.visit_start_date = c.cohort_start_date
        %s
    ),
    earliest_readmit AS (
        SELECT 
            cs.subject_id,
            cs.cohort_start_date,
            cs.cohort_end_date,
            cs.visit_occurrence_id,
            MIN(ro.visit_start_date) AS readmit_date
        FROM cohort_src cs
        LEFT JOIN visit_occurrence ro
          ON ro.person_id = cs.subject_id
         AND ro.visit_occurrence_id != cs.visit_occurrence_id
         AND ro.visit_concept_id IN (%s)
         AND ro.visit_start_date > (cs.cohort_end_date + %d)
         AND ro.visit_start_date <= (cs.cohort_end_date + %d)
        GROUP BY cs.subject_id, cs.cohort_start_date, cs.cohort_end_date, cs.visit_occurrence_id
    ),
    earliest_death AS (
        SELECT 
            cs.subject_id,
            cs.cohort_start_date,
            cs.cohort_end_date,
            cs.visit_occurrence_id,
            MIN(d.death_date) AS death_date
        FROM cohort_src cs
        LEFT JOIN (
            SELECT person_id, death_date FROM death
            UNION
            SELECT person_id, visit_end_date AS death_date 
            FROM visit_occurrence 
            WHERE discharged_to_concept_id = 4216643
        ) d
          ON d.person_id = cs.subject_id
         AND d.death_date > cs.cohort_end_date
         AND d.death_date <= (cs.cohort_end_date + %d)
        GROUP BY cs.subject_id, cs.cohort_start_date, cs.cohort_end_date, cs.visit_occurrence_id
    ),
    combined AS (
        SELECT 
            r.subject_id,
            r.cohort_start_date,
            r.cohort_end_date,
            r.visit_occurrence_id,
            r.readmit_date,
            d.death_date,
            date_diff('day', r.cohort_end_date, r.readmit_date) AS days_to_readmit,
            date_diff('day', r.cohort_end_date, d.death_date) AS days_to_death
        FROM earliest_readmit r
        JOIN earliest_death d
          ON d.subject_id = r.subject_id
         AND d.cohort_start_date = r.cohort_start_date
         AND d.cohort_end_date = r.cohort_end_date
         AND COALESCE(d.visit_occurrence_id, -1) = COALESCE(r.visit_occurrence_id, -1)
    )
    SELECT 
        subject_id,
        visit_occurrence_id,
        cohort_start_date,
        cohort_end_date,
        CAST(CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN days_to_readmit
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN days_to_death
            ELSE %d
        END AS INTEGER) AS time_days,
        CAST(CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN 1
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN 2
            ELSE 0
        END AS INTEGER) AS status,
        CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN 'readmission'
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN 'death_competing'
            ELSE 'censored'
        END AS event_type,
        CAST(CASE 
            WHEN (readmit_date IS NOT NULL OR death_date IS NOT NULL) THEN 1 
            ELSE 0 
        END AS INTEGER) AS composite_event
    FROM combined
    ORDER BY subject_id, cohort_start_date;
  ", vid_col, cohort_table, cohort_filter, out_ids, as.integer(grace_days), as.integer(followup_window_days),
     as.integer(followup_window_days), as.integer(followup_window_days))

  df <- DBI::dbGetQuery(con, query)
  n_total <- nrow(df)
  n_readmit <- if (n_total > 0) sum(df$status == 1) else 0L
  n_competing <- if (n_total > 0) sum(df$status == 2) else 0L
  n_censored <- if (n_total > 0) sum(df$status == 0) else 0L
  n_composite <- n_readmit + n_competing

  summary <- list(
    total_patients = n_total,
    n_readmissions = n_readmit,
    readmission_rate = if (n_total > 0) n_readmit / n_total else 0,
    n_competing_deaths = n_competing,
    competing_death_rate = if (n_total > 0) n_competing / n_total else 0,
    n_censored = n_censored,
    censored_rate = if (n_total > 0) n_censored / n_total else 0,
    n_composite_events = n_composite,
    composite_rate = if (n_total > 0) n_composite / n_total else 0,
    followup_window_days = as.integer(followup_window_days)
  )

  list(data = df, summary = summary)
}

