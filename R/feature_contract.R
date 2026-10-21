#' Load a feature spec
#'
#' @param spec A list with a `features` element (each with `name`, optional `dtype`, `bounds`, `impute`, `unit`),
#'   or the path to a `feature_spec.json`.
#' @return The spec as a list.
#' @export
load_feature_spec <- function(spec) {
  if (is.character(spec) && length(spec) == 1L) {
    spec <- jsonlite::fromJSON(spec, simplifyVector = FALSE)
  }
  feats <- spec$features
  if (is.null(feats) || length(feats) == 0L) stop("feature_spec needs a non-empty 'features' list.", call. = FALSE)
  nm <- vapply(feats, function(f) f$name, character(1))
  if (anyDuplicated(nm)) stop("feature_spec has duplicate feature names.", call. = FALSE)
  spec
}

# feature -> list(c(factor, label)): canonical = value / factor
.unit_suspects <- list(
  creatinine = list(list(88.4, "umol/L")), glucose = list(list(1 / 18.016, "mmol/L")),
  hemoglobin = list(list(10, "g/L")), wbc = list(list(1000, "cells/uL")), bun = list(list(0.357, "mmol/L urea"))
)

#' Validate a feature table against a feature contract
#'
#' Catches train/serve drift before a model scores garbage. Mirror of
#' `omop_etl.feature_contract.validate_feature_frame`. Missing columns, dtype mismatches and unit suspicion
#' (for example creatinine in umol/L) always raise; out-of-bounds values raise under `on_violation = "raise"`, are
#' clipped to the nearest bound under `"clip"` and set to `NA` under `"nullify"`.
#'
#' Bounds come from the spec's `bounds`, then `bounds`, then [DEFAULT_PHYSIOLOGIC_BOUNDS].
#'
#' @param df A data.frame (extra columns such as ids are allowed unless `allow_extra = FALSE`).
#' @param feature_spec Spec list or path to `feature_spec.json`.
#' @param on_violation `"raise"`, `"clip"` or `"nullify"`.
#' @param bounds Optional named list of `c(lo, hi)` defaults.
#' @param allow_extra If `FALSE`, unexpected columns are violations.
#' @return An object of class `feature_validation_report` (a list): `frame` (spec-ordered, repaired features),
#'   `missing`, `extra`, `dtype_mismatch`, `out_of_bounds`, `unit_suspicion`, `n_clipped`, `n_nullified`, `ok`.
#'   On a violation that raises, the error carries the report in `$report`.
#' @export
validate_feature_frame <- function(df, feature_spec, on_violation = "raise", bounds = NULL, allow_extra = TRUE) {
  on_violation <- match.arg(on_violation, c("raise", "clip", "nullify"))
  spec <- load_feature_spec(feature_spec)
  feats <- spec$features
  names_ <- vapply(feats, function(f) f$name, character(1))
  rep <- list(n_rows = nrow(df), missing = setdiff(names_, names(df)), extra = character(),
              dtype_mismatch = list(), out_of_bounds = list(), unit_suspicion = list(), all_null = character(),
              on_violation = on_violation, n_clipped = 0L, n_nullified = 0L)
  if (!allow_extra) rep$extra <- setdiff(names(df), names_)
  out <- df[, intersect(names_, names(df)), drop = FALSE]
  for (f in feats) {
    n <- f$name
    if (!n %in% names(out)) next
    want <- if (is.null(f$dtype)) "float" else f$dtype
    x <- out[[n]]
    num <- is.numeric(x) || is.logical(x)
    if (want %in% c("float", "int", "bool") && !num) {
      rep$dtype_mismatch[[n]] <- list(expected = want, actual = class(x)[1]); next
    }
    if (want %in% c("float", "int") && is.logical(x)) {
      rep$dtype_mismatch[[n]] <- list(expected = want, actual = "logical"); next
    }
    if (want == "str" && !is.character(x) && !is.factor(x)) {
      rep$dtype_mismatch[[n]] <- list(expected = want, actual = class(x)[1]); next
    }
    if (all(is.na(x))) rep$all_null <- c(rep$all_null, n)
    if (!want %in% c("float", "int")) next
    b <- f$bounds
    if (is.null(b) && !is.null(bounds)) b <- bounds[[n]]
    if (is.null(b)) b <- DEFAULT_PHYSIOLOGIC_BOUNDS[[n]]
    if (is.null(b)) next
    lo <- if (is.null(b[[1]])) -Inf else as.numeric(b[[1]])
    hi <- if (is.null(b[[2]])) Inf else as.numeric(b[[2]])
    v <- as.numeric(x)
    nn <- !is.na(v)
    bad <- nn & (v < lo | v > hi)
    nbad <- sum(bad)
    if (nbad > 0) {
      rep$out_of_bounds[[n]] <- list(n = nbad, pct = 100 * nbad / max(sum(nn), 1L),
                                     bounds = list(if (is.finite(lo)) lo else NULL, if (is.finite(hi)) hi else NULL))
    }
    for (s in .unit_suspects[[n]]) {
      if (sum(nn) > 0 && nbad / sum(nn) >= 0.5) {
        resc <- v[nn] / s[[1]]
        if (mean(resc >= lo & resc <= hi) >= 0.5) {
          rep$unit_suspicion[[n]] <- sprintf(
            "%d/%d values are out of range but fall in range if divided by %g: looks like %s rather than %s.",
            nbad, sum(nn), s[[1]], s[[2]], if (is.null(f$unit)) "the canonical unit" else f$unit)
          break
        }
      }
    }
    if (nbad > 0 && on_violation != "raise" && is.null(rep$unit_suspicion[[n]])) {
      if (on_violation == "clip") {
        out[[n]] <- pmin(pmax(v, lo), hi); rep$n_clipped <- rep$n_clipped + nbad
      } else {
        v[bad] <- NA_real_; out[[n]] <- v; rep$n_nullified <- rep$n_nullified + nbad
      }
    }
  }
  rep$frame <- out[, intersect(names_, names(out)), drop = FALSE]
  rep$ok <- !(length(rep$missing) || length(rep$extra) || length(rep$dtype_mismatch) ||
                length(rep$out_of_bounds) || length(rep$unit_suspicion))
  class(rep) <- "feature_validation_report"
  fatal <- length(rep$missing) || length(rep$extra) || length(rep$dtype_mismatch) || length(rep$unit_suspicion) ||
    (length(rep$out_of_bounds) && on_violation == "raise")
  if (fatal) {
    cond <- structure(class = c("feature_contract_error", "error", "condition"),
                      list(message = paste(format(rep), collapse = "\n"), call = NULL, report = rep))
    stop(cond)
  }
  rep
}

