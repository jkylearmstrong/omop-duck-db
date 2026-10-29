#' Point-of-care scoring of one encounter
#'
#' Builds the same feature row as [score_cohort()] for a single `visit_occurrence_id`, but takes the LATEST vital or lab recorded
#' between admission and `as_of` instead of the admission-day value, for discharge-alert hooks. Utilization history, age and
#' `extra_features` are as in [extract_velocity_features()]; validation, imputation, blending and calibration are those of
#' [score_cohort()]. Mirror of Python `omop_etl.encounter.score_encounter`.
#'
#' @param con DuckDB DBI connection to the OMOP CDM.
#' @param visit_occurrence_id The encounter to score.
#' @param bundle A `model_bundle` or a bundle directory (load once and reuse it in a long-running service).
#' @param as_of Only measurements on or before this date count. Default: the earlier of the visit end date and today. CDM 5.4 end
#'   dates cannot be NULL, so for a live open stay with a placeholder end date pass `as_of = Sys.Date()`.
#' @param extra_features Optional data.frame keyed by `subject_id` + `cohort_start_date`.
#' @param on_violation `"raise"`, `"clip"` or `"nullify"`.
#' @param risk_tiers Optional `list(list(name =, min =), ...)` replacing the bundle's tiers (see [fit_capacity_thresholds()]).
#' @param concepts Overrides for [VELOCITY_CONCEPTS].
#' @param explain If `TRUE`, add `top_drivers`: the `top_k` patient-specific risk contributors (`feature`, `name`, `value`, signed `impact` on
#'   the probability), see [explain_rows()].
#' @param top_k Number of drivers.
#' @return A list: `visit_occurrence_id`, `person_id`, `prob`, `risk_tier`, `as_of`, `features` (the values scored),
#'   `measurement_dates` (date of the latest reading behind each lab/vital; absent = none found) and `validation` (contract summary).
#' @export
score_encounter <- function(con, visit_occurrence_id, bundle, as_of = NULL, extra_features = NULL, on_violation = "raise",
                            risk_tiers = NULL, concepts = NULL, explain = FALSE, top_k = 5L) {
  vid <- as.integer(visit_occurrence_id)
  v <- DBI::dbGetQuery(con, sprintf("SELECT person_id, visit_start_date, visit_end_date FROM visit_occurrence WHERE visit_occurrence_id = %d", vid))
  if (!nrow(v)) stop("visit_occurrence_id ", vid, " not found.", call. = FALSE)
  person_id <- v$person_id[[1]]
  start <- as.Date(v$visit_start_date[[1]]); end <- v$visit_end_date[[1]]
  cutoff <- if (!is.null(as_of)) as.Date(as_of) else if (!is.na(end)) min(as.Date(end), Sys.Date()) else Sys.Date()
  if (cutoff < start) stop("as_of ", cutoff, " is before the admission date ", start, ".", call. = FALSE)
  if (is.character(bundle)) bundle <- load_model_bundle(bundle)

  view <- "_score_encounter_cohort"
  duckdb::duckdb_register(con, view, data.frame(subject_id = person_id, cohort_start_date = start, cohort_end_date = cutoff))
  raw <- tryCatch(
    suppressWarnings(extract_velocity_features(con, view, concepts = concepts, extra_features = extra_features, engineer = FALSE)),
    finally = duckdb::duckdb_unregister(con, view))
  latest <- .latest_measurements(con, person_id, start, cutoff, utils::modifyList(VELOCITY_CONCEPTS, as.list(concepts)))

  # The latest reading replaces the admission-day value; derived labs are recomputed from what is now there.
  for (d in list(list("lab_egfr", "lab_creatinine"), list("lab_anion_gap", c("lab_sodium", "lab_chloride", "lab_bicarbonate")))) {
    if (d[[1]] %in% names(latest) || any(d[[2]] %in% names(latest))) raw[[d[[1]]]] <- NA_real_
  }
  for (f in names(latest)) raw[[f]] <- latest[[f]]$value
  raw <- .derive_labs(raw)
  feats <- suppressWarnings(engineer_clinical_features(raw, warn_missing = FALSE))

  frame <- cbind(feats, visit_occurrence_id = vid)
  res <- score_cohort(bundle, frame, key_columns = "visit_occurrence_id", on_violation = on_violation, return_validation = TRUE)
  prob <- res$scores$prob[[1]]
  tier <- if (!is.null(risk_tiers)) map_risk_tiers(prob, risk_tiers) else res$scores$risk_tier[[1]]
  nm <- vapply(bundle$feature_spec$features, function(f) f$name, character(1))
  nm <- nm[nm %in% names(feats)]
  values <- stats::setNames(lapply(nm, function(n) if (is.na(feats[[n]][[1]])) NULL else as.numeric(feats[[n]][[1]])), nm)
  out <- list(visit_occurrence_id = vid, person_id = as.integer(person_id), prob = prob, risk_tier = tier, as_of = format(cutoff),
              features = values, measurement_dates = lapply(latest, function(l) format(l$date)),
              validation = paste(format(res$validation), collapse = "\n"))
  if (isTRUE(explain)) {
    filled <- res$validation$frame
    for (f in bundle$feature_spec$features) if (anyNA(filled[[f$name]]) && !is.null(f$impute)) filled[[f$name]][is.na(filled[[f$name]])] <- f$impute
    out$top_drivers <- explain_rows(bundle, filled, top_k = top_k)[[1]]
  }
  out
}

.latest_measurements <- function(con, person_id, start, cutoff, concepts) {
  cols <- tolower(DBI::dbGetQuery(con, "DESCRIBE measurement")$column_name)
  when <- if ("measurement_datetime" %in% cols) "COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP))" else "CAST(measurement_date AS TIMESTAMP)"
  feats <- c("heart_rate", "systolic_bp", "diastolic_bp", "lab_bun", "lab_creatinine", "lab_sodium", "lab_potassium", "lab_chloride",
             "lab_bicarbonate", "lab_anion_gap", "lab_egfr")
  out <- list()
  for (f in feats) {
    ids <- as.integer(concepts[[f]])
    if (!length(ids)) next
    r <- DBI::dbGetQuery(con, sprintf(
      "SELECT value_as_number, measurement_date FROM measurement WHERE person_id = %d AND measurement_concept_id IN (%s) AND value_as_number IS NOT NULL AND measurement_date BETWEEN DATE '%s' AND DATE '%s' ORDER BY %s DESC, measurement_id DESC LIMIT 1",
      as.integer(person_id), paste(ids, collapse = ", "), format(start), format(cutoff), when))
    if (nrow(r)) out[[f]] <- list(value = as.numeric(r$value_as_number[[1]]), date = as.Date(r$measurement_date[[1]]))
  }
  out
}
