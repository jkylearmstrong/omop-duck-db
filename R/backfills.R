#' Post-Build Database Backfill Wrappers (Python Delegation via reticulate)
#'
#' Fresh builds via [etl_pcornet()] already include censored lab parsing, `VITAL.TOBACCO`
#' observations, and OMOP v5.4 `MED_ADMIN` domain routing natively in both R and Python.
#' For OMOP DuckDB databases built earlier or by an external builder, the post-build
#' backfill routines live in the Python `omop_etl` package (0.36.0, S1/S2); these R
#' wrappers call them through `reticulate` when available and raise a clear error otherwise.
#'
#' Pass `db_path` as a file path (plus optional `central_vocab` path) so Python can open
#' its own DuckDB connection without conflicting with an active R process lock.
#'
#' @param db_path Path to the OMOP DuckDB database file.
#' @param raw_dir Path to the raw PCORnet source extract directory (for [backfill_all()]).
#' @param dry_run Logical; if `TRUE` (default for `backfill_all`), opens read-only and
#'   reports what would change without modifying the database.
#' @param central_vocab Optional path to `central_vocabulary.duckdb` to attach read-only.
#' @param crosswalk Optional path to a `PATID`--`ssid` crosswalk CSV.
#' @param start,end Optional ISO date window (`"YYYY-MM-DD"`) for [backfill_lab_values()].
#' @param lab_result_cm,vital_path,med_admin_path Source file paths (`.csv`, `.parquet`, or `.zip`).
#' @param add_rows Logical; in [backfill_condition_concepts()], add extra rows for multi-target ICD mappings.
#' @param route_domains,force Options for [backfill_med_admin()].
#' @return A `data.frame` (`backfill_all`, `backfill_lab_values`) or named `list` of counts.
#' @name backfill_all
#' @export
backfill_all <- function(db_path, raw_dir, dry_run = TRUE, central_vocab = NULL, crosswalk = NULL,
                         start = NULL, end = NULL) {
  .call_py_backfill("backfill_all", db_path = db_path, central_vocab = central_vocab, dry_run = dry_run,
                    raw_dir = as.character(raw_dir), crosswalk = crosswalk, start = start, end = end, as_df = TRUE)
}

#' @rdname backfill_all
#' @export
backfill_condition_concepts <- function(db_path, central_vocab = NULL, add_rows = TRUE, dry_run = FALSE) {
  .call_py_backfill("backfill_condition_concepts", db_path = db_path, central_vocab = central_vocab,
                    dry_run = dry_run, add_rows = isTRUE(add_rows))
}

#' @rdname backfill_all
#' @export
backfill_drug_concepts <- function(db_path, central_vocab = NULL, dry_run = FALSE) {
  .call_py_backfill("backfill_drug_concepts", db_path = db_path, central_vocab = central_vocab, dry_run = dry_run)
}

#' @rdname backfill_all
#' @export
backfill_lab_values <- function(db_path, lab_result_cm, central_vocab = NULL, start = NULL, end = NULL,
                                dry_run = FALSE, crosswalk = NULL) {
  .call_py_backfill("backfill_lab_values", db_path = db_path, central_vocab = central_vocab, dry_run = dry_run,
                    lab_result_cm = as.character(lab_result_cm), start = start, end = end,
                    crosswalk = crosswalk, as_df = TRUE)
}

#' @rdname backfill_all
#' @export
backfill_vital_tobacco <- function(db_path, vital_path, dry_run = FALSE, crosswalk = NULL) {
  .call_py_backfill("backfill_vital_tobacco", db_path = db_path, central_vocab = NULL, dry_run = dry_run,
                    vital_path = as.character(vital_path), crosswalk = crosswalk)
}

#' @rdname backfill_all
#' @export
backfill_med_admin <- function(db_path, med_admin_path, central_vocab = NULL, dry_run = FALSE,
                               crosswalk = NULL, route_domains = TRUE, force = FALSE) {
  .call_py_backfill("backfill_med_admin", db_path = db_path, central_vocab = central_vocab, dry_run = dry_run,
                    med_admin_path = as.character(med_admin_path), crosswalk = crosswalk,
                    route_domains = isTRUE(route_domains), force = isTRUE(force))
}

#' @rdname backfill_all
#' @export
repair_routed_med_admin_values <- function(db_path, dry_run = FALSE) {
  .call_py_backfill("repair_routed_med_admin_values", db_path = db_path, central_vocab = NULL, dry_run = dry_run)
}

.require_py_omop_etl <- function(fn_name) {
  if (!requireNamespace("reticulate", quietly = TRUE)) {
    stop(sprintf(
      "`%s()` is implemented in Python (`omop_etl`) and requires the `reticulate` R package (or run `python -c \"import omop_etl; ...\"` directly). Fresh builds via `omopduckdb::etl_pcornet()` already apply censored lab parsing, VITAL.TOBACCO, and MED_ADMIN domain routing natively in R.",
      fn_name
    ), call. = FALSE)
  }
  ok <- tryCatch(reticulate::py_module_available("omop_etl"), error = function(e) FALSE)
  if (!isTRUE(ok)) {
    stop(sprintf(
      "`%s()` delegates to Python `omop_etl`, which was not found in the active reticulate Python environment. Install it with `pip install omop-duck-db` (or set `RETICULATE_PYTHON`), or run the backfill directly from Python.",
      fn_name
    ), call. = FALSE)
  }
  reticulate::import("omop_etl", convert = TRUE)
}

.call_py_backfill <- function(fn_name, db_path, central_vocab = NULL, dry_run = FALSE, as_df = FALSE, ...) {
  if (!is.character(db_path) || length(db_path) != 1L || !nzchar(db_path)) {
    stop(sprintf("`%s()` expects `db_path` as a single file path string (disconnect any R connection to that file first).", fn_name), call. = FALSE)
  }
  omop_etl <- .require_py_omop_etl(fn_name)
  duckdb_py <- reticulate::import("duckdb", convert = FALSE)
  con <- duckdb_py$connect(as.character(db_path), read_only = isTRUE(dry_run))
  on.exit(try(con$close(), silent = TRUE), add = TRUE)
  if (!is.null(central_vocab) && nzchar(as.character(central_vocab))) {
    omop_etl$attach_central_vocabulary(con, as.character(central_vocab), temporary = TRUE)
  }
  fn <- omop_etl[[fn_name]]
  res <- do.call(fn, c(list(con, dry_run = isTRUE(dry_run)), list(...)))
  if (isTRUE(as_df)) as.data.frame(res) else as.list(res)
}
