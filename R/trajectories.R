#' Longitudinal Temporal Trajectory Features Module
#'
#' @description
#' Extracts longitudinal measurement slopes, goodness of fit (R^2), volatility (rolling standard deviation),
#' min, and max using DuckDB's vectorized linear regression aggregations.
#'
#' @name trajectories
NULL

#' Extract Longitudinal Measurement Trajectory Features
#'
#' @description
#' Leverages DuckDB's native `regr_slope`, `regr_r2`, and `stddev_samp` aggregations
#' to extract trajectory features across clinical measurement series in `[index_date - window_days, index_date]`.
#'
#' @param con Active DBI/DuckDB connection.
#' @param cohort_table Character table name or data frame containing cohort index dates.
#' @param measurement_concept_ids Integer or numeric vector of measurement concept IDs.
#' @param window_days Look-back window in days prior to index date (default 365L).
#' @param index_date_col Column name for index date (default "cohort_start_date").
#' @param id_col Column identifying patient (default "person_id").
#'
#' @param offset_anchor Milestone date to anchor lookback windows ('visit_start_date' | 'visit_end_date' | 'index_date').
#' @param anchor Alias for offset_anchor.
#' @return A data.frame with wide trajectory features per patient.
#' @export
extract_temporal_trajectories <- function(con,
                                         cohort_table,
                                         measurement_concept_ids,
                                         window_days = 365L,
                                         index_date_col = "cohort_start_date",
                                         id_col = "person_id",
                                         offset_anchor = c("visit_start_date", "visit_end_date", "index_date"),
                                         anchor = NULL) {
  cids <- as.integer(measurement_concept_ids[!is.na(measurement_concept_ids)])
  if (length(cids) == 0) {
    stop("At least one measurement concept ID must be specified.")
  }

  temp_registered <- FALSE
  src_name <- "_cohort_traj_src"

  if (is.data.frame(cohort_table)) {
    duckdb::duckdb_register(con, src_name, as.data.frame(cohort_table))
    coh_rel <- src_name
    temp_registered <- TRUE
  } else if (is.character(cohort_table)) {
    coh_rel <- cohort_table
  } else {
    stop("`cohort_table` must be a character table name or data frame.")
  }

  on.exit({
    if (temp_registered) {
      tryCatch(duckdb::duckdb_unregister(con, src_name), error = function(e) NULL)
    }
  }, add = TRUE)

  desc_df <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", coh_rel))
  cols <- tolower(desc_df$column_name)

  oa <- tolower(if (!is.null(anchor)) anchor else if (!is.null(offset_anchor) && length(offset_anchor) == 1) offset_anchor else "visit_start_date")
  use_end <- oa %in% c("visit_end_date", "end", "discharge", "cohort_end_date")

  actual_id <- if (tolower(id_col) %in% cols) id_col else if ("person_id" %in% cols) "person_id" else "subject_id"
  actual_date <- if (use_end) {
    if ("cohort_end_date" %in% cols) "cohort_end_date" else if ("visit_end_date" %in% cols) "visit_end_date" else index_date_col
  } else {
    if (tolower(index_date_col) %in% cols) index_date_col else if ("cohort_start_date" %in% cols) "cohort_start_date" else "index_date"
  }

  cid_str <- paste(cids, collapse = ", ")

  sql <- sprintf("
    SELECT 
        c.%s AS person_id,
        c.%s AS index_date,
        m.measurement_concept_id,
        COUNT(m.value_as_number) AS n_obs,
        REGR_SLOPE(
            CAST(m.value_as_number AS DOUBLE),
            CAST(date_diff('day', c.%s, m.measurement_date) AS DOUBLE)
        ) AS slope_per_day,
        REGR_R2(
            CAST(m.value_as_number AS DOUBLE),
            CAST(date_diff('day', c.%s, m.measurement_date) AS DOUBLE)
        ) AS r2,
        STDDEV_SAMP(CAST(m.value_as_number AS DOUBLE)) AS volatility_stddev,
        MIN(CAST(m.value_as_number AS DOUBLE)) AS min_val,
        MAX(CAST(m.value_as_number AS DOUBLE)) AS max_val
    FROM %s c
    JOIN measurement m ON c.%s = m.person_id
    WHERE m.measurement_concept_id IN (%s)
      AND m.value_as_number IS NOT NULL
      AND date_diff('day', m.measurement_date, c.%s) BETWEEN 0 AND %d
    GROUP BY c.%s, c.%s, m.measurement_concept_id
  ", actual_id, actual_date, actual_date, actual_date, coh_rel, actual_id, cid_str, actual_date, as.integer(window_days), actual_id, actual_date)

  long_df <- DBI::dbGetQuery(con, sql)
  base_df <- DBI::dbGetQuery(con, sprintf("SELECT DISTINCT %s AS person_id, %s AS index_date FROM %s", actual_id, actual_date, coh_rel))

  if (nrow(long_df) == 0) {
    out_df <- base_df
    for (cid in cids) {
      out_df[[paste0("traj_", cid, "_slope")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_r2")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_volatility")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_min")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_max")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_count")]] <- 0L
    }
    if (actual_id != "person_id") names(out_df)[names(out_df) == "person_id"] <- actual_id
    if (actual_date != "index_date") names(out_df)[names(out_df) == "index_date"] <- actual_date
    return(out_df)
  }

  out_df <- base_df
  base_key <- paste(base_df$person_id, base_df$index_date, sep = "_")

  for (cid in cids) {
    sub <- long_df[long_df$measurement_concept_id == cid, , drop = FALSE]
    if (nrow(sub) == 0) {
      out_df[[paste0("traj_", cid, "_slope")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_r2")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_volatility")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_min")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_max")]] <- NA_real_
      out_df[[paste0("traj_", cid, "_count")]] <- 0L
    } else {
      sub_key <- paste(sub$person_id, sub$index_date, sep = "_")
      idx_match <- match(base_key, sub_key)

      out_df[[paste0("traj_", cid, "_slope")]] <- sub$slope_per_day[idx_match]
      out_df[[paste0("traj_", cid, "_r2")]] <- sub$r2[idx_match]
      out_df[[paste0("traj_", cid, "_volatility")]] <- sub$volatility_stddev[idx_match]
      out_df[[paste0("traj_", cid, "_min")]] <- sub$min_val[idx_match]
      out_df[[paste0("traj_", cid, "_max")]] <- sub$max_val[idx_match]

      cnts <- sub$n_obs[idx_match]
      cnts[is.na(cnts)] <- 0L
      out_df[[paste0("traj_", cid, "_count")]] <- cnts
    }
  }

  if (actual_id != "person_id") names(out_df)[names(out_df) == "person_id"] <- actual_id
  if (actual_date != "index_date") names(out_df)[names(out_df) == "index_date"] <- actual_date

  out_df
}

