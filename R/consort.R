#' CONSORT attrition table from counts, for one site or pooled across sites
#'
#' `summarize_attrition_consort()` turns raw counts - the source population and how many were removed at each step
#' (wash-in, age, mortality, transfer, ...) - into the attrition table that [generate_consort_attrition()] and its exporters
#' (`to_mermaid()`, `to_latex()`, `to_typst()`, `to_tikz()`, `to_markdown()`) render. With `sites` it pools several sites by
#' summing the counts step by step. Nothing here reads patient-level data.
#'
#' A table suppressed for release (`min_cell`) has `NA` where a count is hidden; the exporters write those as `<11`.
#' Pooled output is suppressed by default (`min_cell = 11`) because it leaves the site. Release the pooled table or the
#' per-site tables, not both: with a hidden site cell the pooled total minus the other sites would give it back.
#'
#' @param initial_n Source population size (single site).
#' @param exclusions What each step removed: a named numeric vector or list (`c(Washout = 120, Age = 40)`), a data frame
#'   with `label` and `excluded` (or `remaining`) and optional `reason`, or a list of such lists. A step cannot remove more
#'   people than are left.
#' @param sites Named list for pooling. Each element is an attrition data frame, a `consort_attrition`, or
#'   `list(initial_n =, exclusions =)`.
#' @param min_cell Suppress counts from 1 to `min_cell - 1` and anything they could be recovered from by subtraction
#'   (`NULL` keeps exact counts; for `sites` the default is 11).
#' @param initial_label Label of the first step.
#' @return A `consort_attrition` data frame (`step_number`, `step_name`, `subjects_retained`, `subjects_dropped`,
#'   `percent_retained`, `exclusion_reason`, and `n_sites` / `suppressed` where relevant).
#' @examples
#' att <- summarize_attrition_consort(1000, c(Washout = 100, `Age < 18` = 50, `Died in hospital` = 30))
#' att$subjects_retained
#' cat(to_mermaid(att))
#' @export
summarize_attrition_consort <- function(initial_n = NULL, exclusions = NULL, sites = NULL, min_cell = NULL,
                                        initial_label = "Source population") {
  if (!is.null(sites)) {
    tabs <- lapply(sites, function(s) {
      if (is.list(s) && !is.data.frame(s) && !is.null(s$initial_n)) {
        .consort_from_counts(s$initial_n, s$exclusions, if (is.null(s$initial_label)) initial_label else s$initial_label)
      } else {
        as.data.frame(unclass_consort(s), stringsAsFactors = FALSE)
      }
    })
    mc <- if (is.null(min_cell)) 11L else min_cell
    out <- .pool_consort(tabs, mc)
    return(.as_consort(out, mc))
  }
  if (is.null(initial_n)) stop("Give `initial_n` and `exclusions`, or `sites`.", call. = FALSE)
  if (is.null(exclusions)) stop("`exclusions` is required with `initial_n`.", call. = FALSE)
  df <- .consort_from_counts(initial_n, exclusions, initial_label)
  if (!is.null(min_cell)) df <- suppress_attrition_table(df, min_cell)
  .as_consort(df, min_cell)
}

unclass_consort <- function(x) {
  if (inherits(x, "consort_attrition")) class(x) <- setdiff(class(x), "consort_attrition")
  x
}

.as_consort <- function(df, min_cell) {
  df <- as.data.frame(df, stringsAsFactors = FALSE)
  attr(df, "min_cell") <- if (is.null(min_cell)) NULL else as.integer(min_cell)
  structure(df, class = c("consort_attrition", "data.frame"))
}

.norm_exclusions <- function(exclusions) {
  if (is.data.frame(exclusions)) {
    return(lapply(seq_len(nrow(exclusions)), function(i) as.list(exclusions[i, , drop = FALSE])))
  }
  if (is.atomic(exclusions) && !is.null(names(exclusions))) {
    return(lapply(seq_along(exclusions), function(i) list(label = names(exclusions)[i], excluded = unname(exclusions[[i]]))))
  }
  if (is.list(exclusions) && !is.null(names(exclusions)) && !any(c("label", "excluded", "remaining") %in% names(exclusions)) &&
      all(vapply(exclusions, is.numeric, logical(1)))) {
    return(lapply(seq_along(exclusions), function(i) list(label = names(exclusions)[i], excluded = exclusions[[i]])))
  }
  lapply(exclusions, function(e) {
    if (!is.list(e) || is.null(e$label) || is.null(e$excluded) == is.null(e$remaining)) {
      stop("each exclusion needs a `label` and exactly one of `excluded` or `remaining`.", call. = FALSE)
    }
    e
  })
}

