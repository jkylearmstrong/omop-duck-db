"""Retroactive concept remapping and vocabulary migration helpers for OMOP CDM in DuckDB."""

TABLE_REMAP_CONFIG = {
    "condition_occurrence": {
        "id_col": "condition_occurrence_id",
        "concept_col": "condition_concept_id",
        "source_concept_col": "condition_source_concept_id",
        "source_value_col": "condition_source_value",
        "default_vocabs": ["ICD10CM", "ICD9CM", "SNOMED"],
    },
    "drug_exposure": {
        "id_col": "drug_exposure_id",
        "concept_col": "drug_concept_id",
        "source_concept_col": "drug_source_concept_id",
        "source_value_col": "drug_source_value",
        "default_vocabs": ["RxNorm", "NDC"],
    },
    "measurement": {
        "id_col": "measurement_id",
        "concept_col": "measurement_concept_id",
        "source_concept_col": "measurement_source_concept_id",
        "source_value_col": "measurement_source_value",
        "default_vocabs": ["LOINC"],
    },
    "procedure_occurrence": {
        "id_col": "procedure_occurrence_id",
        "concept_col": "procedure_concept_id",
        "source_concept_col": "procedure_source_concept_id",
        "source_value_col": "procedure_source_value",
        "default_vocabs": ["CPT4", "HCPCS", "ICD10PCS", "ICD9Proc"],
    },
    "provider": {
        "id_col": "provider_id",
        "concept_col": "specialty_concept_id",
        "source_concept_col": "specialty_source_concept_id",
        "source_value_col": "specialty_source_value",
        "default_vocabs": ["NUCC"],
    },
}


def remap_cdm_table(con, table_name, dry_run=False):
    """Remap unmapped (concept_id = 0) or deprecated concepts in an OMOP table.

    Resolves codes against:
      1. source_to_concept_map (STCM)
      2. concept + concept_relationship ('Maps to')
      3. concept (where standard_concept = 'S')

    Args:
        con: Active DuckDB connection.
        table_name: One of condition_occurrence, drug_exposure, measurement,
                    procedure_occurrence, provider.
        dry_run: If True, calculates and returns statistics without modifying the table.

    Returns:
        A dict with summary statistics of the remapping operation.
    """
    tbl = table_name.lower()
    if tbl not in TABLE_REMAP_CONFIG:
        raise ValueError(f"Unsupported table: {table_name}. Choose from: {list(TABLE_REMAP_CONFIG.keys())}")

    cfg = TABLE_REMAP_CONFIG[tbl]
    id_col = cfg["id_col"]
    concept_col = cfg["concept_col"]
    src_concept_col = cfg["source_concept_col"]
    src_val_col = cfg["source_value_col"]
    vocabs_str = ", ".join(f"'{v}'" for v in cfg["default_vocabs"])

    # Build resolution CTE for candidate target concepts
    # Prioritizes STCM, then standard 'Maps to', then direct standard concepts
    resolution_view = f"""
        CREATE OR REPLACE TEMPORARY VIEW _remap_candidates_{tbl} AS
        WITH target_resolution AS (
            -- 1. Check STCM
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
                  AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
            ) WHERE rn = 1

            UNION ALL

            -- 2. Check concept + concept_relationship ('Maps to')
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
            WHERE c.vocabulary_id IN ({vocabs_str})

            UNION ALL

            -- 3. Check direct standard concept
            SELECT
                c.concept_code AS source_code,
                c.concept_id AS resolved_concept_id,
                c.concept_id AS resolved_source_concept_id
            FROM concept c
            WHERE c.vocabulary_id IN ({vocabs_str})
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
    """
    con.execute(resolution_view)

    # Calculate metrics
    stats = con.execute(f"""
        WITH target_rows AS (
            SELECT 
                t.{id_col},
                t.{concept_col},
                t.{src_val_col},
                r.resolved_concept_id,
                r.resolved_source_concept_id
            FROM {tbl} t
            LEFT JOIN _remap_candidates_{tbl} r
              ON r.source_code = t.{src_val_col}
            WHERE t.{concept_col} = 0
               OR t.{concept_col} IN (
                   SELECT concept_id FROM concept 
                   WHERE invalid_reason IS NOT NULL AND invalid_reason != ''
               )
        )
        SELECT
            (SELECT COUNT(*) FROM {tbl}) AS total_rows,
            COUNT(*) AS eligible_rows,
            COUNT(CASE WHEN resolved_concept_id IS NOT NULL THEN 1 END) AS remappable_rows
        FROM target_rows;
    """).fetchone()

    total_rows, eligible_rows, remappable_rows = stats

    result = {
        "table": tbl,
        "total_rows": total_rows,
        "eligible_for_remapping": eligible_rows,
        "remappable_rows": remappable_rows,
        "dry_run": dry_run,
        "remapped": 0,
    }

    if not dry_run and remappable_rows > 0:
        # Perform vectorized in-place update
        con.execute(f"""
            UPDATE {tbl}
            SET 
                {concept_col} = r.resolved_concept_id,
                {src_concept_col} = CASE 
                    WHEN r.resolved_source_concept_id != 0 THEN r.resolved_source_concept_id 
                    ELSE {src_concept_col} 
                END
            FROM _remap_candidates_{tbl} r
            WHERE r.source_code = {tbl}.{src_val_col}
              AND ({tbl}.{concept_col} = 0 OR {tbl}.{concept_col} IN (
                  SELECT concept_id FROM concept 
                  WHERE invalid_reason IS NOT NULL AND invalid_reason != ''
              ));
        """)
        result["remapped"] = remappable_rows
        print(f"[{tbl}] Remapped {remappable_rows} of {eligible_rows} eligible rows.")
    elif dry_run:
        print(f"[{tbl}] Dry run: {remappable_rows} of {eligible_rows} eligible rows can be remapped.")

    con.execute(f"DROP VIEW IF EXISTS _remap_candidates_{tbl};")
    return result


