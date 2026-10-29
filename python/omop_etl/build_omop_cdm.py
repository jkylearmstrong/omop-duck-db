"""PCORnet -> OMOP CDM v5.4 ETL (Python implementation).

Usage:
    python python/omop_etl/build_omop_cdm.py --source-dir path/to/pcornet_extract [--db-path omop_cdm.duckdb]

Mirrors R/etl_pcornet.R table-for-table. Both read the same PCORnet-format
source directory, share the same schema and concept-resolution logic, and generate
100% deterministic surrogate keys and OHDSI v5.4 tables.
"""

import argparse
import os
import re
import warnings
import duckdb
from omop_etl.paths import sanitize_sql_path, sql_path_literal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resource_path(*repo_relative_parts):
    packaged = os.path.join(PACKAGE_DIR, "resources", *repo_relative_parts)
    if os.path.exists(packaged):
        return packaged
    return os.path.join(REPO_ROOT, "inst", *repo_relative_parts)


# EHR-derived record, per the OMOP Type Concept vocabulary.
TYPE_CONCEPT_EHR_ENCOUNTER = 32827
TYPE_CONCEPT_EHR_CONDITION = 32020
TYPE_CONCEPT_EHR_PROCEDURE = 32817
TYPE_CONCEPT_EHR_MEASUREMENT = 32817
CONCEPT_TOBACCO_SMOKING_STATUS = 43054909  # LOINC 72166-2 "Tobacco smoking status"
TYPE_CONCEPT_EHR_DRUG = 32838
TYPE_CONCEPT_MED_ADMIN = 38000180  # Inpatient administration
TYPE_CONCEPT_DISPENSING = 38000175  # Prescription dispensed in pharmacy
TYPE_CONCEPT_EHR_DEATH = 32815
TYPE_CONCEPT_PRIMARY_DX = 44786627  # "Primary admission diagnosis"

DEFAULT_ALIASES = {
    "PATID": ["PATID", "SSID", "PAT_ID", "PERSON_ID"],
    "PROVIDERID": ["PROVIDERID", "PROVIDER_ID"],
    "ENCOUNTERID": ["ENCOUNTERID", "ENCOUNTER_ID"],
    "DIAGNOSISID": ["DIAGNOSISID", "DIAGNOSIS_ID"],
    "PROCEDURESID": ["PROCEDURESID", "PROCEDURES_ID", "PROCEDUREID"],
    "LAB_RESULT_CM_ID": ["LAB_RESULT_CM_ID", "LAB_RESULT_ID", "MEASUREMENTID"],
    "PRESCRIBINGID": ["PRESCRIBINGID", "PRESCRIBING_ID", "DRUGEXPOSUREID"],
    "RAW_RX_MED_NAME": ["RAW_RX_MED_NAME", "RX_MED_NAME", "MED_NAME"],
    "RAW_RX_NDC": ["RAW_RX_NDC", "RX_NDC", "NDC"],
    "RXNORM_CUI": ["RXNORM_CUI", "RXNORM", "CUI"],
    "MEDADMINID": ["MEDADMINID", "MEDADMIN_ID"],
    "MEDADMIN_CODE": ["MEDADMIN_CODE", "MED_CODE", "RXNORM_CUI", "RXNORM"],
    "MEDADMIN_TYPE": ["MEDADMIN_TYPE", "ADMIN_TYPE", "MED_TYPE", "MEDADMIN_CODE_TYPE"],
    "MEDADMIN_START_DATE": ["MEDADMIN_START_DATE", "ADMIN_DATE", "START_DATE"],
    "MEDADMIN_START_TIME": ["MEDADMIN_START_TIME", "ADMIN_TIME", "START_TIME"],
    "MEDADMIN_STOP_DATE": ["MEDADMIN_STOP_DATE", "STOP_DATE"],
    "RAW_MEDADMIN_MED_NAME": ["RAW_MEDADMIN_MED_NAME", "MEDADMIN_MED_NAME", "RAW_RX_MED_NAME", "MED_NAME"],
    "DISPENSINGID": ["DISPENSINGID", "DISPENSING_ID"],
    "DISPENSING_TYPE": ["DISPENSING_TYPE", "DISP_TYPE", "DISPENSING_SRC", "NDC_TYPE", "DISP_CODE_TYPE"],
    "DISPENSING_DATE": ["DISPENSING_DATE", "DISPENSE_DATE"],
    "DISPENSING_SUP": ["DISPENSING_SUP", "DAYS_SUPPLY", "SUPPLY"],
    "DISPENSING_AMT": ["DISPENSING_AMT", "QUANTITY", "AMOUNT"],
    "RAW_LAB_NAME": ["RAW_LAB_NAME", "LAB_NAME"],
    "RAW_LAB_CODE": ["RAW_LAB_CODE", "LAB_CODE"],
    "LAB_LOINC": ["LAB_LOINC", "LOINC"],
    "FACILITYID": ["FACILITYID", "FACILITY_ID"],
    "FACILITY_TYPE": ["FACILITY_TYPE", "FACILITY_LOCATION"],
    "FACILITY_LOCATION": ["FACILITY_LOCATION", "FACILITY_LOCATION_ZIP"],
    "ADDRESSID": ["ADDRESSID", "ADDRESS_ID"],
    "ADDRESS_CITY": ["ADDRESS_CITY", "CITY"],
    "ADDRESS_STATE": ["ADDRESS_STATE", "STATE"],
    "ADDRESS_ZIP5": ["ADDRESS_ZIP5", "ZIP5", "ZIP"],
    "ADDRESS_ZIP9": ["ADDRESS_ZIP9", "ZIP9"],
    "ADDRESS_PREFERRED": ["ADDRESS_PREFERRED", "PREFERRED"],
    "ADDRESS_USE": ["ADDRESS_USE", "USE"],
    "ADDRESS_TYPE": ["ADDRESS_TYPE", "TYPE"],
    "ADDRESS_PERIOD_START": ["ADDRESS_PERIOD_START", "PERIOD_START"],
    "ADDRESS_PERIOD_END": ["ADDRESS_PERIOD_END", "PERIOD_END"],
}


def find_source_file(source_dir, table_name, source_format=None):
    """Locates ``<table_name>.csv`` or ``<table_name>.parquet`` (case-insensitive) in ``source_dir``.

    ``source_format`` ('csv' | 'parquet' | None) restricts the search; None prefers CSV, then Parquet."""
    formats = ("csv", "parquet") if source_format is None else (str(source_format).lower(),)
    for fmt in formats:
        pattern = re.compile(rf"^{re.escape(table_name)}\.{fmt}$", re.IGNORECASE)
        for name in sorted(os.listdir(source_dir)):
            if pattern.match(name):
                return os.path.join(source_dir, name)
    return None


def _source_relation(path):
    """SQL relation reading a source file with every column as VARCHAR (the ETL casts explicitly)."""
    if str(path).lower().endswith(".parquet"):
        return f"(SELECT COLUMNS(*)::VARCHAR FROM read_parquet({sql_path_literal(path)}))"
    return f"read_csv_auto({sql_path_literal(path)}, union_by_name = true, all_varchar = true)"


def prepare_source_view(con, view_name, csv_path, expected_columns, aliases=None):
    """Creates a temporary view over a source CSV with case-insensitive column discovery,
    alias resolution (e.g. ssid -> PATID), and NULL projections for missing columns."""
    if aliases is None:
        aliases = DEFAULT_ALIASES

    cols_info = con.execute(
        f"DESCRIBE SELECT * FROM {_source_relation(csv_path)} LIMIT 0"
    ).fetchall()
    actual_cols = {col[0].upper(): col[0] for col in cols_info}

    exprs = []
    aliased, padded = [], []
    for tgt in expected_columns:
        tgt_upper = tgt.upper()
        candidates = aliases.get(tgt_upper, [tgt_upper])
        if tgt_upper not in candidates:
            candidates = [tgt_upper] + candidates

        found = None
        for cand in candidates:
            if cand.upper() in actual_cols:
                found = actual_cols[cand.upper()]
                break

        if found is not None:
            exprs.append(f'"{found}" AS {tgt}')
            if found.upper() != tgt_upper:
                aliased.append(f"{found} -> {tgt}")
        else:
            exprs.append(f"NULL AS {tgt}")
            padded.append(tgt)

    label = os.path.basename(str(csv_path))
    if aliased:
        print(f"  [{label}] aliased columns: {', '.join(aliased)}")
    if padded:
        print(f"  [{label}] null-padded missing columns: {', '.join(padded)}")

    sql = f"CREATE OR REPLACE TEMPORARY VIEW {view_name} AS SELECT {', '.join(exprs)} FROM {_source_relation(csv_path)};"
    con.execute(sql)


def build_safe_source_view(con, table_name, csv_path, expected_columns, aliases=None):
    """Create view ``src_<table_name>`` over a source CSV, aliasing header variants (e.g. ``ssid`` -> PATID)
    and padding absent expected columns with NULL. Aliased / padded columns are printed."""
    view_name = f"src_{table_name}"
    prepare_source_view(con, view_name, csv_path, expected_columns, aliases)
    return view_name


def run_insert(con, label, sql):
    print(f"Loading {label} ...")
    n = con.execute(sql).rowcount
    print(f"  -> {n} rows inserted")
    return n


def build_schema(con):
    ddl_path = _resource_path("extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql")
    with open(ddl_path) as f:
        ddl = f.read()
    ddl = ddl.replace("@cdmDatabaseSchema.", "")
    ddl = ddl.replace(" NUMERIC ", " DOUBLE ")
    con.execute(ddl)
    print("Schema built from", ddl_path)


MACRO_FILES = ["mapping_macros.sql", "cohort_readmission.sql", "table1_aggregations.sql", "cohort_mortality.sql"]
_CREATE_MACRO_RE = re.compile(r"\bCREATE\s+OR\s+REPLACE\s+MACRO\b", re.IGNORECASE)


