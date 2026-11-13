"""Source-to-concept mapping helpers and OHDSI Usagi integrations for OMOP CDM."""

import csv
import os

from omop_etl.paths import sql_path_literal

def sql_str(value):
    """Single-quoted SQL string literal with embedded quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def sql_int(value, what="value"):
    """Coerce to int for interpolation; reject bools and non-integral values."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be an integer, got {value!r}.")
    try:
        out = int(value)
        if isinstance(value, float) and out != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an integer, got {value!r}.") from None
    return out


_STCM_INSERT_COLUMNS = """
            source_code,
            source_concept_id,
            source_vocabulary_id,
            source_code_description,
            target_concept_id,
            target_vocabulary_id,
            valid_start_date,
            valid_end_date,
            invalid_reason
"""


def import_source_to_concept_map(con, csv_path, default_vocabulary_id="Local"):
    """Ingest custom mappings from a CSV into the OMOP source_to_concept_map table.

    Returns the number of rows inserted. Raises ``ValueError`` if a required column is absent
    (a missing ``target_concept_id`` used to silently map every code to concept 0).

    Expected CSV columns (case-insensitive):
      source_code (required)
      target_concept_id (required)
      source_vocabulary_id (optional, defaults to default_vocabulary_id)
      source_code_description (optional)
      target_vocabulary_id (optional, default '')
      valid_start_date (optional, default CURRENT_DATE)
      valid_end_date (optional, default '2099-12-31')
      invalid_reason (optional, default NULL)
    """
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"Mapping file not found: {csv_path}")

    con.execute(f"""
        CREATE OR REPLACE TEMPORARY VIEW _stcm_raw AS
        SELECT * FROM read_csv_auto({sql_path_literal(csv_path)}, union_by_name = true, all_varchar = true);
    """)
    try:
        cols = {r[0].upper(): r[0] for r in con.execute("DESCRIBE SELECT * FROM _stcm_raw LIMIT 0").fetchall()}

        missing = [c for c in ("source_code", "target_concept_id") if c.upper() not in cols]
        if missing:
            raise ValueError(
                f"Mapping file {csv_path} is missing required column(s): {', '.join(missing)}. "
                f"Found: {sorted(cols.values())}."
            )

        def col_expr(target_name, default_expr):
            actual = cols.get(target_name.upper())
            if actual is None:
                return default_expr
            return '"' + actual.replace('"', '""') + '"'

        vocab_lit = sql_str(default_vocabulary_id)
        source_code = col_expr("source_code", "NULL")
        target_concept_id = col_expr("target_concept_id", "0")
        source_vocab = col_expr("source_vocabulary_id", vocab_lit)
        source_desc = col_expr("source_code_description", "NULL")
        target_vocab = col_expr("target_vocabulary_id", "''")
        start_date = col_expr("valid_start_date", "CURRENT_DATE")
        end_date = col_expr("valid_end_date", "DATE '2099-12-31'")
        inv_reason = col_expr("invalid_reason", "NULL")

        sql = f"""
            INSERT INTO source_to_concept_map ({_STCM_INSERT_COLUMNS})
            SELECT
                {source_code} AS source_code,
                0 AS source_concept_id,
                COALESCE({source_vocab}, {vocab_lit}) AS source_vocabulary_id,
                {source_desc} AS source_code_description,
                TRY_CAST({target_concept_id} AS INTEGER) AS target_concept_id,
                COALESCE({target_vocab}, '') AS target_vocabulary_id,
                COALESCE(TRY_CAST({start_date} AS DATE), CURRENT_DATE) AS valid_start_date,
                COALESCE(TRY_CAST({end_date} AS DATE), DATE '2099-12-31') AS valid_end_date,
                {inv_reason} AS invalid_reason
            FROM _stcm_raw
            WHERE {source_code} IS NOT NULL
              AND TRY_CAST({target_concept_id} AS INTEGER) IS NOT NULL;
        """
        before = con.execute("SELECT COUNT(*) FROM source_to_concept_map").fetchone()[0]
        con.execute(sql)
        after = con.execute("SELECT COUNT(*) FROM source_to_concept_map").fetchone()[0]
        return after - before
    finally:
        con.execute("DROP VIEW IF EXISTS _stcm_raw;")


