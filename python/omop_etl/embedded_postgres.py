"""PostgreSQL inside Python, with no install and no admin rights, and DuckDB <-> PostgreSQL round trips of a site's OMOP CDM.

Install the optional extra::

    pip install "omop-duck-db[postgres-embedded]"

That brings ``pg0-embedded`` (MIT; ships the PostgreSQL server in the wheel for Windows x64, Linux x64 / arm64 with glibc 2.35 or
newer, and macOS), ``psycopg2-binary`` and SQLAlchemy. Nothing is installed system-wide and no service is registered: the server is a
child process that listens on ``127.0.0.1`` only, on a free port, with a password generated on first use and stored next to the data.

On Python 3.12+ the ``pgembed`` package (Apache-2.0, PostgreSQL 17, Unix socket instead of TCP, glibc 2.28 so it also runs on
RHEL 8) is an alternative: ``pip install pgembed`` and pass ``backend="pgembed"``.

PostgreSQL refuses to run as root (``initdb: cannot be run as root``), so start it as an ordinary user. Inside the Docker image, which
runs as root, use a server PostgreSQL instead.

Typical use::

    from omop_etl.embedded_postgres import EmbeddedPostgres, store_site, load_site, compare_duckdb

    with EmbeddedPostgres("derived/postgres") as pg:              # starts, or restarts, the server kept in that folder
        store_site(pg, "derived/omop_duckdb/omop_site1.duckdb", "site_a")   # DuckDB -> PostgreSQL, checked value by value
        load_site(pg, "site_a", "derived/from_postgres/omop_site1.duckdb")  # PostgreSQL -> DuckDB
    compare_duckdb("derived/omop_duckdb/omop_site1.duckdb", "derived/from_postgres/omop_site1.duckdb")  # exact, table by table

:func:`load_text_files` loads raw delimited source files (PCORnet extracts) straight into PostgreSQL with its own ``COPY`` reader and
checks every value against DuckDB reading the same files, so two independent readers agree on the raw data.

:func:`backup_site` / :func:`backup_raw_files` keep a site's copy as a backup (staged and swapped in only after it passes its check, so a
refresh never damages the previous copy), :func:`verify_backup` re-checks it against fingerprints recorded at backup time, and
:func:`restore_site` / :func:`restore_raw_files` rebuild the files from it. ``scripts/postgres_backup.py`` does this for every site.

Schema names are what you pass; use anonymous ones (``site_a``, see :func:`site_schema`) if the database or its reports will be shared.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

import duckdb
import pandas as pd

__all__ = [
    "EmbeddedPostgres",
    "RoundTripError",
    "site_schema",
    "list_site_schemas",
    "store_site",
    "load_site",
    "compare_duckdb",
    "sample_site",
    "load_text_files",
    "BACKUP_SCHEMA",
    "backup_site",
    "backup_raw_files",
    "register_backup",
    "backup_status",
    "verify_backup",
    "restore_site",
    "restore_raw_files",
    "drop_backup",
]

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_BACKENDS = ("pg0", "pgembed")


class RoundTripError(AssertionError):
    """Raised when data read back from PostgreSQL (or DuckDB) differs from what was written."""

    def __init__(self, message: str, report: pd.DataFrame | None = None):
        super().__init__(message)
        self.report = report


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _missing(pkg: str) -> ImportError:
    return ImportError(f"Embedded PostgreSQL needs the {pkg!r} package: pip install \"omop-duck-db[postgres-embedded]\""
                       + (" (pgembed: pip install pgembed, Python 3.12+)" if pkg == "pgembed" else ""))


def site_schema(label: str) -> str:
    """A PostgreSQL schema name for a site label: ``"Site A"`` -> ``"site_a"``. Raises for labels that give no usable name."""
    s = re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower()).strip("_")
    if s and s[0].isdigit():
        s = "s_" + s
    if not _IDENT.match(s or ""):
        raise ValueError(f"Cannot make a schema name from {label!r}.")
    return s


def _check_ident(name: str, what: str = "schema") -> str:
    if not _IDENT.match(str(name)):
        raise ValueError(f"{what} {name!r}: use lower-case letters, digits and _ (start with a letter), at most 63 characters, "
                         f"for example site_schema('Site A') -> 'site_a'.")
    return str(name)


class EmbeddedPostgres:
    """A PostgreSQL server kept in ``root`` and run as a child process of this Python session.

    Args:
        root: Folder for the server's data and its generated password (default ``~/.omop-duck-db/postgres``). Data persists between
            runs; :meth:`drop` deletes it.
        backend: ``"pg0"`` (``pg0-embedded``), ``"pgembed"``, or ``"auto"`` (pg0 if installed, else pgembed).
        database: Database to create and connect to.
        port: TCP port (pg0 only; default a free port chosen at each start).
        stop_on_exit: Stop the server when the ``with`` block ends (default True). The data stays.

    Properties once started: :attr:`url` (SQLAlchemy URL, for ``to_sql`` / ``import_omop`` / ``pandas``), :attr:`dsn` (libpq URI, for
    ``psycopg2.connect`` or DuckDB's ``ATTACH ... (TYPE postgres)``). The password is in both; do not print or log them.
    """

    def __init__(self, root: str | os.PathLike | None = None, *, backend: str = "auto", database: str = "omop", port: int | None = None,
                 stop_on_exit: bool = True):
        self.root = Path(root) if root is not None else Path.home() / ".omop-duck-db" / "postgres"
        self.root = self.root.expanduser().resolve()
        if backend not in ("auto", *_BACKENDS):
            raise ValueError(f"backend must be 'auto', 'pg0' or 'pgembed', not {backend!r}.")
        self.backend = self._pick(backend)
        self.database = _check_ident(database, "database")
        self.port = port
        self.stop_on_exit = stop_on_exit
        self._server: Any = None
        self._dsn: str | None = None

    @staticmethod
    def _pick(backend: str) -> str:
        import importlib.util

        if backend != "auto":
            mod = "pg0" if backend == "pg0" else "pgembed"
            if importlib.util.find_spec(mod) is None:
                raise _missing("pg0-embedded" if backend == "pg0" else "pgembed")
            return backend
        for b, mod in (("pg0", "pg0"), ("pgembed", "pgembed")):
            if importlib.util.find_spec(mod) is not None:
                return b
        raise _missing("pg0-embedded")

    # -- credentials ---------------------------------------------------------------------------------------------------------------
    def _password(self) -> str:
        f = self.root / "credentials.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))["password"]
        self.root.mkdir(parents=True, exist_ok=True)
        pw = secrets.token_hex(24)  # hex: a urlsafe token can start with "-", which pg0 reads as a command-line option
        f.write_text(json.dumps({"password": pw, "note": "generated by omop_etl.embedded_postgres; local server only"}), encoding="utf-8")
        try:
            os.chmod(f, 0o600)
        except OSError:  # pragma: no cover - Windows ACLs
            pass
        return pw

    @property
    def _instance(self) -> str:
        return "omop-" + hashlib.sha1(str(self.root).encode("utf-8")).hexdigest()[:12]  # pg0 names instances globally

    # -- lifecycle -----------------------------------------------------------------------------------------------------------------
    def start(self) -> "EmbeddedPostgres":
        """Starts the server (initialising ``root`` on first use) and creates :attr:`database`. Returns self."""
        if self._dsn:
            return self
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise RuntimeError("PostgreSQL refuses to run as root. Start Python as an ordinary user, or use a server PostgreSQL.")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.backend == "pg0":
            from pg0 import Pg0

            pw = self._password()
            self._server = Pg0(name=self._instance, port=self.port or _free_port(), username="postgres", password=pw,
                               database=self.database, data_dir=str(self.root / "pgdata"),
                               config={"listen_addresses": "127.0.0.1"})
            try:
                info = self._server.start()
            except Exception as exc:
                if "already running" not in str(exc).lower():
                    raise
                info = self._server.info()
            self._dsn = info.uri
            self._ensure_database()
        else:
            import pgembed

            self._server = pgembed.get_server(self.root / "pgdata", cleanup_mode="stop")
            self._dsn = None
            self._ensure_database()
        return self

    def _ensure_database(self) -> None:
        import psycopg2

        admin = self._server.get_uri("postgres") if self.backend == "pgembed" else self._dsn
        conn = psycopg2.connect(admin)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (self.database,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{self.database}"')
        finally:
            conn.close()
        if self.backend == "pgembed":
            self._dsn = self._server.get_uri(self.database)
        else:
            self._dsn = _with_database(self._dsn, self.database)

    def stop(self) -> None:
        """Stops the server; the data in ``root`` stays."""
        if self._server is None:
            return
        if self.backend == "pg0":
            self._server.stop()
        else:
            self._server.cleanup()
        self._server = None
        self._dsn = None

    def drop(self) -> None:
        """Stops the server and deletes its data and password (``root/pgdata`` and ``root/credentials.json``)."""
        import shutil

        if self.backend == "pg0":
            from pg0 import Pg0

            (self._server or Pg0(name=self._instance)).drop()
            self._server = None
            self._dsn = None
        else:
            self.stop()
        shutil.rmtree(self.root / "pgdata", ignore_errors=True)
        (self.root / "credentials.json").unlink(missing_ok=True)

    def __enter__(self) -> "EmbeddedPostgres":
        return self.start()

    def __exit__(self, *exc) -> None:
        if self.stop_on_exit:
            self.stop()

    # -- connections ---------------------------------------------------------------------------------------------------------------
    @property
    def dsn(self) -> str:
        """libpq connection URI (``postgresql://...``)."""
        if not self._dsn:
            raise RuntimeError("The server is not running; call start() or use 'with EmbeddedPostgres(...) as pg'.")
        return self._dsn

    @property
    def url(self) -> str:
        """SQLAlchemy URL (``postgresql+psycopg2://...``)."""
        return "postgresql+psycopg2://" + self.dsn.split("://", 1)[1]

    def connect(self):
        """A new ``psycopg2`` connection (caller closes it)."""
        import psycopg2

        return psycopg2.connect(self.dsn)

    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        """Runs one query and returns the rows as a DataFrame."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cur.description else []
            conn.commit()
        finally:
            conn.close()
        return pd.DataFrame(rows, columns=cols)

    def attach_to(self, con: duckdb.DuckDBPyConnection, alias: str = "pg", *, read_only: bool = True) -> None:
        """Attaches this database to a DuckDB connection with DuckDB's ``postgres`` extension (downloaded by DuckDB on first use)."""
        _check_ident(alias, "alias")
        con.execute("INSTALL postgres; LOAD postgres;")
        ro = ", READ_ONLY" if read_only else ""
        con.execute(f"ATTACH '{_libpq_kv(self.dsn)}' AS {alias} (TYPE postgres{ro})")

    def __repr__(self) -> str:  # never shows the password
        state = "running" if self._dsn else "stopped"
        return f"EmbeddedPostgres(root={str(self.root)!r}, backend={self.backend!r}, database={self.database!r}, {state})"


def _with_database(uri: str, database: str) -> str:
    head, sep, query = uri.partition("?")
    base = head.rsplit("/", 1)[0] if head.count("/") >= 3 else head
    return f"{base}/{database}" + (sep + query if sep else "")


def _libpq_kv(uri: str) -> str:
    """URI -> 'host=... port=... dbname=... user=... password=...' (DuckDB's ATTACH takes either; this survives a socket host)."""
    from urllib.parse import parse_qs, unquote, urlsplit

    u = urlsplit(uri)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    parts = {"host": q.get("host") or u.hostname, "port": q.get("port") or u.port, "dbname": unquote(u.path.lstrip("/")) or None,
             "user": unquote(u.username) if u.username else None, "password": unquote(u.password) if u.password else None}
    return " ".join(f"{k}={v}" for k, v in parts.items() if v not in (None, ""))


def _pg(target) -> tuple[str, str]:
    """(SQLAlchemy URL, libpq dsn) from an EmbeddedPostgres or a URL."""
    if isinstance(target, EmbeddedPostgres):
        return target.url, target.dsn
    url = str(target)
    from omop_etl.extract import resolve_url

    real = resolve_url(url)
    return real, re.sub(r"^postgresql\+\w+://", "postgresql://", real)


def list_site_schemas(pg) -> list[str]:
    """Schemas in the database that hold a ``person`` table (one per stored site)."""
    import psycopg2

    _, dsn = _pg(pg)
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_schema FROM information_schema.tables WHERE table_name = 'person' ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _duck(db) -> tuple[duckdb.DuckDBPyConnection, bool]:
    if isinstance(db, duckdb.DuckDBPyConnection):
        return db, False
    if not Path(db).exists():
        raise FileNotFoundError(f"DuckDB file not found: {db}")
    return duckdb.connect(str(db), read_only=True), True


def _base_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
        "AND table_catalog = current_database() ORDER BY 1").fetchall()]