def remap_all(con, dry_run=False, rebuild_eras=True):
    """Remap all supported clinical tables and optionally rebuild eras."""
    from omop_etl.build_omop_cdm import build_condition_era, build_drug_era

    results = {}
    for tbl in TABLE_REMAP_CONFIG.keys():
        results[tbl] = remap_cdm_table(con, tbl, dry_run=dry_run)

    if not dry_run and rebuild_eras:
        print("Rebuilding condition_era and drug_era after remapping ...")
        build_condition_era(con)
        build_drug_era(con)

    return results


def auto_remap_unmapped(
    con,
    table_name: str,
    strip_formatting: bool = True,
    dry_run: bool = False,
) -> dict:
    """Heuristic remapping and diagnostic repair for unmapped (concept_id = 0) records.

    Normalizes source code strings (stripping periods, spaces, punctuation) and searches
    Athena concept codes and relationships for matching standard concepts.

    Args:
        con: Active DuckDB connection.
        table_name: Clinical table name (e.g. 'condition_occurrence', 'measurement', 'drug_exposure').
        strip_formatting: Whether to strip periods and whitespace for fuzzy matching (default True).
        dry_run: If True, returns diagnostic counts without executing the update.

    Returns:
        dict: Summary of unmapped rows inspected and remapped.
    """
    tbl = table_name.lower().strip()
    if tbl not in TABLE_REMAP_CONFIG:
        raise ValueError(f"Unsupported table: {table_name}. Choose from: {list(TABLE_REMAP_CONFIG.keys())}")

    cfg = TABLE_REMAP_CONFIG[tbl]
    concept_col = cfg["concept_col"]
    src_val_col = cfg["source_value_col"]
    src_concept_col = cfg["source_concept_col"]
    vocabs_str = ", ".join(f"'{v}'" for v in cfg["default_vocabs"])

    clean_expr = f"UPPER(TRIM(REPLACE({src_val_col}, '.', '')))" if strip_formatting else f"UPPER(TRIM({src_val_col}))"
    clean_code = "UPPER(TRIM(REPLACE(c.concept_code, '.', '')))" if strip_formatting else "UPPER(TRIM(c.concept_code))"

    diag_sql = f"""
    CREATE OR REPLACE TEMPORARY VIEW _heuristic_remap_{tbl} AS
    WITH unmapped AS (
        SELECT DISTINCT {src_val_col} AS raw_source_val, {clean_expr} AS clean_val
        FROM {tbl}
        WHERE {concept_col} = 0 AND {src_val_col} IS NOT NULL AND TRIM({src_val_col}) != ''
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
        JOIN concept c ON {clean_code} = u.clean_val AND c.vocabulary_id IN ({vocabs_str})
        LEFT JOIN concept_relationship cr ON cr.concept_id_1 = c.concept_id AND cr.relationship_id = 'Maps to'
    )
    SELECT raw_source_val, target_concept_id, source_concept_id
    FROM matched
    WHERE rn = 1;
    """
    con.execute(diag_sql)

    counts = con.execute(f"""
        SELECT 
            (SELECT COUNT(*) FROM {tbl} WHERE {concept_col} = 0) AS total_unmapped,
            COUNT(t.{concept_col}) AS remappable_rows
        FROM {tbl} t
        JOIN _heuristic_remap_{tbl} m ON t.{src_val_col} = m.raw_source_val
        WHERE t.{concept_col} = 0;
    """).fetchone()

    total_unmapped, remappable_rows = counts[0], counts[1]

    if not dry_run and remappable_rows > 0:
        con.execute(f"""
            UPDATE {tbl}
            SET 
                {concept_col} = m.target_concept_id,
                {src_concept_col} = CASE 
                    WHEN m.source_concept_id IS NOT NULL AND m.source_concept_id != 0 THEN m.source_concept_id 
                    ELSE {src_concept_col} 
                END
            FROM _heuristic_remap_{tbl} m
            WHERE {tbl}.{src_val_col} = m.raw_source_val
              AND {tbl}.{concept_col} = 0;
        """)

    con.execute(f"DROP VIEW IF EXISTS _heuristic_remap_{tbl};")

    return {
        "table": tbl,
        "total_unmapped": total_unmapped,
        "remapped": remappable_rows if not dry_run else 0,
        "remappable_rows": remappable_rows,
        "dry_run": dry_run,
    }

