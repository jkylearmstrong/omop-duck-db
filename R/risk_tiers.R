#' Capacity-calibrated risk tiers
#'
#' Map continuous probabilities to discrete intervention tiers. A tier definition is an ordered list (highest risk first) of
#' `list(name =, top_fraction =)`, where `top_fraction` is the CUMULATIVE share of the reference population in that tier or above;
#' the last tier takes the rest. `list(list(name = "high", top_fraction = 0.10), list(name = "standard"))` means "the riskiest 10%
#' get the nurse transition call". Freeze the cut-points from a reference set (e.g. validation-period scores): re-fitting on each
#' batch would hide drift. Mirror of Python `omop_etl.risk_tiers`.
#'
#' `fit_capacity_thresholds()` returns cut-points lowest risk first as `list(name =, min =, fraction =)` (the shape
#' `write_model_bundle(risk_tiers = )` stores; the realised `fraction` of the reference set is extra). Tied probabilities cannot be
#' split, so a tier can be slightly larger than requested.
#'
#' @param reference_prob Reference probabilities in `[0, 1]`.
#' @param tiers Ordered list of `list(name =, top_fraction =)`, the last without `top_fraction`.
#' @return `fit_capacity_thresholds()`: a list of `list(name =, min =, fraction =)`.
#' @export
fit_capacity_thresholds <- function(reference_prob, tiers) {
  ref <- .tier_probs(reference_prob, "reference_prob")
  if (!length(ref)) stop("reference_prob is empty.", call. = FALSE)
  if (length(tiers) < 2 || !all(vapply(tiers, function(t) !is.null(t$name), logical(1)))) stop("need at least two named tiers.", call. = FALSE)
  nm <- vapply(tiers, function(t) t$name, character(1))
  if (anyDuplicated(nm)) stop("tier names must be unique.", call. = FALSE)
  if (!is.null(tiers[[length(tiers)]]$top_fraction)) stop("the last (lowest-risk) tier takes the remainder: omit its top_fraction.", call. = FALSE)
  fracs <- vapply(tiers[-length(tiers)], function(t) {
    f <- t$top_fraction
    if (is.null(f) || !(f > 0 && f < 1)) stop("tier '", t$name, "': top_fraction must be in (0, 1).", call. = FALSE)
    as.numeric(f)
  }, numeric(1))
  if (any(diff(fracs) <= 0)) stop("top_fraction is cumulative and must increase from the highest-risk tier down.", call. = FALSE)
  cuts <- as.numeric(stats::quantile(ref, 1 - fracs, type = 7, names = FALSE))
  if (any(diff(cuts) >= 0)) stop("reference probabilities are too tied/concentrated to separate the requested tiers.", call. = FALSE)
  mins <- c(0, rev(cuts))
  names_lo <- rev(nm)
  lapply(seq_along(mins), function(i) {
    hi <- if (i < length(mins)) mins[[i + 1]] else Inf
    list(name = names_lo[[i]], min = mins[[i]], fraction = mean(ref >= mins[[i]] & ref < hi))
  })
}

.tier_probs <- function(prob, what = "prob") {
  p <- as.numeric(prob)
  if (length(p) && (anyNA(p) || min(p) < 0 || max(p) > 1)) stop(what, " must be probabilities in [0, 1] with no NA.", call. = FALSE)
  p
}

#' @rdname fit_capacity_thresholds
#' @param prob Probabilities in `[0, 1]`.
#' @param thresholds `list(list(name =, min =), ...)` in any order (from `fit_capacity_thresholds()` or a bundle's `risk_tiers`).
#'   Tiers are `[min, next_min)` and the lowest must start at 0.
#' @param as_category Return an ordered factor (lowest tier first) instead of a character vector.
#' @return `map_risk_tiers()`: tier names (a string for a single probability).
#' @export
map_risk_tiers <- function(prob, thresholds, as_category = FALSE) {
  p <- .tier_probs(prob)
  mins <- vapply(thresholds, function(t) as.numeric(t$min), numeric(1))
  o <- order(mins)
  mins <- mins[o]
  nm <- vapply(thresholds, function(t) as.character(t$name), character(1))[o]
  if (!length(mins) || mins[[1]] > 0) stop("the lowest tier must start at min = 0 so every probability maps to a tier.", call. = FALSE)
  if (any(diff(mins) <= 0)) stop("tier minimums must be distinct.", call. = FALSE)
  out <- nm[findInterval(p, mins)]
  if (length(prob) == 1L && !as_category) return(out)
  if (as_category) factor(out, levels = nm, ordered = TRUE) else out
}

#' @rdname fit_capacity_thresholds
#' @param capacity Optional named numeric: cases per period each tier's team can handle.
#' @param periods Number of days/weeks covered by `prob`.
#' @return `tier_load()`: a data.frame with `tier`, `n`, `share`, `per_period` and, with `capacity`, `capacity` and `over_capacity`.
#' @export
tier_load <- function(prob, thresholds, capacity = NULL, periods = 1) {
  p <- .tier_probs(prob)
  nm <- map_risk_tiers(p, thresholds)
  mins <- vapply(thresholds, function(t) as.numeric(t$min), numeric(1))
  order_names <- vapply(thresholds, function(t) as.character(t$name), character(1))[order(-mins)]
  n <- vapply(order_names, function(t) sum(nm == t), numeric(1))
  df <- data.frame(tier = order_names, n = as.integer(n), share = n / max(length(p), 1), per_period = n / max(as.integer(periods), 1),
                   stringsAsFactors = FALSE, row.names = NULL)
  if (!is.null(capacity)) {
    df$capacity <- as.numeric(capacity[df$tier])
    df$over_capacity <- !is.na(df$capacity) & df$per_period > df$capacity
  }
  df
}