def _copy_in(dsn: str, schema: str, table: str, con: duckdb.DuckDBPyConnection, select_sql: str, columns: list[str]) -> int:
    """Streams a DuckDB query into an existing PostgreSQL table with COPY (CSV via a temporary file)."""
    import psycopg2

    with tempfile.TemporaryDirectory(prefix="omop_pg_") as tmp:
        path = Path(tmp) / "t.csv"
        con.execute(f"COPY ({select_sql}) TO '{path.as_posix()}' (FORMAT csv, HEADER false, QUOTE '\"', FORCE_QUOTE *, NULLSTR '')")
        # FORCE_QUOTE quotes every non-NULL value, so PostgreSQL reads an unquoted empty field as NULL and "" as an empty string
        cols = ", ".join(f'"{c}"' for c in columns)
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur, open(path, "r", encoding="utf-8", newline="") as fh:
                cur.copy_expert(f'COPY "{schema}"."{table}" ({cols}) FROM STDIN WITH (FORMAT csv, HEADER false)', fh)
                n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    return n


def _pg_cast(duck_type: str, col: str) -> str:
    t = duck_type.upper()
    q = f'"{col}"'
    if t == "BOOLEAN":
        return f"CAST({q} AS INTEGER) AS {q}"  # to_sql stores booleans as SMALLINT (portable across servers)
    if t in ("DOUBLE", "FLOAT", "REAL"):     # PostgreSQL reads NaN / Infinity / -Infinity, not DuckDB's nan / inf
        return (f"CASE WHEN isnan({q}) THEN 'NaN' WHEN {q} = 'inf'::DOUBLE THEN 'Infinity' WHEN {q} = '-inf'::DOUBLE THEN '-Infinity' "
                f"ELSE CAST({q} AS VARCHAR) END AS {q}")
    return q


