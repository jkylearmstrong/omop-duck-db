"""Vocabulary loading, inspection, and CPT4 sanitization for OMOP CDM in DuckDB."""

import os
import duckdb

EXPECTED_VOCAB_TABLES = [
    "concept",
    "concept_relationship",
    "concept_ancestor",
    "concept_synonym",
    "vocabulary",
    "relationship",
    "concept_class",
    "domain",
]


def check_vocabulary_version(con):
    """Query the vocabulary version and summary metrics from the database."""
    res = con.execute("""
        SELECT vocabulary_version 
        FROM vocabulary 
        WHERE vocabulary_id = 'None' 
        LIMIT 1
    """).fetchone()
    version = res[0] if res else "Unknown"

    counts = con.execute("""
        SELECT vocabulary_id, COUNT(*) AS count 
        FROM concept 
        GROUP BY vocabulary_id 
        ORDER BY count DESC
    """).fetchall()

    total_concepts = sum(c[1] for c in counts)
    try:
        total_relationships = con.execute("SELECT COUNT(*) FROM concept_relationship").fetchone()[0]
    except Exception:
        total_relationships = 0

    unmapped_discharge = []
    try:
        tables = [r[0].lower() for r in con.execute("SHOW TABLES").fetchall()]
        if "visit_occurrence" in tables:
            unmapped_discharge = [
                {"source_value": r[0], "count": r[1]}
                for r in con.execute("""
                    SELECT discharged_to_source_value, COUNT(*) AS count
                    FROM visit_occurrence
                    WHERE discharged_to_concept_id = 0
                      AND discharged_to_source_value IS NOT NULL
                      AND TRIM(discharged_to_source_value) != ''
                    GROUP BY discharged_to_source_value
                    ORDER BY count DESC
                    LIMIT 10
                """).fetchall()
            ]
    except Exception:
        pass

    return {
        "vocabulary_version": version,
        "total_concepts": total_concepts,
        "total_relationships": total_relationships,
        "vocabularies": {c[0]: c[1] for c in counts},
        "unmapped_discharge_statuses": unmapped_discharge,
    }


def load_vocabulary(vocab_dir, db_path="omop_cdm.duckdb", sanitize_cpt4=True, sanitize_all_null_names=False):
    """Load or refresh OHDSI Athena vocabulary into the OMOP CDM database.

    Supports CPT4 sanitization: Athena downloads without a UMLS license
    contain empty/NULL concept_names in CONCEPT_CPT4.csv (or CPT4 concept entries),
    which violates the NOT NULL constraint on concept.concept_name. This loader
    automatically sanitizes NULL CPT4 concept names to 'CPT4 ' || concept_code.

    Each table is loaded inside its own transaction with row-count verification.
    """
    if not os.path.isdir(vocab_dir):
        raise FileNotFoundError(f"Vocabulary directory not found: {vocab_dir}")

    # Discover all CSV files recursively
    vocab_files = {}
    for root, _, files in os.walk(vocab_dir):
        for f in files:
            if f.lower().endswith(".csv"):
                base_name = os.path.splitext(f)[0].lower()
                vocab_files[base_name] = os.path.join(root, f)

    if not vocab_files:
        raise FileNotFoundError(f"No CSV files found in: {vocab_dir}")

    con = duckdb.connect(db_path)
    existing_tables = {row[0].lower() for row in con.execute("SHOW TABLES").fetchall()}

    all_ok = True

    # 1. Load standard vocabulary tables
    for table_name in EXPECTED_VOCAB_TABLES:
        if table_name not in existing_tables:
            print(f"Skipping {table_name} - no matching table in schema")
            continue

        file_path = vocab_files.get(table_name)
        if not file_path:
            continue

        print(f"Reloading {table_name.upper()} from {os.path.basename(file_path)} ...")
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"DELETE FROM {table_name}")

            if table_name == "concept":
                name_sanitizer = "COALESCE(concept_name, 'CPT4 ' || concept_code)" if sanitize_all_null_names else (
                    "COALESCE(concept_name, CASE WHEN vocabulary_id = 'CPT4' THEN 'CPT4 ' || concept_code ELSE NULL END)"
                    if sanitize_cpt4 else "concept_name"
                )
                con.execute(f"""
                    CREATE OR REPLACE TEMPORARY VIEW _temp_concept_raw AS 
                    SELECT * FROM read_csv(
                        '{file_path}', 
                        delim = '\t', 
                        header = true, 
                        dateformat = '%Y%m%d', 
                        all_varchar = true
                    );
                """)
                con.execute(f"""
                    INSERT INTO concept
                    SELECT
                        TRY_CAST(concept_id AS INTEGER),
                        {name_sanitizer} AS concept_name,
                        domain_id,
                        vocabulary_id,
                        concept_class_id,
                        standard_concept,
                        concept_code,
                        COALESCE(TRY_STRPTIME(valid_start_date, '%Y%m%d')::DATE, TRY_CAST(valid_start_date AS DATE)),
                        COALESCE(TRY_STRPTIME(valid_end_date, '%Y%m%d')::DATE, TRY_CAST(valid_end_date AS DATE)),
                        invalid_reason
                    FROM _temp_concept_raw;
                """)
                con.execute("DROP VIEW IF EXISTS _temp_concept_raw;")
            else:
                con.execute(f"""
                    COPY {table_name} FROM '{file_path}' (
                        DELIMITER '\t', 
                        HEADER, 
                        DATEFORMAT '%Y%m%d'
                    );
                """)

            row_count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            if row_count == 0:
                raise ValueError(f"Table {table_name} loaded with zero rows")

            con.execute("COMMIT")
            print(f"  -> {row_count} rows (committed)")
        except Exception as e:
            con.execute("ROLLBACK")
            print(f"  Error loading {table_name}: {e} - rolled back, table unchanged")
            all_ok = False

    # 2. If separate CONCEPT_CPT4.csv exists, ingest/sanitize it into concept
    if "concept_cpt4" in vocab_files and "concept" in existing_tables:
        cpt4_path = vocab_files["concept_cpt4"]
        print(f"Ingesting separate CPT4 concepts from {os.path.basename(cpt4_path)} ...")
        try:
            con.execute(f"""
                CREATE OR REPLACE TEMPORARY VIEW _temp_cpt4_raw AS 
                SELECT * FROM read_csv(
                    '{cpt4_path}', 
                    delim = '\t', 
                    header = true, 
                    dateformat = '%Y%m%d', 
                    all_varchar = true
                );
            """)
            con.execute("""
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
            """)
            con.execute("DROP VIEW IF EXISTS _temp_cpt4_raw;")
            print("  -> CPT4 concepts sanitized and merged")
        except Exception as e:
            print(f"  Warning: failed to load CONCEPT_CPT4.csv: {e}")

    con.close()
    return all_ok


