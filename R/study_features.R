#' Study-design features: add your own person-level features to an OMOP study
#'
#' A *study design feature* is anything a protocol needs that the OMOP CDM does not define itself: an area-level index of the
#' residence ZIP (RUCA, SDI, census income), a risk score, a site-specific exposure definition. This framework keeps the package
#' small by shipping the mechanism and a few worked examples, not the data. It mirrors the Python module
#' `omop_etl.study_features`, and fitted state and manifests are interchangeable between the two languages.
#'
#' * [study_feature()] describes one feature; [ruca_feature()], [sdi_feature()], [area_feature()], [sql_feature()] and
#'   [custom_feature()] build common ones.
#' * [feature_set()] collects features, extended iteratively with [add_feature()].
#' * [compute_features()] returns one row per index stay (the address in effect at admission is resolved once and shared).
#' * [write_feature_set()] stores the values as `measurement` rows under site-defined concepts, and
#'   [feature_set_covariate_settings()] returns the matching OHDSI `FeatureExtraction` covariate settings, so
#'   FeatureExtraction, PatientLevelPrediction and DeepPatientLevelPrediction read them like any other covariate.
#' * [feature_set_manifest()] / [load_feature_state()] ship the parameters and *fitted state* (SDI cut points, a training
#'   percentile grid, mean and sd) with a model so a replicating site reproduces the features exactly.
#'
#' Transforms that learn from data refuse to run at a new site unless the training state is supplied; pass `fit = TRUE`
#' to [compute_features()] only when building the training reference. Nothing here reads events after the index date.
#'
#' @name study_features
NULL

.sf_manifest_format <- "omop-duck-db/study-feature-set"
.sf_key <- c("person_id", "stay_id", "admit_date")
.sf_transforms <- c("none", "log1p", "percentile", "zscore")

#' Describe one study-design feature
#'
#' @param name Feature name: letters, digits and underscores.
#' @param columns Character vector of the numeric columns the feature produces.
#' @param compute `function(ctx, state, fit)` returning a data frame with `stay_id` and exactly `columns`. `ctx` has `ctx$con`,
#'   `ctx$stays` and `ctx$zip5()` (one row per stay: `person_id, stay_id, zip5, rule`, cached). `state` is an environment for
#'   fitted state, filled only when `fit` is `TRUE`; the function must error when it needs state that is absent and `fit` is
#'   false.
#' @param description Free text recorded in the manifest.
#' @param requires Character vector of OMOP tables the feature reads.
#' @param labels Named character vector of concept names, `c(column = "label")`.
#' @param concept_ids Named numeric vector pinning columns to site-defined concept ids; other columns are registered
#'   automatically on [write_feature_set()].
#' @param params JSON-serialisable list of the settings that define the feature (recorded in the manifest).
#' @param state Optional named list of fitted state to start from.
#' @return An object of class `study_feature`.
#' @export
study_feature <- function(name, columns, compute, description = "", requires = character(), labels = character(),
                          concept_ids = numeric(), params = list(), state = list()) {
  if (!is.character(name) || length(name) != 1L || !grepl("^[A-Za-z0-9_]+$", name))
    stop("feature name ", shQuote(paste(name, collapse = " ")), " must be letters, digits and underscores.", call. = FALSE)
  if (!length(columns) || anyDuplicated(columns)) stop("feature ", shQuote(name), " needs at least one distinct column.", call. = FALSE)
  stray <- setdiff(names(concept_ids), columns)
  if (length(stray)) stop("feature ", shQuote(name), " pins concept ids for unknown columns ", paste(stray, collapse = ", "), ".", call. = FALSE)
  env <- new.env(parent = emptyenv())
  for (k in names(state)) assign(k, state[[k]], envir = env)
  structure(list(name = name, columns = as.character(columns), compute = compute, description = description,
                 requires = as.character(requires), labels = labels, concept_ids = concept_ids, params = params, state = env),
            class = "study_feature")
}

#' @export
print.study_feature <- function(x, ...) {
  cat("<study_feature ", x$name, "> columns: ", paste(x$columns, collapse = ", "), "\n", sep = "")
  invisible(x)
}

