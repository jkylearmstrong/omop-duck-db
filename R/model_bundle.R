#' Default risk tiers of a model bundle
#' @format A list of `list(name, min)`.
#' @export
DEFAULT_RISK_TIERS <- list(list(name = "low", min = 0), list(name = "moderate", min = 0.10), list(name = "high", min = 0.25))

.forbidden_suffixes <- c(".pkl", ".pickle", ".joblib", ".skops", ".dill", ".npy", ".npz", ".pt", ".pth", ".bin", ".rds", ".rdata")
.kind_suffix <- list(logistic = ".json", xgboost_json = c(".json", ".ubj"))

#' SHA-256 of a file
#' @param path File path.
#' @return Lower-case hex digest.
#' @export
sha256_file <- function(path) digest::digest(file = path, algo = "sha256", serialize = FALSE)

.bundle_error <- function(msg) {
  stop(structure(class = c("bundle_integrity_error", "error", "condition"), list(message = msg, call = NULL)))
}

.sigmoid <- function(z) 1 / (1 + exp(-pmin(pmax(z, -500), 500)))
.logit <- function(p) { p <- pmin(pmax(p, 1e-12), 1 - 1e-12); log(p / (1 - p)) }

#' Load and verify a model bundle
#'
#' A bundle is a directory with `manifest.json`, `feature_spec.json`, `blend.json` and the component files
#' (`glm.json` logistic regression; `gbdt_xgb.json` XGBoost native JSON, which needs the `xgboost` package). It is the
#' same layout as the Python `omop_etl.scoring.load_model_bundle`, so a bundle written by either language loads in the
#' other. ONNX and TorchScript components are **Python-only**; the R loader raises a clear error for them.
#'
#' Security: R/Python serialisations (`.rds`, pickle, joblib, ...) are refused. Every file the blend reads must be
#' listed in `manifest.json` with a matching SHA-256, else a `bundle_integrity_error` is raised.
#'
#' @param path Bundle directory.
#' @param verify If `FALSE`, hashes are not compared (the file-type and manifest-listing checks still apply).
#' @return A `model_bundle` object (a list).
#' @export
load_model_bundle <- function(path, verify = TRUE) {
  mp <- file.path(path, "manifest.json")
  if (!file.exists(mp)) .bundle_error(paste0(path, ": manifest.json not found."))
  manifest <- jsonlite::fromJSON(mp, simplifyVector = FALSE)
  for (k in c("name", "version", "feature_spec_sha256", "model_sha256")) {
    if (is.null(manifest[[k]])) .bundle_error(paste0("manifest.json lacks '", k, "'."))
  }
  files <- list.files(path, full.names = FALSE)
  bad <- files[tolower(sub("^.*(\\.[^.]*)$", "\\1", files)) %in% .forbidden_suffixes]
  if (length(bad)) .bundle_error(paste0("Refusing bundle with pickle-like / opaque binary files: ", paste(bad, collapse = ", ")))
  spec_path <- file.path(path, "feature_spec.json")
  if (verify && !identical(sha256_file(spec_path), manifest$feature_spec_sha256)) {
    .bundle_error("feature_spec.json hash does not match manifest.json (tampered or stale).")
  }
  spec <- load_feature_spec(spec_path)
  blend_path <- file.path(path, "blend.json")
  blend <- if (file.exists(blend_path)) jsonlite::fromJSON(blend_path, simplifyVector = FALSE) else NULL
  listed <- manifest$model_sha256
  if (!is.null(blend)) {
    if (is.null(listed[["blend.json"]])) .bundle_error("blend.json is not listed in manifest model_sha256.")
    if (verify && !identical(sha256_file(blend_path), listed[["blend.json"]])) {
      .bundle_error("blend.json hash does not match manifest.json.")
    }
  }
  if (is.null(blend$components) || !length(blend$components)) .bundle_error("blend.json must list at least one component.")
  comps <- lapply(blend$components, function(c) {
    f <- as.character(c$file)
    if (grepl("[/\\\\]", f) || startsWith(f, ".")) .bundle_error(paste0("component file '", f, "' must be a plain file name inside the bundle."))
    fp <- file.path(path, f)
    if (is.null(listed[[f]])) .bundle_error(paste0("component '", f, "' is not listed in manifest model_sha256."))
    if (!file.exists(fp)) .bundle_error(paste0("component file '", f, "' is missing."))
    if (verify && !identical(sha256_file(fp), listed[[f]])) {
      .bundle_error(paste0(f, ": SHA-256 mismatch with manifest.json (file changed after packaging)."))
    }
    if (c$kind %in% c("onnx", "torchscript")) {
      .bundle_error(paste0("component '", f, "' is of kind '", c$kind, "', which is Python-only; score this bundle with omop_etl.scoring."))
    }
    sfx <- .kind_suffix[[c$kind]]
    if (is.null(sfx) || !any(endsWith(tolower(f), sfx))) .bundle_error(paste0("component '", f, "' is not a valid '", c$kind, "' file."))
    w <- if (is.null(c$weight)) 1 else as.numeric(c$weight)
    if (!is.finite(w) || w <= 0) .bundle_error(paste0("component '", f, "': weight must be > 0."))
    list(name = if (is.null(c$name)) f else c$name, kind = c$kind, path = fp, weight = w, options = c)
  })
  structure(list(path = path, manifest = manifest, feature_spec = spec, components = comps,
                 calibration = blend$calibration,
                 risk_tiers = if (is.null(blend$risk_tiers)) DEFAULT_RISK_TIERS else blend$risk_tiers),
            class = "model_bundle")
}

