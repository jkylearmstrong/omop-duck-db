#' Bedside Clinical Risk Score Calculators for OMOP CDM Cohorts
#'
#' Calculates standardized clinical risk scores directly in DuckDB:
#' - LACE Index (30-day readmission/mortality: Length of stay, Acuity, Charlson, ED visits)
#' - HOSPITAL Score (30-day readmission: Hemoglobin, Oncology, Sodium, Procedure, Acuity, Admissions, LOS)
#' - SOFA Score (Sequential Organ Failure Assessment)
#' - CHA2DS2-VASc (Thromboembolism and stroke risk in cardiovascular disease)
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param cohort_table Cohort table or view name (default `"cohort"`).
#' @param scores Character vector of scores to compute (default `c("lace", "hospital", "chads_vasc", "sofa")`).
#' @return A `data.frame` containing cohort keys and requested score columns.
#' @export
calculate_bedside_scores <- function(con,
                                     cohort_table = "cohort",
                                     scores = c("lace", "hospital", "chads_vasc", "sofa")) {
  if (!inherits(con, "duckdb_connection")) {
    stop("`con` must be a DuckDB connection.", call. = FALSE)
  }
  scores_lower <- tolower(gsub("[-2]", "_", scores))

  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0;", cohort_table))$column_name)
  person_col <- if ("subject_id" %in% cols) "subject_id" else "person_id"
  start_col <- if ("cohort_start_date" %in% cols) "cohort_start_date" else "visit_start_date"
  end_col <- if ("cohort_end_date" %in% cols) "cohort_end_date" else "visit_end_date"

  base_df <- DBI::dbGetQuery(con, sprintf("
    SELECT
      %s AS person_id,
      %s AS cohort_start_date,
      %s AS cohort_end_date,
      CAST(GREATEST(1, date_diff('day', %s, %s)) AS INTEGER) AS los_days
    FROM %s
  ", person_col, start_col, end_col, start_col, end_col, cohort_table))

  if (nrow(base_df) == 0) {
    out <- base_df
    if ("lace" %in% scores_lower) { out$lace_score <- integer(0); out$lace_risk <- character(0) }
    if ("hospital" %in% scores_lower) { out$hospital_score <- integer(0); out$hospital_risk <- character(0) }
    if (any(c("chads_vasc", "cha_ds_vasc", "cha2ds2_vasc") %in% scores_lower)) out$chads_vasc_score <- integer(0)
    if ("sofa" %in% scores_lower) out$sofa_score <- integer(0)
    return(out)
  }

  duckdb::duckdb_register(con, "_score_cohort", base_df)
  on.exit(duckdb::duckdb_unregister(con, "_score_cohort"), add = TRUE)

  # LACE Index
  if ("lace" %in% scores_lower) {
    cci_df <- extract_charlson_index(con, cohort_table = cohort_table)
    if ("subject_id" %in% names(cci_df) && !"person_id" %in% names(cci_df)) {
      names(cci_df)[names(cci_df) == "subject_id"] <- "person_id"
    }
    base_df <- merge(base_df, cci_df[, c("person_id", "cohort_start_date", "charlson_index")],
                     by = c("person_id", "cohort_start_date"), all.x = TRUE)
    base_df$charlson_index[is.na(base_df$charlson_index)] <- 0L

    ed_df <- DBI::dbGetQuery(con, "
      SELECT
        c.person_id,
        c.cohort_start_date,
        COUNT(DISTINCT v.visit_occurrence_id) AS prior_ed_visits,
        MAX(CASE WHEN v.visit_concept_id = 9203 AND v.visit_start_date = c.cohort_start_date THEN 1 ELSE 0 END) AS is_ed_acuity
      FROM _score_cohort c
      LEFT JOIN visit_occurrence v
        ON v.person_id = c.person_id
       AND v.visit_concept_id = 9203
       AND v.visit_start_date >= c.cohort_start_date - INTERVAL '180' DAY
       AND v.visit_start_date < c.cohort_start_date
      GROUP BY c.person_id, c.cohort_start_date
    ")
    base_df <- merge(base_df, ed_df, by = c("person_id", "cohort_start_date"), all.x = TRUE)
    base_df$prior_ed_visits[is.na(base_df$prior_ed_visits)] <- 0L
    base_df$is_ed_acuity[is.na(base_df$is_ed_acuity)] <- 0L

    l_score <- vapply(base_df$los_days, function(d) {
      if (d < 1) 0L else if (d == 1) 1L else if (d == 2) 2L else if (d == 3) 3L else if (d <= 6) 4L else if (d <= 13) 5L else 7L
    }, integer(1))
    a_score <- base_df$is_ed_acuity * 3L
    c_score <- vapply(base_df$charlson_index, function(c) {
      if (c == 0) 0L else if (c == 1) 1L else if (c == 2) 2L else if (c == 3) 3L else 5L
    }, integer(1))
    e_score <- vapply(base_df$prior_ed_visits, function(e) {
      if (e == 0) 0L else if (e == 1) 1L else if (e == 2) 2L else if (e == 3) 3L else 4L
    }, integer(1))

    base_df$lace_score <- as.integer(l_score + a_score + c_score + e_score)
    base_df$lace_risk <- vapply(base_df$lace_score, function(s) {
      if (s <= 4) "Low" else if (s <= 9) "Moderate" else "High"
    }, character(1))
  }

  # HOSPITAL Score
  if ("hospital" %in% scores_lower) {
    hosp_df <- DBI::dbGetQuery(con, "
      SELECT
        c.person_id,
        c.cohort_start_date,
        MAX(CASE
          WHEN m.measurement_concept_id IN (3000963, 3004501, 3010813, 3023103, 3023599)
               OR m.measurement_source_value ILIKE '%hemo%' OR m.measurement_source_value ILIKE '%hgb%'
          THEN CASE WHEN m.value_as_number < 12.0 THEN 1 ELSE 0 END ELSE 0
        END) AS low_hemoglobin,
        MAX(CASE
          WHEN m.measurement_concept_id IN (3019550, 3000285, 3014576) OR m.measurement_source_value ILIKE '%sodium%'
          THEN CASE WHEN m.value_as_number < 135.0 THEN 1 ELSE 0 END ELSE 0
        END) AS low_sodium,
        MAX(CASE WHEN pr.procedure_occurrence_id IS NOT NULL THEN 1 ELSE 0 END) AS had_procedure,
        MAX(CASE
          WHEN cond.condition_concept_id IN (SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id = 443392)
               OR cond.condition_source_value ILIKE 'C%' OR cond.condition_source_value ILIKE '14%'
          THEN 1 ELSE 0
        END) AS oncology_history,
        COUNT(DISTINCT prev_v.visit_occurrence_id) AS prior_admissions
      FROM _score_cohort c
      LEFT JOIN measurement m
        ON m.person_id = c.person_id
       AND m.measurement_date >= c.cohort_start_date AND m.measurement_date <= c.cohort_end_date
      LEFT JOIN procedure_occurrence pr
        ON pr.person_id = c.person_id
       AND pr.procedure_date >= c.cohort_start_date AND pr.procedure_date <= c.cohort_end_date
      LEFT JOIN condition_occurrence cond
        ON cond.person_id = c.person_id
       AND cond.condition_start_date >= c.cohort_start_date - INTERVAL '365' DAY
       AND cond.condition_start_date <= c.cohort_start_date
      LEFT JOIN visit_occurrence prev_v
        ON prev_v.person_id = c.person_id
       AND prev_v.visit_concept_id = 9201
       AND prev_v.visit_start_date >= c.cohort_start_date - INTERVAL '365' DAY
       AND prev_v.visit_start_date < c.cohort_start_date
      GROUP BY c.person_id, c.cohort_start_date
    ")
    base_df <- merge(base_df, hosp_df, by = c("person_id", "cohort_start_date"), all.x = TRUE)
    base_df$low_hemoglobin[is.na(base_df$low_hemoglobin)] <- 0L
    base_df$low_sodium[is.na(base_df$low_sodium)] <- 0L
    base_df$had_procedure[is.na(base_df$had_procedure)] <- 0L
    base_df$oncology_history[is.na(base_df$oncology_history)] <- 0L
    base_df$prior_admissions[is.na(base_df$prior_admissions)] <- 0L

    h_pts <- base_df$low_hemoglobin * 1L
    o_pts <- base_df$oncology_history * 2L
    s_pts <- base_df$low_sodium * 1L
    p_pts <- base_df$had_procedure * 1L
    i_pts <- 1L
    t_pts <- vapply(base_df$prior_admissions, function(n) if (n >= 2) 2L else if (n == 1) 0L else 0L, integer(1))
    los_pts <- vapply(base_df$los_days, function(d) if (d >= 5) 2L else 0L, integer(1))

    base_df$hospital_score <- as.integer(h_pts + o_pts + s_pts + p_pts + i_pts + t_pts + los_pts)
    base_df$hospital_risk <- vapply(base_df$hospital_score, function(s) {
      if (s <= 4) "Low" else if (s <= 6) "Intermediate" else "High"
    }, character(1))
  }

  # CHA2DS2-VASc
  if (any(c("chads_vasc", "cha_ds_vasc", "cha2ds2_vasc") %in% scores_lower)) {
    chads_df <- DBI::dbGetQuery(con, "
      SELECT
        c.person_id,
        c.cohort_start_date,
        date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age,
        CASE WHEN p.gender_concept_id = 8532 THEN 1 ELSE 0 END AS is_female,
        MAX(CASE WHEN cond.condition_concept_id IN (316139, 434056, 433435, 4185932, 314378)
                 OR cond.condition_source_value ILIKE 'I50%' OR cond.condition_source_value ILIKE '428%' THEN 1 ELSE 0 END) AS chf,
        MAX(CASE WHEN cond.condition_concept_id IN (316866, 320128, 4326442)
                 OR cond.condition_source_value ILIKE 'I10%' OR cond.condition_source_value ILIKE '401%' THEN 1 ELSE 0 END) AS ht,
        MAX(CASE WHEN cond.condition_concept_id IN (201826, 443238, 316866)
                 OR cond.condition_source_value ILIKE 'E11%' OR cond.condition_source_value ILIKE '250%' THEN 1 ELSE 0 END) AS dm,
        MAX(CASE WHEN cond.condition_concept_id IN (443454, 4253880, 372924, 375557, 4310564)
                 OR cond.condition_source_value ILIKE 'I63%' OR cond.condition_source_value ILIKE 'G45%' THEN 1 ELSE 0 END) AS stroke,
        MAX(CASE WHEN cond.condition_concept_id IN (312327, 4329847, 4030506)
                 OR cond.condition_source_value ILIKE 'I21%' OR cond.condition_source_value ILIKE 'I70%' THEN 1 ELSE 0 END) AS vasc
      FROM _score_cohort c
      JOIN person p ON p.person_id = c.person_id
      LEFT JOIN condition_occurrence cond
        ON cond.person_id = c.person_id
       AND cond.condition_start_date <= c.cohort_start_date
      GROUP BY c.person_id, c.cohort_start_date, p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id
    ")
    base_df <- merge(base_df, chads_df, by = c("person_id", "cohort_start_date"), all.x = TRUE)
    base_df$age[is.na(base_df$age)] <- 60L
    base_df$is_female[is.na(base_df$is_female)] <- 0L
    base_df$chf[is.na(base_df$chf)] <- 0L
    base_df$ht[is.na(base_df$ht)] <- 0L
    base_df$dm[is.na(base_df$dm)] <- 0L
    base_df$stroke[is.na(base_df$stroke)] <- 0L
    base_df$vasc[is.na(base_df$vasc)] <- 0L

    age_pts <- vapply(base_df$age, function(a) if (a >= 75) 2L else if (a >= 65) 1L else 0L, integer(1))
    base_df$chads_vasc_score <- as.integer(
      base_df$chf * 1L + base_df$ht * 1L + age_pts + base_df$dm * 1L +
      base_df$stroke * 2L + base_df$vasc * 1L + base_df$is_female * 1L
    )
  }

  # SOFA Score
  if ("sofa" %in% scores_lower) {
    sofa_df <- DBI::dbGetQuery(con, "
      SELECT
        c.person_id,
        c.cohort_start_date,
        MIN(CASE WHEN m.measurement_concept_id IN (3024929, 3013650, 3007461) OR m.measurement_source_value ILIKE '%platelet%' THEN m.value_as_number END) AS min_platelets,
        MAX(CASE WHEN m.measurement_concept_id IN (3024128, 3017614) OR m.measurement_source_value ILIKE '%bilirubin%' THEN m.value_as_number END) AS max_bilirubin,
        MAX(CASE WHEN m.measurement_concept_id IN (3016723, 3001802) OR m.measurement_source_value ILIKE '%creatinine%' THEN m.value_as_number END) AS max_creatinine,
        MIN(CASE WHEN m.measurement_concept_id IN (3027597, 21492241) OR m.measurement_source_value ILIKE '%map%' THEN m.value_as_number END) AS min_map
      FROM _score_cohort c
      LEFT JOIN measurement m
        ON m.person_id = c.person_id
       AND m.measurement_date >= c.cohort_start_date AND m.measurement_date <= c.cohort_end_date
      GROUP BY c.person_id, c.cohort_start_date
    ")
    base_df <- merge(base_df, sofa_df, by = c("person_id", "cohort_start_date"), all.x = TRUE)

    s_plt <- vapply(base_df$min_platelets, function(p) {
      if (is.na(p) || p >= 150) 0L else if (p >= 100) 1L else if (p >= 50) 2L else if (p >= 20) 3L else 4L
    }, integer(1))
    s_bili <- vapply(base_df$max_bilirubin, function(b) {
      if (is.na(b) || b < 1.2) 0L else if (b <= 1.9) 1L else if (b <= 5.9) 2L else if (b <= 11.9) 3L else 4L
    }, integer(1))
    s_creat <- vapply(base_df$max_creatinine, function(c) {
      if (is.na(c) || c < 1.2) 0L else if (c <= 1.9) 1L else if (c <= 3.4) 2L else if (c <= 4.9) 3L else 4L
    }, integer(1))
    s_map <- vapply(base_df$min_map, function(m) {
      if (is.na(m) || m >= 70) 0L else 1L
    }, integer(1))

    base_df$sofa_score <- as.integer(s_plt + s_bili + s_creat + s_map)
  }

  out_cols <- c("person_id", "cohort_start_date", "cohort_end_date", "los_days")
  for (col in c("lace_score", "lace_risk", "hospital_score", "hospital_risk", "chads_vasc_score", "sofa_score")) {
    if (col %in% names(base_df)) out_cols <- c(out_cols, col)
  }
  base_df[, out_cols, drop = FALSE]
}
