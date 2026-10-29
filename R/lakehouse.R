#' Delta Lake and Apache Iceberg Lakehouse Connector for OMOP DuckDB
#'
#' @description
#' Connects to a Delta Lake or Apache Iceberg OMOP repository and dynamically
#' registers tables as views (`v_person`, `v_condition_occurrence`, etc.).
#'
#' @param lake_uri Root URI or filesystem path to the lakehouse storage directory.
#' @param format Lakehouse table format: `"delta"`, `"iceberg"`, or `"parquet"`. The DuckDB `delta` / `iceberg` extension is
#'   installed and loaded only when a table directory is a Delta (`_delta_log`) or Iceberg (`metadata`) table or the URI is remote;
#'   a plain parquet directory is read with `read_parquet()` whatever `format` says. Install the extension beforehand
#'   (`INSTALL delta`) on machines without internet access: a failed install or load can crash the R session.
#' @param s3_options Optional named list of S3 configuration options (endpoint, access keys, region).
#' @param tables Optional character vector of CDM tables to register.
#' @param output_con Optional existing DBI connection (creates new in-memory connection if `NULL`).
#' @param create_bare_aliases Logical indicating whether to create views without the `v_` prefix (default `TRUE`).
#'
#' @return Active DBI connection with registered views.
#' @export
omop_connect_lakehouse <- function(lake_uri,
                                   format = c("delta", "iceberg", "parquet"),
                                   s3_options = NULL,
                                   tables = NULL,
                                   output_con = NULL,
                                   create_bare_aliases = TRUE) {
  format <- match.arg(format)
  con <- if (!is.null(output_con)) output_con else DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  uri_str <- gsub("[/\\\\]+$", "", as.character(lake_uri))
  uri_str <- gsub("\\\\", "/", uri_str)

  default_tables <- c(
    "person", "observation_period", "visit_occurrence", "condition_occurrence",
    "procedure_occurrence", "drug_exposure", "measurement", "observation",
    "death", "care_site", "provider", "location", "cdm_source", "condition_era", "drug_era"
  )
  target_tables <- if (!is.null(tables)) tables else default_tables

  is_remote <- grepl("^(s3|s3a|http|https|azure|abfs|gcs)://", uri_str)
  if (is_remote) {
    tryCatch({
      DBI::dbExecute(con, "INSTALL httpfs; LOAD httpfs;")
    }, error = function(e) NULL)
  }

  if (!is.null(s3_options) && length(s3_options) > 0) {
    mapping <- list(
      endpoint = "s3_endpoint",
      access_key_id = "s3_access_key_id",
      secret_access_key = "s3_secret_access_key",
      region = "s3_region",
      use_ssl = "s3_use_ssl",
      url_style = "s3_url_style"
    )
    for (opt_name in names(s3_options)) {
      val <- s3_options[[opt_name]]
      var_key <- mapping[[tolower(opt_name)]]
      if (!is.null(var_key)) {
        val_str <- if (is.logical(val)) ifelse(val, "true", "false") else sprintf("'%s'", val)
        tryCatch({
          DBI::dbExecute(con, sprintf("SET %s = %s;", var_key, val_str))
        }, error = function(e) NULL)
      }
    }
  }

  delta_ok <- FALSE
  iceberg_ok <- FALSE

  # The delta / iceberg extension is loaded only for tables that are one (a `_delta_log` or `metadata` directory, or a remote
  # URI that cannot be inspected). A failed INSTALL / LOAD leaves the DuckDB R connection in a state that segfaults intermittently
  # on disconnect or garbage collection (reproduced on Windows with duckdb 2.0.0), so a plain parquet directory must never try.
  marker <- c(delta = "_delta_log", iceberg = "metadata")[format]
  is_lake_table <- function(tbl) is_remote || (!is.na(marker) && dir.exists(file.path(uri_str, tbl, marker)))
  needs_extension <- format != "parquet" && any(vapply(target_tables, is_lake_table, logical(1)))

  if (format == "delta" && needs_extension) {
    tryCatch({
      DBI::dbExecute(con, "INSTALL delta; LOAD delta;")
      delta_ok <- TRUE
    }, error = function(e) {
      tryCatch({
        DBI::dbExecute(con, "LOAD delta;")
        delta_ok <- TRUE
      }, error = function(e2) NULL)
    })
  } else if (format == "iceberg" && needs_extension) {
    tryCatch({
      DBI::dbExecute(con, "INSTALL iceberg; LOAD iceberg;")
      iceberg_ok <- TRUE
    }, error = function(e) {
      tryCatch({
        DBI::dbExecute(con, "LOAD iceberg;")
        iceberg_ok <- TRUE
      }, error = function(e2) NULL)
    })
  }

  for (tbl in target_tables) {
    table_path <- sprintf("%s/%s", uri_str, tbl)
    if (!is_remote && !dir.exists(table_path) && !file.exists(sprintf("%s/%s.parquet", uri_str, tbl))) {
      next
    }

    scan_expr <- NULL
    if (format == "delta" && delta_ok && is_lake_table(tbl)) {
      scan_expr <- sprintf("delta_scan('%s')", table_path)
    } else if (format == "iceberg" && iceberg_ok && is_lake_table(tbl)) {
      scan_expr <- sprintf("iceberg_scan('%s')", table_path)
    } else {
      if (!is_remote) {
        p_files <- list.files(table_path, pattern = "\\.parquet$", recursive = TRUE, full.names = TRUE)
        if (length(p_files) > 0) {
          scan_expr <- sprintf("read_parquet('%s/**/*.parquet')", table_path)
        } else if (file.exists(sprintf("%s/%s.parquet", uri_str, tbl))) {
          scan_expr <- sprintf("read_parquet('%s/%s.parquet')", uri_str, tbl)
        }
      } else {
        scan_expr <- sprintf("read_parquet('%s/**/*.parquet')", table_path)
      }
    }

    if (!is.null(scan_expr)) {
      tryCatch({
        DBI::dbExecute(con, sprintf("CREATE OR REPLACE VIEW v_%s AS SELECT * FROM %s;", tbl, scan_expr))
        if (create_bare_aliases) {
          DBI::dbExecute(con, sprintf("CREATE OR REPLACE VIEW %s AS SELECT * FROM v_%s;", tbl, tbl))
        }
      }, error = function(e) {
        # Fallback to direct parquet scanning if delta/iceberg scanner errored
        tryCatch({
          fallback <- sprintf("read_parquet('%s/**/*.parquet')", table_path)
          DBI::dbExecute(con, sprintf("CREATE OR REPLACE VIEW v_%s AS SELECT * FROM %s;", tbl, fallback))
          if (create_bare_aliases) {
            DBI::dbExecute(con, sprintf("CREATE OR REPLACE VIEW %s AS SELECT * FROM v_%s;", tbl, tbl))
          }
        }, error = function(e2) NULL)
      })
    }
  }

  con
}
