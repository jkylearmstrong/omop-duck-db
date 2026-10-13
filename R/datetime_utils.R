#' Vectorized, fault-tolerant datetime normalization for messy EHR feeds
#'
#' Everything here compiles to a single DuckDB SQL expression: no R callbacks and no per-row fallbacks, so it
#' stays vectorized on multi-million-row tables. Mirror of `omop_etl.datetime_utils` in Python.
#'
#' `normalize_time_sql()` normalizes a time-of-day string to zero-padded `HH:MM` (or `HH:MM:SS`): `4:15` ->
#' `04:15`, `4` -> `04:00`, `415` / `0415` -> `04:15`, `4:15 PM` -> `16:15`, `12:05 AM` -> `00:05`, and
#' NULL / blank -> `00:00`.
#'
#' `vectorized_strptime()` builds an expression parsing `date_col` (+ optional `time_col`) to `TIMESTAMP`,
#' trying a prioritized format list in one `try_strptime(x, [..])` call.
#'
#' @param date_col Column name / expression holding the date (or a full datetime string).
#' @param time_col Optional column holding time-of-day, or `NULL`.
#' @param formats Prioritized strptime formats. Defaults to [DEFAULT_DATETIME_FORMATS].
#' @param on_error What to do with a non-blank input matching no format: `"null"` (return NULL),
#'   `"default_date"` (return `default_date`) or `"raise"` (fail the query naming the value). NULL / blank
#'   dates always yield NULL.
#' @param default_date Timestamp literal used when `on_error = "default_date"`.
#' @return A SQL expression string.
#' @examples
#' con <- DBI::dbConnect(duckdb::duckdb())
#' sql <- vectorized_strptime("'05JAN2020'", "'4:15'")
#' DBI::dbGetQuery(con, paste("SELECT", sql, "AS ts"))
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' @name vectorized_strptime
#' @export
vectorized_strptime <- function(date_col, time_col = NULL, formats = NULL,
                                on_error = c("null", "default_date", "raise"), default_date = "1900-01-01") {
  on_error <- match.arg(on_error)
  fmts <- if (is.null(formats)) DEFAULT_DATETIME_FORMATS else formats
  if (length(fmts) == 0) stop("formats must contain at least one format string.")
  fmt_list <- paste0("[", paste0("'", gsub("'", "''", fmts, fixed = TRUE), "'", collapse = ", "), "]")

  d <- paste0("TRIM(CAST(", date_col, " AS VARCHAR))")
  if (is.null(time_col)) {
    parsed <- paste0("try_strptime(", d, ", ", fmt_list, ")")
  } else {
    text <- paste0("(CASE WHEN ", d, " LIKE '%:%' THEN ", d, " ELSE ", d, " || ' ' || ", normalize_time_sql(time_col), " END)")
    parsed <- paste0("COALESCE(try_strptime(", text, ", ", fmt_list, "), try_strptime(", d, ", ", fmt_list, "))")
  }

  if (on_error == "null") return(parsed)
  present <- paste0("(", d, " IS NOT NULL AND ", d, " <> '')")
  if (on_error == "default_date") {
    return(paste0("(CASE WHEN ", present, " THEN COALESCE(", parsed, ", CAST('", default_date, "' AS TIMESTAMP)) END)"))
  }
  paste0("(CASE WHEN ", present, " THEN COALESCE(", parsed,
         ", CAST(error('vectorized_strptime: unparseable datetime ''' || ", d, " || '''') AS TIMESTAMP)) END)")
}

#' @rdname vectorized_strptime
#' @export
normalize_time_sql <- function(time_col) {
  if (is.null(time_col)) return("'00:00'")
  t <- paste0("UPPER(TRIM(CAST(", time_col, " AS VARCHAR)))")
  core <- paste0("TRIM(regexp_replace(", t, ", '\\s*(AM|PM)$', ''))")
  meridiem <- paste0("regexp_extract(", t, ", '(AM|PM)$', 1)")
  colon <- paste0("regexp_matches(", core, ", '^[0-9]{1,2}(:[0-9]{1,2}){0,2}$')")
  bare <- paste0("regexp_matches(", core, ", '^[0-9]{3,4}$')")
  hh_raw <- paste0("(CASE WHEN ", bare, " THEN CAST(LEFT(LPAD(", core, ", 4, '0'), 2) AS INTEGER) ",
                   "ELSE TRY_CAST(regexp_extract(", core, ", '^([0-9]{1,2})', 1) AS INTEGER) END)")
  mm <- paste0("(CASE WHEN ", bare, " THEN RIGHT(", core, ", 2) ",
               "ELSE COALESCE(NULLIF(regexp_extract(", core, ", '^[0-9]{1,2}:([0-9]{1,2})', 1), ''), '00') END)")
  ss <- paste0("NULLIF(regexp_extract(", core, ", '^[0-9]{1,2}:[0-9]{1,2}:([0-9]{1,2})', 1), '')")
  hh24 <- paste0("(CASE WHEN ", meridiem, " = 'PM' THEN (", hh_raw, " % 12) + 12 ",
                 "WHEN ", meridiem, " = 'AM' THEN (", hh_raw, " % 12) ELSE ", hh_raw, " END)")
  structured <- paste0("LPAD(CAST(", hh24, " AS VARCHAR), 2, '0') || ':' || LPAD(", mm, ", 2, '0') || ",
                       "COALESCE(':' || LPAD(", ss, ", 2, '0'), '')")
  paste0("(CASE WHEN ", t, " IS NULL OR ", t, " = '' THEN '00:00' ",
         "WHEN ", colon, " OR ", bare, " THEN ", structured, " ",
         "ELSE LPAD(", t, ", 5, '0') END)")
}

#' Register `normalize_time()` and `vectorized_strptime()` SQL macros on a connection
#'
#' The macro `vectorized_strptime(d, t)` uses the default format chain with `on_error = "null"`; call
#' [vectorized_strptime()] directly for `"default_date"` / `"raise"` behavior or custom formats.
#'
#' @param con DuckDB DBI connection.
#' @param formats Optional format list for the macro (default [DEFAULT_DATETIME_FORMATS]).
#' @return Invisibly, `con`.
#' @export
install_vectorized_strptime <- function(con, formats = NULL) {
  DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY MACRO normalize_time(t) AS ", normalize_time_sql("t")))
  DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY MACRO vectorized_strptime(d, t) AS ",
                             vectorized_strptime("d", "t", formats)))
  invisible(con)
}

#' Default prioritized datetime formats
#'
#' The formats seen in PCORnet feeds come first (`%d%b%Y %H:%M`, `%Y-%m-%d %H:%M:%S`, `%m/%d/%Y %H:%M`),
#' followed by seconds / minute-less and date-only variants.
#'
#' @format A character vector.
#' @export
DEFAULT_DATETIME_FORMATS <- c(
  "%d%b%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M",
  "%d%b%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M",
  "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
  "%d%b%Y", "%Y-%m-%d", "%m/%d/%Y"
)
