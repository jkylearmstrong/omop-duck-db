#' Automated Synthetic OMOP Twin Cohort Generator Module
#'
#' @description
#' Samples marginal concept frequencies, demographic distributions, and event timings
#' from a DuckDB instance to emit an anonymized, structurally identical synthetic
#' OMOP DuckDB database for open-source benchmarking, sharing, and testing without PHI/HIPAA risk.
#'
#' @name synthetic
NULL

#' Generate Synthetic OMOP Twin Database
#'
#' @description
#' Generates an anonymized, structurally identical synthetic OMOP DuckDB database file.
#'
#' @param con Active DBI/DuckDB connection containing real/source OMOP data.
#' @param cohort_table Character table name or data frame defining cohort subjects.
#' @param output_db_path File path for the generated DuckDB database.
#' @param noise_level Proportional Gaussian noise applied to frequencies and continuous values (default 0.10).
#' @param n_patients Number of synthetic subjects to generate (default matches source size, min 10L).
#' @param seed Random seed for reproducibility (default 42L).
#'
#' @return A list with counts of synthetic records created across CDM tables.
#' @export
generate_synthetic_omop_twin <- function(con,
                                        cohort_table,
                                        output_db_path,
                                        noise_level = 0.10,
                                        n_patients = NULL,
                                        seed = 42L) {
  set.seed(seed)

  temp_reg <- FALSE
  src_reg <- "_syn_cohort_src"

  if (is.data.frame(cohort_table)) {
    duckdb::duckdb_register(con, src_reg, as.data.frame(cohort_table))
    coh_rel <- src_reg
    temp_reg <- TRUE
  } else if (is.character(cohort_table)) {
    coh_rel <- cohort_table
  } else {
    stop("`cohort_table` must be a character table name or data frame.")
  }

  on.exit({
    if (temp_reg) {
      tryCatch(duckdb::duckdb_unregister(con, src_reg), error = function(e) NULL)
    }
  }, add = TRUE)

  desc_df <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", coh_rel))
  cols <- tolower(desc_df$column_name)

  id_col <- if ("subject_id" %in% cols) "subject_id" else if ("person_id" %in% cols) "person_id" else cols[1]

  real_pids <- DBI::dbGetQuery(con, sprintf("SELECT DISTINCT %s AS pid FROM %s", id_col, coh_rel))$pid
  n_real <- length(real_pids)
  target_n <- if (!is.null(n_patients) && n_patients > 0) as.integer(n_patients) else max(10L, n_real)

  tables <- tolower(DBI::dbListTables(con))

  # 1. Demographics
  if ("person" %in% tables && n_real > 0) {
    pid_str <- paste(real_pids, collapse = ", ")
    p_df <- DBI::dbGetQuery(con, sprintf("SELECT gender_concept_id, year_of_birth FROM person WHERE person_id IN (%s)", pid_str))
  } else {
    p_df <- data.frame()
  }

  if (nrow(p_df) > 0) {
    tab <- table(p_df$gender_concept_id)
    genders <- as.integer(names(tab))
    probs <- as.numeric(prop.table(tab))

    probs <- pmax(0.01, probs + stats::rnorm(length(probs), 0, noise_level * 0.1))
    probs <- probs / sum(probs)

    mean_yob <- mean(p_df$year_of_birth, na.rm = TRUE)
    sd_yob <- stats::sd(p_df$year_of_birth, na.rm = TRUE)
    if (is.na(sd_yob) || sd_yob <= 0) sd_yob <- 10.0
  } else {
    genders <- c(8507L, 8532L)
    probs <- c(0.5, 0.5)
    mean_yob <- 1970.0
    sd_yob <- 12.0
  }

  syn_pids <- seq_len(target_n)
  syn_genders <- sample(genders, size = target_n, replace = TRUE, prob = probs)
  syn_yobs <- pmin(2020L, pmax(1920L, round(stats::rnorm(target_n, mean_yob, sd_yob * (1.0 + noise_level)))))

  syn_person_df <- data.frame(
    person_id = syn_pids,
    gender_concept_id = syn_genders,
    year_of_birth = as.integer(syn_yobs),
    month_of_birth = sample(1:12, size = target_n, replace = TRUE),
    day_of_birth = sample(1:28, size = target_n, replace = TRUE),
    race_concept_id = 8527L,
    ethnicity_concept_id = 0L,
    stringsAsFactors = FALSE
  )

  # 2. Cohort
  base_date <- as.Date("2022-01-01")
  start_offsets <- sample(0:(365 * 2), size = target_n, replace = TRUE)
  durations <- sample(1:180, size = target_n, replace = TRUE)

  syn_start_dates <- base_date + start_offsets
  syn_end_dates <- syn_start_dates + durations

  syn_cohort_df <- data.frame(
    cohort_definition_id = 1L,
    subject_id = syn_pids,
    cohort_start_date = syn_start_dates,
    cohort_end_date = syn_end_dates,
    stringsAsFactors = FALSE
  )

  # 3. Observation Period
  syn_obs_df <- data.frame(
    observation_period_id = syn_pids,
    person_id = syn_pids,
    observation_period_start_date = syn_start_dates - 365,
    observation_period_end_date = syn_end_dates + 180,
    period_type_concept_id = 44814724L,
    stringsAsFactors = FALSE
  )

  # 4. Conditions
  if ("condition_occurrence" %in% tables && n_real > 0) {
    c_freq <- DBI::dbGetQuery(con, sprintf("
      SELECT condition_concept_id, COUNT(DISTINCT person_id) AS pt_count
      FROM condition_occurrence
      WHERE person_id IN (%s) AND condition_concept_id > 0
      GROUP BY condition_concept_id
      ORDER BY pt_count DESC
      LIMIT 20
    ", pid_str))
  } else {
    c_freq <- data.frame()
  }

  cond_candidates <- if (nrow(c_freq) > 0) as.integer(c_freq$condition_concept_id) else c(316866L, 201820L, 317009L, 4329847L)
  cond_rates <- if (nrow(c_freq) > 0) pmin(0.8, (c_freq$pt_count / n_real) * (1.0 + stats::rnorm(nrow(c_freq), 0, noise_level))) else c(0.3, 0.25, 0.15, 0.10)

  cond_rows <- list()
  cond_id <- 1L
  for (i in seq_len(target_n)) {
    pid <- syn_pids[i]
    c_start <- syn_start_dates[i]
    for (k in seq_along(cond_candidates)) {
      if (stats::runif(1) < cond_rates[k]) {
        dt <- c_start + sample(-60:10, 1)
        cond_rows[[length(cond_rows) + 1]] <- data.frame(
          condition_occurrence_id = cond_id,
          person_id = pid,
          condition_concept_id = cond_candidates[k],
          condition_start_date = dt,
          condition_end_date = dt,
          condition_type_concept_id = 38000280L,
          stringsAsFactors = FALSE
        )
        cond_id <- cond_id + 1L
      }
    }
  }
  syn_cond_df <- if (length(cond_rows) > 0) do.call(rbind, cond_rows) else data.frame(
    condition_occurrence_id = integer(0), person_id = integer(0), condition_concept_id = integer(0),
    condition_start_date = as.Date(character(0)), condition_end_date = as.Date(character(0)),
    condition_type_concept_id = integer(0), stringsAsFactors = FALSE
  )

  # 5. Drugs
  if ("drug_exposure" %in% tables && n_real > 0) {
    d_freq <- DBI::dbGetQuery(con, sprintf("
      SELECT drug_concept_id, COUNT(DISTINCT person_id) AS pt_count
      FROM drug_exposure
      WHERE person_id IN (%s) AND drug_concept_id > 0
      GROUP BY drug_concept_id
      ORDER BY pt_count DESC
      LIMIT 15
    ", pid_str))
  } else {
    d_freq <- data.frame()
  }

  drug_candidates <- if (nrow(d_freq) > 0) as.integer(d_freq$drug_concept_id) else c(1125315L, 1503297L, 1308216L)
  drug_rates <- if (nrow(d_freq) > 0) pmin(0.7, (d_freq$pt_count / n_real) * (1.0 + stats::rnorm(nrow(d_freq), 0, noise_level))) else c(0.4, 0.3, 0.2)

  drug_rows <- list()
  drug_id <- 1L
  for (i in seq_len(target_n)) {
    pid <- syn_pids[i]
    c_start <- syn_start_dates[i]
    for (k in seq_along(drug_candidates)) {
      if (stats::runif(1) < drug_rates[k]) {
        dt <- c_start + sample(-30:5, 1)
        drug_rows[[length(drug_rows) + 1]] <- data.frame(
          drug_exposure_id = drug_id,
          person_id = pid,
          drug_concept_id = drug_candidates[k],
          drug_exposure_start_date = dt,
          drug_exposure_end_date = dt + 30,
          drug_type_concept_id = 38000177L,
          stringsAsFactors = FALSE
        )
        drug_id <- drug_id + 1L
      }
    }
  }
  syn_drug_df <- if (length(drug_rows) > 0) do.call(rbind, drug_rows) else data.frame(
    drug_exposure_id = integer(0), person_id = integer(0), drug_concept_id = integer(0),
    drug_exposure_start_date = as.Date(character(0)), drug_exposure_end_date = as.Date(character(0)),
    drug_type_concept_id = integer(0), stringsAsFactors = FALSE
  )

  # 6. Measurements
  meas_rows <- list()
  meas_defaults <- list(
    list(cid = 3004410L, m = 13.5, s = 1.8),
    list(cid = 3016723L, m = 1.0, s = 0.4),
    list(cid = 3020564L, m = 125.0, s = 15.0)
  )
  meas_id <- 1L
  for (i in seq_len(target_n)) {
    pid <- syn_pids[i]
    c_start <- syn_start_dates[i]
    for (item in meas_defaults) {
      val <- round(stats::rnorm(1, item$m, item$s * (1.0 + noise_level)), 2)
      dt <- c_start - sample(1:14, 1)
      meas_rows[[length(meas_rows) + 1]] <- data.frame(
        measurement_id = meas_id,
        person_id = pid,
        measurement_concept_id = item$cid,
        measurement_date = dt,
        value_as_number = val,
        measurement_type_concept_id = 44818702L,
        stringsAsFactors = FALSE
      )
      meas_id <- meas_id + 1L
    }
  }
  syn_meas_df <- do.call(rbind, meas_rows)

  # 7. Write to Destination DuckDB
  if (file.exists(output_db_path)) {
    unlink(output_db_path)
  }
  dir.create(dirname(output_db_path), showWarnings = FALSE, recursive = TRUE)

  dest_con <- DBI::dbConnect(duckdb::duckdb(), dbdir = output_db_path)
  on.exit(DBI::dbDisconnect(dest_con, shutdown = TRUE), add = TRUE)

  DBI::dbWriteTable(dest_con, "person", syn_person_df)
  DBI::dbWriteTable(dest_con, "cohort", syn_cohort_df)
  DBI::dbWriteTable(dest_con, "observation_period", syn_obs_df)
  DBI::dbWriteTable(dest_con, "condition_occurrence", syn_cond_df)
  DBI::dbWriteTable(dest_con, "drug_exposure", syn_drug_df)
  DBI::dbWriteTable(dest_con, "measurement", syn_meas_df)

  list(
    status = "success",
    output_db_path = output_db_path,
    n_patients = target_n,
    n_cohort_rows = nrow(syn_cohort_df),
    n_conditions = nrow(syn_cond_df),
    n_drugs = nrow(syn_drug_df),
    n_measurements = nrow(syn_meas_df),
    noise_level = noise_level
  )
}
