#' Retroactive concept remapping and vocabulary migration helpers for OMOP CDM in DuckDB
#'
#' @keywords internal
#' @noRd
.TABLE_REMAP_CONFIG <- list(
  condition_occurrence = list(
    id_col = "condition_occurrence_id",
    concept_col = "condition_concept_id",
    source_concept_col = "condition_source_concept_id",
    source_value_col = "condition_source_value",
    default_vocabs = c("ICD10CM", "ICD9CM", "SNOMED")
  ),
  drug_exposure = list(
    id_col = "drug_exposure_id",
    concept_col = "drug_concept_id",
    source_concept_col = "drug_source_concept_id",
    source_value_col = "drug_source_value",
    default_vocabs = c("RxNorm", "NDC")
  ),
  measurement = list(
    id_col = "measurement_id",
    concept_col = "measurement_concept_id",
    source_concept_col = "measurement_source_concept_id",
    source_value_col = "measurement_source_value",
    default_vocabs = c("LOINC")
  ),
  procedure_occurrence = list(
    id_col = "procedure_occurrence_id",
    concept_col = "procedure_concept_id",
    source_concept_col = "procedure_source_concept_id",
    source_value_col = "procedure_source_value",
    default_vocabs = c("CPT4", "HCPCS", "ICD10PCS", "ICD9Proc")
  ),
  provider = list(
    id_col = "provider_id",
    concept_col = "specialty_concept_id",
    source_concept_col = "specialty_source_concept_id",
    source_value_col = "specialty_source_value",
    default_vocabs = c("NUCC")
  )
)

