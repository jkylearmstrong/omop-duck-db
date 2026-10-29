#' Days used when a patient has no prior visit of a type (the 365-day sentinel)
#'
#' Missing `days_since_prior_*` values are replaced by this constant, then clipped to >= 1, exactly as in
#' `scripts/engineer_clinical_features.py`. Mirror of `omop_etl.velocity.NO_PRIOR_VISIT_DAYS`.
#' @format An integer constant.
#' @export
NO_PRIOR_VISIT_DAYS <- 365L

#' Names of the features added by [engineer_clinical_features()]
#' @format A character vector.
#' @export
VELOCITY_FEATURES <- c(
  "utilization_density", "ip_velocity", "ed_velocity", "recent_ip_ratio", "readmission_intensity", "mean_prior_ip_los",
  "lace_los_score", "lace_ed_score", "lace_comorb_score", "lace_proxy_total", "shock_index", "pulse_pressure",
  "mean_arterial_pressure", "bun_to_creatinine", "bun_creat_azotemia_flag", "severe_anemia_flag",
  "hypoalbuminemia_flag", "egfr_stage4_5_flag", "hyponatremia_flag", "hyperkalemia_flag", "elevated_anion_gap_flag",
  "polypharmacy_count")

#' Input columns required by [engineer_clinical_features()]
#' @format A character vector.
#' @export
REQUIRED_VELOCITY_INPUTS <- c(
  "days_since_prior_encounter", "days_since_prior_ip_ed", "prior_ip_os_ed_count", "prior_ip_count", "prior_ed_count",
  "prior_encounters_all_count", "readmission_count_twelve_months", "prior_ip_los_days", "index_los_days",
  "elix_pre_index_conditions_count", "systolic_bp", "diastolic_bp", "heart_rate", "lab_bun", "lab_creatinine",
  "lab_hemoglobin", "lab_albumin", "lab_egfr", "lab_sodium", "lab_potassium", "lab_anion_gap")

#' Default measurement concepts used by [extract_velocity_features()]
#'
#' Standard OMOP concepts (LOINC): heart rate, systolic BP, diastolic BP, BUN, serum creatinine.
#' @format A named list of integer vectors.
#' @export
VELOCITY_CONCEPTS <- list(heart_rate = 3027018L, systolic_bp = 3004249L, diastolic_bp = 3012888L,
                          lab_bun = 3013682L, lab_creatinine = 3016723L,
                          lab_sodium = c(3019550L, 3014576L), lab_potassium = c(3023103L, 3028437L),
                          lab_chloride = c(3014576L, 3010813L), lab_bicarbonate = c(3007220L, 3016462L),
                          lab_anion_gap = c(3037278L, 3013444L, 3020564L),
                          lab_egfr = c(40764999L, 1619025L, 36306178L, 36303797L))

.fill <- function(x, v) { x[is.na(x)] <- v; x }
.clip <- function(x, lower = -Inf, upper = Inf) pmin(pmax(x, lower), upper)  # NA stays NA

# Race-free CKD-EPI 2021 eGFR (mL/min/1.73m^2); port of `omop_etl.velocity.compute_ckd_epi_2021`.
compute_ckd_epi_2021 <- function(creatinine, age, is_female) {
  fem <- as.logical(is_female)
  kappa <- ifelse(fem, 0.7, 0.9)
  alpha <- ifelse(fem, -0.241, -0.302)
  scr_kappa <- pmax(creatinine, 0.1) / kappa
  142 * pmin(scr_kappa, 1)^alpha * pmax(scr_kappa, 1)^(-1.2) * 0.9938^age * ifelse(fem, 1.012, 1)
}