def _connection_is_read_only(con):
    """True when the connection's current database is attached read-only."""
    row = con.execute(
        "SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchone()
    return bool(row[0]) if row else False


def _split_sql_statements(sql):
    """Split a macro file into statements. Sufficient for ``inst/sql``: no string literal in those
    files contains ``--`` or ``;`` (tests/test_omop_connect.py checks the split against the file)."""
    return [stmt.strip() for stmt in re.sub(r"--[^\r\n]*", "", sql).split(";") if stmt.strip()]


def load_macros(con, temporary=None, skip_unresolved=False):
    """Load the shared SQL macro files (``inst/sql``) into a connection.

    DuckDB validates the body of some macros when it *creates* them, so a macro such as
    ``map_to_standard_concept_id()`` cannot be created while a vocabulary table (or a column it
    reads) is not visible on the connection. Others, like ``descendants_of()``, are created
    regardless and look their table up when they are used.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
    temporary : bool, optional
        ``True`` creates every macro as a ``TEMP`` macro (session-scoped, nothing is written to the
        database file, works on read-only databases); ``False`` creates persistent macros stored in
        the database. The default ``None`` picks ``True`` only when the connection's current database
        is read-only, so ETL runs on a writable database persist macros exactly as before.
    skip_unresolved : bool, default False
        If False, a macro that cannot be created because a table or column it reads is missing
        raises, as always. If True, such macros are skipped (all others are still created) and
        reported in the return value. Only DuckDB's catalog and binder errors are skipped; any other
        error still raises.

    Returns
    -------
    list of str
        Names of the macros skipped because a table or column they read is not available (always
        empty unless ``skip_unresolved``); the caller decides how to report them.
    """
    if temporary is None:
        temporary = _connection_is_read_only(con)
    skipped = []
    for macro_file in MACRO_FILES:
        macros_path = _resource_path("sql", macro_file)
        if not os.path.exists(macros_path):
            warnings.warn(
                f"SQL macro file '{macro_file}' was not found; the macros it defines are not available.",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        with open(macros_path, encoding="utf-8") as f:
            sql = f.read()
        if temporary:
            sql = _CREATE_MACRO_RE.sub("CREATE OR REPLACE TEMP MACRO", sql)
        if not skip_unresolved:
            con.execute(sql)
            continue
        for stmt in _split_sql_statements(sql):
            try:
                con.execute(stmt)
            except (duckdb.CatalogException, duckdb.BinderException):
                skipped.append(re.search(r"\bMACRO\s+(\w+)", stmt).group(1))
    return skipped


def attach_central_vocabulary(con, vocab_db_path, temporary=True):
    """Attach an external DuckDB database containing Athena vocabulary tables and
    create zero-copy views, avoiding copying 10-15 GB of vocabulary data into the local database."""
    if not os.path.exists(vocab_db_path):
        raise FileNotFoundError(f"Central vocabulary database not found: {vocab_db_path}")

    con.execute(f"ATTACH {sql_path_literal(vocab_db_path)} AS central_vocab (READ_ONLY);")
    vocab_tables = [
        "concept", "concept_relationship", "concept_ancestor", "concept_synonym",
        "vocabulary", "relationship", "concept_class", "domain", "drug_strength"
    ]
    for tbl in vocab_tables:
        has_tbl = con.execute(
            f"SELECT 1 FROM information_schema.tables WHERE table_catalog = 'central_vocab' AND table_name = '{tbl}'"
        ).fetchone()
        if has_tbl:
            if temporary:
                con.execute(f"CREATE OR REPLACE TEMPORARY VIEW {tbl} AS SELECT * FROM central_vocab.{tbl};")
            else:
                con.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
                con.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM central_vocab.{tbl};")
    print(f"Attached central vocabulary from {vocab_db_path} with zero-copy views.")


VOCABULARY_TABLES = ("concept", "vocabulary", "domain", "concept_class", "relationship", "concept_relationship",
                     "concept_ancestor", "concept_synonym", "drug_strength")


def materialize_vocabulary(con, tables=VOCABULARY_TABLES, catalog="central_vocab"):
    """Copies vocabulary tables that are views over an attached ``central_vocab`` into real tables (FR-C9).

    HADES (DatabaseConnector) opens the file alone and cannot ATTACH, so views into ``central_vocab`` fail with
    "schema central_vocab does not exist". Returns ``{table: row_count}`` for the tables copied.
    """
    done = {}
    for tbl in tables:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tbl):
            raise ValueError(f"Invalid table name {tbl!r}.")
        kind = con.execute(
            "SELECT table_type FROM information_schema.tables WHERE table_name = ? "
            "AND table_catalog IN (current_database(), 'temp') AND table_type = 'VIEW'", [tbl]).fetchone()
        if kind is None:
            continue
        con.execute(f"CREATE OR REPLACE TABLE {tbl}__mat AS SELECT * FROM {tbl};")
        con.execute(f"DROP VIEW IF EXISTS {tbl};")
        con.execute(f"ALTER TABLE {tbl}__mat RENAME TO {tbl};")
        done[tbl] = int(con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
    return done


def attach_read_only(con, cdm_db, vocab_db=None, cdm_alias="cdm", vocab_alias="central_vocab"):
    """ATTACHes a CDM file, and optionally a vocabulary file, READ_ONLY on ``con`` (FR-D4).

    Site files often expose ``concept`` and the other vocabulary tables as views into a catalog named
    ``central_vocab``; those views only resolve when the vocabulary file is attached under exactly that name
    (``vocab_alias``). Run this on every connection that reads the CDM. Nothing is written to either file.
    Returns ``{"cdm": cdm_alias, "vocab": vocab_alias or None}``.
    """
    for alias in (cdm_alias, vocab_alias):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", alias):
            raise ValueError(f"Invalid catalog alias {alias!r}.")
    for label, path in (("CDM", cdm_db), ("vocabulary", vocab_db)):
        if path is not None and not os.path.exists(path):
            raise FileNotFoundError(f"{label} database not found: {path}")
        if path is not None and os.path.exists(f"{path}.wal"):
            warnings.warn(f"{path}.wal exists: ATTACH ... (READ_ONLY) will fail with 'WAL replay is required'. "
                          "Run checkpoint_database() on the file first.", stacklevel=2)
    con.execute(f"ATTACH {sql_path_literal(cdm_db)} AS {cdm_alias} (READ_ONLY);")
    if vocab_db is not None:
        con.execute(f"ATTACH {sql_path_literal(vocab_db)} AS {vocab_alias} (READ_ONLY);")
    return {"cdm": cdm_alias, "vocab": vocab_alias if vocab_db is not None else None}


def checkpoint_database(db_path):
    """Folds a DuckDB file's write-ahead log into the file so it can be distributed and attached ``READ_ONLY``.

    DuckDB refuses ``ATTACH ... (READ_ONLY)`` while ``<file>.wal`` exists ("WAL replay is required"). Run this on every
    consortium database before copying it. Needs exclusive access: no other process may have the file open. Raises
    ``RuntimeError`` if a ``.wal`` file is still present afterwards. Returns the path.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CHECKPOINT;")
    finally:
        con.close()
    if os.path.exists(f"{db_path}.wal"):
        raise RuntimeError(f"{db_path}.wal still exists after CHECKPOINT; close every other connection and retry.")
    return db_path


def load_care_site(con, source_dir, site_id=None, site_anon=None, site_name=None):
    """Loads care_site records from facility.csv if present and/or seeds root institutional care_site."""
    path = find_source_file(source_dir, "facility")
    loaded_facility = False

    if path:
        prepare_source_view(
            con,
            "_temp_facility",
            path,
            ["FACILITYID", "FACILITY_TYPE", "FACILITY_LOCATION"],
        )
        run_insert(con, "CARE_SITE (from FACILITY)", """
            INSERT INTO care_site (
                care_site_id,
                care_site_name,
                place_of_service_concept_id,
                location_id,
                care_site_source_value,
                place_of_service_source_value
            )
            SELECT
                pcornet_id(src.FACILITYID) AS care_site_id,
                COALESCE(src.FACILITY_TYPE, 'Facility ' || src.FACILITYID) AS care_site_name,
                0 AS place_of_service_concept_id,
                NULL AS location_id,
                src.FACILITYID AS care_site_source_value,
                src.FACILITY_TYPE AS place_of_service_source_value
            FROM _temp_facility src
            WHERE src.FACILITYID IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY src.FACILITYID) = 1;
        """)
        con.execute("DROP VIEW IF EXISTS _temp_facility;")
        loaded_facility = True

    if site_id is not None:
        root_name = site_anon if site_anon is not None else f"Site {site_id}"
        root_source = site_name if site_name is not None else root_name
        root_name_esc = str(root_name).replace("'", "''")
        root_source_esc = str(root_source).replace("'", "''")

        run_insert(con, "CARE_SITE (root institutional record)", f"""
            INSERT INTO care_site (
                care_site_id,
                care_site_name,
                place_of_service_concept_id,
                location_id,
                care_site_source_value,
                place_of_service_source_value
            )
            SELECT
                {int(site_id)} AS care_site_id,
                '{root_name_esc}' AS care_site_name,
                0 AS place_of_service_concept_id,
                NULL AS location_id,
                '{root_source_esc}' AS care_site_source_value,
                NULL AS place_of_service_source_value
            WHERE NOT EXISTS (
                SELECT 1 FROM care_site WHERE care_site_id = {int(site_id)}
            );
        """)
    elif not loaded_facility:
        print("Skipping CARE_SITE - no facility.csv found and no site_id provided")


def load_provider(con, source_dir):
    path = find_source_file(source_dir, "provider")
    if path is None:
        print("Skipping PROVIDER - no provider.csv found")
        return

    prepare_source_view(
        con,
        "_temp_provider",
        path,
        ["PROVIDERID", "PROVIDER_NPI", "PROVIDER_SPECIALTY_PRIMARY", "PROVIDER_SEX", "RAW_PROVIDER_SPECIALTY_PRIMARY"],
    )

    run_insert(con, "PROVIDER", """
        INSERT INTO provider
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.PROVIDERID) + (SELECT COALESCE(MAX(provider_id), 0) FROM provider) AS provider_id,
            NULL AS provider_name,
            src.PROVIDER_NPI AS npi,
            NULL AS dea,
            COALESCE(
                stcm.target_concept_id,
                cr.concept_id_2,
                CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                0
            ) AS specialty_concept_id,
            NULL AS care_site_id,
            NULL AS year_of_birth,
            CASE UPPER(src.PROVIDER_SEX) WHEN 'M' THEN 8507 WHEN 'F' THEN 8532 ELSE 0 END AS gender_concept_id,
            src.PROVIDERID AS provider_source_value,
            src.RAW_PROVIDER_SPECIALTY_PRIMARY AS specialty_source_value,
            COALESCE(c.concept_id, 0) AS specialty_source_concept_id,
            src.PROVIDER_SEX AS gender_source_value,
            0 AS gender_source_concept_id
        FROM _temp_provider src
        LEFT JOIN concept c
          ON c.vocabulary_id = 'NUCC'
         AND c.concept_code = src.PROVIDER_SPECIALTY_PRIMARY
        LEFT JOIN (
            SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
            FROM concept_relationship
            WHERE relationship_id = 'Maps to'
            GROUP BY concept_id_1
        ) cr
          ON cr.concept_id_1 = c.concept_id
        LEFT JOIN (
            SELECT source_code, MIN(target_concept_id) AS target_concept_id
            FROM source_to_concept_map
            WHERE source_vocabulary_id = 'NUCC'
              AND (invalid_reason IS NULL OR invalid_reason = '')
              AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
            GROUP BY source_code
        ) stcm
          ON stcm.source_code = src.PROVIDER_SPECIALTY_PRIMARY
        WHERE src.PROVIDERID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PROVIDERID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_provider;")