def omop_connect(
    db_path,
    vocab_db_path=None,
    read_only=False,
    auto_attach_vocab=True,
    load_sql_macros=True,
):
    """Connect to an OMOP CDM DuckDB database with transparent central vocabulary attachment.

    RFC 1.1: Attaches the central Athena vocabulary read-only and automatically sets
    DuckDB's search_path ('main,central_vocab.main'), allowing queries to reference
    concept, concept_ancestor, and concept_relationship directly without schema prefixing.

    Parameters
    ----------
    db_path : str, Path, or duckdb.DuckDBPyConnection
        Path to the primary OMOP DuckDB database file or an existing connection.
    vocab_db_path : str or Path, optional
        Path to central_vocabulary.duckdb. If None and auto_attach_vocab is True,
        searches for central_vocabulary.duckdb / vocabulary.duckdb in the same directory
        as db_path, or in derived/omop_duckdb/.
    read_only : bool, default False
        Whether to open the database connection in read-only mode.
    auto_attach_vocab : bool, default True
        Whether to auto-discover and attach central vocabulary if vocab_db_path is not given.
    load_sql_macros : bool, default True
        Whether to load standard mapping and cohort SQL macros.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Active DuckDB connection with central_vocab attached and search_path configured.
    """
    if isinstance(db_path, duckdb.DuckDBPyConnection):
        con = db_path
        db_file = None
    else:
        db_str = str(db_path)
        con = duckdb.connect(db_str, read_only=read_only)
        db_file = os.path.abspath(db_str) if db_str != ":memory:" else None

    # Vocabulary discovery & attachment
    vocab_target = None
    if vocab_db_path is not None:
        vocab_target = os.path.abspath(str(vocab_db_path))
    elif auto_attach_vocab and db_file:
        parent_dir = os.path.dirname(db_file)
        candidates = [
            os.path.join(parent_dir, "central_vocabulary.duckdb"),
            os.path.join(parent_dir, "vocabulary.duckdb"),
            os.path.join(parent_dir, "vocab.duckdb"),
            os.path.join(os.getcwd(), "derived", "omop_duckdb", "central_vocabulary.duckdb"),
            os.path.join(os.getcwd(), "central_vocabulary.duckdb"),
        ]
        for c in candidates:
            if os.path.isfile(c) and os.path.abspath(c) != db_file:
                vocab_target = os.path.abspath(c)
                break

    if vocab_target and os.path.isfile(vocab_target):
        try:
            attached = [r[0] for r in con.execute("SELECT database_name FROM duckdb_databases();").fetchall()]
        except Exception:
            attached = []
        if "central_vocab" not in attached:
            norm_path = vocab_target.replace("\\", "/")
            con.execute(f"ATTACH '{norm_path}' AS central_vocab (READ_ONLY);")
        try:
            con.execute("SET search_path = 'main,central_vocab.main';")
        except Exception:
            pass

    if load_sql_macros:
        try:
            from omop_etl.build_omop_cdm import load_macros
            load_macros(con)
        except Exception:
            pass

    return con
