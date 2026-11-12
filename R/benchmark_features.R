# One-call, leakage-free tabular feature table for the readmission benchmark (v0.13.0).
# Mirror of python/omop_etl/benchmark_features.py. History (utilization, medications) ends before the admission,
# Elixhauser is anchored at discharge and includes the index stay's diagnoses, vitals/labs use measurements dated
# in [admission - lab_lookback_days, discharge] only.

#' Discharge disposition concept -> benchmark category
#'
#' 8536 Home, 581476 Home Health, 8863 SNF, 32216 AMA (as written by the PCORnet ETL); anything else is "other".
#' @export
DEFAULT_DISPOSITION_MAP <- c(`8536` = "home", `581476` = "home_health", `8863` = "snf", `32216` = "ama")


#' LOINC codes per lab and per vital sign for the benchmark feature table
#'
#' `BENCHMARK_LAB_LOINCS` is a superset of `CORE_14_LAB_PANEL`: sites code the same test under several LOINC codes (ALT is
#' often 1743-4, HbA1c 17856-6, glucose 41653-7) and a code that is not listed silently drops the lab. It also lists
#' `hematocrit`, `alkaline_phosphatase`, `ldl` and a measured `anion_gap`. `BENCHMARK_VITAL_LOINCS` adds heart rate,
#' respiratory rate, body temperature and oxygen saturation to BMI and blood pressure. A name with no concept in the
#' database is skipped and reported in `attr(, "skipped")`.
#' @format Named lists of character vectors (LOINC codes).
#' @name BENCHMARK_LAB_LOINCS
#' @aliases BENCHMARK_VITAL_LOINCS
#' @export
BENCHMARK_LAB_LOINCS <- list(
  bun = c("3094-0", "6299-2"), creatinine = c("2160-0", "38483-4"), sodium = c("2951-2", "2947-0"),
  potassium = c("2823-3", "6298-4"), chloride = "2075-0", bicarbonate = c("1963-8", "2028-9", "2026-3"),
  glucose = c("2345-7", "2339-0", "41653-7"), calcium = "17861-6", albumin = c("1751-7", "2862-1"),
  bilirubin_total = "1975-2", ast = c("1920-8", "30239-8"), alt = c("1742-6", "1743-4", "1744-2"),
  wbc = c("6690-2", "26464-8"), hemoglobin = c("718-7", "59260-0"), platelets = c("777-3", "26515-7"),
  hba1c = c("4548-4", "17856-6", "17855-8", "59261-8"), hematocrit = c("20570-8", "4544-3", "4545-0"),
  alkaline_phosphatase = "6768-6", ldl = c("13457-7", "2089-1", "18262-6"), anion_gap = c("33037-3", "41276-7", "1863-0"))

#' @rdname BENCHMARK_LAB_LOINCS
#' @export
BENCHMARK_VITAL_LOINCS <- list(
  bmi = "39156-5", systolic_bp = "8480-6", diastolic_bp = "8462-4", heart_rate = "8867-4", respiratory_rate = "9279-1",
  body_temperature = "8310-5", oxygen_saturation = c("2708-6", "59408-5"))

#' Physiologic plausibility bounds and rescaling for single vital / lab measurements
#'
#' `DEFAULT_BOUNDS` is `list(name = c(min, max))` in conventional US units (mg/dL, g/dL, 10^3/uL, degrees Celsius). A single
#' measurement outside its bounds is dropped (not clipped) before the stay's mean and latest value are taken, so one artifact
#' such as a BMI of 562000 cannot dominate a stay. `DEFAULT_RESCALE` is `list(name = c(above, up_to, divisor))`: a value in
#' `(above, up_to]` is divided first (WBC is a count per uL at some sites, 10^3/uL at others).
#' @format Named lists of numeric vectors.
#' @name DEFAULT_BOUNDS
#' @aliases DEFAULT_RESCALE
#' @export
DEFAULT_BOUNDS <- list(
  bmi = c(10, 100), systolic_bp = c(50, 260), diastolic_bp = c(30, 160), heart_rate = c(20, 250), respiratory_rate = c(4, 60),
  body_temperature = c(30, 45), oxygen_saturation = c(50, 100), glucose = c(10, 1500), bun = c(1, 250), creatinine = c(0.1, 30),
  sodium = c(100, 180), potassium = c(1.5, 10), chloride = c(60, 150), bicarbonate = c(5, 60), calcium = c(4, 16),
  hematocrit = c(10, 75), hemoglobin = c(1, 25), wbc = c(0.1, 200), platelets = c(5, 2000), albumin = c(0.5, 7),
  bilirubin_total = c(0.1, 40), alkaline_phosphatase = c(5, 2500), ast = c(1, 5000), alt = c(1, 5000), hba1c = c(3, 25),
  ldl = c(10, 500), anion_gap = c(1, 45))

#' @rdname DEFAULT_BOUNDS
#' @export
DEFAULT_RESCALE <- list(wbc = c(200, 200000, 1000))

