"""Oracle -> DuckDB (-> PostgreSQL -> Oracle) copies of a site's live tables, proved value by value, reading Oracle only.

The live site data stays in Oracle. This module copies it into a DuckDB file for analysis, and checks the copy against Oracle itself:

* :func:`mirror_oracle` - one **read-only** Oracle transaction (``SET TRANSACTION READ ONLY``, so the server refuses any write and every
  query sees the same snapshot while the site keeps loading data). In that snapshot it reads the data dictionary, probes the
  unconstrained ``NUMBER`` columns so they get an exact DuckDB type, computes a fingerprint of every table **inside Oracle**, streams the
  rows into DuckDB, computes the same fingerprint in DuckDB and compares. ``row_level=True`` also reads each table a second time through
  python-oracledb's own Arrow path and compares it with the DuckDB copy row for row (``EXCEPT ALL`` both ways).
* :func:`verify_duckdb_copy` / :func:`verify_postgres_copy` - re-check a DuckDB file, or a PostgreSQL schema made from it with
  :func:`omop_etl.embedded_postgres.store_site`, against the Oracle fingerprint saved next to the mirror, without connecting to Oracle.
* :func:`write_back` - (writes to Oracle, opt-in) loads the DuckDB copy into new scratch tables with :func:`omop_etl.load_sql.to_sql`,
  fingerprints them in Oracle and compares with the original; the scratch tables are dropped afterwards.

The fingerprint is computed by each database on its own data with the same canonical forms, per column:

======== ===================================================================================================================
kind     statistics
======== ===================================================================================================================
int      non-null count, exact sum, min, max, sum of the MD5 of the decimal text (detects any changed value, order-free)
decimal  as ``int``, on the value scaled to an integer (``12.345`` with scale 3 -> ``12345``)
datetime as ``int``, on microseconds since 1970 (Oracle ``DATE`` keeps its time of day); plus a count of sub-microsecond values
text     non-null count, sum of character lengths, sum of the MD5 of the UTF-8 bytes (Oracle text is converted to UTF-8 first,
         so a ``WE8MSWIN1252`` database gives the same hash as DuckDB for accented text)
lob      ``CLOB`` / ``NCLOB``: count, full character length, MD5 of the first 1000 (``NCLOB``: 500) characters
float    ``BINARY_DOUBLE`` / ``BINARY_FLOAT`` (and an unconstrained ``NUMBER`` too large for DECIMAL(38)): count, sum, min, max
         with a relative tolerance of 1e-9; ``row_level=True`` compares them exactly
other    RAW, BLOB, INTERVAL, time-zone timestamps ...: non-null count only (copied, but not compared by value)
======== ===================================================================================================================

Each MD5 contributes its first 60 bits as a number and the numbers are summed exactly (Oracle ``NUMBER``, DuckDB ``HUGEINT``, PostgreSQL
``numeric``), so the result does not depend on row order and a single changed, missing or extra value changes it.

Type mapping (the DuckDB copy keeps Oracle's values exactly): ``NUMBER(p, 0)`` -> ``BIGINT`` (``DECIMAL(38,0)`` above 18 digits),
``NUMBER(p, s)`` -> ``DECIMAL(p, s)``, ``NUMBER`` / ``FLOAT`` without precision -> probed: ``BIGINT`` when every value is a whole number of
at most 18 digits, otherwise the smallest exact ``DECIMAL``, ``DOUBLE`` only when no ``DECIMAL(38, s)`` holds the values;
``DATE`` / ``TIMESTAMP`` -> ``TIMESTAMP`` (microseconds; finer fractions are counted, so a ``TIMESTAMP(9)`` that holds nanoseconds fails
the check rather than being silently truncated); ``VARCHAR2`` / ``CHAR`` / ``NVARCHAR2`` / ``CLOB`` -> ``VARCHAR``; ``BINARY_DOUBLE`` ->
``DOUBLE``; ``RAW`` / ``BLOB`` -> ``BLOB``; anything else -> its text form. Table and column names become lower case.

Typical use (the URL comes from an environment variable and never appears in messages)::

    from omop_etl.oracle_roundtrip import mirror_oracle, verify_postgres_copy
    from omop_etl.embedded_postgres import EmbeddedPostgres, store_site

    rep = mirror_oracle("env:OMOP_ORACLE_URL", "private/oracle_mirror/site_a.duckdb", schema="CDM", tables="all")
    assert rep["match"].all()
    with EmbeddedPostgres("private/postgres") as pg:
        store_site(pg, "private/oracle_mirror/site_a.duckdb", "oracle_site_a")
        assert verify_postgres_copy(pg, "oracle_site_a", "private/oracle_mirror/site_a.duckdb")["match"].all()

The report and the saved fingerprint hold row counts and sums of patient-level columns: keep them with the site's private files.
``scripts/oracle_roundtrip_check.py`` writes a shareable record (PASS / FAIL per table, no counts).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import decimal
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.extract import _load_mapping, _make_engine, _scrub

__all__ = [
    "OracleColumn",
    "OracleTable",
    "describe_oracle",
    "mirror_oracle",
    "fingerprint_oracle",
    "fingerprint_duckdb",
    "fingerprint_postgres",
    "compare_fingerprints",
    "load_fingerprint",
    "verify_duckdb_copy",
    "verify_postgres_copy",
    "write_back",
]

FINGERPRINT_SUFFIX = ".oracle-fingerprint.json"
_FLOAT_RTOL = 1e-9
_HEX15 = "X" * 15
_IDENT = re.compile(r"^[a-z_][a-z0-9_$#]*$")


# -- plan ----------------------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class OracleColumn:
    """One source column: Oracle name and type, the DuckDB name and type of the copy, and the fingerprint kind."""

    source: str
    name: str
    oracle_type: str
    precision: int | None = None
    scale: int | None = None
    kind: str = "other"          # int | decimal | float | datetime | text | lob | other
    duck_type: str = "VARCHAR"
    text_form: str = "char"      # char | nchar | clob | nclob (Oracle only: how to reach the UTF-8 bytes)
    fraction_digits: int = 0     # TIMESTAMP(n)
    hash_chars: int = 0          # lob: only the first n characters are hashed (0: the whole value)
    probed: bool = False         # type decided by looking at the values (unconstrained NUMBER / FLOAT)


@dataclasses.dataclass
class OracleTable:
    owner: str
    source: str
    name: str
    columns: list[OracleColumn]

    @property
    def qualified(self) -> str:
        return f"{_oq(self.owner)}.{_oq(self.source)}"


def _oq(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _dq(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _classify(col: OracleColumn) -> OracleColumn:
    t = col.oracle_type.upper()
    p, s = col.precision, col.scale
    col.text_form = {"NVARCHAR2": "nchar", "NCHAR": "nchar", "CLOB": "clob", "NCLOB": "nclob", "LONG": "clob"}.get(t, "char")
    if t in ("NUMBER", "FLOAT", "INTEGER", "DECIMAL"):
        if t == "NUMBER" and s is not None and s > 0 and p is not None:
            col.kind, col.duck_type, col.scale = "decimal", f"DECIMAL({p},{s})", s
        elif t == "NUMBER" and s == 0 and p is not None:
            col.kind, col.duck_type, col.scale = "int", ("BIGINT" if p <= 18 else "DECIMAL(38,0)"), 0
        else:                                         # NUMBER, NUMBER(*,0) / INTEGER, NUMBER(p,-s), FLOAT(b): decided from the values
            col.kind, col.duck_type, col.probed = "int", "BIGINT", True
    elif t in ("BINARY_DOUBLE", "BINARY_FLOAT"):
        col.kind, col.duck_type = "float", "DOUBLE"
    elif t == "DATE":
        col.kind, col.duck_type = "datetime", "TIMESTAMP"
    elif re.fullmatch(r"TIMESTAMP(\(\d+\))?", t):
        col.kind, col.duck_type = "datetime", "TIMESTAMP"
        col.fraction_digits = 6 if s is None else int(s)
    elif t in ("VARCHAR2", "VARCHAR", "CHAR", "NVARCHAR2", "NCHAR"):
        col.kind, col.duck_type = "text", "VARCHAR"
    elif t in ("CLOB", "NCLOB", "LONG"):
        col.kind, col.duck_type = "lob", "VARCHAR"
        col.hash_chars = 500 if t == "NCLOB" else 1000
    elif t in ("RAW", "BLOB", "LONG RAW"):
        col.kind, col.duck_type = "other", "BLOB"
    else:
        col.kind, col.duck_type = "other", "VARCHAR"
    return col


def _probe_sql(table: OracleTable) -> str | None:
    """One aggregate query deciding the exact type of every unconstrained NUMBER / FLOAT column (whole numbers? how many digits?)."""
    parts = []
    for c in table.columns:
        if not c.probed:
            continue
        q = _oq(c.source)
        parts += [f"SUM(CASE WHEN {q} <> TRUNC({q}) THEN 1 ELSE 0 END)",
                  f"MAX(LENGTH(TO_CHAR(TRUNC(ABS({q})))))",
                  f"MAX(CASE WHEN {q} <> TRUNC({q}) THEN LENGTH(TO_CHAR(ABS({q}) - TRUNC(ABS({q})))) - 1 END)",
                  f"SUM(CASE WHEN INSTR(TO_CHAR({q}), 'E') > 0 THEN 1 ELSE 0 END)"]
    if not parts:
        return None
    return f"SELECT {', '.join(parts)} FROM {table.qualified}"


def _apply_probe(table: OracleTable, row: Sequence[Any]) -> None:
    it = iter(row)
    for c in table.columns:
        if not c.probed:
            continue
        nonint, idig, fdig, sci = (int(x) if x is not None else 0 for x in (next(it), next(it), next(it), next(it)))
        if sci:
            c.kind, c.duck_type = "float", "DOUBLE"
        elif nonint == 0:
            c.kind, c.scale, c.duck_type = "int", 0, ("BIGINT" if idig <= 18 else ("DECIMAL(38,0)" if idig <= 38 else "DOUBLE"))
            if idig > 38:
                c.kind = "float"
        elif idig + fdig <= 38:
            c.kind, c.scale = "decimal", fdig
            c.duck_type = f"DECIMAL({18 if idig + fdig <= 18 else 38},{fdig})"
        else:
            c.kind, c.duck_type = "float", "DOUBLE"


def _current_owner(cur) -> str:
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
    return cur.fetchone()[0]


def describe_oracle(cur, tables: Sequence[str] | str = "all", *, schema: str | None = None,
                    table_map: Mapping[str, str] | str | os.PathLike | None = None, probe: bool = True) -> list[OracleTable]:
    """Reads the data dictionary (``ALL_TAB_COLUMNS``) for ``tables`` and returns the copy plan.

    ``cur``: an open python-oracledb cursor. ``tables``: ``"all"`` (every table the owner has), or names, each optionally ``out=SOURCE``;
    names are matched case-insensitively and may be views. ``table_map``: ``{output_name: oracle_table}`` (dict, JSON or CSV file) to give
    the DuckDB copy other table names, for example when the Oracle tables carry a site prefix. ``probe``: run one aggregate query per
    table to give unconstrained ``NUMBER`` columns an exact type (otherwise they are assumed to be whole numbers)."""
    owner = schema.upper() if schema and schema == schema.lower() else (schema or _current_owner(cur))
    cur.execute("SELECT table_name FROM all_tables WHERE owner = :o AND (dropped IS NULL OR dropped = 'NO') AND nested = 'NO' "
                "AND secondary = 'N' UNION SELECT view_name FROM all_views WHERE owner = :o", o=owner)
    available = [r[0] for r in cur.fetchall()]
    mapping = _load_mapping(table_map)
    wanted: list[tuple[str, str]] = []
    if tables == "all":
        cur.execute("SELECT table_name FROM all_tables WHERE owner = :o AND (dropped IS NULL OR dropped = 'NO') AND nested = 'NO' "
                    "AND secondary = 'N' AND table_name NOT LIKE 'BIN$%' ORDER BY table_name", o=owner)
        reverse = {v.lower(): k for k, v in mapping.items()}
        wanted = [(reverse.get(t.lower(), t.lower()), t) for t in (r[0] for r in cur.fetchall())]
    else:
        for item in tables:
            item = str(item).strip()
            if not item:
                continue
            out, eq, src = item.partition("=")
            out, src = out.strip(), (src.strip() if eq else mapping.get(out.strip().lower(), out.strip()))
            exact = [a for a in available if a == src] or [a for a in available if a.lower() == src.lower()]
            if not exact:
                raise LookupError(f"Table or view {src!r} not found for this owner (positions in --tables: {len(wanted) + 1}).")
            if len(exact) > 1:
                raise LookupError(f"Table name {src!r} is ambiguous; give the exact name.")
            wanted.append((out.lower(), exact[0]))
    names = [w[0] for w in wanted]
    if len(set(names)) != len(names):
        raise ValueError("Two Oracle tables map to the same DuckDB table name; use table_map to rename one.")
    plan: list[OracleTable] = []
    for out, src in wanted:
        if not _IDENT.match(out):
            raise ValueError(f"DuckDB table name {out!r}: use letters, digits and _ (table_map can rename it).")
        cur.execute("SELECT column_name, data_type, data_precision, data_scale FROM all_tab_columns WHERE owner = :o AND table_name = :t "
                    "ORDER BY column_id", o=owner, t=src)
        cols = [_classify(OracleColumn(source=n, name=n.lower(), oracle_type=re.sub(r"\(\d+\)$", "", t) if not t.startswith("TIMESTAMP(") else t,
                                       precision=p, scale=s)) for n, t, p, s in cur.fetchall()]
        lower = [c.name for c in cols]
        if len(set(lower)) != len(lower):
            raise ValueError(f"Table {out!r} has columns whose names collide once lower-cased.")
        tbl = OracleTable(owner=owner, source=src, name=out, columns=cols)
        sql = _probe_sql(tbl)
        if probe and sql:
            cur.execute(sql)
            _apply_probe(tbl, cur.fetchone())
        elif sql:
            for c in tbl.columns:
                if c.probed:
                    c.kind, c.scale, c.duck_type = "int", 0, "DECIMAL(38,0)"
        plan.append(tbl)
    return plan


# -- fingerprints --------------------------------------------------------------------------------------------------------------------
def _stats(kind: str) -> list[str]:
    return {"int": ["n", "sum", "min", "max", "hash"], "decimal": ["n", "sum", "min", "max", "hash"],
            "datetime": ["n", "sum", "min", "max", "hash", "sub_us"], "float": ["n", "sum", "min", "max"],
            "text": ["n", "len", "hash"], "lob": ["n", "len", "hash"], "other": ["n"]}[kind]


def _ora_hash(expr: str) -> str:
    return f"TO_NUMBER(SUBSTR(RAWTOHEX(STANDARD_HASH({expr}, 'MD5')), 1, 15), '{_HEX15}')"


def _ora_us(q: str) -> str:
    iv = f"({q} - TIMESTAMP '1970-01-01 00:00:00')"   # no CAST: a CAST to TIMESTAMP(6) would round nanoseconds
    return (f"(EXTRACT(DAY FROM {iv}) * 86400000000 + EXTRACT(HOUR FROM {iv}) * 3600000000 + EXTRACT(MINUTE FROM {iv}) * 60000000 "
            f"+ TRUNC(EXTRACT(SECOND FROM {iv}) * 1000000))")


def _ora_utf8(c: OracleColumn, q: str, unicode_db: bool) -> str:
    """Expression giving the UTF-8 bytes (or a VARCHAR2 holding them) of a text value."""
    n = c.hash_chars
    if c.text_form in ("clob", "nclob"):
        q = f"DBMS_LOB.SUBSTR({q}, {n or 1000}, 1)"
    elif n:
        q = f"SUBSTR({q}, 1, {n})"
    if c.text_form in ("nchar", "nclob"):
        return f"UTL_I18N.STRING_TO_RAW({q}, 'AL32UTF8')"
    return q if unicode_db else f"CONVERT({q}, 'AL32UTF8')"


def _ora_exprs(c: OracleColumn, unicode_db: bool) -> list[str]:
    q = _oq(c.source)
    nn = f"CASE WHEN {q} IS NOT NULL THEN 1 END"
    if c.kind in ("int", "decimal", "datetime"):
        v = q if c.kind == "int" else (f"({q} * POWER(10, {c.scale or 0}))" if c.kind == "decimal" else _ora_us(q))
        out = [f"COUNT({nn})", f"SUM({v})", f"MIN({v})", f"MAX({v})",
               f"SUM(CASE WHEN {q} IS NOT NULL THEN {_ora_hash(f'TO_CHAR({v})')} END)"]
        if c.kind == "datetime":
            sub = (f"SUM(CASE WHEN MOD(TRUNC(EXTRACT(SECOND FROM {q}) * 1000000000), 1000) <> 0 THEN 1 ELSE 0 END)"
                   if c.fraction_digits > 6 else "0")
            out.append(sub)
        return out
    if c.kind == "float":
        return [f"COUNT({nn})", f"SUM({q})", f"MIN({q})", f"MAX({q})"]
    if c.kind in ("text", "lob"):
        length = f"DBMS_LOB.GETLENGTH({q})" if c.text_form in ("clob", "nclob") else f"LENGTH({q})"
        return [f"COUNT({nn})", f"SUM({length})", f"SUM(CASE WHEN {q} IS NOT NULL THEN {_ora_hash(_ora_utf8(c, q, unicode_db))} END)"]
    return [f"COUNT({nn})"]


def _num(v: Any) -> Any:
    """Driver value -> exact Python number (Oracle values arrive as TO_CHAR text)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("nan", "~"):
            return float("nan")
        if s.lower() in ("inf", "+inf"):
            return float("inf")
        if s.lower() == "-inf":
            return float("-inf")
        d = decimal.Decimal(s)
        return int(d) if d == d.to_integral_value() and "E" not in s.upper() or (d == d.to_integral_value() and abs(d) < 10 ** 38) else d
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else v
    return int(v) if isinstance(v, int) else v


