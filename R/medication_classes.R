#' @keywords internal
#' @noRd
.med_q <- function(x) paste0("'", gsub("'", "''", as.character(x), fixed = TRUE), "'")

#' @keywords internal
#' @noRd
.med_first_present <- function(columns, candidates, what, given = NULL) {
  lower <- tolower(columns)
  if (!is.null(given)) {
    if (!tolower(given) %in% lower) stop(sprintf("%s column '%s' not in %s.", what, given, paste(columns, collapse = ", ")))
    return(columns[match(tolower(given), lower)])
  }
  for (cand in candidates) {
    if (cand %in% lower) return(columns[match(cand, lower)])
  }
  stop(sprintf("Could not find a %s column among %s; pass it explicitly.", what, paste(columns, collapse = ", ")))
}

#' @keywords internal
#' @noRd
.med_resolve_table <- function(con, name) {
  for (ref in c(name, paste0("central_vocab.main.", name), paste0("central_vocab.", name))) {
    hit <- tryCatch(nrow(DBI::dbGetQuery(con, paste0("SELECT 1 FROM ", ref, " LIMIT 1"))) > 0, error = function(e) FALSE)
    if (isTRUE(hit)) return(ref)
  }
  stop(sprintf("Vocabulary table '%s' is not reachable (or empty) on this connection.", name))
}

#' @keywords internal
#' @noRd
.med_normalize_specs <- function(medication_specs) {
  if (length(medication_specs) == 0) stop("medication_specs must define at least one drug class.")
  known <- c("ancestor_concept_ids", "atc_codes", "rxnorm_ingredient_ids", "source_values")
  out <- list()
  for (cls in names(medication_specs)) {
    spec <- medication_specs[[cls]]
    if (is.list(spec)) {
      extra <- setdiff(names(spec), known)
      if (length(extra)) stop(sprintf("Unknown keys %s in spec for '%s'; expected %s.", paste(extra, collapse = ", "), cls, paste(known, collapse = ", ")))
      out[[cls]] <- stats::setNames(lapply(known, function(k) spec[[k]] %||% vector("list", 0)), known)
      out[[cls]] <- lapply(out[[cls]], unlist)
    } else {
      out[[cls]] <- list(ancestor_concept_ids = spec, atc_codes = NULL, rxnorm_ingredient_ids = NULL, source_values = NULL)
    }
  }
  out
}

#' @keywords internal
#' @noRd
.med_load_crosswalk <- function(con, crosswalk) {
  empty <- data.frame(source_value = character(0), drug_class = character(0), stringsAsFactors = FALSE)
  if (is.null(crosswalk)) return(empty)
  df <- if (is.data.frame(crosswalk)) {
    crosswalk
  } else if (grepl("\\.(csv|tsv|txt|csv\\.gz)$", crosswalk, ignore.case = TRUE) || file.exists(crosswalk)) {
    DBI::dbGetQuery(con, paste0("SELECT * FROM ", read_csv_sql(crosswalk, all_varchar = TRUE)))
  } else {
    DBI::dbGetQuery(con, paste0("SELECT * FROM ", crosswalk))
  }
  src <- .med_first_present(names(df), c("source_value", "source_code", "source", "code", "cui", "ndc"), "crosswalk source-value")
  cls <- .med_first_present(names(df), c("drug_class", "medication_class", "class", "class_name"), "crosswalk drug-class")
  out <- data.frame(source_value = toupper(trimws(as.character(df[[src]]))),
                    drug_class = trimws(as.character(df[[cls]])), stringsAsFactors = FALSE)
  out <- out[nzchar(out$source_value) & out$source_value != "NA", , drop = FALSE]
  unique(out)
}