def load_person(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "demographic")
    if path is None:
        print("Skipping PERSON - no demographic.csv found")
        return

    prepare_source_view(
        con,
        "_temp_demographic",
        path,
        ["PATID", "SEX", "BIRTH_DATE", "RACE", "HISPANIC", "PROVIDERID"],
    )

    if disambiguate_patids:
        if site_id is None:
            raise ValueError("disambiguate_patids requires site_id to be specified.")
        person_id_expr = f"pcornet_id(src.PATID || '-{site_id}')"
        person_src_expr = f"src.PATID || '-{site_id}'"
    else:
        person_id_expr = "ROW_NUMBER() OVER (ORDER BY src.PATID) + (SELECT COALESCE(MAX(person_id), 0) FROM person)"
        person_src_expr = "src.PATID"

    care_site_id_expr = str(int(site_id)) if site_id is not None else "NULL"

    run_insert(con, "PERSON (from DEMOGRAPHIC)", f"""
        INSERT INTO person
        SELECT
            {person_id_expr} AS person_id,
            CASE UPPER(src.SEX)
                WHEN 'M' THEN 8507  -- Male
                WHEN 'F' THEN 8532  -- Female
                WHEN 'OT' THEN 8521 -- Other
                WHEN 'UN' THEN 8551 -- Unknown
                WHEN 'NI' THEN 8551 -- No information
                ELSE 0
            END AS gender_concept_id,
            YEAR(parse_omop_date(src.BIRTH_DATE)) AS year_of_birth,
            MONTH(parse_omop_date(src.BIRTH_DATE)) AS month_of_birth,
            DAY(parse_omop_date(src.BIRTH_DATE)) AS day_of_birth,
            parse_omop_datetime(src.BIRTH_DATE, NULL) AS birth_datetime,
            CASE src.RACE
                WHEN '05' THEN 8527 -- White
                WHEN '03' THEN 8516 -- Black or African American
                WHEN '02' THEN 8515 -- Asian
                ELSE 0
            END AS race_concept_id,
            CASE UPPER(src.HISPANIC) WHEN 'Y' THEN 38003563 ELSE 38003564 END AS ethnicity_concept_id,
            NULL AS location_id,
            COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
            {care_site_id_expr} AS care_site_id,
            {person_src_expr} AS person_source_value,
            src.SEX AS gender_source_value,
            0 AS gender_source_concept_id,
            src.RACE AS race_source_value,
            0 AS race_source_concept_id,
            src.HISPANIC AS ethnicity_source_value,
            0 AS ethnicity_source_concept_id
        FROM _temp_demographic src
        LEFT JOIN provider pr
          ON pr.provider_source_value = src.PROVIDERID
        WHERE src.PATID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PATID ORDER BY parse_omop_date(src.BIRTH_DATE) NULLS LAST) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_demographic;")


def _repair_address_csv(path):
    """Return ``path``, or a repaired temp copy when rows have more fields than the header.

    PCORnet address extracts often leave a comma in ``ADDRESS_CITY`` unquoted ("St. Louis, MO"), which shifts every later
    column (state, ZIP, dates) with no error from the CSV reader. Surplus fields are folded back into the city column.
    """
    import csv
    import tempfile

    if str(path).lower().endswith((".parquet", ".pq")):
        return path                                    # columnar sources have no delimiters to misread
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except csv.Error:
        return path                                    # not a delimited text file; let the normal reader decide
    if not rows:
        return path
    header = rows[0]
    city = next((i for i, h in enumerate(header) if h.strip().upper() == "ADDRESS_CITY"), None)
    bad = [r for r in rows[1:] if len(r) > len(header)]
    if city is None or not bad:
        return path
    for r in bad:
        extra = len(r) - len(header)
        r[city:city + extra + 1] = [",".join(r[city:city + extra + 1])]
    out = tempfile.NamedTemporaryFile("w", suffix="_address_history.csv", delete=False, newline="", encoding="utf-8")
    with out:
        csv.writer(out).writerows(rows)
    print(f"  [{os.path.basename(str(path))}] repaired {len(bad)} row(s) with unquoted commas in ADDRESS_CITY")
    return out.name


def load_location(con, source_dir, site_id=None, disambiguate_patids=False):
    """Maps PCORnet lds_address_history.csv into OMOP location and updates person.location_id."""
    path = find_source_file(source_dir, "lds_address_history")
    if path is None:
        path = find_source_file(source_dir, "address_history")
    if path is None:
        print("Skipping LOCATION - no lds_address_history.csv found (optional table)")
        return
    path = _repair_address_csv(path)

    prepare_source_view(
        con,
        "_temp_address_history",
        path,
        [
            "ADDRESSID", "PATID", "ADDRESS_USE", "ADDRESS_TYPE", "ADDRESS_PREFERRED",
            "ADDRESS_CITY", "ADDRESS_STATE", "ADDRESS_ZIP5", "ADDRESS_ZIP9",
            "ADDRESS_PERIOD_START", "ADDRESS_PERIOD_END"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "LOCATION (from LDS_ADDRESS_HISTORY)", """
        INSERT INTO location (
            location_id,
            address_1,
            address_2,
            city,
            state,
            zip,
            county,
            location_source_value,
            country_concept_id,
            country_source_value,
            latitude,
            longitude
        )
        SELECT
            pcornet_id(COALESCE(UPPER(ADDRESS_STATE), '') || '_' || COALESCE(ADDRESS_ZIP5, '') || '_' || COALESCE(UPPER(ADDRESS_CITY), '')) AS location_id,
            NULL AS address_1,
            NULL AS address_2,
            ADDRESS_CITY AS city,
            UPPER(ADDRESS_STATE) AS state,
            COALESCE(ADDRESS_ZIP9, ADDRESS_ZIP5) AS zip,
            NULL AS county,
            COALESCE(ADDRESS_ZIP5, '') || ', ' || COALESCE(ADDRESS_STATE, '') AS location_source_value,
            4330426 AS country_concept_id,
            'US' AS country_source_value,
            NULL AS latitude,
            NULL AS longitude
        FROM _temp_address_history
        WHERE (ADDRESS_STATE IS NOT NULL OR ADDRESS_ZIP5 IS NOT NULL OR ADDRESS_CITY IS NOT NULL)
          AND pcornet_id(COALESCE(UPPER(ADDRESS_STATE), '') || '_' || COALESCE(ADDRESS_ZIP5, '') || '_' || COALESCE(UPPER(ADDRESS_CITY), '')) NOT IN (
              SELECT location_id FROM location
          )
        GROUP BY ADDRESS_CITY, ADDRESS_STATE, ADDRESS_ZIP5, ADDRESS_ZIP9;
    """)

    con.execute(f"""
        UPDATE person
        SET location_id = loc.location_id
        FROM (
            SELECT 
                {patid_expr} AS patid_key,
                pcornet_id(COALESCE(UPPER(src.ADDRESS_STATE), '') || '_' || COALESCE(src.ADDRESS_ZIP5, '') || '_' || COALESCE(UPPER(src.ADDRESS_CITY), '')) AS location_id
            FROM _temp_address_history src
            WHERE src.PATID IS NOT NULL
              AND (src.ADDRESS_STATE IS NOT NULL OR src.ADDRESS_ZIP5 IS NOT NULL OR src.ADDRESS_CITY IS NOT NULL)
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY src.PATID 
                ORDER BY 
                    CASE UPPER(COALESCE(src.ADDRESS_PREFERRED, 'N')) WHEN 'Y' THEN 1 ELSE 2 END,
                    src.ADDRESS_PERIOD_END DESC NULLS LAST,
                    src.ADDRESS_PERIOD_START DESC NULLS LAST,
                    src.ADDRESSID DESC
            ) = 1
        ) loc
        WHERE person.person_source_value = loc.patid_key;
    """)

    # person.location_id keeps one address per person; the dated history is what lets a model pick the address in effect
    # at an index admission (omop_etl.geography). Additive: not part of the CDM DDL, rebuilt per person on every call.
    con.execute("""
        CREATE TABLE IF NOT EXISTS person_address_history (
            person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR
        );
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMPORARY TABLE _new_address_history AS
        SELECT p.person_id,
               pcornet_id(COALESCE(UPPER(src.ADDRESS_STATE), '') || '_' || COALESCE(src.ADDRESS_ZIP5, '') || '_' || COALESCE(UPPER(src.ADDRESS_CITY), '')) AS location_id,
               regexp_extract(CAST(src.ADDRESS_ZIP5 AS VARCHAR), '^\\s*(\\d{{5}})', 1) AS zip5,
               parse_omop_date(src.ADDRESS_PERIOD_START) AS address_start,
               parse_omop_date(src.ADDRESS_PERIOD_END) AS address_end,
               UPPER(src.ADDRESS_PREFERRED) AS preferred
        FROM _temp_address_history src
        JOIN person p ON p.person_source_value = {patid_expr}
        WHERE src.PATID IS NOT NULL
          AND (src.ADDRESS_STATE IS NOT NULL OR src.ADDRESS_ZIP5 IS NOT NULL OR src.ADDRESS_CITY IS NOT NULL);
    """)
    con.execute("DELETE FROM person_address_history WHERE person_id IN (SELECT person_id FROM _new_address_history);")
    con.execute("""
        INSERT INTO person_address_history
        SELECT person_id, location_id, nullif(zip5, ''), address_start, address_end, preferred FROM _new_address_history;
    """)
    con.execute("DROP TABLE IF EXISTS _new_address_history;")
    con.execute("DROP VIEW IF EXISTS _temp_address_history;")


def load_visit_occurrence(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "encounter")
    if path is None:
        print("Skipping VISIT_OCCURRENCE - no encounter.csv found")
        return

    prepare_source_view(
        con,
        "_temp_encounter",
        path,
        [
            "ENCOUNTERID", "PATID", "ENC_TYPE", "ADMIT_DATE", "ADMIT_TIME",
            "DISCHARGE_DATE", "DISCHARGE_TIME", "PROVIDERID", "FACILITYID", "DISCHARGE_STATUS"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"
    care_site_id_expr = str(int(site_id)) if site_id is not None else "NULL"

    run_insert(con, "VISIT_OCCURRENCE (from ENCOUNTER)", f"""
        INSERT INTO visit_occurrence
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.ENCOUNTERID) + (SELECT COALESCE(MAX(visit_occurrence_id), 0) FROM visit_occurrence) AS visit_occurrence_id,
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            CASE UPPER(src.ENC_TYPE)
                WHEN 'IP' THEN 9201 -- Inpatient
                WHEN 'EI' THEN 262 -- Emergency Room and Inpatient Visit (PCORnet emergency-to-inpatient; an index stay with 9201)
                WHEN 'OS' THEN 581385 -- Observation Room (never an index inpatient stay; still counts as prior utilization)
                WHEN 'ED' THEN 9203 -- Emergency
                WHEN 'AV' THEN 9202 -- Outpatient
                WHEN 'OA' THEN 9202 -- Outpatient
                WHEN 'OT' THEN 9202 -- Other Ambulatory
                WHEN 'TH' THEN 5083 -- Telehealth
                ELSE 0
            END AS visit_concept_id,
            parse_omop_date(src.ADMIT_DATE) AS visit_start_date,
            parse_omop_datetime(src.ADMIT_DATE, src.ADMIT_TIME) AS visit_start_datetime,
            COALESCE(parse_omop_date(src.DISCHARGE_DATE), parse_omop_date(src.ADMIT_DATE)) AS visit_end_date,
            parse_omop_datetime(COALESCE(src.DISCHARGE_DATE, src.ADMIT_DATE), src.DISCHARGE_TIME) AS visit_end_datetime,
            {TYPE_CONCEPT_EHR_ENCOUNTER} AS visit_type_concept_id,
            COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
            COALESCE(cs.care_site_id, {care_site_id_expr}, pcornet_id(src.FACILITYID)) AS care_site_id,
            src.ENCOUNTERID AS visit_source_value,
            0 AS visit_source_concept_id,
            0 AS admitted_from_concept_id,
            NULL AS admitted_from_source_value,
            CASE 
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('E', 'EX', 'EXPIRED', 'DIED', 'DECEASED') THEN 4216643 -- Expired
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('AM', 'AMA') THEN 32216 -- Left against medical advice or discontinued care (UB04 Pt dis status 07)
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('HS', 'HOSPICE') THEN 8546 -- Hospice (PCORnet HS)
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('SN', 'SNF') THEN 8863 -- Skilled Nursing Facility
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('NH') THEN 8676 -- Nursing Facility
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('HH') THEN 32215 -- UB04 06: home care of organized home health service
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('RH') THEN 32228 -- UB04 62: inpatient rehabilitation facility
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('IP') THEN 8717 -- Other acute inpatient hospital (transfer)
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('A', 'HO', 'HOME', 'ROUTINE') THEN 8536 -- Home / self care (PCORnet HO)
                ELSE 0
            END AS discharged_to_concept_id,
            src.DISCHARGE_STATUS AS discharged_to_source_value,
            NULL AS preceding_visit_occurrence_id
        FROM _temp_encounter src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN provider pr
          ON pr.provider_source_value = src.PROVIDERID
        LEFT JOIN care_site cs
          ON cs.care_site_source_value = src.FACILITYID
        WHERE src.ENCOUNTERID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.ENCOUNTERID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_encounter;")

    try:
        unmapped_disch = con.execute("""
            SELECT discharged_to_source_value, COUNT(*) AS count
            FROM visit_occurrence
            WHERE discharged_to_concept_id = 0
              AND discharged_to_source_value IS NOT NULL
              AND TRIM(discharged_to_source_value) != ''
            GROUP BY discharged_to_source_value
            ORDER BY count DESC
            LIMIT 10;
        """).fetchall()
        if unmapped_disch:
            unmapped_str = ", ".join(f"'{r[0]}' (n={r[1]})" for r in unmapped_disch)
            print(f"Notice: Visit occurrence contains unmapped discharge statuses (concept_id = 0): {unmapped_str}")
    except Exception:
        pass


