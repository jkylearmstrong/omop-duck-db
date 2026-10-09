"""Vocabulary loading, inspection, and CPT4 sanitization for OMOP CDM in DuckDB."""

import os
import warnings

import duckdb

from .build_omop_cdm import _connection_is_read_only, load_macros

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


# --- omop_connect: session helper that makes an attached central vocabulary transparent ---------------

CENTRAL_VOCAB_ALIAS = "central_vocab"
# Looked up, in this order, in the directory that holds the primary database (nowhere else).
VOCAB_DB_FILENAMES = ("central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb")
# Same tables attach_central_vocabulary() exposes.
_CENTRAL_VOCAB_TABLES = (
    "concept", "concept_relationship", "concept_ancestor", "concept_synonym",
    "vocabulary", "relationship", "concept_class", "domain", "drug_strength",
)
_SEARCH_PATH = f"main,{CENTRAL_VOCAB_ALIAS}.main"


def _sql_string(value):
    """Single-quoted SQL string literal with embedded quotes escaped."""
    return "'" + str(value).replace("'", "''") + "'"


def _sql_identifier(name):
    """Double-quoted SQL identifier with embedded quotes escaped."""
    return '"' + str(name).replace('"', '""') + '"'


def _sql_path(path):
    """Absolute path as a SQL string literal; Windows backslashes become forward slashes."""
    absolute = os.path.abspath(path)
    if os.sep == "\\":
        absolute = absolute.replace("\\", "/")
    return _sql_string(absolute)


def _same_file(a, b):
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:  # one of the paths cannot be stat'ed: compare them textually
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _primary_database_file(con):
    """Absolute path of the connection's current database file, or None for an in-memory database."""
    row = con.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchone()
    return os.path.abspath(row[0]) if row and row[0] else None


def _attached_central_vocab(con):
    """None if no ``central_vocab`` database is attached, else its path ('' if it has none)."""
    row = con.execute(
        f"SELECT path FROM duckdb_databases() WHERE database_name = {_sql_string(CENTRAL_VOCAB_ALIAS)}"
    ).fetchone()
    return None if row is None else (row[0] or "")


def _discover_sibling_vocab(db_file, max_levels=4):
    """First of VOCAB_DB_FILENAMES that sits next to ``db_file`` or in up to ``max_levels``
    parent directories (never ``db_file`` itself), falling back to OMOP_CENTRAL_VOCAB."""
    if db_file:
        current = os.path.dirname(os.path.abspath(db_file))
        for _ in range(max_levels):
            for name in VOCAB_DB_FILENAMES:
                candidate = os.path.join(current, name)
                if os.path.isfile(candidate) and not _same_file(candidate, db_file):
                    return candidate
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    env_path = os.environ.get("OMOP_CENTRAL_VOCAB")
    if env_path and os.path.isfile(env_path):
        if not db_file or not _same_file(env_path, db_file):
            return os.path.abspath(env_path)
    return None


def _central_vocab_table_names(con):
    """Lower-case names of the vocabulary tables the attached ``central_vocab`` holds in ``main``."""
    names = ", ".join(_sql_string(t) for t in _CENTRAL_VOCAB_TABLES)
    return {r[0].lower() for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_catalog = {_sql_string(CENTRAL_VOCAB_ALIAS)} AND table_schema = 'main' "
        f"AND table_name IN ({names})"
    ).fetchall()}


def _expose_central_vocab_tables(con):
    """Let an *empty* local vocabulary table fall through to the attached vocabulary.

    A database built with ``build_schema()`` has empty local ``concept``, ``concept_ancestor``, ...
    tables. ``search_path`` puts ``main`` first, so they would shadow the attached vocabulary and every
    vocabulary query would silently return nothing. For each such empty table (and only those), a
    session-scoped TEMP view over ``central_vocab`` is created; temp objects are resolved first, and
    nothing is written to either database file. A populated local table, or any temp object of the
    same name (e.g. a view left by ``attach_central_vocabulary()``), is left alone.
    """
    primary = con.execute("SELECT current_database()").fetchone()[0]
    if primary == CENTRAL_VOCAB_ALIAS:
        return
    names = ", ".join(_sql_string(t) for t in _CENTRAL_VOCAB_TABLES)
    central = _central_vocab_table_names(con)
    local = {r[0].lower() for r in con.execute(
        "SELECT table_name FROM duckdb_tables() "
        f"WHERE database_name = {_sql_string(primary)} AND schema_name = 'main' AND table_name IN ({names})"
    ).fetchall()}
    session = {r[0].lower() for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'temp' "
        f"AND table_name IN ({names}) "
        "UNION ALL SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal "
        f"AND view_name IN ({names})"
    ).fetchall()}
    for table in _CENTRAL_VOCAB_TABLES:
        if table not in central or table not in local or table in session:
            continue
        ident = _sql_identifier(table)
        has_rows = con.execute(
            f"SELECT EXISTS (SELECT 1 FROM {_sql_identifier(primary)}.main.{ident})"
        ).fetchone()[0]
        if not has_rows:
            con.execute(
                f"CREATE OR REPLACE TEMP VIEW {ident} AS "
                f"SELECT * FROM {CENTRAL_VOCAB_ALIAS}.main.{ident}"
            )


