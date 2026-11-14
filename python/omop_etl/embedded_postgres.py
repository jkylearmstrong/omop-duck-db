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
    "load_text_files",
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
