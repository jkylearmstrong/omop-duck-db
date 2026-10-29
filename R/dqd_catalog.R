# Schema-driven DataQualityDashboard check catalog, executed in DuckDB SQL (the R twin of python/omop_etl/dqd_catalog.py).
# Check names, thresholds and the result shape are the same in both languages.

# check names that exist in the official DataQualityDashboard (checked against v2.x for CDM 5.4); the other catalog checks
# (plausibleFutureDate, visitPersonMatch) are extensions of this package and are labelled so in `check_origin`
.OFFICIAL_DQD_CHECKS <- c("cdmTable", "cdmField", "isRequired", "isPrimaryKey", "isForeignKey", "measureValueCompleteness",
                          "standardConceptRecordCompleteness", "plausibleStartBeforeEnd", "plausibleAfterBirth",
                          "plausibleBeforeDeath", "fkDomain", "isStandardValidConcept", "fkClass", "plausibleValueLow",
                          "plausibleValueHigh", "sourceConceptRecordCompleteness", "measurePersonCompleteness",
                          "measureConditionEraCompleteness", "measureObservationPeriodOverlap")
.DQD_KAHN <- list(
  cdmTable = c("Conformance", "Verification"), cdmField = c("Conformance", "Verification"),
  isRequired = c("Conformance", "Verification"), isPrimaryKey = c("Conformance", "Verification"),
  isForeignKey = c("Conformance", "Verification"), visitPersonMatch = c("Conformance", "Verification"),
  measureValueCompleteness = c("Completeness", "Verification"),
  standardConceptRecordCompleteness = c("Completeness", "Verification"),
  fkDomain = c("Conformance", "Verification"), isStandardValidConcept = c("Conformance", "Verification"),
  fkClass = c("Conformance", "Verification"), sourceConceptRecordCompleteness = c("Completeness", "Verification"),
  measurePersonCompleteness = c("Completeness", "Verification"), measureConditionEraCompleteness = c("Completeness", "Verification"),
  measureObservationPeriodOverlap = c("Plausibility", "Validation"),
  plausibleValueLow = c("Plausibility", "Verification"), plausibleValueHigh = c("Plausibility", "Verification"),
  plausibleStartBeforeEnd = c("Plausibility", "Validation"), plausibleAfterBirth = c("Plausibility", "Validation"),
  plausibleBeforeDeath = c("Plausibility", "Validation"), plausibleFutureDate = c("Plausibility", "Validation"))
.DQD_FK <- c(person_id = "person", visit_occurrence_id = "visit_occurrence", preceding_visit_occurrence_id = "visit_occurrence",
             visit_detail_id = "visit_detail", provider_id = "provider", care_site_id = "care_site", location_id = "location")
# the domain the concept of a clinical field must belong to (the official fkDomain check); the measurement and observation
# fields accept both unless `strict_domains` is set
.DQD_DOMAIN <- list(condition_concept_id = "Condition", drug_concept_id = "Drug", procedure_concept_id = "Procedure",
                    device_concept_id = "Device", visit_concept_id = "Visit", specimen_concept_id = "Specimen",
                    measurement_concept_id = "Measurement", observation_concept_id = "Observation", gender_concept_id = "Gender",
                    race_concept_id = "Race", ethnicity_concept_id = "Ethnicity", unit_concept_id = "Unit")
# fields whose concept must also be standard and valid (the official isStandardValidConcept)
.DQD_STANDARD <- c("condition_concept_id", "drug_concept_id", "procedure_concept_id", "device_concept_id", "visit_concept_id",
                   "specimen_concept_id", "measurement_concept_id", "observation_concept_id")
# the concept class the concept of a field must have (the official fkClass)
.DQD_CLASS <- list("drug_era.drug_concept_id" = "Ingredient", "drug_strength.ingredient_concept_id" = "Ingredient")
# fixed plausible bounds (low, high) of a field; NA leaves that side open. year_of_birth's upper bound is the run year
.DQD_RANGE <- list("person.year_of_birth" = c(1850, NA), "drug_exposure.days_supply" = c(0, 365), "drug_exposure.quantity" = c(0, NA))
# a person with no row in these tables is a completeness gap (the official measurePersonCompleteness)
.DQD_PERSON_TABLES <- c("visit_occurrence", "condition_occurrence", "drug_exposure", "procedure_occurrence", "measurement",
                        "observation", "device_exposure")
