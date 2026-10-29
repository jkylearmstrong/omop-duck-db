# Compare feature sets and model runs without sharing patient rows (the R twin of python/omop_etl/model_ablation.py; same metrics,
# same paired bootstrap, same tests).

.ABLATION_METRICS <- c("auroc", "auprc", "brier")

.m_auroc <- function(y, p) {
  n1 <- sum(y == 1); n0 <- length(y) - n1
  if (n1 == 0 || n0 == 0) return(NA_real_)
  r <- rank(p, ties.method = "average")
  (sum(r[y == 1]) - n1 * (n1 + 1) / 2) / (n1 * n0)
}

# average precision: the mean precision at each positive when patients are ranked by score (ties broken by order)
.m_auprc <- function(y, p) {
  n1 <- sum(y == 1)
  if (n1 == 0 || n1 == length(y)) return(NA_real_)
  ys <- y[order(-p)]
  sum((cumsum(ys) / seq_along(ys))[ys == 1]) / n1
}

.m_brier <- function(y, p) mean((p - y)^2)

.METRIC_FN <- list(auroc = .m_auroc, auprc = .m_auprc, brier = .m_brier)

#' Ridge-regularised logistic regression for the ablation tools
#'
#' Newton / IRLS on standardised columns; missing values take the training mean; the intercept is not penalised. It needs nothing
#' beyond base R and is the default `fit_predict` of [ablate_feature_groups()].
#'
#' @param x_train,x_test Numeric matrices (or data frames) with the same columns.
#' @param y_train 0/1 outcome of the training rows.
#' @param l2 Ridge penalty.
#' @param max_iter Maximum Newton steps.
#' @return Predicted probabilities for the rows of `x_test`.
#' @export
ridge_logistic_fit_predict <- function(x_train, y_train, x_test, l2 = 1, max_iter = 50L) {
  xtr <- as.matrix(x_train); xte <- as.matrix(x_test)
  mu <- colMeans(xtr, na.rm = TRUE); mu[is.nan(mu)] <- 0
  fill <- function(m) { idx <- which(is.na(m), arr.ind = TRUE); if (nrow(idx)) m[idx] <- mu[idx[, 2]]; m }
  xtr <- fill(xtr); xte <- fill(xte)
  sdv <- apply(xtr, 2, stats::sd); sdv[is.na(sdv) | sdv == 0] <- 1
  xtr <- sweep(sweep(xtr, 2, mu), 2, sdv, "/"); xte <- sweep(sweep(xte, 2, mu), 2, sdv, "/")
  a <- cbind(1, xtr)
  b <- numeric(ncol(a))
  pen <- diag(l2, ncol(a)); pen[1, 1] <- 0
  y <- as.numeric(y_train)
  for (i in seq_len(max_iter)) {
    z <- pmin(pmax(as.vector(a %*% b), -30), 30)
    p <- 1 / (1 + exp(-z))
    w <- pmax(p * (1 - p), 1e-6)
    grad <- crossprod(a, p - y) + pen %*% b
    hess <- crossprod(a, a * w) + pen
    step <- as.vector(solve(hess, grad))
    b <- b - step
    if (max(abs(step)) < 1e-8) break
  }
  z <- pmin(pmax(as.vector(cbind(1, xte) %*% b), -30), 30)
  1 / (1 + exp(-z))
}

.ablate_folds <- function(y, k) {
  fold <- integer(length(y))
  for (cls in 0:1) {
    idx <- which(y == cls)
    idx <- idx[sample.int(length(idx))]
    fold[idx] <- (seq_along(idx) - 1L) %% k
  }
  fold
}

.ablate_oof <- function(x, y, fold, fit_predict) {
  out <- numeric(length(y))
  for (f in unique(fold)) {
    test <- fold == f
    out[test] <- as.numeric(fit_predict(x[!test, , drop = FALSE], y[!test], x[test, , drop = FALSE]))
  }
  out
}