# Fills `lab_egfr` (CKD-EPI 2021, FR-A6) and `lab_anion_gap` (Na - (Cl + HCO3)); mirror of `omop_etl.velocity.derive_labs`.
.derive_labs <- function(raw) {
  if (all(c("lab_creatinine", "age", "gender_concept_id") %in% names(raw))) {
    has_creat <- !is.na(raw$lab_creatinine)
    if (any(has_creat)) {
      calc_egfr <- compute_ckd_epi_2021(.fill(raw$lab_creatinine, 1.0), .fill(raw$age, 65.0), raw$gender_concept_id == 8532)
      egfr_cur <- if ("lab_egfr" %in% names(raw)) raw$lab_egfr else rep(NA_real_, nrow(raw))
      raw$lab_egfr <- ifelse(has_creat & (is.na(egfr_cur) | egfr_cur >= 60.0), calc_egfr, egfr_cur)
    }
  }
  if (all(c("lab_sodium", "lab_chloride", "lab_bicarbonate") %in% names(raw))) {
    calc_ag <- raw$lab_sodium - (raw$lab_chloride + raw$lab_bicarbonate)
    ag_cur <- if ("lab_anion_gap" %in% names(raw)) raw$lab_anion_gap else rep(NA_real_, nrow(raw))
    raw$lab_anion_gap <- ifelse(is.na(ag_cur), calc_ag, ag_cur)
  }
  raw
}

