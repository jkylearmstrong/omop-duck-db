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
#' @return A data.frame with wide trajectory features per patient.
#' @export
extract_temporal_trajectories <- function(con,
                                         cohort_table,
                                         measurement_concept_ids,
                                         window_days = 365L,
                                         index_date_col = "cohort_start_date",
                                         id_col = "person_id") {
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

  actual_id <- if (tolower(id_col) %in% cols) id_col else if ("person_id" %in% cols) "person_id" else "subject_id"
  actual_date <- if (tolower(index_date_col) %in% cols) index_date_col else if ("cohort_start_date" %in% cols) "cohort_start_date" else "index_date"

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
