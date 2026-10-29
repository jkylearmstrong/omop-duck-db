#' Post-deployment drift and calibration monitoring
#'
#' `population_stability_index()` compares the predicted-risk distribution with a frozen baseline over the baseline's quantile
#' bins (outer bins open-ended; empty bins floored at 1e-4). Rule of thumb: < 0.10 stable, 0.10-0.25 watch, >= 0.25 shifted.
#' `calibration_summary()` gives observed vs predicted for scored encounters with known outcomes. `monitor_scores()` builds a monthly
#' table, and `update_monitoring_log()` upserts it into a CSV. Output is aggregate-only. Mirror of Python `omop_etl.monitoring`.
#'
#' @param baseline Frozen reference probabilities (e.g. validation-period scores).
#' @param current Probabilities to compare.
#' @param bins Number of quantile bins.
#' @return `population_stability_index()`: a number.
#' @export
population_stability_index <- function(baseline, current, bins = 10) {
  b <- .mon_num(baseline, "baseline"); cu <- .mon_num(current, "current")
  if (!length(b) || !length(cu)) stop("baseline and current must be non-empty.", call. = FALSE)
  edges <- unique(as.numeric(stats::quantile(b, seq(0, 1, length.out = as.integer(bins) + 1)[-c(1, as.integer(bins) + 1)], type = 7, names = FALSE)))
  share <- function(x) pmax(tabulate(findInterval(x, edges) + 1L, nbins = length(edges) + 1L) / length(x), 1e-4)
  pb <- share(b); pc <- share(cu)
  sum((pc - pb) * log(pc / pb))
}

.mon_num <- function(x, what) {
  a <- as.numeric(x)
  if (anyNA(a)) stop(what, " contains NA.", call. = FALSE)
  a
}

#' @rdname population_stability_index
#' @param y 0/1 outcomes.
#' @param p Predicted probabilities.
#' @param min_events Minimum events AND non-events before slope, intercept and AUROC are reported (else `NA`).
#' @return `calibration_summary()`: a list with `n`, `events`, `observed`, `mean_predicted`, `oe_ratio`, `brier`, `ece`, `slope`,
#'   `intercept`, `auroc`.
#' @export
calibration_summary <- function(y, p, bins = 10, min_events = 30) {
  y <- .mon_num(y, "y"); p <- .mon_num(p, "p")
  if (length(y) != length(p)) stop("y and p must have the same length.", call. = FALSE)
  n <- length(y)
  out <- list(n = n, events = as.integer(sum(y)), observed = NA_real_, mean_predicted = NA_real_, oe_ratio = NA_real_,
              brier = NA_real_, ece = NA_real_, slope = NA_real_, intercept = NA_real_, auroc = NA_real_)
  if (n == 0) return(out)
  out$observed <- mean(y); out$mean_predicted <- mean(p)
  out$oe_ratio <- if (mean(p) > 0) mean(y) / mean(p) else NA_real_
  out$brier <- mean((p - y)^2)
  edges <- unique(as.numeric(stats::quantile(p, seq(0, 1, length.out = as.integer(bins) + 1)[-c(1, as.integer(bins) + 1)], type = 7, names = FALSE)))
  idx <- findInterval(p, edges)
  out$ece <- sum(vapply(unique(idx), function(k) abs(mean(y[idx == k]) - mean(p[idx == k])) * sum(idx == k) / n, numeric(1)))
  if (out$events >= min_events && n - out$events >= min_events) {
    fit <- .calibration_fit(y, p)
    if (!is.null(fit$slope)) { out$slope <- fit$slope; out$intercept <- fit$intercept }
    out$auroc <- unname(auroc_delong(y, p)["auc"])
  }
  out
}

