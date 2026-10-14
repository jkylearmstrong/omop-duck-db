#' Modular clinical domain profiles and `table1_generator()`
#'
#' A *profile* bundles labs / vitals (LOINC), medication classes (ATC codes resolved through `concept` +
#' `concept_ancestor`) and comorbidities (ICD-10 code prefixes on `condition_source_value`) for a clinical
#' domain. The built-in profiles live in `inst/extdata/domain_profiles.yml` (shared with the Python
#' `omop_etl.DOMAIN_PROFILES`). They are a starting point for review by a clinician of that domain, not a
#' validated specification.
#' @name domain_profiles
NULL

#' @keywords internal
#' @noRd
.load_domain_profiles <- function() {
  path <- system.file("extdata", "domain_profiles.yml", package = "omopduckdb")
  if (!nzchar(path)) path <- file.path("inst", "extdata", "domain_profiles.yml")
  prof <- yaml::read_yaml(path)
  lapply(prof, function(p) {
    p$labs <- lapply(p$labs, function(v) as.character(unlist(v)))
    p$bounds <- lapply(p$bounds, function(v) as.numeric(unlist(v)))
    p$conditions <- lapply(p$conditions, function(v) as.character(unlist(v)))
    p$medications <- lapply(p$medications, function(s) lapply(s, unlist))
    p
  })
}

#' Built-in domain profile names
#' @return Character vector of profile names.
#' @export
domain_profile_names <- function() names(.load_domain_profiles())

#' Load a domain profile
#'
#' @param source Built-in profile name, a profile list (`labs`, `bounds`, `medications`, `conditions`) or a
#'   YAML file path.
#' @return A profile list.
#' @export
load_domain_profile <- function(source) {
  if (is.list(source)) {
    prof <- source
  } else if (is.character(source) && length(source) == 1 && source %in% domain_profile_names()) {
    prof <- .load_domain_profiles()[[source]]
  } else if (is.character(source) && file.exists(source)) {
    prof <- yaml::read_yaml(source)
  } else {
    stop(sprintf("Unknown profile '%s'; built-ins: %s.", format(source), paste(domain_profile_names(), collapse = ", ")),
         call. = FALSE)
  }
  unknown <- setdiff(names(prof), c("labs", "bounds", "medications", "conditions"))
  if (length(unknown)) stop(sprintf("Unknown profile keys: %s.", paste(unknown, collapse = ", ")), call. = FALSE)
  prof
}

