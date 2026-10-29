# Reconcile a reference patient list with a cohort built here, and say why patients are missing (the R twin of
# python/omop_etl/cohort_reconcile.py; same reasons, same counts, same tests).

# the reasons, in the order they are checked
GAP_REASONS <- c("in_cohort", "not_in_cdm", "no_observation_period", "no_entry_event", "failed_entry_correlated_criteria",
                 "entry_outside_observation_window", "failed_additional_criteria", "failed_inclusion_rule", "in_definition_not_in_cohort",
                 "not_in_cohort_no_definition_given")

.recon_table <- function(con, x) {
  if (is.data.frame(x)) return(x)
  p <- as.character(x)
  if (grepl("\\.parquet$", p)) return(DBI::dbGetQuery(con, sprintf("SELECT * FROM read_parquet('%s')", p)))
  if (grepl("\\.(csv|tsv|csv\\.gz)$", p) || file.exists(p)) return(DBI::dbGetQuery(con, sprintf("SELECT * FROM read_csv_auto('%s')", p)))
  DBI::dbGetQuery(con, sprintf("SELECT * FROM %s", p))
}

.recon_definition <- function(d) {
  if (is.list(d)) return(d)
  jsonlite::fromJSON(d, simplifyVector = FALSE)   # a path or JSON text
}

.recon_persons <- function(con, data, stage, cdm_schema) {
  sql <- suppressWarnings(compile_circe_to_duckdb(data, cdm_schema = cdm_schema, stage = stage))
  unique(as.numeric(DBI::dbGetQuery(con, sprintf("SELECT DISTINCT subject_id FROM (%s)", sql))$subject_id))
}

.recon_strip_correlated <- function(data) {
  for (i in seq_along(data$PrimaryCriteria$CriteriaList)) {
    crit <- data$PrimaryCriteria$CriteriaList[[i]]
    for (k in names(crit)) if (is.list(crit[[k]])) data$PrimaryCriteria$CriteriaList[[i]][[k]]$CorrelatedCriteria <- NULL
  }
  data
}

# one row per reference row: ref_id (text), ref_date, grp, person_id (NA when it maps to nobody)
.recon_map_reference <- function(con, reference, id_col, id_type, date_col, group_col) {
  ref <- .recon_table(con, reference)
  nm <- stats::setNames(names(ref), tolower(names(ref)))
  if (!(tolower(id_col) %in% names(nm))) stop(sprintf("reference has no column '%s'; columns: %s", id_col, paste(names(ref), collapse = ", ")), call. = FALSE)
  frame <- data.frame(ref_id = trimws(as.character(ref[[nm[[tolower(id_col)]]]])), stringsAsFactors = FALSE)
  frame$ref_date <- if (!is.null(date_col) && tolower(date_col) %in% names(nm)) as.character(as.Date(ref[[nm[[tolower(date_col)]]]])) else NA_character_
  frame$grp <- if (!is.null(group_col) && tolower(group_col) %in% names(nm)) as.character(ref[[nm[[tolower(group_col)]]]]) else "ALL"
  if (!(id_type %in% c("person_source_value", "person_id"))) stop("id_type must be 'person_source_value' or 'person_id'", call. = FALSE)
  join <- if (id_type == "person_id") "p.person_id = TRY_CAST(r.ref_id AS BIGINT)" else "UPPER(TRIM(CAST(p.person_source_value AS VARCHAR))) = UPPER(r.ref_id)"
  duckdb::duckdb_register(con, "_recon_ref", frame)
  on.exit(duckdb::duckdb_unregister(con, "_recon_ref"), add = TRUE)
  out <- DBI::dbGetQuery(con, sprintf("SELECT r.ref_id, r.ref_date, r.grp, p.person_id FROM _recon_ref r LEFT JOIN person p ON %s", join))
  out$person_id <- as.numeric(out$person_id)
  out
}

#' Blank small counts so a report can leave a site
#'
#' Counts from 1 to `min_cell - 1` (and the `also` columns of those rows) become `NA`, and a `suppressed` flag is added. A total shown
#' beside suppressed cells can reveal them by subtraction: suppress the total too, or merge the small reasons, when that matters.
#'
#' @param df A data frame.
#' @param count_cols Names of the count columns.
#' @param min_cell Smallest count that may be shown (0 or 1 suppresses nothing).
#' @param also Further columns to blank in the suppressed rows, such as a percentage.
#' @return `df` with a `suppressed` column.
#' @export
suppress_small_cells <- function(df, count_cols, min_cell, also = NULL) {
  out <- df
  out$suppressed <- FALSE
  if (!is.null(min_cell) && min_cell > 1) {
    mask <- Reduce(`|`, lapply(count_cols, function(c) !is.na(out[[c]]) & out[[c]] > 0 & out[[c]] < min_cell))
    for (c in c(count_cols, also)) { out[[c]] <- as.numeric(out[[c]]); out[[c]][mask] <- NA_real_ }
    out$suppressed <- mask
  }
  out
}

