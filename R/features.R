#' Extract Machine Learning Feature Matrix from OMOP CDM
#'
#' Builds vectorized patient feature matrices anchored to cohort index date T0 (`cohort_start_date`).
#' Computes baseline demographics and prior observation lookback window event counts (e.g. 30d, 180d, 365d),
#' directly connecting OMOP DuckDB cohorts to predictive modeling workflows.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_id Integer cohort definition ID defining the target population.
#' @param outcome_cohort_id Optional integer cohort definition ID defining binary outcome Y.
#' @param lookback_days Integer vector of days for prior observation windows (default `c(30, 180, 365)`).
#' @param include_demographics Logical. Include age at index, gender, race, ethnicity, and index year.
#' @param include_conditions Logical. Include condition occurrence counts across lookback windows.
#' @param include_drugs Logical. Include drug exposure counts across lookback windows.
#' @param include_procedures Logical. Include procedure occurrence counts across lookback windows.
#' @param include_measurements Logical. Include measurement counts across lookback windows.
#' @param format Output format: `"df"` (data.frame), `"arrow"`, or `"sparse"`.
#' @return A `data.frame`, Arrow Table, or sparse covariate data.frame.
#' @export
extract_patient_features <- function(con,
                                     cohort_id,
                                     outcome_cohort_id = NULL,
                                     lookback_days = c(30, 180, 365),
                                     include_demographics = TRUE,
                                     include_conditions = TRUE,
                                     include_drugs = TRUE,
                                     include_procedures = TRUE,
                                     include_measurements = TRUE,
                                     format = "df") {
  fmt <- tolower(trimws(format))
  windows <- sort(as.integer(lookback_days))

  select_items <- c("c.subject_id", "c.cohort_start_date", "c.cohort_end_date")

  if (!is.null(outcome_cohort_id)) {
    select_items <- c(select_items, "COALESCE(oc.outcome_flag, 0) AS y")
    outcome_join <- sprintf("
      LEFT JOIN (
          SELECT DISTINCT subject_id, 1 AS outcome_flag
          FROM cohort
          WHERE cohort_definition_id = %d
      ) oc ON c.subject_id = oc.subject_id
    ", as.integer(outcome_cohort_id))
  } else {
    outcome_join <- ""
  }

  if (isTRUE(include_demographics)) {
    select_items <- c(select_items,
      "date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age_at_index",
      "p.gender_concept_id",
      "p.race_concept_id",
      "p.ethnicity_concept_id",
      "YEAR(c.cohort_start_date) AS index_year"
    )
    demo_join <- "LEFT JOIN person p ON c.subject_id = p.person_id"
  } else {
    demo_join <- ""
  }

  lookback_joins <- character(0)

  if (isTRUE(include_conditions)) {
    cond_cols <- sprintf(
      "COUNT(DISTINCT CASE WHEN co.condition_start_date BETWEEN c.cohort_start_date - %d AND c.cohort_start_date - 1 THEN co.condition_occurrence_id END) AS condition_count_%dd",
      windows, windows
    )
    select_items <- c(select_items, cond_cols)
    lookback_joins <- c(lookback_joins, "LEFT JOIN condition_occurrence co ON c.subject_id = co.person_id")
  }

  if (isTRUE(include_drugs)) {
    drug_cols <- sprintf(
      "COUNT(DISTINCT CASE WHEN de.drug_exposure_start_date BETWEEN c.cohort_start_date - %d AND c.cohort_start_date - 1 THEN de.drug_exposure_id END) AS drug_count_%dd",
      windows, windows
    )
    select_items <- c(select_items, drug_cols)
    lookback_joins <- c(lookback_joins, "LEFT JOIN drug_exposure de ON c.subject_id = de.person_id")
  }

  if (isTRUE(include_procedures)) {
    proc_cols <- sprintf(
      "COUNT(DISTINCT CASE WHEN po.procedure_date BETWEEN c.cohort_start_date - %d AND c.cohort_start_date - 1 THEN po.procedure_occurrence_id END) AS procedure_count_%dd",
      windows, windows
    )
    select_items <- c(select_items, proc_cols)
    lookback_joins <- c(lookback_joins, "LEFT JOIN procedure_occurrence po ON c.subject_id = po.person_id")
  }

  if (isTRUE(include_measurements)) {
    meas_cols <- sprintf(
      "COUNT(DISTINCT CASE WHEN m.measurement_date BETWEEN c.cohort_start_date - %d AND c.cohort_start_date - 1 THEN m.measurement_id END) AS measurement_count_%dd",
      windows, windows
    )
    select_items <- c(select_items, meas_cols)
    lookback_joins <- c(lookback_joins, "LEFT JOIN measurement m ON c.subject_id = m.person_id")
  }

  select_clause <- paste(select_items, collapse = ",\n        ")
  all_joins <- paste(c(demo_join, outcome_join, lookback_joins)[nchar(c(demo_join, outcome_join, lookback_joins)) > 0], collapse = "\n    ")

  group_demo <- if (isTRUE(include_demographics)) ", p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id" else ""
  group_outcome <- if (!is.null(outcome_cohort_id)) ", oc.outcome_flag" else ""

  dense_sql <- sprintf("
    SELECT 
        %s
    FROM cohort c
    %s
    WHERE c.cohort_definition_id = %d
    GROUP BY 
        c.subject_id, c.cohort_start_date, c.cohort_end_date
        %s
        %s
    ORDER BY c.subject_id, c.cohort_start_date;
  ", select_clause, all_joins, as.integer(cohort_id), group_outcome, group_demo)

  df_dense <- DBI::dbGetQuery(con, dense_sql)

  if (fmt == "sparse") {
    feature_cols <- setdiff(names(df_dense), c("subject_id", "cohort_start_date", "cohort_end_date", "y"))
    sparse_rows <- list()
    for (row_idx in seq_len(nrow(df_dense))) {
      sub_id <- df_dense$subject_id[row_idx]
      for (col_idx in seq_along(feature_cols)) {
        col_name <- feature_cols[col_idx]
        val <- df_dense[[col_name]][row_idx]
        if (!is.na(val) && val != 0) {
          sparse_rows[[length(sparse_rows) + 1]] <- list(
            row_id = as.integer(row_idx),
            subject_id = sub_id,
            covariate_id = as.integer(100 + col_idx),
            covariate_name = col_name,
            covariate_value = as.numeric(val)
          )
        }
      }
    }
    if (length(sparse_rows) == 0) {
      return(data.frame(row_id = integer(0), subject_id = integer(0), covariate_id = integer(0), covariate_name = character(0), covariate_value = numeric(0)))
    }
    return(do.call(rbind, lapply(sparse_rows, as.data.frame, stringsAsFactors = FALSE)))
  }

  if (fmt == "arrow" && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(df_dense))
  }

  df_dense
}

#' Extract Temporal Baseline and Prior Utilization Features
#'
#' Extracts baseline historical utilization (lookback encounter counts by category,
#' recency, prior 30-day readmissions), stay characteristics (index LOS, discharge destination),
#' and demographics directly in DuckDB SQL.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_table Character string of cohort table or view (default "cohort").
#' @param cohort_id Optional integer cohort definition ID filter.
#' @param lookback_days Integer lookback window in days prior to admission (default 365).
#' @param encounter_categories Named list of integer vectors mapping category names to visit concept IDs.
#' @param include_demographics Logical. Include age, age group, sex, race, ethnicity (default TRUE).
#' @param include_stay_characteristics Logical. Include index LOS and discharge category (default TRUE).
#' @param format Output format: "df" (data.frame) or "arrow".
#' @return A `data.frame` or Arrow Table.
#' @export
extract_temporal_features <- function(con,
                                      cohort_table = "cohort",
                                      cohort_id = NULL,
                                      lookback_days = 365,
                                      encounter_categories = NULL,
                                      include_demographics = TRUE,
                                      include_stay_characteristics = TRUE,
                                      format = "df") {
  fmt <- tolower(trimws(format))
  filter_cohort <- if (!is.null(cohort_id)) sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

  cols_info <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", cohort_table))
  actual_cols <- stats::setNames(cols_info$column_name, toupper(cols_info$column_name))

  has_outcome <- "OUTCOME_FLAG" %in% names(actual_cols) || "Y" %in% names(actual_cols)
  outcome_col <- if ("OUTCOME_FLAG" %in% names(actual_cols)) actual_cols[["OUTCOME_FLAG"]] else if ("Y" %in% names(actual_cols)) actual_cols[["Y"]] else ""

  has_visit_id <- "VISIT_OCCURRENCE_ID" %in% names(actual_cols)
  visit_id_col <- if (has_visit_id) actual_cols[["VISIT_OCCURRENCE_ID"]] else ""

  if (has_visit_id) {
    visit_join <- sprintf('LEFT JOIN visit_occurrence v ON c."%s" = v.visit_occurrence_id', visit_id_col)
    visit_id_select <- sprintf('c."%s" AS visit_occurrence_id,', visit_id_col)
  } else {
    visit_join <- "LEFT JOIN visit_occurrence v ON c.subject_id = v.person_id AND c.cohort_start_date = v.visit_start_date"
    visit_id_select <- "v.visit_occurrence_id,"
  }

  stay_select <- if (isTRUE(include_stay_characteristics)) {
    "
      date_diff('day', c.cohort_start_date, c.cohort_end_date) AS index_los,
      v.discharged_to_concept_id,
      CASE 
          WHEN COALESCE(v.discharged_to_concept_id, 0) IN (8536, 4132319) THEN 'Home'
          WHEN COALESCE(v.discharged_to_concept_id, 0) IN (581476) THEN 'Home Health'
          WHEN COALESCE(v.discharged_to_concept_id, 0) IN (38004284, 8676, 8863, 8920, 38004285) THEN 'SNF/Rehab'
          WHEN COALESCE(v.discharged_to_concept_id, 0) = 44814650 THEN 'AMA'
          WHEN COALESCE(v.discharged_to_concept_id, 0) = 4216643 THEN 'Expired'
          ELSE 'Other'
      END AS discharge_category,
    "
  } else {
    ""
  }

  demo_select <- if (isTRUE(include_demographics)) {
    "
      date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age_at_admission,
      CASE 
          WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) < 18 THEN '<18'
          WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 18 AND 44 THEN '18-44'
          WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 45 AND 64 THEN '45-64'
          WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 65 AND 74 THEN '65-74'
          ELSE '75+'
      END AS age_group,
      p.gender_concept_id,
      CASE WHEN p.gender_concept_id = 8532 THEN 'Female' WHEN p.gender_concept_id = 8507 THEN 'Male' ELSE 'Other' END AS gender_name,
      p.race_concept_id,
      CASE WHEN p.race_concept_id = 8527 THEN 'White' WHEN p.race_concept_id = 8516 THEN 'Black' ELSE 'Other' END AS race_name,
      p.ethnicity_concept_id,
      CASE WHEN p.ethnicity_concept_id = 38003563 THEN 'Hispanic' WHEN p.ethnicity_concept_id = 38003564 THEN 'Non-Hispanic' ELSE 'Other' END AS ethnicity_name,
    "
  } else {
    ""
  }
  demo_join <- if (isTRUE(include_demographics)) "LEFT JOIN person p ON c.subject_id = p.person_id" else ""

  if (is.null(encounter_categories)) {
    enc_selects <- "
      COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9201) THEN pv.visit_occurrence_id END) AS prior_ip_count,
      COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9203) THEN pv.visit_occurrence_id END) AS prior_ed_count,
      COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9201, 9203) THEN pv.visit_occurrence_id END) AS prior_ip_ed_obs_count,
      COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9202) THEN pv.visit_occurrence_id END) AS prior_op_count,
      COUNT(DISTINCT pv.visit_occurrence_id) AS prior_visit_count,
    "
  } else {
    parts <- character(0)
    for (nm in names(encounter_categories)) {
      ids_str <- paste(as.integer(encounter_categories[[nm]]), collapse = ", ")
      parts <- c(parts, sprintf("COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (%s) THEN pv.visit_occurrence_id END) AS prior_%s_count", ids_str, nm))
    }
    parts <- c(parts, "COUNT(DISTINCT pv.visit_occurrence_id) AS prior_visit_count")
    enc_selects <- paste(paste(parts, collapse = ",\n      "), ",\n")
  }

  outcome_select <- if (has_outcome) sprintf('c."%s" AS outcome_flag,', outcome_col) else ""

  sql <- sprintf("
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        c.cohort_end_date,
        %s
        %s
        %s
        %s
        %s
        MIN(date_diff('day', pv.visit_start_date, c.cohort_start_date)) AS days_since_prior_encounter,
        MIN(CASE WHEN pv.visit_concept_id IN (9201, 9203) THEN date_diff('day', pv.visit_start_date, c.cohort_start_date) END) AS days_since_prior_ip_ed,
        COUNT(DISTINCT CASE 
            WHEN pv.visit_concept_id = 9201 
             AND EXISTS (
                 SELECT 1 FROM visit_occurrence prev_ip
                 WHERE prev_ip.person_id = c.subject_id
                   AND prev_ip.visit_occurrence_id != pv.visit_occurrence_id
                   AND prev_ip.visit_concept_id = 9201
                   AND pv.visit_start_date > prev_ip.visit_end_date
                   AND pv.visit_start_date <= prev_ip.visit_end_date + 30
             )
            THEN pv.visit_occurrence_id END) AS prior_readmissions_30d
    FROM %s c
    %s
    %s
    LEFT JOIN visit_occurrence pv 
      ON c.subject_id = pv.person_id 
     AND pv.visit_start_date >= (c.cohort_start_date - %d)
     AND pv.visit_start_date < c.cohort_start_date
    %s
    GROUP BY 
        c.subject_id, c.cohort_start_date, c.cohort_end_date
        %s
        %s
        %s
        %s
    ORDER BY c.subject_id, c.cohort_start_date;
  ", visit_id_select, outcome_select, stay_select, demo_select, enc_selects,
     cohort_table, visit_join, demo_join, as.integer(lookback_days), filter_cohort,
     if (has_visit_id) sprintf(', c."%s"', visit_id_col) else ", v.visit_occurrence_id",
     if (has_outcome) sprintf(', c."%s"', outcome_col) else "",
     if (isTRUE(include_stay_characteristics)) ", v.discharged_to_concept_id" else "",
     if (isTRUE(include_demographics)) ", p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id" else "")

  df_res <- DBI::dbGetQuery(con, sql)
  if (fmt == "arrow" && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(df_res))
  }
  df_res
}

