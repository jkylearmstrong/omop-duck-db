# Leave-one-site-out transport evaluation, probability recalibration and capacity thresholds (v0.13.0).
# Mirror of python/omop_etl/transport.py (the logistic model only: R has no xgboost dependency here).

.tr_eps <- 1e-7
.tr_logit <- function(p) { p <- pmin(pmax(as.numeric(p), .tr_eps), 1 - .tr_eps); log(p / (1 - p)) }
.tr_expit <- function(z) 1 / (1 + exp(-pmin(pmax(z, -30), 30)))

.tr_binary <- function(y) {
  a <- as.numeric(y)
  if (!length(a) || anyNA(a) || !all(a %in% c(0, 1))) stop("y must be a non-empty 0/1 vector without missing values.", call. = FALSE)
  a
}

.tr_prob <- function(p) {
  a <- as.numeric(p)
  if (anyNA(a) || any(a < 0) || any(a > 1)) stop("probabilities must be in [0, 1] with no missing values.", call. = FALSE)
  a
}

# Logistic calibration logit(y) ~ intercept + slope * logit(p) by IRLS (same iteration as the Python helper).
.tr_fit_logistic_calibration <- function(y, p) {
  z <- .tr_logit(p)
  x <- cbind(1, z)
  b <- c(0, 1)
  for (i in seq_len(25)) {
    ph <- .tr_expit(as.vector(x %*% b))
    w <- pmin(pmax(ph * (1 - ph), 1e-6), 0.25)
    grad <- crossprod(x, y - ph)
    hess <- -crossprod(x * w, x) - diag(1e-6, 2)
    step <- tryCatch(solve(-hess, grad), error = function(e) NULL)
    if (is.null(step)) break
    b <- b + as.vector(step)
    if (max(abs(step)) < 1e-5) break
  }
  as.numeric(b)
}

#' Platt scaling of probabilities
#'
#' `platt_scaling_fit()` fits `logit(p') = intercept + slope * logit(p)` by maximum likelihood; `platt_scaling_apply()`
#' applies it.
#'
#' @param y 0/1 outcomes
#' @param p predicted probabilities
#' @param params list with `intercept` and `slope`.
#' @return `platt_scaling_fit()`: list(intercept, slope); `platt_scaling_apply()`: numeric vector.
#' @export
platt_scaling_fit <- function(y, p) {
  yy <- .tr_binary(y); pp <- .tr_prob(p)
  if (length(yy) != length(pp)) stop("y and p must have the same length.", call. = FALSE)
  if (min(yy) == max(yy)) stop("Platt scaling needs both outcome classes.", call. = FALSE)
  b <- .tr_fit_logistic_calibration(yy, pp)
  list(intercept = b[[1]], slope = b[[2]])
}

#' @rdname platt_scaling_fit
#' @export
platt_scaling_apply <- function(p, params) .tr_expit(params$intercept + params$slope * .tr_logit(.tr_prob(p)))

#' Isotonic recalibration (pool-adjacent-violators)
#'
#'
#' @param y 0/1 outcomes
#' @param p predicted probabilities
#' @param params list with knots `x` and `y`.
#' @return `isotonic_fit()`: list(x, y) step-function knots; `isotonic_apply()`: numeric vector.
#' @export
isotonic_fit <- function(y, p) {
  yy <- .tr_binary(y); pp <- .tr_prob(p)
  if (length(yy) != length(pp)) stop("y and p must have the same length.", call. = FALSE)
  o <- order(pp)
  x <- pp[o]; t <- yy[o]
  ux <- unique(x)
  idx <- match(x, ux)
  w <- as.numeric(tabulate(idx, length(ux)))
  m <- as.numeric(tapply(t, idx, sum)) / w
  mean_v <- numeric(0); wt <- numeric(0); lo <- numeric(0); hi <- numeric(0)
  for (i in seq_along(ux)) {
    mean_v <- c(mean_v, m[[i]]); wt <- c(wt, w[[i]]); lo <- c(lo, ux[[i]]); hi <- c(hi, ux[[i]])
    while (length(mean_v) > 1 && mean_v[length(mean_v) - 1] >= mean_v[length(mean_v)]) {
      k <- length(mean_v)
      tw <- wt[k - 1] + wt[k]
      mean_v[k - 1] <- (mean_v[k - 1] * wt[k - 1] + mean_v[k] * wt[k]) / tw
      wt[k - 1] <- tw
      hi[k - 1] <- hi[k]
      mean_v <- mean_v[-k]; wt <- wt[-k]; lo <- lo[-k]; hi <- hi[-k]
    }
  }
  list(x = as.vector(rbind(lo, hi)), y = as.vector(rbind(mean_v, mean_v)))
}