# differences b - a of each metric over n_boot resamples of the patients (the same resample for both)
.paired_bootstrap <- function(y, p_a, p_b, metrics, n_boot) {
  out <- stats::setNames(lapply(metrics, function(m) numeric(0)), metrics)
  n <- length(y)
  for (b in seq_len(n_boot)) {
    i <- sample.int(n, n, replace = TRUE)
    yy <- y[i]
    if (min(yy) == max(yy)) next
    for (m in metrics) out[[m]] <- c(out[[m]], .METRIC_FN[[m]](yy, p_b[i]) - .METRIC_FN[[m]](yy, p_a[i]))
  }
  out
}

.boot_interval <- function(draws, level) {
  if (!length(draws)) return(c(NA_real_, NA_real_, NA_real_))
  q <- stats::quantile(draws, c((1 - level) / 2, (1 + level) / 2), names = FALSE)
  c(q[[1]], q[[2]], min(2 * min(mean(draws <= 0), mean(draws >= 0)), 1))
}

.with_seed <- function(seed, expr_fn) {
  had <- exists(".Random.seed", envir = globalenv(), inherits = FALSE)
  old <- if (had) get(".Random.seed", envir = globalenv()) else NULL
  on.exit(if (had) assign(".Random.seed", old, envir = globalenv()) else if (exists(".Random.seed", envir = globalenv(), inherits = FALSE)) rm(".Random.seed", envir = globalenv()), add = TRUE)
  set.seed(seed)
  expr_fn()
}

