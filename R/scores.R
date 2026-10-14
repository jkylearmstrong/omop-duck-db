#' Clinical Benchmark Risk Score Calculators (LACE Index and HOSPITAL Score)
#'
#' Standardized bedside heuristics for observational readmission and clinical outcome studies:
#' - LACE Index (Length of stay, Acuity of admission, Charlson Comorbidity Index, ED visits in prior 6 months)
#' - HOSPITAL Score (Hemoglobin at discharge, Oncology discharge, Sodium level, Procedure during stay,
#'   Index admission type, Prior hospital admissions in 12 months, Length of stay >= 5 days)
#'
#' @name scores
NULL

#' Calculate the LACE Index score for an acute hospital cohort
#'
#' Evaluates the standard bedside LACE index (0-19):
#' - L: Length of stay (0 to 7 pts: <1d=0, 1d=1, 2d=2, 3d=3, 4-6d=4, 7-13d=5, >=14d=7)
#' - A: Acuity of admission (3 pts if emergency admission, 0 otherwise)
#' - C: Charlson Comorbidity Index (0 to 5 pts: 0=0, 1=1, 2=2, 3=3, >=4=5)
#' - E: Emergency department visits in prior 6 months (0 to 4 pts: 0=0, 1=1, 2=2, 3=3, >=4=4)
#'
#' Risk stratification:
#' - Low: 0-4
#' - Moderate: 5-9
#' - High: >= 10
#'
#' @param con Active DuckDB connection with CDM tables.
#' @param cohort_table Cohort table or view name containing subject_id/person_id and cohort_start_date.
#' @return A `data.frame` with cohort keys, predictor components, `lace_score`, and `lace_risk`.
#' @export
calculate_lace_index <- function(con, cohort_table = "cohort") {
  df <- calculate_bedside_scores(con, cohort_table = cohort_table, scores = "lace")
  drop_cols <- c("hospital_score", "hospital_risk", "chads_vasc_score", "sofa_score")
  df[, setdiff(names(df), drop_cols), drop = FALSE]
}

#' Calculate the HOSPITAL Score for potentially avoidable 30-day readmissions
#'
#' Evaluates the bedside HOSPITAL score (0-10):
#' - H: Hemoglobin at discharge < 12.0 g/dL (1 pt)
#' - O: Oncology discharge / active neoplasm diagnosis in prior 365 days (2 pts)
#' - S: Sodium level at discharge < 135 mEq/L (1 pt)
#' - P: Procedure during index stay (1 pt)
#' - I: Index admission type urgent / emergency (1 pt)
#' - T: Number of prior hospital admissions in past 12 months (0=0, 1=1, >=2=2 pts)
#' - L: Length of stay >= 5 days (2 pts)
#'
#' Risk stratification:
#' - Low: 0-4
#' - Intermediate: 5-6
#' - High: >= 7
#'
#' @param con Active DuckDB connection with CDM tables.
#' @param cohort_table Cohort table or view name.
#' @return A `data.frame` with cohort keys, predictor components, `hospital_score`, and `hospital_risk`.
#' @export
calculate_hospital_score <- function(con, cohort_table = "cohort") {
  df <- calculate_bedside_scores(con, cohort_table = cohort_table, scores = "hospital")
  drop_cols <- c("lace_score", "lace_risk", "chads_vasc_score", "sofa_score")
  df[, setdiff(names(df), drop_cols), drop = FALSE]
}