#' @rdname isotonic_fit
#' @export
isotonic_apply <- function(p, params) stats::approx(params$x, params$y, xout = .tr_prob(p), rule = 2, ties = "ordered")$y

#' Prior-shift correction between a training and a target prevalence
#'
#' Adds the log odds ratio of the two base rates to the log odds, so scores of a model trained on a balanced
#' sample (prevalence 0.5) map to the natural incidence.
#'
#' @param p probabilities
#' @param train_prevalence,target_prevalence base rates in (0, 1).
#' @export
adjust_for_prevalence <- function(p, train_prevalence, target_prevalence) {
  for (v in list(train_prevalence, target_prevalence)) if (!(v > 0 && v < 1)) stop("prevalences must be in (0, 1).", call. = FALSE)
  shift <- log(target_prevalence / (1 - target_prevalence)) - log(train_prevalence / (1 - train_prevalence))
  .tr_expit(.tr_logit(.tr_prob(p)) + shift)
}

#' Fit a recalibration map
#'
#' @param method `"none"`, `"prior_shift"`, `"platt"` or `"isotonic"`. `platt` / `isotonic` need `y` and `p` at natural
#'   prevalence; `prior_shift` needs the two prevalences.
#'
#' @param y,p outcomes and probabilities
#' @param train_prevalence,target_prevalence base rates.
#' @return A list of class `omop_recalibrator` with `method`, `params` and a `predict` function.
#' @export
fit_recalibrator <- function(method, y = NULL, p = NULL, train_prevalence = NULL, target_prevalence = NULL) {
  method <- tolower(method)
  params <- switch(method,
    none = list(),
    platt = platt_scaling_fit(y, p),
    isotonic = isotonic_fit(y, p),
    prior_shift = {
      if (is.null(train_prevalence) || is.null(target_prevalence)) stop("prior_shift needs train_prevalence and target_prevalence.", call. = FALSE)
      list(train_prevalence = as.numeric(train_prevalence), target_prevalence = as.numeric(target_prevalence))
    },
    stop("calibration must be 'none', 'prior_shift', 'platt' or 'isotonic'.", call. = FALSE))
  predict <- switch(method,
    none = function(p) .tr_prob(p),
    platt = function(p) platt_scaling_apply(p, params),
    isotonic = function(p) isotonic_apply(p, params),
    prior_shift = function(p) adjust_for_prevalence(p, params$train_prevalence, params$target_prevalence))
  structure(list(method = method, params = params, predict = predict), class = "omop_recalibrator")
}

#' Probability cut-off that flags the riskiest share
#'
#'
#' @param prob probabilities
#' @param top_fraction share in (0, 1) to flag, e.g. 0.10. Flag `p >= cutoff`; ties cannot be split.
#' @export
capacity_threshold <- function(prob, top_fraction) {
  if (!(top_fraction > 0 && top_fraction < 1)) stop("top_fraction must be in (0, 1).", call. = FALSE)
  p <- .tr_prob(prob)
  if (!length(p)) stop("prob is empty.", call. = FALSE)
  as.numeric(stats::quantile(p, 1 - top_fraction, type = 7, names = FALSE))
}

#' Two risk tiers whose cut-point is set by capacity
#'
#'
#' @param reference_prob scores used to place the cut-point
#' @param top_fraction share in the high tier.
#'
#' @param high_name,low_name tier names
#' @return Thresholds for [map_risk_tiers()].
#' @export
capacity_risk_tiers <- function(reference_prob, top_fraction = 0.10, high_name = "high_risk", low_name = "standard") {
  fit_capacity_thresholds(reference_prob, list(list(name = high_name, top_fraction = top_fraction), list(name = low_name)))
}

#' What flagging the riskiest share achieves
#'
#'
#' @param y outcomes
#' @param prob probabilities
#' @param top_fraction share flagged
#' @param cutoff optional frozen cut-off.
#' @return list(cutoff, n_flagged, share_flagged, ppv, sensitivity, lift).
#' @export
evaluate_at_capacity <- function(y, prob, top_fraction = 0.10, cutoff = NULL) {
  yy <- .tr_binary(y); pp <- .tr_prob(prob)
  cut <- if (is.null(cutoff)) capacity_threshold(pp, top_fraction) else as.numeric(cutoff)
  flag <- pp >= cut
  tp <- sum(yy[flag] == 1)
  prev <- mean(yy)
  ppv <- if (any(flag)) tp / sum(flag) else NaN
  list(cutoff = cut, n_flagged = sum(flag), share_flagged = mean(flag), ppv = ppv,
       sensitivity = if (sum(yy)) tp / sum(yy) else NaN, lift = if (prev > 0) ppv / prev else NaN)
}

.tr_auroc <- function(y, s) {
  n1 <- sum(y); n0 <- length(y) - n1
  if (n1 == 0 || n0 == 0) return(NaN)
  r <- rank(s, ties.method = "average")
  (sum(r[y == 1]) - n1 * (n1 + 1) / 2) / (n1 * n0)
}