#' Aggregate Concept Sets via Athena Ancestor Traversal
#'
#' Maps high-level concept sets to standard descendants and aggregates across cohort observation windows.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_table Cohort table or view name (default "cohort").
#' @param cohort_id Optional integer cohort definition ID filter.
#' @param concept_sets Named list of integer vectors of ancestor concept IDs. If NULL and domain="drug",
#'                     defaults to the 8 core chronic medication classes.
#' @param domain Character string: "drug", "condition", "procedure", or "measurement".
#' @param lookback_days Integer lookback window in days (default 365).
#' @param window Window strategy: "lookback", "stay", or "all".
#' @param as_flag Logical. If TRUE, returns 0/1 binary indicator; if FALSE, returns event counts.
#' @param format Output format: "df" or "arrow".
#' @return A `data.frame` or Arrow Table.
#' @export
aggregate_concept_sets <- function(con,
                                   cohort_table = "cohort",
                                   cohort_id = NULL,
                                   concept_sets = NULL,
                                   domain = "drug",
                                   lookback_days = 365,
                                   window = "lookback",
                                   as_flag = TRUE,
                                   format = "df") {
  sets <- concept_sets
  if (is.null(sets) && tolower(domain) == "drug") {
    sets <- DEFAULT_MEDICATION_CONCEPTS
  } else if (is.null(sets)) {
    stop("concept_sets list must be provided when domain is not 'drug'.")
  }

  filter_cohort <- if (!is.null(cohort_id)) sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id)) else ""
  domain_lower <- tolower(trimws(domain))

  if (domain_lower == "drug") {
    event_table <- "drug_exposure"
    concept_col <- "drug_concept_id"
    date_col <- "drug_exposure_start_date"
    id_col <- "drug_exposure_id"
  } else if (domain_lower == "condition") {
    event_table <- "condition_occurrence"
    concept_col <- "condition_concept_id"
    date_col <- "condition_start_date"
    id_col <- "condition_occurrence_id"
  } else if (domain_lower == "procedure") {
    event_table <- "procedure_occurrence"
    concept_col <- "procedure_concept_id"
    date_col <- "procedure_date"
    id_col <- "procedure_occurrence_id"
  } else if (domain_lower == "measurement") {
    event_table <- "measurement"
    concept_col <- "measurement_concept_id"
    date_col <- "measurement_date"
    id_col <- "measurement_id"
  } else {
    stop(sprintf("Unsupported domain '%s'. Use 'drug', 'condition', 'procedure', or 'measurement'.", domain))
  }

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_seed_concepts;")
  DBI::dbExecute(con, "CREATE TEMPORARY TABLE _temp_seed_concepts (set_name VARCHAR, ancestor_concept_id BIGINT);")
  for (s_name in names(sets)) {
    anc_ids <- as.integer(sets[[s_name]])
    for (aid in anc_ids) {
      DBI::dbExecute(con, sprintf("INSERT INTO _temp_seed_concepts VALUES ('%s', %d);", s_name, aid))
    }
  }

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _concept_set_resolved;")
  DBI::dbExecute(con, "
    CREATE TEMPORARY TABLE _concept_set_resolved AS
    SELECT s.set_name, ca.descendant_concept_id AS concept_id
    FROM _temp_seed_concepts s
    JOIN concept_ancestor ca ON s.ancestor_concept_id = ca.ancestor_concept_id
    UNION
    SELECT s.set_name, s.ancestor_concept_id AS concept_id
    FROM _temp_seed_concepts s;
  ")

  date_cond <- if (window == "lookback") {
    sprintf("e.%s >= (c.cohort_start_date - %d) AND e.%s < c.cohort_start_date", date_col, as.integer(lookback_days), date_col)
  } else if (window == "stay") {
    sprintf("e.%s >= c.cohort_start_date AND e.%s <= c.cohort_end_date", date_col, date_col)
  } else if (window == "all") {
    sprintf("e.%s <= c.cohort_end_date", date_col)
  } else {
    stop(sprintf("Unsupported window '%s'. Use 'lookback', 'stay', or 'all'.", window))
  }

  set_names <- names(sets)
  agg_exprs <- if (isTRUE(as_flag)) {
    paste(sprintf("MAX(CASE WHEN e.set_name = '%s' THEN 1 ELSE 0 END) AS %s", set_names, set_names), collapse = ",\n      ")
  } else {
    paste(sprintf("COUNT(DISTINCT CASE WHEN e.set_name = '%s' THEN e.%s END) AS %s", set_names, id_col, set_names), collapse = ",\n      ")
  }


  sql <- sprintf("
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        %s
    FROM %s c
    LEFT JOIN (
        SELECT e.person_id, e.%s, e.%s, r.set_name
        FROM %s e
        JOIN _concept_set_resolved r ON e.%s = r.concept_id
    ) e ON c.subject_id = e.person_id AND %s
    %s
    GROUP BY c.subject_id, c.cohort_start_date
    ORDER BY c.subject_id, c.cohort_start_date;
  ", agg_exprs, cohort_table, date_col, id_col, event_table, concept_col, date_cond, filter_cohort)

  res_df <- DBI::dbGetQuery(con, sql)
  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_seed_concepts; DROP TABLE IF EXISTS _concept_set_resolved;")

  fmt <- tolower(trimws(format))
  if (fmt == "arrow" && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(res_df))
  }
  res_df
}

