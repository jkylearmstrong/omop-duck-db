"""DuckDB -> server database (Oracle, PostgreSQL, SQL Server, Snowflake ... through SQLAlchemy) and a check that both sides agree (unreleased).

The other direction already exists: :func:`omop_etl.extract.import_omop` copies an OMOP CDM from a server into DuckDB and
:func:`omop_etl.extract.from_sql` streams server tables to Parquet. This module writes:

* :func:`to_sql`           - copies DuckDB tables, or the result of a query (a study feature table, a cohort), into a server schema in
  chunks: creates the table with portable column types (``NUMBER(19)``, ``VARCHAR2``, ``DATE``, ``TIMESTAMP`` on Oracle), upper-cases and
  length-checks identifiers on Oracle, and appends / replaces / refuses per ``if_exists``.
* :func:`verify_transfer`  - per table and column: row counts, non-null counts and, for numbers, sum / min / max on both sides (and,
  with ``content=True``, a value-level fingerprint of text, date, timestamp and boolean columns), so a round trip
  (DuckDB -> Oracle -> DuckDB) can be shown to be lossless.

Credentials come from a SQLAlchemy URL or ``env:NAME`` and never appear in messages. The offline tests run the same code against SQLite
and compile the Oracle DDL without a server. ``scripts/verify_server_roundtrip.py`` has been run against one live Oracle 19c server (see
``testing/tested-environments.md`` for what that does and does not show).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import time
import warnings
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.extract import _make_engine, _masked, _sa, _scrub, resolve_url

__all__ = ["to_sql", "verify_transfer"]

_ORACLE_MAX_IDENT = 30  # conservative: Oracle before 12.2 (and many sites' naming rules) allow 30 characters


def _sa_type(sa: Any, duck_type: str, dialect: str, varchar_len: int, unicode_text: bool = False):
    t = duck_type.upper()
    if t.startswith("DECIMAL"):
        inner = t[t.index("(") + 1:t.index(")")] if "(" in t else "38,9"
        p, s = [int(x) for x in inner.split(",")]
        return sa.Numeric(p, s)
    if t in ("BIGINT", "HUGEINT", "UBIGINT"):
        return sa.BigInteger()
    if t in ("INTEGER", "INT", "UINTEGER"):
        return sa.Integer()
    if t in ("SMALLINT", "TINYINT", "USMALLINT", "UTINYINT"):
        return sa.SmallInteger()
    if t in ("DOUBLE", "FLOAT", "REAL"):
        return sa.Float(53) if dialect != "oracle" else sa.dialects.oracle.BINARY_DOUBLE()
    if t == "BOOLEAN":
        return sa.SmallInteger()
    if t == "DATE":
        return sa.Date()
    if t.startswith("TIMESTAMP"):
        return sa.TIMESTAMP()
    if t == "VARCHAR" or t.startswith("VARCHAR"):
        if unicode_text:                              # NVARCHAR2 / NCLOB on Oracle: the national character set (usually Unicode)
            if dialect == "oracle":                   # explicit types: the generic Unicode type is VARCHAR2 unless the engine sets use_nchar_for_unicode
                return sa.dialects.oracle.NCLOB() if varchar_len > 2000 else sa.dialects.oracle.NVARCHAR2(varchar_len)
            return sa.UnicodeText() if varchar_len > 2000 else sa.Unicode(varchar_len)
        return sa.Text() if varchar_len > 4000 else sa.String(varchar_len)
    return sa.String(varchar_len)


def _server_name(name: str, dialect: str) -> str:
    if dialect == "oracle":
        up = name.upper()
        if len(up) > _ORACLE_MAX_IDENT:
            raise ValueError(f"{name!r} is {len(up)} characters; Oracle identifiers are limited to {_ORACLE_MAX_IDENT} here. Rename or shorten it.")
        return up
    return name


def _py(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (_dt.datetime, _dt.date, decimal.Decimal, int, float, str)) or v is None:
        return v
    return str(v)


def _source_sql(source: str) -> tuple[str, str]:
    s = source.strip()
    if s.lower().startswith(("select ", "with ", "from ")):
        return f"({s})", "query"
    return s, "table"


_MASK = (1 << 64) - 1


def _content_kind(duck_type: str) -> str | None:
    """Column kinds whose values :func:`verify_transfer` can compare by value (text, DATE, TIMESTAMP, BOOLEAN); others return None."""
    t = duck_type.upper()
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "text"
    return {"DATE": "date", "TIMESTAMP": "timestamp", "BOOLEAN": "bool"}.get(t)


def _norm(kind: str, v: Any, empty_is_null: bool) -> str | None:
    """One comparable string per value, the same whichever side it came from (Oracle returns a DATE as a datetime and a CLOB as a LOB)."""
    if v is None:
        return None
    if kind == "text":
        s = v.read() if hasattr(v, "read") else str(v)
        return None if (s == "" and empty_is_null) else s
    if kind == "date":
        if isinstance(v, str):                      # SQLite and some drivers return text for an untyped column
            return v[:10]
        return (v.date() if isinstance(v, _dt.datetime) else v).isoformat()
    if kind == "timestamp":
        if isinstance(v, str):
            v = _dt.datetime.fromisoformat(v)
        return v.replace(tzinfo=None).isoformat(timespec="microseconds")
    return str(bool(v))


def _column_values(fetchmany, kind: str, empty_is_null: bool, chunk: int = 50_000):
    while True:
        rows = fetchmany(chunk)
        if not rows:
            return
        for (v,) in rows:
            yield _norm(kind, v, empty_is_null)


def _fingerprint(values) -> tuple[int, int, int]:
    """Order-independent summary of a column: non-null count, sum and xor of the value hashes (valid within one process)."""
    n = total = x = 0
    for v in values:
        if v is None:
            continue
        h = hash(v) & _MASK
        n += 1
        total = (total + h) & _MASK
        x ^= h
    return n, total, x


_ORACLE_UNICODE = {"AL32UTF8", "UTF8", "AL16UTF16"}
_ORACLE_CODECS = {"WE8MSWIN1252": "cp1252", "WE8ISO8859P1": "latin-1", "WE8ISO8859P15": "iso8859-15", "US7ASCII": "ascii",
                  "EE8MSWIN1250": "cp1250", "CL8MSWIN1251": "cp1251"}


def _target_codec(engine: Any, dialect: str) -> tuple[str | None, str | None]:
    """(Python codec, Oracle character set) when the target database stores VARCHAR2 in a known non-Unicode character set, else (None, name).

    Anything that cannot be read (no privilege, other database) or is not in the table returns no codec, so no check is made."""
    if dialect != "oracle":
        return None, None
    sa = _sa()
    try:
        with engine.connect() as c:
            name = c.execute(sa.text("SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_CHARACTERSET'")).scalar()
    except Exception:
        return None, None
    name = str(name or "").upper()
    return (None if name in _ORACLE_UNICODE else _ORACLE_CODECS.get(name)), name or None


def _unencodable_message(table: str, bad: dict[str, int], charset: str | None, n: int) -> str:
    cols = ", ".join(f"{c} ({k} of {n} rows)" for c, k in bad.items())
    return (f"{table}: the database character set {charset} cannot store some text; the server would replace those characters. Affected columns: "
            f"{cols}. Pass nvarchar=True (or a list of column names) to create them as NVARCHAR2, or on_unencodable='ignore' to skip this check.")


def _count_unencodable(values, codec: str) -> int:
    n = 0
    for v in values:
        if isinstance(v, str):
            try:
                v.encode(codec)
            except UnicodeEncodeError:
                n += 1
    return n


def to_sql(con: duckdb.DuckDBPyConnection, tables: Sequence[str] | Mapping[str, str], url: str, *, schema: str | None = None,
           if_exists: str = "fail", chunksize: int = 50_000, varchar_length: int = 4000, columns: Mapping[str, Sequence[str]] | None = None,
           nvarchar: bool | Sequence[str] = False, on_unencodable: str = "warn") -> list[dict[str, Any]]:
    """Copies DuckDB tables or queries into a server schema.

    ``tables``: DuckDB table names (created under the same name on the server), or ``{server_table: "table or SELECT query"}`` to load a
    query result such as a feature table under another name. ``url``: SQLAlchemy URL or ``env:NAME`` (for Oracle
    ``oracle+oracledb://`` + user, password, host:1521 and ``?service_name=SVC``). ``if_exists``: ``"fail"`` (default; refuses an existing table),
    ``"replace"`` (drops and recreates), ``"append"``. ``columns``: optional ``{server_table: [column, ...]}`` subset. Oracle identifiers are
    upper-cased and limited to 30 characters; text columns become ``VARCHAR2(varchar_length)`` (``CLOB`` beyond 4000).

    ``VARCHAR2`` uses the database character set. If that is not Unicode (for example ``WE8MSWIN1252``), characters it cannot hold (Japanese,
    Cyrillic, many symbols) are silently replaced by the server, and counts and sums cannot show it. On Oracle, ``to_sql`` reads
    ``NLS_CHARACTERSET`` and, for a known non-Unicode set, counts the values that cannot be encoded: ``on_unencodable="warn"`` (default) emits a
    warning naming the columns and row counts (never the values), ``"error"`` refuses the load and rolls it back, ``"ignore"`` skips the check.
    ``nvarchar=True`` (all text columns) or a list of server column names creates those columns as ``NVARCHAR2`` / ``NCLOB``, which use the
    national character set (``AL16UTF16`` on most databases) and keep any text; ``NVARCHAR2`` is limited to 2000 characters, longer text becomes
    ``NCLOB``. ``verify_transfer(..., content=True)`` confirms the result.

    Returns one dict per table: ``table``, ``rows``, ``seconds``, ``ddl``, ``unencodable`` (``{column: rows}``, empty unless the check found some). Nothing is committed for a table whose load fails."""
    sa = _sa()
    if if_exists not in ("fail", "replace", "append"):
        raise ValueError("if_exists must be 'fail', 'replace' or 'append'.")
    if int(chunksize) < 1:
        raise ValueError("chunksize must be >= 1.")
    if on_unencodable not in ("warn", "error", "ignore"):
        raise ValueError("on_unencodable must be 'warn', 'error' or 'ignore'.")
    uni_cols = None if nvarchar is True else {str(c).lower() for c in (nvarchar or [])}
    spec = dict(tables) if isinstance(tables, Mapping) else {t: t for t in tables}
    engine = _make_engine(url)
    dialect = engine.dialect.name
    out: list[dict[str, Any]] = []
    codec, charset = (None, None) if (on_unencodable == "ignore" or nvarchar is True) else _target_codec(engine, dialect)
    try:
        for target, source in spec.items():
            t0 = time.time()
            src, kind = _source_sql(source)
            frm = src if kind == "table" else f"{src} AS q"
            types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {frm}").fetchall()}
            names = list(types)
            if columns and target in columns:
                names = [c for c in columns[target] if c in names]
            tname = _server_name(target, dialect)
            md = sa.MetaData(schema=schema)
            is_uni = {c: (nvarchar is True or (c.lower() in uni_cols) or (_server_name(c, dialect).lower() in uni_cols)) for c in names}
            cols = [sa.Column(_server_name(c, dialect), _sa_type(sa, types[c], dialect, varchar_length, unicode_text=is_uni[c])) for c in names]
            check = [c for c in names if codec and types[c].upper().startswith("VARCHAR") and not is_uni[c]]
            bad: dict[str, int] = {}
            table = sa.Table(tname, md, *cols)
            ddl = str(sa.schema.CreateTable(table).compile(dialect=engine.dialect)).strip()
            with engine.begin() as conn:
                exists = sa.inspect(conn).has_table(tname, schema=schema)
                if exists and if_exists == "fail":
                    raise ValueError(f"table {tname!r} already exists (use if_exists='replace' or 'append').")
                if exists and if_exists == "replace":
                    table.drop(conn)
                if not exists or if_exists == "replace":
                    table.create(conn)
                idx = [list(types).index(c) for c in names]
                cur = con.execute(f"SELECT * FROM {frm}")      # opened last: any other statement on this connection would reset it
                n = 0
                insert = table.insert()
                keys = [c.name for c in cols]
                while True:
                    rows = cur.fetchmany(int(chunksize))
                    if not rows:
                        break
                    conn.execute(insert, [dict(zip(keys, (_py(r[i]) for i in idx))) for r in rows])
                    n += len(rows)
                    for c in check:
                        k = _count_unencodable((r[list(types).index(c)] for r in rows), codec)
                        if k:
                            bad[c] = bad.get(c, 0) + k
                if bad and on_unencodable == "error":
                    raise ValueError(_unencodable_message(target, bad, charset, n) + " Nothing was loaded.")
            if bad:
                warnings.warn(_unencodable_message(tname, bad, charset, n), UserWarning, stacklevel=2)
            out.append({"table": tname, "rows": n, "seconds": round(time.time() - t0, 2), "ddl": ddl, "unencodable": bad})
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        engine.dispose()
    return out


def verify_transfer(con: duckdb.DuckDBPyConnection, tables: Sequence[str] | Mapping[str, str], url: str, *, schema: str | None = None,
                    rtol: float = 1e-9, content: bool = False, content_examples: int = 0) -> pd.DataFrame:
    """Compares each DuckDB table (or query) with the copy on the server: row count, and per column the non-null count and, for numeric
    columns, the sum, minimum and maximum. Returns one row per table and column with ``match`` True / False; a lossless round trip
    has no False. Column names are matched case-insensitively (Oracle upper-cases them).

    ``content=True`` also compares text, DATE, TIMESTAMP and BOOLEAN columns **by value**: both sides are read once (streamed) and each
    column gets an order-independent fingerprint, reported as ``<column>:content`` (``local`` / ``remote`` are ``"<non-null count>:<hex>"``).
    This catches what counts and sums cannot: text altered by the server's character set (non-Latin characters replaced by ``?`` in a
    ``WE8MSWIN1252`` database), truncated strings, shifted dates. It reads every value, so it costs a full scan of each column. Time-zone
    timestamps and binary columns are not compared. On Oracle an empty string is stored as NULL, so empty strings count as NULL on the
    local side. ``content_examples=n`` adds a ``detail`` column listing up to n differing values per mismatching column; leave it at 0 for
    real data, because the examples are data values."""
    sa = _sa()
    spec = dict(tables) if isinstance(tables, Mapping) else {t: t for t in tables}
    engine = _make_engine(url)
    dialect = engine.dialect.name
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            for target, source in spec.items():
                src, kind = _source_sql(source)
                frm = src if kind == "table" else f"{src} AS q"
                desc = con.execute(f"DESCRIBE SELECT * FROM {frm}").fetchall()
                n_local = con.execute(f"SELECT count(*) FROM {frm}").fetchone()[0]
                qname = (f"{schema}." if schema else "") + _server_name(target, dialect)
                n_remote = conn.execute(sa.text(f"SELECT count(*) FROM {qname}")).scalar()
                rows.append({"table": target, "column": "(rows)", "local": n_local, "remote": int(n_remote), "match": n_local == int(n_remote)})
                for name, typ, *_ in desc:
                    numeric = any(k in typ.upper() for k in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "REAL"))
                    rc = _server_name(name, dialect)
                    q_local = f'count("{name}")' + (f', sum("{name}"), min("{name}"), max("{name}")' if numeric else "")
                    q_remote = f"count({rc})" + (f", sum({rc}), min({rc}), max({rc})" if numeric else "")
                    a = con.execute(f"SELECT {q_local} FROM {frm}").fetchone()
                    b = conn.execute(sa.text(f"SELECT {q_remote} FROM {qname}")).fetchone()
                    for label, x, y in zip(("non_null", "sum", "min", "max"), a, b):
                        fx = None if x is None else float(x)
                        fy = None if y is None else float(y)
                        ok = (fx is None and fy is None) or (fx is not None and fy is not None and abs(fx - fy) <= rtol * max(1.0, abs(fx)))
                        rows.append({"table": target, "column": f"{name}:{label}", "local": fx, "remote": fy, "match": bool(ok)})
                    kind = _content_kind(typ) if content else None
                    if kind:
                        eon = dialect == "oracle"
                        cur = con.execute(f'SELECT "{name}" FROM {frm}')
                        fl = _fingerprint(_column_values(cur.fetchmany, kind, eon))
                        res = conn.execution_options(stream_results=True).execute(sa.text(f"SELECT {rc} FROM {qname}"))
                        fr = _fingerprint(_column_values(res.fetchmany, kind, False))
                        row = {"table": target, "column": f"{name}:content", "local": f"{fl[0]}:{fl[1]:016x}", "remote": f"{fr[0]}:{fr[1]:016x}", "match": fl == fr}
                        if fl != fr and content_examples > 0:
                            from collections import Counter

                            cur = con.execute(f'SELECT "{name}" FROM {frm}')
                            cl = Counter(_column_values(cur.fetchmany, kind, eon))
                            res = conn.execution_options(stream_results=True).execute(sa.text(f"SELECT {rc} FROM {qname}"))
                            cr = Counter(_column_values(res.fetchmany, kind, False))
                            only_l = [repr(v)[:60] for v in (cl - cr) if v is not None][:content_examples]
                            only_r = [repr(v)[:60] for v in (cr - cl) if v is not None][:content_examples]
                            row["detail"] = f"local only: {only_l}; remote only: {only_r}"
                        rows.append(row)
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        engine.dispose()
    return pd.DataFrame(rows)