#' How much does each group of features contribute?
#'
#' Refits a model with each named group of columns removed (or kept alone) in cross-validation and reports the change in AUROC, AUPRC
#' and Brier score with a paired bootstrap interval. A group is any list of columns: RUCA/SDI, a site's Charlson index beside the
#' standard one, velocity features. Only aggregate numbers are returned, so a site can send the result back.
#'
#' Groups may overlap; columns outside every group stay in all models. The full model is refit with each group removed (`mode = "drop"`),
#' kept alone together with the ungrouped columns (`"only"`), or both (`"both"`), on the same stratified folds, so every difference is paired.
#' Differences are `without - full` for `"drop"` (negative = the group helps for AUROC/AUPRC, positive for Brier) and `only - full` for
#' `"only"`; the interval is a paired bootstrap over patients of the out-of-fold predictions and `p_boot` is the two-sided share of
#' resamples on the other side of zero.
#'
#' @param features A data frame with one numeric column per feature.
#' @param outcome 0/1 outcome per row.
#' @param groups Named list: group name -> character vector of columns of `features`.
#' @param fit_predict `function(x_train, y_train, x_test)` returning probabilities; default [ridge_logistic_fit_predict()].
#' @param mode `"drop"`, `"only"` or `"both"`.
#' @param folds Cross-validation folds.
#' @param n_boot Bootstrap resamples.
#' @param ci_level Interval level.
#' @param seed Random seed (the caller's RNG state is restored).
#' @return A data frame with one row per `(mode, group)` plus the full model (`mode = "full"`): `n_features`, `n`, `events`, and for
#'   `auroc`, `auprc`, `brier` the columns `*_full`, `*_variant`, `*_delta`, `*_delta_lo`, `*_delta_hi`, `*_p_boot`.
#' @export
ablate_feature_groups <- function(features, outcome, groups, fit_predict = NULL, mode = "drop", folds = 5L, n_boot = 200L,
                                  ci_level = 0.95, seed = 0) {
  if (!(mode %in% c("drop", "only", "both"))) stop("mode must be 'drop', 'only' or 'both'", call. = FALSE)
  y <- as.numeric(outcome)
  if (length(y) != nrow(features)) stop("outcome and features must have the same number of rows", call. = FALSE)
  if (anyNA(y) || !all(y %in% c(0, 1))) stop("outcome must be 0/1 with no missing values", call. = FALSE)
  if (sum(y) < folds || sum(1 - y) < folds) stop(sprintf("need at least %d events and %d non-events for %d-fold cross-validation", folds, folds, folds), call. = FALSE)
  cols <- names(features)
  for (nm in names(groups)) {
    missing <- setdiff(groups[[nm]], cols)
    if (length(missing)) stop(sprintf("group '%s' names columns that are not in features: %s", nm, paste(missing, collapse = ", ")), call. = FALSE)
    if (!length(groups[[nm]])) stop(sprintf("group '%s' is empty", nm), call. = FALSE)
  }
  fit <- fit_predict %||% ridge_logistic_fit_predict
  .with_seed(seed, function() {
    fold <- .ablate_folds(y, folds)
    base <- setdiff(cols, unlist(groups))
    run <- function(use) {
      if (!length(use)) return(rep(mean(y), length(y)))
      .ablate_oof(as.matrix(features[use]), y, fold, fit)
    }
    full_pred <- run(cols)
    variants <- list()
    for (nm in names(groups)) {
      if (mode %in% c("drop", "both")) variants[[length(variants) + 1L]] <- list("drop", nm, setdiff(cols, groups[[nm]]))
      if (mode %in% c("only", "both")) variants[[length(variants) + 1L]] <- list("only", nm, c(base, setdiff(groups[[nm]], base)))
    }
    full_row <- data.frame(mode = "full", group = "(all features)", n_features = length(cols), n = length(y), events = sum(y), stringsAsFactors = FALSE)
    for (m in .ABLATION_METRICS) full_row[[paste0(m, "_full")]] <- .METRIC_FN[[m]](y, full_pred)
    rows <- list(full_row)
    for (v in variants) {
      pred <- run(v[[3]])
      draws <- .paired_bootstrap(y, full_pred, pred, .ABLATION_METRICS, n_boot)
      row <- data.frame(mode = v[[1]], group = v[[2]], n_features = length(v[[3]]), n = length(y), events = sum(y), stringsAsFactors = FALSE)
      for (m in .ABLATION_METRICS) {
        iv <- .boot_interval(draws[[m]], ci_level)
        fv <- .METRIC_FN[[m]](y, full_pred); vv <- .METRIC_FN[[m]](y, pred)
        row[[paste0(m, "_full")]] <- fv; row[[paste0(m, "_variant")]] <- vv; row[[paste0(m, "_delta")]] <- vv - fv
        row[[paste0(m, "_delta_lo")]] <- iv[[1]]; row[[paste0(m, "_delta_hi")]] <- iv[[2]]; row[[paste0(m, "_p_boot")]] <- iv[[3]]
      }
      rows[[length(rows) + 1L]] <- row
    }
    cols_all <- unique(unlist(lapply(rows, names)))
    out <- do.call(rbind, lapply(rows, function(r) { for (c in setdiff(cols_all, names(r))) r[[c]] <- NA_real_; r[cols_all] }))
    rownames(out) <- NULL
    out
  })
}

