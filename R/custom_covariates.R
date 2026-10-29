#' Add your own covariates so the OHDSI tools read them
#'
#' FeatureExtraction, PatientLevelPrediction and ATLAS know only OMOP tables. A feature that is not in the CDM (an area
#' deprivation index, a risk score, an alternative definition of a standard feature) becomes visible to them when it is written
#' as `measurement` rows with a site-defined concept id (at least 2,000,000,000, the OMOP convention). Studies then extract it
#' with the standard tool, and an alternative definition of an existing feature sits beside the standard one under its own
#' concept id, so the two can be compared (see [compare_feature_tables()]) before one is chosen.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param values A long data frame: `person_id`, `date` (the date the value applies to), `feature` (a name in `concepts`),
#'   `value` (numeric), and optionally `visit_occurrence_id`. Missing values write no row.
#' @param concepts Named list; each element is `c(concept_id, concept_name)` (concept id as number, at least 2e9).
#' @param source Written to `measurement_source_value`.
#' @param visit_ids Link rows to `visit_occurrence_id` when that column is present in `values`.
#' @param register_concepts Add the concepts to `concept` (Measurement domain) when that table exists.
#' @param type_concept_id Measurement type concept; default 45754907 ("Derived value").
#' @details Re-running replaces the rows of the same concepts for the same persons. `unit_concept_id` is 0 because
#'   FeatureExtraction joins measurement covariates on the unit and skips rows where it is NULL.
#' @return The number of rows written (invisibly).
#' @export
write_custom_covariates <- function(con, values, concepts, source = "omop_etl.custom_covariates", visit_ids = FALSE,
                                    register_concepts = TRUE, type_concept_id = 45754907) {
  miss <- setdiff(c("person_id", "date", "feature", "value"), names(values))
  if (length(miss)) stop("values is missing column(s) ", paste(miss, collapse = ", "), call. = FALSE)
  cid <- vapply(concepts, function(x) as.numeric(x[[1]]), numeric(1))
  cname <- vapply(concepts, function(x) as.character(x[[2]]), character(1))
  if (any(cid < 2e9)) stop("custom concept ids must be >= 2,000,000,000 (site-defined range): ",
                           paste(names(cid)[cid < 2e9], collapse = ", "), call. = FALSE)
  unknown <- setdiff(unique(stats::na.omit(values$feature)), names(concepts))
  if (length(unknown)) stop("feature(s) ", paste(unknown, collapse = ", "), " have no entry in concepts", call. = FALSE)
  keep <- !is.na(values$value)
  rows <- data.frame(person_id = values$person_id[keep], date = as.Date(values$date[keep]),
                     value = as.numeric(values$value[keep]), measurement_concept_id = unname(cid[values$feature[keep]]))
  rows$visit_id <- if (isTRUE(visit_ids) && "visit_occurrence_id" %in% names(values)) values$visit_occurrence_id[keep] else NA_real_
  if (!nrow(rows)) return(invisible(0L))
  duckdb::duckdb_register(con, "_custom_cov", rows)
  on.exit(duckdb::duckdb_unregister(con, "_custom_cov"), add = TRUE)
  DBI::dbExecute(con, sprintf("DELETE FROM measurement WHERE measurement_concept_id IN (%s) AND person_id IN (SELECT person_id FROM _custom_cov)",
                              paste(sprintf("%.0f", cid), collapse = ", ")))
  start <- DBI::dbGetQuery(con, "SELECT COALESCE(MAX(measurement_id), 0) AS m FROM measurement")$m[[1]]
  DBI::dbExecute(con, sprintf("
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime,
                             measurement_type_concept_id, value_as_number, unit_concept_id, visit_occurrence_id, measurement_source_value)
    SELECT %.0f + row_number() OVER (ORDER BY person_id, date, measurement_concept_id),
           CAST(person_id AS BIGINT), CAST(measurement_concept_id AS INTEGER), date, CAST(date AS TIMESTAMP),
           %.0f, CAST(value AS DOUBLE), 0, CAST(visit_id AS BIGINT), ?
    FROM _custom_cov", start, type_concept_id), params = list(source))
  if (isTRUE(register_concepts) &&
      DBI::dbGetQuery(con, "SELECT count(*) AS n FROM information_schema.tables WHERE lower(table_name) = 'concept' AND table_catalog = current_database() AND table_schema = current_schema()")$n[[1]] > 0) {
    for (key in names(cid)) {
      if (DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM concept WHERE concept_id = %.0f", cid[[key]]))$n[[1]] == 0) {
        DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES (?, ?, 'Measurement', 'omop-duck-db', 'Custom covariate', 'S', ?, DATE '1970-01-01', DATE '2099-12-31')",
                       params = list(cid[[key]], unname(cname[[key]]), key))
      }
    }
  }
  invisible(nrow(rows))
}

#' FeatureExtraction settings that read custom covariates
#'
#' Builds the `FeatureExtraction::createCovariateSettings()` object that extracts the covariates written by
#' [write_custom_covariates()] (or [write_geography_measurements()]), so a study asks the standard tool for them.
#'
#' @param concept_ids Numeric vector of the custom concept ids to extract (for example `GEOGRAPHY_CONCEPT_IDS`).
#' @param long_term_start_days,end_days Window relative to the cohort start, default the 365 days before.
#' @param ... Further arguments passed to `FeatureExtraction::createCovariateSettings()`, for example
#'   `useDemographicsGender = TRUE` to extract standard covariates in the same pass.
#' @return A FeatureExtraction covariate settings object.
#' @export
custom_covariate_settings <- function(concept_ids, long_term_start_days = -365, end_days = 0, ...) {
  if (!requireNamespace("FeatureExtraction", quietly = TRUE)) stop("FeatureExtraction is not installed", call. = FALSE)
  FeatureExtraction::createCovariateSettings(useMeasurementValueLongTerm = TRUE, longTermStartDays = long_term_start_days,
                                             endDays = end_days, includedCovariateConceptIds = as.numeric(concept_ids), ...)
}