def load_condition_occurrence(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "diagnosis")
    if path is None:
        print("Skipping CONDITION_OCCURRENCE - no diagnosis.csv found")
        return

    prepare_source_view(
        con,
        "_temp_diagnosis",
        path,
        ["DIAGNOSISID", "PATID", "DX_TYPE", "DX", "DX_DATE", "ADMIT_DATE", "PDX", "PROVIDERID", "ENCOUNTERID"],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "CONDITION_OCCURRENCE (from DIAGNOSIS)", f"""
        INSERT INTO condition_occurrence
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.DIAGNOSISID) + (SELECT COALESCE(MAX(condition_occurrence_id), 0) FROM condition_occurrence) AS condition_occurrence_id,
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            COALESCE(
                stcm.target_concept_id,
                cr.concept_id_2,
                CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                0
            ) AS condition_concept_id,
            parse_omop_date(COALESCE(src.DX_DATE, src.ADMIT_DATE)) AS condition_start_date,
            parse_omop_datetime(COALESCE(src.DX_DATE, src.ADMIT_DATE), NULL) AS condition_start_datetime,
            NULL AS condition_end_date,
            NULL AS condition_end_datetime,
            CASE UPPER(src.PDX) WHEN 'P' THEN {TYPE_CONCEPT_PRIMARY_DX} ELSE 0 END AS condition_type_concept_id,
            0 AS condition_status_concept_id,
            NULL AS stop_reason,
            COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
            COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
            NULL AS visit_detail_id,
            src.DX AS condition_source_value,
            COALESCE(c.concept_id, 0) AS condition_source_concept_id,
            src.PDX AS condition_status_source_value
        FROM (
            SELECT *,
                CASE UPPER(DX_TYPE) WHEN '09' THEN 'ICD9CM' ELSE 'ICD10CM' END AS dx_vocabulary_id
            FROM _temp_diagnosis
        ) src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN provider pr
          ON pr.provider_source_value = src.PROVIDERID
        LEFT JOIN visit_occurrence vo
          ON vo.visit_source_value = src.ENCOUNTERID
        LEFT JOIN concept c
          ON c.vocabulary_id = src.dx_vocabulary_id
         AND c.concept_code = src.DX
        LEFT JOIN (
            SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
            FROM concept_relationship
            WHERE relationship_id = 'Maps to'
            GROUP BY concept_id_1
        ) cr
          ON cr.concept_id_1 = c.concept_id
        LEFT JOIN (
            SELECT source_vocabulary_id, source_code, MIN(target_concept_id) AS target_concept_id
            FROM source_to_concept_map
            WHERE (invalid_reason IS NULL OR invalid_reason = '')
              AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
            GROUP BY source_vocabulary_id, source_code
        ) stcm
          ON stcm.source_vocabulary_id = src.dx_vocabulary_id
         AND stcm.source_code = src.DX
        WHERE src.DIAGNOSISID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.DIAGNOSISID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_diagnosis;")


def load_procedure_occurrence(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "procedures")
    if path is None:
        print("Skipping PROCEDURE_OCCURRENCE - no procedures.csv found")
        return

    prepare_source_view(
        con,
        "_temp_procedures",
        path,
        ["PROCEDURESID", "PATID", "PX_TYPE", "PX", "PX_DATE", "PROVIDERID", "ENCOUNTERID"],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "PROCEDURE_OCCURRENCE (from PROCEDURES)", f"""
        INSERT INTO procedure_occurrence
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.PROCEDURESID) + (SELECT COALESCE(MAX(procedure_occurrence_id), 0) FROM procedure_occurrence) AS procedure_occurrence_id,
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            COALESCE(
                stcm.target_concept_id,
                cr.concept_id_2,
                CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                0
            ) AS procedure_concept_id,
            parse_omop_date(src.PX_DATE) AS procedure_date,
            parse_omop_datetime(src.PX_DATE, NULL) AS procedure_datetime,
            NULL AS procedure_end_date,
            NULL AS procedure_end_datetime,
            {TYPE_CONCEPT_EHR_PROCEDURE} AS procedure_type_concept_id,
            0 AS modifier_concept_id,
            NULL AS quantity,
            COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
            COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
            NULL AS visit_detail_id,
            src.PX AS procedure_source_value,
            COALESCE(c.concept_id, 0) AS procedure_source_concept_id,
            NULL AS modifier_source_value
        FROM (
            SELECT *,
                CASE PX_TYPE
                    WHEN '01' THEN 'CPT4'
                    WHEN '02' THEN 'HCPCS'
                    WHEN '09' THEN 'ICD9Proc'
                    WHEN '10' THEN 'ICD10PCS'
                    ELSE PX_TYPE
                END AS px_vocabulary_id
            FROM _temp_procedures
        ) src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN provider pr
          ON pr.provider_source_value = src.PROVIDERID
        LEFT JOIN visit_occurrence vo
          ON vo.visit_source_value = src.ENCOUNTERID
        LEFT JOIN concept c
          ON c.vocabulary_id = src.px_vocabulary_id
         AND c.concept_code = src.PX
        LEFT JOIN (
            SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
            FROM concept_relationship
            WHERE relationship_id = 'Maps to'
            GROUP BY concept_id_1
        ) cr
          ON cr.concept_id_1 = c.concept_id
        LEFT JOIN (
            SELECT source_vocabulary_id, source_code, MIN(target_concept_id) AS target_concept_id
            FROM source_to_concept_map
            WHERE (invalid_reason IS NULL OR invalid_reason = '')
              AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
            GROUP BY source_vocabulary_id, source_code
        ) stcm
          ON stcm.source_vocabulary_id = src.px_vocabulary_id
         AND stcm.source_code = src.PX
        WHERE src.PROCEDURESID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PROCEDURESID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_procedures;")


