#' Ingest custom mappings from a CSV into the OMOP source_to_concept_map table
#'
#' @param con Active DuckDB connection
#' @param csv_path Path to the CSV file containing custom source-to-concept mappings
#' @param default_vocabulary_id Fallback source_vocabulary_id if not present in CSV
#' @return Number of rows inserted. Errors when `source_code` or `target_concept_id` is missing from the CSV.
#' @export
import_source_to_concept_map <- function(con, csv_path, default_vocabulary_id = "Local") {
  if (!file.exists(csv_path)) {
    stop("Mapping file not found: ", csv_path)
  }

  DBI::dbExecute(con, sprintf(
    "CREATE OR REPLACE TEMPORARY VIEW _stcm_raw AS SELECT * FROM read_csv_auto(%s, union_by_name = true, all_varchar = true);",
    sql_path_literal(csv_path)
  ))

  cols_df <- DBI::dbGetQuery(con, "DESCRIBE SELECT * FROM _stcm_raw LIMIT 0")
  cols <- stats::setNames(cols_df$column_name, toupper(cols_df$column_name))
  on.exit(DBI::dbExecute(con, "DROP VIEW IF EXISTS _stcm_raw;"), add = TRUE)
  missing_req <- setdiff(c("SOURCE_CODE", "TARGET_CONCEPT_ID"), names(cols))
  if (length(missing_req)) {
    stop("Mapping file ", csv_path, " is missing required column(s): ", paste(tolower(missing_req), collapse = ", "),
         ". Found: ", paste(cols_df$column_name, collapse = ", "), call. = FALSE)
  }

  col_expr <- function(target_name, default_expr) {
    cand <- toupper(target_name)
    if (cand %in% names(cols)) {
      paste0('"', gsub('"', '""', cols[[cand]], fixed = TRUE), '"')
    } else {
      default_expr
    }
  }

  today_lit <- sprintf("DATE '%s'", format(Sys.Date(), "%Y-%m-%d"))

  source_code <- col_expr("source_code", "NULL")
  target_concept_id <- col_expr("target_concept_id", "0")
  vocab_lit <- .sql_str(default_vocabulary_id)
  source_vocab <- col_expr("source_vocabulary_id", vocab_lit)
  source_desc <- col_expr("source_code_description", "NULL")
  target_vocab <- col_expr("target_vocabulary_id", "''")
  start_date <- col_expr("valid_start_date", today_lit)
  end_date <- col_expr("valid_end_date", "DATE '2099-12-31'")
  inv_reason <- col_expr("invalid_reason", "NULL")

  sql <- sprintf("
    INSERT INTO source_to_concept_map (
        source_code,
        source_concept_id,
        source_vocabulary_id,
        source_code_description,
        target_concept_id,
        target_vocabulary_id,
        valid_start_date,
        valid_end_date,
        invalid_reason
    )
    SELECT
        %s AS source_code,
        0 AS source_concept_id,
        COALESCE(%s, %s) AS source_vocabulary_id,
        %s AS source_code_description,
        TRY_CAST(%s AS INTEGER) AS target_concept_id,
        COALESCE(%s, '') AS target_vocabulary_id,
        COALESCE(TRY_CAST(%s AS DATE), %s) AS valid_start_date,
        COALESCE(TRY_CAST(%s AS DATE), DATE '2099-12-31') AS valid_end_date,
        %s AS invalid_reason
    FROM _stcm_raw
    WHERE %s IS NOT NULL AND TRY_CAST(%s AS INTEGER) IS NOT NULL;
  ", source_code, source_vocab, vocab_lit, source_desc, target_concept_id,
     target_vocab, start_date, today_lit, end_date, inv_reason, source_code, target_concept_id)

  before <- DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM source_to_concept_map")$n
  DBI::dbExecute(con, sql)
  as.integer(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM source_to_concept_map")$n - before)
}

#' Import mappings exported from OHDSI Usagi into source_to_concept_map
#'
#' @param con Active DuckDB connection
#' @param usagi_csv_path Path to the exported Usagi CSV
#' @param source_vocabulary_id Name of the source vocabulary to assign
#' @param approved_only Only import records whose mappingStatus is exactly 'APPROVED' (a blank status is
#'   not approval). Rows with a missing or zero targetConceptId are never imported; when a code has several
#'   qualifying targets the lowest concept id is kept, deterministically.
#' @return Number of rows inserted
#' @export
import_usagi_mappings <- function(con, usagi_csv_path, source_vocabulary_id = "Custom", approved_only = TRUE) {
  if (!file.exists(usagi_csv_path)) {
    stop("Usagi mapping file not found: ", usagi_csv_path)
  }

  DBI::dbExecute(con, sprintf(
    "CREATE OR REPLACE TEMPORARY VIEW _usagi_raw AS SELECT * FROM read_csv_auto(%s, union_by_name = true, all_varchar = true);",
    sql_path_literal(usagi_csv_path)
  ))

  on.exit(DBI::dbExecute(con, "DROP VIEW IF EXISTS _usagi_raw;"), add = TRUE)
  where <- c("sourceCode IS NOT NULL", "TRY_CAST(targetConceptId AS INTEGER) IS NOT NULL",
             "TRY_CAST(targetConceptId AS INTEGER) <> 0")
  if (isTRUE(approved_only)) where <- c(where, "UPPER(TRIM(mappingStatus)) = 'APPROVED'")
  status_filter <- paste("WHERE", paste(where, collapse = " AND "))
  vocab_lit <- .sql_str(source_vocabulary_id)
  today_lit <- sprintf("DATE '%s'", format(Sys.Date(), "%Y-%m-%d"))

  sql <- sprintf("
    INSERT INTO source_to_concept_map (
        source_code,
        source_concept_id,
        source_vocabulary_id,
        source_code_description,
        target_concept_id,
        target_vocabulary_id,
        valid_start_date,
        valid_end_date,
        invalid_reason
    )
    SELECT
        sourceCode AS source_code,
        0 AS source_concept_id,
        %s AS source_vocabulary_id,
        sourceName AS source_code_description,
        TRY_CAST(targetConceptId AS INTEGER) AS target_concept_id,
        COALESCE(targetVocabularyId, '') AS target_vocabulary_id,
        %s AS valid_start_date,
        DATE '2099-12-31' AS valid_end_date,
        NULL AS invalid_reason
    FROM _usagi_raw
    %s
    QUALIFY ROW_NUMBER() OVER (PARTITION BY sourceCode ORDER BY TRY_CAST(targetConceptId AS INTEGER) ASC) = 1;
  ", vocab_lit, today_lit, status_filter)

  count_sql <- sprintf("SELECT COUNT(*) AS n FROM source_to_concept_map WHERE source_vocabulary_id = %s", vocab_lit)
  before <- DBI::dbGetQuery(con, count_sql)$n
  DBI::dbExecute(con, sql)
  as.integer(DBI::dbGetQuery(con, count_sql)$n - before)
}

#' Export unmapped (concept_id = 0) codes aggregated by frequency ready for OHDSI Usagi
#'
#' @param con Active DuckDB connection
#' @param table_name Name of OMOP table (e.g. condition_occurrence, drug_exposure, measurement, procedure_occurrence, provider)
#' @param output_csv Path to write the output CSV
#' @param min_frequency Minimum occurrence count to include
#' @return Number of unique unmapped codes exported
#' @export
export_unmapped_codes <- function(con, table_name, output_csv, min_frequency = 1) {
  column_map <- list(
    condition_occurrence = c("condition_concept_id", "condition_source_value"),
    drug_exposure = c("drug_concept_id", "drug_source_value"),
    measurement = c("measurement_concept_id", "measurement_source_value"),
    procedure_occurrence = c("procedure_concept_id", "procedure_source_value"),
    provider = c("specialty_concept_id", "specialty_source_value")
  )

  tbl <- tolower(table_name)
  if (!(tbl %in% names(column_map))) {
    stop("Unsupported table: ", table_name, ". Choose from: ", paste(names(column_map), collapse = ", "))
  }

  cols <- column_map[[tbl]]
  concept_col <- cols[[1]]
  source_col <- cols[[2]]

  sql <- sprintf("
    SELECT
        %s AS sourceCode,
        %s AS sourceName,
        COUNT(*) AS frequency
    FROM %s
    WHERE (%s = 0 OR %s IS NULL)
      AND %s IS NOT NULL
    GROUP BY %s
    HAVING COUNT(*) >= %d
    ORDER BY frequency DESC;
  ", source_col, source_col, tbl, concept_col, concept_col, source_col, source_col, min_frequency)

  df <- DBI::dbGetQuery(con, sql)
  dir.create(dirname(normalizePath(output_csv, mustWork = FALSE)), recursive = TRUE, showWarnings = FALSE)
  utils::write.csv(df, output_csv, row.names = FALSE)
  cat("Exported", nrow(df), "unmapped codes from", tbl, "to", output_csv, "\n")
  invisible(nrow(df))
}