def _rows_to_fp(table: OracleTable, values: Sequence[Any], *, kinds_float: set[str] | None = None) -> dict[str, Any]:
    it = iter(values)
    fp: dict[str, Any] = {"(rows)": _num(next(it))}
    for c in table.columns:
        for st in _stats(c.kind):
            v = _num(next(it))
            if c.kind == "float" and st != "n" and v is not None:
                v = float(v)
            fp[f"{c.name}:{st}"] = v
    return fp


def _unicode_db(cur) -> bool:
    try:
        cur.execute("SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_CHARACTERSET'")
        return str(cur.fetchone()[0]).upper() in ("AL32UTF8", "UTF8")
    except Exception:  # cannot tell: converting is always correct, only slower
        return False


def fingerprint_oracle(cur, table: OracleTable, *, unicode_db: bool | None = None) -> dict[str, Any]:
    """Fingerprint of one Oracle table, computed by Oracle in one full scan (see the module docstring)."""
    if unicode_db is None:
        unicode_db = _unicode_db(cur)
    exprs = ["COUNT(*)"] + [e for c in table.columns for e in _ora_exprs(c, unicode_db)]
    cur.execute(f"SELECT {', '.join(f'TO_CHAR({e})' for e in exprs)} FROM {table.qualified}")
    return _rows_to_fp(table, cur.fetchone())