#' @rdname population_stability_index
#' @param scores A data.frame with the score date, the probability and (optionally) the 0/1 outcome.
#' @param baseline_prob Frozen reference distribution for the PSI.
#' @param date_col,prob_col,outcome_col Column names (`outcome_col = NULL` for none).
#' @param as_of Date the report is run (default today); a month's calibration is computed only once month end + `followup_days` <= `as_of`.
#' @param followup_days Outcome window in days.
#' @param psi_bins Bins for the PSI.
#' @param oe_range,slope_range Acceptable observed/expected ratio and calibration slope; outside them (with >= `min_events` events) the
#'   month is `"miscalibrated"`.
#' @return `monitor_scores()`: a data.frame with one row per month (`month`, `n`, `mean_prob`, `psi`, `outcomes_matured`,
#'   `n_with_outcome`, the `calibration_summary()` fields and `status`: `ok`, `watch` (PSI 0.10-0.25), `drift` (PSI >= 0.25),
#'   `miscalibrated` or `insufficient_outcomes`; `miscalibrated` takes precedence).
#' @export
monitor_scores <- function(scores, baseline_prob, date_col = "score_date", prob_col = "prob", outcome_col = "outcome_flag", as_of = NULL,
                           followup_days = 30, min_events = 30, psi_bins = 10, oe_range = c(0.8, 1.25), slope_range = c(0.8, 1.2)) {
  for (col in c(date_col, prob_col)) if (!col %in% names(scores)) stop("scores needs a '", col, "' column.", call. = FALSE)
  now <- if (is.null(as_of)) Sys.Date() else as.Date(as_of)
  has_outcome <- !is.null(outcome_col) && outcome_col %in% names(scores)
  d <- as.Date(scores[[date_col]])
  month <- format(d, "%Y-%m")
  rows <- lapply(sort(unique(month)), function(m) {
    g <- month == m
    p <- as.numeric(scores[[prob_col]][g])
    first <- as.Date(paste0(m, "-01"))
    month_end <- seq(first, by = "month", length.out = 2)[2] - 1
    matured <- has_outcome && (month_end + as.integer(followup_days) <= now)
    known <- if (has_outcome) !is.na(scores[[outcome_col]][g]) else rep(FALSE, sum(g))
    cal <- if (matured && any(known)) calibration_summary(as.numeric(scores[[outcome_col]][g][known]), p[known], min_events = min_events) else calibration_summary(numeric(0), numeric(0))
    row <- c(list(month = m, n = length(p), mean_prob = mean(p), psi = population_stability_index(baseline_prob, p, psi_bins), outcomes_matured = matured,
                  n_with_outcome = cal$n), cal[setdiff(names(cal), "n")])
    row$status <- .monitor_status(row, matured, min_events, oe_range, slope_range)
    as.data.frame(row, stringsAsFactors = FALSE)
  })
  cols <- c("month", "n", "mean_prob", "psi", "outcomes_matured", "n_with_outcome", "events", "observed", "mean_predicted", "oe_ratio",
            "brier", "ece", "slope", "intercept", "auroc", "status")
  if (!length(rows)) return(stats::setNames(as.data.frame(matrix(nrow = 0, ncol = length(cols))), cols))
  out <- do.call(rbind, rows)[, cols]
  rownames(out) <- NULL
  out
}

.monitor_status <- function(row, matured, min_events, oe, slope) {
  enough <- matured && row$events >= min_events
  if (enough) {
    bad_oe <- !is.na(row$oe_ratio) && (row$oe_ratio < oe[[1]] || row$oe_ratio > oe[[2]])
    bad_slope <- !is.na(row$slope) && (row$slope < slope[[1]] || row$slope > slope[[2]])
    if (bad_oe || bad_slope) return("miscalibrated")
  }
  if (row$psi >= 0.25) return("drift")
  if (row$psi >= 0.10) return("watch")
  if (enough) "ok" else "insufficient_outcomes"
}

#' @rdname population_stability_index
#' @param report The data.frame from `monitor_scores()`.
#' @param path CSV log path.
#' @param model_version Version string; the log is keyed on (`model_version`, `month`).
#' @return `update_monitoring_log()`: the full, sorted log (a later run replaces a month, e.g. once its outcomes mature).
#' @export
update_monitoring_log <- function(report, path, model_version = "") {
  new <- cbind(model_version = rep(as.character(model_version), nrow(report)), report, stringsAsFactors = FALSE)
  if (file.exists(path)) {
    old <- utils::read.csv(path, stringsAsFactors = FALSE, colClasses = c(model_version = "character", month = "character"))
    old <- old[!paste(old$model_version, old$month) %in% paste(new$model_version, new$month), , drop = FALSE]
    if (nrow(old)) new <- rbind(old[, names(new)], new)
  }
  new <- new[order(new$model_version, new$month), , drop = FALSE]
  rownames(new) <- NULL
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  utils::write.csv(new, path, row.names = FALSE)
  new
}
