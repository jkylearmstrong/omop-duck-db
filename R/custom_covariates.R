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
  if (anyDuplicated(cid)) stop("two features share one concept id: ", paste(names(cid)[duplicated(cid) | duplicated(cid, fromLast = TRUE)], collapse = ", "), call. = FALSE)
  if (.has_concept_table(con)) {
    for (key in names(cid)) {
      row <- DBI::dbGetQuery(con, sprintf("SELECT concept_code, vocabulary_id FROM concept WHERE concept_id = %.0f", cid[[key]]))
      if (nrow(row) && !(identical(row$concept_code[[1]], key) && identical(row$vocabulary_id[[1]], .CUSTOM_VOCABULARY)))
        stop(sprintf("concept id %.0f for feature %s is already used by %s (%s); use register_custom_concepts() to pick ids that do not collide",
                     cid[[key]], key, row$concept_code[[1]], row$vocabulary_id[[1]]), call. = FALSE)
    }
  }
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

.CUSTOM_VOCABULARY <- "omop-duck-db"
.LOCAL_CONCEPT_MIN <- 2e9

.has_concept_table <- function(con) {
  DBI::dbGetQuery(con, "SELECT count(*) AS n FROM information_schema.tables WHERE lower(table_name) = 'concept' AND table_catalog = current_database() AND table_schema = current_schema()")$n[[1]] > 0
}

# name -> id from a named vector/list, a list of list(concept_id = ...), or a JSON file from export_custom_concept_registry()
.load_registry <- function(registry) {
  if (is.null(registry)) return(stats::setNames(numeric(0), character(0)))
  if (is.character(registry) && length(registry) == 1 && is.null(names(registry))) registry <- jsonlite::fromJSON(registry, simplifyVector = FALSE)
  ids <- vapply(registry, function(v) as.numeric(if (is.list(v)) v[["concept_id"]] else v[[1]]), numeric(1))
  stats::setNames(ids, names(registry))
}

#' Concept ids for your own feature names
#'
#' Returns the `concepts` list for [write_custom_covariates()], reusing the id already registered for a feature name in this
#' database (`concept_code` = the name, vocabulary `omop-duck-db`) and otherwise taking the next free id from 2,001,000,000.
#' Ids are per database unless sites agree on them: pass `registry` to pin the ids a consortium shares.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param names Character vector of feature names, or a named character vector `c(name = "label")`.
#' @param registry Optional shared registry: a named vector or list `name = id`, or the path of the JSON file written by
#'   [export_custom_concept_registry()]. A name in the registry takes its id; a name outside it gets an id above every registry id.
#'   A registry id this database already gives to another feature, a vocabulary concept, or that is below 2,000,000,000 is an error.
#' @return A named list; each element is `c(concept_id, label)`.
#' @export
register_custom_concepts <- function(con, names, registry = NULL) {
  labels <- if (is.null(base::names(names))) stats::setNames(as.character(names), names) else names
  pinned <- .load_registry(registry)
  out <- list()
  used <- numeric(0)
  taken <- data.frame(concept_id = numeric(0), concept_code = character(0), vocabulary_id = character(0))
  if (.has_concept_table(con)) {
    have <- DBI::dbGetQuery(con, "SELECT concept_id, concept_code FROM concept WHERE vocabulary_id = 'omop-duck-db'")
    for (i in seq_len(nrow(have))) if (have$concept_code[[i]] %in% base::names(labels)) out[[have$concept_code[[i]]]] <- c(have$concept_id[[i]], labels[[have$concept_code[[i]]]])
    used <- DBI::dbGetQuery(con, "SELECT concept_id FROM concept WHERE concept_id >= 2000100000")$concept_id
    taken <- DBI::dbGetQuery(con, "SELECT concept_id, concept_code, vocabulary_id FROM concept WHERE concept_id >= 2000000000")
  }
  for (nm in base::names(pinned)) {
    if (!nm %in% base::names(labels)) next
    cid <- pinned[[nm]]
    if (cid < .LOCAL_CONCEPT_MIN) stop(sprintf("registry id %.0f for %s is under 2000000000 (site-defined range)", cid, nm), call. = FALSE)
    if (!is.null(out[[nm]]) && as.numeric(out[[nm]][[1]]) != cid)
      stop(sprintf("%s is concept %.0f in this database but %.0f in the registry", nm, as.numeric(out[[nm]][[1]]), cid), call. = FALSE)
    hit <- taken[taken$concept_id == cid, , drop = FALSE]
    if (nrow(hit) && !(hit$concept_code[[1]] == nm && hit$vocabulary_id[[1]] == .CUSTOM_VOCABULARY))
      stop(sprintf("registry id %.0f for %s is already used here by %s (%s)", cid, nm, hit$concept_code[[1]], hit$vocabulary_id[[1]]), call. = FALSE)
    out[[nm]] <- c(cid, labels[[nm]])
  }
  nxt <- max(c(2000999999, used, unname(pinned), vapply(out, function(x) as.numeric(x[[1]]), numeric(1)))) + 1
  for (nm in base::names(labels)) {
    if (is.null(out[[nm]])) { out[[nm]] <- c(nxt, labels[[nm]]); nxt <- nxt + 1 }
  }
  out
}

#' The custom concepts registered in a database
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @return A named list: feature name -> `list(concept_id, concept_name)`; empty when there is no `concept` table.
#' @export
custom_concept_registry <- function(con) {
  if (!.has_concept_table(con)) return(list())
  d <- DBI::dbGetQuery(con, "SELECT concept_id, concept_code, concept_name FROM concept WHERE vocabulary_id = 'omop-duck-db' ORDER BY concept_id")
  stats::setNames(lapply(seq_len(nrow(d)), function(i) list(concept_id = d$concept_id[[i]], concept_name = d$concept_name[[i]])), d$concept_code)
}