def load_measurement(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "lab_result_cm")
    if path is None:
        print("Skipping MEASUREMENT - no lab_result_cm.csv found")
        return

    prepare_source_view(
        con,
        "_temp_lab_result",
        path,
        [
            "LAB_RESULT_CM_ID", "PATID", "LAB_LOINC", "RESULT_DATE", "RESULT_TIME",
            "RESULT_NUM", "RESULT_UNIT", "PROVIDERID", "ENCOUNTERID", "RAW_LAB_NAME", "RAW_LAB_CODE"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "MEASUREMENT (from LAB_RESULT_CM)", f"""
        INSERT INTO measurement
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.LAB_RESULT_CM_ID) + (SELECT COALESCE(MAX(measurement_id), 0) FROM measurement) AS measurement_id,
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            COALESCE(
                stcm.target_concept_id,
                cr.concept_id_2,
                CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                0
            ) AS measurement_concept_id,
            parse_omop_date(src.RESULT_DATE) AS measurement_date,
            parse_omop_datetime(src.RESULT_DATE, src.RESULT_TIME) AS measurement_datetime,
            src.RESULT_TIME AS measurement_time,
            {TYPE_CONCEPT_EHR_MEASUREMENT} AS measurement_type_concept_id,
            0 AS operator_concept_id,
            TRY_CAST(src.RESULT_NUM AS DOUBLE) AS value_as_number,
            0 AS value_as_concept_id,
            0 AS unit_concept_id,
            NULL AS range_low,
            NULL AS range_high,
            COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
            COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
            NULL AS visit_detail_id,
            COALESCE(src.LAB_LOINC, src.RAW_LAB_NAME, src.RAW_LAB_CODE) AS measurement_source_value,
            COALESCE(c.concept_id, 0) AS measurement_source_concept_id,
            src.RESULT_UNIT AS unit_source_value,
            0 AS unit_source_concept_id,
            src.RESULT_NUM AS value_source_value,
            NULL AS measurement_event_id,
            0 AS meas_event_field_concept_id
        FROM _temp_lab_result src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN provider pr
          ON pr.provider_source_value = src.PROVIDERID
        LEFT JOIN visit_occurrence vo
          ON vo.visit_source_value = src.ENCOUNTERID
        LEFT JOIN concept c
          ON c.vocabulary_id = 'LOINC'
         AND c.concept_code = src.LAB_LOINC
        LEFT JOIN (
            SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
            FROM concept_relationship
            WHERE relationship_id = 'Maps to'
            GROUP BY concept_id_1
        ) cr
          ON cr.concept_id_1 = c.concept_id
        LEFT JOIN (
            SELECT source_code, MIN(target_concept_id) AS target_concept_id
            FROM source_to_concept_map
            WHERE source_vocabulary_id = 'LOINC'
              AND (invalid_reason IS NULL OR invalid_reason = '')
              AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
            GROUP BY source_code
        ) stcm
          ON stcm.source_code = src.LAB_LOINC
        WHERE src.LAB_RESULT_CM_ID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.LAB_RESULT_CM_ID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_lab_result;")


def load_vital(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "vital")
    if path is None:
        print("Skipping VITAL - no vital.csv found (optional table)")
        return

    prepare_source_view(
        con,
        "_temp_vital",
        path,
        [
            "VITALID", "PATID", "ENCOUNTERID", "MEASURE_DATE", "MEASURE_TIME",
            "VITAL_SOURCE", "HT", "WT", "ORIGINAL_BMI", "BMI", "SYSTOLIC", "DIASTOLIC", "SMOKING"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    # Staged, single-analyte inserts (FR-C5) avoid DuckDB memory access violations
    # on massive vital files by avoiding a single massive 5-way UNION ALL with sort.
    analytes = [
        ("HT", "8302-2", "TRY_CAST(HT AS DOUBLE)", "[in_us]", 9326, "HT IS NOT NULL AND TRY_CAST(HT AS DOUBLE) IS NOT NULL AND TRY_CAST(HT AS DOUBLE) > 0"),
        ("WT", "29463-7", "TRY_CAST(WT AS DOUBLE)", "[lb_av]", 8739, "WT IS NOT NULL AND TRY_CAST(WT AS DOUBLE) IS NOT NULL AND TRY_CAST(WT AS DOUBLE) > 0"),
        ("BMI", "39156-5", "TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE)", "kg/m2", 9531, "COALESCE(ORIGINAL_BMI, BMI) IS NOT NULL AND TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE) IS NOT NULL AND TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE) > 0"),
        ("SYSTOLIC", "8480-6", "TRY_CAST(SYSTOLIC AS DOUBLE)", "mm[Hg]", 8876, "SYSTOLIC IS NOT NULL AND TRY_CAST(SYSTOLIC AS DOUBLE) IS NOT NULL AND TRY_CAST(SYSTOLIC AS DOUBLE) > 0"),
        ("DIASTOLIC", "8462-4", "TRY_CAST(DIASTOLIC AS DOUBLE)", "mm[Hg]", 8876, "DIASTOLIC IS NOT NULL AND TRY_CAST(DIASTOLIC AS DOUBLE) IS NOT NULL AND TRY_CAST(DIASTOLIC AS DOUBLE) > 0"),
    ]
    for tag, loinc_code, val_expr, unit_str, unit_cid, where_cond in analytes:
        run_insert(con, f"MEASUREMENT (from VITAL - {tag})", f"""
            INSERT INTO measurement
            SELECT
                ROW_NUMBER() OVER () + (SELECT COALESCE(MAX(measurement_id), 0) FROM measurement) AS measurement_id,
                COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
                COALESCE(
                    stcm.target_concept_id,
                    cr.concept_id_2,
                    CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                    0
                ) AS measurement_concept_id,
                parse_omop_date(src.MEASURE_DATE) AS measurement_date,
                parse_omop_datetime(src.MEASURE_DATE, src.MEASURE_TIME) AS measurement_datetime,
                src.MEASURE_TIME AS measurement_time,
                {TYPE_CONCEPT_EHR_MEASUREMENT} AS measurement_type_concept_id,
                0 AS operator_concept_id,
                {val_expr} AS value_as_number,
                0 AS value_as_concept_id,
                {unit_cid} AS unit_concept_id,
                NULL AS range_low,
                NULL AS range_high,
                NULL AS provider_id,
                COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
                NULL AS visit_detail_id,
                '{loinc_code}' AS measurement_source_value,
                COALESCE(c.concept_id, 0) AS measurement_source_concept_id,
                '{unit_str}' AS unit_source_value,
                {unit_cid} AS unit_source_concept_id,
                CAST({val_expr} AS VARCHAR) AS value_source_value,
                NULL AS measurement_event_id,
                0 AS meas_event_field_concept_id
            FROM _temp_vital src
            LEFT JOIN person p
              ON p.person_source_value = {patid_expr}
            LEFT JOIN visit_occurrence vo
              ON vo.visit_source_value = src.ENCOUNTERID
            LEFT JOIN concept c
              ON c.vocabulary_id = 'LOINC'
             AND c.concept_code = '{loinc_code}'
            LEFT JOIN (
                SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
                FROM concept_relationship
                WHERE relationship_id = 'Maps to'
                GROUP BY concept_id_1
            ) cr
              ON cr.concept_id_1 = c.concept_id
            LEFT JOIN (
                SELECT source_code, MIN(target_concept_id) AS target_concept_id
                FROM source_to_concept_map
                WHERE source_vocabulary_id = 'LOINC'
                  AND (invalid_reason IS NULL OR invalid_reason = '')
                  AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
                GROUP BY source_code
            ) stcm
              ON stcm.source_code = '{loinc_code}'
            WHERE {where_cond}
            QUALIFY ROW_NUMBER() OVER (PARTITION BY src.VITALID, '{tag}') = 1;
        """)
    # PCORnet VITAL.SMOKING -> OBSERVATION "Tobacco smoking status" (LOINC 72166-2, concept 43054909) with the
    # matching LOINC answer concept in value_as_concept_id. NI/UN/OT and unrecognized codes carry no status.
    run_insert(con, "OBSERVATION (smoking status from VITAL)", f"""
        INSERT INTO observation
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.VITALID) + (SELECT COALESCE(MAX(observation_id), 0) FROM observation) AS observation_id,
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            {CONCEPT_TOBACCO_SMOKING_STATUS} AS observation_concept_id,
            parse_omop_date(src.MEASURE_DATE) AS observation_date,
            parse_omop_datetime(src.MEASURE_DATE, src.MEASURE_TIME) AS observation_datetime,
            {TYPE_CONCEPT_EHR_MEASUREMENT} AS observation_type_concept_id,
            NULL AS value_as_number,
            NULL AS value_as_string,
            sm.value_concept_id AS value_as_concept_id,
            NULL AS qualifier_concept_id,
            NULL AS unit_concept_id,
            NULL AS provider_id,
            COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
            NULL AS visit_detail_id,
            '72166-2' AS observation_source_value,
            0 AS observation_source_concept_id,
            NULL AS unit_source_value,
            NULL AS qualifier_source_value,
            src.SMOKING AS value_source_value,
            NULL AS observation_event_id,
            0 AS obs_event_field_concept_id
        FROM _temp_vital src
        JOIN (VALUES ('01', 45881517), ('02', 45884037), ('03', 45883458), ('04', 45879404), ('05', 45881518), ('06', 45885135), ('07', 45884038), ('08', 45878118)) sm(code, value_concept_id)
          ON sm.code = LPAD(TRIM(src.SMOKING), 2, '0')
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN visit_occurrence vo
          ON vo.visit_source_value = src.ENCOUNTERID
        WHERE src.VITALID IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.VITALID) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_vital;")


