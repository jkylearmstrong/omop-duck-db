#' Production scoring bundle with a train/serve skew guard
#'
#' Ties a fitted imputer, scaler and model to the ORDERED feature schema they were trained on, and refuses to score a
#' data frame whose columns do not match that schema. It is model-agnostic: the stages are plain functions (or lists
#' of functions), so any R modelling package works. Mirror of `omop_etl.scoring.OMOPScoringPipeline` in Python.
#'
#' Pair it with the leak-free feature extractors (`extract_elixhauser_comorbidities(exclude_index_diagnoses = TRUE)`,
#' [extract_prior_utilization()]) so the features available at training time are the ones available at the bedside.
#'
#' Bundles are saved with [base::saveRDS()]; only load files you created or trust.
#'
#' @param predict_fn `function(x)` taking the transformed numeric matrix and returning the positive-class probability
#'   (one value per row).
#' @param feature_names Ordered feature columns the stages were fitted on.
#' @param impute_fn,scale_fn Optional `function(x)` applied (imputer first, then scaler) to the numeric matrix. They
#'   must be closures over the values learned at fit time.
#' @param metadata Free-form list stored with the bundle (cohort definition, git sha, training dates, ...).
#' @param strict If `TRUE` (default) columns not in the schema raise an error instead of being dropped.
#' @param allow_missing_values If `FALSE` (default) `NA` in the input raises unless `impute_fn` is given.
#' @param ranges Optional named list `feature -> c(min, max)` seen at fit time (used by
#'   [scoring_pipeline_out_of_range()]).
#' @return An object of class `omop_scoring_pipeline`.
#' @export
omop_scoring_pipeline <- function(predict_fn, feature_names, impute_fn = NULL, scale_fn = NULL, metadata = list(),
                                  strict = TRUE, allow_missing_values = !is.null(impute_fn), ranges = list()) {
  feature_names <- as.character(feature_names)
  if (length(feature_names) == 0) stop("feature_names must not be empty.", call. = FALSE)
  if (anyDuplicated(feature_names)) {
    stop("feature_names contains duplicates: ", paste(unique(feature_names[duplicated(feature_names)]), collapse = ", "),
         call. = FALSE)
  }
  if (!is.function(predict_fn)) stop("predict_fn must be a function.", call. = FALSE)
  structure(list(predict_fn = predict_fn, feature_names = feature_names, impute_fn = impute_fn, scale_fn = scale_fn,
                 metadata = metadata, strict = isTRUE(strict), allow_missing_values = isTRUE(allow_missing_values),
                 ranges = ranges, bundle_version = 1L),
            class = "omop_scoring_pipeline")
}

#' Fit a scoring pipeline on a training data frame
#'
#' Records the feature schema and per-feature training ranges, then calls `fit_model(x, y)` on the (optionally
#' imputed and scaled) numeric matrix. Imputation and scaling use column means / standard deviations learned on `x`.
#'
#' @param x Training data frame (numeric feature columns).
#' @param y Outcome vector.
#' @param fit_model `function(x, y)` returning a `function(x)` that gives the positive-class probability.
#' @param feature_names Columns of `x` to use (default: all, in order).
#' @param impute If `TRUE`, missing values are replaced by the training column mean.
#' @param scale If `TRUE`, features are standardised with the training mean and standard deviation.
#' @inheritParams omop_scoring_pipeline
#' @return An `omop_scoring_pipeline`.
#' @export
fit_scoring_pipeline <- function(x, y, fit_model, feature_names = names(x), impute = FALSE, scale = FALSE,
                                 metadata = list(), strict = TRUE) {
  if (!is.data.frame(x)) stop("x must be a data.frame so the column schema can be recorded.", call. = FALSE)
  missing <- setdiff(feature_names, names(x))
  if (length(missing)) stop("training frame is missing feature column(s): ", paste(missing, collapse = ", "), call. = FALSE)
  m <- as.matrix(x[, feature_names, drop = FALSE])
  storage.mode(m) <- "double"
  ranges <- lapply(stats::setNames(seq_along(feature_names), feature_names), function(i) {
    v <- m[, i]
    if (all(is.na(v))) NULL else range(v, na.rm = TRUE)
  })
  ranges <- Filter(Negate(is.null), ranges)
  impute_fn <- scale_fn <- NULL
  if (isTRUE(impute)) {
    means <- colMeans(m, na.rm = TRUE)
    impute_fn <- function(z) {
      for (j in seq_len(ncol(z))) z[is.na(z[, j]), j] <- means[[j]]
      z
    }
    m <- impute_fn(m)
  }
  if (isTRUE(scale)) {
    mu <- colMeans(m)
    sdv <- apply(m, 2, stats::sd)
    sdv[is.na(sdv) | sdv == 0] <- 1
    scale_fn <- function(z) sweep(sweep(z, 2, mu, "-"), 2, sdv, "/")
    m <- scale_fn(m)
  }
  predict_fn <- fit_model(m, y)
  omop_scoring_pipeline(predict_fn, feature_names, impute_fn = impute_fn, scale_fn = scale_fn, metadata = metadata,
                        strict = strict, allow_missing_values = !is.null(impute_fn), ranges = ranges)
}