def store_site(pg, duckdb_path, schema: str, *, tables: Sequence[str] | str = "all", if_exists: str = "replace", verify: bool = True,
               content: bool = True, method: str = "copy", varchar_length: int = 4000) -> pd.DataFrame:
    """Copies a site's DuckDB tables into one PostgreSQL schema and checks the copy.

    Args:
        pg: :class:`EmbeddedPostgres` (started) or a SQLAlchemy URL / ``env:NAME`` of any PostgreSQL server.
        duckdb_path: DuckDB file (opened read-only) or an open connection.
        schema: Target schema (created if missing), e.g. ``site_schema("Site A")``.
        tables: ``"all"`` (every base table in the file: the CDM, vocabulary and any study tables), ``"cdm"``
            (:data:`omop_etl.extract.CDM_STUDY_TABLES` that exist) or a list of names.
        if_exists: ``"replace"`` (default), ``"fail"`` or ``"append"``, as in :func:`omop_etl.load_sql.to_sql`.
        verify: Run :func:`omop_etl.load_sql.verify_transfer` afterwards and raise :class:`RoundTripError` on any mismatch.
        content: Also compare text, date, timestamp and boolean columns value by value (a full scan; default True).
        method: ``"copy"`` (PostgreSQL ``COPY``, roughly ten times faster) or ``"insert"`` (``to_sql`` batches, as for Oracle).

    Returns the per-column check (``verify_transfer`` output plus a ``table_rows`` / ``seconds`` summary in ``attrs["load"]``).
    """
    from omop_etl.extract import CDM_STUDY_TABLES, _make_engine
    from omop_etl.load_sql import to_sql, verify_transfer

    _check_ident(schema)
    if method not in ("copy", "insert"):
        raise ValueError("method must be 'copy' or 'insert'.")
    url, dsn = _pg(pg)
    con, close = _duck(duckdb_path)
    try:
        have = _base_tables(con)
        if tables == "all":
            names = have
        elif tables == "cdm":
            names = [t for t in CDM_STUDY_TABLES if t in have]
        else:
            names = list(tables)
            missing = [t for t in names if t not in have]
            if missing:
                raise LookupError(f"Not in the DuckDB file: {missing}")
        bad = [t for t in names if not _IDENT.match(t)]
        if bad:
            raise ValueError(f"Table names PostgreSQL would need quoting for: {bad}")
        sa = __import__("sqlalchemy")
        engine = _make_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        finally:
            engine.dispose()
        load = []
        for t in names:
            t0 = time.time()
            if method == "insert":
                r = to_sql(con, [t], url, schema=schema, if_exists=if_exists, varchar_length=varchar_length)[0]
                n = r["rows"]
            else:
                types = {r[0]: r[1] for r in con.execute(f'DESCRIBE SELECT * FROM "{t}"').fetchall()}
                to_sql(con, {t: f'SELECT * FROM "{t}" LIMIT 0'}, url, schema=schema, if_exists=if_exists, varchar_length=varchar_length)
                sel = f'SELECT {", ".join(_pg_cast(ty, c) for c, ty in types.items())} FROM "{t}"'
                n = _copy_in(dsn, schema, t, con, sel, list(types))
            load.append({"table": t, "rows": n, "seconds": round(time.time() - t0, 2)})
        report = verify_transfer(con, names, url, schema=schema, content=content) if verify and names else pd.DataFrame()
    finally:
        if close:
            con.close()
    report.attrs["load"] = load
    if verify and len(report) and not bool(report["match"].all()):
        bad = report.loc[~report["match"], ["table", "column"]].drop_duplicates()
        raise RoundTripError(f"PostgreSQL copy differs from DuckDB in {len(bad)} column(s): "
                             + ", ".join(f"{a}.{b}" for a, b in bad.itertuples(index=False)), report)
    return report


def load_site(pg, schema: str, out, *, tables: Sequence[str] | str = "all", chunksize: int = 500_000) -> list[dict[str, Any]]:
    """Copies a stored site back from PostgreSQL into a new DuckDB file.

    CDM tables are created with the package's CDM v5.4 DDL (:func:`omop_etl.extract.import_omop`), so their DuckDB types are the
    usual ones. Other tables (study tables, vocabulary extras) are created from the PostgreSQL column types. ``tables``: ``"all"``
    (every table in the schema), ``"cdm"`` or a list. ``out`` must not exist yet.
    """
    from omop_etl.extract import CDM_STUDY_TABLES, CDM_VOCAB_TABLES, from_sql, import_omop

    _check_ident(schema)
    url, dsn = _pg(pg)
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} exists; choose a new file so nothing is overwritten.")
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1",
                        (schema,))
            have = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    if not have:
        raise LookupError(f"Schema {schema!r} has no tables.")
    wanted = have if tables == "all" else ([t for t in CDM_STUDY_TABLES if t in have] if tables == "cdm" else list(tables))
    cdm_known = set(CDM_STUDY_TABLES) | set(CDM_VOCAB_TABLES)
    cdm = [t for t in wanted if t in cdm_known]
    other = [t for t in wanted if t not in cdm_known]
    report: list[dict[str, Any]] = []
    if "person" in cdm:
        report += import_omop(url, out, schema=schema, tables=cdm, chunksize=chunksize)
    else:
        other = cdm + other
    if other:
        with tempfile.TemporaryDirectory(prefix="omop_pg_load_") as tmp:
            items = from_sql(url, other, tmp, chunksize=chunksize, schema=schema)
            con = duckdb.connect(str(out))
            try:
                for it in items:
                    t = it["table"]
                    con.execute(f'CREATE OR REPLACE TABLE "{t}" AS SELECT * FROM read_parquet(\'{Path(it["file"]).as_posix()}\')')
                    report.append({"table": t, "rows": it["rows"]})
            finally:
                con.close()
    return report