#' Extract Consolidated Measurements and Vitals
#'
#' Extracts laboratory and vital sign measurements with selectable windowing strategies.
#' Supports LOINC consolidation groups (e.g. BUN, WBC, Creatinine) and aggregates numeric
#' measurement values (`value_as_number`) during the index stay or lookback window.
#'
#' @param con Active DuckDB DBI connection.
#' @param cohort_table Cohort table or view name (default "cohort").
#' @param cohort_id Optional integer cohort definition ID filter.
#' @param loinc_map Named list mapping measurement feature name to LOINC codes or concept IDs.
#'                  If NULL, defaults to the 14 consolidated acute labs and vitals.
#' @param strategy Aggregation strategy: 'last_before_discharge', 'first_on_admission', 'mean', 'median', 'min', 'max'.
#' @param window Window scope: 'stay', 'lookback', or 'all'.
#' @param lookback_days Lookback window in days when window='lookback' (default 365).
#' @param format Output format: 'df' or 'arrow'.
#' @return A `data.frame` or Arrow Table.
#' @export
extract_measurements <- function(con,
                                 cohort_table = "cohort",
                                 cohort_id = NULL,
                                 loinc_map = NULL,
                                 strategy = "last_before_discharge",
                                 window = "stay",
                                 lookback_days = NULL,
                                 format = "df") {
  active_map <- if (is.null(loinc_map)) c(DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS) else loinc_map

  strat <- tolower(trimws(strategy))
  strat_allowed <- c("last_before_discharge", "first_on_admission", "mean", "median", "min", "max")
  if (!strat %in% strat_allowed) {
    stop(sprintf("Unsupported strategy '%s'. Use one of %s.", strategy, paste(strat_allowed, collapse = ", ")))
  }

  filter_cohort <- if (!is.null(cohort_id)) sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_loinc_seeds;")
  DBI::dbExecute(con, "CREATE TEMPORARY TABLE _temp_loinc_seeds (lab_name VARCHAR, code_or_id VARCHAR, is_concept_id BOOLEAN);")
  for (lab_name in names(active_map)) {
    codes <- active_map[[lab_name]]
    for (cd in codes) {
      is_id <- is.numeric(cd) || grepl("^[0-9]+$", as.character(cd))
      DBI::dbExecute(con, sprintf("INSERT INTO _temp_loinc_seeds VALUES ('%s', '%s', %s);", lab_name, as.character(cd), if (is_id) "TRUE" else "FALSE"))
    }
  }

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_loinc_resolved;")
  DBI::dbExecute(con, "
    CREATE TEMPORARY TABLE _temp_loinc_resolved AS
    SELECT s.lab_name, CAST(s.code_or_id AS BIGINT) AS concept_id, s.code_or_id AS loinc_code
    FROM _temp_loinc_seeds s
    WHERE s.is_concept_id = TRUE
    UNION
    SELECT s.lab_name, c.concept_id, s.code_or_id AS loinc_code
    FROM _temp_loinc_seeds s
    JOIN concept c ON (c.vocabulary_id = 'LOINC' AND c.concept_code = s.code_or_id)
    WHERE s.is_concept_id = FALSE;
  ")

  date_cond <- if (window == "stay") {
    "m.measurement_date >= c.cohort_start_date AND m.measurement_date <= c.cohort_end_date"
  } else if (window == "lookback") {
    sprintf("m.measurement_date >= (c.cohort_start_date - %d) AND m.measurement_date < c.cohort_start_date", as.integer(lookback_days %||% 365))
  } else if (window == "all") {
    "m.measurement_date <= c.cohort_end_date"
  } else {
    stop(sprintf("Unsupported window '%s'. Use 'stay', 'lookback', or 'all'.", window))
  }

  cohort_filter_and <- if (!is.null(cohort_id)) sprintf("AND c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_matching_meas;")
  DBI::dbExecute(con, sprintf("
    CREATE TEMPORARY TABLE _temp_matching_meas AS
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        r.lab_name,
        m.measurement_id,
        m.measurement_date,
        m.measurement_datetime,
        m.value_as_number
    FROM %s c
    JOIN measurement m ON c.subject_id = m.person_id AND %s
    JOIN (
        SELECT lab_name, concept_id FROM _temp_loinc_resolved
        UNION
        SELECT s.lab_name, m2.measurement_concept_id AS concept_id
        FROM _temp_loinc_seeds s
        JOIN measurement m2 ON m2.measurement_source_value = s.code_or_id
    ) r ON m.measurement_concept_id = r.concept_id
    WHERE m.value_as_number IS NOT NULL
      %s;
  ", cohort_table, date_cond, cohort_filter_and))


  lab_names <- sort(names(active_map))

  agg_sql <- if (strat == "last_before_discharge") {
    "
      SELECT subject_id, cohort_start_date, lab_name, value_as_number
      FROM (
          SELECT 
              subject_id, cohort_start_date, lab_name, value_as_number,
              ROW_NUMBER() OVER (
                  PARTITION BY subject_id, cohort_start_date, lab_name 
                  ORDER BY COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP)) DESC, measurement_id DESC
              ) AS rn
          FROM _temp_matching_meas
      ) WHERE rn = 1
    "
  } else if (strat == "first_on_admission") {
    "
      SELECT subject_id, cohort_start_date, lab_name, value_as_number
      FROM (
          SELECT 
              subject_id, cohort_start_date, lab_name, value_as_number,
              ROW_NUMBER() OVER (
                  PARTITION BY subject_id, cohort_start_date, lab_name 
                  ORDER BY COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP)) ASC, measurement_id ASC
              ) AS rn
          FROM _temp_matching_meas
      ) WHERE rn = 1
    "
  } else if (strat == "mean") {
    "
      SELECT subject_id, cohort_start_date, lab_name, AVG(value_as_number) AS value_as_number
      FROM _temp_matching_meas
      GROUP BY subject_id, cohort_start_date, lab_name
    "
  } else if (strat == "median") {
    "
      SELECT subject_id, cohort_start_date, lab_name, MEDIAN(value_as_number) AS value_as_number
      FROM _temp_matching_meas
      GROUP BY subject_id, cohort_start_date, lab_name
    "
  } else if (strat == "min") {
    "
      SELECT subject_id, cohort_start_date, lab_name, MIN(value_as_number) AS value_as_number
      FROM _temp_matching_meas
      GROUP BY subject_id, cohort_start_date, lab_name
    "
  } else if (strat == "max") {
    "
      SELECT subject_id, cohort_start_date, lab_name, MAX(value_as_number) AS value_as_number
      FROM _temp_matching_meas
      GROUP BY subject_id, cohort_start_date, lab_name
    "
  }

  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_meas_aggregated;")
  DBI::dbExecute(con, sprintf("CREATE TEMPORARY TABLE _temp_meas_aggregated AS %s;", agg_sql))

  pivots <- paste(sprintf("MAX(CASE WHEN m.lab_name = '%s' THEN m.value_as_number END) AS %s", lab_names, lab_names), collapse = ",\n      ")

  pivot_sql <- sprintf("
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        %s
    FROM %s c
    LEFT JOIN _temp_meas_aggregated m ON c.subject_id = m.subject_id AND c.cohort_start_date = m.cohort_start_date
    %s
    GROUP BY c.subject_id, c.cohort_start_date
    ORDER BY c.subject_id, c.cohort_start_date;
  ", pivots, cohort_table, filter_cohort)

  res_df <- DBI::dbGetQuery(con, pivot_sql)
  DBI::dbExecute(con, "DROP TABLE IF EXISTS _temp_loinc_seeds; DROP TABLE IF EXISTS _temp_loinc_resolved; DROP TABLE IF EXISTS _temp_matching_meas; DROP TABLE IF EXISTS _temp_meas_aggregated;")

  fmt <- tolower(trimws(format))
  if (fmt == "arrow" && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(res_df))
  }
  res_df
}

