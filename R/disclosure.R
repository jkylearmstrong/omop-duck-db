#' Small-cell suppression and disclosure control
#'
#' Helpers that every table leaving a site goes through (CONSORT attrition, pooled Table 1, parity ledgers): one
#' threshold, one masking rule, one set of checks. The default threshold is 11, the usual cell-size rule of data-use
#' agreements. Zero is not a small cell.
#'
#' `small_cell_mask()` is `TRUE` where a count is from 1 to `threshold - 1`. `format_count()` renders a count with
#' thousands separators, or `"<11"` for a hidden (`NA`) or small one.
#'
#' `suppress_small_counts()` blanks small counts (and the `also` columns of their rows) and adds a `suppressed` flag. With
#' `group_by` and `complementary = TRUE`, a group with exactly one blanked cell also loses its smallest remaining non-zero
#' cell, so the hidden value cannot be recovered from a published total or margin.
#'
#' `suppress_attrition_table()` suppresses a CONSORT flow so no small count can be recovered by subtraction: step `i` obeys
#' `retained[i-1] - dropped[i] == retained[i]`, so a hidden number is recoverable when the other two terms of an equation
#' are shown. The table is closed to a fixed point (a lone hidden term hides a second one), and any run of hidden steps
#' must account for no one or at least `threshold` subjects between the shown counts around it.
#'
#' `assert_disclosure_safe()` stops when a finished table still holds a small count (names the cells, never the values).
#'
#' @param x Numeric vector of counts.
#' @param n A count (or vector of counts).
#' @param df A data frame.
#' @param count_cols Names of the count columns.
#' @param threshold Smallest count that may be shown (default 11). Must be a positive whole number; 1 suppresses nothing.
#' @param also Further columns blanked in rows that lost a count (percentages, SMDs, ...).
#' @param group_by Column(s) defining groups whose total or margin is published (complementary suppression).
#' @param complementary Apply complementary suppression within `group_by` groups.
#' @param flag_col Name of the logical column marking rows that lost a value (`NULL` to omit).
#' @param retained_col,dropped_col Column names of the attrition table.
#' @param what Name of the table for the error message.
#' @return `small_cell_mask()`: logical vector. `format_count()`: character. `suppress_*()`: the data frame with `NA` where a
#'   count is hidden. `assert_disclosure_safe()`: `invisible(TRUE)` or an error.
#' @examples
#' small_cell_mask(c(0, 5, 11, 400))
#' format_count(c(1234, 5, NA))
#' suppress_small_counts(data.frame(n = c(5, 400), pct = c(1.2, 98.8)), "n", also = "pct")
#' @name disclosure
NULL

.check_threshold <- function(threshold) {
  if (length(threshold) != 1L || !is.numeric(threshold) || is.na(threshold) || threshold < 1 || threshold != round(threshold)) {
    stop("`threshold` must be a positive whole number (use 1 to switch suppression off).", call. = FALSE)
  }
  as.integer(threshold)
}

#' @rdname disclosure
#' @export
small_cell_mask <- function(x, threshold = 11L) {
  t <- .check_threshold(threshold)
  x <- suppressWarnings(as.numeric(x))
  !is.na(x) & x > 0 & x < t
}

#' @rdname disclosure
#' @export
format_count <- function(n, threshold = 11L) {
  t <- .check_threshold(threshold)
  n <- suppressWarnings(as.numeric(n))
  ifelse(is.na(n) | (n > 0 & n < t), paste0("<", t), formatC(round(n), format = "d", big.mark = ","))
}