#' Clinical velocity, LACE-proxy, hemodynamic and organ-dysfunction features
#'
#' Faithful port of `engineer_features()` (sections 1-5) in `scripts/engineer_clinical_features.py`, pinned to it by a
#' golden file produced by the script itself (shared with the Python tests). Same function as
#' `omop_etl.velocity.engineer_clinical_features`.
#'
#' Reference semantics worth knowing: missing `days_since_prior_*` become 365 then clip to >= 1; LACE length-of-stay points
#' use an *exact integer match* (1, 2, 3 -> 1, 2, 3; 4-6 -> 4; 7-13 -> 5; >= 14 -> 7), so a fractional or zero LOS scores 0;
#' missing vitals and labs are imputed with fixed normal values (HR 75, SBP 120, DBP 80, BUN 15, creatinine 1, hemoglobin
#' 13, albumin 3.8, eGFR 85, sodium 140, potassium 4.2, anion gap 10), so **missing is indistinguishable from normal
#' downstream**. Absent input columns are created as all-missing and reported with a warning. The reference's target-encoded
#' `site_baseline_risk` leaks validation labels and is not included; see [site_baseline_risk()].
#'
#' @param df A data.frame with (some of) [REQUIRED_VELOCITY_INPUTS]; `med_*` columns (not `*_legacy`) feed
#'   `polypharmacy_count`, and `elix_*` / `med_*` columns feed the `combo_*` interactions when present.
#' @param warn_missing Warn about absent input columns.
#' @return `df` with [VELOCITY_FEATURES] (and `combo_*`) added.
#' @export
engineer_clinical_features <- function(df, warn_missing = TRUE) {
  feat <- as.data.frame(df, stringsAsFactors = FALSE)
  for (nm in names(feat)) if (is.character(feat[[nm]]) || is.factor(feat[[nm]])) {
    num <- suppressWarnings(as.numeric(as.character(feat[[nm]])))
    if (sum(!is.na(num)) == sum(!is.na(feat[[nm]]))) feat[[nm]] <- num  # text columns are left alone (unlike the reference)
  }
  absent <- setdiff(REQUIRED_VELOCITY_INPUTS, names(feat))
  if (length(absent)) {
    if (warn_missing) warning("engineer_clinical_features: input columns absent, treated as all-missing (default-imputed): ",
                              paste(absent, collapse = ", "), call. = FALSE)
    for (c_ in absent) feat[[c_]] <- NA_real_
  }
  sent <- as.numeric(NO_PRIOR_VISIT_DAYS)
  days_enc <- .clip(.fill(feat$days_since_prior_encounter, sent), lower = 1)
  days_ip_ed <- .clip(.fill(feat$days_since_prior_ip_ed, sent), lower = 1)
  feat$utilization_density <- feat$prior_ip_os_ed_count / (days_enc / 30 + 1)
  feat$ip_velocity <- feat$prior_ip_count / (days_ip_ed / 30 + 1)
  feat$ed_velocity <- feat$prior_ed_count / (days_ip_ed / 30 + 1)
  feat$recent_ip_ratio <- feat$prior_ip_count / (feat$prior_encounters_all_count + 1)
  feat$readmission_intensity <- feat$readmission_count_twelve_months / (feat$prior_ip_count + 1)
  feat$mean_prior_ip_los <- feat$prior_ip_los_days / (feat$prior_ip_count + 1)

  los <- .fill(feat$index_los_days, 1)
  lace_los <- rep(0, nrow(feat))
  lace_los[los == 1] <- 1; lace_los[los == 2] <- 2; lace_los[los == 3] <- 3
  lace_los[los >= 4 & los <= 6] <- 4; lace_los[los >= 7 & los <= 13] <- 5; lace_los[los >= 14] <- 7
  feat$lace_los_score <- lace_los
  ed <- .fill(feat$prior_ed_count, 0)
  lace_ed <- rep(0, nrow(feat))
  lace_ed[ed == 1] <- 1; lace_ed[ed == 2] <- 2; lace_ed[ed == 3] <- 3; lace_ed[ed >= 4] <- 4
  feat$lace_ed_score <- lace_ed
  comorb <- .clip(.fill(feat$elix_pre_index_conditions_count, 0), upper = 5)
  feat$lace_comorb_score <- comorb
  feat$lace_proxy_total <- lace_los + lace_ed + comorb + 3

  sbp <- .clip(.fill(feat$systolic_bp, 120), lower = 60)
  dbp <- .clip(.fill(feat$diastolic_bp, 80), lower = 30)
  hr <- .fill(feat$heart_rate, 75)
  feat$shock_index <- hr / sbp
  feat$pulse_pressure <- sbp - dbp
  feat$mean_arterial_pressure <- dbp + feat$pulse_pressure / 3

  bun <- .fill(feat$lab_bun, 15)
  creat <- .clip(.fill(feat$lab_creatinine, 1), lower = 0.2)
  feat$bun_to_creatinine <- bun / creat
  feat$bun_creat_azotemia_flag <- as.numeric(feat$bun_to_creatinine > 20)
  feat$severe_anemia_flag <- as.numeric(.fill(feat$lab_hemoglobin, 13) < 8.5)
  feat$hypoalbuminemia_flag <- as.numeric(.fill(feat$lab_albumin, 3.8) < 3.2)

  # Dynamic Race-Free CKD-EPI 2021 eGFR (FR-A6)
  has_demog <- (("age" %in% names(feat) || "age_at_admission" %in% names(feat)) && ("gender_concept_id" %in% names(feat) || "is_female" %in% names(feat)))
  if (has_demog) {
    age_col <- if ("age" %in% names(feat)) .fill(feat$age, 65) else .fill(feat$age_at_admission, 65)
    is_fem <- if ("gender_concept_id" %in% names(feat)) (feat$gender_concept_id == 8532) else as.logical(feat$is_female)
    computed_egfr <- compute_ckd_epi_2021(creat, age_col, is_fem)
    egfr_val <- feat$lab_egfr
    resolved_egfr <- ifelse(is.na(egfr_val) | egfr_val >= 60.0, computed_egfr, egfr_val)
    feat$egfr_stage4_5_flag <- as.numeric(.fill(resolved_egfr, 85) < 30)
  } else {
    feat$egfr_stage4_5_flag <- as.numeric(.fill(feat$lab_egfr, 85) < 30)
  }

  feat$hyponatremia_flag <- as.numeric(.fill(feat$lab_sodium, 140) < 135)
  feat$hyperkalemia_flag <- as.numeric(.fill(feat$lab_potassium, 4.2) > 5.1)

  # Dynamic Anion Gap Stoichiometric Fallback (FR-A6): Na - (Cl + HCO3)
  has_lytes <- all(c("lab_sodium", "lab_chloride", "lab_bicarbonate") %in% names(feat))
  if (has_lytes) {
    calc_ag <- feat$lab_sodium - (feat$lab_chloride + feat$lab_bicarbonate)
    resolved_ag <- ifelse(is.na(feat$lab_anion_gap), calc_ag, feat$lab_anion_gap)
    feat$elevated_anion_gap_flag <- as.numeric(.fill(resolved_ag, 10) > 14)
  } else {
    feat$elevated_anion_gap_flag <- as.numeric(.fill(feat$lab_anion_gap, 10) > 14)
  }

  med_cols <- names(feat)[startsWith(names(feat), "med_") & !endsWith(names(feat), "_legacy")]
  feat$polypharmacy_count <- if (length(med_cols)) rowSums(feat[, med_cols, drop = FALSE], na.rm = TRUE) else 0
  has <- function(...) all(c(...) %in% names(feat))
  if (has("elix_chf", "elix_diabetes_complicated")) feat$combo_chf_dm <- feat$elix_chf * feat$elix_diabetes_complicated
  if (has("elix_copd", "med_systemic_corticosteroids")) feat$combo_copd_steroids <- feat$elix_copd * feat$med_systemic_corticosteroids
  if (has("elix_renal_failure")) feat$combo_ckd_hyperk <- feat$elix_renal_failure * feat$hyperkalemia_flag
  if (has("elix_chf", "med_raas")) feat$combo_chf_raas <- feat$elix_chf * feat$med_raas
  feat
}

