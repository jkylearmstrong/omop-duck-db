"""DuckDB -> server database (Oracle, PostgreSQL, SQL Server, Snowflake ... through SQLAlchemy) and a check that both sides agree (unreleased).

The other direction already exists: :func:`omop_etl.extract.import_omop` copies an OMOP CDM from a server into DuckDB and
:func:`omop_etl.extract.from_sql` streams server tables to Parquet. This module writes:

* :func:`to_sql`           - copies DuckDB tables, or the result of a query (a study feature table, a cohort), into a server schema in
  chunks: creates the table with portable column types (``NUMBER(19)``, ``VARCHAR2``, ``DATE``, ``TIMESTAMP`` on Oracle), upper-cases and
  length-checks identifiers on Oracle, and appends / replaces / refuses per ``if_exists``.
* :func:`verify_transfer`  - per table and column: row counts, non-null counts and, for numbers, sum / min / max on both sides, so a
  round trip (DuckDB -> Oracle -> DuckDB) can be shown to be lossless.

Credentials come from a SQLAlchemy URL or ``env:NAME`` and never appear in messages. The offline tests run the same code against SQLite
and compile the Oracle DDL without a server; nothing here has been run against a live Oracle database.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import time
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.extract import _make_engine, _masked, _sa, _scrub, resolve_url

__all__ = ["to_sql", "verify_transfer"]

_ORACLE_MAX_IDENT = 30  # conservative: Oracle before 12.2 (and many sites' naming rules) allow 30 characters


def _sa_type(sa: Any, duck_type: str, dialect: str, varchar_len: int):
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


def to_sql(con: duckdb.DuckDBPyConnection, tables: Sequence[str] | Mapping[str, str], url: str, *, schema: str | None = None,
           if_exists: str = "fail", chunksize: int = 50_000, varchar_length: int = 4000, columns: Mapping[str, Sequence[str]] | None = None) -> list[dict[str, Any]]:
    """Copies DuckDB tables or queries into a server schema.

    ``tables``: DuckDB table names (created under the same name on the server), or ``{server_table: "table or SELECT query"}`` to load a
    query result such as a feature table under another name. ``url``: SQLAlchemy URL or ``env:NAME`` (for Oracle
    ``oracle+oracledb://user:pass@host:1521/?service_name=SVC``). ``if_exists``: ``"fail"`` (default; refuses an existing table),
    ``"replace"`` (drops and recreates), ``"append"``. ``columns``: optional ``{server_table: [column, ...]}`` subset. Oracle identifiers are
    upper-cased and limited to 30 characters; text columns become ``VARCHAR2(varchar_length)`` (``CLOB`` beyond 4000).

    Returns one dict per table: ``table``, ``rows``, ``seconds``, ``ddl``. Nothing is committed for a table whose load fails."""
    sa = _sa()
    if if_exists not in ("fail", "replace", "append"):
        raise ValueError("if_exists must be 'fail', 'replace' or 'append'.")
    if int(chunksize) < 1:
        raise ValueError("chunksize must be >= 1.")
    spec = dict(tables) if isinstance(tables, Mapping) else {t: t for t in tables}
    engine = _make_engine(url)
    dialect = engine.dialect.name
    out: list[dict[str, Any]] = []
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
            cols = [sa.Column(_server_name(c, dialect), _sa_type(sa, types[c], dialect, varchar_length)) for c in names]
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
            out.append({"table": tname, "rows": n, "seconds": round(time.time() - t0, 2), "ddl": ddl})
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        engine.dispose()
    return out


def verify_transfer(con: duckdb.DuckDBPyConnection, tables: Sequence[str] | Mapping[str, str], url: str, *, schema: str | None = None,
                    rtol: float = 1e-9) -> pd.DataFrame:
    """Compares each DuckDB table (or query) with the copy on the server: row count, and per column the non-null count and, for numeric
    columns, the sum, minimum and maximum. Returns one row per table and column with ``match`` True / False; a lossless round trip
    has no False. Column names are matched case-insensitively (Oracle upper-cases them)."""
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
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        engine.dispose()
    return pd.DataFrame(rows)
