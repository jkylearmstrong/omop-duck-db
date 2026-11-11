# One-call, leakage-free tabular feature table for the readmission benchmark (v0.13.0).
# Mirror of python/omop_etl/benchmark_features.py. History (utilization, medications) ends before the admission,
# Elixhauser is anchored at discharge and includes the index stay's diagnoses, vitals/labs use measurements dated
# in [admission - lab_lookback_days, discharge] only.

#' Discharge disposition concept -> benchmark category
#'
#' 8536 Home, 581476 Home Health, 8863 SNF, 32216 AMA (as written by the PCORnet ETL); anything else is "other".
#' @export
DEFAULT_DISPOSITION_MAP <- c(`8536` = "home", `581476` = "home_health", `8863` = "snf", `32216` = "ama")

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

.bf_measurement_stats <- function(con, cohort, cmap, pre_days) {
  if (!nrow(cmap)) return(data.frame(row_id = integer(), name = character(), mean = numeric(), last = numeric(), n = integer()))
  duckdb::duckdb_register(con, "_bf_c", cohort[, c("row_id", "subject_id", "cohort_start_date", "cohort_end_date")])
  duckdb::duckdb_register(con, "_bf_map", cmap)
  on.exit({ duckdb::duckdb_unregister(con, "_bf_c"); duckdb::duckdb_unregister(con, "_bf_map") }, add = TRUE)
  DBI::dbGetQuery(con, sprintf("
    SELECT c.row_id, f.name, avg(m.value_as_number) AS mean,
           arg_max(m.value_as_number, struct_pack(t := COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)),
                                                  id := m.measurement_id)) AS last,
           count(*) AS n
    FROM _bf_c c
    JOIN measurement m ON m.person_id = c.subject_id
         AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - %d
         AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)
    JOIN _bf_map f ON f.concept_id = m.measurement_concept_id
    WHERE m.value_as_number IS NOT NULL
    GROUP BY c.row_id, f.name", as.integer(pre_days)))
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
#' @param medication_window frame, default `"pre_admission"`.
#' @param lab_loincs,vital_loincs `list(name = c(loinc, ...))` (defaults: core-14 panel; `DEFAULT_VITAL_LOINCS`).
#' @param lab_concept_ids,vital_concept_ids `list(name = c(concept_id))` for databases without the LOINC vocabulary.
#' @param derive_labs add `lab_egfr` and `lab_anion_gap` (mean and `_last`).
#' @param ruca_crosswalk,sdi_table,sdi_cut_points inputs of [derive_geography_features()].
#' @param disposition_map named character vector concept id -> category.
#' @return A data frame in cohort order; `attr(, "feature_groups")` lists the columns per group and `attr(, "skipped")` the
#'   features that could not be built.
#' @export
extract_benchmark_features <- function(con, cohort_table = "cohort", cohort_id = NULL, include_demographics = TRUE,
                                       include_sdoh = FALSE, include_elixhauser = TRUE, include_medications = TRUE,
                                       include_vitals = TRUE, include_labs = TRUE, include_utilization = TRUE,
                                       include_disposition = TRUE, lookback_days = 365, lab_lookback_days = 0,
                                       medication_specs = NULL, medication_window = "pre_admission", lab_loincs = NULL,
                                       vital_loincs = NULL, lab_concept_ids = NULL, vital_concept_ids = NULL,
                                       derive_labs = TRUE, ruca_crosswalk = NULL, sdi_table = NULL, sdi_cut_points = NULL,
                                       disposition_map = NULL) {
  if (lookback_days < 0 || lab_lookback_days < 0) stop("lookback_days and lab_lookback_days must be non-negative.", call. = FALSE)
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
    el <- suppressWarnings(extract_elixhauser_comorbidities(con, view, person_col = "subject_id", index_date_col = "cohort_end_date",
                                                            lookback_days = lookback_days, exclude_index_diagnoses = FALSE,
                                                            end_inclusive = TRUE))
    add("elixhauser", .bf_align(cohort, el))
  }
  if (include_utilization) {
    ut <- extract_prior_utilization(con, view, lookback_days = lookback_days)
    add("utilization", .bf_align(cohort, ut))
  }
  if (include_medications) {
    specs <- if (!is.null(medication_specs)) medication_specs else .bf_default_med_specs()
    med <- suppressWarnings(extract_medication_classes(con, view, specs, strategy = "standard", person_col = "subject_id",
                                                       index_date_col = "cohort_start_date", end_date_col = "cohort_end_date",
                                                       lookback_days = lookback_days, window = medication_window,
                                                       on_unresolved = "ignore"))
    add("medications", .bf_align(cohort, med, prefix = "med_", only = names(specs)))
  }

  if (include_vitals) {
    vl <- if (!is.null(vital_loincs)) vital_loincs else DEFAULT_VITAL_LOINCS
    vmap <- .bf_concept_map(con, vl, vital_concept_ids)
    stats <- .bf_measurement_stats(con, cohort, vmap, lab_lookback_days)
    present <- intersect(names(vl), unique(vmap$name))
    skipped <- c(skipped, paste0("vital_", setdiff(names(vl), present)))
    add("vitals", .bf_wide(stats, "vital_", present, n, with_last = FALSE))
  }
  if (include_labs) {
    ll <- if (!is.null(lab_loincs)) lab_loincs else CORE_14_LAB_PANEL
    lmap <- .bf_concept_map(con, ll, lab_concept_ids)
    stats <- .bf_measurement_stats(con, cohort, lmap, lab_lookback_days)
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
        if (all(parts %in% names(labs))) labs[[paste0("lab_anion_gap", sfx)]] <- labs[[parts[[1]]]] - labs[[parts[[2]]]] - labs[[parts[[3]]]]
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
    if (is.null(ruca_crosswalk) || is.null(sdi_table) || is.null(sdi_cut_points)) {
      stop("include_sdoh = TRUE needs ruca_crosswalk, sdi_table and the frozen sdi_cut_points.", call. = FALSE)
    }
    stays <- data.frame(person_id = cohort$subject_id, stay_id = cohort$row_id, admit_date = cohort$cohort_start_date)
    geo <- derive_geography_features(con, ruca_crosswalk, sdi_table, sdi_cut_points, stays = stays)
    geo <- geo[!duplicated(geo$stay_id), , drop = FALSE]
    idx <- match(cohort$row_id, geo$stay_id)
    add("sdoh", data.frame(sdi_quintile = geo$sdi_quintile[idx], ruca_category = geo$ruca_category[idx]))
  }

  leaked <- setdiff(names(out)[tolower(names(out)) %in% c("y", "outcome", "outcome_flag", "outcome_date")], keys)
  if (length(leaked)) stop("Outcome-like columns in the feature table: ", paste(leaked, collapse = ", "), call. = FALSE)
  attr(out, "feature_groups") <- groups
  attr(out, "skipped") <- skipped
  attr(out, "key_columns") <- keys
  out
}