#' Remap unmapped (concept_id = 0) or deprecated concepts in an OMOP table
#'
#' @param con Active DuckDB connection
#' @param table_name Name of OMOP table to remap
#' @param dry_run If TRUE, calculates and returns statistics without modifying the table
#' @return List with remapping metrics (total_rows, eligible_for_remapping, remappable_rows, remapped)
#' @export
remap_cdm_table <- function(con, table_name, dry_run = FALSE) {
  tbl <- tolower(table_name)
  if (!(tbl %in% names(.TABLE_REMAP_CONFIG))) {
    stop("Unsupported table: ", table_name, ". Choose from: ", paste(names(.TABLE_REMAP_CONFIG), collapse = ", "))
  }

  cfg <- .TABLE_REMAP_CONFIG[[tbl]]
  id_col <- cfg$id_col
  concept_col <- cfg$concept_col
  src_concept_col <- cfg$source_concept_col
  src_val_col <- cfg$source_value_col
  vocabs_str <- paste(sprintf("'%s'", cfg$default_vocabs), collapse = ", ")
  today_lit <- sprintf("DATE '%s'", format(Sys.Date(), "%Y-%m-%d"))

  res_view_sql <- sprintf("
    CREATE OR REPLACE TEMPORARY VIEW _remap_candidates_%s AS
    WITH target_resolution AS (
        SELECT
            source_code,
            target_concept_id AS resolved_concept_id,
            0 AS resolved_source_concept_id
        FROM (
            SELECT
                source_code,
                target_concept_id,
                ROW_NUMBER() OVER (PARTITION BY source_code ORDER BY valid_start_date DESC) AS rn
            FROM source_to_concept_map
            WHERE (invalid_reason IS NULL OR invalid_reason = '')
              AND (valid_end_date IS NULL OR valid_end_date >= %s)
        ) WHERE rn = 1

        UNION ALL

        SELECT
            c.concept_code AS source_code,
            cr.concept_id_2 AS resolved_concept_id,
            c.concept_id AS resolved_source_concept_id
        FROM concept c
        JOIN (
            SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
            FROM concept_relationship
            WHERE relationship_id = 'Maps to'
            GROUP BY concept_id_1
        ) cr ON cr.concept_id_1 = c.concept_id
        WHERE c.vocabulary_id IN (%s)

        UNION ALL

        SELECT
            c.concept_code AS source_code,
            c.concept_id AS resolved_concept_id,
            c.concept_id AS resolved_source_concept_id
        FROM concept c
        WHERE c.vocabulary_id IN (%s)
          AND c.standard_concept = 'S'
    )
    SELECT
        source_code,
        resolved_concept_id,
        resolved_source_concept_id
    FROM (
        SELECT
            source_code,
            resolved_concept_id,
            resolved_source_concept_id,
            ROW_NUMBER() OVER (PARTITION BY source_code ORDER BY resolved_concept_id DESC) AS rnk
        FROM target_resolution
        WHERE resolved_concept_id != 0 AND resolved_concept_id IS NOT NULL
    ) WHERE rnk = 1;
  ", tbl, today_lit, vocabs_str, vocabs_str)

  DBI::dbExecute(con, res_view_sql)

  stats_sql <- sprintf("
    WITH target_rows AS (
        SELECT
            t.%s,
            t.%s,
            t.%s,
            r.resolved_concept_id,
            r.resolved_source_concept_id
        FROM %s t
        LEFT JOIN _remap_candidates_%s r
          ON r.source_code = t.%s
        WHERE t.%s = 0
           OR t.%s IN (
               SELECT concept_id FROM concept
               WHERE invalid_reason IS NOT NULL AND invalid_reason != ''
           )
    )
    SELECT
        (SELECT COUNT(*) FROM %s) AS total_rows,
        COUNT(*) AS eligible_rows,
        COUNT(CASE WHEN resolved_concept_id IS NOT NULL THEN 1 END) AS remappable_rows
    FROM target_rows;
  ", id_col, concept_col, src_val_col, tbl, tbl, src_val_col, concept_col, concept_col, tbl)

  stats_df <- DBI::dbGetQuery(con, stats_sql)
  total_rows <- stats_df$total_rows[[1]]
  eligible_rows <- stats_df$eligible_rows[[1]]
  remappable_rows <- stats_df$remappable_rows[[1]]

  res <- list(
    table = tbl,
    total_rows = total_rows,
    eligible_for_remapping = eligible_rows,
    remappable_rows = remappable_rows,
    dry_run = dry_run,
    remapped = 0
  )

  if (!dry_run && remappable_rows > 0) {
    update_sql <- sprintf("
      UPDATE %s
      SET
          %s = r.resolved_concept_id,
          %s = CASE
              WHEN r.resolved_source_concept_id != 0 THEN r.resolved_source_concept_id
              ELSE %s
          END
      FROM _remap_candidates_%s r
      WHERE r.source_code = %s.%s
        AND (%s.%s = 0 OR %s.%s IN (
            SELECT concept_id FROM concept
            WHERE invalid_reason IS NOT NULL AND invalid_reason != ''
        ));
    ", tbl, concept_col, src_concept_col, src_concept_col, tbl, tbl, src_val_col, tbl, concept_col, tbl, concept_col)

    DBI::dbExecute(con, update_sql)
    res$remapped <- remappable_rows
    cat(sprintf("[%s] Remapped %d of %d eligible rows.\n", tbl, remappable_rows, eligible_rows))
  } else if (dry_run) {
    cat(sprintf("[%s] Dry run: %d of %d eligible rows can be remapped.\n", tbl, remappable_rows, eligible_rows))
  }

  DBI::dbExecute(con, sprintf("DROP VIEW IF EXISTS _remap_candidates_%s;", tbl))
  res
}

#' Remap all supported clinical tables and optionally rebuild eras
#'
#' @param con Active DuckDB connection
#' @param dry_run If TRUE, calculates and returns statistics without modifying tables
#' @param rebuild_eras Rebuild condition_era and drug_era if tables are modified
#' @return Named list of remapping results per table
#' @export
remap_all <- function(con, dry_run = FALSE, rebuild_eras = TRUE) {
  results <- list()
  for (tbl in names(.TABLE_REMAP_CONFIG)) {
    results[[tbl]] <- remap_cdm_table(con, tbl, dry_run = dry_run)
  }

  if (!dry_run && isTRUE(rebuild_eras)) {
    cat("Rebuilding condition_era and drug_era after remapping ...\n")
    build_condition_era(con)
    build_drug_era(con)
  }

  invisible(results)
}

#' Heuristic Remapping and Diagnostic Repair for Unmapped Records
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param table_name Clinical table name (e.g. `"condition_occurrence"`, `"measurement"`, `"drug_exposure"`).
#' @param strip_formatting Logical indicating whether to strip periods and spaces (default `TRUE`).
#' @param dry_run Logical indicating whether to calculate without updating (default `FALSE`).
#' @return A list with `table`, `total_unmapped`, `remapped`, `remappable_rows`, and `dry_run`.
#' @export
auto_remap_unmapped <- function(con, table_name, strip_formatting = TRUE, dry_run = FALSE) {
  tbl <- tolower(trimws(table_name))
  if (!tbl %in% names(.TABLE_REMAP_CONFIG)) {
    stop(sprintf("Unsupported table: %s. Choose from: %s", table_name,
                 paste(names(.TABLE_REMAP_CONFIG), collapse = ", ")), call. = FALSE)
  }

  cfg <- .TABLE_REMAP_CONFIG[[tbl]]
  concept_col <- cfg$concept_col
  src_val_col <- cfg$source_value_col
  src_concept_col <- cfg$source_concept_col
  vocabs_str <- paste(sprintf("'%s'", cfg$default_vocabs), collapse = ", ")

  clean_expr <- if (isTRUE(strip_formatting)) sprintf("UPPER(TRIM(REPLACE(%s, '.', '')))", src_val_col) else sprintf("UPPER(TRIM(%s))", src_val_col)
  clean_code <- if (isTRUE(strip_formatting)) "UPPER(TRIM(REPLACE(c.concept_code, '.', '')))" else "UPPER(TRIM(c.concept_code))"

  diag_sql <- sprintf("
    CREATE OR REPLACE TEMPORARY VIEW _heuristic_remap_%s AS
    WITH unmapped AS (
        SELECT DISTINCT %s AS raw_source_val, %s AS clean_val
        FROM %s
        WHERE %s = 0 AND %s IS NOT NULL AND TRIM(%s) != ''
    ),
    matched AS (
        SELECT
            u.raw_source_val,
            COALESCE(cr.concept_id_2, c.concept_id) AS target_concept_id,
            c.concept_id AS source_concept_id,
            ROW_NUMBER() OVER (
                PARTITION BY u.raw_source_val
                ORDER BY CASE WHEN cr.concept_id_2 IS NOT NULL THEN 1 ELSE 2 END, c.concept_id
            ) AS rn
        FROM unmapped u
        JOIN concept c ON %s = u.clean_val AND c.vocabulary_id IN (%s)
        LEFT JOIN concept_relationship cr ON cr.concept_id_1 = c.concept_id AND cr.relationship_id = 'Maps to'
    )
    SELECT raw_source_val, target_concept_id, source_concept_id
    FROM matched
    WHERE rn = 1;
  ", tbl, src_val_col, clean_expr, tbl, concept_col, src_val_col, src_val_col, clean_code, vocabs_str)

  DBI::dbExecute(con, diag_sql)

  counts <- DBI::dbGetQuery(con, sprintf("
    SELECT
        (SELECT COUNT(*) FROM %s WHERE %s = 0) AS total_unmapped,
        COUNT(t.%s) AS remappable_rows
    FROM %s t
    JOIN _heuristic_remap_%s m ON t.%s = m.raw_source_val
    WHERE t.%s = 0;
  ", tbl, concept_col, concept_col, tbl, tbl, src_val_col, concept_col))

  total_unmapped <- as.integer(counts$total_unmapped[[1]])
  remappable_rows <- as.integer(counts$remappable_rows[[1]])

  if (!dry_run && remappable_rows > 0) {
    DBI::dbExecute(con, sprintf("
      UPDATE %s
      SET
          %s = m.target_concept_id,
          %s = CASE
              WHEN m.source_concept_id IS NOT NULL AND m.source_concept_id != 0 THEN m.source_concept_id
              ELSE %s
          END
      FROM _heuristic_remap_%s m
      WHERE %s.%s = m.raw_source_val
        AND %s.%s = 0;
    ", tbl, concept_col, src_concept_col, src_concept_col, tbl, tbl, src_val_col, tbl, concept_col))
  }

  DBI::dbExecute(con, sprintf("DROP VIEW IF EXISTS _heuristic_remap_%s;", tbl))

  list(
    table = tbl,
    total_unmapped = total_unmapped,
    remapped = if (!dry_run) remappable_rows else 0L,
    remappable_rows = remappable_rows,
    dry_run = dry_run
  )
}