def _has_vocabulary_rows(con):
    """Whether ``concept`` resolves (locally or through the search path) and holds at least one row."""
    try:
        return bool(con.execute("SELECT EXISTS (SELECT 1 FROM concept)").fetchone()[0])
    except duckdb.CatalogException:
        return False


def _has_vocabulary_tables(con):
    """Whether the primary or temp catalog holds any vocabulary table or view (even an empty or dangling one)."""
    names = ", ".join(_sql_string(t) for t in _CENTRAL_VOCAB_TABLES)
    return con.execute(
        "SELECT EXISTS (SELECT 1 FROM ("
        "SELECT database_name, table_name AS name FROM duckdb_tables() "
        "UNION ALL SELECT database_name, view_name FROM duckdb_views() WHERE NOT internal"
        f") WHERE lower(name) IN ({names}) AND database_name IN (current_database(), 'temp'))"
    ).fetchone()[0]


def _search_path_with_central_vocab(current):
    """``current`` search_path with ``central_vocab.main`` added; the default (empty) path becomes
    ``main,central_vocab.main`` and a path that already lists ``central_vocab`` is returned as is."""
    entries = [e.strip() for e in (current or "").split(",") if e.strip()]
    if not entries:
        return _SEARCH_PATH
    if any(e.replace('"', "").lower().split(".")[0] == CENTRAL_VOCAB_ALIAS for e in entries):
        return ",".join(entries)
    return ",".join(entries + [f"{CENTRAL_VOCAB_ALIAS}.main"])


def _configure_omop_connection(con, explicit_vocab, auto_attach_vocab, load_sql_macros):
    db_file = _primary_database_file(con)
    if explicit_vocab and _same_file(explicit_vocab, db_file):
        raise ValueError(
            f"vocab_db_path is the primary database itself ({explicit_vocab}). A database that already "
            "holds its own vocabulary needs no separate vocabulary database."
        )

    attached = _attached_central_vocab(con)
    if explicit_vocab:
        if attached is None:
            con.execute(f"ATTACH {_sql_path(explicit_vocab)} AS {CENTRAL_VOCAB_ALIAS} (READ_ONLY)")
        elif not _same_file(attached, explicit_vocab):
            raise ValueError(
                f"A different database is already attached as '{CENTRAL_VOCAB_ALIAS}' "
                f"({attached or 'no file path'}); cannot attach {explicit_vocab} under the same name."
            )
    elif attached is None and auto_attach_vocab:
        discovered = _discover_sibling_vocab(db_file)
        if discovered:
            con.execute(f"ATTACH {_sql_path(discovered)} AS {CENTRAL_VOCAB_ALIAS} (READ_ONLY)")

    attached = _attached_central_vocab(con)
    vocab_attached = attached is not None
    empty_vocab = False
    if vocab_attached:
        # A search_path the caller set on a connection passed in is kept, with central_vocab appended.
        current = con.execute("SELECT current_setting('search_path')").fetchone()[0]
        wanted = _search_path_with_central_vocab(current)
        if wanted != current:
            con.execute(f"SET search_path = {_sql_string(wanted)}")
        _expose_central_vocab_tables(con)
        empty_vocab = not _central_vocab_table_names(con)

    # Macros whose body DuckDB validates when it creates them (map_to_standard_concept_id(), ...) can only
    # be created while the tables and columns they read are visible; the rest are still created, and the
    # gap is reported below.
    skipped = load_macros(con, temporary=True, skip_unresolved=True) if load_sql_macros else []

    notes = []
    if empty_vocab:
        # Attached on purpose or found next to the database, but a file that holds no vocabulary tables
        # (an empty database, or some other database) would otherwise connect without a word.
        notes.append(
            f"The vocabulary database attached as '{CENTRAL_VOCAB_ALIAS}' ({attached or 'no file path'}) "
            f"contains none of the vocabulary tables ({', '.join(_CENTRAL_VOCAB_TABLES)}) in its main "
            "schema, so it provides no concepts. Check that vocab_db_path points at an Athena "
            "vocabulary database."
        )
    if not vocab_attached and auto_attach_vocab and not _has_vocabulary_rows(con):
        where = (
            f"next to {db_file}" if db_file
            else "(an in-memory database has no directory to look in)"
        )
        notes.append(
            f"No vocabulary available: no central vocabulary database ({', '.join(VOCAB_DB_FILENAMES)}) "
            f"was found {where}, and the database has no local vocabulary (`concept` is missing or "
            "empty). Pass vocab_db_path= to attach one, or auto_attach_vocab=False if none is needed."
        )
    if skipped:
        # Opting out of a vocabulary (auto_attach_vocab=False, none given) on a database with no vocabulary
        # tables at all makes the skipped vocabulary macros expected, so they are not worth a warning.
        opted_out = not vocab_attached and not auto_attach_vocab and not _has_vocabulary_tables(con)
        if not opted_out:
            notes.append(
                "These SQL macros were not created because the vocabulary tables or columns they read are "
                f"not available on this connection: {', '.join(skipped)}. Reconnect once a complete "
                "vocabulary is available."
            )
    if notes:
        warnings.warn("omop_connect: " + " ".join(notes), UserWarning, stacklevel=3)


