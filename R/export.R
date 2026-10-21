#' Enterprise Multi-Target Export Engine for OMOP DuckDB CDM
#'
#' Exports OMOP CDM tables to partitioned Parquet, PostgreSQL, Oracle, or CSV/TSV.
#'
#' @param con Active DuckDB DBI connection.
#' @param target_type Character: `"parquet"`, `"postgres"`, `"oracle"`, `"sqlserver"`, `"snowflake"`, `"bigquery"`, `"csv"`, or `"tsv"`.
#' @param output_path Character destination directory path or connection string.
#' @param tables Optional character vector of table names (default: all populated CDM tables).
#' @param partition_by Optional named list of table -> SQL partition expression (for Parquet).
#' @param compression Character compression codec for Parquet (default `"zstd"`).
#' @param oracle_max_identifier 30 (Oracle < 12.2, the safe default) or 128; longer identifiers raise an error.
#' @return A list with export status and file paths.
#' @export
export_cdm <- function(con,
                       target_type,
                       output_path,
                       tables = NULL,
                       partition_by = NULL,
                       compression = "zstd",
                       oracle_max_identifier = 30L) {
  tgt <- tolower(trimws(target_type))

  all_cdm_tables <- c(
    "person", "observation_period", "visit_occurrence", "condition_occurrence",
    "procedure_occurrence", "drug_exposure", "measurement", "death", "care_site",
    "provider", "location", "cdm_source", "condition_era", "drug_era"
  )

  if (is.null(tables)) {
    existing <- character(0)
    for (tbl in all_cdm_tables) {
      cnt <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM information_schema.tables WHERE lower(table_name) = '%s'", tolower(tbl)))$n[1]
      if (cnt > 0) {
        row_cnt <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s", tbl))$n[1]
        if (row_cnt > 0) existing <- c(existing, tbl)
      }
    }
    tables <- if (length(existing) > 0) existing else all_cdm_tables
  }

  if (tgt != "postgres") {
    dir.create(normalizePath(output_path, mustWork = FALSE), showWarnings = FALSE, recursive = TRUE)
  }

  results <- list()

  if (tgt == "parquet") {
    for (tbl in tables) {
      part_expr <- if (!is.null(partition_by)) partition_by[[tbl]] else NULL
      if (!is.null(part_expr)) {
        tbl_path <- file.path(output_path, tbl)
        dir.create(tbl_path, showWarnings = FALSE, recursive = TRUE)
        sql <- sprintf("
          COPY (
              SELECT *, %s AS _part_col 
              FROM %s
          ) TO %s (FORMAT PARQUET, PARTITION_BY (_part_col), COMPRESSION %s);
        ", part_expr, tbl, sql_path_literal(tbl_path), compression)
      } else {
        out_file <- file.path(output_path, paste0(tbl, ".parquet"))
        sql <- sprintf("COPY %s TO %s (FORMAT PARQUET, COMPRESSION %s);", tbl, sql_path_literal(out_file), compression)
      }
      DBI::dbExecute(con, sql)
      results[[tbl]] <- list(status = "EXPORTED", format = "parquet")
    }

  } else if (tgt == "postgres") {
    DBI::dbExecute(con, "INSTALL postgres; LOAD postgres;")
    pg_uri <- gsub("'", "''", output_path)
    DBI::dbExecute(con, sprintf("ATTACH '%s' AS target_pg (TYPE POSTGRES);", pg_uri))
    for (tbl in tables) {
      DBI::dbExecute(con, sprintf("INSERT INTO target_pg.%s SELECT * FROM %s;", tbl, tbl))
      results[[tbl]] <- list(status = "STREAMED_TO_POSTGRES")
    }
    DBI::dbExecute(con, "DETACH target_pg;")

  } else if (tgt %in% EXTRA_EXPORT_TARGETS) {
    model <- load_cdm_schema()
    unknown <- setdiff(tolower(tables), names(model))
    if (length(unknown)) stop("Tables not in the CDM v5.4 DDL: ", paste(unknown, collapse = ", "), call. = FALSE)
    writeLines(generate_ddl(tgt, tables, schema = model, oracle_max_identifier = oracle_max_identifier),
               file.path(output_path, sprintf("omop_%s_ddl.sql", tgt)), sep = "")
    for (tbl in tables) {
      cols <- model[[tolower(tbl)]]
      if (tgt %in% c("oracle", "sqlserver")) {
        data <- file.path(output_path, paste0(tbl, ".csv"))
        DBI::dbExecute(con, sprintf("COPY (%s) TO %s (FORMAT CSV, HEADER TRUE);", .csv_select(tbl, cols), sql_path_literal(data)))
        results[[tbl]] <- list(status = "EXPORTED", format = paste0(tgt, "_csv"))
        if (tgt == "oracle") {
          ctl_file <- file.path(output_path, paste0(tbl, ".ctl"))
          writeLines(.oracle_ctl(tbl, cols, oracle_max_identifier), ctl_file, sep = "")
          results[[tbl]] <- list(status = "EXPORTED", format = "oracle_sqlldr", ctl_file = ctl_file)
        }
      } else {
        data <- file.path(output_path, paste0(tbl, ".parquet"))
        DBI::dbExecute(con, sprintf("COPY %s TO %s (FORMAT PARQUET, COMPRESSION %s);", tbl, sql_path_literal(data), compression))
        results[[tbl]] <- list(status = "EXPORTED", format = paste0(tgt, "_parquet"))
      }
    }
    if (tgt == "oracle") {
      writeLines(c("#!/usr/bin/env bash", "set -euo pipefail",
                   sprintf("sqlldr userid=$ORA_CONN control=%s.ctl log=%s.log bad=%s.bad", tables, tables, tables)),
                 file.path(output_path, "load_oracle.sh"))
    } else if (tgt == "sqlserver") {
      writeLines(.sqlserver_load(tables), file.path(output_path, "load_sqlserver.sql"), sep = "")
    } else if (tgt == "snowflake") {
      writeLines(.snowflake_load(tables), file.path(output_path, "load_snowflake.sql"), sep = "")
    } else {
      writeLines(.bigquery_load(tables), file.path(output_path, "load_bigquery.sh"), sep = "")
    }

  } else if (tgt %in% c("csv", "tsv")) {
    delim <- if (tgt == "tsv") "\\t" else ","
    ext <- tgt
    for (tbl in tables) {
      out_file <- file.path(output_path, paste0(tbl, ".", ext))
      sql <- sprintf("COPY %s TO %s (FORMAT CSV, HEADER TRUE, DELIMITER '%s');", tbl, sql_path_literal(out_file), delim)
      DBI::dbExecute(con, sql)
      results[[tbl]] <- list(status = "EXPORTED", format = tgt)
    }

  } else {
    stop("Unsupported export target_type '", target_type, "'. Use parquet, postgres, oracle, sqlserver, snowflake, bigquery, csv, or tsv.")
  }

  list(
    target_type = tgt,
    output_path = output_path,
    exported_tables = results
  )
}

#' Export a CDM Table to Parquet
#'
#' @param db_path Character path to DuckDB database file or an active DBI connection.
#' @param table_name Character table or view name in DuckDB to export.
#' @param output_dir Character destination directory path.
#' @param partition_by Optional character vector of column names to partition by.
#' @param compression Character compression codec (default `"zstd"`).
#' @return Absolute path to destination directory.
#' @export
export_to_parquet <- function(db_path,
                               table_name,
                               output_dir,
                               partition_by = NULL,
                               compression = "zstd") {
  should_disconnect <- FALSE
  if (is.character(db_path)) {
    con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
    should_disconnect <- TRUE
  } else {
    con <- db_path
  }
  on.exit({
    if (should_disconnect) DBI::dbDisconnect(con, shutdown = TRUE)
  }, add = TRUE)

  dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)

  if (!is.null(partition_by) && length(partition_by) > 0) {
    out_posix <- sql_path_literal(output_dir)
    part_clause <- sprintf(", PARTITION_BY (%s)", paste(partition_by, collapse = ", "))
    sql <- sprintf("COPY %s TO %s (FORMAT PARQUET, COMPRESSION '%s'%s);",
                   table_name, out_posix, compression, part_clause)
  } else {
    out_file <- file.path(output_dir, paste0(table_name, ".parquet"))
    out_posix <- sql_path_literal(out_file)
    sql <- sprintf("COPY %s TO %s (FORMAT PARQUET, COMPRESSION '%s');",
                   table_name, out_posix, compression)
  }

  DBI::dbExecute(con, sql)
  normalizePath(output_dir, mustWork = FALSE)
}