def load_drug_exposure(con, source_dir, site_id=None, disambiguate_patids=False):
    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"
    loaded_any = False

    # 1. PRESCRIBING (Prescriptions written)
    path_rx = find_source_file(source_dir, "prescribing")
    if path_rx is not None:
        loaded_any = True
        prepare_source_view(
            con,
            "_temp_prescribing",
            path_rx,
            [
                "PRESCRIBINGID", "PATID", "RXNORM_CUI", "RX_START_DATE", "RX_END_DATE",
                "RAW_RX_MED_NAME", "RAW_RX_NDC", "PROVIDERID", "ENCOUNTERID"
            ],
        )
        run_insert(con, "DRUG_EXPOSURE (from PRESCRIBING)", f"""
            INSERT INTO drug_exposure
            SELECT
                ROW_NUMBER() OVER (ORDER BY src.PRESCRIBINGID) + (SELECT COALESCE(MAX(drug_exposure_id), 0) FROM drug_exposure) AS drug_exposure_id,
                COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
                COALESCE(
                    stcm.target_concept_id,
                    cr.concept_id_2,
                    CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                    0
                ) AS drug_concept_id,
                parse_omop_date(src.RX_START_DATE) AS drug_exposure_start_date,
                parse_omop_datetime(src.RX_START_DATE, NULL) AS drug_exposure_start_datetime,
                COALESCE(parse_omop_date(src.RX_END_DATE), parse_omop_date(src.RX_START_DATE)) AS drug_exposure_end_date,
                parse_omop_datetime(src.RX_END_DATE, NULL) AS drug_exposure_end_datetime,
                NULL AS verbatim_end_date,
                {TYPE_CONCEPT_EHR_DRUG} AS drug_type_concept_id,
                NULL AS stop_reason,
                NULL AS refills,
                NULL AS quantity,
                NULL AS days_supply,
                NULL AS sig,
                0 AS route_concept_id,
                NULL AS lot_number,
                COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
                COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
                NULL AS visit_detail_id,
                COALESCE(src.RXNORM_CUI, src.RAW_RX_NDC, src.RAW_RX_MED_NAME) AS drug_source_value,
                COALESCE(c.concept_id, 0) AS drug_source_concept_id,
                NULL AS route_source_value,
                NULL AS dose_unit_source_value
            FROM _temp_prescribing src
            LEFT JOIN person p
              ON p.person_source_value = {patid_expr}
            LEFT JOIN provider pr
              ON pr.provider_source_value = src.PROVIDERID
            LEFT JOIN visit_occurrence vo
              ON vo.visit_source_value = src.ENCOUNTERID
            LEFT JOIN concept c
              ON c.vocabulary_id = 'RxNorm'
             AND c.concept_code = src.RXNORM_CUI
            LEFT JOIN (
                SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
                FROM concept_relationship
                WHERE relationship_id = 'Maps to'
                GROUP BY concept_id_1
            ) cr
              ON cr.concept_id_1 = c.concept_id
            LEFT JOIN (
                SELECT source_code, MIN(target_concept_id) AS target_concept_id
                FROM source_to_concept_map
                WHERE source_vocabulary_id = 'RxNorm'
                  AND (invalid_reason IS NULL OR invalid_reason = '')
                  AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
                GROUP BY source_code
            ) stcm
              ON stcm.source_code = src.RXNORM_CUI
            WHERE src.PRESCRIBINGID IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PRESCRIBINGID) = 1;
        """)
        con.execute("DROP VIEW IF EXISTS _temp_prescribing;")

    # 2. MED_ADMIN (Administered medications)
    path_admin = find_source_file(source_dir, "med_admin")
    if path_admin is not None:
        loaded_any = True
        prepare_source_view(
            con,
            "_temp_med_admin",
            path_admin,
            [
                "MEDADMINID", "PATID", "ENCOUNTERID", "MEDADMIN_CODE", "MEDADMIN_TYPE", "MEDADMIN_START_DATE",
                "MEDADMIN_START_TIME", "MEDADMIN_STOP_DATE", "RAW_MEDADMIN_MED_NAME",
                "RAW_RX_NDC", "PROVIDERID"
            ],
        )
        run_insert(con, "DRUG_EXPOSURE (from MED_ADMIN)", f"""
            INSERT INTO drug_exposure
            SELECT
                ROW_NUMBER() OVER (ORDER BY src.MEDADMINID) + (SELECT COALESCE(MAX(drug_exposure_id), 0) FROM drug_exposure) AS drug_exposure_id,
                COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
                COALESCE(
                    stcm.target_concept_id,
                    cr.concept_id_2,
                    CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                    0
                ) AS drug_concept_id,
                parse_omop_date(src.MEDADMIN_START_DATE) AS drug_exposure_start_date,
                parse_omop_datetime(src.MEDADMIN_START_DATE, src.MEDADMIN_START_TIME) AS drug_exposure_start_datetime,
                COALESCE(parse_omop_date(src.MEDADMIN_STOP_DATE), parse_omop_date(src.MEDADMIN_START_DATE)) AS drug_exposure_end_date,
                parse_omop_datetime(COALESCE(src.MEDADMIN_STOP_DATE, src.MEDADMIN_START_DATE), NULL) AS drug_exposure_end_datetime,
                NULL AS verbatim_end_date,
                {TYPE_CONCEPT_MED_ADMIN} AS drug_type_concept_id,
                NULL AS stop_reason,
                NULL AS refills,
                NULL AS quantity,
                NULL AS days_supply,
                NULL AS sig,
                0 AS route_concept_id,
                NULL AS lot_number,
                COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
                COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
                NULL AS visit_detail_id,
                COALESCE(src.MEDADMIN_CODE, src.RAW_RX_NDC, src.RAW_MEDADMIN_MED_NAME) AS drug_source_value,
                COALESCE(c.concept_id, 0) AS drug_source_concept_id,
                NULL AS route_source_value,
                NULL AS dose_unit_source_value
            FROM _temp_med_admin src
            LEFT JOIN person p
              ON p.person_source_value = {patid_expr}
            LEFT JOIN provider pr
              ON pr.provider_source_value = src.PROVIDERID
            LEFT JOIN visit_occurrence vo
              ON vo.visit_source_value = src.ENCOUNTERID
            LEFT JOIN concept c
              ON c.vocabulary_id = CASE
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) = 'ND' THEN 'NDC'
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) IN ('CH', 'CP') THEN 'HCPCS'
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) = 'RX' THEN 'RxNorm'
                   WHEN src.MEDADMIN_CODE IS NULL AND src.RAW_RX_NDC IS NOT NULL THEN 'NDC'
                   ELSE 'RxNorm'
                 END
             AND c.concept_code = COALESCE(src.MEDADMIN_CODE, src.RAW_RX_NDC)
            LEFT JOIN (
                SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
                FROM concept_relationship
                WHERE relationship_id = 'Maps to'
                GROUP BY concept_id_1
            ) cr
              ON cr.concept_id_1 = c.concept_id
            LEFT JOIN (
                SELECT source_code, source_vocabulary_id, MIN(target_concept_id) AS target_concept_id
                FROM source_to_concept_map
                WHERE (invalid_reason IS NULL OR invalid_reason = '')
                  AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
                GROUP BY source_code, source_vocabulary_id
            ) stcm
              ON stcm.source_code = COALESCE(src.MEDADMIN_CODE, src.RAW_RX_NDC)
             AND stcm.source_vocabulary_id = CASE
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) = 'ND' THEN 'NDC'
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) IN ('CH', 'CP') THEN 'HCPCS'
                   WHEN UPPER(TRIM(COALESCE(src.MEDADMIN_TYPE, ''))) = 'RX' THEN 'RxNorm'
                   WHEN src.MEDADMIN_CODE IS NULL AND src.RAW_RX_NDC IS NOT NULL THEN 'NDC'
                   ELSE 'RxNorm'
                 END
            WHERE src.MEDADMINID IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY src.MEDADMINID) = 1;
        """)
        con.execute("DROP VIEW IF EXISTS _temp_med_admin;")

    # 3. DISPENSING (Prescriptions dispensed in pharmacy)
    path_disp = find_source_file(source_dir, "dispensing")
    if path_disp is not None:
        loaded_any = True
        prepare_source_view(
            con,
            "_temp_dispensing",
            path_disp,
            [
                "DISPENSINGID", "PATID", "PRESCRIBINGID", "DISPENSING_DATE",
                "RAW_RX_NDC", "DISPENSING_SUP", "DISPENSING_AMT", "DISPENSING_TYPE"
            ],
        )
        run_insert(con, "DRUG_EXPOSURE (from DISPENSING)", f"""
            INSERT INTO drug_exposure
            SELECT
                ROW_NUMBER() OVER (ORDER BY src.DISPENSINGID) + (SELECT COALESCE(MAX(drug_exposure_id), 0) FROM drug_exposure) AS drug_exposure_id,
                COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
                COALESCE(
                    stcm.target_concept_id,
                    cr.concept_id_2,
                    CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
                    0
                ) AS drug_concept_id,
                parse_omop_date(src.DISPENSING_DATE) AS drug_exposure_start_date,
                parse_omop_datetime(src.DISPENSING_DATE, NULL) AS drug_exposure_start_datetime,
                parse_omop_date(src.DISPENSING_DATE) AS drug_exposure_end_date,
                parse_omop_datetime(src.DISPENSING_DATE, NULL) AS drug_exposure_end_datetime,
                NULL AS verbatim_end_date,
                {TYPE_CONCEPT_DISPENSING} AS drug_type_concept_id,
                NULL AS stop_reason,
                NULL AS refills,
                TRY_CAST(src.DISPENSING_AMT AS DOUBLE) AS quantity,
                TRY_CAST(src.DISPENSING_SUP AS INTEGER) AS days_supply,
                NULL AS sig,
                0 AS route_concept_id,
                NULL AS lot_number,
                NULL AS provider_id,
                NULL AS visit_occurrence_id,
                NULL AS visit_detail_id,
                src.RAW_RX_NDC AS drug_source_value,
                COALESCE(c.concept_id, 0) AS drug_source_concept_id,
                NULL AS route_source_value,
                NULL AS dose_unit_source_value
            FROM _temp_dispensing src
            LEFT JOIN person p
              ON p.person_source_value = {patid_expr}
            LEFT JOIN concept c
              ON c.vocabulary_id = CASE
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) = 'RX' THEN 'RxNorm'
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) IN ('CH', 'CP') THEN 'HCPCS'
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) = 'ND' THEN 'NDC'
                   ELSE 'NDC'
                 END
             AND c.concept_code = src.RAW_RX_NDC
            LEFT JOIN (
                SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
                FROM concept_relationship
                WHERE relationship_id = 'Maps to'
                GROUP BY concept_id_1
            ) cr
              ON cr.concept_id_1 = c.concept_id
            LEFT JOIN (
                SELECT source_code, source_vocabulary_id, MIN(target_concept_id) AS target_concept_id
                FROM source_to_concept_map
                WHERE (invalid_reason IS NULL OR invalid_reason = '')
                  AND (valid_end_date IS NULL OR valid_end_date >= CURRENT_DATE)
                GROUP BY source_code, source_vocabulary_id
            ) stcm
              ON stcm.source_code = src.RAW_RX_NDC
             AND stcm.source_vocabulary_id = CASE
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) = 'RX' THEN 'RxNorm'
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) IN ('CH', 'CP') THEN 'HCPCS'
                   WHEN UPPER(TRIM(COALESCE(src.DISPENSING_TYPE, ''))) = 'ND' THEN 'NDC'
                   ELSE 'NDC'
                 END
            WHERE src.DISPENSINGID IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY src.DISPENSINGID) = 1;
        """)
        con.execute("DROP VIEW IF EXISTS _temp_dispensing;")

    if not loaded_any:
        print("Skipping DRUG_EXPOSURE - no prescribing.csv, med_admin.csv, or dispensing.csv found")