#' Medication classes of the eDERRI protocol
#'
#' The ATC class concepts behind the protocol's `med_<name>` flags. They differ from `DEFAULT_MEDICATION_CONCEPTS`
#' (`antihypertensives` is ATC C02 only, `lipid_modifying` is all of C10, `biguanides` is A10BA). Use with
#' `extract_benchmark_features(profile = "ederri")` or `medication_set`.
#' @format A named list of `list(ancestor_concept_ids = ...)` specs.
#' @export
EDERRI_MEDICATION_SPECS <- list(
  biguanides = list(ancestor_concept_ids = 21600745), insulins = list(ancestor_concept_ids = 21600713),
  sulfonylureas = list(ancestor_concept_ids = 21600749), raas = list(ancestor_concept_ids = 21601782),
  antihypertensives = list(ancestor_concept_ids = 21600381), beta_blockers = list(ancestor_concept_ids = 21601664),
  systemic_corticosteroids = list(ancestor_concept_ids = 21602722), lipid_modifying = list(ancestor_concept_ids = 21601853))

.bf_default_med_specs <- function() {
  stats::setNames(lapply(DEFAULT_MEDICATION_CONCEPTS, function(ids) list(ancestor_concept_ids = as.numeric(ids))),
                  names(DEFAULT_MEDICATION_CONCEPTS))
}

.bf_cohort <- function(con, cohort_table, cohort_id) {
  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", cohort_table))$column_name)
  pick <- function(...) { for (n in c(...)) if (n %in% cols) return(n); NULL }
  person <- pick("subject_id", "person_id"); start <- pick("cohort_start_date", "visit_start_date", "index_date")
  end <- pick("cohort_end_date", "visit_end_date")
  if (is.null(person) || is.null(start)) {
    stop(sprintf("'%s' needs a person column (subject_id / person_id) and an admission date (cohort_start_date / visit_start_date).", cohort_table), call. = FALSE)
  }
  sel <- c(sprintf("%s AS subject_id", person), sprintf("CAST(%s AS DATE) AS cohort_start_date", start),
           sprintf("CAST(%s AS DATE) AS cohort_end_date", if (is.null(end)) start else end))
  for (extra in c("visit_occurrence_id", "outcome_flag", "discharged_to_concept_id", "site", "site_anon", "stay_rank")) {
    if (extra %in% cols) sel <- c(sel, extra)
  }
  where <- if (!is.null(cohort_id) && "cohort_definition_id" %in% cols) sprintf(" WHERE cohort_definition_id = %d", as.integer(cohort_id)) else ""
  df <- DBI::dbGetQuery(con, sprintf("SELECT %s FROM %s%s", paste(sel, collapse = ", "), cohort_table, where))
  if (!nrow(df)) stop(sprintf("'%s' has no rows.", cohort_table), call. = FALSE)
  df$cohort_start_date <- as.Date(df$cohort_start_date)
  df$cohort_end_date <- as.Date(df$cohort_end_date)
  df$cohort_end_date[is.na(df$cohort_end_date)] <- df$cohort_start_date[is.na(df$cohort_end_date)]
  if (any(df$cohort_end_date < df$cohort_start_date)) stop("Some cohort rows are discharged before they are admitted.", call. = FALSE)
  df$row_id <- seq_len(nrow(df)) - 1L
  df
}

