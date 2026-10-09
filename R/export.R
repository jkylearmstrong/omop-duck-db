#' Enterprise Multi-Target Export Engine for OMOP DuckDB CDM
#'
#' Exports OMOP CDM tables to partitioned Parquet, PostgreSQL, Oracle, or CSV/TSV.
#'
#' @param con Active DuckDB DBI connection.
#' @param target_type Character: `"parquet"`, `"postgres"`, `"oracle"`, `"csv"`, or `"tsv"`.
#' @param output_path Character destination directory path or connection string.
#' @param tables Optional character vector of table names (default: all populated CDM tables).
#' @param partition_by Optional named list of table -> SQL partition expression (for Parquet).
#' @param compression Character compression codec for Parquet (default `"zstd"`).
#' @return A list with export status and file paths.
#' @export
export_cdm <- function(con,
                       target_type,
                       output_path,
                       tables = NULL,
                       partition_by = NULL,
                       compression = "zstd") {
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
          ) TO '%s' (FORMAT PARQUET, PARTITION_BY (_part_col), COMPRESSION %s);
        ", part_expr, tbl, gsub("\\\\", "/", tbl_path), compression)
      } else {
        out_file <- file.path(output_path, paste0(tbl, ".parquet"))
        sql <- sprintf("COPY %s TO '%s' (FORMAT PARQUET, COMPRESSION %s);", tbl, gsub("\\\\", "/", out_file), compression)
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

  } else if (tgt == "oracle") {
    ddl_statements <- character(0)

    for (tbl in tables) {
      csv_file <- file.path(output_path, paste0(tbl, ".csv"))
      DBI::dbExecute(con, sprintf("COPY %s TO '%s' (FORMAT CSV, HEADER TRUE);", tbl, gsub("\\\\", "/", csv_file)))

      cols_info <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", tbl))
      oracle_cols <- character(nrow(cols_info))
      ctl_col_defs <- character(nrow(cols_info))

      for (j in seq_len(nrow(cols_info))) {
        col_name <- toupper(cols_info$column_name[j])
        col_type <- toupper(cols_info$column_type[j])
        ora_type <- if (grepl("BIGINT", col_type)) "NUMBER(19)"
                    else if (grepl("INT", col_type)) "NUMBER(10)"
                    else if (grepl("DATE", col_type)) "DATE"
                    else if (grepl("TIMESTAMP", col_type)) "TIMESTAMP"
                    else "VARCHAR2(255)"
        oracle_cols[j] <- sprintf("    %s %s", col_name, ora_type)
        ctl_col_defs[j] <- sprintf("    %s CHAR(4000)", col_name)
      }

      ddl_statements <- c(ddl_statements, sprintf("CREATE TABLE %s (\n%s\n);", toupper(tbl), paste(oracle_cols, collapse = ",\n")))

      ctl_content <- sprintf("LOAD DATA\nINFILE '%s.csv'\nINTO TABLE %s\nFIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\nTRAILING NULLCOLS\n(\n%s\n)\n",
                             tbl, toupper(tbl), paste(ctl_col_defs, collapse = ",\n"))
      ctl_file <- file.path(output_path, paste0(tbl, ".ctl"))
      writeLines(ctl_content, ctl_file)

      results[[tbl]] <- list(status = "EXPORTED", format = "oracle_sqlldr", ctl_file = ctl_file)
    }

    ddl_file <- file.path(output_path, "omop_oracle_ddl.sql")
    writeLines(paste(ddl_statements, collapse = "\n\n"), ddl_file)

    sh_file <- file.path(output_path, "load_oracle.sh")
    writeLines(c("#!/usr/bin/env bash", sprintf("sqlldr control=%s.ctl log=%s.log bad=%s.bad direct=true", tables, tables, tables)), sh_file)

  } else if (tgt %in% c("csv", "tsv")) {
    delim <- if (tgt == "tsv") "\\t" else ","
    ext <- tgt
    for (tbl in tables) {
      out_file <- file.path(output_path, paste0(tbl, ".", ext))
      sql <- sprintf("COPY %s TO '%s' (FORMAT CSV, HEADER TRUE, DELIMITER '%s');", tbl, gsub("\\\\", "/", out_file), delim)
      DBI::dbExecute(con, sql)
      results[[tbl]] <- list(status = "EXPORTED", format = tgt)
    }

  } else {
    stop("Unsupported export target_type '", target_type, "'. Use parquet, postgres, oracle, csv, or tsv.")
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
    out_posix <- gsub("\\\\", "/", normalizePath(output_dir, mustWork = FALSE))
    part_clause <- sprintf(", PARTITION_BY (%s)", paste(partition_by, collapse = ", "))
    sql <- sprintf("COPY %s TO '%s' (FORMAT PARQUET, COMPRESSION '%s'%s);",
                   table_name, out_posix, compression, part_clause)
  } else {
    out_file <- file.path(output_dir, paste0(table_name, ".parquet"))
    out_posix <- gsub("\\\\", "/", normalizePath(out_file, mustWork = FALSE))
    sql <- sprintf("COPY %s TO '%s' (FORMAT PARQUET, COMPRESSION '%s');",
                   table_name, out_posix, compression)
  }

  DBI::dbExecute(con, sql)
  normalizePath(output_dir, mustWork = FALSE)
}