def _duck_exprs(c: OracleColumn) -> list[str]:
    q = _dq(c.name)

    def h(text: str) -> str:
        return f"sum(CASE WHEN {q} IS NOT NULL THEN CAST(('0x' || substr(md5({text}), 1, 15)) AS BIGINT) END)::HUGEINT"

    if c.kind in ("int", "decimal", "datetime"):
        if c.kind == "int":
            v = f"CAST({q} AS HUGEINT)" if c.duck_type == "BIGINT" else f"CAST({q} AS DECIMAL(38,0))"
        elif c.kind == "decimal":
            v = f"CAST(replace(CAST({q} AS VARCHAR), '.', '') AS HUGEINT)"
        else:
            v = f"epoch_us(CAST({q} AS TIMESTAMP))"
        out = [f"count({q})", f"sum({v})", f"min({v})", f"max({v})", h(f"CAST({v} AS VARCHAR)")]
        if c.kind == "datetime":
            out.append("0")
        return out
    if c.kind == "float":
        return [f"count({q})", f"sum({q})", f"min({q})", f"max({q})"]
    if c.kind in ("text", "lob"):
        text = f"substr({q}, 1, {c.hash_chars})" if c.hash_chars else q
        return [f"count({q})", f"sum(length({q}))", h(text)]
    return [f"count({q})"]