def sample_site(src, dest, *, rows: int = 10_000, tables: Sequence[str] | str = "all", seed: int = 42) -> list[dict[str, Any]]:
    """Writes a small copy of a site's DuckDB file for a quick functional round trip.

    Each base table keeps its column types and at most ``rows`` rows: the whole table when it is that small, otherwise a repeatable
    reservoir sample (``seed``). Every column type, NULL handling and text encoding still goes through PostgreSQL, but the copy takes
    seconds instead of hours, because the round trip tests the package's code, not the size of the data. ``tables``: ``"all"`` or
    ``"cdm"`` (every base table; :func:`store_site` filters) or a list. ``dest`` must not exist. Returns ``table`` / ``rows`` /
    ``sampled`` per table; keep it private (it has row counts).
    """
    if rows < 1:
        raise ValueError("rows must be at least 1.")
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"Sample file already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(dest))
    out = []
    try:
        con.execute(f"ATTACH '{Path(src).as_posix()}' AS src (READ_ONLY)")
        have = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'src' AND table_schema = 'main' "
            "AND table_type = 'BASE TABLE' ORDER BY 1").fetchall()]
        names = have if tables in ("all", "cdm") else list(tables)
        missing = [t for t in names if t not in have]
        if missing:
            raise LookupError(f"Not in the DuckDB file: {missing}")
        for t in names:
            n = con.execute(f'SELECT count(*) FROM src."{t}"').fetchone()[0]
            sampled = n > rows
            pick = f" USING SAMPLE reservoir({int(rows)} ROWS) REPEATABLE ({int(seed)})" if sampled else ""
            con.execute(f'CREATE TABLE main."{t}" AS SELECT * FROM src."{t}"{pick}')
            out.append({"table": t, "rows": min(n, rows), "sampled": sampled})
    finally:
        con.close()
    return out


def compare_duckdb(a, b, *, tables: Sequence[str] | None = None, ignore_empty_extra: bool = True) -> pd.DataFrame:
    """Compares two DuckDB files table by table, row for row (multiset ``EXCEPT ALL`` both ways after casting ``b`` to ``a``'s types).

    Returns one row per table: ``rows_a``, ``rows_b``, ``only_in_a``, ``only_in_b``, ``match``. Tables only in ``b`` that are empty
    (the CDM DDL creates every table) are ignored when ``ignore_empty_extra``. Raises nothing; check ``match``. Row counts are in the
    result, so keep it with the site's private files.
    """
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{Path(a).as_posix()}' AS da (READ_ONLY)")
        con.execute(f"ATTACH '{Path(b).as_posix()}' AS db (READ_ONLY)")

        def tabs(alias):
            return [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog = ? AND table_schema = 'main' "
                "AND table_type = 'BASE TABLE' ORDER BY 1", [alias]).fetchall()]

        ta, tb = tabs("da"), tabs("db")
        names = list(tables) if tables else sorted(set(ta) | set(tb))
        out = []
        for t in names:
            ra = con.execute(f'SELECT count(*) FROM da."{t}"').fetchone()[0] if t in ta else None
            rb = con.execute(f'SELECT count(*) FROM db."{t}"').fetchone()[0] if t in tb else None
            if ra is None or rb is None:
                if ignore_empty_extra and ra is None and rb == 0:
                    continue
                out.append({"table": t, "rows_a": ra, "rows_b": rb, "only_in_a": None, "only_in_b": None, "match": False,
                            "note": "missing in " + ("a" if ra is None else "b")})
                continue
            cols_a = con.execute(f'DESCRIBE da."{t}"').fetchall()
            cols_b = {r[0].lower() for r in con.execute(f'DESCRIBE db."{t}"').fetchall()}
            missing = [c for c, *_ in cols_a if c.lower() not in cols_b]
            if missing:
                out.append({"table": t, "rows_a": ra, "rows_b": rb, "only_in_a": None, "only_in_b": None, "match": False,
                            "note": f"columns missing in b: {missing}"})
                continue
            sel_a = ", ".join(f'"{c}"' for c, *_ in cols_a)
            sel_b = ", ".join(f'CAST("{c}" AS {ty}) AS "{c}"' for c, ty, *_ in cols_a)
            oa = con.execute(f'SELECT count(*) FROM (SELECT {sel_a} FROM da."{t}" EXCEPT ALL SELECT {sel_b} FROM db."{t}")').fetchone()[0]
            ob = con.execute(f'SELECT count(*) FROM (SELECT {sel_b} FROM db."{t}" EXCEPT ALL SELECT {sel_a} FROM da."{t}")').fetchone()[0]
            out.append({"table": t, "rows_a": ra, "rows_b": rb, "only_in_a": oa, "only_in_b": ob, "match": ra == rb and oa == 0 and ob == 0,
                        "note": ""})
    finally:
        con.close()
    return pd.DataFrame(out, columns=["table", "rows_a", "rows_b", "only_in_a", "only_in_b", "match", "note"])


def _sniff(con: duckdb.DuckDBPyConnection, path: Path) -> dict[str, str]:
    row = con.execute("SELECT Delimiter, Quote, Escape FROM sniff_csv(?)", [path.as_posix()]).fetchone()

    def one(v, default):  # the sniffer reports "(empty)" when a file has no quoted field; both readers then use the CSV default
        return v if isinstance(v, str) and len(v) == 1 else default

    delim = row[0] if isinstance(row[0], str) and row[0] not in ("", "(empty)") else ","
    if len(delim) != 1:
        raise ValueError(f"{path.name}: multi-character delimiter {delim!r} is not supported by PostgreSQL COPY.")
    return {"delim": delim, "quote": one(row[1], '"'), "escape": one(row[2], '"')}