.consort_from_counts <- function(initial_n, exclusions, initial_label = "Source population") {
  if (length(initial_n) != 1L || is.na(initial_n) || initial_n < 0 || initial_n != round(initial_n)) {
    stop("`initial_n` must be a non-negative whole number.", call. = FALSE)
  }
  left <- as.numeric(initial_n)
  labels <- initial_label; retained <- left; dropped <- 0; reasons <- ""
  for (e in .norm_exclusions(exclusions)) {
    drop <- if (!is.null(e$excluded)) e$excluded else left - e$remaining
    if (length(drop) != 1L || is.na(drop) || drop < 0 || drop != round(drop) || drop > left) {
      stop(sprintf("step '%s': cannot exclude %s from %s remaining.", e$label, format(drop), format(left)), call. = FALSE)
    }
    left <- left - drop
    labels <- c(labels, as.character(e$label)); retained <- c(retained, left); dropped <- c(dropped, drop)
    reasons <- c(reasons, if (!is.null(e$reason) && nzchar(e$reason)) as.character(e$reason) else as.character(e$label))
  }
  data.frame(step_number = seq_along(labels), step_name = labels, subjects_retained = retained, subjects_dropped = dropped,
             percent_retained = if (retained[1] > 0) 100 * retained / retained[1] else 0,
             exclusion_reason = reasons, stringsAsFactors = FALSE)
}

.pool_consort <- function(tabs, min_cell) {
  if (!length(tabs)) stop("no site tables given.", call. = FALSE)
  ref <- as.character(tabs[[1]]$step_name)
  for (nm in names(tabs)[-1]) {
    if (!identical(as.character(tabs[[nm]]$step_name), ref)) {
      stop(sprintf("site '%s' has different steps from '%s'.", nm, names(tabs)[1]), call. = FALSE)
    }
  }
  ret <- Reduce(`+`, lapply(tabs, function(t) as.numeric(t$subjects_retained)))   # NA propagates: an unknown site cell is unknown
  drop <- Reduce(`+`, lapply(tabs, function(t) {
    if ("subjects_dropped" %in% names(t)) as.numeric(t$subjects_dropped) else c(0, pmax(head(t$subjects_retained, -1) - tail(t$subjects_retained, -1), 0))
  }))
  reasons <- vapply(seq_along(ref), function(i) {
    r <- unlist(lapply(tabs, function(t) if ("exclusion_reason" %in% names(t)) as.character(t$exclusion_reason[i]) else NA_character_))
    r <- r[!is.na(r) & nzchar(r)]
    if (length(r)) r[1] else ""
  }, character(1))
  out <- data.frame(step_number = seq_along(ref), step_name = ref, subjects_retained = ret, subjects_dropped = drop,
                    percent_retained = if (!is.na(ret[1]) && ret[1] > 0) 100 * ret / ret[1] else NA_real_,
                    exclusion_reason = reasons, n_sites = length(tabs), stringsAsFactors = FALSE)
  if (!is.null(min_cell)) suppress_attrition_table(out, min_cell) else out
}

.consort_n <- function(v, min_cell = NULL) {
  v <- suppressWarnings(as.numeric(v))
  if (is.na(v)) paste0("<", if (is.null(min_cell)) 11L else min_cell) else format(v, big.mark = ",", scientific = FALSE, trim = TRUE)
}

#' @return (internal) the ", 88.7%" suffix of a step label, or "" when the percentage is hidden
#' @noRd
.consort_pct <- function(v) {
  v <- suppressWarnings(as.numeric(v))
  if (is.na(v)) "" else sprintf(", %.1f%%", v)
}

#' Write a CONSORT attrition table as a publication-ready CSV
#'
#' Hidden (`NA`) counts are written as `<11` (or `<min_cell`); percentages are rounded.
#'
#' @param x A `consort_attrition` or attrition data frame.
#' @param path Output file.
#' @param digits Decimals for `percent_retained`.
#' @return `path`, invisibly.
#' @export
write_consort_csv <- function(x, path, digits = 1L) {
  mc <- attr(x, "min_cell")
  out <- unclass_consort(x)
  attr(out, "min_cell") <- NULL
  out <- as.data.frame(out, stringsAsFactors = FALSE)
  if (is.null(mc) && "suppressed" %in% names(out) && any(out$suppressed, na.rm = TRUE)) mc <- 11L
  for (c in intersect(c("subjects_retained", "subjects_dropped"), names(out))) {
    out[[c]] <- ifelse(is.na(out[[c]]), paste0("<", if (is.null(mc)) 11L else mc), format(out[[c]], scientific = FALSE, trim = TRUE))
  }
  if ("percent_retained" %in% names(out)) out$percent_retained <- round(out$percent_retained, digits)
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  utils::write.csv(out, path, row.names = FALSE, na = "")
  invisible(path)
}