#' Collect study-design features
#'
#' A feature set is mutable (an environment): `add_feature()` extends it in place and returns it invisibly, so calls chain with `|>`.
#'
#' @param ... Zero or more `study_feature` objects to start with.
#' @return An object of class `feature_set`.
#' @export
feature_set <- function(...) {
  fs <- structure(new.env(parent = emptyenv()), class = "feature_set")
  fs$features <- list()
  for (f in list(...)) add_feature(fs, f)
  fs
}

#' @rdname feature_set
#' @param fs A `feature_set`.
#' @param feature A `study_feature`. Names and output columns must be unique within the set.
#' @export
add_feature <- function(fs, feature) {
  stopifnot(inherits(fs, "feature_set"))
  if (!inherits(feature, "study_feature"))
    stop("add_feature() takes a study_feature; build one with ruca_feature(), area_feature(), sql_feature(), custom_feature() ...", call. = FALSE)
  if (feature$name %in% names(fs$features)) stop("feature ", shQuote(feature$name), " is already in the set.", call. = FALSE)
  taken <- unlist(lapply(fs$features, `[[`, "columns"))
  clash <- intersect(feature$columns, c(taken, .sf_key))
  if (length(clash)) stop("feature ", shQuote(feature$name), " would duplicate column(s) ", paste(clash, collapse = ", "), ".", call. = FALSE)
  fs$features[[feature$name]] <- feature
  invisible(fs)
}

#' @rdname feature_set
#' @param name Feature name to remove.
#' @export
remove_feature <- function(fs, name) {
  if (!name %in% names(fs$features)) stop("no feature ", shQuote(name), "; have ", paste(names(fs$features), collapse = ", "), ".", call. = FALSE)
  fs$features[[name]] <- NULL
  invisible(fs)
}

#' @rdname feature_set
#' @return `feature_set_columns()`: all output columns, in order.
#' @export
feature_set_columns <- function(fs) as.character(unlist(lapply(fs$features, `[[`, "columns"), use.names = FALSE))

#' @export
print.feature_set <- function(x, ...) {
  cat("<feature_set> ", length(x$features), " feature(s): ", paste(names(x$features), collapse = ", "), "\n", sep = "")
  invisible(x)
}

#' @rdname feature_set
#' @param con A DBI connection to an OMOP DuckDB.
#' @return `check_feature_set()`: a named list of missing OMOP tables per feature (empty when all is in place).
#' @export
check_feature_set <- function(fs, con) {
  out <- list()
  for (f in fs$features) {
    gone <- Filter(function(t) !.geo_has_table(con, t), f$requires)
    if (length(gone)) out[[f$name]] <- unlist(gone)
  }
  out
}

#' Index stays of a cohort table
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param cohort_table Table with `subject_id` (or `person_id`) and `cohort_start_date` (the admission).
#' @param cohort_id Optional `cohort_definition_id` to keep.
#' @return A data frame `person_id, stay_id, admit_date` in cohort order; `stay_id` is `visit_occurrence_id` when present.
#' @export
stays_from_cohort <- function(con, cohort_table = "cohort", cohort_id = NULL) {
  if (!grepl("^[A-Za-z0-9_.]+$", cohort_table)) stop("invalid cohort table name ", shQuote(cohort_table), ".", call. = FALSE)
  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE %s", cohort_table))$column_name)
  pid <- if ("subject_id" %in% cols) "subject_id" else if ("person_id" %in% cols) "person_id" else NA_character_
  if (is.na(pid) || !"cohort_start_date" %in% cols) stop(cohort_table, " needs subject_id (or person_id) and cohort_start_date.", call. = FALSE)
  sid <- if ("visit_occurrence_id" %in% cols) "visit_occurrence_id" else "row_number() OVER ()"
  where <- if (!is.null(cohort_id) && "cohort_definition_id" %in% cols) sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_id)) else ""
  df <- DBI::dbGetQuery(con, sprintf("SELECT %s AS person_id, %s AS stay_id, CAST(cohort_start_date AS DATE) AS admit_date FROM %s %s",
                                     pid, sid, cohort_table, where))
  df$admit_date <- as.Date(df$admit_date)
  df
}

