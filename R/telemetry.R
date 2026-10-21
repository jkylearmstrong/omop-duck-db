#' Schema version of the site telemetry packet
#' @format A string.
#' @export
TELEMETRY_SCHEMA_VERSION <- "1.0"

# ---- statistics ---------------------------------------------------------------------------------------

#' AUROC with DeLong standard error
#'
#' @param y 0/1 outcomes.
#' @param p Predicted probabilities.
#' @return `c(auc =, se =)`.
#' @export
auroc_delong <- function(y, p) {
  pos <- p[y == 1]; neg <- p[y == 0]
  m <- length(pos); n <- length(neg)
  tz <- rank(c(pos, neg), ties.method = "average")
  tx <- rank(pos, ties.method = "average"); ty <- rank(neg, ties.method = "average")
  v10 <- (tz[seq_len(m)] - tx) / n
  v01 <- 1 - (tz[m + seq_len(n)] - ty) / m
  c(auc = mean(v10), se = sqrt(max(stats::var(v10) / m + stats::var(v01) / n, 0)))
}

#' Average precision (area under the precision-recall step curve)
#' @param y 0/1 outcomes.
#' @param p Predicted probabilities.
#' @return A number.
#' @export
average_precision <- function(y, p) {
  o <- order(-p)
  ys <- y[o]; ps <- p[o]
  tp <- cumsum(ys); fp <- cumsum(1 - ys)
  last <- c(ps[-1] != ps[-length(ps)], TRUE)
  tp <- tp[last]; fp <- fp[last]
  sum(diff(c(0, tp / sum(ys))) * (tp / (tp + fp)))
}

.calibration_fit <- function(y, p) {
  pc <- pmin(pmax(p, 1e-9), 1 - 1e-9)
  z <- log(pc / (1 - pc))
  X <- cbind(1, z)
  beta <- c(0, 0)
  H <- diag(2)
  ok <- tryCatch({
    for (i in 1:50) {
      mu <- 1 / (1 + exp(-pmin(pmax(X %*% beta, -30), 30)))
      W <- as.numeric(mu * (1 - mu))
      H <- crossprod(X, X * W) + 1e-10 * diag(2)
      step <- solve(H, crossprod(X, y - mu))
      beta <- beta + as.numeric(step)
      if (max(abs(step)) < 1e-9) break
    }
    cov <- solve(H)
    TRUE
  }, error = function(e) FALSE)
  if (!ok) return(list(slope = NULL, slope_se = NULL, intercept = NULL, intercept_se = NULL, citl = NULL))
  a <- 0
  for (i in 1:50) {
    mu <- 1 / (1 + exp(-pmin(pmax(a + z, -30), 30)))
    step <- sum(y - mu) / max(sum(mu * (1 - mu)), 1e-12)
    a <- a + step
    if (abs(step) < 1e-10) break
  }
  list(slope = beta[2], slope_se = sqrt(cov[2, 2]), intercept = beta[1], intercept_se = sqrt(cov[1, 1]), citl = a)
}

.suppress_mask <- function(counts, min_cell) {
  mask <- counts > 0 & counts < min_cell
  if (sum(mask) == 1L) {
    rest <- which(!mask & counts > 0)
    if (length(rest)) mask[rest[which.min(counts[rest])]] <- TRUE else mask[] <- TRUE
  }
  mask
}