#' Per-site outcome rate as a feature, from training rows only
#'
#' Replacement for the reference's section 6 target encoding, which computes the rate over every row (validation labels
#' included). Sites unseen in the training rows get the training-wide rate.
#'
#' @param df Data frame with the site and label columns.
#' @param site_col,label_col Column names.
#' @param train_mask Logical vector selecting training rows (`NULL` = all rows; only appropriate inside a CV fold).
#' @return Numeric vector, one value per row.
#' @export
site_baseline_risk <- function(df, site_col = "site_source", label_col = "readmitted_30d", train_mask = NULL) {
  mask <- if (is.null(train_mask)) rep(TRUE, nrow(df)) else as.logical(train_mask)
  train <- df[mask, , drop = FALSE]
  rates <- tapply(train[[label_col]], train[[site_col]], mean)
  out <- unname(rates[as.character(df[[site_col]])])
  out[is.na(out)] <- mean(train[[label_col]])
  out
}

#' Raw utilization inputs plus the engineered velocity set for a cohort
#'
#' Builds, for every distinct (`subject_id`, `cohort_start_date`), the reference script's raw utilization inputs from
#' `visit_occurrence` (only visits strictly before the admission date inside `lookback_days`; `days_since_*` are `NA` when
#' there is no prior visit), the first admission-day heart rate / SBP / DBP / BUN / creatinine, optionally joins
#' `extra_features` (labs, `elix_*`, `med_*`), and applies [engineer_clinical_features()]. Mirror of
#' `omop_etl.velocity.extract_velocity_features`.
#'
#' @param con DuckDB DBI connection with `visit_occurrence`, `measurement` and the cohort table.
#' @param cohort_table Table or view with `subject_id` and `cohort_start_date` (optionally `cohort_end_date`,
#'   `cohort_definition_id`).
#' @param lookback_days,readmit_gap_days Window lengths in days (>= 1).
#' @param ip_concepts,ed_concepts,os_concepts Visit concept ids counted as inpatient / emergency / outpatient-or-observation. The defaults count concept 262 (emergency-to-inpatient) as inpatient only, as the legacy study did.
#' @param concepts Overrides for [VELOCITY_CONCEPTS].
#' @param extra_features Optional data.frame joined on `subject_id` + `cohort_start_date`.
#' @param cohort_id Optional `cohort_definition_id` filter.
#' @param engineer If `FALSE`, return only the raw inputs.
#' @return A data.frame.
#' @export
extract_velocity_features <- function(con, cohort_table = "cohort", lookback_days = 365, readmit_gap_days = 30,
                                      ip_concepts = c(9201L, 262L), ed_concepts = 9203L, os_concepts = c(9202L, 581385L),
                                      concepts = NULL, extra_features = NULL, cohort_id = NULL, engineer = TRUE) {
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)?$", cohort_table)) {
    stop("Invalid cohort_table '", cohort_table, "'.", call. = FALSE)
  }
  lb <- as.integer(lookback_days); gap <- as.integer(readmit_gap_days)
  if (lb < 1L || gap < 1L) stop("lookback_days and readmit_gap_days must be >= 1.", call. = FALSE)
  cc <- utils::modifyList(VELOCITY_CONCEPTS, as.list(concepts))
  ids <- function(x) paste(as.integer(x), collapse = ", ")
  cols <- tolower(DBI::dbGetQuery(con, paste0("DESCRIBE SELECT * FROM ", cohort_table))$column_name)
  if (!all(c("subject_id", "cohort_start_date") %in% cols)) stop("cohort_table needs subject_id and cohort_start_date.", call. = FALSE)
  where <- if (!is.null(cohort_id) && "cohort_definition_id" %in% cols) sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_id)) else ""
  end_expr <- if ("cohort_end_date" %in% cols) "cohort_end_date" else "cohort_start_date"
  pick <- function(key) {
    sprintf(paste0("arg_min(CASE WHEN m.measurement_concept_id IN (%1$s) THEN m.value_as_number END, ",
                   "CASE WHEN m.measurement_concept_id IN (%1$s) AND m.value_as_number IS NOT NULL THEN m.measurement_id END)"),
            ids(cc[[key]]))
  }
  ip <- ids(ip_concepts); ed <- ids(ed_concepts); os_ <- ids(os_concepts)
  sql <- sprintf("
    WITH c AS (
        SELECT subject_id, cohort_start_date, max(%1$s) AS cohort_end_date
        FROM %2$s %3$s GROUP BY subject_id, cohort_start_date
    ),
    v AS (
        SELECT c.subject_id, c.cohort_start_date, v.visit_concept_id AS vc, v.visit_start_date AS vs,
               COALESCE(v.visit_end_date, v.visit_start_date) AS ve
        FROM c JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE v.visit_start_date < c.cohort_start_date
          AND v.visit_start_date >= c.cohort_start_date - INTERVAL (%4$d) DAY
    ),
    agg AS (
        SELECT subject_id, cohort_start_date,
               count(*) AS prior_encounters_all_count,
               count(*) FILTER (WHERE vc IN (%5$s)) AS prior_ip_count,
               count(*) FILTER (WHERE vc IN (%6$s)) AS prior_ed_count,
               count(*) FILTER (WHERE vc IN (%5$s, %6$s, %7$s)) AS prior_ip_os_ed_count,
               COALESCE(sum(date_diff('day', vs, ve)) FILTER (WHERE vc IN (%5$s)), 0) AS prior_ip_los_days,
               max(ve) AS last_any_end,
               max(ve) FILTER (WHERE vc IN (%5$s, %6$s)) AS last_ip_ed_end
        FROM v GROUP BY 1, 2
    ),
    readm AS (
        SELECT i.subject_id, i.cohort_start_date, count(DISTINCT i.vs) AS n_readmit
        FROM v i
        JOIN visit_occurrence p ON p.person_id = i.subject_id AND p.visit_concept_id IN (%5$s)
         AND COALESCE(p.visit_end_date, p.visit_start_date) < i.vs
         AND i.vs <= COALESCE(p.visit_end_date, p.visit_start_date) + INTERVAL (%8$d) DAY
        WHERE i.vc IN (%5$s)
        GROUP BY 1, 2
    ),
    m AS (
        SELECT c.subject_id, c.cohort_start_date,
               %9$s AS heart_rate, %10$s AS systolic_bp, %11$s AS diastolic_bp, %12$s AS lab_bun, %13$s AS lab_creatinine,
               %14$s AS lab_sodium, %15$s AS lab_potassium, %16$s AS lab_chloride, %17$s AS lab_bicarbonate,
               %18$s AS lab_anion_gap, %19$s AS lab_egfr
        FROM c JOIN measurement m ON m.person_id = c.subject_id AND m.measurement_date = c.cohort_start_date
        GROUP BY 1, 2
    ),
    demo AS (
        SELECT person_id AS subject_id, gender_concept_id,
               COALESCE(year_of_birth, date_part('year', birth_datetime)) AS birth_year
        FROM person
    )
    SELECT c.subject_id, c.cohort_start_date,
           date_diff('day', agg.last_any_end, c.cohort_start_date)    AS days_since_prior_encounter,
           date_diff('day', agg.last_ip_ed_end, c.cohort_start_date)  AS days_since_prior_ip_ed,
           COALESCE(agg.prior_ip_os_ed_count, 0)                      AS prior_ip_os_ed_count,
           COALESCE(agg.prior_ip_count, 0)                            AS prior_ip_count,
           COALESCE(agg.prior_ed_count, 0)                            AS prior_ed_count,
           COALESCE(agg.prior_encounters_all_count, 0)                AS prior_encounters_all_count,
           COALESCE(readm.n_readmit, 0)                               AS readmission_count_twelve_months,
           COALESCE(agg.prior_ip_los_days, 0)                         AS prior_ip_los_days,
           date_diff('day', c.cohort_start_date, c.cohort_end_date)   AS index_los_days,
           demo.gender_concept_id,
           (date_part('year', c.cohort_start_date) - demo.birth_year) AS age,
           m.heart_rate, m.systolic_bp, m.diastolic_bp, m.lab_bun, m.lab_creatinine,
           m.lab_sodium, m.lab_potassium, m.lab_chloride, m.lab_bicarbonate,
           m.lab_anion_gap, m.lab_egfr
    FROM c
    LEFT JOIN agg USING (subject_id, cohort_start_date)
    LEFT JOIN readm USING (subject_id, cohort_start_date)
    LEFT JOIN m USING (subject_id, cohort_start_date)
    LEFT JOIN demo ON demo.subject_id = c.subject_id
    ORDER BY c.subject_id, c.cohort_start_date",
    end_expr, cohort_table, where, lb, ip, ed, os_, gap,
    pick("heart_rate"), pick("systolic_bp"), pick("diastolic_bp"), pick("lab_bun"), pick("lab_creatinine"),
    pick("lab_sodium"), pick("lab_potassium"), pick("lab_chloride"), pick("lab_bicarbonate"),
    pick("lab_anion_gap"), pick("lab_egfr"))
  raw <- .derive_labs(DBI::dbGetQuery(con, sql))
  if (!is.null(extra_features)) {
    extra <- extra_features[, setdiff(names(extra_features), setdiff(names(raw), c("subject_id", "cohort_start_date"))), drop = FALSE]
    raw <- merge(raw, extra, by = c("subject_id", "cohort_start_date"), all.x = TRUE, sort = TRUE)
  }
  if (!engineer) return(raw)
  absent <- setdiff(REQUIRED_VELOCITY_INPUTS, names(raw))
  if (length(absent)) {
    warning("extract_velocity_features: inputs not supplied (add them via extra_features); they are default-imputed as if normal: ",
            paste(absent, collapse = ", "), call. = FALSE)
  }
  engineer_clinical_features(raw, warn_missing = FALSE)
}
