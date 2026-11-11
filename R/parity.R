#' Parity audit between a legacy table and its OMOP-derived counterpart
#'
#' Matches the rows of two data frames on a key and reports, for every shared column, how far the two derivations
#' agree: marginal differences, missingness discrepancies and concordance statistics. Generic: it needs two data frames
#' and a key.
#'
#' Numeric columns: matched N, means, SDs, standardized mean difference (SMD), Pearson correlation, Lin's concordance
#' correlation coefficient (CCC), mean absolute difference and the share of pairs within `tolerance`. Other columns: percent
#' agreement, Cohen's kappa and the total variation distance between the two marginal distributions. All columns: missingness
#' in each table and the gap. `levels` gives each category's count in each table. Counts below `min_cell` (default 11) and the
#' statistics computed from them are blanked ([suppress_small_counts()]); key values are never written.
#'
#' This is the R counterpart of Python's `omop_etl.audit_matrix_parity()`; the R version reports the pooled (`ALL`) stratum only.
#'
#' @param legacy_df,omop_df The two tables. Duplicate keys in either one are an error.
#' @param key_col Key column name(s) present in both.
#' @param columns Columns to compare (default: all shared columns except the key).
#' @param tolerance Numeric pairs agree when `|a - b| <= tolerance`.
#' @param smd_threshold,missingness_gap_threshold,kappa_threshold,ccc_threshold Thresholds that set `flagged`.
#' @param max_levels Categorical columns with more levels than this are compared but not tabulated in `levels`.
#' @param min_cell Suppression threshold (`NULL` or 1 keeps exact counts; only for use inside the site).
#' @return A list with `ledger` (one row per column), `levels`, `keys` (match counts) and `flagged` (column names).
#' @examples
#' legacy <- data.frame(id = 1:40, sbp = seq(100, 139), sex = rep(c("F", "M"), 20))
#' omop <- legacy; omop$sbp <- omop$sbp + 6
#' audit_matrix_parity(legacy, omop, "id", min_cell = NULL)$ledger[, c("column", "smd", "ccc", "flagged")]
#' @export
audit_matrix_parity <- function(legacy_df, omop_df, key_col, columns = NULL, tolerance = 0, smd_threshold = 0.10,
                                missingness_gap_threshold = 0.05, kappa_threshold = 0.80, ccc_threshold = 0.90,
                                max_levels = 50L, min_cell = 11L) {
  for (nm in c("legacy_df", "omop_df")) {
    d <- get(nm)
    if (!all(key_col %in% names(d))) stop(sprintf("%s has no key column(s) %s", nm, paste(setdiff(key_col, names(d)), collapse = ", ")), call. = FALSE)
    if (anyDuplicated(d[key_col])) stop(sprintf("%s has duplicate values of the key; deduplicate before auditing parity.", nm), call. = FALSE)
  }
  cols <- if (is.null(columns)) setdiff(intersect(names(legacy_df), names(omop_df)), key_col) else columns
  if (!length(cols)) stop("the two tables share no columns to compare.", call. = FALSE)
  m <- merge(legacy_df[c(key_col, cols)], omop_df[c(key_col, cols)], by = key_col, suffixes = c(".L", ".R"))
  n_left <- nrow(legacy_df); n_right <- nrow(omop_df); n_match <- nrow(m)
  keys <- data.frame(n_legacy = n_left, n_omop = n_right, n_matched = n_match, n_only_legacy = n_left - n_match,
                     n_only_omop = n_right - n_match,
                     pct_legacy_matched = if (n_left) 100 * n_match / n_left else NA_real_,
                     pct_omop_matched = if (n_right) 100 * n_match / n_right else NA_real_)
  ccc <- function(x, y) {
    if (length(x) < 2L) return(NA_real_)
    vx <- mean((x - mean(x))^2); vy <- mean((y - mean(y))^2)
    den <- vx + vy + (mean(x) - mean(y))^2
    if (den > 0) 2 * mean((x - mean(x)) * (y - mean(y))) / den else if (identical(x, y)) 1 else NA_real_
  }
  kappa <- function(a, b) {
    lv <- union(a, b); ct <- table(factor(a, lv), factor(b, lv)); n <- sum(ct)
    if (!n) return(NA_real_)
    po <- sum(diag(ct)) / n; pe <- sum(rowSums(ct) * colSums(ct)) / n^2
    if (pe < 1) (po - pe) / (1 - pe) else if (po == 1) 1 else NA_real_
  }
  rows <- list(); lev_rows <- list()
  for (cn in cols) {
    a <- m[[paste0(cn, ".L")]]; b <- m[[paste0(cn, ".R")]]
    if (is.null(a)) { a <- m[[cn]]; b <- m[[cn]] }
    ma <- is.na(a); mb <- is.na(b); both <- !(ma | mb); n <- length(a)
    rec <- list(column = cn, n_matched = n, n_both_missing = sum(ma & mb), n_one_missing = sum(xor(ma, mb)),
                left_missing_rate = if (n) mean(ma) else NA_real_, right_missing_rate = if (n) mean(mb) else NA_real_)
    rec$missingness_gap <- abs(rec$left_missing_rate - rec$right_missing_rate)
    num <- list(left_mean = NA_real_, right_mean = NA_real_, left_sd = NA_real_, right_sd = NA_real_, smd = NA_real_,
                correlation = NA_real_, ccc = NA_real_, mean_abs_diff = NA_real_, pct_within_tolerance = NA_real_,
                pct_agreement = NA_real_, kappa = NA_real_, tv_distance = NA_real_)
    reasons <- character()
    if (is.numeric(a) && is.numeric(b)) {
      rec$kind <- "numeric"
      x <- as.numeric(a[both]); y <- as.numeric(b[both])
      num$left_mean <- if (any(!ma)) mean(a, na.rm = TRUE) else NA_real_
      num$right_mean <- if (any(!mb)) mean(b, na.rm = TRUE) else NA_real_
      num$left_sd <- if (sum(!ma) > 1L) stats::sd(a, na.rm = TRUE) else NA_real_
      num$right_sd <- if (sum(!mb) > 1L) stats::sd(b, na.rm = TRUE) else NA_real_
      pooled <- sqrt((ifelse(is.na(num$left_sd), 0, num$left_sd)^2 + ifelse(is.na(num$right_sd), 0, num$right_sd)^2) / 2)
      num$smd <- if (pooled > 0) (num$left_mean - num$right_mean) / pooled else if (isTRUE(num$left_mean == num$right_mean)) 0 else NA_real_
      num$correlation <- if (length(x) > 2L && stats::sd(x) > 0 && stats::sd(y) > 0) stats::cor(x, y) else NA_real_
      num$ccc <- ccc(x, y)
      num$mean_abs_diff <- if (length(x)) mean(abs(x - y)) else NA_real_
      num$pct_within_tolerance <- if (length(x)) 100 * mean(abs(x - y) <= tolerance) else NA_real_
      if (!is.na(num$smd) && abs(num$smd) > smd_threshold) reasons <- c(reasons, sprintf("|SMD| %.2f > %s", abs(num$smd), smd_threshold))
      if (!is.na(num$ccc) && num$ccc < ccc_threshold) reasons <- c(reasons, sprintf("CCC %.2f < %s", num$ccc, ccc_threshold))
    } else {
      rec$kind <- "categorical"
      xa <- as.character(a[both]); xb <- as.character(b[both])
      num$pct_agreement <- if (length(xa)) 100 * mean(xa == xb) else NA_real_
      num$kappa <- if (length(xa)) kappa(xa, xb) else NA_real_
      pa <- prop.table(table(as.character(a[!ma]))); pb <- prop.table(table(as.character(b[!mb])))
      lv <- union(names(pa), names(pb))
      num$tv_distance <- if (length(lv)) 0.5 * sum(abs(as.numeric(pa[lv]) %|na|% 0 - as.numeric(pb[lv]) %|na|% 0)) else NA_real_
      if (!is.na(num$kappa) && num$kappa < kappa_threshold) reasons <- c(reasons, sprintf("kappa %.2f < %s", num$kappa, kappa_threshold))
      if (length(lv) <= max_levels) {
        ca <- table(as.character(a[!ma])); cb <- table(as.character(b[!mb]))
        for (l in sort(lv)) {
          lev_rows[[length(lev_rows) + 1L]] <- data.frame(column = cn, level = l, n_legacy = as.numeric(if (l %in% names(ca)) ca[[l]] else 0),
                                                           n_omop = as.numeric(if (l %in% names(cb)) cb[[l]] else 0))
        }
      }
    }
    if (!is.na(rec$missingness_gap) && rec$missingness_gap > missingness_gap_threshold) {
      reasons <- c(reasons, sprintf("missingness gap %.2f > %s", rec$missingness_gap, missingness_gap_threshold))
    }
    rec <- c(rec, num, list(flagged = length(reasons) > 0L, reason = paste(reasons, collapse = "; ")))
    rows[[length(rows) + 1L]] <- as.data.frame(rec, stringsAsFactors = FALSE)
  }
  ledger <- do.call(rbind, rows)
  levels <- if (length(lev_rows)) do.call(rbind, lev_rows) else data.frame(column = character(), level = character(), n_legacy = numeric(), n_omop = numeric())
  flagged <- sort(unique(ledger$column[ledger$flagged %in% TRUE]))
  mc <- if (!is.null(min_cell) && min_cell > 1L) as.integer(min_cell) else NULL
  if (!is.null(mc)) {
    derived <- c("left_missing_rate", "right_missing_rate", "missingness_gap", "left_mean", "right_mean", "left_sd", "right_sd",
                 "smd", "correlation", "ccc", "mean_abs_diff", "pct_within_tolerance", "pct_agreement", "kappa", "tv_distance")
    ledger <- suppress_small_counts(ledger, c("n_matched", "n_both_missing", "n_one_missing"), mc, also = derived, complementary = FALSE)
    ledger$flagged[ledger$suppressed] <- NA
    ledger$reason[ledger$suppressed] <- "suppressed (small cell)"
    if (nrow(levels)) levels <- suppress_small_counts(levels, c("n_legacy", "n_omop"), mc, group_by = "column", complementary = TRUE)
    kc <- c("n_legacy", "n_omop", "n_matched", "n_only_legacy", "n_only_omop")
    keys <- suppress_small_counts(keys, kc, mc, also = c("pct_legacy_matched", "pct_omop_matched"), complementary = FALSE)
    for (c in kc) keys[[c]][keys$suppressed] <- NA_real_   # the counts are linked: one blank would be recoverable
  } else {
    ledger$suppressed <- FALSE; keys$suppressed <- FALSE
    if (nrow(levels)) levels$suppressed <- FALSE
  }
  list(ledger = ledger, levels = levels, keys = keys, flagged = flagged, min_cell = mc)
}

`%|na|%` <- function(x, y) ifelse(is.na(x), y, x)