def fingerprint_duckdb(con: duckdb.DuckDBPyConnection, table: OracleTable, *, relation: str | None = None) -> dict[str, Any]:
    """The same fingerprint computed by DuckDB on the copy (``relation``: table or view to read, default the table's DuckDB name)."""
    exprs = ["count(*)"] + [e for c in table.columns for e in _duck_exprs(c)]
    row = con.execute(f"SELECT {', '.join(exprs)} FROM {relation or _dq(table.name)}").fetchone()
    return _rows_to_fp(table, row)


def _pg_exprs(c: OracleColumn) -> list[str]:
    q = _dq(c.name)

    def h(text: str) -> str:
        return f"sum(CASE WHEN {q} IS NOT NULL THEN ('x' || lpad(substr(md5({text}), 1, 15), 16, '0'))::bit(64)::bigint END)"

    if c.kind in ("int", "decimal", "datetime"):
        if c.kind == "int":
            v = f"({q})::numeric"
        elif c.kind == "decimal":
            v = f"(({q})::numeric * power(10::numeric, {c.scale or 0}))::numeric(38,0)"
        else:
            v = f"round(extract(epoch from ({q})::timestamp) * 1000000)::numeric(38,0)"
        out = [f"count({q})", f"sum({v})", f"min({v})", f"max({v})", h(f"({v})::text")]
        if c.kind == "datetime":
            out.append("0")
        return out
    if c.kind == "float":
        return [f"count({q})", f"sum({q})", f"min({q})", f"max({q})"]
    if c.kind in ("text", "lob"):
        text = f"substr({q}, 1, {c.hash_chars})" if c.hash_chars else q
        return [f"count({q})", f"sum(char_length({q}))", h(text)]
    return [f"count({q})"]


