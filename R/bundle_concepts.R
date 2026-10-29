# Make a model bundle refuse to run against custom concept ids that mean something else at this site (the R twin of
# python/omop_etl/bundle_concepts.py).

.bundle_local_ids <- function(source) {
  if (inherits(source, "DBIConnection")) {
    reg <- custom_concept_registry(source)
    return(stats::setNames(vapply(reg, function(v) as.numeric(v$concept_id), numeric(1)), names(reg)))
  }
  .load_registry(source)
}

.bundle_spec <- function(bundle) {
  if (is.character(bundle)) return(load_model_bundle(bundle)$feature_spec)
  if (!is.null(bundle$feature_spec)) return(bundle$feature_spec)
  bundle
}

#' Record the custom concept ids in a feature spec
#'
#' A model that reads a custom feature (a site's Charlson index, an area deprivation index) by concept id is only portable if the id means
#' the same feature everywhere, and custom concept ids are per database ([register_custom_concepts()]). Stamping a feature spec adds
#' `concept_id` to every feature that is a registered custom concept, so the bundle records the id the feature had when the model was
#' trained; [validate_bundle_concepts()] checks it at the site that scores. A feature is matched by name, or by `concept_key` when the
#' name differs from the registered concept code.
#'
#' @param feature_spec A feature spec list (see [load_feature_spec()]).
#' @param source A DBI connection to the OMOP DuckDB, a registry (named vector or list `name = id`), or the JSON file from
#'   [export_custom_concept_registry()].
#' @return The spec with `concept_id` added where a feature is a registered custom concept.
#' @export
stamp_feature_spec_concepts <- function(feature_spec, source) {
  ids <- .bundle_local_ids(source)
  feature_spec$features <- lapply(feature_spec$features, function(f) {
    key <- f$concept_key %||% f$name
    if (key %in% names(ids)) f$concept_id <- ids[[key]]
    f
  })
  feature_spec
}

#' Compare the custom concept ids recorded in a bundle with this site's
#'
#' Returns one row per feature that records a `concept_id`: `feature`, `key`, `bundle_concept_id`, `local_concept_id`, `status` (`ok`,
#' `different_id`, `not_registered_here` or `id_used_by_other_feature`) and `detail`. With `error = TRUE` (default) any other status than
#' `ok` is an error listing them, so a bundle never scores a column that means another feature here. Features without a recorded
#' `concept_id` are not checked.
#'
#' @param bundle A `model_bundle`, a bundle directory, or a feature spec list.
#' @param source A DBI connection, a registry (named vector or list), or a registry JSON file (see [stamp_feature_spec_concepts()]).
#' @param error Raise an error on a conflict.
#' @return A data frame (zero rows when the bundle records no concept ids).
#' @export
validate_bundle_concepts <- function(bundle, source, error = TRUE) {
  ids <- .bundle_local_ids(source)
  rows <- list()
  for (f in .bundle_spec(bundle)$features) {
    if (is.null(f$concept_id)) next
    declared <- as.numeric(f$concept_id)
    key <- f$concept_key %||% f$name
    local <- if (key %in% names(ids)) ids[[key]] else NA_real_
    if (is.na(local)) {
      other <- if (length(ids)) names(ids)[match(declared, ids)] else NA_character_
      if (!is.na(other)) { status <- "id_used_by_other_feature"; detail <- sprintf("id %.0f is '%s' at this site, and '%s' is not registered here", declared, other, key) }
      else { status <- "not_registered_here"; detail <- sprintf("'%s' is not a registered custom concept at this site", key) }
    } else if (local != declared) {
      status <- "different_id"; detail <- sprintf("the bundle was trained with '%s' = %.0f, this site has %.0f", key, declared, local)
    } else { status <- "ok"; detail <- "" }
    rows[[length(rows) + 1L]] <- data.frame(feature = f$name, key = key, bundle_concept_id = declared, local_concept_id = local,
                                            status = status, detail = detail, stringsAsFactors = FALSE)
  }
  out <- if (length(rows)) do.call(rbind, rows) else data.frame(feature = character(), key = character(), bundle_concept_id = numeric(),
                                                               local_concept_id = numeric(), status = character(), detail = character())
  bad <- out[out$status != "ok", , drop = FALSE]
  if (error && nrow(bad)) {
    stop("bundle custom concept ids do not match this site: ", paste(sprintf("%s (%s: %s)", bad$feature, bad$status, bad$detail), collapse = "; "), call. = FALSE)
  }
  out
}