# CDM keys that are not <table>_id
.DQD_PK <- c(death = "person_id", concept_class = "concept_class_id", vocabulary = "vocabulary_id", domain = "domain_id",
             relationship = "relationship_id", concept = "concept_id", cohort_definition = "cohort_definition_id")
.DQD_VOCAB <- c("concept", "vocabulary", "domain", "concept_class", "relationship", "concept_relationship", "concept_ancestor",
                "concept_synonym", "drug_strength", "source_to_concept_map", "cdm_source", "metadata", "cohort_definition")

.dqd_q <- function(name) paste0('"', gsub('"', '""', name), '"')

#' Schema-driven DataQualityDashboard checks in DuckDB
#'
#' Generates the checks from the CDM DDL ([load_cdm_schema()]) so every table and field of the CDM is covered, one batched scan per
#' table, and returns them in the OHDSI DQD JSON shape (`Metadata`, `Overview`, `CheckResults`) with `category` and `context` (Kahn
#' framework) added. This is the same catalog as `omop_etl.dqd_catalog.run_dqd_catalog` in Python.
#'
#' Checks: `cdmTable`, `cdmField`, `isRequired`, `isPrimaryKey`, `isForeignKey`, `visitPersonMatch`, `measureValueCompleteness`,
#' `standardConceptRecordCompleteness`, `sourceConceptRecordCompleteness`, `fkDomain`, `fkClass`, `isStandardValidConcept`,
#' `measurePersonCompleteness`, `measureConditionEraCompleteness`, `measureObservationPeriodOverlap`, `plausibleValueLow`,
#' `plausibleValueHigh`, `plausibleStartBeforeEnd`, `plausibleAfterBirth`, `plausibleBeforeDeath` and `plausibleFutureDate`.
#' `fkDomain`, `fkClass` and `isStandardValidConcept` need a populated `concept` table with `domain_id` / `concept_class_id` (and
#' `standard_concept` / `invalid_reason`); without them those checks are not generated.
#'
#' `plausibleValueLow` / `plausibleValueHigh` check fixed bounds (birth year, days supply, quantity) and the bundled physiologic
#' limits of `measurement.value_as_number`; the latter apply only to rows whose `unit_concept_id` matches a limit, so an ETL that
#' leaves it 0 gets `NOT_APPLICABLE` (use [sanitize_measurements()] there). Not run: the per-concept checks of the official list
#' (`plausibleGender`, `plausibleUnitConceptIds`, ...); report the subset when you cite the result.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param output_json Optional path to write the result as JSON.
#' @param cdm_version CDM version recorded in `Metadata`.
#' @param tables Optional character vector restricting the CDM tables checked.
#' @param threshold_pct A conformance or plausibility check fails when its violating share exceeds this percentage (default 0).
#' @param value_completeness_threshold_pct `measureValueCompleteness` fails a field at or above this NULL share (default 100, fully empty).
#' @param zero_concept_threshold_pct `standardConceptRecordCompleteness` fails above this share of `*_concept_id = 0` (default 10).
#' @param death_grace_days Days after death that `plausibleBeforeDeath` tolerates (default 60).
#' @param today Date used by `plausibleFutureDate` and the birth-year bound (default today).
#' @param strict_domains If `TRUE`, measurement and observation fields do not accept each other's domain.
#' @param source_concept_threshold_pct `sourceConceptRecordCompleteness` fails a `*_source_concept_id` that is 0 in at least this share (default 100).
#' @param person_completeness_threshold_pct `measurePersonCompleteness` fails a clinical table with no row for at least this share of persons (default 100).
#' @return A list with `Metadata`, `Overview` and `CheckResults`.
#' @export
run_dqd_catalog <- function(con, output_json = NULL, cdm_version = "5.4", tables = NULL, threshold_pct = 0,
                            value_completeness_threshold_pct = 100, zero_concept_threshold_pct = 10, death_grace_days = 60L,
                            today = Sys.Date(), strict_domains = FALSE, source_concept_threshold_pct = 100,
                            person_completeness_threshold_pct = 100) {
  schema <- load_cdm_schema()
  want <- if (length(tables)) tolower(tables) else NULL
  today <- as.Date(today)
  info <- DBI::dbGetQuery(con, "SELECT lower(table_name) AS t, lower(column_name) AS c, upper(data_type) AS d FROM information_schema.columns WHERE table_catalog = current_database() AND table_schema = current_schema()")
  present <- list()
  for (i in seq_len(nrow(info))) present[[info$t[[i]]]][[info$c[[i]]]] <- info$d[[i]]
  results <- list()
  one <- function(sql) as.numeric(DBI::dbGetQuery(con, sql)[[1]][[1]])

  add <- function(name, tbl, fld, desc, viol, denom, thr, level = "FIELD", cmp = "gt") {
    if (denom == 0) {
      status <- "NOT_APPLICABLE"; failed <- FALSE; pct <- 0
    } else {
      pct <- 100 * viol / denom
      failed <- if (cmp == "ge") pct >= thr else pct > thr
      status <- if (failed) "FAILED" else "PASSED"
    }
    kahn <- .DQD_KAHN[[name]]
    results[[length(results) + 1L]] <<- list(
      check_id = toupper(sprintf("CAT_%s_%s_%s", name, tbl, if (is.null(fld)) "TABLE" else fld)), check_name = name, check_level = level,
      category = kahn[[1]], context = kahn[[2]], cdm_table_name = tbl, cdm_field_name = fld,
      check_origin = if (name %in% .OFFICIAL_DQD_CHECKS) "DQD" else "omop-duck-db", check_description = desc,
      num_violated_rows = as.integer(viol), pct_violated_rows = round(pct, 4), num_denominator_rows = as.integer(denom),
      threshold_value = thr, execution_status = status, failed = failed)
  }

  concept_rows <- if ("concept" %in% names(present)) one("SELECT count(*) FROM concept") else 0
  death_ok <- "death" %in% names(present) && all(c("person_id", "death_date") %in% names(present$death))
  person_ok <- "person" %in% names(present) && "year_of_birth" %in% names(present$person)
  for (tbl in names(schema)) {
    if (!is.null(want) && !(tbl %in% want)) next
    cols <- schema[[tbl]]
    exists <- tbl %in% names(present)
    add("cdmTable", tbl, NULL, sprintf("Table %s exists in the database.", tbl), if (exists) 0 else 1, 1, 0, level = "TABLE")
    if (!exists) next
    have <- present[[tbl]]
    for (cn in cols$name) add("cdmField", tbl, cn, sprintf("Column %s.%s exists.", tbl, cn), if (cn %in% names(have)) 0 else 1, 1, 0)
    usable <- cols[cols$name %in% names(have), , drop = FALSE]
    n <- one(sprintf("SELECT count(*) FROM %s", .dqd_q(tbl)))
    row <- NULL
    if (nrow(usable)) {
      aggs <- unlist(lapply(seq_len(nrow(usable)), function(i) {
        cn <- usable$name[[i]]
        c(sprintf("count(*) FILTER (WHERE %s IS NULL) AS n%d", .dqd_q(cn), i),
          if (endsWith(cn, "_concept_id")) sprintf("count(*) FILTER (WHERE %s = 0) AS z%d", .dqd_q(cn), i) else sprintf("0 AS z%d", i))
      }))
      row <- as.numeric(unlist(DBI::dbGetQuery(con, sprintf("SELECT %s FROM %s", paste(aggs, collapse = ", "), .dqd_q(tbl)))[1, ]))
    }
    pk <- if (!is.na(.DQD_PK[tbl])) .DQD_PK[[tbl]] else paste0(tbl, "_id")
    for (i in seq_len(nrow(usable))) {
      cn <- usable$name[[i]]
      nulls <- row[[2 * i - 1]]; zeros <- row[[2 * i]]
      if (!usable$nullable[[i]]) add("isRequired", tbl, cn, sprintf("%s.%s is NOT NULL in the CDM.", tbl, cn), nulls, n, threshold_pct)
      add("measureValueCompleteness", tbl, cn, sprintf("Share of NULL values in %s.%s.", tbl, cn), nulls, n,
          value_completeness_threshold_pct, cmp = "ge")
      if (endsWith(cn, "_concept_id") && !endsWith(cn, "source_concept_id")) {
        add("standardConceptRecordCompleteness", tbl, cn, sprintf("Share of %s.%s = 0 (unmapped).", tbl, cn), zeros, n, zero_concept_threshold_pct)
      } else if (endsWith(cn, "source_concept_id")) {
        add("sourceConceptRecordCompleteness", tbl, cn, sprintf("Share of %s.%s = 0 (source code not mapped to a concept).", tbl, cn),
            zeros, n, source_concept_threshold_pct, cmp = "ge")
      }
      key <- paste0(tbl, ".", cn)
      bounds <- .DQD_RANGE[[key]]
      if (!is.null(bounds) && n > 0) {
        lo <- bounds[[1]]; hi <- if (key == "person.year_of_birth") as.numeric(format(today, "%Y")) else bounds[[2]]
        nn <- n - nulls
        if (!is.na(lo)) add("plausibleValueLow", tbl, cn, sprintf("%s.%s is at least %s.", tbl, cn, format(lo)),
                            one(sprintf("SELECT count(*) FROM %s WHERE %s < %s", .dqd_q(tbl), .dqd_q(cn), format(lo))), nn, threshold_pct)
        if (!is.na(hi)) add("plausibleValueHigh", tbl, cn, sprintf("%s.%s is at most %s.", tbl, cn, format(hi)),
                            one(sprintf("SELECT count(*) FROM %s WHERE %s > %s", .dqd_q(tbl), .dqd_q(cn), format(hi))), nn, threshold_pct)
      }
      if (concept_rows > 0 && n > 0 && !is.null(.DQD_CLASS[[key]]) && "concept_class_id" %in% names(present$concept)) {
        lst <- paste0("'", .DQD_CLASS[[key]], "'", collapse = ", ")
        v <- one(sprintf("SELECT count(*) FROM %s t JOIN concept r ON r.concept_id = t.%s WHERE t.%s <> 0 AND r.concept_class_id NOT IN (%s)",
                         .dqd_q(tbl), .dqd_q(cn), .dqd_q(cn), lst))
        add("fkClass", tbl, cn, sprintf("The concept of %s.%s has class %s.", tbl, cn, paste(.DQD_CLASS[[key]], collapse = "/")), v, n, threshold_pct)
      }
      if (cn == pk && n > 0) {
        # like the official DQD: every row whose key occurs more than once (not only the surplus copies)
        dup <- one(sprintf("SELECT count(*) FROM (SELECT count(*) OVER (PARTITION BY %s) AS k FROM %s) WHERE k > 1", .dqd_q(pk), .dqd_q(tbl)))
        add("isPrimaryKey", tbl, pk, sprintf("%s.%s is unique.", tbl, pk), dup, n, threshold_pct)
      }
      ref <- if (!is.na(.DQD_FK[cn])) .DQD_FK[[cn]] else NA_character_
      if (!is.na(ref) && ref != tbl && ref %in% names(present) && n > 0) {
        rid <- if (cn != "preceding_visit_occurrence_id") paste0(ref, "_id") else "visit_occurrence_id"
        if (rid %in% names(present[[ref]])) {
          v <- one(sprintf("SELECT count(*) FROM %s t WHERE t.%s IS NOT NULL AND NOT EXISTS (SELECT 1 FROM %s r WHERE r.%s = t.%s)",
                           .dqd_q(tbl), .dqd_q(cn), .dqd_q(ref), .dqd_q(rid), .dqd_q(cn)))
          add("isForeignKey", tbl, cn, sprintf("%s.%s resolves in %s.%s.", tbl, cn, ref, rid), v, n, threshold_pct)
          if (ref == "visit_occurrence" && cn == "visit_occurrence_id" && "person_id" %in% names(have) && "person_id" %in% names(present[[ref]])) {
            v <- one(sprintf("SELECT count(*) FROM %s t JOIN %s r ON r.%s = t.%s WHERE t.person_id IS NOT NULL AND r.person_id <> t.person_id",
                             .dqd_q(tbl), .dqd_q(ref), .dqd_q(rid), .dqd_q(cn)))
            add("visitPersonMatch", tbl, cn, sprintf("%s.person_id equals the person of its visit_occurrence.", tbl), v, n, threshold_pct)
          }
        }
      } else if (endsWith(cn, "_concept_id") && concept_rows > 0 && tbl != "concept" && n > 0) {
        v <- one(sprintf("SELECT count(*) FROM %s t WHERE t.%s IS NOT NULL AND t.%s <> 0 AND NOT EXISTS (SELECT 1 FROM concept r WHERE r.concept_id = t.%s)",
                         .dqd_q(tbl), .dqd_q(cn), .dqd_q(cn), .dqd_q(cn)))
        add("isForeignKey", tbl, cn, sprintf("%s.%s resolves in concept.concept_id.", tbl, cn), v, n, threshold_pct)
      }
      if (concept_rows > 0 && tbl != "concept" && n > 0 && !is.null(.DQD_DOMAIN[[cn]]) && "domain_id" %in% names(present$concept)) {
        allowed <- c(.DQD_DOMAIN[[cn]], if (cn %in% c("measurement_concept_id", "observation_concept_id") && !strict_domains) c("Observation", "Measurement"))
        allowed <- unique(allowed)
        lst <- paste0("'", allowed, "'", collapse = ", ")
        v <- one(sprintf("SELECT count(*) FROM %s t JOIN concept r ON r.concept_id = t.%s WHERE t.%s <> 0 AND r.domain_id NOT IN (%s)",
                         .dqd_q(tbl), .dqd_q(cn), .dqd_q(cn), lst))
        add("fkDomain", tbl, cn, sprintf("The concept of %s.%s is in the %s domain.", tbl, cn, paste(allowed, collapse = "/")), v, n, threshold_pct)
      }
      if (concept_rows > 0 && tbl != "concept" && n > 0 && cn %in% .DQD_STANDARD && all(c("standard_concept", "invalid_reason") %in% names(present$concept))) {
        v <- one(sprintf("SELECT count(*) FROM %s t JOIN concept r ON r.concept_id = t.%s WHERE t.%s <> 0 AND (r.standard_concept IS DISTINCT FROM 'S' OR r.invalid_reason IS NOT NULL)",
                         .dqd_q(tbl), .dqd_q(cn), .dqd_q(cn)))
        add("isStandardValidConcept", tbl, cn, sprintf("The concept of %s.%s is standard and valid.", tbl, cn), v, n, threshold_pct)
      }
    }

    if (tbl %in% .DQD_PERSON_TABLES && n > 0 && "person_id" %in% names(have) && "person" %in% names(present) && "person_id" %in% names(present$person)) {
      people <- one("SELECT count(*) FROM person")
      miss <- one(sprintf("SELECT count(*) FROM person p WHERE NOT EXISTS (SELECT 1 FROM %s t WHERE t.person_id = p.person_id)", .dqd_q(tbl)))
      add("measurePersonCompleteness", tbl, "person_id", sprintf("Share of persons with no row in %s.", tbl), miss, people,
          person_completeness_threshold_pct, level = "TABLE", cmp = "ge")
    }
    if (tbl == "condition_era" && "condition_occurrence" %in% names(present) && "person_id" %in% names(have) && "person_id" %in% names(present$condition_occurrence)) {
      miss <- one("SELECT count(DISTINCT person_id) FROM condition_occurrence o WHERE NOT EXISTS (SELECT 1 FROM condition_era e WHERE e.person_id = o.person_id)")
      den <- one("SELECT count(DISTINCT person_id) FROM condition_occurrence")
      add("measureConditionEraCompleteness", tbl, "person_id", "Share of persons with a condition_occurrence but no condition_era.", miss, den,
          threshold_pct, level = "TABLE")
    }
    if (tbl == "observation_period" && n > 0 && all(c("observation_period_id", "person_id", "observation_period_start_date", "observation_period_end_date") %in% names(have))) {
      # a self-join, not a correlated EXISTS: DuckDB returned 0 for the EXISTS form on overlapping rows
      v <- one(paste("SELECT count(DISTINCT a.observation_period_id) FROM observation_period a JOIN observation_period b ON",
                     "b.person_id = a.person_id AND b.observation_period_id <> a.observation_period_id AND",
                     "b.observation_period_start_date <= a.observation_period_end_date AND",
                     "b.observation_period_end_date >= a.observation_period_start_date"))
      add("measureObservationPeriodOverlap", tbl, "observation_period_start_date", "Observation periods of one person do not overlap.", v, n, threshold_pct)
    }
    if (tbl == "measurement" && n > 0 && all(c("measurement_concept_id", "unit_concept_id", "value_as_number") %in% names(have))) {
      lim <- .sanitize_limits_cte(.sanitize_bundled_limits())
      r <- DBI::dbGetQuery(con, paste0("WITH ", lim, " SELECT count(*) FILTER (WHERE m.value_as_number < l.min_value) AS lo, ",
                                       "count(*) FILTER (WHERE m.value_as_number > l.max_value) AS hi, count(*) AS den ",
                                       "FROM measurement m JOIN lim l ON l.concept_id = m.measurement_concept_id AND l.unit_concept_id = m.unit_concept_id ",
                                       "WHERE m.value_as_number IS NOT NULL"))
      add("plausibleValueLow", tbl, "value_as_number", "measurement.value_as_number is not below the bundled physiologic minimum (rows whose unit matches a limit).",
          as.numeric(r$lo), as.numeric(r$den), threshold_pct)
      add("plausibleValueHigh", tbl, "value_as_number", "measurement.value_as_number is not above the bundled physiologic maximum (rows whose unit matches a limit).",
          as.numeric(r$hi), as.numeric(r$den), threshold_pct)
    }

    if (tbl %in% c(.DQD_VOCAB, "cohort", "person") || !("person_id" %in% names(have)) || n == 0) next
    dates <- usable$name[vapply(usable$name, function(cn) have[[cn]] == "DATE" || startsWith(have[[cn]], "TIMESTAMP"), logical(1))]
    for (sd in dates[grepl("_start_date(time)?$", dates)]) {
      ed <- sub("_start_date", "_end_date", sd, fixed = TRUE)
      if (ed %in% dates) {
        v <- one(sprintf("SELECT count(*) FROM %s WHERE %s < %s", .dqd_q(tbl), .dqd_q(ed), .dqd_q(sd)))
        nn <- one(sprintf("SELECT count(*) FROM %s WHERE %s IS NOT NULL AND %s IS NOT NULL", .dqd_q(tbl), .dqd_q(ed), .dqd_q(sd)))
        add("plausibleStartBeforeEnd", tbl, sd, sprintf("%s.%s is not before %s.", tbl, ed, sd), v, nn, threshold_pct)
      }
    }
    for (d in dates) {
      nn <- one(sprintf("SELECT count(*) FROM %s WHERE %s IS NOT NULL", .dqd_q(tbl), .dqd_q(d)))
      if (person_ok) {
        v <- one(sprintf("SELECT count(*) FROM %s t JOIN person p ON p.person_id = t.person_id WHERE t.%s IS NOT NULL AND p.year_of_birth IS NOT NULL AND year(t.%s) < p.year_of_birth",
                         .dqd_q(tbl), .dqd_q(d), .dqd_q(d)))
        add("plausibleAfterBirth", tbl, d, sprintf("%s.%s is not before the person's birth year.", tbl, d), v, nn, threshold_pct)
      }
      if (death_ok && tbl != "death") {
        v <- one(sprintf("SELECT count(*) FROM %s t JOIN (SELECT person_id, min(death_date) AS dd FROM death GROUP BY 1) x ON x.person_id = t.person_id WHERE t.%s > x.dd + INTERVAL '%d' DAY",
                         .dqd_q(tbl), .dqd_q(d), as.integer(death_grace_days)))
        add("plausibleBeforeDeath", tbl, d, sprintf("%s.%s is within %d days after death at most.", tbl, d, as.integer(death_grace_days)), v, nn, threshold_pct)
      }
      v <- one(sprintf("SELECT count(*) FROM %s WHERE CAST(%s AS DATE) > DATE '%s'", .dqd_q(tbl), .dqd_q(d), format(today)))
      add("plausibleFutureDate", tbl, d, sprintf("%s.%s is not after %s.", tbl, d, format(today)), v, nn, threshold_pct)
    }
  }
  .dqd_package(results, cdm_version, output_json)
}