#' Write the custom concept registry so sites share concept ids
#'
#' A consortium shares one registry file so every site gives a feature the same concept id (`register_custom_concepts(registry = )`),
#' and [check_custom_concepts()] finds sites whose ids drifted apart.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param path Optional JSON file to write.
#' @return The registry, invisibly.
#' @export
export_custom_concept_registry <- function(con, path = NULL) {
  reg <- custom_concept_registry(con)
  if (!is.null(path)) jsonlite::write_json(reg, path, auto_unbox = TRUE, pretty = TRUE, digits = NA)
  invisible(reg)
}

#' Check the custom concept ids of a database, alone and against other sites
#'
#' Finds `below_local_range` (an id under 2,000,000,000 in the custom vocabulary), `id_collision` (a custom id also used by a concept
#' of another vocabulary), `duplicate_id`, and, for each registry passed, `name_different_id` (the site and the registry give a feature
#' different ids) and `id_different_name` (one id means different features). A model trained on feature names is portable across
#' sites; one that stores the id is not, so these conflicts break an external validation silently.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param ... Other sites' registries: named vectors/lists `name = id`, or JSON files from [export_custom_concept_registry()].
#' @param error Raise an error when there are conflicts.
#' @return A data frame with columns `kind`, `feature`, `detail` (zero rows when clean).
#' @export
check_custom_concepts <- function(con, ..., error = FALSE) {
  rows <- list()
  add <- function(kind, feature, detail) rows[[length(rows) + 1]] <<- data.frame(kind = kind, feature = feature, detail = detail, stringsAsFactors = FALSE)
  local <- custom_concept_registry(con)
  ids <- vapply(local, function(v) as.numeric(v$concept_id), numeric(1))
  for (nm in names(ids)) {
    if (ids[[nm]] < .LOCAL_CONCEPT_MIN) add("below_local_range", nm, sprintf("id %.0f is under 2000000000", ids[[nm]]))
    first <- names(ids)[match(ids[[nm]], ids)]
    if (first != nm) add("duplicate_id", nm, sprintf("id %.0f is also used by %s", ids[[nm]], first))
  }
  if (length(ids)) {
    hit <- DBI::dbGetQuery(con, sprintf("SELECT concept_id, vocabulary_id FROM concept WHERE vocabulary_id <> 'omop-duck-db' AND concept_id IN (%s)",
                                        paste(sprintf("%.0f", ids), collapse = ", ")))
    for (i in seq_len(nrow(hit))) add("id_collision", names(ids)[match(hit$concept_id[[i]], ids)], sprintf("id %.0f is also a %s concept", hit$concept_id[[i]], hit$vocabulary_id[[i]]))
  }
  regs <- list(...)
  for (k in seq_along(regs)) {
    other <- .load_registry(regs[[k]])
    for (nm in names(ids)) {
      if (nm %in% names(other) && other[[nm]] != ids[[nm]]) {
        add("name_different_id", nm, sprintf("registry %d has %s = %.0f, this database %.0f", k, nm, other[[nm]], ids[[nm]]))
      } else if (!nm %in% names(other) && ids[[nm]] %in% other) {
        add("id_different_name", nm, sprintf("id %.0f is %s in registry %d, %s here", ids[[nm]], names(other)[match(ids[[nm]], other)], k, nm))
      }
    }
  }
  out <- if (length(rows)) do.call(rbind, rows) else data.frame(kind = character(), feature = character(), detail = character())
  if (error && nrow(out)) stop("custom concept conflicts: ", paste(sprintf("%s %s (%s)", out$kind, out$feature, out$detail), collapse = "; "), call. = FALSE)
  out
}

#' Write your own features so the OHDSI tools read them
#'
#' Writes a wide data frame (`person_id`, a date column and one numeric column per feature) as covariates that
#' FeatureExtraction, PatientLevelPrediction and DeepPatientLevelPrediction read like any other. This is how a study adds its
#' own version of a feature OMOP already defines (a Charlson index, an Elixhauser score, a frailty index) next to the standard
#' one: give it its own name (`charlson_site`) and extract both with the same covariate settings.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param features Wide data frame with `person_id`, the date column, optionally `visit_occurrence_id`, and one column per feature.
#' @param labels Optional named character vector of concept names, `c(feature = "label")`.
#' @param date_col Name of the date column (default `"date"`).
#' @param registry Optional shared registry that pins the concept ids (see [register_custom_concepts()]).
#' @param ... Passed to [write_custom_covariates()].
#' @return The `concepts` list used (see [register_custom_concepts()]); pass its ids to [custom_covariate_settings()].
#' @export
write_custom_features <- function(con, features, labels = NULL, date_col = "date", registry = NULL, ...) {
  id_cols <- intersect(c("person_id", date_col, "visit_occurrence_id"), names(features))
  feats <- setdiff(names(features), c("person_id", date_col, "visit_occurrence_id"))
  lab <- stats::setNames(feats, feats)
  lab[names(labels)] <- labels
  concepts <- register_custom_concepts(con, lab, registry = registry)
  long <- do.call(rbind, lapply(feats, function(f) {
    d <- features[, id_cols, drop = FALSE]
    names(d)[names(d) == date_col] <- "date"
    d$feature <- f
    d$value <- as.numeric(features[[f]])
    d
  }))
  write_custom_covariates(con, long, concepts, ...)
  concepts
}