#' Table 1 generator extended by clinical domain profiles
#'
#' Builds a cohort-level Table 1 from a DuckDB OMOP database. Labs use the last value in the stay by default;
#' bounds (user `bounds`, then profile, then [DEFAULT_PHYSIOLOGIC_BOUNDS]) are applied BEFORE aggregation and
#' `bounds = FALSE` disables bounding. Units are harmonized first ([harmonize_measurement_units()]). The cohort
#' table needs `subject_id`, `cohort_start_date`, `cohort_end_date`. Mirror of Python `Table1Generator`.
#'
#' ```
#' t1 <- table1_generator(con = con)
#' t1$add_domain_profile("heart_failure")
#' t1$add_custom_lab("troponin_t", c("6598-7", "42757-5"), bounds = c(0, 50))
#' out <- t1$generate("inpatient_hf_cohort", stratify_by = "readmitted_30d")
#' ```
#'
#' @param db_path Path to a DuckDB file (opened read-only), or give `con`.
#' @param con An existing DuckDB DBI connection.
#' @param read_only Open `db_path` read-only (default).
#' @param bounds `NULL` (defaults), a named list of overrides, or `FALSE`.
#' @param harmonize_units Run the unit preflight first (default `TRUE`).
#' @return A list of functions: `add_domain_profile`, `add_custom_lab`, `add_custom_medication`,
#'   `add_custom_condition`, `extract`, `generate`, `state` (the current configuration), `close`. Mutators
#'   return the generator invisibly so calls can be chained.
#' @export
table1_generator <- function(db_path = NULL, con = NULL, read_only = TRUE, bounds = NULL, harmonize_units = TRUE) {
  if (is.null(con) && is.null(db_path)) stop("Provide db_path or con.", call. = FALSE)
  owns <- is.null(con)
  if (owns) con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = read_only)
  st <- new.env(parent = emptyenv())
  st$labs <- list(); st$lab_bounds <- list(); st$medications <- list(); st$conditions <- list()
  st$profiles <- character(0); st$features <- NULL; st$unit_report <- NULL
  self <- new.env(parent = emptyenv())

  add_domain_profile <- function(profile) {
    prof <- load_domain_profile(profile)
    for (k in names(prof$labs)) st$labs[[k]] <- prof$labs[[k]]
    for (k in names(prof$bounds)) st$lab_bounds[[k]] <- prof$bounds[[k]]
    for (k in names(prof$medications)) st$medications[[k]] <- prof$medications[[k]]
    for (k in names(prof$conditions)) st$conditions[[k]] <- prof$conditions[[k]]
    st$profiles <- c(st$profiles, if (is.character(profile)) profile else "custom")
    invisible(self)
  }
  add_custom_lab <- function(name, loinc_codes, bounds = NULL) {
    st$labs[[name]] <- as.character(loinc_codes)
    if (!is.null(bounds)) st$lab_bounds[[name]] <- as.numeric(bounds)
    invisible(self)
  }
  add_custom_medication <- function(name, spec) {
    st$medications[[name]] <- spec
    invisible(self)
  }
  add_custom_condition <- function(name, icd10_prefixes) {
    st$conditions[[name]] <- as.character(icd10_prefixes)
    invisible(self)
  }
  effective_bounds <- function() {
    if (identical(bounds, FALSE)) return(list())
    eff <- resolve_bounds(NULL)
    for (k in names(st$lab_bounds)) eff[[k]] <- st$lab_bounds[[k]]
    if (is.list(bounds)) for (k in names(bounds)) eff[[k]] <- as.numeric(bounds[[k]])
    eff[names(eff) %in% names(st$labs)]
  }
  extract <- function(cohort_table, lab_strategy = "last_before_discharge", med_window = "pre_admission") {
    base <- DBI::dbGetQuery(con, paste0("SELECT * FROM ", cohort_table))
    out <- base
    key <- c("subject_id", "cohort_start_date")
    keyof <- function(d) paste(d$subject_id, as.character(d$cohort_start_date))
    st$unit_report <- NULL
    if (length(st$labs)) {
      tbl <- "measurement"
      if (isTRUE(harmonize_units)) {
        st$unit_report <- harmonize_measurement_units(con, st$labs)
        tbl <- "measurement_harmonized"
      }
      labs <- extract_measurements(con, cohort_table, loinc_map = st$labs, strategy = lab_strategy,
                                   outlier_bounds = effective_bounds(), measurement_table = tbl)
      idx <- match(keyof(out), keyof(labs))
      for (nm in setdiff(names(labs), key)) out[[nm]] <- labs[[nm]][idx]
    }
    if (length(st$medications)) {
      med <- extract_medication_classes(con, cohort_table, st$medications, "standard", window = med_window)
      for (nm in names(st$medications)) out[[paste0("rx_", nm)]] <- med[[nm]]
    }
    for (nm in names(st$conditions)) {
      pats <- paste(sprintf("starts_with(REPLACE(UPPER(co.condition_source_value), '.', ''), '%s')",
                            toupper(gsub(".", "", st$conditions[[nm]], fixed = TRUE))), collapse = " OR ")
      hit <- DBI::dbGetQuery(con, sprintf(paste0(
        "SELECT DISTINCT c.subject_id, c.cohort_start_date FROM %s c JOIN condition_occurrence co ",
        "ON co.person_id = c.subject_id AND co.condition_start_date <= c.cohort_end_date WHERE %s"),
        cohort_table, pats))
      out[[paste0("dx_", nm)]] <- as.integer(keyof(out) %in% keyof(hit))
    }
    out
  }
  generate <- function(cohort_table, stratify_by = NULL, lab_strategy = "last_before_discharge",
                       med_window = "pre_admission", extra_continuous = character(0),
                       extra_categorical = character(0), ...) {
    df <- extract(cohort_table, lab_strategy, med_window)
    st$features <- df
    cat_vars <- c(grep("^(rx_|dx_)", names(df), value = TRUE), extra_categorical)
    generate_table1(df, strata_col = stratify_by, continuous_vars = c(names(st$labs), extra_continuous),
                    categorical_vars = cat_vars, ...)
  }
  self$add_domain_profile <- add_domain_profile
  self$add_custom_lab <- add_custom_lab
  self$add_custom_medication <- add_custom_medication
  self$add_custom_condition <- add_custom_condition
  self$extract <- extract
  self$generate <- generate
  self$state <- st
  self$close <- function() if (owns) DBI::dbDisconnect(con, shutdown = TRUE)
  class(self) <- "table1_generator"
  self
}