#' Extract Acute In-Hospital Episode Trajectories
#'
#' Evaluates acute physiologic shifts over `[visit_start_date, visit_end_date]`:
#' - Creatinine baseline, peak, nadir, delta from admission, and KDIGO Acute Kidney Injury staging (0-3).
#' - Shock Index (Heart Rate / Systolic Blood Pressure) and binary high shock indicator (> 1.0).
#' - Glycemic volatility (max Glucose - min Glucose) and intra-encounter glucose peak and nadir.
#'
#' @param con Active DBI/DuckDB connection.
#' @param cohort_table Character table name or data frame containing cohort index visits.
#' @param measurement_table Name of measurement table (default "measurement").
#' @param visit_table Name of visit table (default "visit_occurrence").
#' @param id_col Patient ID column (default "person_id").
#' @param visit_col Visit occurrence ID column (default "visit_occurrence_id").
#' @param creatinine_concept_ids Concept IDs for Serum Creatinine (default c(3016723L, 3020564L)).
#' @param glucose_concept_ids Concept IDs for Serum/Blood Glucose (default c(3004501L, 3000483L)).
#' @param heart_rate_concept_ids Concept IDs for Heart Rate (default c(3027018L, 3024171L)).
#' @param sbp_concept_ids Concept IDs for Systolic Blood Pressure (default c(3004249L, 3012888L)).
#' @return A data frame with acute trajectory metrics per patient and encounter.
#' @export
extract_acute_trajectories <- function(con,
                                       cohort_table = "cohort",
                                       measurement_table = "measurement",
                                       visit_table = "visit_occurrence",
                                       id_col = "person_id",
                                       visit_col = "visit_occurrence_id",
                                       creatinine_concept_ids = c(3016723L, 3020564L),
                                       glucose_concept_ids = c(3004501L, 3000483L),
                                       heart_rate_concept_ids = c(3027018L, 3024171L),
                                       sbp_concept_ids = c(3004249L, 3012888L)) {
  src_name <- "_cohort_acute_src"
  temp_registered <- FALSE

  if (is.data.frame(cohort_table)) {
    duckdb::duckdb_register(con, src_name, as.data.frame(cohort_table))
    coh_rel <- src_name
    temp_registered <- TRUE
  } else if (is.character(cohort_table)) {
    coh_rel <- cohort_table
  } else {
    stop("`cohort_table` must be a character table name or data frame.")
  }

  on.exit({
    if (temp_registered) {
      tryCatch(duckdb::duckdb_unregister(con, src_name), error = function(e) NULL)
    }
  }, add = TRUE)

  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", coh_rel))$column_name)
  actual_id <- if (tolower(id_col) %in% cols) id_col else if ("person_id" %in% cols) "person_id" else "subject_id"
  actual_vid <- if (tolower(visit_col) %in% cols) visit_col else "visit_occurrence_id"

  cr_cids <- paste(as.integer(creatinine_concept_ids), collapse = ", ")
  glu_cids <- paste(as.integer(glucose_concept_ids), collapse = ", ")
  hr_cids <- paste(as.integer(heart_rate_concept_ids), collapse = ", ")
  sbp_cids <- paste(as.integer(sbp_concept_ids), collapse = ", ")

  has_dates <- "visit_start_date" %in% cols && "visit_end_date" %in% cols
  has_cohort_dates <- "cohort_start_date" %in% cols && "cohort_end_date" %in% cols

  if (has_dates) {
    episode_sql <- sprintf("SELECT %s AS person_id, %s AS visit_id, visit_start_date, visit_end_date FROM %s",
                           actual_id, actual_vid, coh_rel)
  } else if (has_cohort_dates) {
    episode_sql <- sprintf("SELECT %s AS person_id, %s AS visit_id, cohort_start_date AS visit_start_date, cohort_end_date AS visit_end_date FROM %s",
                           actual_id, actual_vid, coh_rel)
  } else {
    episode_sql <- sprintf("
      SELECT c.%s AS person_id, c.%s AS visit_id,
             v.visit_start_date, v.visit_end_date
      FROM %s c
      JOIN %s v ON v.visit_occurrence_id = c.%s
    ", actual_id, actual_vid, coh_rel, visit_table, actual_vid)
  }

  m_cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", measurement_table))$column_name)
  has_m_date <- "measurement_date" %in% m_cols
  has_m_dt <- "measurement_datetime" %in% m_cols

  if (has_m_date && has_m_dt) {
    m_date_expr <- "m.measurement_date"
    m_time_expr <- "COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP))"
  } else if (has_m_dt) {
    m_date_expr <- "CAST(m.measurement_datetime AS DATE)"
    m_time_expr <- "m.measurement_datetime"
  } else if (has_m_date) {
    m_date_expr <- "m.measurement_date"
    m_time_expr <- "CAST(m.measurement_date AS TIMESTAMP)"
  } else {
    m_date_expr <- "CURRENT_DATE"
    m_time_expr <- "CURRENT_TIMESTAMP"
  }

  m_id_order <- if ("measurement_id" %in% m_cols) ", m.measurement_id" else ""

  sql <- sprintf("
    WITH episodes AS (
        %s
    ),
    in_stay_measurements AS (
        SELECT 
            e.person_id,
            e.visit_id,
            m.measurement_concept_id,
            %s AS measurement_date,
            %s AS m_time,
            CAST(m.value_as_number AS DOUBLE) AS val,
            ROW_NUMBER() OVER (
                PARTITION BY e.person_id, e.visit_id, m.measurement_concept_id 
                ORDER BY %s%s
            ) AS rn_first
        FROM episodes e
        JOIN %s m ON m.person_id = e.person_id
        WHERE m.value_as_number IS NOT NULL
          AND %s >= e.visit_start_date
          AND %s <= e.visit_end_date
    ),
    cr_stats AS (
        SELECT 
            person_id, visit_id,
            MAX(CASE WHEN rn_first = 1 THEN val END) AS cr_baseline,
            MAX(val) AS cr_peak,
            MIN(val) AS cr_nadir
        FROM in_stay_measurements
        WHERE measurement_concept_id IN (%s)
        GROUP BY person_id, visit_id
    ),
    glu_stats AS (
        SELECT 
            person_id, visit_id,
            MAX(CASE WHEN rn_first = 1 THEN val END) AS glu_baseline,
            MAX(val) AS glu_peak,
            MIN(val) AS glu_nadir
        FROM in_stay_measurements
        WHERE measurement_concept_id IN (%s)
        GROUP BY person_id, visit_id
    ),
    vitals_stats AS (
        SELECT 
            person_id, visit_id,
            MAX(CASE WHEN measurement_concept_id IN (%s) THEN val END) AS hr_peak,
            AVG(CASE WHEN measurement_concept_id IN (%s) THEN val END) AS hr_mean,
            MIN(CASE WHEN measurement_concept_id IN (%s) THEN val END) AS sbp_nadir,
            AVG(CASE WHEN measurement_concept_id IN (%s) THEN val END) AS sbp_mean
        FROM in_stay_measurements
        WHERE measurement_concept_id IN (%s, %s)
        GROUP BY person_id, visit_id
    )
    SELECT 
        e.person_id AS %s,
        e.visit_id AS %s,
        cr.cr_baseline,
        cr.cr_peak,
        cr.cr_nadir,
        (cr.cr_peak - cr.cr_baseline) AS cr_delta,
        cr.cr_baseline AS creatinine_admission,
        cr.cr_peak AS creatinine_peak,
        cr.cr_nadir AS creatinine_nadir,
        (cr.cr_peak - cr.cr_baseline) AS creatinine_delta,
        CASE 
            WHEN cr.cr_peak IS NULL OR cr.cr_baseline IS NULL THEN 0
            WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 3.0 OR (cr.cr_peak >= 4.0 AND (cr.cr_peak - cr.cr_baseline) >= 0.3) THEN 3
            WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 2.0 THEN 2
            WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 1.5 OR (cr.cr_peak - cr.cr_baseline) >= 0.3 THEN 1
            ELSE 0 
        END AS kdigo_aki_stage,
        glu.glu_baseline,
        glu.glu_peak,
        glu.glu_nadir,
        (glu.glu_peak - glu.glu_nadir) AS glycemic_volatility,
        (glu.glu_peak - glu.glu_nadir) AS glucose_volatility,
        v.hr_peak,
        v.sbp_nadir,
        ROUND(v.hr_peak / NULLIF(v.sbp_nadir, 0), 3) AS shock_index,
        CASE WHEN (v.hr_peak / NULLIF(v.sbp_nadir, 0)) >= 1.0 THEN 1 ELSE 0 END AS shock_index_elevated
    FROM episodes e
    LEFT JOIN cr_stats cr ON cr.person_id = e.person_id AND cr.visit_id = e.visit_id
    LEFT JOIN glu_stats glu ON glu.person_id = e.person_id AND glu.visit_id = e.visit_id
    LEFT JOIN vitals_stats v ON v.person_id = e.person_id AND v.visit_id = e.visit_id
    ORDER BY e.person_id, e.visit_id
  ", episode_sql, m_date_expr, m_time_expr, m_time_expr, m_id_order, measurement_table,
     m_date_expr, m_date_expr, cr_cids, glu_cids, hr_cids, hr_cids, sbp_cids, sbp_cids,
     hr_cids, sbp_cids, actual_id, actual_vid)

  DBI::dbGetQuery(con, sql)
}

