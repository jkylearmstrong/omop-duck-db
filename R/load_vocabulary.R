#' Load or refresh the OHDSI Athena vocabulary into the OMOP CDM database
#'
#' Each table is truncated and reloaded inside its own transaction, with a
#' row-count sanity check before commit -- so a bad/partial download can only
#' fail that one table (rolled back, left untouched), not half-clobber the
#' rest of the vocabulary.
#'
#' Supports CPT4 sanitization: Athena downloads without a UMLS license
#' output `CONCEPT_CPT4.csv` (or CPT4 concept entries) with NULL concept_names,
#' which violates the NOT NULL constraint on `concept.concept_name`.
#' When `sanitize_cpt4 = TRUE`, CPT4 concept names are automatically
#' sanitized to `'CPT4 ' || concept_code`.
#'
#' @param vocab_dir Path to an extracted Athena vocabulary download (a
#'   directory of tab-delimited `*.csv` files).
#' @param db_path Path to the DuckDB database file (schema must already exist,
#'   see [build_schema()]).
#' @param sanitize_cpt4 Automatically sanitize NULL concept_names for CPT4 concepts.
#' @param sanitize_all_null_names Automatically sanitize any NULL concept_names.
#' @return `TRUE` if every matched table loaded successfully, `FALSE` if any
#'   table failed and was rolled back.
#' @export
load_vocabulary <- function(vocab_dir, db_path = "omop_cdm.duckdb", sanitize_cpt4 = TRUE, sanitize_all_null_names = FALSE) {
  if (!dir.exists(vocab_dir)) {
    stop("Vocabulary directory not found: ", vocab_dir)
  }

  vocab_files <- list.files(path = vocab_dir, full.names = TRUE, recursive = TRUE, pattern = "\\.csv$")
  if (length(vocab_files) == 0) {
    stop("No CSV files found under: ", vocab_dir)
  }
  names(vocab_files) <- tolower(stringr::str_remove_all(basename(vocab_files), "(?i)\\.csv$"))

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    try(DBI::dbDisconnect(con, shutdown = TRUE), silent = TRUE)
    gc()
  })
  existing_tables <- tolower(DBI::dbListTables(con))

  all_ok <- TRUE
  for (table_name in names(vocab_files)) {
    if (table_name == "concept_cpt4") next # handled separately below
    if (!(table_name %in% existing_tables)) {
      cat("Skipping", table_name, "- no matching table in schema (run build_schema() first)\n")
      next
    }
    file_path <- vocab_files[[table_name]]
    cat("Reloading", toupper(table_name), "from", basename(file_path), "...\n")

    DBI::dbExecute(con, "BEGIN TRANSACTION")
    ok <- tryCatch(
      {
        DBI::dbExecute(con, paste0("DELETE FROM ", table_name))

        if (table_name == "concept") {
          name_expr <- if (isTRUE(sanitize_all_null_names)) {
            "COALESCE(concept_name, 'CPT4 ' || concept_code)"
          } else if (isTRUE(sanitize_cpt4)) {
            "COALESCE(concept_name, CASE WHEN vocabulary_id = 'CPT4' THEN 'CPT4 ' || concept_code ELSE NULL END)"
          } else {
            "concept_name"
          }

          DBI::dbExecute(con, sprintf("
            CREATE OR REPLACE TEMPORARY VIEW _temp_concept_raw AS
            SELECT * FROM read_csv('%s', delim = '\t', header = true, all_varchar = true);
          ", file_path))

          DBI::dbExecute(con, sprintf("
            INSERT INTO concept
            SELECT
                TRY_CAST(concept_id AS INTEGER),
                %s AS concept_name,
                domain_id,
                vocabulary_id,
                concept_class_id,
                standard_concept,
                concept_code,
                COALESCE(TRY_STRPTIME(valid_start_date, '%%Y%%m%%d')::DATE, TRY_CAST(valid_start_date AS DATE)),
                COALESCE(TRY_STRPTIME(valid_end_date, '%%Y%%m%%d')::DATE, TRY_CAST(valid_end_date AS DATE)),
                invalid_reason
            FROM _temp_concept_raw;
          ", name_expr))

          DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_concept_raw;")
        } else {
          DBI::dbExecute(con, paste0(
            "COPY ", table_name, " FROM '", file_path, "' (DELIMITER '\t', HEADER, DATEFORMAT '%Y%m%d');"
          ))
        }

        row_count <- DBI::dbGetQuery(con, paste0("SELECT COUNT(*) AS n FROM ", table_name))$n
        if (row_count == 0) stop("table loaded with zero rows")
        DBI::dbExecute(con, "COMMIT")
        cat("  ->", row_count, "rows (committed)\n")
        TRUE
      },
      error = function(e) {
        DBI::dbExecute(con, "ROLLBACK")
        cat("  Error loading", table_name, ":", conditionMessage(e), "- rolled back, table unchanged\n")
        FALSE
      }
    )
    all_ok <- all_ok && ok
  }

  # Ingest separate CONCEPT_CPT4.csv if present
  if ("concept_cpt4" %in% names(vocab_files) && "concept" %in% existing_tables) {
    cpt4_path <- vocab_files[["concept_cpt4"]]
    cat("Ingesting separate CPT4 concepts from", basename(cpt4_path), "...\n")
    tryCatch({
      DBI::dbExecute(con, sprintf("
        CREATE OR REPLACE TEMPORARY VIEW _temp_cpt4_raw AS
        SELECT * FROM read_csv('%s', delim = '\t', header = true, all_varchar = true);
      ", cpt4_path))
      DBI::dbExecute(con, "
        INSERT OR REPLACE INTO concept
        SELECT
            TRY_CAST(concept_id AS INTEGER),
            COALESCE(concept_name, 'CPT4 ' || concept_code) AS concept_name,
            domain_id,
            vocabulary_id,
            concept_class_id,
            standard_concept,
            concept_code,
            COALESCE(TRY_STRPTIME(valid_start_date, '%Y%m%d')::DATE, TRY_CAST(valid_start_date AS DATE)),
            COALESCE(TRY_STRPTIME(valid_end_date, '%Y%m%d')::DATE, TRY_CAST(valid_end_date AS DATE)),
            invalid_reason
        FROM _temp_cpt4_raw;
      ")
      DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_cpt4_raw;")
      cat("  -> CPT4 concepts sanitized and merged\n")
    }, error = function(e) {
      cat("  Warning: failed to load CONCEPT_CPT4.csv:", conditionMessage(e), "\n")
    })
  }

  invisible(all_ok)
}

#' Query vocabulary version and summary metrics from DuckDB OMOP CDM
#' @param con Active DuckDB connection
#' @return List with vocabulary_version, total_concepts, total_relationships, and counts by vocabulary.
#' @export
check_vocabulary_version <- function(con) {
  version_df <- tryCatch(
    DBI::dbGetQuery(con, "SELECT vocabulary_version FROM vocabulary WHERE vocabulary_id = 'None' LIMIT 1"),
    error = function(e) data.frame(vocabulary_version = "Unknown")
  )
  version <- if (nrow(version_df) > 0) version_df$vocabulary_version[[1]] else "Unknown"

  counts <- DBI::dbGetQuery(con, "
    SELECT vocabulary_id, COUNT(*) AS count 
    FROM concept 
    GROUP BY vocabulary_id 
    ORDER BY count DESC;
  ")
  total_concepts <- sum(counts$count)
  total_rel <- tryCatch(
    DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM concept_relationship")$n,
    error = function(e) 0
  )

  unmapped_discharge <- tryCatch(
    {
      tables <- tolower(DBI::dbListTables(con))
      if ("visit_occurrence" %in% tables) {
        DBI::dbGetQuery(con, "
          SELECT discharged_to_source_value, COUNT(*) AS count
          FROM visit_occurrence
          WHERE discharged_to_concept_id = 0
            AND discharged_to_source_value IS NOT NULL
            AND TRIM(discharged_to_source_value) != ''
          GROUP BY discharged_to_source_value
          ORDER BY count DESC
          LIMIT 10;
        ")
      } else {
        data.frame(discharged_to_source_value = character(), count = integer())
      }
    },
    error = function(e) data.frame(discharged_to_source_value = character(), count = integer())
  )

  list(
    vocabulary_version = version,
    total_concepts = total_concepts,
    total_relationships = total_rel,
    vocabularies = stats::setNames(as.list(counts$count), counts$vocabulary_id),
    unmapped_discharge_statuses = unmapped_discharge
  )
}