.sf_context <- function(con, stays, address_table) {
  ctx <- new.env(parent = emptyenv())
  ctx$con <- con
  ctx$stays <- stays
  cache <- NULL
  ctx$zip5 <- function() {
    if (is.null(cache)) {
      tbl <- address_table
      if (identical(tbl, "auto")) {
        tbl <- if (.geo_has_table(con, "person_address_history") &&
                   DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person_address_history")$n[[1]] > 0) "person_address_history" else NULL
      }
      if (!is.null(tbl) && !grepl("^[A-Za-z0-9_]+$", tbl)) stop("invalid address table name ", shQuote(tbl), ".", call. = FALSE)
      cache <<- .geo_index_addresses(con, stays[, .sf_key, drop = FALSE], tbl)
    }
    cache
  }
  ctx
}

.sf_clean_stays <- function(con, stays) {
  if (is.null(stays)) {
    stays <- DBI::dbGetQuery(con, "SELECT person_id, visit_occurrence_id AS stay_id, CAST(visit_start_date AS DATE) AS admit_date
                                   FROM visit_occurrence WHERE visit_concept_id IN (9201, 262)")
  } else {
    if (!"stay_id" %in% names(stays)) stays$stay_id <- seq_len(nrow(stays))
    miss <- setdiff(c("person_id", "admit_date"), names(stays))
    if (length(miss)) stop("stays is missing column(s) ", paste(miss, collapse = ", "), ".", call. = FALSE)
    if (anyDuplicated(stays$stay_id)) stop("stays.stay_id must be unique.", call. = FALSE)
  }
  stays <- stays[, .sf_key, drop = FALSE]
  stays$admit_date <- as.Date(stays$admit_date)
  rownames(stays) <- NULL
  stays
}

#' Compute the features of a set for a group of index stays
#'
#' @param fs A `feature_set`.
#' @param con A DBI connection to an OMOP DuckDB.
#' @param stays Data frame with `person_id`, `admit_date` and optionally a unique `stay_id`; default every inpatient visit.
#'   See [stays_from_cohort()].
#' @param fit `TRUE` (fit every feature that learns from data: the training reference), a character vector of feature names, or
#'   `FALSE` (use the state already on each feature or loaded with [load_feature_state()]).
#' @param address_table `"auto"`, a table name, or `NULL` (see [derive_geography_features()]).
#' @return A data frame with `person_id, stay_id, admit_date` and every feature column, one row per stay in input order. The
#'   fitted state is in `attr(, "feature_state")` and the manifest in `attr(, "feature_provenance")`.
#' @export
compute_features <- function(fs, con, stays = NULL, fit = FALSE, address_table = "auto") {
  missing_tables <- check_feature_set(fs, con)
  if (length(missing_tables)) stop("required OMOP table(s) missing: ", paste(names(missing_tables), collapse = ", "), ".", call. = FALSE)
  stays <- .sf_clean_stays(con, stays)
  fit_names <- if (isTRUE(fit)) names(fs$features) else if (isFALSE(fit) || is.null(fit)) character() else as.character(fit)
  unknown <- setdiff(fit_names, names(fs$features))
  if (length(unknown)) stop("fit names unknown features ", paste(unknown, collapse = ", "), ".", call. = FALSE)
  ctx <- .sf_context(con, stays, address_table)
  out <- stays
  for (f in fs$features) {
    got <- f$compute(ctx, f$state, f$name %in% fit_names)
    absent <- setdiff(f$columns, names(got))
    if (!"stay_id" %in% names(got) || length(absent))
      stop("feature ", shQuote(f$name), " returned ", paste(names(got), collapse = ", "), "; needs stay_id and ", paste(f$columns, collapse = ", "), ".", call. = FALSE)
    if (anyDuplicated(got$stay_id)) stop("feature ", shQuote(f$name), " returned more than one row for a stay.", call. = FALSE)
    idx <- match(out$stay_id, got$stay_id)
    for (cn in f$columns) out[[cn]] <- as.numeric(got[[cn]])[idx]
  }
  attr(out, "feature_state") <- feature_set_state(fs)
  attr(out, "feature_provenance") <- feature_set_manifest(fs)
  out
}

.sf_state_list <- function(f) {
  s <- as.list(f$state)
  if (!length(s)) return(list())
  s[order(names(s))]
}

#' Fitted state and manifest of a feature set
#'
#' @param fs A `feature_set`.
#' @return `feature_set_state()`: named list (by feature) of the fitted state of features that have any.
#' @export
feature_set_state <- function(fs) {
  out <- lapply(fs$features, .sf_state_list)
  out[lengths(out) > 0L]
}

#' @rdname feature_set_state
#' @param state A list from [feature_set_state()], a manifest list, or the path of a manifest JSON file (also one written by Python).
#' @export
load_feature_state <- function(fs, state) {
  if (is.character(state) && length(state) == 1L) state <- jsonlite::fromJSON(state, simplifyVector = FALSE)
  if (identical(state$format, .sf_manifest_format)) {
    state <- stats::setNames(lapply(state$features, function(f) f$state), vapply(state$features, function(f) f$name, character(1)))
  }
  for (nm in intersect(names(state), names(fs$features))) {
    env <- fs$features[[nm]]$state
    rm(list = ls(env, all.names = TRUE), envir = env)
    for (k in names(state[[nm]])) {
      v <- state[[nm]][[k]]
      assign(k, if (is.list(v)) unlist(v, use.names = FALSE) else v, envir = env)
    }
  }
  invisible(fs)
}

#' @rdname feature_set_state
#' @param concepts Optional result of [write_feature_set()] to record the concept ids actually used.
#' @return `feature_set_manifest()`: a list describing the set (columns, requirements, parameters, fitted state, concept ids).
#' @export
feature_set_manifest <- function(fs, concepts = NULL) {
  feats <- lapply(unname(fs$features), function(f) {
    ids <- lapply(stats::setNames(f$columns, f$columns), function(cn) {
      if (!is.null(concepts[[cn]])) as.numeric(concepts[[cn]][[1]]) else if (cn %in% names(f$concept_ids)) as.numeric(f$concept_ids[[cn]]) else NULL
    })
    list(name = f$name, description = f$description, columns = as.list(f$columns), requires = as.list(f$requires),
         params = f$params, state = .sf_state_list(f), concept_ids = Filter(Negate(is.null), ids))
  })
  list(format = .sf_manifest_format, format_version = 1L,
       omop_duck_db_version = as.character(utils::packageVersion("omopduckdb")), features = feats)
}

#' @rdname feature_set_state
#' @param path Where to write the manifest JSON.
#' @export
export_feature_manifest <- function(fs, path, concepts = NULL) {
  m <- feature_set_manifest(fs, concepts)
  writeLines(jsonlite::toJSON(m, auto_unbox = TRUE, digits = NA, pretty = TRUE, null = "null"), path)
  invisible(m)
}

#' Write computed features as `measurement` rows so OHDSI tools read them
#'
#' One row per stay and column, dated the admission date. Pinned ids (the geography ids of SDI and RUCA) are kept; other columns
#' are registered with [register_custom_concepts()]. Missing values write no row.
#'
#' @param fs A `feature_set`.
#' @param con A DBI connection to an OMOP DuckDB.
#' @param features The result of [compute_features()].
#' @param registry Optional shared registry pinning concept ids across sites (see [register_custom_concepts()]).
#' @param visit_ids Link rows to `visit_occurrence_id = stay_id` (right when the stays are `visit_occurrence` rows).
#' @param register_concepts Add the concepts to `concept` so covariate names resolve.
#' @param type_concept_id Measurement type concept; default 45754907 ("Derived value").
#' @return A named list `column -> c(concept_id, label)`.
#' @export
write_feature_set <- function(fs, con, features, registry = NULL, visit_ids = TRUE, register_concepts = TRUE, type_concept_id = 45754907) {
  cols <- feature_set_columns(fs)
  miss <- setdiff(c(.sf_key, cols), names(features))
  if (length(miss)) stop("features is missing column(s) ", paste(miss, collapse = ", "), "; pass the result of compute_features().", call. = FALSE)
  label_of <- function(f, cn) if (cn %in% names(f$labels)) unname(f$labels[[cn]]) else cn
  pinned <- list(); rest <- character()
  for (f in fs$features) for (cn in f$columns) {
    if (cn %in% names(f$concept_ids)) pinned[[cn]] <- c(as.numeric(f$concept_ids[[cn]]), label_of(f, cn)) else rest[[cn]] <- label_of(f, cn)
  }
  concepts <- c(pinned, if (length(rest)) register_custom_concepts(con, rest, registry = registry))
  long <- do.call(rbind, lapply(cols, function(cn) {
    data.frame(person_id = features$person_id, visit_occurrence_id = features$stay_id, date = as.Date(features$admit_date),
               feature = cn, value = as.numeric(features[[cn]]), stringsAsFactors = FALSE)
  }))
  write_custom_covariates(con, long, concepts, source = "omop_etl.study_features", visit_ids = visit_ids,
                          register_concepts = register_concepts, type_concept_id = type_concept_id)
  concepts
}

.sf_concept_ids <- function(fs, concepts) {
  ids <- stats::setNames(rep(NA_real_, length(feature_set_columns(fs))), feature_set_columns(fs))
  for (f in fs$features) for (cn in names(f$concept_ids)) ids[[cn]] <- as.numeric(f$concept_ids[[cn]])
  for (cn in intersect(names(concepts), names(ids))) ids[[cn]] <- as.numeric(concepts[[cn]][[1]])
  if (anyNA(ids)) stop("no concept id for ", paste(names(ids)[is.na(ids)], collapse = ", "), "; pass the result of write_feature_set().", call. = FALSE)
  ids
}

#' OHDSI FeatureExtraction settings for a feature set
#'
#' @param fs A `feature_set`.
#' @param concepts The result of [write_feature_set()] (needed for columns without a pinned concept id).
#' @param long_term_start_days,end_days Window relative to the index date (default the 365 days before and the day itself).
#' @param ... Passed to [custom_covariate_settings()] (and so to `FeatureExtraction::createCovariateSettings()`).
#' @return `feature_set_covariate_settings()`: a FeatureExtraction covariate-settings object (needs the FeatureExtraction package).
#'   `feature_set_extraction_r()`: the equivalent R code as a string.
#' @export
feature_set_covariate_settings <- function(fs, concepts = NULL, long_term_start_days = -365, end_days = 0, ...) {
  custom_covariate_settings(.sf_concept_ids(fs, concepts), long_term_start_days = long_term_start_days, end_days = end_days, ...)
}

#' @rdname feature_set_covariate_settings
#' @export
feature_set_extraction_r <- function(fs, concepts = NULL, long_term_start_days = -365, end_days = 0) {
  ids <- .sf_concept_ids(fs, concepts)
  sprintf("covariateSettings <- FeatureExtraction::createCovariateSettings(\n  useMeasurementValueLongTerm = TRUE,\n  longTermStartDays = %d, endDays = %d,\n  includedCovariateConceptIds = c(%s))   # %s",
          as.integer(long_term_start_days), as.integer(end_days), paste(sprintf("%.0f", ids), collapse = ", "), paste(names(ids), collapse = ", "))
}

# ---------------------------------------------------------------------------------------------------------------------
# Built-in features
# ---------------------------------------------------------------------------------------------------------------------
#' Built-in study-design features
#'
#' * `ruca_feature()`: USDA RUCA of the residence ZIP at admission: `ruca_code` (primary code) and `ruca_group` (1 metropolitan,
#'   2 micropolitan, 3 small town / rural). Same concept ids as [GEOGRAPHY_CONCEPT_IDS].
#' * `sdi_feature()`: Robert Graham Center SDI: `sdi_score` and `sdi_quintile` (1 least to 5 most deprived) with the training
#'   cohort's fixed cut points; errors without them unless computed with `fit = TRUE`.
#' * `area_feature()`: any ZIP-keyed numeric table (ACS median household income, % below poverty, ADI ...) as a patient feature,
#'   with `none` / `log1p` / `percentile` / `zscore` transforms fitted on the training sites only, an optional missing indicator
#'   (`<name>_missing`) and optional median imputation. Learned parameters live in the feature state.
#' * `sql_feature()`: one SQL query over the OMOP tables; it selects `stay_id` and the numeric columns from a view `_stays`
#'   (`person_id, stay_id, admit_date`). Use only data dated before the admission (unless the protocol says otherwise).
#' * `custom_feature()`: your own R function `fn(con, stays)` returning `stay_id` and the numeric columns.
#'
#' @param crosswalk,table Data frame or CSV path (RUCA crosswalk; SDI or area table).
#' @param code_99_missing Treat USDA RUCA 99 as unknown.
#' @param cut_points Four ascending SDI scores from the training cohort.
#' @param name Feature name (also the output column of `area_feature()`).
#' @param value_column,key_column Columns of the area table (defaults: the only value column; a ZIP-like column).
#' @param transform One of `none`, `log1p` (right-skewed dollars), `percentile` (position in the training distribution, 0-1),
#'   `zscore` (training mean and sd).
#' @param impute Fill unmapped ZIPs with the training median of the transformed value.
#' @param missing_indicator Add the `<name>_missing` column.
#' @param description,label Free text for the manifest and the concept name.
#' @param state Optional named list of fitted state (from a training site).
#' @param sql SQL for `sql_feature()`.
#' @param columns Output columns of `sql_feature()` / `custom_feature()`.
#' @param requires OMOP tables the feature needs.
#' @param labels Named character vector of concept names.
#' @param params JSON-serialisable settings recorded in the manifest.
#' @param fn `function(con, stays)` for `custom_feature()`.
#' @return A `study_feature`.
#' @name builtin_study_features
#' @export
ruca_feature <- function(crosswalk, name = "ruca", code_99_missing = FALSE) {
  xwalk <- read_ruca_crosswalk(crosswalk)
  groups <- c("RUCA: Metropolitan (1.0)" = 1, "RUCA: Micropolitan (2.0)" = 2, "RUCA: Small Town / Rural (3+)" = 3)
  study_feature(name, c("ruca_code", "ruca_group"), function(ctx, state, fit) {
    g <- merge(ctx$zip5(), xwalk, by = "zip5", all.x = TRUE, sort = FALSE)
    cat <- ruca_category(g$ruca_code, code_99_missing = code_99_missing)
    data.frame(stay_id = g$stay_id, ruca_code = floor(as.numeric(g$ruca_code)), ruca_group = unname(groups[cat]))
  }, description = "Rural-Urban Commuting Area of the residence ZIP at admission", requires = "person",
  labels = c(ruca_code = "RUCA primary code of the residence ZIP at admission",
             ruca_group = "RUCA group (1 metro, 2 micro, 3 small town/rural) at admission"),
  concept_ids = c(ruca_code = GEOGRAPHY_CONCEPT_IDS[["ruca_code"]]), params = list(code_99_missing = isTRUE(code_99_missing)))
}

#' @rdname builtin_study_features
#' @export
sdi_feature <- function(table, cut_points = NULL, name = "sdi") {
  tbl <- read_sdi_table(table)
  study_feature(name, c("sdi_score", "sdi_quintile"), function(ctx, state, fit) {
    g <- merge(ctx$zip5(), tbl, by = "zip5", all.x = TRUE, sort = FALSE)
    if (isTRUE(fit)) state$cut_points <- fit_sdi_cut_points(g$sdi_score)
    if (is.null(state$cut_points))
      stop("sdi_feature needs the training cohort's cut_points (or fit = TRUE when building the training reference); ",
           "local quantiles would change quintiles between sites.", call. = FALSE)
    q <- match(assign_sdi_quintile(g$sdi_score, state$cut_points), .sdi_labels)
    data.frame(stay_id = g$stay_id, sdi_score = g$sdi_score, sdi_quintile = as.numeric(q))
  }, description = "Social Deprivation Index of the residence ZIP at admission", requires = "person",
  labels = c(sdi_score = "SDI score of the residence ZIP at admission", sdi_quintile = "SDI quintile (1 least to 5 most deprived) at admission"),
  concept_ids = GEOGRAPHY_CONCEPT_IDS[c("sdi_score", "sdi_quintile")],
  state = if (!is.null(cut_points)) list(cut_points = validate_sdi_cut_points(cut_points)) else list())
}

#' @rdname builtin_study_features
#' @export
area_feature <- function(name, table, value_column = NULL, key_column = NULL, transform = "none", impute = FALSE,
                         missing_indicator = TRUE, description = "", label = NULL, state = list()) {
  transform <- match.arg(transform, .sf_transforms)
  df <- if (is.data.frame(table)) table else utils::read.csv(table, colClasses = "character", check.names = FALSE)
  lower <- tolower(names(df))
  key <- key_column %||% names(df)[match(TRUE, lower %in% c("zip5", "zcta5_fips", "zcta5", "zcta", "zipcode", "zip_code", "zip"))]
  if (is.null(key) || is.na(key) || !key %in% names(df)) stop("area table needs a ZIP column (zip5, zcta, zipcode ...); found ", paste(names(df), collapse = ", "), ".", call. = FALSE)
  val <- value_column %||% if (ncol(df) == 2L) setdiff(names(df), key) else NULL
  if (is.null(val) || !val %in% names(df)) stop("pass value_column: the table has more than one candidate column.", call. = FALSE)
  area <- data.frame(zip5 = .zip5(df[[key]]), raw = suppressWarnings(as.numeric(df[[val]])), stringsAsFactors = FALSE)
  area <- area[!is.na(area$zip5) & !duplicated(area$zip5), , drop = FALSE]
  cols <- if (isTRUE(missing_indicator)) c(name, paste0(name, "_missing")) else name
  apply_tf <- function(x, st) {
    switch(transform,
           log1p = log1p(ifelse(is.finite(x) & x > -1, x, NA_real_)),
           percentile = {
             grid <- as.numeric(st$percentile_grid)
             r <- suppressWarnings(stats::approx(grid, seq(0, 1, length.out = length(grid)), xout = x, rule = 2, ties = "ordered")$y)
             r[is.na(x)] <- NA_real_
             r
           },
           zscore = (x - st$mean) / (if (isTRUE(st$sd == 0)) 1 else st$sd),
           x)
  }
  labels <- c(sprintf("%s (%s) of the residence ZIP at admission", name, transform), sprintf("%s unavailable (1 = ZIP not in the area table)", name))
  names(labels) <- c(name, paste0(name, "_missing"))
  if (!is.null(label)) labels[[name]] <- label
  study_feature(name, cols, function(ctx, st, fit) {
    g <- merge(ctx$zip5(), area, by = "zip5", all.x = TRUE, sort = FALSE)
    raw <- as.numeric(g$raw)
    if (isTRUE(fit)) {
      ok <- raw[is.finite(raw)]
      if (length(ok) < 5L) stop(name, ": need at least 5 mapped stays to fit the transform; got ", length(ok), ".", call. = FALSE)
      if (transform == "percentile") st$percentile_grid <- unname(stats::quantile(ok, seq(0, 1, length.out = 101), type = 7))
      if (transform == "zscore") { st$mean <- mean(ok); st$sd <- sqrt(mean((ok - mean(ok))^2)) }
      if (isTRUE(impute)) st$fill_value <- stats::median(apply_tf(raw, st)[is.finite(apply_tf(raw, st))])
    }
    need <- c(percentile = "percentile_grid", zscore = "mean")[transform]
    if ((!is.na(need) && is.null(st[[need]])) || (isTRUE(impute) && is.null(st$fill_value)))
      stop(name, ": the ", shQuote(transform), " transform needs the training fit (state); pass state = ... / load_feature_state() or ",
           "fit = TRUE when building the training reference.", call. = FALSE)
    x <- apply_tf(raw, st)
    out <- data.frame(stay_id = g$stay_id)
    if (isTRUE(missing_indicator)) out[[paste0(name, "_missing")]] <- as.numeric(is.na(raw))
    if (isTRUE(impute)) x[is.na(x)] <- st$fill_value
    out[[name]] <- x
    out
  }, description = if (nzchar(description)) description else sprintf("Area-level %s of the residence ZIP at admission", val),
  requires = "person", labels = labels[cols],
  params = list(transform = transform, impute = isTRUE(impute), missing_indicator = isTRUE(missing_indicator), value_column = val),
  state = state)
}

#' @rdname builtin_study_features
#' @export
sql_feature <- function(name, sql, columns, requires = character(), description = "", labels = character(), params = list()) {
  study_feature(name, columns, function(ctx, state, fit) {
    duckdb::duckdb_register(ctx$con, "_stays", ctx$stays)
    on.exit(duckdb::duckdb_unregister(ctx$con, "_stays"), add = TRUE)
    DBI::dbGetQuery(ctx$con, sql)
  }, description = description, requires = requires, labels = labels, params = c(list(sql = sql), params))
}

#' @rdname builtin_study_features
#' @export
custom_feature <- function(name, columns, fn, requires = character(), description = "", labels = character(), params = list()) {
  study_feature(name, columns, function(ctx, state, fit) fn(ctx$con, ctx$stays), description = description, requires = requires,
                labels = labels, params = params)
}

# ---------------------------------------------------------------------------------------------------------------------
# Factory registry and named feature lists
# ---------------------------------------------------------------------------------------------------------------------
.sf_registry <- new.env(parent = emptyenv())
.sf_registry$factories <- list(ruca = ruca_feature, sdi = sdi_feature, area = area_feature, sql = sql_feature, custom = custom_feature)
.sf_registry$lists <- list(geography = c("ruca", "sdi"))

#' Registry of feature kinds and named feature lists
#'
#' @param kind Name of a feature kind (`ruca`, `sdi`, `area`, `sql`, `custom`, or one you register).
#' @param factory `function(...)` returning a `study_feature`.
#' @param overwrite Replace an existing kind.
#' @param ... Arguments for the factory.
#' @return `register_feature_factory()`: `NULL` invisibly. `make_feature()`: a `study_feature`. `available_features()`: kind names.
#' @export
register_feature_factory <- function(kind, factory, overwrite = FALSE) {
  if (kind %in% names(.sf_registry$factories) && !isTRUE(overwrite)) stop("feature kind ", shQuote(kind), " is already registered; pass overwrite = TRUE to replace it.", call. = FALSE)
  .sf_registry$factories[[kind]] <- factory
  invisible(NULL)
}

#' @rdname register_feature_factory
#' @export
available_features <- function() sort(names(.sf_registry$factories))

#' @rdname register_feature_factory
#' @export
make_feature <- function(kind, ...) {
  if (!kind %in% names(.sf_registry$factories)) stop("unknown feature kind ", shQuote(kind), "; available: ", paste(available_features(), collapse = ", "), ".", call. = FALSE)
  .sf_registry$factories[[kind]](...)
}

#' @rdname register_feature_factory
#' @param name Name of a feature list (`"geography"` = RUCA + SDI).
#' @param args Named list: for each kind in the list, a list of arguments for its factory.
#' @return `feature_list()`: a `feature_set`.
#' @export
feature_list <- function(name, args) {
  kinds <- .sf_registry$lists[[name]]
  if (is.null(kinds)) stop("unknown feature list ", shQuote(name), "; available: ", paste(names(.sf_registry$lists), collapse = ", "), ".", call. = FALSE)
  absent <- setdiff(kinds, names(args))
  if (length(absent)) stop("feature list ", shQuote(name), " needs arguments for ", paste(absent, collapse = ", "), ".", call. = FALSE)
  fs <- feature_set()
  for (k in kinds) add_feature(fs, do.call(make_feature, c(list(kind = k), args[[k]])))
  fs
}
