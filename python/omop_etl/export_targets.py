"""Multi-database export targets generated from ONE schema source (FR-B4).

The CDM DDL file shipped with the package (``extdata/5.4/duckdb/OMOPCDM_duckdb_5.4_ddl.sql``) is parsed into a neutral
schema model; DDL, loader scripts and data-file specifications for Oracle, SQL Server, Snowflake and BigQuery are all
derived from that model, so they cannot drift apart. :func:`lint_ddl` statically checks generated DDL (used in CI, and
with ``sqlglot`` when installed).

Per target:

========== ============================ ==========================================================================
target     data files                   loader
========== ============================ ==========================================================================
oracle     CSV (+ ``.ctl`` per table)   ``sqlldr`` control files with explicit date masks (independent of
                                        ``NLS_DATE_FORMAT``), ``SKIP=1`` for the header, ``load_oracle.sh``
sqlserver  CSV                          ``BULK INSERT ... WITH (FORMAT='CSV', FIELDQUOTE='"', KEEPNULLS)``
                                        (SQL Server 2017+; handles quoted commas, unlike classic ``bcp``)
snowflake  Parquet                      ``PUT`` to an internal stage and ``COPY INTO ... MATCH_BY_COLUMN_NAME``
bigquery   Parquet                      ``bq load --source_format=PARQUET`` load jobs
========== ============================ ==========================================================================

Oracle: identifiers longer than 30 characters (Oracle < 12.2) raise :class:`IdentifierError` unless
``oracle_max_identifier`` is raised to 128; reserved words (``DATE``, ``COMMENT``, ...) are double-quoted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from omop_etl.build_omop_cdm import _resource_path

__all__ = ["Column", "IdentifierError", "load_cdm_schema", "generate_ddl", "lint_ddl", "EXTRA_TARGETS",
           "oracle_ctl", "sqlserver_load_script", "snowflake_load_script", "bigquery_load_script", "csv_select"]

EXTRA_TARGETS = ("oracle", "sqlserver", "snowflake", "bigquery")


class IdentifierError(ValueError):
    """An identifier is too long for the target database."""


@dataclass(frozen=True)
class Column:
    name: str
    base: str  # integer | varchar | text | date | timestamp | numeric
    length: int | None
    nullable: bool


_COL_RE = re.compile(r'^\s*"?([a-z_0-9]+)"?\s+([A-Za-z]+)(?:\(\s*(\d+)\s*\))?\s+(NOT NULL|NULL)\s*,?\s*\)?\s*;?\s*$', re.I)
_TABLE_RE = re.compile(r"CREATE TABLE\s+@cdmDatabaseSchema\.([a-z_0-9]+)\s*\(", re.I)
_BASES = {"integer": "integer", "varchar": "varchar", "text": "text", "date": "date", "timestamp": "timestamp",
          "numeric": "numeric"}


def load_cdm_schema(ddl_path: str | Path | None = None) -> dict[str, list[Column]]:
    """Parses the CDM DDL file into ``{table: [Column, ...]}`` (ordered)."""
    path = Path(ddl_path) if ddl_path else Path(_resource_path("extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql"))
    schema: dict[str, list[Column]] = {}
    current: list[Column] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue
        m = _TABLE_RE.search(line)
        if m:
            current = schema.setdefault(m.group(1).lower(), [])
            continue
        if current is None:
            continue
        c = _COL_RE.match(line)
        if not c:
            if line.strip():
                raise ValueError(f"Unparsed DDL line in {path.name}: {line.strip()!r}")
            continue
        base = _BASES.get(c.group(2).lower())
        if base is None:
            raise ValueError(f"Unknown type {c.group(2)!r} in {path.name}")
        current.append(Column(c.group(1).lower(), base, int(c.group(3)) if c.group(3) else None,
                              c.group(4).upper() == "NULL"))
        if line.rstrip().endswith(";"):
            current = None
    return schema


# ---- dialects ----------------------------------------------------------------------------------------------

_ORACLE_RESERVED = {
    "ACCESS", "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUDIT", "BETWEEN", "BY", "CHAR", "CHECK", "CLUSTER",
    "COLUMN", "COMMENT", "COMPRESS", "CONNECT", "CREATE", "CURRENT", "DATE", "DECIMAL", "DEFAULT", "DELETE", "DESC",
    "DISTINCT", "DROP", "ELSE", "EXCLUSIVE", "EXISTS", "FILE", "FLOAT", "FOR", "FROM", "GRANT", "GROUP", "HAVING",
    "IDENTIFIED", "IMMEDIATE", "IN", "INCREMENT", "INDEX", "INITIAL", "INSERT", "INTEGER", "INTERSECT", "INTO", "IS",
    "LEVEL", "LIKE", "LOCK", "LONG", "MAXEXTENTS", "MINUS", "MLSLABEL", "MODE", "MODIFY", "NOAUDIT", "NOCOMPRESS",
    "NOT", "NOWAIT", "NULL", "NUMBER", "OF", "OFFLINE", "ON", "ONLINE", "OPTION", "OR", "ORDER", "PCTFREE", "PRIOR",
    "PUBLIC", "RAW", "RENAME", "RESOURCE", "REVOKE", "ROW", "ROWID", "ROWNUM", "ROWS", "SELECT", "SESSION", "SET",
    "SHARE", "SIZE", "SMALLINT", "START", "SUCCESSFUL", "SYNONYM", "SYSDATE", "TABLE", "THEN", "TO", "TRIGGER", "UID",
    "UNION", "UNIQUE", "UPDATE", "USER", "VALIDATE", "VALUES", "VARCHAR", "VARCHAR2", "VIEW", "WHENEVER", "WHERE", "WITH",
}
_SQLSERVER_RESERVED = {
    "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BACKUP", "BEGIN", "BETWEEN", "BY", "CASE", "CHECK", "COLUMN",
    "COMMIT", "CONSTRAINT", "CREATE", "CURRENT", "DATABASE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE",
    "END", "EXEC", "EXISTS", "FILE", "FOR", "FOREIGN", "FROM", "FULL", "FUNCTION", "GROUP", "HAVING", "IN", "INDEX",
    "INNER", "INSERT", "INTO", "IS", "JOIN", "KEY", "LEFT", "LIKE", "NOT", "NULL", "OF", "ON", "OPEN", "OPTION", "OR",
    "ORDER", "OUTER", "PRIMARY", "PRINT", "PUBLIC", "REFERENCES", "RIGHT", "SCHEMA", "SELECT", "SET", "TABLE", "TO",
    "TOP", "TRAN", "UNION", "UNIQUE", "UPDATE", "USER", "VALUES", "VIEW", "WHEN", "WHERE", "WITH",
}
_SNOWFLAKE_RESERVED = {
    "ACCOUNT", "ALL", "ALTER", "AND", "ANY", "AS", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN", "CONNECT",
    "CONSTRAINT", "CREATE", "CURRENT", "DELETE", "DISTINCT", "DROP", "ELSE", "EXISTS", "FALSE", "FOLLOWING", "FOR",
    "FROM", "FULL", "GRANT", "GROUP", "GSCLUSTER", "HAVING", "ILIKE", "IN", "INCREMENT", "INNER", "INSERT", "INTERSECT",
    "INTO", "IS", "ISSUE", "JOIN", "LATERAL", "LEFT", "LIKE", "LOCALTIME", "LOCALTIMESTAMP", "MINUS", "NATURAL", "NOT",
    "NULL", "OF", "ON", "OR", "ORDER", "ORGANIZATION", "QUALIFY", "REGEXP", "REVOKE", "RIGHT", "RLIKE", "ROW", "ROWS",
    "SAMPLE", "SCHEMA", "SELECT", "SET", "SOME", "START", "TABLE", "TABLESAMPLE", "THEN", "TO", "TRIGGER", "TRUE",
    "TRY_CAST", "UNION", "UNIQUE", "UPDATE", "USING", "VALUES", "VIEW", "WHEN", "WHENEVER", "WHERE", "WITH",
}
_BIGQUERY_RESERVED = {
    "ALL", "AND", "ANY", "ARRAY", "AS", "ASC", "ASSERT_ROWS_MODIFIED", "AT", "BETWEEN", "BY", "CASE", "CAST", "COLLATE",
    "CONTAINS", "CREATE", "CROSS", "CUBE", "CURRENT", "DEFAULT", "DEFINE", "DESC", "DISTINCT", "ELSE", "END", "ENUM",
    "ESCAPE", "EXCEPT", "EXCLUDE", "EXISTS", "EXTRACT", "FALSE", "FETCH", "FOLLOWING", "FOR", "FROM", "FULL", "GROUP",
    "GROUPING", "GROUPS", "HASH", "HAVING", "IF", "IGNORE", "IN", "INNER", "INTERSECT", "INTERVAL", "INTO", "IS", "JOIN",
    "LATERAL", "LEFT", "LIKE", "LIMIT", "LOOKUP", "MERGE", "NATURAL", "NEW", "NO", "NOT", "NULL", "NULLS", "OF", "ON",
    "OR", "ORDER", "OUTER", "OVER", "PARTITION", "PRECEDING", "PROTO", "RANGE", "RECURSIVE", "RESPECT", "RIGHT", "ROLLUP",
    "ROWS", "SELECT", "SET", "SOME", "STRUCT", "TABLESAMPLE", "THEN", "TO", "TREAT", "TRUE", "UNBOUNDED", "UNION",
    "UNNEST", "USING", "WHEN", "WHERE", "WINDOW", "WITH", "WITHIN",
}


def _types(dialect: str, col: Column) -> str:
    b, n = col.base, col.length
    if dialect == "oracle":
        return {"integer": "NUMBER(10)", "date": "DATE", "timestamp": "TIMESTAMP(0)", "numeric": "NUMBER",
                "text": "CLOB"}.get(b) or f"VARCHAR2({n} CHAR)"
    if dialect == "sqlserver":
        return {"integer": "INT", "date": "DATE", "timestamp": "DATETIME2(0)", "numeric": "FLOAT",
                "text": "VARCHAR(MAX)"}.get(b) or f"VARCHAR({n})"
    if dialect == "snowflake":
        return {"integer": "NUMBER(10,0)", "date": "DATE", "timestamp": "TIMESTAMP_NTZ(0)", "numeric": "FLOAT",
                "text": "VARCHAR"}.get(b) or f"VARCHAR({n})"
    if dialect == "bigquery":
        return {"integer": "INT64", "date": "DATE", "timestamp": "DATETIME", "numeric": "FLOAT64"}.get(b, "STRING")
    raise ValueError(f"Unsupported dialect {dialect!r}; use one of {EXTRA_TARGETS}.")


def _rules(dialect: str, oracle_max_identifier: int) -> tuple[set[str], int]:
    return {"oracle": (_ORACLE_RESERVED, oracle_max_identifier), "sqlserver": (_SQLSERVER_RESERVED, 128),
            "snowflake": (_SNOWFLAKE_RESERVED, 255), "bigquery": (_BIGQUERY_RESERVED, 300)}[dialect]


def _ident(dialect: str, name: str, oracle_max_identifier: int = 30) -> str:
    reserved, limit = _rules(dialect, oracle_max_identifier)
    if len(name) > limit:
        raise IdentifierError(f"{dialect}: identifier {name!r} is {len(name)} characters; the limit is {limit}"
                              + (" (Oracle < 12.2; pass oracle_max_identifier=128 for 12.2+)" if dialect == "oracle" else "."))
    n = name.upper() if dialect in ("oracle", "snowflake") else name
    if n.upper() in reserved:
        return {"oracle": f'"{n}"', "snowflake": f'"{n}"', "sqlserver": f"[{n}]", "bigquery": f"`{n}`"}[dialect]
    return n


def generate_ddl(dialect: str, tables: Sequence[str] | None = None, schema_name: str | None = None,
                 schema: dict[str, list[Column]] | None = None, oracle_max_identifier: int = 30) -> str:
    """``CREATE TABLE`` statements for ``dialect`` from the single CDM schema source."""
    model = schema or load_cdm_schema()
    names = [t.lower() for t in (tables or model)]
    unknown = [t for t in names if t not in model]
    if unknown:
        raise LookupError(f"Tables not in the CDM v5.4 DDL: {unknown}")
    out = []
    for t in names:
        q = (f"{schema_name}." if schema_name else "") + _ident(dialect, t, oracle_max_identifier)
        if dialect == "bigquery" and schema_name:
            q = f"`{schema_name}.{t}`"
        cols = []
        for c in model[t]:
            null = "" if c.nullable else " NOT NULL"
            cols.append(f"    {_ident(dialect, c.name, oracle_max_identifier)} {_types(dialect, c)}{null}")
        out.append(f"CREATE TABLE {q} (\n" + ",\n".join(cols) + "\n);")
    return "\n\n".join(out) + "\n"


# ---- data files + loader scripts -----------------------------------------------------------------------------------

def csv_select(table: str, columns: Sequence[Column]) -> str:
    """SELECT for CSV targets: timestamps rendered as ``YYYY-MM-DD HH:MM:SS`` (no fraction) so loader masks are exact."""
    exprs = [f"strftime(\"{c.name}\", '%Y-%m-%d %H:%M:%S') AS \"{c.name}\"" if c.base == "timestamp" else f'"{c.name}"'
             for c in columns]
    return f"SELECT {', '.join(exprs)} FROM {table}"


def oracle_ctl(table: str, columns: Sequence[Column], oracle_max_identifier: int = 30) -> str:
    """SQL*Loader control file: header skipped, explicit date masks (so ``NLS_DATE_FORMAT`` is irrelevant)."""
    fields = []
    for c in columns:
        name = _ident("oracle", c.name, oracle_max_identifier)
        if c.base == "date":
            fields.append(f'    {name} DATE "YYYY-MM-DD"')
        elif c.base == "timestamp":
            fields.append(f'    {name} TIMESTAMP "YYYY-MM-DD HH24:MI:SS"')
        elif c.base == "text":
            fields.append(f"    {name} CHAR(1000000)")
        elif c.base == "varchar":
            fields.append(f"    {name} CHAR({min(int(c.length or 255) * 4, 4000)})")
        else:
            fields.append(f"    {name} CHAR(64)")
    return (f"-- NLS-independent: every DATE/TIMESTAMP field carries an explicit mask.\n"
            f"OPTIONS (SKIP=1, DIRECT=TRUE)\nLOAD DATA\nCHARACTERSET UTF8\nINFILE '{table}.csv'\n"
            f"APPEND INTO TABLE {_ident('oracle', table, oracle_max_identifier)}\n"
            f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\nTRAILING NULLCOLS\n(\n" + ",\n".join(fields) + "\n)\n")


def sqlserver_load_script(tables: Sequence[str], data_dir: str = ".", schema_name: str = "dbo") -> str:
    base = data_dir.rstrip("/\\")
    lines = ["-- Run after omop_sqlserver_ddl.sql. Requires SQL Server 2017+ (FORMAT = 'CSV'); paths are as seen by the server."]
    for t in tables:
        lines.append(f"BULK INSERT {schema_name}.{_ident('sqlserver', t)}\nFROM '{base}/{t}.csv'\n"
                     "WITH (FORMAT = 'CSV', FIRSTROW = 2, FIELDQUOTE = '\"', FIELDTERMINATOR = ',', ROWTERMINATOR = '0x0a', "
                     "CODEPAGE = '65001', KEEPNULLS, TABLOCK);\nGO")
    return "\n\n".join(lines) + "\n"


def snowflake_load_script(tables: Sequence[str], data_dir: str = ".", stage: str = "OMOP_STAGE") -> str:
    base = data_dir.rstrip("/\\")
    lines = [f"-- Run in SnowSQL (PUT is a client command). Run omop_snowflake_ddl.sql first.",
             f"CREATE STAGE IF NOT EXISTS {stage} FILE_FORMAT = (TYPE = PARQUET);"]
    for t in tables:
        lines.append(f"PUT file://{base}/{t}.parquet @{stage} AUTO_COMPRESS = FALSE OVERWRITE = TRUE;")
    for t in tables:
        lines.append(f"COPY INTO {_ident('snowflake', t)} FROM @{stage}/{t}.parquet\n"
                     "  FILE_FORMAT = (TYPE = PARQUET) MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE ON_ERROR = ABORT_STATEMENT;")
    return "\n".join(lines) + "\n"


def bigquery_load_script(tables: Sequence[str], data_dir: str = ".", dataset: str = "omop") -> str:
    base = data_dir.rstrip("/\\")
    lines = ["#!/usr/bin/env bash", "set -euo pipefail",
             f"# Run omop_bigquery_ddl.sql first (bq query --use_legacy_sql=false < omop_bigquery_ddl.sql); dataset: {dataset}"]
    for t in tables:
        lines.append(f"bq load --source_format=PARQUET --replace {dataset}.{t} {base}/{t}.parquet")
    return "\n".join(lines) + "\n"


# ---- linting ----------------------------------------------------------------------------------------------------------

_ALLOWED = {
    "oracle": re.compile(r"^(NUMBER(\(\d+\))?|DATE|TIMESTAMP\(0\)|CLOB|VARCHAR2\(\d+ CHAR\))$"),
    "sqlserver": re.compile(r"^(INT|DATE|DATETIME2\(0\)|FLOAT|VARCHAR\((MAX|\d+)\))$"),
    "snowflake": re.compile(r"^(NUMBER\(10,0\)|DATE|TIMESTAMP_NTZ\(0\)|FLOAT|VARCHAR(\(\d+\))?)$"),
    "bigquery": re.compile(r"^(INT64|DATE|DATETIME|FLOAT64|STRING)$"),
}


def lint_ddl(sql: str, dialect: str, oracle_max_identifier: int = 30) -> list[str]:
    """Static checks; returns a list of problems (empty = clean). With ``sqlglot`` installed every statement is also parsed."""
    problems: list[str] = []
    reserved, limit = _rules(dialect, oracle_max_identifier)
    stmts = [s for s in sql.split(";") if s.strip()]
    if not stmts:
        return ["no statements"]
    for s in (x.strip() for x in stmts):
        head = re.search(r"CREATE TABLE\s+(\S+)\s*\(", s)
        if not head:
            problems.append(f"not a CREATE TABLE: {s.strip()[:40]!r}")
            continue
        if s.count("(") < s.count(")"):
            problems.append(f"unbalanced parentheses in {head.group(1)}")
        for line in s.splitlines()[1:]:
            line = line.strip().rstrip(",")
            if not line or line == ")":
                continue
            m = re.match(r'^(?P<id>"[^"]+"|\[[^\]]+\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s+(?P<type>.+?)(\s+NOT NULL)?$', line)
            if not m:
                problems.append(f"{head.group(1)}: cannot parse column line {line!r}")
                continue
            ident = m.group("id").strip('"[]`')
            if len(ident) > limit:
                problems.append(f"{head.group(1)}.{ident}: identifier longer than {limit}")
            if ident.upper() in reserved and m.group("id") == ident:
                problems.append(f"{head.group(1)}.{ident}: unquoted reserved word")
            if not _ALLOWED[dialect].match(m.group("type").strip()):
                problems.append(f"{head.group(1)}.{ident}: unexpected type {m.group('type')!r}")
    try:
        import sqlglot

        read = {"oracle": "oracle", "sqlserver": "tsql", "snowflake": "snowflake", "bigquery": "bigquery"}[dialect]
        for s in (x.strip() for x in stmts):
            try:
                sqlglot.parse_one(s, read=read)
            except Exception as exc:  # pragma: no cover - only with sqlglot
                problems.append(f"sqlglot({read}): {str(exc)[:120]}")
    except ImportError:
        pass
    return problems