.bf_concept_map <- function(con, loincs, overrides) {
  rows <- list()
  for (nm in names(overrides)) for (i in overrides[[nm]]) rows[[length(rows) + 1L]] <- data.frame(name = nm, concept_id = as.numeric(i))
  has_concept <- nrow(DBI::dbGetQuery(con, "SELECT 1 FROM information_schema.tables WHERE table_name = 'concept'")) > 0
  if (has_concept) {
    codes <- do.call(rbind, lapply(setdiff(names(loincs), names(overrides)), function(n)
      data.frame(name = n, code = as.character(unlist(loincs[[n]])), stringsAsFactors = FALSE)))
    if (!is.null(codes) && nrow(codes)) {
      duckdb::duckdb_register(con, "_bf_codes", codes)
      on.exit(duckdb::duckdb_unregister(con, "_bf_codes"), add = TRUE)
      got <- DBI::dbGetQuery(con, "SELECT DISTINCT k.name, CAST(c.concept_id AS DOUBLE) AS concept_id FROM _bf_codes k
                                   JOIN concept c ON c.concept_code = k.code AND c.vocabulary_id = 'LOINC'")
      if (nrow(got)) rows[[length(rows) + 1L]] <- got
    }
  }
  if (!length(rows)) return(data.frame(name = character(), concept_id = numeric()))
  unique(do.call(rbind, rows))
}

.bf_bounds_frame <- function(cmap, bounds, rescale) {
  big <- 1e300
  get <- function(lst, nm, k, default) {
    v <- lst[[nm]]
    if (is.null(v) || is.na(v[k])) default else as.numeric(v[k])
  }
  cmap$lo <- vapply(cmap$name, function(nm) get(bounds, nm, 1L, -big), numeric(1))
  cmap$hi <- vapply(cmap$name, function(nm) get(bounds, nm, 2L, big), numeric(1))
  cmap$rs_lo <- vapply(cmap$name, function(nm) get(rescale, nm, 1L, big), numeric(1))
  cmap$rs_hi <- vapply(cmap$name, function(nm) get(rescale, nm, 2L, big), numeric(1))
  cmap$rs_div <- vapply(cmap$name, function(nm) get(rescale, nm, 3L, 1), numeric(1))
  cmap
}

.bf_measurement_stats <- function(con, cohort, cmap, pre_days, bounds = NULL, rescale = NULL) {
  if (!nrow(cmap)) return(data.frame(row_id = integer(), name = character(), mean = numeric(), last = numeric(), n = integer()))
  duckdb::duckdb_register(con, "_bf_c", cohort[, c("row_id", "subject_id", "cohort_start_date", "cohort_end_date")])
  duckdb::duckdb_register(con, "_bf_map", .bf_bounds_frame(cmap, bounds, rescale))
  on.exit({ duckdb::duckdb_unregister(con, "_bf_c"); duckdb::duckdb_unregister(con, "_bf_map") }, add = TRUE)
  DBI::dbGetQuery(con, sprintf("
    SELECT t.row_id, t.name, avg(t.v) AS mean,
           arg_max(t.v, struct_pack(t := t.ts, id := t.mid)) AS last,
           count(*) AS n
    FROM (
      SELECT c.row_id, f.name, m.measurement_id AS mid,
             COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)) AS ts,
             CASE WHEN m.value_as_number > f.rs_lo AND m.value_as_number <= f.rs_hi
                  THEN m.value_as_number / f.rs_div ELSE m.value_as_number END AS v,
             f.lo AS lo, f.hi AS hi
      FROM _bf_c c
      JOIN measurement m ON m.person_id = c.subject_id
           AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - %d
           AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)
      JOIN _bf_map f ON f.concept_id = m.measurement_concept_id
      WHERE m.value_as_number IS NOT NULL
    ) t
    WHERE t.v BETWEEN t.lo AND t.hi
    GROUP BY t.row_id, t.name", as.integer(pre_days)))
}

.bf_wide <- function(stats, prefix, names_, n_rows, with_last) {
  out <- list()
  for (nm in names_) {
    s <- stats[stats$name == nm, , drop = FALSE]
    idx <- match(seq_len(n_rows) - 1L, s$row_id)
    out[[paste0(prefix, nm)]] <- s$mean[idx]
    if (with_last) out[[paste0(prefix, nm, "_last")]] <- s$last[idx]
  }
  as.data.frame(out, stringsAsFactors = FALSE, check.names = FALSE)
}