#' Are two sets of predictions for the same patients really different?
#'
#' Each run is a data frame with a patient id, a predicted probability and (in at least one of them) the 0/1 outcome. Patients are
#' matched on `id_col`; those in only one run are counted and left out. A 0.004 AUROC gap whose interval spans zero is not a
#' difference. No patient-level rows are returned.
#'
#' @param run_a,run_b Data frames, one row per patient.
#' @param id_col,pred_col,outcome_col Column names.
#' @param run_names Labels of the two runs.
#' @param n_boot Bootstrap resamples.
#' @param ci_level Interval level.
#' @param top_fraction Share of patients flagged as highest risk for the overlap measure.
#' @param seed Random seed (the caller's RNG state is restored).
#' @return A list: `n` (patients in both runs, in only one, events); `metrics` (AUROC, AUPRC, Brier, mean predicted and observed risk of
#'   each run); `difference` (B - A of each metric with a paired bootstrap interval and `p_boot`); `agreement` (Spearman correlation,
#'   mean absolute difference in predicted risk, share of the top `top_fraction` that both runs flag).
#' @export
compare_model_runs <- function(run_a, run_b, id_col = "person_id", pred_col = "pred", outcome_col = "outcome", run_names = c("A", "B"),
                               n_boot = 500L, ci_level = 0.95, top_fraction = 0.1, seed = 0) {
  for (lab in c("run_a", "run_b")) {
    d <- get(lab)
    for (c in c(id_col, pred_col)) if (!(c %in% names(d))) stop(sprintf("%s has no column '%s'", lab, c), call. = FALSE)
    if (anyDuplicated(d[[id_col]])) stop(sprintf("%s has duplicate values in '%s'; one row per patient", lab, id_col), call. = FALSE)
  }
  a <- data.frame(id = run_a[[id_col]], pa = as.numeric(run_a[[pred_col]]))
  if (outcome_col %in% names(run_a)) a$ya <- run_a[[outcome_col]]
  b <- data.frame(id = run_b[[id_col]], pb = as.numeric(run_b[[pred_col]]))
  if (outcome_col %in% names(run_b)) b$yb <- run_b[[outcome_col]]
  both <- merge(a, b, by = "id")
  if (!("ya" %in% names(both)) && !("yb" %in% names(both))) stop(sprintf("neither run has an outcome column '%s'", outcome_col), call. = FALSE)
  if (all(c("ya", "yb") %in% names(both)) && !all(both$ya == both$yb)) stop("the two runs disagree on the outcome of some patients", call. = FALSE)
  if (!nrow(both)) stop("the two runs have no patient in common", call. = FALSE)
  y <- as.numeric(if ("ya" %in% names(both)) both$ya else both$yb)
  pa <- both$pa; pb <- both$pb
  boot <- .with_seed(seed, function() .paired_bootstrap(y, pa, pb, .ABLATION_METRICS, n_boot))
  metrics <- data.frame(run = run_names, auroc = c(.m_auroc(y, pa), .m_auroc(y, pb)), auprc = c(.m_auprc(y, pa), .m_auprc(y, pb)),
                        brier = c(.m_brier(y, pa), .m_brier(y, pb)), mean_predicted = c(mean(pa), mean(pb)), observed = mean(y), stringsAsFactors = FALSE)
  diff_rows <- lapply(.ABLATION_METRICS, function(m) {
    iv <- .boot_interval(boot[[m]], ci_level)
    r <- data.frame(metric = m, a = .METRIC_FN[[m]](y, pa), b = .METRIC_FN[[m]](y, pb), stringsAsFactors = FALSE)
    names(r)[2:3] <- run_names
    r$delta <- r[[3]] - r[[2]]; r$delta_lo <- iv[[1]]; r$delta_hi <- iv[[2]]; r$p_boot <- iv[[3]]
    r
  })
  k <- max(1L, as.integer(round(top_fraction * length(y))))
  top_a <- order(-pa)[seq_len(k)]; top_b <- order(-pb)[seq_len(k)]
  agreement <- list(spearman = if (length(y) > 1 && stats::sd(pa) > 0 && stats::sd(pb) > 0) stats::cor(rank(pa), rank(pb)) else NA_real_,
                    mean_abs_diff = mean(abs(pa - pb)), top_fraction = top_fraction, top_overlap = length(intersect(top_a, top_b)) / k)
  n <- stats::setNames(list(nrow(both), nrow(a) - nrow(both), nrow(b) - nrow(both), as.integer(sum(y))),
                       c("common", paste0("only_", run_names[[1]]), paste0("only_", run_names[[2]]), "events"))
  list(n = n, metrics = metrics, difference = do.call(rbind, diff_rows), agreement = agreement)
}