.predict_component <- function(comp, X) {
  if (comp$kind == "logistic") {
    s <- jsonlite::fromJSON(comp$path, simplifyVector = FALSE)
    coef <- as.numeric(unlist(s$coef))
    if (length(coef) != ncol(X)) .bundle_error(paste0(basename(comp$path), ": ", length(coef), " coefficients for ", ncol(X), " features."))
    mean_ <- if (is.null(s$mean)) rep(0, length(coef)) else as.numeric(unlist(s$mean))
    scale_ <- if (is.null(s$scale)) rep(1, length(coef)) else as.numeric(unlist(s$scale))
    b <- if (is.null(s$intercept)) 0 else as.numeric(s$intercept)
    return(as.numeric(.sigmoid(sweep(sweep(X, 2, mean_), 2, scale_, "/") %*% coef + b)))
  }
  if (comp$kind == "xgboost_json") {
    if (!requireNamespace("xgboost", quietly = TRUE)) stop("The 'xgboost' package is required for 'xgboost_json' components.", call. = FALSE)
    bst <- xgboost::xgb.load(comp$path)
    return(as.numeric(stats::predict(bst, X)))
  }
  .bundle_error(paste0("unknown component kind '", comp$kind, "'"))
}

#' Probabilities of a bundle for an ordered numeric matrix
#'
#' Blends `sum(w_i * p_i) / sum(w_i)`, then applies the calibration (Platt on the blended logit, or isotonic by
#' linear interpolation).
#'
#' @param bundle A `model_bundle`.
#' @param X Numeric matrix with columns in the bundle's feature order and no `NA`.
#' @return A numeric vector.
#' @export
bundle_predict_proba <- function(bundle, X) {
  w <- vapply(bundle$components, function(c) c$weight, numeric(1))
  p <- Reduce(`+`, Map(function(wi, c) wi * .predict_component(c, X), w, bundle$components)) / sum(w)
  cal <- bundle$calibration
  if (!is.null(cal)) {
    p <- switch(cal$method,
      platt = .sigmoid(as.numeric(cal$slope) * .logit(p) + as.numeric(cal$intercept)),
      isotonic = stats::approx(as.numeric(unlist(cal$x)), as.numeric(unlist(cal$y)), xout = p, rule = 2)$y,
      .bundle_error(paste0("unknown calibration method '", cal$method, "'")))
  }
  p
}