#' The eDERRI protocol's history columns
#'
#' One row per cohort row, in cohort order: `prior_encounters_all_count`, `prior_ip_os_ed_count` (inpatient 9201 / 262, ED
#' 9203 and 2000000048 visits), `days_since_prior_encounter`, `days_since_prior_ip_ed` (`lookback_days` when there is none),
#' `readmission_count_twelve_months`, `n_secondary_dx_ccs` (distinct condition concepts from admission to discharge),
#' `elix_pre_index_conditions_count` (distinct condition concepts before the admission, all history) and `tobacco`
#' (`"Current user"`, `"Former user"` or `"Never user"` from tobacco observation concepts and ICD F17 / Z72.0 / 305.1 / Z87.891 /
#' V15.82). Visits are counted in `[admission - lookback_days, admission)`; diagnoses and observations up to the discharge date.
#' Same as Python `omop_etl.extract_protocol_history_features`.
#'
#' @param con DuckDB connection to an OMOP CDM.
#' @param cohort data frame with `row_id`, `subject_id`, `cohort_start_date`, `cohort_end_date` (as built by
#'   [extract_benchmark_features()]).
#' @param lookback_days visit window in days (default 365).
#' @return A data frame with one row per cohort row.
#' @export
extract_protocol_history_features <- function(con, cohort, lookback_days = 365) {
  if (lookback_days < 0) stop("lookback_days must be non-negative.", call. = FALSE)
  lb <- as.integer(lookback_days)
  duckdb::duckdb_register(con, "_bf_ph", cohort[, c("row_id", "subject_id", "cohort_start_date", "cohort_end_date")])
  on.exit(duckdb::duckdb_unregister(con, "_bf_ph"), add = TRUE)
  util <- DBI::dbGetQuery(con, sprintf("
    SELECT c.row_id,
           COUNT(pv.visit_occurrence_id) AS prior_encounters_all_count,
           COUNT(CASE WHEN pv.visit_concept_id IN (9201, 9203, 262, 2000000048) THEN 1 END) AS prior_ip_os_ed_count,
           COALESCE(date_diff('day', MAX(pv.visit_start_date), CAST(c.cohort_start_date AS DATE)), %1$d) AS days_since_prior_encounter,
           COALESCE(date_diff('day', MAX(CASE WHEN pv.visit_concept_id IN (9201, 9203, 262) THEN pv.visit_start_date END),
                              CAST(c.cohort_start_date AS DATE)), %1$d) AS days_since_prior_ip_ed,
           COUNT(CASE WHEN pv.visit_concept_id IN (9201, 262)
                       AND pv.visit_start_date >= CAST(c.cohort_start_date AS DATE) - 365 THEN 1 END) AS readmission_count_twelve_months
    FROM _bf_ph c
    LEFT JOIN visit_occurrence pv ON pv.person_id = c.subject_id
         AND pv.visit_start_date < CAST(c.cohort_start_date AS DATE)
         AND pv.visit_start_date >= CAST(c.cohort_start_date AS DATE) - %1$d
    GROUP BY c.row_id, c.cohort_start_date ORDER BY c.row_id", lb))
  dx <- DBI::dbGetQuery(con, "
    SELECT c.row_id,
           COUNT(DISTINCT CASE WHEN co.condition_start_date >= CAST(c.cohort_start_date AS DATE) THEN co.condition_concept_id END) AS n_secondary_dx_ccs,
           COUNT(DISTINCT CASE WHEN co.condition_start_date < CAST(c.cohort_start_date AS DATE) THEN co.condition_concept_id END) AS elix_pre_index_conditions_count,
           MAX(CASE WHEN co.condition_source_value LIKE 'F17%' OR co.condition_source_value LIKE 'Z72.0%'
                     OR co.condition_source_value LIKE '305.1%' THEN 1 ELSE 0 END) AS tob_cur_dx,
           MAX(CASE WHEN co.condition_source_value LIKE 'Z87.891%' OR co.condition_source_value LIKE 'V15.82%' THEN 1 ELSE 0 END) AS tob_former_dx
    FROM _bf_ph c
    LEFT JOIN condition_occurrence co ON co.person_id = c.subject_id
         AND co.condition_start_date <= CAST(c.cohort_end_date AS DATE)
    GROUP BY c.row_id ORDER BY c.row_id")
  obs <- DBI::dbGetQuery(con, "
    SELECT c.row_id,
           MAX(CASE WHEN o.observation_concept_id IN (4005823, 4276526) THEN 1 ELSE 0 END) AS tob_cur_obs,
           MAX(CASE WHEN o.observation_concept_id = 4310280 THEN 1 ELSE 0 END) AS tob_former_obs
    FROM _bf_ph c
    LEFT JOIN observation o ON o.person_id = c.subject_id AND o.observation_date <= CAST(c.cohort_end_date AS DATE)
    GROUP BY c.row_id ORDER BY c.row_id")
  cur <- dx$tob_cur_dx == 1 | obs$tob_cur_obs == 1
  former <- dx$tob_former_dx == 1 | obs$tob_former_obs == 1
  out <- util[, setdiff(names(util), "row_id"), drop = FALSE]
  out$n_secondary_dx_ccs <- dx$n_secondary_dx_ccs
  out$elix_pre_index_conditions_count <- dx$elix_pre_index_conditions_count
  out$tobacco <- ifelse(cur, "Current user", ifelse(former, "Former user", "Never user"))
  out
}

# Reorder an extractor result (one row per distinct person x index date) to the cohort's row order.
.bf_align <- function(cohort, df, prefix = "", only = NULL) {
  date_col <- NULL
  for (cc in c("cohort_start_date", "cohort_end_date", "index_date")) if (cc %in% names(df)) { date_col <- cc; break }
  person <- if ("subject_id" %in% names(df)) "subject_id" else "person_id"
  if (is.null(date_col)) stop("Cannot align extractor output without a date column.", call. = FALSE)
  key_date <- if (date_col == "cohort_end_date") "cohort_end_date" else "cohort_start_date"
  d <- df
  d[[date_col]] <- as.Date(d[[date_col]])
  d <- d[!duplicated(d[, c(person, date_col)]), , drop = FALSE]
  idx <- match(paste(cohort$subject_id, as.character(cohort[[key_date]])), paste(d[[person]], as.character(d[[date_col]])))
  value_cols <- setdiff(names(d), c(person, date_col, "cohort_definition_id", "site", "site_anon"))
  if (!is.null(only)) value_cols <- intersect(value_cols, only)
  res <- d[idx, value_cols, drop = FALSE]
  rownames(res) <- NULL
  if (nzchar(prefix)) names(res) <- paste0(prefix, names(res))
  res
}

#' Leakage-free benchmark feature table in one call
#'
#' Chains the Elixhauser, prior-utilization and medication-class extractors and adds index-admission vital means,
#' lab means **and** latest in-stay values (`lab_<name>`, `lab_<name>_last`), CKD-EPI 2021 eGFR, anion gap and the
#' discharge disposition. Same function as `omop_etl.extract_benchmark_features`.
#'
#' @param con DuckDB connection to an OMOP CDM.
#' @param cohort_table table/view with `subject_id` (or `person_id`), `cohort_start_date`, `cohort_end_date`; optional
#'   `visit_occurrence_id`, `outcome_flag`, `discharged_to_concept_id`, `site` / `site_anon`.
#' @param cohort_id optional `cohort_definition_id` filter.
#' @param include_demographics,include_sdoh,include_elixhauser,include_medications,include_vitals,include_labs,include_utilization,include_disposition
#'   feature groups. `include_sdoh` needs `ruca_crosswalk`, `sdi_table` and frozen `sdi_cut_points`.
#' @param lookback_days history window for Elixhauser (back from discharge), utilization and medications.
#' @param lab_lookback_days days before admission included in the vital/lab window (default 0: the stay only).
#'
#' @param medication_specs drug class specs (default `DEFAULT_MEDICATION_CONCEPTS`, 8 classes)
#' @param medication_window `"pre_admission"` (default; the `lookback_days` before the admission), `"pre_discharge"` (the
#'   `lookback_days` up to and including the discharge date, as the eDERRI protocol does), `"inpatient"` or `"discharge"`.
#'   Default `NULL`: `"pre_discharge"` for `medication_set = "ederri"`, else `"pre_admission"`.
#' @param lab_loincs,vital_loincs `list(name = c(loinc, ...))` (defaults: `BENCHMARK_LAB_LOINCS`, `BENCHMARK_VITAL_LOINCS`).
#' @param lab_concept_ids,vital_concept_ids `list(name = c(concept_id))` for databases without the LOINC vocabulary.
#' @param derive_labs add `lab_egfr` and fill `lab_anion_gap` where none was measured (mean and `_last`).
#' @param bounds `"default"` (`DEFAULT_BOUNDS`), a named list `list(name = c(min, max))` overriding those names (`NA` leaves a
#'   side open) or `NULL` for no bounds. A single vital / lab measurement outside its bounds is dropped before the stay's mean and
#'   latest value are taken; WBC counts per uL are rescaled first (`DEFAULT_RESCALE`, off with `NULL`).
#' @param profile `NULL` (generic definitions), `"ederri"` (the eDERRI protocol's: its ATC medication classes over
#'   [discharge - `lookback_days`, discharge], its Elixhauser ICD prefix lists over all history up to discharge, hemoglobin from
#'   hematocrit / 3 and its history columns) or `"both"` (the generic columns plus the protocol's Elixhauser flags and medication
#'   classes as `ederri_elix_*` / `ederri_med_*`, and its history columns). Any single choice can be set with the arguments
#'   below; an explicit argument wins over the profile.
#' @param hematocrit_fallback fill a missing hemoglobin with hematocrit / 3. Default: `TRUE` for `profile = "ederri"`.
#' @param elixhauser_lookback_days history window of the Elixhauser flags; `"lookback_days"` (default) uses `lookback_days`,
#'   `NULL` is all history (what `profile = "ederri"` uses).
#' @param elixhauser_code_table `"quan"`, `"ederri"` or `"both"` (the protocol's flags as `ederri_elix_*`); default from `profile`.
#' @param medication_set `"generic"`, `"ederri"` or `"both"` (the protocol's classes as `ederri_med_*`); default from `profile`.
#' @param include_protocol_history add [extract_protocol_history_features()] columns; default `TRUE` for `profile = "ederri"` / `"both"`.
#' @param ruca_crosswalk,sdi_table,sdi_cut_points inputs of [derive_geography_features()].
#' @param disposition_map named character vector concept id -> category.
#' @param sdoh_fallback `"indicator"` (default): when `ruca_crosswalk`, `sdi_table` or `sdi_cut_points` is missing, `include_sdoh = TRUE`
#'   gives `sdi_quintile` / `ruca_category` = NA plus `sdoh_missing = 1` and a warning; `"raise"` keeps the error. No national cut points are bundled.
#' @param address_table dated address history for `include_sdoh` (columns `person_id`, `zip5`, `address_start`, `address_end`,
#'   `preferred`): `"auto"` (default) uses `person_address_history` when present, else `person.location_id`; or a table name (for
#'   example a study's own `ederri_address_history`); `NULL` for `person.location_id`.
#' @param address_undated rank of an address with no start date (`undated` of `derive_geography_features()`).
#' @param address_tie_break how `derive_geography_features()` resolves addresses that tie on every rule key (`tie_break`).
#' @param impute `NULL`, `"median"` or `"mean"`: fill missing vital / lab values. Statistics come from this table unless `impute_values`
#'   (named numeric) is given; fit them on the training sites (`attr(, "imputation_values")`) and pass them to other tables.
#' @param impute_values named numeric vector of frozen fill values.
#' @return A data frame in cohort order; `attr(, "feature_groups")` lists the columns per group and `attr(, "skipped")` the
#'   features that could not be built.
#' @export
extract_benchmark_features <- function(con, cohort_table = "cohort", cohort_id = NULL, include_demographics = TRUE,
                                       include_sdoh = FALSE, include_elixhauser = TRUE, include_medications = TRUE,
                                       include_vitals = TRUE, include_labs = TRUE, include_utilization = TRUE,
                                       include_disposition = TRUE, lookback_days = 365, lab_lookback_days = 0,
                                       medication_specs = NULL, medication_window = NULL, lab_loincs = NULL,
                                       vital_loincs = NULL, lab_concept_ids = NULL, vital_concept_ids = NULL,
                                       derive_labs = TRUE, bounds = "default", profile = NULL, hematocrit_fallback = NULL,
                                       elixhauser_lookback_days = "lookback_days", elixhauser_code_table = NULL,
                                       medication_set = NULL, include_protocol_history = NULL,
                                       ruca_crosswalk = NULL, sdi_table = NULL, sdi_cut_points = NULL,
                                       disposition_map = NULL, sdoh_fallback = "indicator", address_table = "auto", address_tie_break = "deterministic", address_undated = "last", impute = NULL,
                                       impute_values = NULL) {
  if (lookback_days < 0 || lab_lookback_days < 0) stop("lookback_days and lab_lookback_days must be non-negative.", call. = FALSE)
  if (!is.null(profile) && !profile %in% c("ederri", "both")) stop("profile must be 'ederri', 'both' or NULL.", call. = FALSE)
  prof <- if (is.null(profile)) "none" else profile
  if (is.null(medication_set)) medication_set <- switch(prof, ederri = "ederri", both = "both", "generic")
  if (!medication_set %in% c("generic", "ederri", "both")) stop("medication_set must be 'generic', 'ederri' or 'both'.", call. = FALSE)
  if (is.null(medication_window)) medication_window <- if (medication_set == "ederri") "pre_discharge" else "pre_admission"
  if (is.null(hematocrit_fallback)) hematocrit_fallback <- identical(profile, "ederri")
  if (is.null(elixhauser_code_table)) elixhauser_code_table <- switch(prof, ederri = "ederri", both = "both", "quan")
  if (!elixhauser_code_table %in% c("quan", "ederri", "both")) stop("elixhauser_code_table must be 'quan', 'ederri' or 'both'.", call. = FALSE)
  if (!identical(elixhauser_lookback_days, "lookback_days") && !is.null(elixhauser_lookback_days) && elixhauser_lookback_days < 0) {
    stop("elixhauser_lookback_days must be non-negative or NULL.", call. = FALSE)
  }
  if (is.null(include_protocol_history)) include_protocol_history <- prof %in% c("ederri", "both")
  if (is.character(bounds)) {
    if (!identical(bounds, "default")) stop("bounds must be 'default', a named list(name = c(min, max)) or NULL.", call. = FALSE)
    bounds <- DEFAULT_BOUNDS
  } else if (!is.null(bounds)) {
    bounds <- utils::modifyList(DEFAULT_BOUNDS, lapply(bounds, as.numeric))
  }
  rescale <- if (!is.null(bounds)) DEFAULT_RESCALE else NULL
  cohort <- .bf_cohort(con, cohort_table, cohort_id)
  n <- nrow(cohort)
  out <- data.frame(person_id = cohort$subject_id)
  if ("visit_occurrence_id" %in% names(cohort)) out$visit_occurrence_id <- cohort$visit_occurrence_id
  out$index_date <- cohort$cohort_start_date
  out$discharge_date <- cohort$cohort_end_date
  if ("outcome_flag" %in% names(cohort)) out$y <- cohort$outcome_flag
  for (s in c("site", "site_anon")) if (s %in% names(cohort)) out[[s]] <- cohort[[s]]
  keys <- names(out)
  groups <- list(); skipped <- character()
  add <- function(group, frame) {
    for (cc in names(frame)) out[[cc]] <<- frame[[cc]]
    groups[[group]] <<- c(groups[[group]], names(frame))
  }

  age <- rep(NA_real_, n); female <- rep(NA_real_, n)
  if (include_demographics || (include_labs && derive_labs)) {
    duckdb::duckdb_register(con, "_bf_c", cohort[, c("row_id", "subject_id", "cohort_start_date")])
    demo <- tryCatch(DBI::dbGetQuery(con, "
      SELECT c.row_id, date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)),
                                 CAST(c.cohort_start_date AS DATE)) AS age_at_index,
             p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id
      FROM _bf_c c LEFT JOIN person p ON p.person_id = c.subject_id ORDER BY c.row_id"),
      finally = duckdb::duckdb_unregister(con, "_bf_c"))
    age <- as.numeric(demo$age_at_index)
    female <- ifelse(is.na(demo$gender_concept_id), NA_real_, as.numeric(demo$gender_concept_id == 8532))
    if (include_demographics) {
      add("demographics", data.frame(age_at_index = age, female = female, race_concept_id = demo$race_concept_id,
                                     ethnicity_concept_id = demo$ethnicity_concept_id))
    }
  }

  view <- "_bf_cohort_view"
  duckdb::duckdb_register(con, view, cohort[, setdiff(names(cohort), "row_id")])
  on.exit(duckdb::duckdb_unregister(con, view), add = TRUE)
  if (include_elixhauser) {
    tables <- if (elixhauser_code_table == "both") c("quan", "ederri") else elixhauser_code_table
    for (tb in tables) {
      lb <- if (identical(elixhauser_lookback_days, "lookback_days")) { if (tb == "ederri") NULL else lookback_days } else elixhauser_lookback_days
      el <- suppressWarnings(extract_elixhauser_comorbidities(con, view, person_col = "subject_id", index_date_col = "cohort_end_date",
                                                              lookback_days = lb, exclude_index_diagnoses = FALSE,
                                                              end_inclusive = TRUE, code_table = tb,
                                                              source_and_standard = tb != "ederri"))
      prefix <- if (elixhauser_code_table == "both" && tb == "ederri") "ederri_" else ""
      add(if (nzchar(prefix)) "elixhauser_ederri" else "elixhauser", .bf_align(cohort, el, prefix = prefix))
    }
  }
  if (include_utilization) {
    ut <- extract_prior_utilization(con, view, lookback_days = lookback_days)
    add("utilization", .bf_align(cohort, ut))
  }
  if (include_medications) {
    runs <- list()
    if (medication_set %in% c("generic", "both")) {
      runs[[length(runs) + 1L]] <- list(specs = if (!is.null(medication_specs)) medication_specs else .bf_default_med_specs(),
                                        window = medication_window, prefix = "")
    }
    if (medication_set %in% c("ederri", "both")) {
      runs[[length(runs) + 1L]] <- if (medication_set == "ederri" && !is.null(medication_specs)) {
        list(specs = medication_specs, window = medication_window, prefix = "")
      } else {
        list(specs = EDERRI_MEDICATION_SPECS, window = if (medication_set == "both") "pre_discharge" else medication_window,
             prefix = if (medication_set == "both") "ederri_" else "")
      }
    }
    for (r in runs) {
      win_args <- if (identical(r$window, "pre_discharge")) list(window = NULL, offset_anchor = "visit_end_date", lookahead_days = 0L) else list(window = r$window)
      med <- suppressWarnings(do.call(extract_medication_classes, c(list(con, view, r$specs, strategy = "standard", person_col = "subject_id",
                                                                         index_date_col = "cohort_start_date", end_date_col = "cohort_end_date",
                                                                         lookback_days = lookback_days, on_unresolved = "ignore"), win_args)))
      add(if (nzchar(r$prefix)) "medications_ederri" else "medications",
          .bf_align(cohort, med, prefix = paste0(r$prefix, "med_"), only = names(r$specs)))
    }
  }
  if (include_protocol_history) {
    ph <- tryCatch(extract_protocol_history_features(con, cohort, lookback_days = lookback_days), error = function(e) {
      if (grepl("Catalog Error|does not exist", conditionMessage(e))) {
        warning("protocol history columns skipped: ", strsplit(conditionMessage(e), "\n")[[1]][1], call. = FALSE)
        NULL
      } else stop(e)
    })
    if (is.null(ph)) skipped <- c(skipped, "protocol_history") else add("protocol_history", ph)
  }

  if (include_vitals) {
    vl <- if (!is.null(vital_loincs)) vital_loincs else BENCHMARK_VITAL_LOINCS
    vmap <- .bf_concept_map(con, vl, vital_concept_ids)
    stats <- .bf_measurement_stats(con, cohort, vmap, lab_lookback_days, bounds, rescale)
    present <- intersect(names(vl), unique(vmap$name))
    skipped <- c(skipped, paste0("vital_", setdiff(names(vl), present)))
    add("vitals", .bf_wide(stats, "vital_", present, n, with_last = FALSE))
  }
  if (include_labs) {
    ll <- if (!is.null(lab_loincs)) lab_loincs else BENCHMARK_LAB_LOINCS
    lmap <- .bf_concept_map(con, ll, lab_concept_ids)
    stats <- .bf_measurement_stats(con, cohort, lmap, lab_lookback_days, bounds, rescale)
    present <- intersect(names(ll), unique(lmap$name))
    skipped <- c(skipped, paste0("lab_", setdiff(names(ll), present)))
    labs <- .bf_wide(stats, "lab_", present, n, with_last = TRUE)
    if (derive_labs) {
      for (sfx in c("", "_last")) {
        cr <- paste0("lab_creatinine", sfx)
        if (cr %in% names(labs)) {
          ok <- !is.na(labs[[cr]]) & !is.na(age) & !is.na(female)
          eg <- rep(NA_real_, n)
          if (any(ok)) eg[ok] <- compute_ckd_epi_2021(labs[[cr]][ok], age[ok], female[ok] == 1)
          labs[[paste0("lab_egfr", sfx)]] <- eg
        }
        parts <- paste0("lab_", c("sodium", "chloride", "bicarbonate"), sfx)
        if (all(parts %in% names(labs))) {
          calc <- labs[[parts[[1]]]] - labs[[parts[[2]]]] - labs[[parts[[3]]]]
          ag <- paste0("lab_anion_gap", sfx)
          labs[[ag]] <- if (ag %in% names(labs)) ifelse(is.na(labs[[ag]]), calc, labs[[ag]]) else calc
        }
        hct <- paste0("lab_hematocrit", sfx)
        if (isTRUE(hematocrit_fallback) && hct %in% names(labs)) {
          hg <- paste0("lab_hemoglobin", sfx)
          fill <- labs[[hct]] / 3
          labs[[hg]] <- if (hg %in% names(labs)) ifelse(is.na(labs[[hg]]), fill, labs[[hg]]) else fill
        }
      }
    }
    add("labs", labs)
  }

  if (include_disposition) {
    disp <- if ("discharged_to_concept_id" %in% names(cohort)) cohort$discharged_to_concept_id else if ("visit_occurrence_id" %in% names(cohort)) {
      v <- DBI::dbGetQuery(con, "SELECT visit_occurrence_id, discharged_to_concept_id FROM visit_occurrence")
      v$discharged_to_concept_id[match(cohort$visit_occurrence_id, v$visit_occurrence_id)]
    } else NULL
    mapping <- if (is.null(disposition_map)) DEFAULT_DISPOSITION_MAP else disposition_map
    if (is.null(disp)) {
      skipped <- c(skipped, "disposition")
    } else {
      cat <- ifelse(is.na(disp), "missing", ifelse(as.character(disp) %in% names(mapping), unname(mapping[as.character(disp)]), "other"))
      levels <- unique(c("home", "home_health", "snf", "ama", "other", "missing", sort(setdiff(unname(mapping), c("home", "home_health", "snf", "ama")))))
      add("disposition", as.data.frame(stats::setNames(lapply(levels, function(lv) as.integer(cat == lv)), paste0("disp_", levels))))
    }
  }

  if (include_sdoh) {
    if (!sdoh_fallback %in% c("indicator", "raise")) stop("sdoh_fallback must be 'indicator' or 'raise'.", call. = FALSE)
    if (is.null(ruca_crosswalk) || is.null(sdi_table) || is.null(sdi_cut_points)) {
      if (sdoh_fallback == "raise") stop("include_sdoh = TRUE needs ruca_crosswalk, sdi_table and the frozen sdi_cut_points.", call. = FALSE)
      warning("include_sdoh = TRUE but ruca_crosswalk, sdi_table or sdi_cut_points is missing: sdi_quintile and ruca_category are NA and sdoh_missing = 1 for every row.", call. = FALSE)
      add("sdoh", data.frame(sdi_quintile = rep(NA_real_, n), ruca_category = rep(NA_character_, n), sdoh_missing = rep(1L, n)))
    } else {
      stays <- data.frame(person_id = cohort$subject_id, stay_id = cohort$row_id, admit_date = cohort$cohort_start_date)
      geo <- derive_geography_features(con, ruca_crosswalk, sdi_table, sdi_cut_points, stays = stays, address_table = address_table,
                                        tie_break = address_tie_break, undated = address_undated)
      if (all(is.na(geo$zip5))) warning("include_sdoh: no ZIP code was found for any stay, so sdi_quintile and ruca_category are all missing. ",
                                        "The default address_table = 'auto' reads person_address_history, else person.location_id; pass ",
                                        "address_table = the name of your dated address table.", call. = FALSE)
      geo <- geo[!duplicated(geo$stay_id), , drop = FALSE]
      idx <- match(cohort$row_id, geo$stay_id)
      add("sdoh", data.frame(sdi_quintile = geo$sdi_quintile[idx], ruca_category = geo$ruca_category[idx],
                             sdoh_missing = as.integer(is.na(geo$sdi_quintile[idx]) | geo$sdi_quintile[idx] == "SDI: Missing / Unknown")))
    }
  }

  imputation <- numeric()
  if (!is.null(impute) || !is.null(impute_values)) {
    method <- if (is.null(impute)) "median" else tolower(impute)
    if (!method %in% c("median", "mean")) stop("impute must be 'median', 'mean' or NULL.", call. = FALSE)
    for (cc in c(groups$vitals, groups$labs)) {
      val <- if (!is.null(impute_values) && cc %in% names(impute_values)) as.numeric(impute_values[[cc]]) else
        if (any(!is.na(out[[cc]]))) as.numeric(if (method == "median") stats::median(out[[cc]], na.rm = TRUE) else mean(out[[cc]], na.rm = TRUE)) else NA_real_
      imputation[[cc]] <- val
      if (!is.na(val)) out[[cc]][is.na(out[[cc]])] <- val
    }
  }

  leaked <- setdiff(names(out)[tolower(names(out)) %in% c("y", "outcome", "outcome_flag", "outcome_date")], keys)
  if (length(leaked)) stop("Outcome-like columns in the feature table: ", paste(leaked, collapse = ", "), call. = FALSE)
  attr(out, "feature_groups") <- groups
  attr(out, "skipped") <- skipped
  attr(out, "key_columns") <- keys
  attr(out, "imputation_values") <- imputation
  out
}