#' Validate a frame against a scoring pipeline's schema
#'
#' Returns the numeric matrix restricted to and ordered by the schema, or raises an error whose class includes
#' `omop_feature_schema_error`.
#'
#' @param pipeline An `omop_scoring_pipeline`.
#' @param x Data frame to score.
#' @return A numeric matrix with one column per feature, in schema order.
#' @export
validate_scoring_input <- function(pipeline, x) {
  fail <- function(msg) {
    stop(structure(class = c("omop_feature_schema_error", "error", "condition"),
                   list(message = paste("feature schema mismatch:", msg), call = NULL)))
  }
  if (!is.data.frame(x)) fail("scoring input must be a data.frame with named columns.")
  cols <- names(x)
  if (anyDuplicated(cols)) fail("scoring input has duplicate column names.")
  missing <- setdiff(pipeline$feature_names, cols)
  extra <- setdiff(cols, pipeline$feature_names)
  parts <- c(if (length(missing)) paste0("missing column(s) ", paste(missing, collapse = ", ")),
             if (length(extra) && pipeline$strict) paste0("unexpected column(s) ", paste(extra, collapse = ", ")))
  if (length(parts)) fail(paste(parts, collapse = "; "))
  out <- x[, pipeline$feature_names, drop = FALSE]
  bad <- names(out)[!vapply(out, function(v) is.numeric(v) || is.logical(v), logical(1))]
  if (length(bad)) fail(paste0("non-numeric feature values in ", paste(bad, collapse = ", ")))
  m <- as.matrix(out)
  storage.mode(m) <- "double"
  if (!pipeline$allow_missing_values && anyNA(m)) {
    fail(paste0("missing values in ", paste(colnames(m)[colSums(is.na(m)) > 0], collapse = ", "),
                " and the pipeline has no imputer."))
  }
  if (any(is.infinite(m))) fail("infinite values in scoring input.")
  m
}

#' Score new patients with a scoring pipeline
#'
#' @param pipeline An `omop_scoring_pipeline`.
#' @param x Data frame with exactly the pipeline's feature columns (any order).
#' @param threshold Probability cut-off for `predict_scoring_class()`.
#' @return `predict_scoring_pipeline()`: positive-class probability per row. `predict_scoring_class()`: 0/1 integer.
#' @export
predict_scoring_pipeline <- function(pipeline, x) {
  m <- validate_scoring_input(pipeline, x)
  if (!is.null(pipeline$impute_fn)) m <- pipeline$impute_fn(m)
  if (!is.null(pipeline$scale_fn)) m <- pipeline$scale_fn(m)
  as.numeric(pipeline$predict_fn(m))
}

#' @rdname predict_scoring_pipeline
#' @export
predict_scoring_class <- function(pipeline, x, threshold = 0.5) {
  as.integer(predict_scoring_pipeline(pipeline, x) >= threshold)
}

#' Rows per feature outside the min/max seen at fit time (a drift check)
#'
#' @param pipeline An `omop_scoring_pipeline`.
#' @param x Data frame to check.
#' @return Named integer vector of out-of-range row counts (only features with at least one).
#' @export
scoring_pipeline_out_of_range <- function(pipeline, x) {
  m <- validate_scoring_input(pipeline, x)
  counts <- vapply(names(pipeline$ranges), function(n) {
    r <- pipeline$ranges[[n]]
    sum(m[, n] < r[[1]] | m[, n] > r[[2]], na.rm = TRUE)
  }, numeric(1))
  counts[counts > 0]
}

#' Save / load a scoring pipeline bundle
#'
#' Bundles are RDS files; only load files you created or trust.
#'
#' @param pipeline An `omop_scoring_pipeline`.
#' @param path File path.
#' @return `save_scoring_pipeline()`: `path`, invisibly. `load_scoring_pipeline()`: the pipeline.
#' @export
save_scoring_pipeline <- function(pipeline, path) {
  dir.create(dirname(path), showWarnings = FALSE, recursive = TRUE)
  saveRDS(pipeline, path)
  invisible(path)
}

#' @rdname save_scoring_pipeline
#' @export
load_scoring_pipeline <- function(path) {
  obj <- readRDS(path)
  if (!inherits(obj, "omop_scoring_pipeline")) stop(path, " is not an omop_scoring_pipeline bundle.", call. = FALSE)
  if (!identical(obj$bundle_version, 1L)) stop("unsupported bundle version ", format(obj$bundle_version), ".", call. = FALSE)
  obj
}

#' @export
print.omop_scoring_pipeline <- function(x, ...) {
  cat(sprintf("<omop_scoring_pipeline> %d features, imputer=%s, scaler=%s, strict=%s\n", length(x$feature_names),
              !is.null(x$impute_fn), !is.null(x$scale_fn), x$strict))
  invisible(x)
}