#' Why is a reference patient not in the cohort?
#'
#' Gives every patient of a reference list the first step that keeps them out of a cohort, by walking a Circe definition step by step
#' (entry event, correlated criteria, observation window, each inclusion rule) with the native compiler.
#'
#' Reasons, in the order they are checked: `in_cohort`; `not_in_cdm` (the identifier maps to no person); `no_observation_period`;
#' `no_entry_event` (no event of the entry criteria, including their concept sets, age and values); `failed_entry_correlated_criteria`;
#' `entry_outside_observation_window`; `failed_additional_criteria`; `failed_inclusion_rule` (one row per rule, named in `detail`); `in_definition_not_in_cohort` (the
#' definition keeps them but the cohort table does not); `not_in_cohort_no_definition_given`. The report is counts by reason; patient-level
#' rows are returned only with `include_person_rows = TRUE` and stay on the machine that ran the function.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param reference A data frame, CSV / parquet path or table name with one identifier column.
#' @param definition A Circe definition (list, JSON text or path) the native compiler can evaluate.
#' @param cohort A table name or data frame with `subject_id` (or `person_id`); optional when `definition` is given.
#' @param id_col Name of the identifier column in `reference`.
#' @param id_type `"person_source_value"` or `"person_id"`.
#' @param group_col Optional column of `reference` kept in the person rows.
#' @param cdm_schema Schema of the CDM tables.
#' @param min_cell Suppress counts from 1 to `min_cell - 1` (see [suppress_small_cells()]).
#' @param include_person_rows Return one row per reference patient with its reason.
#' @return A list with `gaps` (`reason`, `detail`, `n`, `pct`, `suppressed`), `n_reference` and `persons` (or `NULL`).
#' @export
explain_cohort_gaps <- function(con, reference, definition = NULL, cohort = NULL, id_col = "person_source_value",
                                id_type = "person_source_value", group_col = NULL, cdm_schema = "main", min_cell = 0,
                                include_person_rows = FALSE) {
  if (is.null(definition) && is.null(cohort)) stop("give a Circe definition, a cohort table, or both", call. = FALSE)
  res <- .recon_explain(con, reference, definition, cohort, id_col, id_type, group_col, cdm_schema, min_cell, include_person_rows)
  res[c("gaps", "n_reference", "persons")]
}

.recon_explain <- function(con, reference, definition, cohort, id_col, id_type, group_col, cdm_schema, min_cell, include_person_rows) {
  data <- if (!is.null(definition)) .recon_definition(definition) else NULL
  mapped <- .recon_map_reference(con, reference, id_col, id_type, NULL, group_col)

  steps <- list()
  if (!is.null(data)) {
    no_rules <- data; no_rules$InclusionRules <- list()
    steps[[1]] <- list("no_entry_event", "", .recon_persons(con, { x <- .recon_strip_correlated(data); x$InclusionRules <- list(); x }, "_primary_events_raw", cdm_schema))
    steps[[2]] <- list("failed_entry_correlated_criteria", "", .recon_persons(con, no_rules, "_primary_events_raw", cdm_schema))
    steps[[3]] <- list("entry_outside_observation_window", "", .recon_persons(con, no_rules, "_primary_limited", cdm_schema))
    if (!.circe_empty(data$AdditionalCriteria)) steps[[length(steps) + 1L]] <- list("failed_additional_criteria", "", .recon_persons(con, no_rules, "_primary_qualified", cdm_schema))
    rules <- data$InclusionRules %||% list()
    for (k in seq_along(rules)) {
      upto <- data; upto$InclusionRules <- rules[seq_len(k)]
      nm <- rules[[k]]$name %||% sprintf("rule %d", k)
      steps[[length(steps) + 1L]] <- list("failed_inclusion_rule", as.character(nm), .recon_persons(con, upto, "_primary_qualified", cdm_schema))
    }
  }
  if (!is.null(cohort)) {
    cdf <- .recon_table(con, cohort)
    pcol <- if ("subject_id" %in% names(cdf)) "subject_id" else "person_id"
    in_cohort <- unique(as.numeric(cdf[[pcol]][!is.na(cdf[[pcol]])]))
  } else {
    in_cohort <- .recon_persons(con, data, "_primary_qualified", cdm_schema)
  }
  observed <- unique(as.numeric(DBI::dbGetQuery(con, "SELECT DISTINCT person_id FROM observation_period")$person_id))

  classify <- function(pid) {
    if (is.na(pid)) return(c("not_in_cdm", ""))
    if (!(pid %in% observed)) return(c("no_observation_period", ""))
    if (pid %in% in_cohort) return(c("in_cohort", ""))
    if (is.null(data)) return(c("not_in_cohort_no_definition_given", ""))
    for (s in steps) if (!(pid %in% s[[3]])) return(c(s[[1]], s[[2]]))
    c("in_definition_not_in_cohort", "")
  }
  verdict <- t(vapply(mapped$person_id, classify, character(2)))
  mapped$reason <- verdict[, 1]
  mapped$detail <- verdict[, 2]
  # one verdict per reference identifier: a patient who maps to several persons counts once, with the earliest reason in GAP_REASONS
  mapped <- mapped[order(match(mapped$reason, GAP_REASONS)), , drop = FALSE]
  per_patient <- mapped[!duplicated(mapped$ref_id), , drop = FALSE]
  n_ref <- nrow(per_patient)
  counts <- stats::aggregate(list(n = rep(1, n_ref)), by = list(reason = per_patient$reason, detail = per_patient$detail), FUN = sum)
  counts <- counts[order(match(counts$reason, GAP_REASONS), counts$detail), , drop = FALSE]
  counts$pct <- if (n_ref) round(100 * counts$n / n_ref, 2) else 0
  rownames(counts) <- NULL
  gaps <- suppress_small_cells(counts, "n", min_cell, also = "pct")
  persons <- if (include_person_rows) {
    p <- per_patient; names(p)[names(p) == "grp"] <- "group"; rownames(p) <- NULL; p
  } else NULL
  list(gaps = gaps, n_reference = n_ref, persons = persons, in_cohort = in_cohort, mapped = mapped)
}

