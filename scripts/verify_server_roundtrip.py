"""Live check of ``to_sql`` / ``verify_transfer`` against a database server (written for Oracle; runs on any SQLAlchemy URL).

Loads a small SYNTHETIC edge-case table (NULLs, empty strings, accented and non-Latin text, dates before 1970, fractional-second timestamps,
DECIMAL, values near the BIGINT limit) into a scratch table, compares it with ``verify_transfer``, drops the scratch table and writes a short
record you can paste into an issue. No patient data is used.

The connection URL is read from an environment variable so it never appears in shell history, the command line or the record:

    $env:OMOP_SERVER_URL = "<SQLAlchemy URL: oracle+oracledb scheme, then your account, host:1521 and ?service_name=...>"   # PowerShell
    python scripts/verify_server_roundtrip.py --schema MY_TEST_SCHEMA --out roundtrip_record.md

The record names the dialect, driver and server version, the character-set settings and the per-column results. It never contains the URL, host or
user. On Oracle an empty string is stored as NULL, so the ``non_null`` count of the empty-string column is expected to differ; that row is reported
as a known difference, not a failure. Exit code 1 when any other check fails.
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.load_sql import _make_engine, _sa, _server_name, to_sql, verify_transfer  # noqa: E402

EDGE_TABLE_SQL = """
CREATE TABLE edge AS
SELECT i::BIGINT AS id,
       (CASE WHEN i = 0 THEN 4611686018427387904 WHEN i = 1 THEN -4611686018427387904 ELSE 0 END)::BIGINT AS near_limit,  -- 2^62: the sum stays in range
       (i * 0.1)::DOUBLE AS dbl,
       (i * 1.2345)::DECIMAL(18, 4) AS dec,
       (i % 2 = 0) AS flag,
       DATE '1960-01-01' + (i % 20000)::INTEGER AS d,
       TIMESTAMP '1999-12-31 23:59:59.123456' + to_seconds(i) AS ts,
       CASE i % 4 WHEN 0 THEN 'plain ascii text ' || i WHEN 1 THEN 'café naïve résumé ' || i WHEN 2 THEN '日本語のテキスト ' || i ELSE repeat('x', 900) END AS txt,
       CASE WHEN i % 3 = 0 THEN '' ELSE NULL END AS txt_empty,
       CAST(NULL AS BIGINT) AS all_null
FROM range(2000) t(i)
"""


def _server_info(url: str) -> dict:
    sa = _sa()
    engine = _make_engine(url)
    info = {"dialect": engine.dialect.name, "driver": engine.dialect.driver}
    try:
        with engine.connect() as c:
            if engine.dialect.name == "oracle":
                try:
                    info["server_version"] = c.execute(sa.text("SELECT banner FROM v$version WHERE ROWNUM = 1")).scalar()
                except Exception:
                    info["server_version"] = "not readable with this account"
                try:
                    rows = c.execute(sa.text("SELECT parameter, value FROM nls_database_parameters WHERE parameter IN "
                                             "('NLS_CHARACTERSET', 'NLS_NCHAR_CHARACTERSET', 'NLS_LENGTH_SEMANTICS')")).fetchall()
                    info.update({k: v for k, v in rows})
                except Exception:
                    info["nls"] = "not readable with this account"
            else:
                try:
                    info["server_version"] = str(c.execute(sa.text("SELECT sqlite_version()" if engine.dialect.name == "sqlite" else "SELECT version()")).scalar())
                except Exception:
                    info["server_version"] = "unknown"
    finally:
        engine.dispose()
    return info


def _drop(url: str, name: str, schema):
    sa = _sa()
    engine = _make_engine(url)
    try:
        with engine.begin() as c:
            c.execute(sa.text(f"DROP TABLE {(schema + '.') if schema else ''}{_server_name(name, engine.dialect.name)}"))
    finally:
        engine.dispose()


def run(url: str, schema=None, keep=False) -> tuple[pd.DataFrame, dict, list, bool]:
    con = duckdb.connect()
    con.execute(EDGE_TABLE_SQL)
    target = "OMOP_RT_" + uuid.uuid4().hex[:8].upper()
    spec = {target: "edge"}
    info = _server_info(url)
    load = to_sql(con, spec, url, schema=schema, if_exists="fail")
    try:
        res = verify_transfer(con, spec, url, schema=schema)
    finally:
        if not keep:
            _drop(url, target, schema)
    res["known_difference"] = False
    if info["dialect"] == "oracle":
        res.loc[res["column"] == "txt_empty:non_null", "known_difference"] = True
    failed = res[(~res["match"]) & (~res["known_difference"])]
    return res, info, load, failed.empty


def render(res: pd.DataFrame, info: dict, load: list, ok: bool) -> str:
    lines = ["# Server round-trip record", "", f"Result: **{'PASS' if ok else 'FAIL'}**", "", "## Server", ""]
    lines += [f"- {k}: {v}" for k, v in info.items()]
    lines += ["", "## Load", ""] + [f"- {r['table']}: {r['rows']} rows in {r['seconds']} s" for r in load]
    lines += ["", "## Checks", "", "| column / statistic | local | remote | match |", "|---|---|---|---|"]
    for _, r in res.iterrows():
        tag = "known difference" if r["known_difference"] and not r["match"] else ("yes" if r["match"] else "NO")
        lines.append(f"| {r['column']} | {r['local']} | {r['remote']} | {tag} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url-env", default="OMOP_SERVER_URL", help="environment variable holding the SQLAlchemy URL (default OMOP_SERVER_URL)")
    ap.add_argument("--schema", help="schema to create the scratch table in (default: the account's own)")
    ap.add_argument("--keep", action="store_true", help="leave the scratch table in place")
    ap.add_argument("--out", help="write the record (Markdown) to this file")
    a = ap.parse_args(argv)
    url = os.environ.get(a.url_env)
    if not url:
        ap.error(f"environment variable {a.url_env} is not set")
    res, info, load, ok = run(url, schema=a.schema, keep=a.keep)
    text = render(res, info, load, ok)
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