def fingerprint_postgres(conn, schema: str, table: OracleTable) -> dict[str, Any]:
    """The same fingerprint computed by PostgreSQL on a copy stored with :func:`omop_etl.embedded_postgres.store_site` (psycopg2
    connection). ``to_sql`` stores DuckDB booleans and integers as integers and text as ``VARCHAR`` / ``TEXT``, which this reads as is."""
    exprs = ["count(*)"] + [e for c in table.columns for e in _pg_exprs(c)]
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(exprs)} FROM {_dq(schema)}.{_dq(table.name)}")
        row = cur.fetchone()
    return _rows_to_fp(table, row)


def _same(kind: str, stat: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if kind == "float" and stat != "n":
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        if math.isinf(fa) or math.isinf(fb):
            return fa == fb
        return fa == fb or abs(fa - fb) <= _FLOAT_RTOL * max(1.0, abs(fa))
    return decimal.Decimal(str(a)) == decimal.Decimal(str(b))


def compare_fingerprints(table: OracleTable, a: Mapping[str, Any], b: Mapping[str, Any], *, sides: tuple[str, str] = ("oracle", "copy")
                         ) -> pd.DataFrame:
    """One row per statistic: ``table``, ``column``, ``stat``, ``kind``, the two values and ``match``."""
    kinds = {c.name: c.kind for c in table.columns}
    rows = []
    for key in a:
        col, _, stat = key.rpartition(":") if ":" in key else ("(rows)", "", "n")
        if key == "(rows)":
            col, stat = "(rows)", "n"
        kind = kinds.get(col, "int")
        rows.append({"table": table.name, "column": col, "stat": stat, "kind": kind, sides[0]: a[key], sides[1]: b.get(key),
                     "match": key in b and _same(kind, stat, a[key], b[key])})
    return pd.DataFrame(rows)


# -- copy ----------------------------------------------------------------------------------------------------------------------------
def _output_handler(cursor, metadata):
    import oracledb

    t = metadata.type_code
    if t is oracledb.DB_TYPE_NUMBER:
        return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)
    if t is oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if t is oracledb.DB_TYPE_NCLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_NVARCHAR, arraysize=cursor.arraysize)
    if t is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def _arrow_type(c: OracleColumn):
    import pyarrow as pa

    t = c.duck_type
    if t == "BIGINT":
        return pa.int64()
    if t.startswith("DECIMAL"):
        p, s = (int(x) for x in t[t.index("(") + 1:-1].split(","))
        return pa.decimal128(p, s)
    if t == "DOUBLE":
        return pa.float64()
    if t == "TIMESTAMP":
        return pa.timestamp("us")
    if t == "BLOB":
        return pa.binary()
    return pa.string()


