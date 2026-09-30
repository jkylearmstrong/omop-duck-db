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
