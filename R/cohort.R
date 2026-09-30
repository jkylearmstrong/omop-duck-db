#' Ensure Standard OMOP Cohort Tables Exist
#'
#' Creates `cohort` and `cohort_definition` tables in the active DuckDB connection
#' if they are not already present.
#'
#' @param con Active DuckDB DBI connection.
#' @return Invisibly NULL.
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