#' Build an aggregate-only site telemetry packet
#'
#' Computes discrimination, calibration, a decile table and screening tiers at a site. The packet contains no
#' row-level data: counts in `1..min_cell-1` are suppressed with secondary suppression so they cannot be recovered
#' by subtraction. Mirror of `omop_etl.telemetry.site_telemetry`; both languages write the same JSON schema
#' (`inst/extdata/telemetry_packet.schema.json`).
#'
#' If either outcome class has fewer than `min_cell` members every metric is withheld (`suppressed = TRUE`).
#'
#' @param scores Predicted probabilities.
#' @param y_true Observed 0/1 outcomes.
#' @param site_id Opaque consortium code matching `[A-Za-z0-9_-]{1,32}` (not a facility name).
#' @param min_cell Smallest count that may be published (default 11).
#' @param tier_fractions Top-k fractions of scores flagged for the screening tiers (NNS = 1 / PPV).
#' @param validation Optional `feature_validation_report` whose clip / nullify counts are recorded.
#' @param model Optional list with `name` and `version` of the scoring bundle.
#' @param output Optional path; the packet is written there as JSON.
#' @return The packet (a list).
#' @export
site_telemetry <- function(scores, y_true, site_id, min_cell = 11L, tier_fractions = c(0.05, 0.10, 0.20),
                           validation = NULL, model = NULL, output = NULL) {
  if (!grepl("^[A-Za-z0-9_-]{1,32}$", site_id)) {
    stop("site_id must be an opaque code matching [A-Za-z0-9_-]{1,32}.", call. = FALSE)
  }
  min_cell <- as.integer(min_cell)
  if (min_cell < 2L) stop("min_cell must be >= 2.", call. = FALSE)
  p <- as.numeric(scores); y <- as.numeric(y_true)
  if (length(p) != length(y)) stop("scores and y_true must have the same length.", call. = FALSE)
  keep <- is.finite(p) & !is.na(y)
  p <- p[keep]; y <- y[keep]
  if (!all(y %in% c(0, 1))) stop("y_true must be binary 0/1.", call. = FALSE)
  if (length(p) && (min(p) < 0 || max(p) > 1)) stop("scores must be probabilities in [0, 1].", call. = FALSE)
  n <- length(p); ne <- as.integer(sum(y))
  packet <- list(schema_version = TELEMETRY_SCHEMA_VERSION, site_id = site_id, min_cell = min_cell,
                 model = if (is.null(model)) NULL else as.list(model),
                 n = n, n_events = ne, prevalence = NULL, suppressed = FALSE, metrics = NULL,
                 deciles = list(), tiers = list(),
                 feature_validation = if (is.null(validation)) NULL else feature_validation_telemetry(validation))
  if (n - ne < min_cell || ne < min_cell) {
    packet$suppressed <- TRUE
    packet["n_events"] <- list(if ((ne > 0 && ne < min_cell) || (n - ne > 0 && n - ne < min_cell)) NULL else ne)
    packet["n"] <- list(if (n >= min_cell) n else NULL)
  } else {
    d <- auroc_delong(y, p)
    cal <- .calibration_fit(y, p)
    packet$prevalence <- ne / n
    packet$metrics <- list(auroc = unname(d["auc"]), auroc_se = unname(d["se"]), auprc = average_precision(y, p),
                           brier = mean((p - y)^2), cal_slope = cal$slope, cal_slope_se = cal$slope_se,
                           cal_intercept = cal$intercept, cal_intercept_se = cal$intercept_se,
                           cal_in_the_large = cal$citl, mean_prediction = mean(p))
    groups <- split(order(p), cut(seq_len(n), breaks = 10, labels = FALSE))
    ev <- vapply(groups, function(g) as.integer(sum(y[g])), integer(1))
    nev <- vapply(groups, function(g) as.integer(length(g) - sum(y[g])), integer(1))
    he <- .suppress_mask(ev, min_cell); hn <- .suppress_mask(nev, min_cell)
    packet$deciles <- lapply(seq_along(groups), function(i) {
      g <- groups[[i]]; hide <- he[i] || hn[i]
      list(decile = i, n = if (hide) NULL else length(g), events = if (hide) NULL else ev[i],
           mean_pred = if (hide) NULL else mean(p[g]), observed_rate = if (hide) NULL else ev[i] / length(g))
    })
    packet$tiers <- lapply(tier_fractions, function(frac) {
      k <- max(1L, as.integer(round(frac * n)))
      top <- order(-p)[seq_len(k)]
      tp <- as.integer(sum(y[top])); fp <- k - tp
      hide <- (tp > 0 && tp < min_cell) || (fp > 0 && fp < min_cell) || (ne - tp > 0 && ne - tp < min_cell)
      list(top_fraction = frac, flagged = if (hide) NULL else k, true_positives = if (hide) NULL else tp,
           sensitivity = if (hide) NULL else tp / ne, ppv = if (hide || tp == 0) NULL else tp / k,
           nns = if (hide || tp == 0) NULL else k / tp)
    })
  }
  validate_packet(packet)
  if (!is.null(output)) {
    dir.create(dirname(output), showWarnings = FALSE, recursive = TRUE)
    jsonlite::write_json(packet, output, auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE)
  }
  packet
}