def _to_arrow(values: list, c: OracleColumn, at):
    import pyarrow as pa

    if c.duck_type == "BIGINT":
        values = [None if v is None else int(v) for v in values]
    elif c.duck_type.startswith("DECIMAL"):
        values = [None if v is None else (v if isinstance(v, decimal.Decimal) else decimal.Decimal(str(v))) for v in values]
    elif c.duck_type == "DOUBLE":
        values = [None if v is None else float(v) for v in values]
    elif c.duck_type == "TIMESTAMP":
        values = [None if v is None else (v if isinstance(v, _dt.datetime) else _dt.datetime.combine(v, _dt.time())) for v in values]
    elif c.duck_type == "BLOB":
        values = [None if v is None else bytes(v) for v in values]
    else:
        values = [None if v is None else (v if isinstance(v, str) else str(v)) for v in values]
    return pa.array(values, type=at)


def _copy_table(ocon, table: OracleTable, con: duckdb.DuckDBPyConnection, chunksize: int) -> int:
    import pyarrow as pa

    con.execute(f"CREATE TABLE {_dq(table.name)} ({', '.join(f'{_dq(c.name)} {c.duck_type}' for c in table.columns)})")
    types = [_arrow_type(c) for c in table.columns]
    schema = pa.schema([pa.field(c.name, at) for c, at in zip(table.columns, types)])
    cur = ocon.cursor()
    cur.arraysize = min(int(chunksize), 50_000)
    cur.prefetchrows = cur.arraysize + 1
    cur.outputtypehandler = _output_handler
    n = 0
    try:
        cur.execute(f"SELECT {', '.join(_oq(c.source) for c in table.columns)} FROM {table.qualified}")
        while True:
            rows = cur.fetchmany(int(chunksize))
            if not rows:
                break
            cols = list(zip(*rows))
            batch = pa.Table.from_arrays([_to_arrow(list(cols[i]), c, types[i]) for i, c in enumerate(table.columns)], schema=schema)
            con.register("_ora_batch", batch)
            con.execute(f"INSERT INTO {_dq(table.name)} SELECT * FROM _ora_batch")
            con.unregister("_ora_batch")
            n += len(rows)
    finally:
        cur.close()
    return n


def _row_level(ocon, table: OracleTable, con: duckdb.DuckDBPyConnection, chunksize: int) -> tuple[int, int, list[str]]:
    """Second, independent read (python-oracledb's Arrow fetch, numbers as their exact decimal text) compared row for row.

    Returns (rows only in Oracle, rows only in the copy, columns left out)."""
    import pyarrow as pa

    cols = [c for c in table.columns if c.kind != "other"]
    skipped = [c.name for c in table.columns if c.kind == "other"]
    sel = []
    for c in cols:
        q = _oq(c.source)
        if c.kind in ("int", "decimal"):
            sel.append(f"TO_CHAR({q}) AS {_oq(c.name)}")
        elif c.kind == "float" and c.oracle_type not in ("BINARY_DOUBLE", "BINARY_FLOAT"):
            sel.append(f"TO_BINARY_DOUBLE({q}) AS {_oq(c.name)}")
        else:
            sel.append(f"{q} AS {_oq(c.name)}")
    stage = f"_rowcheck_{table.name}"
    con.execute(f"CREATE TEMP TABLE {_dq(stage)} ({', '.join(f'{_dq(c.name)} {c.duck_type}' for c in cols)})")
    casts = ", ".join(f"CAST({_dq(c.name)} AS {c.duck_type})" for c in cols)
    try:
        for df in ocon.fetch_df_batches(f"SELECT {', '.join(sel)} FROM {table.qualified}", size=int(chunksize)):
            at = pa.table(df)
            at = at.rename_columns([c.name for c in cols])
            con.register("_ora_df", at)
            con.execute(f"INSERT INTO {_dq(stage)} SELECT {casts} FROM _ora_df")
            con.unregister("_ora_df")
        names = ", ".join(_dq(c.name) for c in cols)
        a = con.execute(f"SELECT count(*) FROM (SELECT {names} FROM {_dq(stage)} EXCEPT ALL SELECT {names} FROM {_dq(table.name)})").fetchone()[0]
        b = con.execute(f"SELECT count(*) FROM (SELECT {names} FROM {_dq(table.name)} EXCEPT ALL SELECT {names} FROM {_dq(stage)})").fetchone()[0]
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_dq(stage)}")
    return int(a), int(b), skipped


def _driver_connection(url: str, engine=None):
    """A python-oracledb connection from a SQLAlchemy URL (``oracle+oracledb://``) or ``env:NAME``; the engine is returned for disposal."""
    engine = engine or _make_engine(url)
    if engine.dialect.name != "oracle":
        raise ValueError(f"This check reads Oracle; the URL is for {engine.dialect.name}.")
    raw = engine.raw_connection()
    return engine, raw, raw.driver_connection


def _plan_to_json(plan: Sequence[OracleTable]) -> list[dict]:
    return [{"owner": "", "source": t.source, "name": t.name, "columns": [dataclasses.asdict(c) for c in t.columns]} for t in plan]


def _plan_from_json(items: Sequence[dict]) -> list[OracleTable]:
    return [OracleTable(owner=t.get("owner", ""), source=t["source"], name=t["name"], columns=[OracleColumn(**c) for c in t["columns"]])
            for t in items]