#' @rdname disclosure
#' @export
suppress_small_counts <- function(df, count_cols, threshold = 11L, also = character(), group_by = NULL,
                                  complementary = TRUE, flag_col = "suppressed") {
  t <- .check_threshold(threshold)
  df <- as.data.frame(df, stringsAsFactors = FALSE)
  missing <- setdiff(c(count_cols, also, group_by), names(df))
  if (length(missing)) stop("columns not in the table: ", paste(missing, collapse = ", "), call. = FALSE)
  for (c in count_cols) df[[c]] <- suppressWarnings(as.numeric(df[[c]]))
  mask <- vapply(count_cols, function(c) !is.na(df[[c]]) & df[[c]] > 0 & df[[c]] < t, logical(nrow(df)))
  if (!is.matrix(mask)) mask <- matrix(mask, nrow = nrow(df), dimnames = list(NULL, count_cols))
  if (complementary && !is.null(group_by) && t > 1L && nrow(df) > 0L) {
    key <- do.call(paste, c(lapply(group_by, function(g) as.character(df[[g]])), sep = "\r"))
    for (idx in split(seq_len(nrow(df)), key)) {
      for (c in count_cols) {
        if (sum(mask[idx, c]) == 1L) {
          cand <- idx[!mask[idx, c] & !is.na(df[[c]][idx]) & df[[c]][idx] > 0]
          if (length(cand)) mask[cand[which.min(df[[c]][cand])], c] <- TRUE
        }
      }
    }
  }
  row_mask <- rowSums(mask) > 0
  for (c in count_cols) df[[c]][mask[, c]] <- NA_real_
  for (c in also) {
    if (is.numeric(df[[c]])) df[[c]] <- as.numeric(df[[c]])
    df[[c]][row_mask] <- NA
  }
  if (!is.null(flag_col)) df[[flag_col]] <- row_mask
  df
}

#' @rdname disclosure
#' @export
suppress_attrition_table <- function(df, threshold = 11L, retained_col = "subjects_retained",
                                     dropped_col = "subjects_dropped", also = "percent_retained", flag_col = "suppressed") {
  t <- .check_threshold(threshold)
  df <- as.data.frame(df, stringsAsFactors = FALSE)
  n <- nrow(df)
  ret <- suppressWarnings(as.numeric(df[[retained_col]]))
  drop <- if (dropped_col %in% names(df)) suppressWarnings(as.numeric(df[[dropped_col]])) else c(0, pmax(head(ret, -1) - tail(ret, -1), 0))
  if (n == 0L || t <= 1L) {
    df[[dropped_col]] <- drop
    if (!is.null(flag_col)) df[[flag_col]] <- rep(FALSE, n)
    return(df)
  }
  hr <- !is.na(ret) & ret > 0 & ret < t
  hd <- !is.na(drop) & drop > 0 & drop < t
  hd[1] <- FALSE
  changed <- TRUE
  guard <- 0L
  while (changed && guard < 10L * n + 10L) {
    guard <- guard + 1L
    changed <- FALSE
    if (n >= 2L) for (i in 2:n) {
      hidden <- as.integer(hr[i - 1L]) + as.integer(hd[i]) + as.integer(hr[i])
      if (hidden == 1L) {
        if (hd[i]) hr[i] <- TRUE else hd[i] <- TRUE
        changed <- TRUE
      }
    }
    shown <- which(!hr)
    if (length(shown) >= 2L) for (k in seq_len(length(shown) - 1L)) {
      a <- shown[k]; b <- shown[k + 1L]
      d <- ret[a] - ret[b]
      if (b - a > 1L && !(d == 0 || d >= t)) {
        hr[b] <- TRUE
        changed <- TRUE
      }
    }
  }
  df[[retained_col]] <- ret
  df[[dropped_col]] <- drop
  df[[retained_col]][hr] <- NA_real_
  df[[dropped_col]][hd] <- NA_real_
  for (c in also) if (c %in% names(df)) {
    df[[c]] <- as.numeric(df[[c]])
    df[[c]][hr] <- NA_real_
  }
  if (!is.null(flag_col)) df[[flag_col]] <- hr | hd
  df
}

#' @rdname disclosure
#' @export
assert_disclosure_safe <- function(df, count_cols, threshold = 11L, what = "table") {
  t <- .check_threshold(threshold)
  bad <- character()
  for (c in count_cols) {
    if (!c %in% names(df)) stop("column not in the table: ", c, call. = FALSE)
    rows <- which(small_cell_mask(df[[c]], t))
    if (length(rows)) bad <- c(bad, sprintf("%s[row %d]", c, rows))
  }
  if (length(bad)) {
    stop(sprintf("The %s holds %d count(s) below %d: %s. Suppress it before sharing.", what, length(bad), t,
                 paste(utils::head(bad, 10L), collapse = ", ")), call. = FALSE)
  }
  invisible(TRUE)
}