.dqd_package <- function(results, cdm_version, output_json) {
  applicable <- Filter(function(r) !identical(r$execution_status, "NOT_APPLICABLE"), results)
  failed <- sum(vapply(applicable, function(r) isTRUE(r$failed), logical(1)))
  out <- list(
    Metadata = list(cdm_version = cdm_version, dqa_tool = "omop-duck-db native DQD catalog",
                    execution_timestamp = format(as.POSIXlt(Sys.time(), "UTC"), "%Y-%m-%dT%H:%M:%SZ")),
    Overview = list(count_total = length(results), count_not_applicable = length(results) - length(applicable),
                    count_passed = length(applicable) - failed, count_failed = failed,
                    percent_passed = if (length(applicable)) round(100 * (length(applicable) - failed) / length(applicable), 2) else 100),
    CheckResults = results)
  if (!is.null(output_json)) {
    dir.create(dirname(normalizePath(output_json, mustWork = FALSE)), showWarnings = FALSE, recursive = TRUE)
    jsonlite::write_json(out, output_json, pretty = TRUE, auto_unbox = TRUE, na = "null", null = "null")
  }
  out
}

#' [run_dqd()] and [run_dqd_catalog()] in one DQD-shaped result
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param output_json Optional path to write the combined result as JSON.
#' @param ... Passed to [run_dqd_catalog()].
#' @return A list with `Metadata`, `Overview` and `CheckResults`.
#' @export
run_dqd_full <- function(con, output_json = NULL, ...) {
  base <- run_dqd(con)
  cat <- run_dqd_catalog(con, ...)
  .dqd_package(c(base$CheckResults, cat$CheckResults), list(...)$cdm_version %||% "5.4", output_json)
}