def load_death(con, source_dir, site_id=None, disambiguate_patids=False):
    """Modular ingestion of PCORnet death.csv into OMOP death table.
    Gracefully skipped if death.csv is missing."""
    path = find_source_file(source_dir, "death")
    if path is None:
        print("Skipping DEATH - no death.csv found (optional table)")
        return

    prepare_source_view(
        con,
        "_temp_death",
        path,
        ["PATID", "DEATH_DATE", "DEATH_DATE_IMPUTE", "DEATH_SOURCE", "DEATH_MATCH_CONFIDENCE"],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "DEATH", f"""
        INSERT INTO death (
            person_id,
            death_date,
            death_datetime,
            death_type_concept_id,
            cause_concept_id,
            cause_source_value,
            cause_source_concept_id
        )
        SELECT
            COALESCE(p.person_id, pcornet_id({patid_expr})) AS person_id,
            parse_omop_date(src.DEATH_DATE) AS death_date,
            parse_omop_datetime(src.DEATH_DATE, NULL) AS death_datetime,
            {TYPE_CONCEPT_EHR_DEATH} AS death_type_concept_id,
            0 AS cause_concept_id,
            src.DEATH_SOURCE AS cause_source_value,
            0 AS cause_source_concept_id
        FROM _temp_death src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        WHERE src.PATID IS NOT NULL 
          AND src.DEATH_DATE IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY {patid_expr} ORDER BY parse_omop_date(src.DEATH_DATE) DESC) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_death;")


def _iso_date(value, name):
    """Returns ``value`` (a date or ISO string) as a validated ``YYYY-MM-DD`` string."""
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be a date like '1900-01-02', got {value!r}.") from exc


def resolve_max_date(con, max_date=None, min_date="1900-01-02"):
    """Resolves the data cutoff for observation periods (N2).

    ``None`` keeps today (the extraction date). ``"auto"`` takes the latest clinical event date strictly before today, so
    events a source fills with the build date do not extend the periods. Any other value must be an ISO date. Returns an ISO
    date string, or ``None`` when ``max_date`` is ``None`` or ``"auto"`` finds no event.
    """
    if max_date is None:
        return None
    if isinstance(max_date, str) and max_date.lower() == "auto":
        lo = _iso_date(min_date, "min_date")
        row = con.execute(f"""
            SELECT MAX(d) FROM (
                SELECT MAX(visit_start_date) d FROM visit_occurrence WHERE visit_start_date < CURRENT_DATE AND visit_start_date >= DATE '{lo}'
                UNION ALL SELECT MAX(visit_end_date) FROM visit_occurrence WHERE visit_end_date < CURRENT_DATE AND visit_end_date >= DATE '{lo}'
                UNION ALL SELECT MAX(condition_start_date) FROM condition_occurrence WHERE condition_start_date < CURRENT_DATE AND condition_start_date >= DATE '{lo}'
                UNION ALL SELECT MAX(procedure_date) FROM procedure_occurrence WHERE procedure_date < CURRENT_DATE AND procedure_date >= DATE '{lo}'
                UNION ALL SELECT MAX(drug_exposure_start_date) FROM drug_exposure WHERE drug_exposure_start_date < CURRENT_DATE AND drug_exposure_start_date >= DATE '{lo}'
                UNION ALL SELECT MAX(measurement_date) FROM measurement WHERE measurement_date < CURRENT_DATE AND measurement_date >= DATE '{lo}'
            )""").fetchone()[0]
        return None if row is None else row.isoformat()
    return _iso_date(max_date, "max_date")


def load_enrollment_spans(con, source_dir, site_id=None, disambiguate_patids=False,
                          min_date="1900-01-02", max_date=None):
    """Builds ``_enrollment_spans(person_id, start_date, end_date)`` from the PCORnet ENROLLMENT file (FR-C10).

    Returns the number of merged enrolment spans (0 and no table when the source has no ENROLLMENT file). Spans are
    clipped to ``[min_date, max_date]`` (default: today), dropped when empty, and merged when they overlap or touch,
    so each row is one continuous enrolment. Persons are matched on ``person_source_value``.
    """
    path = find_source_file(source_dir, "enrollment")
    if path is None:
        return 0
    lo = _iso_date(min_date, "min_date")
    hi = f"DATE '{_iso_date(max_date, 'max_date')}'" if max_date is not None else "CURRENT_DATE"
    prepare_source_view(con, "_temp_enrollment", path, ["PATID", "ENR_START_DATE", "ENR_END_DATE"])
    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"
    con.execute("DROP TABLE IF EXISTS _enrollment_spans;")
    con.execute(f"""
        CREATE TEMPORARY TABLE _enrollment_spans AS
        WITH raw AS (
            SELECT p.person_id,
                   GREATEST(CAST(parse_omop_date(src.ENR_START_DATE) AS DATE), DATE '{lo}') AS s,
                   LEAST(COALESCE(CAST(parse_omop_date(src.ENR_END_DATE) AS DATE), {hi}), {hi}) AS e
            FROM _temp_enrollment src
            JOIN person p ON p.person_source_value = {patid_expr}
            WHERE parse_omop_date(src.ENR_START_DATE) IS NOT NULL
        ),
        valid AS (SELECT * FROM raw WHERE s <= e),
        marked AS (
            SELECT *, CASE WHEN s <= MAX(e) OVER (PARTITION BY person_id ORDER BY s, e
                                                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + INTERVAL 1 DAY
                           THEN 0 ELSE 1 END AS new_span
            FROM valid
        ),
        grouped AS (SELECT *, SUM(new_span) OVER (PARTITION BY person_id ORDER BY s, e) AS span_id FROM marked)
        SELECT person_id, MIN(s) AS start_date, MAX(e) AS end_date
        FROM grouped GROUP BY person_id, span_id;
    """)
    n = con.execute("SELECT COUNT(*) FROM _enrollment_spans").fetchone()[0]
    print(f"  -> {n} enrolment spans from {os.path.basename(path)}")
    return int(n)


def build_observation_period(con, min_date="1900-01-02", max_date=None, use_enrollment=False,
                             enrollment_type_concept_id=0):
    """Synthesizes patient observation envelopes across all clinical event tables (FR-C10).

    The period is the span from a person's first to last *plausible* event, not a real enrolment window. Events
    dated before ``min_date`` (default 1900-01-02: SAS/Excel zero dates such as 1840-12-31 and 1899-12-30 and
    ``1900-01-01`` placeholders) or after ``max_date`` (default: today, the extraction date) are ignored. A person
    with no plausible event gets no observation period. Pass the source's real data cutoff as ``max_date`` when events
    are filled with the build date, which today's date cannot distinguish from a genuine event; ``max_date="auto"``
    uses the latest event dated before today (see :func:`resolve_max_date`).

    ``use_enrollment=True`` (needs ``_enrollment_spans`` from :func:`load_enrollment_spans`) writes each person's real
    enrolment spans with ``period_type_concept_id = enrollment_type_concept_id`` (default 0: set your site's concept)
    and keeps the event-span rule only for people with no enrolment record.
    """
    lo = _iso_date(min_date, "min_date")
    cutoff = resolve_max_date(con, max_date, min_date)
    hi = f"DATE '{cutoff}'" if cutoff is not None else "CURRENT_DATE"
    con.execute("DELETE FROM observation_period;")
    if use_enrollment:
        enr_type = int(enrollment_type_concept_id)
        run_insert(con, "OBSERVATION_PERIOD (enrolment spans)", f"""
            INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,
                                            observation_period_end_date, period_type_concept_id)
            SELECT ROW_NUMBER() OVER (ORDER BY person_id, start_date), person_id, start_date, end_date, {enr_type}
            FROM _enrollment_spans;
        """)
    run_insert(con, "OBSERVATION_PERIOD (synthesized)", f"""
        INSERT INTO observation_period (
            observation_period_id,
            person_id,
            observation_period_start_date,
            observation_period_end_date,
            period_type_concept_id
        )
        WITH clinical_events AS (
            SELECT person_id, visit_start_date AS event_date FROM visit_occurrence WHERE visit_start_date IS NOT NULL
            UNION ALL
            SELECT person_id, visit_end_date AS event_date FROM visit_occurrence WHERE visit_end_date IS NOT NULL
            UNION ALL
            SELECT person_id, condition_start_date AS event_date FROM condition_occurrence WHERE condition_start_date IS NOT NULL
            UNION ALL
            SELECT person_id, COALESCE(condition_end_date, condition_start_date) AS event_date FROM condition_occurrence WHERE condition_start_date IS NOT NULL
            UNION ALL
            SELECT person_id, procedure_date AS event_date FROM procedure_occurrence WHERE procedure_date IS NOT NULL
            UNION ALL
            SELECT person_id, drug_exposure_start_date AS event_date FROM drug_exposure WHERE drug_exposure_start_date IS NOT NULL
            UNION ALL
            SELECT person_id, COALESCE(drug_exposure_end_date, drug_exposure_start_date) AS event_date FROM drug_exposure WHERE drug_exposure_start_date IS NOT NULL
            UNION ALL
            SELECT person_id, measurement_date AS event_date FROM measurement WHERE measurement_date IS NOT NULL
        ),
        envelopes AS (
            SELECT
                person_id,
                MIN(event_date) AS obs_start_date,
                MAX(event_date) AS obs_end_date
            FROM clinical_events
            WHERE event_date >= DATE '{lo}' AND event_date <= {hi}
            GROUP BY person_id
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY person_id) + (SELECT COALESCE(MAX(observation_period_id), 0) FROM observation_period)
                AS observation_period_id,
            person_id,
            obs_start_date AS observation_period_start_date,
            obs_end_date AS observation_period_end_date,
            32827 AS period_type_concept_id -- EHR encounter record
        FROM envelopes
        {"WHERE person_id NOT IN (SELECT person_id FROM _enrollment_spans)" if use_enrollment else ""};
    """)


def build_drug_era(con):
    """Collapses drug exposures into continuous eras using standard OHDSI 30-day gap window."""
    con.execute("DELETE FROM drug_era;")
    run_insert(con, "DRUG_ERA (synthesized)", """
        INSERT INTO drug_era (
            drug_era_id,
            person_id,
            drug_concept_id,
            drug_era_start_date,
            drug_era_end_date,
            drug_exposure_count,
            gap_days
        )
        WITH exposures AS (
            SELECT
                person_id,
                drug_concept_id,
                drug_exposure_start_date AS start_date,
                COALESCE(drug_exposure_end_date, drug_exposure_start_date) AS end_date
            FROM drug_exposure
            WHERE drug_concept_id != 0 AND drug_exposure_start_date IS NOT NULL
        ),
        lagged AS (
            SELECT
                person_id,
                drug_concept_id,
                start_date,
                end_date,
                MAX(end_date) OVER (
                    PARTITION BY person_id, drug_concept_id
                    ORDER BY start_date, end_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS prev_max_end_date
            FROM exposures
        ),
        era_starts AS (
            SELECT
                person_id,
                drug_concept_id,
                start_date,
                end_date,
                CASE
                    WHEN prev_max_end_date IS NULL THEN 1
                    WHEN start_date > prev_max_end_date + INTERVAL 30 DAY THEN 1
                    ELSE 0
                END AS is_new_era
            FROM lagged
        ),
        era_groups AS (
            SELECT
                person_id,
                drug_concept_id,
                start_date,
                end_date,
                SUM(is_new_era) OVER (
                    PARTITION BY person_id, drug_concept_id
                    ORDER BY start_date, end_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS era_group
            FROM era_starts
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY person_id, drug_concept_id, MIN(start_date)) AS drug_era_id,
            person_id,
            drug_concept_id,
            MIN(start_date) AS drug_era_start_date,
            MAX(end_date) AS drug_era_end_date,
            COUNT(*) AS drug_exposure_count,
            NULL AS gap_days
        FROM era_groups
        GROUP BY person_id, drug_concept_id, era_group;
    """)


def build_condition_era(con):
    """Collapses condition occurrences into continuous eras using standard OHDSI 30-day persistence window."""
    con.execute("DELETE FROM condition_era;")
    run_insert(con, "CONDITION_ERA (synthesized)", """
        INSERT INTO condition_era (
            condition_era_id,
            person_id,
            condition_concept_id,
            condition_era_start_date,
            condition_era_end_date,
            condition_occurrence_count
        )
        WITH conditions AS (
            SELECT
                person_id,
                condition_concept_id,
                condition_start_date AS start_date,
                COALESCE(condition_end_date, condition_start_date) AS end_date
            FROM condition_occurrence
            WHERE condition_concept_id != 0 AND condition_start_date IS NOT NULL
        ),
        lagged AS (
            SELECT
                person_id,
                condition_concept_id,
                start_date,
                end_date,
                MAX(end_date) OVER (
                    PARTITION BY person_id, condition_concept_id
                    ORDER BY start_date, end_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS prev_max_end_date
            FROM conditions
        ),
        era_starts AS (
            SELECT
                person_id,
                condition_concept_id,
                start_date,
                end_date,
                CASE
                    WHEN prev_max_end_date IS NULL THEN 1
                    WHEN start_date > prev_max_end_date + INTERVAL 30 DAY THEN 1
                    ELSE 0
                END AS is_new_era
            FROM lagged
        ),
        era_groups AS (
            SELECT
                person_id,
                condition_concept_id,
                start_date,
                end_date,
                SUM(is_new_era) OVER (
                    PARTITION BY person_id, condition_concept_id
                    ORDER BY start_date, end_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS era_group
            FROM era_starts
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY person_id, condition_concept_id, MIN(start_date)) AS condition_era_id,
            person_id,
            condition_concept_id,
            MIN(start_date) AS condition_era_start_date,
            MAX(end_date) AS condition_era_end_date,
            COUNT(*) AS condition_occurrence_count
        FROM era_groups
        GROUP BY person_id, condition_concept_id, era_group;
    """)


def load_cdm_source(con, cdm_source_name=None, cdm_holder=None, site_id=None, site_anon=None, site_name=None):
    """Populates the CDM_SOURCE metadata table with OMOP CDM v5.4.0 concept ID (756265)."""
    con.execute("DELETE FROM cdm_source;")
    if cdm_source_name is None:
        if site_anon is not None:
            cdm_source_name = site_anon
        elif site_name is not None:
            cdm_source_name = site_name
        else:
            cdm_source_name = "PCORnet -> DuckDB OMOP CDM"

    cdm_source_abbreviation = site_anon if site_anon is not None else "PCORNET"

    if cdm_holder is None:
        cdm_holder = site_name if site_name is not None else (site_anon if site_anon is not None else "omop-duck-db")

    src_name_esc = str(cdm_source_name).replace("'", "''")
    src_abbr_esc = str(cdm_source_abbreviation).replace("'", "''")
    holder_esc = str(cdm_holder).replace("'", "''")

    run_insert(con, "CDM_SOURCE", f"""
        INSERT INTO cdm_source
        SELECT
            '{src_name_esc}' AS cdm_source_name,
            '{src_abbr_esc}' AS cdm_source_abbreviation,
            '{holder_esc}' AS cdm_holder,
            'PCORnet extract mapped to OMOP CDM v5.4 in DuckDB' AS source_description,
            'https://github.com/jkylearmstrong/omop-duck-db' AS source_documentation_reference,
            'https://github.com/jkylearmstrong/omop-duck-db' AS cdm_etl_reference,
            CURRENT_DATE AS source_release_date,
            CURRENT_DATE AS cdm_release_date,
            'v5.4' AS cdm_version,
            756265 AS cdm_version_concept_id,
            COALESCE((SELECT vocabulary_version FROM vocabulary WHERE vocabulary_id = 'None' LIMIT 1), 'Unknown') AS vocabulary_version;
    """)


def etl_pcornet(
    source_dir,
    db_path="omop_cdm.duckdb",
    build_schema_flag=False,
    memory_limit=None,
    threads=None,
    temp_dir=None,
    central_vocab=None,
    site_id=None,
    site_anon=None,
    site_name=None,
    disambiguate_patids=False,
    auto_resources=False,
    source_format=None,
    strict_source=False,
    key_check="warn",
    key_check_callback=None,
    use_enrollment="auto",
    enrollment_type_concept_id=0,
    data_cutoff=None,
):
    """Run full PCORnet -> OMOP CDM v5.4 pipeline.

    ``key_check`` ('warn' | 'raise' | 'ignore') compares distinct source ids with distinct surrogate keys at the end
    of the build (FR-C12, see ``omop_etl.key_checks.check_etl_keys``); ``key_check_callback`` receives the list of
    result dicts (the CLI writes them to ``run_manifest.json``).

    ``use_enrollment`` ('auto' | True | False) builds ``observation_period`` from the PCORnet ENROLLMENT file when the
    source has one (see ``build_observation_period``); people without an enrolment record keep the event-span period.

    ``data_cutoff`` (None | ISO date | ``"auto"``) caps observation-period ends (N2): None keeps today, ``"auto"`` uses
    the latest event dated before today, so build-date placeholders do not set period ends.

    ``auto_resources=True`` sizes DuckDB to the host (see ``omop_etl.resources.configure_resources``);
    explicit ``memory_limit`` / ``threads`` / ``temp_dir`` still win over the detected values.

    ``source_dir`` holds ``<table>.csv`` or ``<table>.parquet`` files (see ``omop_etl.source_check``).
    ``strict_source=True`` runs ``check_source`` first and raises ``SourceContractError`` naming any missing
    required table / column instead of null-padding it; ``source_format`` ('csv' | 'parquet') restricts that check.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    if strict_source:
        from omop_etl.source_check import check_source

        check_source(source_dir, source_format, raise_on_error=True)

    if disambiguate_patids and site_id is None:
        raise ValueError("disambiguate_patids requires site_id to be specified.")

    con = duckdb.connect(sanitize_sql_path(db_path))
    if auto_resources:
        from omop_etl.resources import configure_resources

        cfg = configure_resources(con, temp_directory=temp_dir, threads=threads)
        print(f"Auto resources: memory_limit={cfg.memory_limit}, threads={cfg.threads}, temp={cfg.temp_directory}")
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}';")
    if threads:
        con.execute(f"SET threads = {threads};")
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
        con.execute(f"SET temp_directory = '{sanitize_sql_path(temp_dir)}';")

    if central_vocab:
        attach_central_vocabulary(con, central_vocab, temporary=True)

    if build_schema_flag:
        build_schema(con)
    load_macros(con)

    load_care_site(con, source_dir, site_id=site_id, site_anon=site_anon, site_name=site_name)
    load_provider(con, source_dir)
    load_person(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_location(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_visit_occurrence(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_condition_occurrence(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_procedure_occurrence(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_measurement(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_vital(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_drug_exposure(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)
    load_death(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids)

    cutoff = resolve_max_date(con, data_cutoff)
    enrolled = False
    if use_enrollment in ("auto", True):
        enrolled = load_enrollment_spans(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids,
                                         max_date=cutoff) > 0
        if use_enrollment is True and not enrolled:
            raise FileNotFoundError(f"use_enrollment=True but {source_dir} has no ENROLLMENT file with usable rows.")
    build_observation_period(con, max_date=cutoff, use_enrollment=enrolled, enrollment_type_concept_id=enrollment_type_concept_id)
    build_drug_era(con)
    build_condition_era(con)
    load_cdm_source(con, site_id=site_id, site_anon=site_anon, site_name=site_name)

    if key_check != "ignore":
        from omop_etl.key_checks import check_etl_keys

        try:
            report = check_etl_keys(con, source_dir, site_id=site_id, disambiguate_patids=disambiguate_patids,
                                    on_collision=key_check)
        except Exception:
            con.close()
            raise
        for r in report:
            print(f"Key check {r['table']}.{r['id_column']}: {r['distinct_ids']} ids -> {r['distinct_keys']} keys "
                  f"({'ok' if r['ok'] else str(r['lost']) + ' lost'}); {r['cdm_table']} rows = {r['cdm_rows']}")
        if key_check_callback is not None:
            key_check_callback(report)

    con.close()
    print("PCORnet ETL complete.")
    return db_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, help="Directory containing PCORnet-format CSVs")
    parser.add_argument("--db-path", default="omop_cdm.duckdb", help="Path to the OMOP CDM DuckDB file")
    parser.add_argument("--central-vocab", default=None, help="Optional path to central vocabulary DuckDB to attach with zero-copy views")
    parser.add_argument("--build-schema", action="store_true", help="(Re)create CDM tables before loading")
    parser.add_argument("--memory-limit", default=None, help="DuckDB memory limit (e.g. 16GB)")
    parser.add_argument("--threads", type=int, default=None, help="DuckDB worker threads")
    parser.add_argument("--temp-dir", default=None, help="DuckDB disk spill temporary directory")
    parser.add_argument("--auto-resources", action="store_true", help="Size DuckDB memory_limit (75%% of available RAM), threads and the spill directory to this host")
    parser.add_argument("--site-id", type=int, default=None, help="Numeric site identifier (e.g. 1 to 5)")
    parser.add_argument("--site-anon", default=None, help="Publication pseudonym (e.g. 'Site A')")
    parser.add_argument("--site-name", default=None, help="Optional institutional name or internal descriptor (e.g. 'Hospital System A')")
    parser.add_argument("--disambiguate-patids", action="store_true", help="Suffix PATID with -<site_id> to disambiguate patient IDs across sites")
    parser.add_argument("--key-check", choices=["warn", "raise", "ignore"], default="warn", help="Compare distinct source ids with distinct surrogate keys after the build")
    parser.add_argument("--use-enrollment", choices=["auto", "true", "false"], default="auto", help="Build observation_period from the PCORnet ENROLLMENT file")
    parser.add_argument("--enrollment-type-concept-id", type=int, default=0, help="period_type_concept_id for enrolment-derived periods")
    args = parser.parse_args()

    etl_pcornet(
        source_dir=args.source_dir,
        db_path=args.db_path,
        build_schema_flag=args.build_schema,
        memory_limit=args.memory_limit,
        threads=args.threads,
        temp_dir=args.temp_dir,
        central_vocab=args.central_vocab,
        site_id=args.site_id,
        site_anon=args.site_anon,
        site_name=args.site_name,
        disambiguate_patids=args.disambiguate_patids,
        auto_resources=args.auto_resources,
        key_check=args.key_check,
        use_enrollment={"auto": "auto", "true": True, "false": False}[args.use_enrollment],
        enrollment_type_concept_id=args.enrollment_type_concept_id,
    )


if __name__ == "__main__":
    main()