#' Compare a reference patient list with a cohort built here and explain the patients we miss
#'
#' Returns the patient-level overlap (`counts`: `n_reference`, `n_mapped_to_cdm`, `n_cohort`, `both`, `reference_only`, `cohort_only`,
#' `jaccard`), the reasons for the missing patients (`gaps`, see [explain_cohort_gaps()]), and, when `index_date_col` names a date in the
#' reference, how many matched patients have the same index date and how far apart the others are (`dates`; the nearest
#' `cohort_start_date` is used when the cohort has several). Counts only unless `include_person_rows = TRUE`.
#'
#' @inheritParams explain_cohort_gaps
#' @param index_date_col Optional name of a date column in `reference`.
#' @return A list with `counts`, `gaps`, `dates` (or `NULL`) and `persons` (or `NULL`).
#' @examples
#' \dontrun{
#' # a legacy extraction's patient list (one identifier per row, optionally its index date) against a Circe definition
#' con <- DBI::dbConnect(duckdb::duckdb(), "cdm.duckdb", read_only = TRUE)
#' out <- reconcile_cohort(con, "legacy_patients.csv", definition = file.path(cohort_definitions_dir(), "target_cohort.circe.json"),
#'                         id_col = "patient_id", index_date_col = "admit_date", min_cell = 11)
#' out$counts   # both, reference_only, cohort_only, jaccard
#' out$gaps     # why the reference patients we miss are missing, as counts by step
#' out$dates    # how many matched patients share the index date
#' }
#' @export
reconcile_cohort <- function(con, reference, definition = NULL, cohort = NULL, id_col = "person_source_value",
                             id_type = "person_source_value", index_date_col = NULL, group_col = NULL, cdm_schema = "main",
                             min_cell = 0, include_person_rows = FALSE) {
  if (is.null(definition) && is.null(cohort)) stop("give a Circe definition, a cohort table, or both", call. = FALSE)
  res <- .recon_explain(con, reference, definition, cohort, id_col, id_type, group_col, cdm_schema, min_cell, include_person_rows)
  mapped <- res$mapped; in_cohort <- res$in_cohort
  ref_persons <- unique(mapped$person_id[!is.na(mapped$person_id)])
  n_ref <- res$n_reference
  found <- unique(mapped$ref_id[mapped$person_id %in% in_cohort])
  both <- length(found)
  cohort_only <- length(setdiff(in_cohort, ref_persons))
  union <- n_ref + cohort_only
  counts <- list(n_reference = n_ref, n_mapped_to_cdm = length(unique(mapped$ref_id[!is.na(mapped$person_id)])), n_cohort = length(in_cohort),
                 both = both, reference_only = n_ref - both, cohort_only = cohort_only, jaccard = if (union) round(both / union, 4) else 1)
  dates <- NULL
  if (!is.null(index_date_col)) {
    rd <- .recon_map_reference(con, reference, id_col, id_type, index_date_col, group_col)
    rd <- rd[!is.na(rd$person_id) & !is.na(rd$ref_date), , drop = FALSE]
    cdf <- if (!is.null(cohort)) .recon_table(con, cohort) else {
      DBI::dbGetQuery(con, suppressWarnings(compile_circe_to_duckdb(.recon_definition(definition), cdm_schema = cdm_schema)))
    }
    pcol <- if ("subject_id" %in% names(cdf)) "subject_id" else "person_id"
    if ("cohort_start_date" %in% names(cdf)) {
      starts <- split(as.Date(cdf$cohort_start_date), as.numeric(cdf[[pcol]]))
      diffs <- numeric(0)
      for (i in seq_len(nrow(rd))) {
        s <- starts[[as.character(rd$person_id[[i]])]]
        if (length(s)) diffs <- c(diffs, min(abs(as.numeric(s - as.Date(rd$ref_date[[i]])))))
      }
      dates <- list(n_compared = length(diffs), n_same_date = sum(diffs == 0), n_different_date = sum(diffs > 0),
                    median_abs_days = if (any(diffs > 0)) stats::median(diffs[diffs > 0]) else 0,
                    max_abs_days = if (length(diffs)) max(diffs) else 0)
    }
  }
  list(counts = counts, gaps = res$gaps, dates = dates, persons = res$persons)
}