def _header(path: Path, delim: str, quote: str) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        head = next(csv.reader(fh, delimiter=delim, quotechar=quote))
    cols, seen = [], {}
    for i, h in enumerate(head):
        c = re.sub(r"[^a-z0-9_]+", "_", h.strip().lower()).strip("_") or f"column{i}"
        if c[0].isdigit():
            c = "c_" + c
        k = seen.get(c, 0)
        seen[c] = k + 1
        cols.append(c if k == 0 else f"{c}_{k}")
    return cols


def load_text_files(pg, folder, schema: str, *, pattern: str = "*.csv", verify: bool = True, prefix: str = "") -> pd.DataFrame:
    """Loads raw delimited text files into PostgreSQL with ``COPY`` and checks them against DuckDB reading the same files.

    Every column is stored as ``text`` (as the ETL reads sources). The delimiter and quote character are detected by DuckDB's CSV
    sniffer and given to both readers; files must be UTF-8 with a header row. With ``verify``, each table is compared with
    ``read_csv(..., all_varchar = true)`` by :func:`omop_etl.load_sql.verify_transfer` (row count, non-null counts and a value
    fingerprint per column), so the two independent parsers must agree on every value. Raises :class:`RoundTripError` if not.

    Returns the per-column check, with ``file`` -> table names in ``attrs["files"]`` (no row counts are printed).
    """
    import psycopg2

    from omop_etl.load_sql import verify_transfer

    _check_ident(schema)
    url, dsn = _pg(pg)
    files = sorted(p for p in Path(folder).glob(pattern) if p.is_file())
    if not files:
        raise FileNotFoundError(f"No files matching {pattern!r} in {folder}")
    duck = duckdb.connect()
    conn = psycopg2.connect(dsn)
    done: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.commit()
        for f in files:
            table = _check_ident(prefix + site_schema(f.stem), "table")
            fmt = _sniff(duck, f)
            cols = _header(f, fmt["delim"], fmt["quote"])
            col_sql = ", ".join(f'"{c}" text' for c in cols)
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}"')
                cur.execute(f'CREATE TABLE "{schema}"."{table}" ({col_sql})')
                opts = "FORMAT csv, HEADER true, DELIMITER %s, QUOTE %s, ESCAPE %s, ENCODING 'UTF8'"
                copy_sql = cur.mogrify(f'COPY "{schema}"."{table}" FROM STDIN WITH ({opts})',
                                       (fmt["delim"], fmt["quote"], fmt["escape"])).decode()
                with open(f, "r", encoding="utf-8-sig", newline="") as fh:   # utf-8-sig drops a byte-order mark
                    cur.copy_expert(copy_sql, fh)
            conn.commit()
            done[str(f.name)] = table
        if not verify:
            return pd.DataFrame()
        parts = []
        for f in files:
            table = done[f.name]
            fmt = _sniff(duck, f)
            cols = _header(f, fmt["delim"], fmt["quote"])
            names = ", ".join(f"'{c}'" for c in cols)
            q = (f"SELECT * FROM read_csv('{f.as_posix()}', header = true, all_varchar = true, delim = '{fmt['delim']}', "
                 f"quote = '{fmt['quote']}', escape = '{fmt['escape']}', names = [{names}])")
            rep = verify_transfer(duck, {table: q}, url, schema=schema, content=True)
            rep.insert(0, "file", f.name)
            parts.append(rep)
    finally:
        conn.close()
        duck.close()
    report = pd.concat(parts, ignore_index=True)
    report.attrs["files"] = done
    if not bool(report["match"].all()):
        bad = report.loc[~report["match"], ["file", "column"]].drop_duplicates()
        raise RoundTripError("PostgreSQL COPY and DuckDB read_csv disagree on: " + ", ".join(f"{a}:{b}" for a, b in bad.itertuples(index=False)),
                             report)
    return report


# ====================================================================================================================== backups
# A PostgreSQL copy kept on purpose: each site's DuckDB file (and, if wanted, its raw text files) in one schema, with a manifest in
# schema ``omop_backup`` that records what was copied and a fingerprint of every table, so the copy can be checked later without the
# original, refreshed safely (the old copy stays until the new one has passed its check) and restored into a new DuckDB file.

BACKUP_SCHEMA = "omop_backup"
_STAGE, _OLD = "__new", "__old"


def _connect(pg):
    import psycopg2

    return psycopg2.connect(_pg(pg)[1])