.tr_auprc <- function(y, s) {
  if (sum(y) == 0) return(NaN)
  o <- order(-s)
  ys <- y[o]; ss <- s[o]
  tp <- cumsum(ys)
  last <- c(ss[-1] != ss[-length(ss)], TRUE)
  tp_t <- tp[last]; n_t <- seq_along(ys)[last]
  prec <- tp_t / n_t; rec <- tp_t / sum(y)
  sum(diff(c(0, rec)) * prec)
}

.tr_metrics <- function(y, p, fractions) {
  out <- list(auroc = .tr_auroc(y, p), auprc = .tr_auprc(y, p), brier = mean((p - y)^2),
              oe_ratio = if (sum(p) > 0) sum(y) / sum(p) else NaN)
  if (sum(y) > 0 && sum(y) < length(y)) {
    ab <- .tr_fit_logistic_calibration(y, p)
    out$calibration_intercept <- ab[[1]]; out$calibration_slope <- ab[[2]]
  } else {
    out$calibration_intercept <- NaN; out$calibration_slope <- NaN
  }
  for (f in fractions) {
    r <- evaluate_at_capacity(y, p, f)
    tag <- sprintf("top%d", as.integer(round(f * 100)))
    out[[paste0(tag, "_ppv")]] <- r$ppv; out[[paste0(tag, "_sensitivity")]] <- r$sensitivity; out[[paste0(tag, "_lift")]] <- r$lift
  }
  out
}

.tr_balance <- function(y, ratio) {
  if (is.null(ratio)) return(seq_along(y))
  if (!(ratio > 0)) stop("balance_ratio must be positive (or NULL to train on the natural sample).", call. = FALSE)
  pos <- which(y == 1); neg <- which(y == 0)
  if (length(pos) <= length(neg)) { minority <- pos; majority <- neg } else { minority <- neg; majority <- pos }
  take <- min(length(majority), as.integer(round(ratio * length(minority))))
  sort(c(minority, majority[sample.int(length(majority), take)]))
}