# ---- schema validation ----------------------------------------------------------------------------------

#' Load the telemetry packet JSON Schema
#' @return The schema as a list.
#' @export
load_packet_schema <- function() {
  jsonlite::fromJSON(system.file("extdata", "telemetry_packet.schema.json", package = "omopduckdb", mustWork = TRUE),
                     simplifyVector = FALSE)
}

.check_schema <- function(value, schema, path, errors) {
  p <- if (nzchar(path)) path else "$"
  types <- unlist(schema$type)
  if (!is.null(types)) {
    ok1 <- function(tt) switch(tt,
      "null" = is.null(value),
      "object" = is.list(value) && !is.null(names(value)) || (is.list(value) && length(value) == 0 && FALSE),
      "array" = is.list(value) && is.null(names(value)),
      "string" = is.character(value) && length(value) == 1L,
      "boolean" = is.logical(value) && length(value) == 1L && !is.na(value),
      "number" = is.numeric(value) && length(value) == 1L && is.finite(value),
      "integer" = is.numeric(value) && length(value) == 1L && is.finite(value) && value == floor(value),
      FALSE)
    if (!any(vapply(types, ok1, logical(1)))) {
      return(c(errors, sprintf("%s: expected %s", p, paste(types, collapse = "|"))))
    }
  }
  if (is.null(value)) return(errors)
  if (!is.null(schema$const) && !identical(value, schema$const)) errors <- c(errors, sprintf("%s: must equal %s", p, schema$const))
  if (!is.null(schema$enum) && !(value %in% unlist(schema$enum))) errors <- c(errors, sprintf("%s: not in enum", p))
  if (is.character(value) && !is.null(schema$pattern) && !grepl(schema$pattern, value, perl = TRUE)) {
    errors <- c(errors, sprintf("%s: does not match %s", p, schema$pattern))
  }
  if (is.numeric(value) && length(value) == 1L) {
    if (!is.null(schema$minimum) && value < schema$minimum) errors <- c(errors, sprintf("%s: below minimum", p))
    if (!is.null(schema$maximum) && value > schema$maximum) errors <- c(errors, sprintf("%s: above maximum", p))
  }
  if (is.list(value) && !is.null(names(value))) {
    for (r in unlist(schema$required)) if (!r %in% names(value)) errors <- c(errors, sprintf("%s: missing required '%s'", p, r))
    props <- schema$properties
    if (identical(schema$additionalProperties, FALSE)) {
      for (k in setdiff(names(value), names(props))) errors <- c(errors, sprintf("%s: unexpected property '%s'", p, k))
    }
    for (k in intersect(names(value), names(props))) {
      errors <- .check_schema(value[[k]], props[[k]], paste0(path, ".", k), errors)
    }
  }
  if (is.list(value) && is.null(names(value))) {
    if (!is.null(schema$maxItems) && length(value) > schema$maxItems) errors <- c(errors, sprintf("%s: too many items", p))
    if (!is.null(schema$items)) {
      for (i in seq_along(value)) errors <- .check_schema(value[[i]], schema$items, sprintf("%s[%d]", path, i), errors)
    }
  }
  errors
}

#' Validate a telemetry packet against the JSON Schema
#' @param packet A packet list (as returned by [site_telemetry()] or read with `jsonlite::fromJSON(simplifyVector = FALSE)`).
#' @return Invisibly `TRUE`; otherwise an error listing every violation.
#' @export
validate_packet <- function(packet) {
  # a packet round-tripped through JSON: normalise so numbers/NULL compare the same way
  packet <- jsonlite::fromJSON(jsonlite::toJSON(packet, auto_unbox = TRUE, null = "null", digits = NA),
                               simplifyVector = FALSE)
  errors <- .check_schema(packet, load_packet_schema(), "", character())
  if (length(errors)) stop("Invalid telemetry packet:\n  ", paste(errors, collapse = "\n  "), call. = FALSE)
  invisible(TRUE)
}

