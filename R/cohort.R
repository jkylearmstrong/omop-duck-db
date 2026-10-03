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

#' Build 30-Day Readmission Cohort
#'
#' Constructs a leak-free 30-day readmission cohort directly in DuckDB SQL.
#' Enforces adult inpatient stay, LOS >= 1 day, discharged alive, verified follow-up window
#' (filtering right-censored lost-to-follow-up stays), reproducible index admission sampling,
#' and disjoint lookback vs post-discharge outcome scoping.
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
  ensure_cohort_tables(con)

  tgt_ids <- as.integer(target_visit_concept_ids)
  tgt_in <- paste(tgt_ids, collapse = ", ")

  out_ids <- as.integer(outcome_visit_concept_ids)
  out_in <- paste(out_ids, collapse = ", ")

  rule <- tolower(trimws(index_selection_rule))
  order_by <- if (rule == "random") {
    sprintf("hash(f.visit_occurrence_id, %d), f.visit_occurrence_id", as.integer(random_state))
  } else if (rule == "first") {
    "f.visit_start_date ASC, f.visit_occurrence_id ASC"
  } else if (rule == "last") {
    "f.visit_start_date DESC, f.visit_occurrence_id DESC"
  } else {
    stop(sprintf("Unsupported index_selection_rule '%s'. Use 'random', 'first', or 'last'.", index_selection_rule))
  }

  washin_clause <- if (!is.null(washin_days) && washin_days > 0) {
    sprintf("
      AND (
          EXISTS (
              SELECT 1 FROM observation_period op 
              WHERE op.person_id = v.person_id 
                AND op.observation_period_start_date <= (v.visit_start_date - %d)
                AND op.observation_period_end_date >= v.visit_start_date
          )
          OR EXISTS (
              SELECT 1 FROM visit_occurrence pv 
              WHERE pv.person_id = v.person_id 
                AND pv.visit_occurrence_id != v.visit_occurrence_id
                AND pv.visit_start_date <= (v.visit_start_date - %d)
          )
      )
    ", as.integer(washin_days), as.integer(washin_days))
  } else {
    ""
  }

  followup_filter <- if (isTRUE(require_verified_followup)) {
    "WHERE (outcome_flag = 1 OR has_subsequent_event = 1)"
  } else {
    ""
  }

  query <- sprintf("
    WITH eligible_stays AS (
        SELECT 
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) AS age_at_admission
        FROM visit_occurrence v
        JOIN person p ON v.person_id = p.person_id
        WHERE v.visit_concept_id IN (%s)
          AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
          AND date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) >= 18
          AND COALESCE(v.discharged_to_concept_id, 0) != 4216643
          AND NOT EXISTS (
              SELECT 1 FROM death d 
              WHERE d.person_id = v.person_id 
                AND d.death_date <= v.visit_end_date
          )
          %s
    ),
    outcomes_and_followup AS (
        SELECT 
            e.*,
            -- 30-day readmission outcome
            CASE WHEN EXISTS (
                SELECT 1 FROM visit_occurrence ro
                WHERE ro.person_id = e.subject_id
                  AND ro.visit_occurrence_id != e.visit_occurrence_id
                  AND ro.visit_concept_id IN (%s)
                  AND ro.visit_start_date > (e.visit_end_date + %d)
                  AND ro.visit_start_date <= (e.visit_end_date + %d)
            ) THEN 1 ELSE 0 END AS outcome_flag,
            -- Verified follow-up (subsequent encounter >= followup_window_days)
            CASE WHEN (
                EXISTS (
                    SELECT 1 FROM visit_occurrence sv
                    WHERE sv.person_id = e.subject_id
                      AND sv.visit_occurrence_id != e.visit_occurrence_id
                      AND sv.visit_start_date >= (e.visit_end_date + %d)
                )
                OR EXISTS (
                    SELECT 1 FROM measurement sm
                    WHERE sm.person_id = e.subject_id
                      AND sm.measurement_date >= (e.visit_end_date + %d)
                )
                OR EXISTS (
                    SELECT 1 FROM condition_occurrence sc
                    WHERE sc.person_id = e.subject_id
                      AND sc.condition_start_date >= (e.visit_end_date + %d)
                )
                OR EXISTS (
                    SELECT 1 FROM drug_exposure sd
                    WHERE sd.person_id = e.subject_id
                      AND sd.drug_exposure_start_date >= (e.visit_end_date + %d)
                )
            ) THEN 1 ELSE 0 END AS has_subsequent_event
        FROM eligible_stays e
    ),
    filtered_stays AS (
        SELECT *
        FROM outcomes_and_followup
        %s
    ),
    ranked_stays AS (
        SELECT 
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id 
                ORDER BY %s
            ) AS stay_rank
        FROM filtered_stays f
    )
    SELECT 
        %d AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        outcome_flag,
        los_days,
        age_at_admission,
        discharged_to_concept_id
    FROM ranked_stays
    WHERE stay_rank = 1
    ORDER BY subject_id, cohort_start_date;
  ", tgt_in, washin_clause, out_in, as.integer(grace_days), as.integer(followup_window_days),
     as.integer(followup_window_days), as.integer(followup_window_days), as.integer(followup_window_days), as.integer(followup_window_days),
     followup_filter, order_by, as.integer(cohort_id))

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_readmission_cohort;")
  DBI::dbExecute(con, sprintf("CREATE TEMPORARY TABLE _temp_readmission_cohort AS %s;", query))

  name_esc <- gsub("'", "''", cohort_name)
  desc_esc <- gsub("'", "''", cohort_description %||% "Inpatient 30-day readmission index cohort")

  DBI::dbExecute(con, sprintf("DELETE FROM cohort_definition WHERE cohort_definition_id = %d;", as.integer(cohort_id)))
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
        %d,
        '%s',
        '%s',
        0,
        NULL,
        0,
        DATE '%s'
    );
  ", as.integer(cohort_id), name_esc, desc_esc, as.character(Sys.Date())))

  DBI::dbExecute(con, sprintf("DELETE FROM cohort WHERE cohort_definition_id = %d;", as.integer(cohort_id)))
  DBI::dbExecute(con, "
    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT cohort_definition_id, subject_id, cohort_start_date, cohort_end_date
    FROM _temp_readmission_cohort;
  ")

  if (!is.null(outcome_cohort_id)) {
    out_name_esc <- gsub("'", "''", paste(cohort_name, "- Readmission Outcome"))
    DBI::dbExecute(con, sprintf("DELETE FROM cohort_definition WHERE cohort_definition_id = %d;", as.integer(outcome_cohort_id)))
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
          %d,
          '%s',
          'Patients experiencing 30-day readmission outcome',
          0,
          NULL,
          0,
          DATE '%s'
      );
    ", as.integer(outcome_cohort_id), out_name_esc, as.character(Sys.Date())))


    DBI::dbExecute(con, sprintf("DELETE FROM cohort WHERE cohort_definition_id = %d;", as.integer(outcome_cohort_id)))
    DBI::dbExecute(con, sprintf("
      INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
      SELECT DISTINCT
          %d AS cohort_definition_id,
          tc.subject_id,
          ro.visit_start_date AS cohort_start_date,
          ro.visit_end_date AS cohort_end_date
      FROM _temp_readmission_cohort tc
      JOIN visit_occurrence ro ON tc.subject_id = ro.person_id
      WHERE tc.outcome_flag = 1
        AND ro.visit_occurrence_id != tc.visit_occurrence_id
        AND ro.visit_concept_id IN (%s)
        AND ro.visit_start_date > (tc.cohort_end_date + %d)
        AND ro.visit_start_date <= (tc.cohort_end_date + %d);
    ", as.integer(outcome_cohort_id), out_in, as.integer(grace_days), as.integer(followup_window_days)))
  }

  if (!is.null(table_name)) {
    DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s;", table_name))
    DBI::dbExecute(con, sprintf("CREATE TABLE %s AS SELECT * FROM _temp_readmission_cohort;", table_name))
  }

  res_df <- DBI::dbGetQuery(con, "SELECT * FROM _temp_readmission_cohort ORDER BY subject_id;")
  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_readmission_cohort;")
  res_df
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
  ensure_cohort_tables(con)

  valid_types <- c("in_hospital", "post_discharge", "fixed_window", "composite_readmit_or_death")
  m_type <- tolower(trimws(mortality_type))
  if (!m_type %in% valid_types) {
    stop(sprintf("Unsupported mortality_type '%s'. Must be one of: %s", mortality_type, paste(valid_types, collapse = ", ")))
  }

  tgt_clause <- if (is.null(target_visit_concept_ids)) {
    "1=1"
  } else {
    tgt_ids <- as.integer(target_visit_concept_ids)
    sprintf("v.visit_concept_id IN (%s)", paste(tgt_ids, collapse = ", "))
  }

  out_ids <- as.integer(outcome_visit_concept_ids)
  out_in <- paste(out_ids, collapse = ", ")

  rule <- tolower(trimws(index_selection_rule))
  order_by <- if (rule == "random") {
    sprintf("hash(f.visit_occurrence_id, %d), f.visit_occurrence_id", as.integer(random_state))
  } else if (rule == "first") {
    "f.visit_start_date ASC, f.visit_occurrence_id ASC"
  } else if (rule == "last") {
    "f.visit_start_date DESC, f.visit_occurrence_id DESC"
  } else {
    stop(sprintf("Unsupported index_selection_rule '%s'. Use 'random', 'first', or 'last'.", index_selection_rule))
  }

  washin_clause <- if (!is.null(washin_days) && washin_days > 0) {
    sprintf("
      AND (
          EXISTS (
              SELECT 1 FROM %s.observation_period op 
              WHERE op.person_id = v.person_id 
                AND op.observation_period_start_date <= (v.visit_start_date - %d)
                AND op.observation_period_end_date >= v.visit_start_date
          )
          OR EXISTS (
              SELECT 1 FROM %s.visit_occurrence pv 
              WHERE pv.person_id = v.person_id 
                AND pv.visit_occurrence_id != v.visit_occurrence_id
                AND pv.visit_start_date <= (v.visit_start_date - %d)
          )
      )
    ", schema, as.integer(washin_days), schema, as.integer(washin_days))
  } else {
    ""
  }

  if (m_type == "in_hospital") {
    stay_criteria <- "AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 0 AND (pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)"
    outcome_expr <- "
      CASE WHEN COALESCE(e.discharged_to_concept_id, 0) IN (4216643, 4155309) 
             OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date)
           THEN 1 ELSE 0 END AS outcome_flag,
      CASE WHEN COALESCE(e.discharged_to_concept_id, 0) IN (4216643, 4155309) 
             OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date)
           THEN CAST(COALESCE(e.death_date, e.visit_end_date) AS DATE) ELSE NULL END AS outcome_date
    "
    anchor_expr <- "e.visit_end_date"
  } else if (m_type == "post_discharge") {
    stay_criteria <- "
      AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
      AND COALESCE(v.discharged_to_concept_id, 0) NOT IN (4216643, 4155309)
      AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)
    "
    outcome_expr <- sprintf("
      CASE WHEN e.death_date IS NOT NULL 
            AND e.death_date > (e.visit_end_date + %d) 
            AND e.death_date <= (e.visit_end_date + %d)
           THEN 1 ELSE 0 END AS outcome_flag,
      CASE WHEN e.death_date IS NOT NULL 
            AND e.death_date > (e.visit_end_date + %d) 
            AND e.death_date <= (e.visit_end_date + %d)
           THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date
    ", as.integer(gap_days), as.integer(mortality_window_days),
       as.integer(gap_days), as.integer(mortality_window_days))
    anchor_expr <- "e.visit_end_date"
  } else if (m_type == "fixed_window") {
    stay_criteria <- "AND (pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)"
    outcome_expr <- sprintf("
      CASE WHEN e.death_date IS NOT NULL 
            AND e.death_date > (e.visit_start_date + %d) 
            AND e.death_date <= (e.visit_start_date + %d)
           THEN 1 ELSE 0 END AS outcome_flag,
      CASE WHEN e.death_date IS NOT NULL 
            AND e.death_date > (e.visit_start_date + %d) 
            AND e.death_date <= (e.visit_start_date + %d)
           THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date
    ", as.integer(gap_days), as.integer(mortality_window_days),
       as.integer(gap_days), as.integer(mortality_window_days))
    anchor_expr <- "e.visit_start_date"
  } else if (m_type == "composite_readmit_or_death") {
    stay_criteria <- "
      AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
      AND COALESCE(v.discharged_to_concept_id, 0) NOT IN (4216643, 4155309)
      AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)
    "
    outcome_expr <- sprintf("
      CASE WHEN (
          (e.death_date IS NOT NULL AND e.death_date > (e.visit_end_date + %d) AND e.death_date <= (e.visit_end_date + %d))
          OR EXISTS (
              SELECT 1 FROM %s.visit_occurrence ro
              WHERE ro.person_id = e.subject_id
                AND ro.visit_occurrence_id != e.visit_occurrence_id
                AND ro.visit_concept_id IN (%s)
                AND ro.visit_start_date > (e.visit_end_date + %d)
                AND ro.visit_start_date <= (e.visit_end_date + %d)
          )
      ) THEN 1 ELSE 0 END AS outcome_flag,
      CAST(LEAST(
          CASE WHEN e.death_date IS NOT NULL AND e.death_date > (e.visit_end_date + %d) AND e.death_date <= (e.visit_end_date + %d) THEN e.death_date ELSE NULL END,
          (SELECT MIN(ro.visit_start_date) FROM %s.visit_occurrence ro WHERE ro.person_id = e.subject_id AND ro.visit_occurrence_id != e.visit_occurrence_id AND ro.visit_concept_id IN (%s) AND ro.visit_start_date > (e.visit_end_date + %d) AND ro.visit_start_date <= (e.visit_end_date + %d))
      ) AS DATE) AS outcome_date
    ", as.integer(gap_days), as.integer(mortality_window_days),
       schema, out_in, as.integer(gap_days), as.integer(mortality_window_days),
       as.integer(gap_days), as.integer(mortality_window_days),
       schema, out_in, as.integer(gap_days), as.integer(mortality_window_days))
    anchor_expr <- "e.visit_end_date"
  }

  if (isTRUE(require_verified_followup) && m_type != "in_hospital") {
    followup_check <- sprintf("
      CASE WHEN (
          EXISTS (
              SELECT 1 FROM %s.observation_period op
              WHERE op.person_id = e.subject_id
                AND op.observation_period_end_date >= (%s + %d)
          )
          OR EXISTS (
              SELECT 1 FROM %s.visit_occurrence sv
              WHERE sv.person_id = e.subject_id
                AND sv.visit_occurrence_id != e.visit_occurrence_id
                AND sv.visit_start_date >= (%s + %d)
          )
          OR EXISTS (
              SELECT 1 FROM %s.measurement sm
              WHERE sm.person_id = e.subject_id
                AND sm.measurement_date >= (%s + %d)
          )
          OR EXISTS (
              SELECT 1 FROM %s.condition_occurrence sc
              WHERE sc.person_id = e.subject_id
                AND sc.condition_start_date >= (%s + %d)
          )
          OR EXISTS (
              SELECT 1 FROM %s.drug_exposure sd
              WHERE sd.person_id = e.subject_id
                AND sd.drug_exposure_start_date >= (%s + %d)
          )
          OR (e.death_date IS NOT NULL AND e.death_date >= (%s + %d))
      ) THEN 1 ELSE 0 END AS has_subsequent_event
    ", schema, anchor_expr, as.integer(mortality_window_days),
       schema, anchor_expr, as.integer(mortality_window_days),
       schema, anchor_expr, as.integer(mortality_window_days),
       schema, anchor_expr, as.integer(mortality_window_days),
       schema, anchor_expr, as.integer(mortality_window_days),
       anchor_expr, as.integer(mortality_window_days))
    filter_clause <- "WHERE (outcome_flag = 1 OR has_subsequent_event = 1)"
  } else {
    followup_check <- "1 AS has_subsequent_event"
    filter_clause <- ""
  }

  query <- sprintf("
    CREATE OR REPLACE TEMP TABLE _temp_eol_cohort AS
    WITH all_deaths AS (
        SELECT person_id, CAST(death_date AS DATE) AS death_date
        FROM %s.death
        WHERE death_date IS NOT NULL
        UNION ALL
        SELECT person_id, CAST(visit_end_date AS DATE) AS death_date
        FROM %s.visit_occurrence
        WHERE discharged_to_concept_id IN (4216643, 4155309)
          AND visit_end_date IS NOT NULL
    ),
    patient_death AS (
        SELECT person_id, MIN(death_date) AS death_date
        FROM all_deaths
        GROUP BY person_id
    ),
    eligible_stays AS (
        SELECT 
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            pd.death_date,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) AS age_at_index
        FROM %s.visit_occurrence v
        JOIN %s.person p ON v.person_id = p.person_id
        LEFT JOIN patient_death pd ON v.person_id = pd.person_id
        WHERE %s
          AND date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) >= %d
          %s
          %s
    ),
    outcomes_and_followup AS (
        SELECT 
            e.*,
            '%s' AS mortality_type,
            %s,
            %s
        FROM eligible_stays e
    ),
    filtered_stays AS (
        SELECT *
        FROM outcomes_and_followup
        %s
    ),
    ranked_stays AS (
        SELECT 
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id 
                ORDER BY %s
            ) AS rnk
        FROM filtered_stays f
    )
    SELECT 
        %d AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        outcome_flag,
        outcome_date,
        mortality_type,
        age_at_index,
        los_days,
        discharged_to_concept_id
    FROM ranked_stays
    WHERE rnk = 1;
  ", schema, schema, schema, schema, tgt_clause, as.integer(min_age), stay_criteria, washin_clause,
     m_type, outcome_expr, followup_check, filter_clause, order_by, as.integer(cohort_id))

  DBI::dbExecute(con, query)

  name_esc <- gsub("'", "''", cohort_name)
  desc_esc <- gsub("'", "''", cohort_description %||% sprintf("%s (%s)", cohort_name, m_type))

  if (isTRUE(overwrite)) {
    DBI::dbExecute(con, sprintf("DELETE FROM %s.cohort WHERE cohort_definition_id = %d;", schema, as.integer(cohort_id)))
    DBI::dbExecute(con, sprintf("DELETE FROM %s.cohort_definition WHERE cohort_definition_id = %d;", schema, as.integer(cohort_id)))
    if (!is.null(outcome_cohort_id)) {
      DBI::dbExecute(con, sprintf("DELETE FROM %s.cohort WHERE cohort_definition_id = %d;", schema, as.integer(outcome_cohort_id)))
      DBI::dbExecute(con, sprintf("DELETE FROM %s.cohort_definition WHERE cohort_definition_id = %d;", schema, as.integer(outcome_cohort_id)))
    }
  }

  DBI::dbExecute(con, sprintf("
    INSERT INTO %s.cohort_definition (
        cohort_definition_id, cohort_definition_name, cohort_definition_description,
        definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
    ) VALUES (
        %d, '%s', '%s', 0, NULL, 0, DATE '%s'
    );
  ", schema, as.integer(cohort_id), name_esc, desc_esc, as.character(Sys.Date())))

  DBI::dbExecute(con, sprintf("
    INSERT INTO %s.cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT DISTINCT
        cohort_definition_id,
        subject_id,
        cohort_start_date,
        cohort_end_date
    FROM _temp_eol_cohort;
  ", schema))

  if (!is.null(outcome_cohort_id)) {
    out_name_esc <- gsub("'", "''", paste(name_esc, "Outcome"))
    DBI::dbExecute(con, sprintf("
      INSERT INTO %s.cohort_definition (
          cohort_definition_id, cohort_definition_name, cohort_definition_description,
          definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
      ) VALUES (
          %d, '%s', 'Outcome events for %s', 0, NULL, 0, DATE '%s'
      );
    ", schema, as.integer(outcome_cohort_id), out_name_esc, name_esc, as.character(Sys.Date())))

    DBI::dbExecute(con, sprintf("
      INSERT INTO %s.cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
      SELECT DISTINCT
          %d AS cohort_definition_id,
          subject_id,
          COALESCE(outcome_date, cohort_end_date) AS cohort_start_date,
          COALESCE(outcome_date, cohort_end_date) AS cohort_end_date
      FROM _temp_eol_cohort
      WHERE outcome_flag = 1;
    ", schema, as.integer(outcome_cohort_id)))
  }

  if (!is.null(table_name)) {
    DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s;", table_name))
    DBI::dbExecute(con, sprintf("CREATE TABLE %s AS SELECT * FROM _temp_eol_cohort;", table_name))
  }

  res_df <- DBI::dbGetQuery(con, "SELECT * FROM _temp_eol_cohort ORDER BY subject_id;")
  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_eol_cohort;")
  res_df
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