#' Dual-strategy medication-class classifier
#'
#' Classifies each cohort member's medication exposure two independent ways, so sites with very different
#' drug coding (legacy CUI / NDC source strings vs standardized RxNorm) can be compared:
#'
#' * **Strategy A, source / crosswalk** (`"source"`): a user-supplied crosswalk (CSV path, DuckDB table, or
#'   data frame) maps source strings to drug classes. Matched against `drug_source_value` and the
#'   `concept_code` of `drug_source_concept_id`.
#' * **Strategy B, OMOP standard hierarchy** (`"standard"`): `concept_ancestor` traversal from ATC class
#'   concepts (levels 3/4/5, by concept id or ATC code) or RxNorm ingredients down to `drug_concept_id`.
#' * `strategy = "both"` runs both; the combined flag is their OR.
#'
#' Mirror of `omop_etl.medication_classes.extract_medication_classes` in Python.
#'
#' @param db_con DuckDB DBI connection with `drug_exposure` (and `concept_ancestor` / `concept` for the
#'   standard strategy).
#' @param cohort_table Table / view with a person column (`subject_id` / `person_id`) and an index date
#'   (`cohort_start_date` / `index_date`).
#' @param medication_specs Named list `class_name -> spec`. `spec` is a list with any of
#'   `ancestor_concept_ids` (ATC class or RxNorm ingredient concept ids), `atc_codes`, `rxnorm_ingredient_ids`,
#'   `source_values` (inline strategy-A strings); or a bare vector, taken as `ancestor_concept_ids`.
#' @param strategy `"source"`, `"standard"` or `"both"`.
#' @param crosswalk Strategy-A mapping of source strings to classes (data frame, CSV path or table name with
#'   columns like `source_value` / `drug_class`). Combined with inline `source_values`.
#' @param cohort_definition_id Restrict a stacked OMOP cohort table to one definition.
#' @param person_col,index_date_col,site_col Override column auto-detection. A site column
#'   (`site_anon` / `site_id` / `site`) carries through for per-site auditing.
#' @param lookback_days,lookahead_days Exposure window around the index date. `NULL` lookback = unbounded.
#' @param return_audit Also return [audit_medication_strategies()] output (needs `strategy = "both"`).
#' @param ... Forwarded to [audit_medication_strategies()].
#' @return A data frame with `person_id`, `index_date`, optional `site`, and per class `<class>_source`,
#'   `<class>_standard` (0/1, per the chosen strategy) and `<class>` (OR of the available flags). With
#'   `return_audit = TRUE`, a list `list(flags =, audit =)`.
#' @export
extract_medication_classes <- function(db_con, cohort_table, medication_specs, strategy = c("both", "source", "standard"),
                                       crosswalk = NULL, cohort_definition_id = NULL, person_col = NULL,
                                       index_date_col = NULL, site_col = NULL, lookback_days = NULL,
                                       lookahead_days = 0L, return_audit = FALSE, ...) {
  strategy <- match.arg(strategy)
  if (isTRUE(return_audit) && strategy != "both") stop("return_audit = TRUE requires strategy = 'both'.")
  specs <- .med_normalize_specs(medication_specs)
  use_source <- strategy %in% c("source", "both")
  use_standard <- strategy %in% c("standard", "both")

  cols <- DBI::dbGetQuery(db_con, paste0("DESCRIBE SELECT * FROM ", cohort_table))$column_name
  pcol <- .med_first_present(cols, c("subject_id", "person_id"), "person", person_col)
  icol <- .med_first_present(cols, c("cohort_start_date", "index_date"), "index date", index_date_col)
  scol <- NULL
  if (!is.null(site_col) || any(c("site_anon", "site_id", "site") %in% tolower(cols))) {
    scol <- .med_first_present(cols, c("site_anon", "site_id", "site"), "site", site_col)
  }
  where <- ""
  if (!is.null(cohort_definition_id)) {
    .med_first_present(cols, "cohort_definition_id", "cohort_definition_id")
    where <- sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_definition_id))
  }

  site_sel <- if (!is.null(scol)) sprintf(', "%s" AS site', scol) else ""
  base <- DBI::dbGetQuery(db_con, sprintf(
    'SELECT ROW_NUMBER() OVER () AS row_id, "%s" AS person_id, CAST("%s" AS DATE) AS index_date%s FROM %s %s',
    pcol, icol, site_sel, cohort_table, where))
  result <- base[, setdiff(names(base), "row_id"), drop = FALSE]

  lo <- if (is.null(lookback_days)) "" else sprintf("AND de.drug_exposure_start_date >= v.index_date - %d", as.integer(lookback_days))
  hi <- sprintf("AND de.drug_exposure_start_date <= v.index_date + %d", as.integer(lookahead_days))

  flags <- list(source = list(), standard = list())
  tmp <- character(0)
  on.exit(for (t in tmp) try(DBI::dbExecute(db_con, paste0("DROP TABLE IF EXISTS ", t)), silent = TRUE), add = TRUE)
  DBI::dbWriteTable(db_con, "_med_cohort", base[, c("row_id", "person_id", "index_date")], temporary = TRUE, overwrite = TRUE)
  tmp <- c(tmp, "_med_cohort")

  if (use_standard) {
    anc <- .med_resolve_table(db_con, "concept_ancestor")
    concept <- NULL
    rows <- list()
    for (cls in names(specs)) {
      spec <- specs[[cls]]
      ids <- as.integer(c(spec$ancestor_concept_ids, spec$rxnorm_ingredient_ids))
      if (length(spec$atc_codes)) {
        concept <- concept %||% .med_resolve_table(db_con, "concept")
        codes <- paste(.med_q(spec$atc_codes), collapse = ", ")
        ids <- c(ids, DBI::dbGetQuery(db_con, sprintf(
          "SELECT concept_id FROM %s WHERE vocabulary_id LIKE 'ATC%%' AND concept_code IN (%s)", concept, codes))$concept_id)
      }
      if (length(ids)) {
        rows[[length(rows) + 1L]] <- data.frame(drug_class = cls, ancestor_concept_id = sort(unique(as.integer(ids))),
                                                stringsAsFactors = FALSE)
      }
    }
    anc_df <- if (length(rows)) do.call(rbind, rows) else data.frame(drug_class = character(0), ancestor_concept_id = integer(0))
    DBI::dbWriteTable(db_con, "_med_anc", anc_df, temporary = TRUE, overwrite = TRUE)
    tmp <- c(tmp, "_med_anc", "_med_class_concepts")
    DBI::dbExecute(db_con, sprintf(
      paste0("CREATE OR REPLACE TEMPORARY TABLE _med_class_concepts AS ",
             "SELECT DISTINCT a.drug_class, ca.descendant_concept_id AS concept_id FROM _med_anc a ",
             "JOIN %s ca ON ca.ancestor_concept_id = a.ancestor_concept_id"), anc))
    hits <- DBI::dbGetQuery(db_con, paste0(
      "SELECT DISTINCT v.row_id, m.drug_class FROM _med_cohort v ",
      "JOIN drug_exposure de ON de.person_id = v.person_id ",
      "JOIN _med_class_concepts m ON m.concept_id = de.drug_concept_id ",
      "WHERE TRUE ", lo, " ", hi))
    flags$standard <- split(hits$row_id, hits$drug_class)
  }

  if (use_source) {
    xw <- .med_load_crosswalk(db_con, crosswalk)
    inline <- do.call(rbind, c(list(data.frame(source_value = character(0), drug_class = character(0), stringsAsFactors = FALSE)),
      lapply(names(specs), function(cls) {
        v <- specs[[cls]]$source_values
        if (length(v)) data.frame(source_value = toupper(trimws(as.character(v))), drug_class = cls, stringsAsFactors = FALSE)
      })))
    xw <- unique(rbind(xw, inline))
    xw <- xw[xw$drug_class %in% names(specs), , drop = FALSE]
    if (nrow(xw) == 0) {
      stop("Strategy 'source' needs a crosswalk or inline source_values for the classes in medication_specs.")
    }
    DBI::dbWriteTable(db_con, "_med_xw", xw, temporary = TRUE, overwrite = TRUE)
    tmp <- c(tmp, "_med_xw")
    code_join <- ""
    code_expr <- "NULL"
    concept_ref <- tryCatch(.med_resolve_table(db_con, "concept"), error = function(e) NULL)
    if (!is.null(concept_ref)) {
      code_join <- sprintf("LEFT JOIN %s sc ON sc.concept_id = de.drug_source_concept_id", concept_ref)
      code_expr <- "UPPER(TRIM(sc.concept_code))"
    }
    hits <- DBI::dbGetQuery(db_con, paste0(
      "SELECT DISTINCT v.row_id, x.drug_class FROM _med_cohort v ",
      "JOIN drug_exposure de ON de.person_id = v.person_id ", code_join, " ",
      "JOIN _med_xw x ON x.source_value = UPPER(TRIM(de.drug_source_value)) OR x.source_value = ", code_expr, " ",
      "WHERE TRUE ", lo, " ", hi))
    flags$source <- split(hits$row_id, hits$drug_class)
  }

  for (cls in names(specs)) {
    present <- character(0)
    if (use_source) {
      result[[paste0(cls, "_source")]] <- as.integer(base$row_id %in% flags$source[[cls]])
      present <- c(present, paste0(cls, "_source"))
    }
    if (use_standard) {
      result[[paste0(cls, "_standard")]] <- as.integer(base$row_id %in% flags$standard[[cls]])
      present <- c(present, paste0(cls, "_standard"))
    }
    result[[cls]] <- do.call(pmax, unname(as.list(result[present])))
  }

  if (isTRUE(return_audit)) {
    return(list(flags = result, audit = audit_medication_strategies(result, classes = names(specs), ...)))
  }
  result
}

