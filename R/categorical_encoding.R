#' Deterministic one-hot encoding of categorical model features from fixed level lists
#'
#' `model.matrix()` / `get_dummies(drop_first = TRUE)` derive their columns from the levels present in the data, so the
#' same model sees different columns at a site that lacks a level. A model release should ship the level list of every
#' categorical feature; these functions always produce the same columns in the same order, named `<feature>_<level>`
#' (as pandas `get_dummies` does). Missing values encode as all zeros, which equals the reference level, so give
#' "missing" its own level when it matters (the SDI / RUCA labels do).
#'
#' @param levels Named list; each element is a character vector of levels, or a list with `levels` and an optional
#'   `reference` (the level that gets no column; `NULL` keeps every level).
#' @param df A data frame.
#' @param unknown `"error"` (default) or `"zero"`: what to do with a value outside the level list.
#' @param keep Keep the original string columns as well.
#' @return `encoded_column_names()`: the dummy column names. `encode_categoricals()`: `df` with each listed column
#'   replaced by 0/1 numeric dummies.
#' @name categorical_encoding
#' @export
encoded_column_names <- function(levels) {
  unlist(lapply(names(levels), function(col) {
    n <- .cat_norm(levels[[col]], col)
    paste0(col, "_", setdiff(n$levels, n$reference))
  }), use.names = FALSE)
}

.cat_norm <- function(v, col) {
  lv <- as.character(if (is.list(v)) v$levels else v)
  ref <- if (is.list(v) && !is.null(v$reference)) as.character(v$reference) else NULL
  if (!length(lv) || anyDuplicated(lv)) stop("levels for '", col, "' must be non-empty and unique.", call. = FALSE)
  if (!is.null(ref) && !ref %in% lv) stop("reference '", ref, "' is not one of the levels of '", col, "'.", call. = FALSE)
  list(levels = lv, reference = ref)
}

#' @rdname categorical_encoding
#' @export
encode_categoricals <- function(df, levels, unknown = c("error", "zero"), keep = FALSE) {
  unknown <- match.arg(unknown)
  miss <- setdiff(names(levels), names(df))
  if (length(miss)) stop("categorical column(s) missing from the frame: ", paste(miss, collapse = ", "), call. = FALSE)
  blocks <- list()
  for (col in names(levels)) {
    n <- .cat_norm(levels[[col]], col)
    s <- trimws(as.character(df[[col]]))
    bad <- unique(s[!is.na(s) & !s %in% n$levels])
    if (length(bad) && unknown == "error") {
      stop(col, ": value(s) not in the model's levels, e.g. ", paste(utils::head(sort(bad), 5), collapse = ", "),
           "; levels are ", paste(n$levels, collapse = ", "), ".", call. = FALSE)
    }
    for (lv in setdiff(n$levels, n$reference)) blocks[[paste0(col, "_", lv)]] <- as.numeric(!is.na(s) & s == lv)
  }
  out <- if (keep) df else df[, setdiff(names(df), names(levels)), drop = FALSE]
  for (nm in names(blocks)) out[[nm]] <- blocks[[nm]]
  out
}
