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
TYPE_CONCEPT_EHR_DRUG = 32838
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


def find_source_file(source_dir, table_name):
    pattern = re.compile(rf"^{re.escape(table_name)}\.csv$", re.IGNORECASE)
    for name in os.listdir(source_dir):
        if pattern.match(name):
            return os.path.join(source_dir, name)
    return None


def prepare_source_view(con, view_name, csv_path, expected_columns, aliases=None):
    """Creates a temporary view over a source CSV with case-insensitive column discovery,
    alias resolution (e.g. ssid -> PATID), and NULL projections for missing columns."""
    if aliases is None:
        aliases = DEFAULT_ALIASES

    cols_info = con.execute(
        f"DESCRIBE SELECT * FROM read_csv_auto('{csv_path}', union_by_name = true, all_varchar = true) LIMIT 0"
    ).fetchall()
    actual_cols = {col[0].upper(): col[0] for col in cols_info}

    exprs = []
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
        else:
            exprs.append(f"NULL AS {tgt}")

    sql = f"CREATE OR REPLACE TEMPORARY VIEW {view_name} AS SELECT {', '.join(exprs)} FROM read_csv_auto('{csv_path}', union_by_name = true, all_varchar = true);"
    con.execute(sql)


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

    normalized_path = os.path.abspath(vocab_db_path).replace("\\", "/")
    con.execute(f"ATTACH '{normalized_path}' AS central_vocab (READ_ONLY);")
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


def load_location(con, source_dir, site_id=None, disambiguate_patids=False):
    """Maps PCORnet lds_address_history.csv into OMOP location and updates person.location_id."""
    path = find_source_file(source_dir, "lds_address_history")
    if path is None:
        path = find_source_file(source_dir, "address_history")
    if path is None:
        print("Skipping LOCATION - no lds_address_history.csv found (optional table)")
        return

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
                WHEN 'OS' THEN 9201 -- Observation Services
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
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('AM', 'AMA') THEN 4155309 -- Left against medical advice
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('HO', 'HOSPICE') THEN 8546 -- Hospice
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('SN', 'SNF') THEN 8863 -- Skilled Nursing Facility
                WHEN UPPER(TRIM(src.DISCHARGE_STATUS)) IN ('A', 'HOME', 'ROUTINE') THEN 8536 -- Home
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
            "VITAL_SOURCE", "HT", "WT", "ORIGINAL_BMI", "BMI", "SYSTOLIC", "DIASTOLIC"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

    run_insert(con, "MEASUREMENT (from VITAL)", f"""
        WITH unpivoted_vitals AS (
          SELECT
            VITALID || '_HT' AS vital_sub_id,
            PATID, ENCOUNTERID, MEASURE_DATE, MEASURE_TIME,
            '8302-2' AS loinc_code,
            TRY_CAST(HT AS DOUBLE) AS val_num,
            '[in_us]' AS unit_str,
            9326 AS unit_concept_id
          FROM _temp_vital
          WHERE HT IS NOT NULL AND TRY_CAST(HT AS DOUBLE) IS NOT NULL AND TRY_CAST(HT AS DOUBLE) > 0

          UNION ALL

          SELECT
            VITALID || '_WT' AS vital_sub_id,
            PATID, ENCOUNTERID, MEASURE_DATE, MEASURE_TIME,
            '29463-7' AS loinc_code,
            TRY_CAST(WT AS DOUBLE) AS val_num,
            '[lb_av]' AS unit_str,
            8739 AS unit_concept_id
          FROM _temp_vital
          WHERE WT IS NOT NULL AND TRY_CAST(WT AS DOUBLE) IS NOT NULL AND TRY_CAST(WT AS DOUBLE) > 0

          UNION ALL

          SELECT
            VITALID || '_BMI' AS vital_sub_id,
            PATID, ENCOUNTERID, MEASURE_DATE, MEASURE_TIME,
            '39156-5' AS loinc_code,
            TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE) AS val_num,
            'kg/m2' AS unit_str,
            9531 AS unit_concept_id
          FROM _temp_vital
          WHERE COALESCE(ORIGINAL_BMI, BMI) IS NOT NULL
            AND TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE) IS NOT NULL
            AND TRY_CAST(COALESCE(ORIGINAL_BMI, BMI) AS DOUBLE) > 0

          UNION ALL

          SELECT
            VITALID || '_SYSTOLIC' AS vital_sub_id,
            PATID, ENCOUNTERID, MEASURE_DATE, MEASURE_TIME,
            '8480-6' AS loinc_code,
            TRY_CAST(SYSTOLIC AS DOUBLE) AS val_num,
            'mm[Hg]' AS unit_str,
            8876 AS unit_concept_id
          FROM _temp_vital
          WHERE SYSTOLIC IS NOT NULL AND TRY_CAST(SYSTOLIC AS DOUBLE) IS NOT NULL AND TRY_CAST(SYSTOLIC AS DOUBLE) > 0

          UNION ALL

          SELECT
            VITALID || '_DIASTOLIC' AS vital_sub_id,
            PATID, ENCOUNTERID, MEASURE_DATE, MEASURE_TIME,
            '8462-4' AS loinc_code,
            TRY_CAST(DIASTOLIC AS DOUBLE) AS val_num,
            'mm[Hg]' AS unit_str,
            8876 AS unit_concept_id
          FROM _temp_vital
          WHERE DIASTOLIC IS NOT NULL AND TRY_CAST(DIASTOLIC AS DOUBLE) IS NOT NULL AND TRY_CAST(DIASTOLIC AS DOUBLE) > 0
        )
        INSERT INTO measurement
        SELECT
            ROW_NUMBER() OVER (ORDER BY src.vital_sub_id) + (SELECT COALESCE(MAX(measurement_id), 0) FROM measurement) AS measurement_id,
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
            src.val_num AS value_as_number,
            0 AS value_as_concept_id,
            src.unit_concept_id AS unit_concept_id,
            NULL AS range_low,
            NULL AS range_high,
            NULL AS provider_id,
            COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
            NULL AS visit_detail_id,
            src.loinc_code AS measurement_source_value,
            COALESCE(c.concept_id, 0) AS measurement_source_concept_id,
            src.unit_str AS unit_source_value,
            src.unit_concept_id AS unit_source_concept_id,
            CAST(src.val_num AS VARCHAR) AS value_source_value,
            NULL AS measurement_event_id,
            0 AS meas_event_field_concept_id
        FROM unpivoted_vitals src
        LEFT JOIN person p
          ON p.person_source_value = {patid_expr}
        LEFT JOIN visit_occurrence vo
          ON vo.visit_source_value = src.ENCOUNTERID
        LEFT JOIN concept c
          ON c.vocabulary_id = 'LOINC'
         AND c.concept_code = src.loinc_code
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
          ON stcm.source_code = src.loinc_code
        WHERE src.vital_sub_id IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY src.vital_sub_id) = 1;
    """)
    con.execute("DROP VIEW IF EXISTS _temp_vital;")