def _ensure_manifest(cur) -> None:
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{BACKUP_SCHEMA}"')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS "{BACKUP_SCHEMA}".backups (
        schema_name text PRIMARY KEY, kind text NOT NULL, label text, source_signature text, views jsonb NOT NULL DEFAULT '[]',
        created_at timestamptz NOT NULL, verified_at timestamptz, package_version text, note text, selection text NOT NULL DEFAULT 'all')''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS "{BACKUP_SCHEMA}".backup_tables (
        schema_name text NOT NULL, table_name text NOT NULL, n_rows bigint NOT NULL, fingerprint text NOT NULL,
        PRIMARY KEY (schema_name, table_name))''')


def _backup_name(schema: str) -> str:
    _check_ident(schema)
    if schema == BACKUP_SCHEMA or schema.endswith((_STAGE, _OLD)):
        raise ValueError(f"{schema!r} is reserved for the backup manifest or a backup in progress.")
    if len(schema) > 63 - len(_STAGE):
        raise ValueError(f"Backup schema names can have at most {63 - len(_STAGE)} characters.")
    return schema


def _schema_tables(cur, schema: str) -> list[str]:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY 1",
                (schema,))
    return [r[0] for r in cur.fetchall()]


def _fingerprints(cur, schema: str, tables: Sequence[str] | None = None) -> dict[str, tuple[int, str]]:
    """Per table (rows, fingerprint): the sum of the first 64 bits of md5 of each row's text, so order does not matter, duplicates
    count and any changed value changes it. Computed by PostgreSQL, so it checks the stored copy without the original."""
    out = {}
    for t in tables if tables is not None else _schema_tables(cur, schema):
        cur.execute(f"SELECT count(*), coalesce(sum(('x' || substr(md5(r::text), 1, 16))::bit(64)::bigint::numeric), 0)::text "
                    f'FROM "{schema}"."{t}" AS r')
        n, fp = cur.fetchone()
        out[t] = (int(n), fp)
    return out


def _package_version() -> str:
    try:
        from omop_etl import __version__

        return __version__
    except Exception:  # pragma: no cover
        return ""


def _duckdb_signature(path: Path, con: duckdb.DuckDBPyConnection) -> str:
    st = path.stat()
    counts = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in _base_tables(con)}
    blob = json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "tables": counts}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _files_signature(files: Sequence[Path]) -> str:
    blob = json.dumps([[f.name, f.stat().st_size, f.stat().st_mtime_ns] for f in files])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _duckdb_views(con: duckdb.DuckDBPyConnection) -> list[dict[str, str]]:
    return [{"name": n, "sql": s} for n, s in con.execute(
        "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal AND schema_name = 'main' "
        "AND database_name = current_database() ORDER BY 1").fetchall()]


def _other_schemas(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_catalog = current_database() "
        "AND table_schema NOT IN ('main', 'information_schema', 'pg_catalog') ORDER BY 1").fetchall()]


def _manifest_row(cur, schema: str):
    cur.execute(f'SELECT kind, label, source_signature, views, created_at, verified_at FROM "{BACKUP_SCHEMA}".backups '
                "WHERE schema_name = %s", (schema,))
    return cur.fetchone()


def _stored_signature(pg, schema: str) -> str | None:
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            if cur.fetchone()[0] is None:
                return None
            row = _manifest_row(cur, schema)
            return None if row is None else row[2]
    finally:
        conn.close()


def _drop_stage(pg, schema: str) -> None:
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}{_STAGE}" CASCADE')
        conn.commit()
    finally:
        conn.close()


def _swap_in(pg, schema: str, *, kind: str, label: str | None, signature: str, views: list[dict[str, str]], note: str = "",
             selection: str = "all") -> dict:
    """Makes the verified staging schema the backup, in one transaction: the old copy is replaced only if everything succeeds."""
    stage = schema + _STAGE
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            _ensure_manifest(cur)
            fps = _fingerprints(cur, stage)
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
            existed = cur.fetchone() is not None
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}{_OLD}" CASCADE')
            if existed:
                cur.execute(f'ALTER SCHEMA "{schema}" RENAME TO "{schema}{_OLD}"')
            cur.execute(f'ALTER SCHEMA "{stage}" RENAME TO "{schema}"')
            if existed:
                cur.execute(f'DROP SCHEMA "{schema}{_OLD}" CASCADE')
            cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s', (schema,))
            cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backups WHERE schema_name = %s', (schema,))
            cur.execute(f'INSERT INTO "{BACKUP_SCHEMA}".backups VALUES (%s, %s, %s, %s, %s, now(), now(), %s, %s, %s)',
                        (schema, kind, label, signature, json.dumps(views), _package_version(), note or None, selection))
            for t, (n, fp) in fps.items():
                cur.execute(f'INSERT INTO "{BACKUP_SCHEMA}".backup_tables VALUES (%s, %s, %s, %s)', (schema, t, n, fp))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"schema": schema, "status": "refreshed" if existed else "created", "tables": len(fps)}


def backup_site(pg, duckdb_path, schema: str, *, label: str | None = None, tables: Sequence[str] | str = "all", if_changed: bool = False,
                content: bool = True, method: str = "copy") -> pd.DataFrame:
    """Keeps a site's DuckDB file in PostgreSQL as a backup, or refreshes that backup.

    The tables are copied into a staging schema with :func:`store_site` and checked value by value. Only if the check passes does the
    staging schema replace ``schema`` (one transaction), and the manifest in schema ``omop_backup`` records the label, the DuckDB views
    (so :func:`restore_site` can recreate them), a signature of the source file and a fingerprint of every table. If anything fails,
    the previous backup is left as it was and :class:`RoundTripError` (or the original error) is raised.

    Args:
        pg: :class:`EmbeddedPostgres` (started) or a SQLAlchemy URL of any PostgreSQL server.
        duckdb_path: The site's DuckDB file (opened read-only).
        schema: Backup schema, e.g. ``site_schema("Site A")``.
        label: Anonymous site label to record (e.g. ``"Site A"``); never a real site name.
        tables: As in :func:`store_site`; ``"all"`` (default) keeps every table, so the backup is a full copy.
        if_changed: Skip the copy when the source file (size, modification time, tables and their row counts) is unchanged since the
            last backup; the result's ``status`` is then ``"unchanged"``.

    Returns the per-column check of :func:`store_site` (empty when unchanged), with ``attrs["backup"]`` = ``{"schema", "status",
    "tables", "skipped_schemas"}``. ``skipped_schemas`` lists DuckDB schemas other than ``main``, whose tables are not copied.
    """
    schema = _backup_name(schema)
    path = Path(duckdb_path)
    con, close = _duck(path)
    try:
        signature = _duckdb_signature(path, con)
        views = _duckdb_views(con)
        skipped = _other_schemas(con)
        if if_changed and _stored_signature(pg, schema) == signature:
            rep = pd.DataFrame()
            rep.attrs["load"] = []
            rep.attrs["backup"] = {"schema": schema, "status": "unchanged", "tables": None, "skipped_schemas": skipped}
            return rep
        _drop_stage(pg, schema)
        try:
            rep = store_site(pg, con, schema + _STAGE, tables=tables, content=content, method=method)
            info = _swap_in(pg, schema, kind="duckdb", label=label, signature=signature, views=views,
                            note=("not copied: DuckDB schemas " + ", ".join(skipped)) if skipped else "",
                            selection=tables if isinstance(tables, str) else "list")
        except BaseException:
            _drop_stage(pg, schema)
            raise
    finally:
        if close:
            con.close()
    rep.attrs["backup"] = {**info, "skipped_schemas": skipped}
    return rep


def backup_raw_files(pg, folder, schema: str, *, label: str | None = None, pattern: str = "*.csv", if_changed: bool = False) -> pd.DataFrame:
    """Keeps a site's raw text files in PostgreSQL as a backup (one text table per file), or refreshes it.

    Loads with :func:`load_text_files` into a staging schema (PostgreSQL ``COPY``, checked value by value against DuckDB's
    ``read_csv``), then swaps it in and records it in the manifest, as :func:`backup_site` does. ``if_changed`` skips the load when the
    files' names, sizes and modification times are unchanged. Use a schema such as ``"raw_" + site_schema(label)``.
    """
    schema = _backup_name(schema)
    files = sorted(p for p in Path(folder).glob(pattern) if p.is_file())
    if not files:
        raise FileNotFoundError(f"No files matching {pattern!r} in {folder}")
    signature = _files_signature(files)
    if if_changed and _stored_signature(pg, schema) == signature:
        rep = pd.DataFrame()
        rep.attrs["files"] = {}
        rep.attrs["backup"] = {"schema": schema, "status": "unchanged", "tables": None}
        return rep
    _drop_stage(pg, schema)
    try:
        rep = load_text_files(pg, folder, schema + _STAGE, pattern=pattern)
        info = _swap_in(pg, schema, kind="raw", label=label, signature=signature, views=[])
    except BaseException:
        _drop_stage(pg, schema)
        raise
    rep.attrs["backup"] = info
    return rep


def register_backup(pg, schema: str, duckdb_path, *, label: str | None = None, content: bool = True) -> pd.DataFrame:
    """Adopts a schema already stored with :func:`store_site` (for example by ``postgres_roundtrip_check.py``) as a backup, without
    copying again: checks it value by value against ``duckdb_path`` and, if it matches, records it in the manifest.

    Raises :class:`RoundTripError` if the schema differs from the DuckDB file (nothing is recorded then).
    """
    from omop_etl.load_sql import verify_transfer

    schema = _backup_name(schema)
    url, _ = _pg(pg)
    path = Path(duckdb_path)
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            stored = _schema_tables(cur, schema)
    finally:
        conn.close()
    if not stored:
        raise LookupError(f"Schema {schema!r} has no tables.")
    con, close = _duck(path)
    try:
        have = set(_base_tables(con))
        extra = [t for t in stored if t not in have]
        if extra:
            raise RoundTripError(f"{len(extra)} table(s) in {schema!r} are not in the DuckDB file: {extra}")
        rep = verify_transfer(con, stored, url, schema=schema, content=content)
        if not bool(rep["match"].all()):
            bad = rep.loc[~rep["match"], ["table", "column"]].drop_duplicates()
            raise RoundTripError(f"{schema!r} differs from the DuckDB file in {len(bad)} column(s).", rep)
        signature, views, skipped = _duckdb_signature(path, con), _duckdb_views(con), _other_schemas(con)
        missing = sorted(have - set(stored))
    finally:
        if close:
            con.close()
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            _ensure_manifest(cur)
            fps = _fingerprints(cur, schema)
            note = "; ".join(x for x in ((f"not copied: {len(missing)} table(s) of the DuckDB file" if missing else ""),
                                          ("not copied: DuckDB schemas " + ", ".join(skipped)) if skipped else "") if x)
            cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s', (schema,))
            cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backups WHERE schema_name = %s', (schema,))
            cur.execute(f'INSERT INTO "{BACKUP_SCHEMA}".backups VALUES (%s, %s, %s, %s, %s, now(), now(), %s, %s, %s)',
                        (schema, "duckdb", label, signature, json.dumps(views), _package_version(), note or None,
                         "partial" if missing else "all"))
            for t, (n, fp) in fps.items():
                cur.execute(f'INSERT INTO "{BACKUP_SCHEMA}".backup_tables VALUES (%s, %s, %s, %s)', (schema, t, n, fp))
        conn.commit()
    finally:
        conn.close()
    rep.attrs["backup"] = {"schema": schema, "status": "registered", "tables": len(fps), "missing_tables": missing}
    return rep


def backup_status(pg) -> pd.DataFrame:
    """One row per backup: ``schema``, ``kind`` (``duckdb`` / ``raw``), ``label``, ``tables``, ``views``, ``created_at``,
    ``verified_at``, ``package_version``, ``note``. No row counts, so it can be shown on screen."""
    cols = ["schema", "kind", "label", "tables", "views", "created_at", "verified_at", "package_version", "note"]
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            if cur.fetchone()[0] is None:
                return pd.DataFrame(columns=cols)
            cur.execute(f'''SELECT b.schema_name, b.kind, b.label, (SELECT count(*) FROM "{BACKUP_SCHEMA}".backup_tables t
                            WHERE t.schema_name = b.schema_name), jsonb_array_length(b.views), b.created_at, b.verified_at,
                            b.package_version, b.note FROM "{BACKUP_SCHEMA}".backups b ORDER BY 1''')
            return pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()


def verify_backup(pg, schema: str | None = None, *, against=None, content: bool = True) -> pd.DataFrame:
    """Checks backups (one ``schema``, or all when None) against the fingerprints recorded when they were made: every recorded table
    must still be there with the same rows, and no table may have been added. Needs only PostgreSQL.

    ``against``: also compare the backup value by value with a DuckDB file (duckdb backups) or a raw-file folder (raw backups, read
    with DuckDB's ``read_csv``), so it is checked against the current source too; this needs a single ``schema``. For a full backup
    (``tables="all"``) a table in the DuckDB file that is not in the backup is reported too.

    Returns one row per schema and table: ``schema``, ``table``, ``match``, ``note``; ``verified_at`` is updated for schemas that pass.
    Row counts are not in the result.
    """
    if against is not None and schema is None:
        raise ValueError("'against' needs a single schema.")
    out = []
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            if cur.fetchone()[0] is None:
                raise LookupError("No backups recorded in this database.")
            cur.execute(f'SELECT schema_name, kind, selection FROM "{BACKUP_SCHEMA}".backups' + (" WHERE schema_name = %s" if schema else "")
                        + " ORDER BY 1", (schema,) if schema else None)
            todo = cur.fetchall()
            if schema and not todo:
                raise LookupError(f"No backup recorded for schema {schema!r}.")
            for s, kind, selection in todo:
                cur.execute(f'SELECT table_name, n_rows, fingerprint FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s', (s,))
                want = {t: (n, fp) for t, n, fp in cur.fetchall()}
                now = _schema_tables(cur, s)
                got = _fingerprints(cur, s, [t for t in now if t in want])
                for t in sorted(set(want) | set(now)):
                    if t not in now:
                        out.append({"schema": s, "table": t, "match": False, "note": "table missing"})
                    elif t not in want:
                        out.append({"schema": s, "table": t, "match": False, "note": "table not in the backup record"})
                    elif got[t][0] != want[t][0]:
                        out.append({"schema": s, "table": t, "match": False, "note": "row count changed"})
                    elif got[t][1] != want[t][1]:
                        out.append({"schema": s, "table": t, "match": False, "note": "values changed"})
                    else:
                        out.append({"schema": s, "table": t, "match": True, "note": ""})
                if against is not None:
                    out += _verify_against(pg, s, kind, against, list(want), content, full=selection == "all")
        result = pd.DataFrame(out, columns=["schema", "table", "match", "note"])
        with conn.cursor() as cur:
            for s, *_ in todo:
                if bool(result.loc[result["schema"] == s, "match"].all()):
                    cur.execute(f'UPDATE "{BACKUP_SCHEMA}".backups SET verified_at = now() WHERE schema_name = %s', (s,))
        conn.commit()
    finally:
        conn.close()
    return result


def _verify_against(pg, schema: str, kind: str, source, tables: list[str], content: bool, full: bool) -> list[dict]:
    from omop_etl.load_sql import verify_transfer

    url, _ = _pg(pg)
    if kind == "duckdb":
        con, close = _duck(source)
        try:
            have = set(_base_tables(con))
            rows = [{"schema": schema, "table": t, "match": False, "note": "not in the DuckDB file"} for t in tables if t not in have]
            rows += [{"schema": schema, "table": t, "match": False, "note": "in the DuckDB file but not in the backup"}
                     for t in sorted(have - set(tables)) if full]
            names = [t for t in tables if t in have]
            rep = verify_transfer(con, names, url, schema=schema, content=content) if names else pd.DataFrame(columns=["table", "match"])
        finally:
            if close:
                con.close()
    else:
        files = {site_schema(p.stem): p for p in Path(source).iterdir() if p.is_file()}
        rows = [{"schema": schema, "table": t, "match": False, "note": "no source file"} for t in tables if t not in files]
        duck = duckdb.connect()
        try:
            spec = {}
            for t in tables:
                if t in files:
                    f = files[t]
                    fmt = _sniff(duck, f)
                    names = ", ".join(f"'{c}'" for c in _header(f, fmt["delim"], fmt["quote"]))
                    spec[t] = (f"SELECT * FROM read_csv('{f.as_posix()}', header = true, all_varchar = true, delim = '{fmt['delim']}', "
                               f"quote = '{fmt['quote']}', escape = '{fmt['escape']}', names = [{names}])")
            rep = verify_transfer(duck, spec, url, schema=schema, content=True) if spec else pd.DataFrame(columns=["table", "match"])
        finally:
            duck.close()
    for t, ok in rep.groupby("table")["match"].all().items():
        rows.append({"schema": schema, "table": t, "match": bool(ok), "note": "" if ok else "differs from the source"})
    return rows


def restore_site(pg, schema: str, out, *, verify: bool = True) -> pd.DataFrame:
    """Restores a DuckDB backup into a new DuckDB file ``out`` (which must not exist): every recorded table, then the recorded views.

    With ``verify`` the restored file is checked against the backup: each table's row count against the manifest, and every value
    against PostgreSQL (:func:`omop_etl.load_sql.verify_transfer` with ``content=True``). On a mismatch the file is kept for inspection
    and :class:`RoundTripError` is raised. Returns one row per table: ``table``, ``match``, ``note``.
    """
    from omop_etl.load_sql import verify_transfer

    schema = _backup_name(schema)
    url, _ = _pg(pg)
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            row = _manifest_row(cur, schema) if cur.fetchone()[0] is not None else None
            if row is None:
                raise LookupError(f"No backup recorded for schema {schema!r}.")
            if row[0] != "duckdb":
                raise ValueError(f"{schema!r} is a raw-file backup; use restore_raw_files.")
            views = row[3] if isinstance(row[3], list) else json.loads(row[3])
            cur.execute(f'SELECT table_name, n_rows FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s ORDER BY 1', (schema,))
            want = dict(cur.fetchall())
    finally:
        conn.close()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    load_site(pg, schema, out, tables=list(want))
    con = duckdb.connect(str(out))
    try:
        pending, failed_views = list(views), []
        while pending:                                    # views may use each other; create them in an order that works
            left = []
            for v in pending:
                try:
                    con.execute(v["sql"])
                except duckdb.Error:
                    left.append(v)
            if len(left) == len(pending):
                failed_views = [v["name"] for v in left]
                break
            pending = left
        res = []
        rep = verify_transfer(con, list(want), url, schema=schema, content=True) if verify else pd.DataFrame(columns=["table", "match"])
        ok_by_table = rep.groupby("table")["match"].all().to_dict()
        for t, n in want.items():
            got = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            if got != n:
                res.append({"table": t, "match": False, "note": "row count differs from the backup record"})
            elif verify and not ok_by_table.get(t, False):
                res.append({"table": t, "match": False, "note": "values differ from the backup"})
            else:
                res.append({"table": t, "match": True, "note": ""})
        for name in failed_views:
            res.append({"table": name, "match": False, "note": "view could not be recreated"})
    finally:
        con.close()
    result = pd.DataFrame(res, columns=["table", "match", "note"])
    if not bool(result["match"].all()):
        raise RoundTripError(f"Restored file differs from the backup in {int((~result['match']).sum())} table(s) or view(s).", result)
    return result


def restore_raw_files(pg, schema: str, out_dir) -> pd.DataFrame:
    """Writes each table of a raw-file backup to ``<out_dir>/<table>.csv`` (comma-delimited, header row, UTF-8; NULL as an empty
    field). Files that already exist are not overwritten. Returns one row per table: ``table``, ``file``, ``match`` (row count equals
    the backup record)."""
    schema = _backup_name(schema)
    out_dir = Path(out_dir)
    conn = _connect(pg)
    res = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            row = _manifest_row(cur, schema) if cur.fetchone()[0] is not None else None
            if row is None:
                raise LookupError(f"No backup recorded for schema {schema!r}.")
            cur.execute(f'SELECT table_name, n_rows FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s ORDER BY 1', (schema,))
            want = dict(cur.fetchall())
            clash = [t for t in want if (out_dir / f"{t}.csv").exists()]
            if clash:
                raise FileExistsError(f"{len(clash)} file(s) already exist in {out_dir}; choose an empty folder.")
            out_dir.mkdir(parents=True, exist_ok=True)
            for t, n in want.items():
                f = out_dir / f"{t}.csv"
                with open(f, "w", encoding="utf-8", newline="") as fh:
                    cur.copy_expert(f'COPY "{schema}"."{t}" TO STDOUT WITH (FORMAT csv, HEADER true)', fh)
                    got = cur.rowcount
                res.append({"table": t, "file": f.name, "match": got == n})
    finally:
        conn.close()
    return pd.DataFrame(res, columns=["table", "file", "match"])


def drop_backup(pg, schema: str) -> None:
    """Deletes a backup: its schema and its manifest entries."""
    schema = _backup_name(schema)
    conn = _connect(pg)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute("SELECT to_regclass(%s)", (f"{BACKUP_SCHEMA}.backups",))
            if cur.fetchone()[0] is not None:
                cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backup_tables WHERE schema_name = %s', (schema,))
                cur.execute(f'DELETE FROM "{BACKUP_SCHEMA}".backups WHERE schema_name = %s', (schema,))
        conn.commit()
    finally:
        conn.close()