def omop_connect(
    db_path,
    vocab_db_path=None,
    read_only=False,
    auto_attach_vocab=True,
    load_sql_macros=True,
):
    """Connect to an OMOP CDM DuckDB database with the central vocabulary attached transparently.

    The vocabulary database is attached read-only as ``central_vocab`` and the connection's
    ``search_path`` becomes ``'main,central_vocab.main'``, so ``concept``, ``concept_ancestor``,
    ``concept_relationship``, ... can be queried with no schema prefix while a 10M-concept Athena
    vocabulary is stored once instead of inside every site database. (A ``search_path`` you already
    set on a connection you pass in is kept, with ``central_vocab.main`` appended; see Notes.)

    Parameters
    ----------
    db_path : str, os.PathLike, or duckdb.DuckDBPyConnection
        Path of the primary OMOP DuckDB database (a leading ``~`` is expanded; ``":memory:"`` for an
        in-memory one), or an existing connection. A path that does not exist is created as an empty
        database unless ``read_only`` is True (then it is an error).
        A connection passed in is used as is and is **never closed** by this function, nor are its
        read-only/read-write mode or existing contents changed.
    vocab_db_path : str or os.PathLike, optional
        Vocabulary database to attach (a leading ``~`` is expanded). Must exist, otherwise
        ``FileNotFoundError`` is raised. When omitted and ``auto_attach_vocab`` is True,
        ``central_vocabulary.duckdb``, ``vocabulary.duckdb`` or ``vocab.duckdb`` is looked up (in that
        order) in the directory that holds the primary database -- nowhere else, and never the primary
        database itself.
    read_only : bool, default False
        Open the primary database read-only. An in-memory database cannot be read-only (it has no file
        to open and starts out empty), so ``read_only=True`` with ``":memory:"`` raises ``ValueError``.
        Ignored (with a warning if True) for an existing connection, whose mode cannot be changed.
    auto_attach_vocab : bool, default True
        Auto-discover a sibling vocabulary database when ``vocab_db_path`` is not given. An explicit
        ``vocab_db_path`` is always attached. If nothing is found and the database has no local
        vocabulary, a ``UserWarning`` explains how to supply one; the connection is still returned.
        A vocabulary database that is attached but holds none of the vocabulary tables (``concept``,
        ``concept_ancestor``, ...) also draws a ``UserWarning``, whether it was given or discovered.
        With ``False`` and no vocabulary tables in the database at all, the vocabulary macros are
        skipped without a warning, since you opted out (``load_sql_macros=False`` skips all macros).
    load_sql_macros : bool, default True
        Load the shared SQL macros (``descendants_of()``, ``ancestors_of()``,
        ``clamp_physiologic()``, ``map_to_standard_concept_id()``, the cohort macros, ...). They
        are created as session-scoped ``TEMP`` macros, so connecting never writes to a database
        file and also works on read-only databases. DuckDB validates the body of some macros when it
        creates them, so ``map_to_standard_concept_id()`` and ``source_concept_id()`` are created only
        if the vocabulary tables (with the columns they read) are visible at connect time; otherwise
        they are skipped and named in the warning. ``descendants_of()`` and ``ancestors_of()`` are
        always created and look ``concept_ancestor`` up when they are used, raising a catalog error
        if it is not visible then.

    Returns
    -------
    duckdb.DuckDBPyConnection
        The connection; when it was opened here, close it with ``con.close()``.

    Raises
    ------
    FileNotFoundError
        ``vocab_db_path`` (or, with ``read_only=True``, ``db_path``) does not exist.
    ValueError
        ``vocab_db_path`` is the primary database itself, a *different* database is already
        attached as ``central_vocab``, or ``read_only=True`` was combined with an in-memory
        database.

    Notes
    -----
    Calling it again, or on a connection that already has the same vocabulary attached as
    ``central_vocab``, is a no-op for the attachment.

    Search path: on a fresh connection (empty ``search_path``) it becomes ``'main,central_vocab.main'``.
    A ``search_path`` already set on a connection you pass in is not replaced: ``central_vocab.main``
    is appended to it (unless it already lists ``central_vocab``), so schemas you put first keep
    taking precedence.

    Vocabulary precedence is per table: a vocabulary table that holds rows in the primary database
    wins over the attached one. A local table that is merely *empty* -- as every vocabulary table is
    after ``build_schema()`` -- does not shadow the attached vocabulary: this function adds a
    session-only TEMP view over the central table for it (so ``INSERT`` into that table through
    this connection is refused; use a separate connection to load a local vocabulary).

    Per-connection state: ``search_path``, the session-only TEMP views and the TEMP macros belong to
    the connection this function configured; the attachment itself belongs to the database
    instance. A new connection to the same database -- notably ``con.cursor()``, which some DataFrame
    readers and thread pools call for you -- starts without any of them. On a database built with
    ``build_schema()`` such a cursor sees the *empty* local ``concept`` table rather than the central
    vocabulary and has no ``descendants_of()``, so a count of concepts there is silently zero. Pass the
    cursor (or any other new connection) through ``omop_connect(cursor)`` itself, or give every
    thread its own ``omop_connect(...)`` connection.

    Relation to :func:`attach_central_vocabulary`: that function is the ETL-time helper -- it
    attaches the vocabulary and creates a zero-copy view per vocabulary table (temporary, or
    persistent in the database), and fails if ``central_vocab`` is already attached.
    ``omop_connect`` is the analysis-time helper and recognises an attachment made earlier by
    ``attach_central_vocabulary`` (same file; no second ATTACH, its views are reused). Call
    ``attach_central_vocabulary`` first when using both on one connection, since the other order
    fails at its ATTACH.

    Examples
    --------
    >>> con = omop_connect("site_cdm.duckdb", vocab_db_path="central_vocabulary.duckdb",
    ...                    read_only=True)                                   # doctest: +SKIP
    >>> con.execute(
    ...     "SELECT COUNT(*) FROM condition_occurrence "
    ...     "WHERE condition_concept_id IN (SELECT concept_id FROM descendants_of(201826))"
    ... ).fetchone()    # 201826 = SNOMED 'Type 2 diabetes mellitus'         # doctest: +SKIP
    """
    explicit_vocab = None
    if vocab_db_path is not None:
        explicit_vocab = os.path.abspath(os.path.expanduser(os.fspath(vocab_db_path)))
        if not os.path.isfile(explicit_vocab):
            raise FileNotFoundError(f"Vocabulary database not found: {explicit_vocab}")

    owns_connection = not isinstance(db_path, duckdb.DuckDBPyConnection)
    if owns_connection:
        db_str = os.path.expanduser(os.fspath(db_path))
        if read_only and (db_str == "" or db_str.startswith(":memory:")):
            raise ValueError(
                "read_only=True cannot be combined with an in-memory database: it has no file to open "
                "read-only and starts out empty. Pass the path of a database file."
            )
        if read_only and not os.path.isfile(db_str):
            raise FileNotFoundError(f"Database file not found: {db_str}")
        con = duckdb.connect(db_str, read_only=read_only)
    else:
        con = db_path
        if read_only and not _connection_is_read_only(con):
            warnings.warn(
                "omop_connect: read_only=True has no effect on an existing connection that is open "
                "read-write; open it with duckdb.connect(path, read_only=True) instead.",
                UserWarning,
                stacklevel=2,
            )

    try:
        _configure_omop_connection(con, explicit_vocab, auto_attach_vocab, load_sql_macros)
    except BaseException:
        if owns_connection:
            con.close()
        raise
    return con