def load_fingerprint(path: str | os.PathLike) -> tuple[list[OracleTable], dict[str, dict[str, Any]], dict[str, Any]]:
    """(plan, {table: fingerprint}, info) from a mirror file (``<mirror>.oracle-fingerprint.json`` next to it) or that JSON itself."""
    p = Path(path)
    if not p.name.endswith(FINGERPRINT_SUFFIX):
        p = p.with_name(p.name + FINGERPRINT_SUFFIX)
    data = json.loads(p.read_text(encoding="utf-8"), parse_float=decimal.Decimal)

    def back(v):
        if isinstance(v, dict) and "float" in v:
            return float(v["float"])
        if isinstance(v, decimal.Decimal):
            return v
        return v

    fps = {t: {k: back(v) for k, v in fp.items()} for t, fp in data["fingerprints"].items()}
    return _plan_from_json(data["plan"]), fps, data.get("info", {})


def _save_fingerprint(path: Path, plan, fps, info) -> None:
    def enc(v):
        if isinstance(v, float):
            return {"float": repr(v)}
        if isinstance(v, decimal.Decimal):
            return str(v) if v != v.to_integral_value() else int(v)
        return v

    data = {"format": 1, "info": info, "plan": _plan_to_json(plan),
            "fingerprints": {t: {k: enc(v) for k, v in fp.items()} for t, fp in fps.items()}}
    text = json.dumps(data, indent=1)
    # decimals were written as strings; mark them so they read back exactly
    path.write_text(text, encoding="utf-8")


def mirror_oracle(url: str, out: str | os.PathLike, *, schema: str | None = None, tables: Sequence[str] | str = "all",
                  table_map: Mapping[str, str] | str | os.PathLike | None = None, chunksize: int = 100_000, row_level: bool = False,
                  snapshot: bool = True, progress=None) -> pd.DataFrame:
    """Copies Oracle tables into a new DuckDB file ``out`` and proves the copy equals Oracle; reads Oracle only.

    Args:
        url: SQLAlchemy URL (``oracle+oracledb://...``) or ``env:NAME``. Never logged.
        out: New DuckDB file (refused if it exists). The Oracle fingerprint is saved next to it as ``<out>.oracle-fingerprint.json``
            for :func:`verify_duckdb_copy` / :func:`verify_postgres_copy` later.
        schema: Oracle owner (default: the account's current schema).
        tables: ``"all"`` or a list (see :func:`describe_oracle`). ``table_map``: output name -> Oracle table.
        row_level: Also read every table a second time and compare row for row (doubles the reading).
        snapshot: Run everything in one ``SET TRANSACTION READ ONLY`` transaction (default). Oracle then refuses any write and every
            query sees the data as of the start, so rows loaded meanwhile do not cause false differences. A very long run can hit
            ``ORA-01555 snapshot too old`` if the undo retention is short; then copy fewer tables per run or pass ``snapshot=False``.
        progress: Optional callable receiving short status strings (table names only).

    Returns one row per table and statistic (see :func:`compare_fingerprints`), plus ``stat == "row_level"`` rows when asked.
    ``attrs``: ``tables`` (per-table rows and seconds), ``info`` (server version, character set), ``fingerprint`` (path).
    """
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} exists; choose a new file so nothing is overwritten.")
    out.parent.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda s: None)
    engine = raw = None
    reports, fps, summary = [], {}, []
    try:
        engine, raw, ocon = _driver_connection(url)
        cur = ocon.cursor()
        if snapshot:
            ocon.rollback()                       # SET TRANSACTION must start a transaction
            cur.execute("SET TRANSACTION READ ONLY")
        info = {"read_only_snapshot": bool(snapshot)}
        try:
            cur.execute("SELECT banner FROM v$version WHERE ROWNUM = 1")
            info["server_version"] = cur.fetchone()[0]
        except Exception:
            info["server_version"] = f"Oracle {ocon.version}"
        try:
            cur.execute("SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_CHARACTERSET'")
            info["NLS_CHARACTERSET"] = cur.fetchone()[0]
        except Exception:
            info["NLS_CHARACTERSET"] = "not readable with this account"
        unicode_db = str(info["NLS_CHARACTERSET"]).upper() in ("AL32UTF8", "UTF8")
        plan = describe_oracle(cur, tables, schema=schema, table_map=table_map)
        con = duckdb.connect(str(out))
        try:
            for t in plan:
                t0 = time.time()
                say(f"{t.name}: Oracle fingerprint")
                fo = fingerprint_oracle(cur, t, unicode_db=unicode_db)
                say(f"{t.name}: copy")
                n = _copy_table(ocon, t, con, chunksize)
                fd = fingerprint_duckdb(con, t)
                rep = compare_fingerprints(t, fo, fd, sides=("oracle", "duckdb"))
                if row_level:
                    say(f"{t.name}: row-level read")
                    a, b, skipped = _row_level(ocon, t, con, chunksize)
                    rep = pd.concat([rep, pd.DataFrame([{"table": t.name, "column": "(rows)", "stat": "row_level", "kind": "rows",
                                                         "oracle": a, "duckdb": b, "match": a == 0 and b == 0,
                                                         "note": ("not compared: " + ", ".join(skipped)) if skipped else ""}])],
                                    ignore_index=True)
                fps[t.name] = fo
                reports.append(rep)
                summary.append({"table": t.name, "rows": n, "seconds": round(time.time() - t0, 1)})
            con.execute("CHECKPOINT")
        finally:
            con.close()
        if snapshot:
            ocon.rollback()                       # ends the read-only transaction; nothing was written
        cur.close()
    except Exception as exc:
        if isinstance(exc, (FileExistsError,)):
            raise
        raise _scrub(exc, url) from None
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()
    fp_path = out.with_name(out.name + FINGERPRINT_SUFFIX)
    _save_fingerprint(fp_path, plan, fps, info)
    report = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame(
        columns=["table", "column", "stat", "kind", "oracle", "duckdb", "match"])
    report.attrs.update({"tables": summary, "info": info, "fingerprint": str(fp_path)})
    return report