#' Score a feature table with a model bundle
#'
#' Validates the frame against the bundle's feature contract ([validate_feature_frame()]), fills remaining `NA` with
#' each feature's `impute` value, blends and calibrates. Mirror of Python `score_cohort`.
#'
#' @param bundle A `model_bundle` or a bundle directory path.
#' @param features A data.frame, or the path of a `.csv` / `.parquet` file (parquet needs the `arrow` package).
#' @param key_columns Columns to return alongside the score (default: non-feature columns ending in `_id`).
#' @param on_violation `"raise"`, `"clip"` or `"nullify"`.
#' @param return_validation If `TRUE`, return `list(scores =, validation =)`.
#' @return A data.frame with the key columns, `prob` and `risk_tier`.
#' @export
score_cohort <- function(bundle, features, key_columns = NULL, on_violation = "raise", return_validation = FALSE) {
  if (is.character(bundle)) bundle <- load_model_bundle(bundle)
  if (is.character(features)) {
    features <- if (grepl("\\.parquet$", features, ignore.case = TRUE)) {
      if (!requireNamespace("arrow", quietly = TRUE)) stop("The 'arrow' package is required to read parquet.", call. = FALSE)
      as.data.frame(arrow::read_parquet(features))
    } else utils::read.csv(features, stringsAsFactors = FALSE)
  }
  rep <- validate_feature_frame(features, bundle$feature_spec, on_violation)
  X <- rep$frame
  for (f in bundle$feature_spec$features) {
    n <- f$name
    if (anyNA(X[[n]])) {
      if (is.null(f$impute)) stop("feature '", n, "' has missing values and no 'impute' value in the feature spec.", call. = FALSE)
      X[[n]][is.na(X[[n]])] <- f$impute
    }
  }
  nm <- vapply(bundle$feature_spec$features, function(f) f$name, character(1))
  prob <- bundle_predict_proba(bundle, as.matrix(X[, nm, drop = FALSE]))
  keys <- if (is.null(key_columns)) setdiff(grep("_id$", names(features), value = TRUE), nm) else key_columns
  out <- features[, keys, drop = FALSE]
  out$prob <- prob
  tiers <- bundle$risk_tiers[order(vapply(bundle$risk_tiers, function(t) t$min, numeric(1)))]
  mins <- vapply(tiers, function(t) t$min, numeric(1))
  idx <- pmax(findInterval(prob, mins), 1L)
  out$risk_tier <- vapply(tiers, function(t) t$name, character(1))[idx]
  rownames(out) <- NULL
  if (return_validation) list(scores = out, validation = rep) else out
}

#' Package a model bundle
#'
#' Writes `feature_spec.json`, the component files, `blend.json` and `manifest.json` (with SHA-256 hashes).
#'
#' @param out_dir Output directory.
#' @param name,version Model name and version.
#' @param feature_spec Spec list (see [load_feature_spec()]).
#' @param components List of components, each `list(kind =, filename =, weight =, name =)` plus either `content`
#'   (a list written as JSON, for `"logistic"`) or `file` (an existing file to copy, e.g. an XGBoost JSON).
#' @param calibration Optional list, e.g. `list(method = "platt", slope = 1, intercept = 0)`.
#' @param risk_tiers Optional list of `list(name =, min =)`.
#' @param license,intended_use Free-text provenance.
#' @return `out_dir`, invisibly.
#' @export
write_model_bundle <- function(out_dir, name, version, feature_spec, components, calibration = NULL,
                               risk_tiers = NULL, license = "UNLICENSED", intended_use = "") {
  dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
  spec <- load_feature_spec(feature_spec)
  wj <- function(x, f) jsonlite::write_json(x, file.path(out_dir, f), auto_unbox = TRUE, null = "null", digits = NA, pretty = TRUE)
  wj(spec, "feature_spec.json")
  comps <- lapply(components, function(c) {
    if (!is.null(c$content)) wj(c$content, c$filename) else file.copy(c$file, file.path(out_dir, c$filename), overwrite = TRUE)
    list(name = if (is.null(c$name)) c$filename else c$name, kind = c$kind, file = c$filename,
         weight = if (is.null(c$weight)) 1 else c$weight)
  })
  wj(list(components = comps, calibration = calibration, risk_tiers = if (is.null(risk_tiers)) DEFAULT_RISK_TIERS else risk_tiers),
     "blend.json")
  hashes <- stats::setNames(lapply(comps, function(c) sha256_file(file.path(out_dir, c$file))), vapply(comps, function(c) c$file, character(1)))
  hashes[["blend.json"]] <- sha256_file(file.path(out_dir, "blend.json"))
  wj(list(bundle_schema_version = 1L, name = name, version = version,
          created = format(Sys.time(), "%Y-%m-%dT%H:%M:%S+00:00", tz = "UTC"),
          feature_spec_sha256 = sha256_file(file.path(out_dir, "feature_spec.json")), model_sha256 = hashes,
          license = license, intended_use = intended_use), "manifest.json")
  invisible(out_dir)
}