# ---- aggregator -----------------------------------------------------------------------------------------

.random_effects <- function(est, se) {
  k <- length(est); w <- 1 / se^2
  fixed <- sum(w * est) / sum(w)
  q <- sum(w * (est - fixed)^2); df <- k - 1L
  c_ <- sum(w) - sum(w^2) / sum(w)
  tau2 <- if (df > 0 && c_ > 0) max(0, (q - df) / c_) else 0
  ws <- 1 / (se^2 + tau2)
  pooled <- sum(ws * est) / sum(ws); pse <- sqrt(1 / sum(ws))
  list(k = k, fixed_effect = fixed, pooled = pooled, se = pse, ci95 = c(pooled - 1.96 * pse, pooled + 1.96 * pse),
       cochran_q = q, q_df = df, q_p = if (df > 0) stats::pchisq(q, df, lower.tail = FALSE) else NULL,
       i2_percent = if (q > 0 && df > 0) max(0, (q - df) / q) * 100 else 0, tau2 = tau2)
}

#' Pool the telemetry packets of many sites
#'
#' Validates every packet in `directory` and pools them with DerSimonian-Laird random effects (Cochran's Q, I^2,
#' tau^2). Mirror of `omop_etl.telemetry.aggregate_telemetry`.
#'
#' @param directory Directory of packet JSON files, one per site.
#' @param pattern Glob for the packet files.
#' @return A list: `sites`, `excluded` (suppressed sites), `n`, `n_events`, `auroc` and `cal_slope` random-effects
#'   summaries, n-weighted `brier`, `prevalence` and `auprc_weighted` (AUPRC does not pool exactly).
#' @export
aggregate_telemetry <- function(directory, pattern = "\\.json$") {
  files <- sort(list.files(directory, pattern = pattern, full.names = TRUE))
  if (!length(files)) stop("No telemetry packets found in ", directory, ".", call. = FALSE)
  packets <- lapply(files, function(f) {
    pk <- jsonlite::fromJSON(f, simplifyVector = FALSE)
    tryCatch(validate_packet(pk), error = function(e) stop(basename(f), ": ", conditionMessage(e), call. = FALSE))
    pk
  })
  ids <- vapply(packets, function(p) p$site_id, character(1))
  if (anyDuplicated(ids)) stop("Duplicate site_id among packets; each site must contribute one packet.", call. = FALSE)
  if (length(unique(vapply(packets, function(p) p$schema_version, character(1)))) != 1L) {
    stop("Packets use different schema versions.", call. = FALSE)
  }
  good <- Filter(function(p) !p$suppressed, packets)
  out <- list(schema_version = packets[[1]]$schema_version, sites = ids,
              excluded = ids[vapply(packets, function(p) p$suppressed, logical(1))],
              n = sum(vapply(good, function(p) p$n, numeric(1))),
              n_events = sum(vapply(good, function(p) p$n_events, numeric(1))))
  if (!length(good)) return(c(out, list(auroc = NULL, cal_slope = NULL, brier = NULL, prevalence = NULL, auprc_weighted = NULL)))
  w <- vapply(good, function(p) p$n, numeric(1))
  col <- function(key) vapply(good, function(p) { v <- p$metrics[[key]]; if (is.null(v)) NA_real_ else v }, numeric(1))
  out$auroc <- if (length(good) > 1) .random_effects(col("auroc"), pmax(col("auroc_se"), 1e-9)) else
    list(k = 1L, pooled = col("auroc"), se = col("auroc_se"), i2_percent = 0, tau2 = 0)
  sl <- col("cal_slope")
  out["cal_slope"] <- list(if (!anyNA(sl) && length(good) > 1) .random_effects(sl, pmax(col("cal_slope_se"), 1e-9)) else NULL)
  out$brier <- sum(w * col("brier")) / sum(w)
  out$prevalence <- out$n_events / sum(w)
  out$auprc_weighted <- sum(w * col("auprc")) / sum(w)
  out
}
