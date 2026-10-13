#' Cross-platform path sanitization for DuckDB file operations
#'
#' DuckDB parses SQL string literals itself, so raw Windows paths, paths containing single quotes, and
#' relative paths interpolated into `sprintf()` are a recurring source of parser errors. Everything that puts
#' a filesystem path into SQL should go through these helpers (mirror of `omop_etl.paths` in Python).
#'
#' `sanitize_sql_path()` returns an absolute path with forward slashes. Remote URIs (`s3://`, `https://`, ...)
#' are only slash-normalized, and glob patterns survive because the path need not exist.
#' `sql_path_literal()` returns the same path as a quoted SQL string literal with single quotes escaped.
#'
#' @param p A path (character).
#' @return `sanitize_sql_path()`: a character string. `sql_path_literal()`: a quoted SQL literal.
#' @examples
#' sanitize_sql_path("C:\\data\\site a\\vital.csv")
#' sql_path_literal("o'brien.csv")
#' @name sanitize_sql_path
#' @export
sanitize_sql_path <- function(p) {
  s <- as.character(p)[[1]]
  if (grepl("^[A-Za-z][A-Za-z0-9+.-]+://", s)) {
    return(gsub("\\\\", "/", s))
  }
  s <- path.expand(s)
  if (!grepl("^([A-Za-z]:|/|\\\\\\\\|//)", s)) {
    s <- file.path(getwd(), s)
  }
  gsub("\\\\", "/", normalizePath(s, winslash = "/", mustWork = FALSE))
}

#' @rdname sanitize_sql_path
#' @export
sql_path_literal <- function(p) {
  paste0("'", gsub("'", "''", sanitize_sql_path(p), fixed = TRUE), "'")
}

#' @keywords internal
#' @noRd
.sql_options <- function(options) {
  if (length(options) == 0) return("")
  parts <- vapply(names(options), function(key) {
    value <- options[[key]]
    rendered <- if (is.logical(value)) {
      if (isTRUE(value)) "true" else "false"
    } else if (is.numeric(value)) {
      as.character(value)
    } else if (length(value) > 1) {
      paste0("[", paste0("'", gsub("'", "''", value, fixed = TRUE), "'", collapse = ", "), "]")
    } else {
      paste0("'", gsub("'", "''", as.character(value), fixed = TRUE), "'")
    }
    paste0(key, " = ", rendered)
  }, character(1))
  paste0(", ", paste(parts, collapse = ", "))
}

#' SQL table-function expressions and file operations with sanitized paths
#'
#' `read_csv_sql()` and `read_parquet_sql()` build `read_csv_auto('<path>', ...)` /
#' `read_parquet('<path>', ...)` expressions, `attach_database()` runs `ATTACH`, and `sql_copy_to()` runs
#' `COPY ... TO`, all through [sanitize_sql_path()].
#'
#' @param path File path (see [sanitize_sql_path()]).
#' @param ... Named reader options, e.g. `all_varchar = TRUE`.
#' @param con DuckDB DBI connection.
#' @param alias Alias for the attached database.
#' @param read_only Logical; attach read-only.
#' @param db_type Optional ATTACH `TYPE` (e.g. `"SQLITE"`).
#' @param source Table name or parenthesized query to copy.
#' @param format COPY `FORMAT` (default `"PARQUET"`).
#' @param options Named list of extra COPY options.
#' @return `read_csv_sql()` / `read_parquet_sql()`: a SQL string. `attach_database()` and `sql_copy_to()`:
#'   invisibly, `con`.
#' @name sql_path_helpers
#' @export
read_csv_sql <- function(path, ...) {
  paste0("read_csv_auto(", sql_path_literal(path), .sql_options(list(...)), ")")
}

#' @rdname sql_path_helpers
#' @export
read_parquet_sql <- function(path, ...) {
  paste0("read_parquet(", sql_path_literal(path), .sql_options(list(...)), ")")
}

#' @rdname sql_path_helpers
#' @export
attach_database <- function(con, path, alias, read_only = FALSE, db_type = NULL) {
  opts <- c(if (!is.null(db_type)) paste0("TYPE ", db_type), if (isTRUE(read_only)) "READ_ONLY")
  suffix <- if (length(opts)) paste0(" (", paste(opts, collapse = ", "), ")") else ""
  DBI::dbExecute(con, paste0("ATTACH ", sql_path_literal(path), " AS ", alias, suffix))
  invisible(con)
}

#' @rdname sql_path_helpers
#' @export
sql_copy_to <- function(con, source, path, format = "PARQUET", options = list()) {
  opts <- paste0("FORMAT ", format)
  for (key in names(options)) {
    value <- options[[key]]
    opts <- paste0(opts, ", ", key, " ", if (is.logical(value)) {
      if (isTRUE(value)) "TRUE" else "FALSE"
    } else if (is.numeric(value)) {
      as.character(value)
    } else {
      paste0("'", value, "'")
    })
  }
  DBI::dbExecute(con, paste0("COPY ", source, " TO ", sql_path_literal(path), " (", opts, ")"))
  invisible(con)
}
