#' @keywords internal
#' @noRd
find_source_file <- function(source_dir, table_name) {
  matches <- list.files(source_dir, pattern = paste0("(?i)^", table_name, "\\.csv$"), full.names = TRUE)
  if (length(matches) == 0) {
    return(NULL)
  }
  matches[[1]]
}

#' @keywords internal
#' @noRd
run_insert <- function(con, label, sql) {
  cat("Loading", label, "...\n")
  n <- DBI::dbExecute(con, sql)
  cat("  ->", n, "rows inserted\n")
  n
}

#' @keywords internal
#' @noRd
read_local_config <- function(filename) {
  config_path <- file.path("config", filename)
  if (file.exists(config_path)) yaml::read_yaml(config_path) else list()
}

#' @keywords internal
#' @noRd
load_mapping_macros <- function(con) {
  macros_path <- system.file("sql", "mapping_macros.sql", package = "omopduckdb")
  DBI::dbExecute(con, paste(readLines(macros_path), collapse = "\n"))
}

#' @keywords internal
#' @noRd
`%||%` <- function(x, y) if (is.null(x)) y else x