def import_usagi_mappings(con, usagi_csv_path, source_vocabulary_id="Custom", approved_only=True):
    """Import mappings exported from OHDSI Usagi into source_to_concept_map.

    Returns the number of rows inserted. When ``approved_only`` is true only rows whose
    ``mappingStatus`` is exactly APPROVED (case-insensitive) are imported - a blank status is *not*
    approval. Rows with a NULL/0 ``targetConceptId`` are always skipped. When a code has several
    qualifying targets the lowest concept id wins, deterministically.

    OHDSI Usagi outputs columns:
      sourceCode, sourceName, targetConceptId, targetConceptName,
      targetDomainId, targetVocabularyId, mappingStatus, ...
    """
    if not os.path.isfile(usagi_csv_path):
        raise FileNotFoundError(f"Usagi mapping file not found: {usagi_csv_path}")

    con.execute(f"""
        CREATE OR REPLACE TEMPORARY VIEW _usagi_raw AS
        SELECT * FROM read_csv_auto({sql_path_literal(usagi_csv_path)}, union_by_name = true, all_varchar = true);
    """)
    try:
        vocab_lit = sql_str(source_vocabulary_id)
        where = [
            "sourceCode IS NOT NULL",
            "TRY_CAST(targetConceptId AS INTEGER) IS NOT NULL",
            "TRY_CAST(targetConceptId AS INTEGER) <> 0",
        ]
        if approved_only:
            where.append("UPPER(TRIM(mappingStatus)) = 'APPROVED'")
        where_sql = "WHERE " + " AND ".join(where)

        sql = f"""
            INSERT INTO source_to_concept_map ({_STCM_INSERT_COLUMNS})
            SELECT
                sourceCode AS source_code,
                0 AS source_concept_id,
                {vocab_lit} AS source_vocabulary_id,
                sourceName AS source_code_description,
                TRY_CAST(targetConceptId AS INTEGER) AS target_concept_id,
                COALESCE(targetVocabularyId, '') AS target_vocabulary_id,
                CURRENT_DATE AS valid_start_date,
                DATE '2099-12-31' AS valid_end_date,
                NULL AS invalid_reason
            FROM _usagi_raw
            {where_sql}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY sourceCode
                ORDER BY TRY_CAST(targetConceptId AS INTEGER) ASC
            ) = 1;
        """
        count_sql = f"SELECT COUNT(*) FROM source_to_concept_map WHERE source_vocabulary_id = {vocab_lit}"
        before = con.execute(count_sql).fetchone()[0]
        con.execute(sql)
        return con.execute(count_sql).fetchone()[0] - before
    finally:
        con.execute("DROP VIEW IF EXISTS _usagi_raw;")


def export_unmapped_codes(con, table_name, output_csv, min_frequency=1):
    """Export unmapped (concept_id = 0) codes aggregated by frequency ready for OHDSI Usagi.

    Supported tables:
      condition_occurrence (condition_source_value)
      drug_exposure (drug_source_value)
      measurement (measurement_source_value)
      procedure_occurrence (procedure_source_value)
      provider (specialty_source_value)
    """
    column_map = {
        "condition_occurrence": ("condition_concept_id", "condition_source_value"),
        "drug_exposure": ("drug_concept_id", "drug_source_value"),
        "measurement": ("measurement_concept_id", "measurement_source_value"),
        "procedure_occurrence": ("procedure_concept_id", "procedure_source_value"),
        "provider": ("specialty_concept_id", "specialty_source_value"),
    }

    tbl = table_name.lower()
    if tbl not in column_map:
        raise ValueError(f"Unsupported table: {table_name}. Choose from: {list(column_map.keys())}")

    concept_col, source_col = column_map[tbl]
    min_frequency = sql_int(min_frequency, "min_frequency")

    sql = f"""
        SELECT
            {source_col} AS sourceCode,
            {source_col} AS sourceName,
            COUNT(*) AS frequency
        FROM {tbl}
        WHERE ({concept_col} = 0 OR {concept_col} IS NULL)
          AND {source_col} IS NOT NULL
        GROUP BY {source_col}
        HAVING COUNT(*) >= {min_frequency}
        ORDER BY frequency DESC, sourceCode;
    """
    rows = con.execute(sql).fetchall()

    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sourceCode", "sourceName", "frequency"])
        for r in rows:
            writer.writerow(r)

    print(f"Exported {len(rows)} unmapped codes from {tbl} to {output_csv}")
    return len(rows)