#' @export
format.feature_validation_report <- function(x, ...) {
  if (isTRUE(x$ok)) return(sprintf("Feature contract OK (%d rows).", x$n_rows))
  l <- sprintf("Feature contract violations (%d rows):", x$n_rows)
  if (length(x$missing)) l <- c(l, paste("  missing columns:", paste(x$missing, collapse = ", ")))
  if (length(x$extra)) l <- c(l, paste("  unexpected columns:", paste(x$extra, collapse = ", ")))
  for (n in names(x$dtype_mismatch)) {
    l <- c(l, sprintf("  dtype %s: expected %s, got %s", n, x$dtype_mismatch[[n]]$expected, x$dtype_mismatch[[n]]$actual))
  }
  for (n in names(x$out_of_bounds)) {
    l <- c(l, sprintf("  out of bounds %s: %d values (%.2f%%)", n, x$out_of_bounds[[n]]$n, x$out_of_bounds[[n]]$pct))
  }
  for (n in names(x$unit_suspicion)) l <- c(l, sprintf("  unit suspicion %s: %s", n, x$unit_suspicion[[n]]))
  l
}

#' @export
print.feature_validation_report <- function(x, ...) {
  cat(format(x), sep = "\n")
  invisible(x)
}

#' Aggregate-only validation counts for a telemetry packet
#' @param report A `feature_validation_report`.
#' @return A named list of counts (no values).
#' @export
feature_validation_telemetry <- function(report) {
  list(n_rows = report$n_rows, n_missing_features = length(report$missing), n_extra_features = length(report$extra),
       n_dtype_mismatch = length(report$dtype_mismatch), n_unit_suspicion = length(report$unit_suspicion),
       n_values_clipped = as.integer(report$n_clipped), n_values_nullified = as.integer(report$n_nullified),
       on_violation = report$on_violation)
}