def load_drug_exposure(con, source_dir, site_id=None, disambiguate_patids=False):
    path = find_source_file(source_dir, "prescribing")
    if path is None:
        print("Skipping DRUG_EXPOSURE - no prescribing.csv found")
        return

    prepare_source_view(
        con,
        "_temp_prescribing",
        path,
        [
            "PRESCRIBINGID", "PATID", "RXNORM_CUI", "RX_START_DATE", "RX_END_DATE",
            "RAW_RX_MED_NAME", "RAW_RX_NDC", "PROVIDERID", "ENCOUNTERID"
        ],
    )

    patid_expr = f"src.PATID || '-{site_id}'" if (disambiguate_patids and site_id is not None) else "src.PATID"

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


def build_observation_period(con):
    """Synthesizes patient observation envelopes across all clinical event tables."""
    con.execute("DELETE FROM observation_period;")
    run_insert(con, "OBSERVATION_PERIOD (synthesized)", """
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
            GROUP BY person_id
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY person_id) AS observation_period_id,
            person_id,
            obs_start_date AS observation_period_start_date,
            obs_end_date AS observation_period_end_date,
            32827 AS period_type_concept_id -- EHR encounter record
        FROM envelopes;
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
):
    """Run full PCORnet -> OMOP CDM v5.4 pipeline."""
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    if disambiguate_patids and site_id is None:
        raise ValueError("disambiguate_patids requires site_id to be specified.")

    con = duckdb.connect(db_path)
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}';")
    if threads:
        con.execute(f"SET threads = {threads};")
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
        con.execute(f"SET temp_directory = '{temp_dir}';")

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

    build_observation_period(con)
    build_drug_era(con)
    build_condition_era(con)
    load_cdm_source(con, site_id=site_id, site_anon=site_anon, site_name=site_name)

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
    parser.add_argument("--site-id", type=int, default=None, help="Numeric site identifier (e.g. 1 to 5)")
    parser.add_argument("--site-anon", default=None, help="Publication pseudonym (e.g. 'Site A')")
    parser.add_argument("--site-name", default=None, help="Optional institutional name or internal descriptor (e.g. 'Hospital System A')")
    parser.add_argument("--disambiguate-patids", action="store_true", help="Suffix PATID with -<site_id> to disambiguate patient IDs across sites")
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
    )


if __name__ == "__main__":
    main()