def verify_duckdb_copy(duckdb_path: str | os.PathLike, fingerprint: str | os.PathLike | None = None) -> pd.DataFrame:
    """Re-checks a DuckDB file (the mirror, or any copy with the same tables, such as one read back from PostgreSQL) against the
    saved Oracle fingerprint. ``fingerprint``: the JSON or the mirror it sits next to (default: next to ``duckdb_path``)."""
    plan, fps, _ = load_fingerprint(fingerprint or duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return pd.concat([compare_fingerprints(t, fps[t.name], fingerprint_duckdb(con, t), sides=("oracle", "duckdb")) for t in plan],
                         ignore_index=True)
    finally:
        con.close()


def verify_postgres_copy(pg, schema: str, fingerprint: str | os.PathLike) -> pd.DataFrame:
    """Checks a PostgreSQL schema (made from the mirror with ``store_site``) against the Oracle fingerprint, with no Oracle connection.
    ``pg``: :class:`omop_etl.embedded_postgres.EmbeddedPostgres` (started), a libpq DSN or a psycopg2 connection."""
    import psycopg2

    plan, fps, _ = load_fingerprint(fingerprint)
    if hasattr(pg, "cursor"):
        conn, close = pg, False
    else:
        conn, close = psycopg2.connect(pg if isinstance(pg, str) else pg.dsn), True
    try:
        return pd.concat([compare_fingerprints(t, fps[t.name], fingerprint_postgres(conn, schema, t), sides=("oracle", "postgres"))
                          for t in plan], ignore_index=True)
    finally:
        if close:
            conn.close()


def write_back(url: str, duckdb_path: str | os.PathLike, *, prefix: str = "OMOP_RT_", schema: str | None = None, keep: bool = False,
               nvarchar: bool = False, fingerprint: str | os.PathLike | None = None) -> pd.DataFrame:
    """DuckDB -> Oracle: **writes** each mirrored table to a new Oracle table ``<prefix><NAME>`` (refused if it exists), fingerprints
    it in Oracle and compares with the saved fingerprint of the original, then drops it unless ``keep``. Completes the round trip
    Oracle -> DuckDB -> Oracle; use a scratch schema, never the live one. Names longer than 30 characters are refused by ``to_sql``."""
    from omop_etl.load_sql import to_sql

    plan, fps, _ = load_fingerprint(fingerprint or duckdb_path)
    con = duckdb.connect(str(duckdb_path), read_only=True)
    engine = raw = None
    created: list[str] = []
    reports = []
    try:
        for t in plan:
            target = (prefix + t.name).upper()
            to_sql(con, {target: f'SELECT * FROM {_dq(t.name)}'}, url, schema=schema, if_exists="fail", nvarchar=nvarchar)
            created.append(target)
        engine, raw, ocon = _driver_connection(url)
        cur = ocon.cursor()
        unicode_db = _unicode_db(cur)
        for t, target in zip(plan, created):
            back = describe_oracle(cur, [f"{t.name}={target}"], schema=schema, probe=False)[0]
            by_name = {c.name: c for c in back.columns}
            cols = []
            for c in t.columns:                                     # kinds from the original; Oracle types of the new table
                b = by_name[c.name]
                cols.append(dataclasses.replace(c, source=b.source, oracle_type=b.oracle_type, text_form=b.text_form,
                                                fraction_digits=b.fraction_digits))
            fb = fingerprint_oracle(cur, OracleTable(back.owner, back.source, t.name, cols), unicode_db=unicode_db)
            reports.append(compare_fingerprints(t, fps[t.name], fb, sides=("oracle", "written_back")))
        cur.close()
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        con.close()
        if raw is not None:
            raw.close()
        if engine is not None:
            engine.dispose()
        if not keep and created:
            _drop_tables(url, created, schema)
    return pd.concat(reports, ignore_index=True) if reports else pd.DataFrame()


def _drop_tables(url: str, names: Sequence[str], schema: str | None) -> None:
    import sqlalchemy as sa

    engine = _make_engine(url)
    try:
        with engine.begin() as c:
            for n in names:
                c.execute(sa.text(f"DROP TABLE {(_oq(schema.upper()) + '.') if schema else ''}{_oq(n)} PURGE"))
    finally:
        engine.dispose()