#' Leave-one-site-out transport evaluation
#'
#' Trains on every site but one (optionally on a balanced sample), scores the held-out site at its natural prevalence,
#' maps the scores back to the natural incidence and reports discrimination, calibration and top-k performance with
#' bootstrap intervals. The model is the ridge logistic regression of [ridge_logistic_fit_predict()].
#'
#' @param features_df data frame with the site column, the 0/1 label and numeric features.
#'
#' @param site_col site column
#' @param feature_cols features (default: numeric columns other than label, site and ids/dates).
#'
#' @param label_col label column
#' @param balance_ratio majority rows per minority row in training (1 = 50/50); `NULL` = natural.
#' @param score_natural score every row of the held-out site (`TRUE`) or a balanced subsample (diagnostic only).
#' @param calibration `"prior_shift"` (default), `"platt"`, `"isotonic"` or `"none"`. `platt` / `isotonic` are fitted on the
#'   training sites' natural data scored by the model (in-sample for those rows).
#' @param n_bootstraps bootstrap resamples for 95 percent intervals (0 = none).
#'
#' @param capacity_fractions top-k shares flagged
#' @param random_state seed
#' @param keep_predictions keep row-level scores.
#' @param l2 ridge penalty.
#' @return A list of class `omop_loso_transport` with `per_site`, `summary`, `predictions` and `settings`.
#'   The R sampling and bootstrap draws come from R's RNG, so they differ from the Python twin for the same seed.
#' @export
evaluate_loso_transport <- function(features_df, site_col = "site_anon", feature_cols = NULL, label_col = "y",
                                    balance_ratio = 1.0, score_natural = TRUE, calibration = "prior_shift",
                                    n_bootstraps = 100L, capacity_fractions = 0.10, random_state = 20261104,
                                    keep_predictions = FALSE, l2 = 1) {
  if (!all(c(site_col, label_col) %in% names(features_df))) {
    stop(sprintf("features_df needs the columns '%s' and '%s'.", site_col, label_col), call. = FALSE)
  }
  df <- as.data.frame(features_df)
  skip <- c(site_col, label_col, "person_id", "visit_occurrence_id", "index_date", "discharge_date", "site", "site_anon")
  cols <- if (!is.null(feature_cols)) feature_cols else names(df)[vapply(df, is.numeric, logical(1)) & !(names(df) %in% skip)]
  if (!length(cols)) stop("No feature columns.", call. = FALSE)
  if (!calibration %in% c("prior_shift", "platt", "isotonic", "none")) {
    stop("calibration must be 'prior_shift', 'platt', 'isotonic' or 'none'.", call. = FALSE)
  }
  y_all <- .tr_binary(df[[label_col]])
  sites <- sort(unique(as.character(df[[site_col]][!is.na(df[[site_col]])])))
  if (length(sites) < 2) stop("Leave-one-site-out needs at least two sites.", call. = FALSE)
  X <- as.matrix(df[, cols, drop = FALSE])
  storage.mode(X) <- "double"
  rows <- list(); preds <- list()
  for (k in seq_along(sites)) {
    site <- sites[[k]]
    is_test <- as.character(df[[site_col]]) == site & !is.na(df[[site_col]])
    is_train <- !is_test & !is.na(df[[site_col]])
    ytr_nat <- y_all[is_train]; yte <- y_all[is_test]
    if (min(ytr_nat) == max(ytr_nat)) stop(sprintf("Training sites for held-out site '%s' contain a single outcome class.", site), call. = FALSE)
    set.seed(as.integer((random_state + k) %% .Machine$integer.max))
    idx <- .tr_balance(ytr_nat, balance_ratio)
    xtr_nat <- X[is_train, , drop = FALSE]
    xtr <- xtr_nat[idx, , drop = FALSE]; ytr <- ytr_nat[idx]
    xte <- X[is_test, , drop = FALSE]
    kept <- which(is_test)
    if (!isTRUE(score_natural)) {
      sub <- .tr_balance(yte, if (is.null(balance_ratio)) 1 else balance_ratio)
      xte <- xte[sub, , drop = FALSE]; yte <- yte[sub]; kept <- kept[sub]
    }
    both <- ridge_logistic_fit_predict(xtr, ytr, rbind(xtr_nat, xte), l2 = l2)
    p_train_nat <- both[seq_len(nrow(xtr_nat))]
    p_raw <- both[-seq_len(nrow(xtr_nat))]
    nat_prev <- mean(ytr_nat); bal_prev <- mean(ytr)
    recal <- if (calibration == "prior_shift") {
      if (abs(nat_prev - bal_prev) > 1e-12) fit_recalibrator("prior_shift", train_prevalence = bal_prev, target_prevalence = nat_prev)
      else fit_recalibrator("none")
    } else if (calibration %in% c("platt", "isotonic")) {
      fit_recalibrator(calibration, y = ytr_nat, p = p_train_nat)
    } else fit_recalibrator("none")
    p <- recal$predict(p_raw)
    row <- c(list(site = site, n = length(yte), n_events = sum(yte), prevalence = mean(yte),
                  train_prevalence_natural = nat_prev, train_prevalence_balanced = bal_prev),
             .tr_metrics(yte, p, capacity_fractions))
    if (length(yte) && n_bootstraps > 0) {
      set.seed(as.integer((random_state + 10000L + k) %% .Machine$integer.max))
      boot <- lapply(seq_len(n_bootstraps), function(i) {
        bi <- sample.int(length(yte), length(yte), replace = TRUE)
        .tr_metrics(yte[bi], p[bi], capacity_fractions)
      })
      bdf <- do.call(rbind, lapply(boot, function(b) as.data.frame(b)))
      for (nm in names(bdf)) {
        row[[paste0(nm, "_lo")]] <- as.numeric(stats::quantile(bdf[[nm]], 0.025, na.rm = TRUE, names = FALSE))
        row[[paste0(nm, "_hi")]] <- as.numeric(stats::quantile(bdf[[nm]], 0.975, na.rm = TRUE, names = FALSE))
      }
    }
    rows[[k]] <- as.data.frame(row, stringsAsFactors = FALSE)
    if (isTRUE(keep_predictions)) preds[[k]] <- data.frame(row = kept, site = site, y = yte, p_raw = p_raw, p = p)
  }
  per_site <- do.call(rbind, rows)
  mcols <- setdiff(names(per_site), c("site", "n", "n_events"))
  mcols <- mcols[!grepl("(_lo|_hi)$", mcols) & !grepl("^train_", mcols)]
  summary <- data.frame(metric = mcols,
                        mean = vapply(mcols, function(m) mean(per_site[[m]], na.rm = TRUE), numeric(1)),
                        min = vapply(mcols, function(m) min(per_site[[m]], na.rm = TRUE), numeric(1)),
                        max = vapply(mcols, function(m) max(per_site[[m]], na.rm = TRUE), numeric(1)),
                        row.names = NULL, stringsAsFactors = FALSE)
  structure(list(per_site = per_site, summary = summary,
                 predictions = if (length(preds)) do.call(rbind, preds) else NULL,
                 settings = list(model_type = "logistic", balance_ratio = balance_ratio, score_natural = score_natural,
                                 calibration = calibration, n_bootstraps = n_bootstraps, capacity_fractions = capacity_fractions,
                                 features = cols, random_state = random_state)),
            class = "omop_loso_transport")
}