#' @keywords internal
#' @noRd
.med_kappa <- function(a, b, c, d) {
  n <- a + b + c + d
  if (n == 0) return(NaN)
  po <- (a + d) / n
  pe <- ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
  if (pe >= 1) return(if (po == 1) 1 else NaN)
  (po - pe) / (1 - pe)
}

#' Per-site agreement audit of the source-crosswalk and OMOP-hierarchy strategies
#'
#' Computes, for each site and drug class (plus a pooled `ALL` row), the 2x2 agreement table of the two
#' strategies, Cohen's kappa, raw concordance and prevalences, and flags data-quality anomalies. Sites with
#' broken drug mappings stand out; nothing is hardcoded to particular sites.
#' Mirror of `omop_etl.medication_classes.audit_medication_strategies` in Python.
#'
#' @param flags Output of [extract_medication_classes()] with `strategy = "both"`.
#' @param classes Classes to audit (default: every class with both `_source` and `_standard` columns).
#' @param site_col Site column in `flags`; if absent all rows are audited as one site, `"all"`.
#' @param kappa_threshold Flag Cohen's kappa below this.
#' @param concordance_threshold Flag raw agreement below this.
#' @param prevalence_gap_threshold Flag `|prevalence_source - prevalence_standard|` above this.
#' @param peer_kappa_gap With at least 3 sites, flag a site whose kappa trails the median of the *other* sites
#'   by more than this.
#' @return A data frame with one row per (site, class): `site`, `medication_class`, `n`, `both`,
#'   `source_only`, `standard_only`, `neither`, `source_prevalence`, `standard_prevalence`, `concordance`,
#'   `kappa`, `reason` (`source_blind`, `standard_blind`, `low_kappa`, `low_concordance`, `prevalence_gap`,
#'   `peer_outlier`) and `flagged`.
#' @export
audit_medication_strategies <- function(flags, classes = NULL, site_col = "site", kappa_threshold = 0.6,
                                        concordance_threshold = 0.9, prevalence_gap_threshold = 0.10,
                                        peer_kappa_gap = 0.25) {
  if (is.null(classes)) {
    src <- sub("_source$", "", grep("_source$", names(flags), value = TRUE))
    classes <- src[paste0(src, "_standard") %in% names(flags)]
  }
  if (length(classes) == 0) {
    stop("No class has both '<class>_source' and '<class>_standard' columns; run extract_medication_classes(strategy = 'both').")
  }
  frame <- flags
  if (!site_col %in% names(frame)) frame[[site_col]] <- "all"
  sites <- as.character(frame[[site_col]])

  records <- list()
  for (cls in classes) {
    s <- as.logical(frame[[paste0(cls, "_source")]])
    t <- as.logical(frame[[paste0(cls, "_standard")]])
    groups <- c(split(seq_along(s), sites), list(ALL = seq_along(s)))
    for (site in names(groups)) {
      idx <- groups[[site]]
      ss <- s[idx]
      tt <- t[idx]
      a <- sum(ss & tt)
      b <- sum(ss & !tt)
      c_ <- sum(!ss & tt)
      d <- sum(!ss & !tt)
      n <- a + b + c_ + d
      records[[length(records) + 1L]] <- data.frame(
        site = site, medication_class = cls, n = n, both = a, source_only = b, standard_only = c_, neither = d,
        source_prevalence = if (n) (a + b) / n else NaN, standard_prevalence = if (n) (a + c_) / n else NaN,
        concordance = if (n) (a + d) / n else NaN, kappa = .med_kappa(a, b, c_, d), stringsAsFactors = FALSE
      )
    }
  }
  audit <- do.call(rbind, records)
  rownames(audit) <- NULL

  reasons <- vector("list", nrow(audit))
  for (i in seq_len(nrow(audit))) {
    r <- audit[i, ]
    why <- character(0)
    if (r$n > 0) {
      if (r$source_prevalence == 0 && r$standard_prevalence > 0) why <- c(why, "source_blind")
      if (r$standard_prevalence == 0 && r$source_prevalence > 0) why <- c(why, "standard_blind")
      if (!is.nan(r$kappa) && r$kappa < kappa_threshold && length(why) == 0) why <- c(why, "low_kappa")
      if (r$concordance < concordance_threshold) why <- c(why, "low_concordance")
      if (abs(r$source_prevalence - r$standard_prevalence) > prevalence_gap_threshold) why <- c(why, "prevalence_gap")
    }
    reasons[[i]] <- why
  }
  per_site <- audit$site != "ALL"
  for (cls in classes) {
    mask <- per_site & audit$medication_class == cls
    if (sum(mask) >= 3) {
      for (i in which(mask)) {
        k <- audit$kappa[[i]]
        peers <- audit$kappa[mask & seq_len(nrow(audit)) != i]
        peers <- peers[!is.nan(peers)]
        if (!is.nan(k) && length(peers) && k < stats::median(peers) - peer_kappa_gap) {
          reasons[[i]] <- c(reasons[[i]], "peer_outlier")
        }
      }
    }
  }
  audit$reason <- vapply(reasons, function(x) paste(unique(x), collapse = ","), character(1))
  audit$flagged <- nzchar(audit$reason)
  audit
}
