"""Live check of ``to_sql`` / ``verify_transfer`` against a database server (written for Oracle; runs on any SQLAlchemy URL).

Loads a small SYNTHETIC edge-case table (NULLs, empty strings, accented and non-Latin text, dates before 1970, fractional-second timestamps,
DECIMAL, values near the BIGINT limit) into a scratch table, compares it with ``verify_transfer``, drops the scratch table and writes a short
record you can paste into an issue. No patient data is used.

The connection URL is read from an environment variable so it never appears in shell history, the command line or the record:

    $env:OMOP_SERVER_URL = "<SQLAlchemy URL: oracle+oracledb scheme, then your account, host:1521 and ?service_name=...>"   # PowerShell
    python scripts/verify_server_roundtrip.py --schema MY_TEST_SCHEMA --out roundtrip_record.md
    python scripts/verify_server_roundtrip.py --nvarchar --out roundtrip_record_nvarchar.md     # text columns as NVARCHAR2

The record names the dialect, driver and server version, the character-set settings and the per-column results. It never contains the URL, host or
user. On Oracle an empty string is stored as NULL, so the ``non_null`` count of the empty-string column is expected to differ; that row is reported
as a known difference, not a failure. Text, date, timestamp and boolean columns are also compared by value (``verify_transfer(content=True)``).
On a database whose character set is not Unicode (for example WE8MSWIN1252) the Japanese test column cannot be stored; that mismatch is reported
as a known difference and listed with the differing values, so a site learns that non-Latin text does not survive there. Exit code 1 when any
other check fails.

``--extended`` adds: ``if_exists`` (``fail`` refuses an existing table, ``append`` doubles it, ``replace`` restores it) and NaN / Infinity /
-Infinity / NULL doubles (Oracle, PostgreSQL; SQLite stores NaN as NULL, so it is skipped there). ``--schema`` exercises the ``schema=`` argument
(the record says only whether one was given, not its name). ``--scrub-test`` makes ONE deliberately failed login with a wrong password and checks that
neither it nor the real password appears in the error; it is opt-in because the server records a failed login for your account. The record keeps
only the Oracle error code from that attempt, never the message.
"""
import argparse
import os
import re
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
       CASE i % 3 WHEN 0 THEN 'plain ascii text ' || i WHEN 1 THEN 'café naïve résumé ' || i ELSE repeat('x', 900) END AS txt,
       '日本語のテキスト ' || i AS txt_nonlatin,
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


def run(url: str, schema=None, keep=False, nvarchar=False) -> tuple[pd.DataFrame, dict, list, bool]:
    con = duckdb.connect()
    con.execute(EDGE_TABLE_SQL)
    target = "OMOP_RT_" + uuid.uuid4().hex[:8].upper()
    spec = {target: "edge"}
    info = _server_info(url)
    load = to_sql(con, spec, url, schema=schema, if_exists="fail", nvarchar=nvarchar)
    try:
        res = verify_transfer(con, spec, url, schema=schema, content=True, content_examples=2)   # synthetic data, so examples are safe to print
    finally:
        if not keep:
            _drop(url, target, schema)
    res["known_difference"] = False
    if info["dialect"] == "oracle":
        res.loc[res["column"] == "txt_empty:non_null", "known_difference"] = True
        if not nvarchar and str(info.get("NLS_CHARACTERSET", "")).upper() not in ("AL32UTF8", "UTF8", ""):
            # a non-Unicode database character set cannot store the Japanese text; it is replaced on the way in. Reported, not hidden.
            res.loc[res["column"] == "txt_nonlatin:content", "known_difference"] = True
    failed = res[(~res["match"]) & (~res["known_difference"])]
    return res, info, load, failed.empty


def _failed(res: pd.DataFrame, info: dict, nvarchar: bool) -> pd.DataFrame:
    """Mismatching rows that are not one of the documented, expected differences."""
    res = res.copy()
    res["known_difference"] = False
    if info["dialect"] == "oracle":
        res.loc[res["column"] == "txt_empty:non_null", "known_difference"] = True
        if not nvarchar and str(info.get("NLS_CHARACTERSET", "")).upper() not in ("AL32UTF8", "UTF8", ""):
            res.loc[res["column"] == "txt_nonlatin:content", "known_difference"] = True
    return res[(~res["match"]) & (~res["known_difference"])]


def run_extended(url: str, info: dict, schema=None, nvarchar=False, scrub_test=False) -> list[tuple[str, bool | None, str]]:
    """Extra checks as (name, passed, note); passed None means skipped. Every scratch table it creates is dropped."""
    sa = _sa()
    out: list[tuple[str, bool | None, str]] = []
    con = duckdb.connect()
    con.execute(EDGE_TABLE_SQL)
    qual = (schema + ".") if schema else ""

    # if_exists: fail / append / replace
    target = "OMOP_RX_" + uuid.uuid4().hex[:8].upper()
    spec = {target: "edge"}
    doubled = {target: "SELECT * FROM edge UNION ALL SELECT * FROM edge"}
    try:
        to_sql(con, spec, url, schema=schema, nvarchar=nvarchar)
        try:
            to_sql(con, spec, url, schema=schema, if_exists="fail", nvarchar=nvarchar)
            out.append(("if_exists='fail' refuses an existing table", False, "no error was raised"))
        except ValueError as e:
            out.append(("if_exists='fail' refuses an existing table", "already exists" in str(e), "ValueError"))
        to_sql(con, spec, url, schema=schema, if_exists="append", nvarchar=nvarchar)
        bad = _failed(verify_transfer(con, doubled, url, schema=schema, content=True), info, nvarchar)
        out.append(("if_exists='append' doubles the rows (remote equals local twice)", bad.empty, "" if bad.empty else f"{len(bad)} mismatching checks"))
        to_sql(con, spec, url, schema=schema, if_exists="replace", nvarchar=nvarchar)
        bad = _failed(verify_transfer(con, spec, url, schema=schema, content=True), info, nvarchar)
        out.append(("if_exists='replace' restores the original rows", bad.empty, "" if bad.empty else f"{len(bad)} mismatching checks"))
    except Exception as e:
        out.append(("if_exists checks ran to completion", False, type(e).__name__))
    finally:
        try:
            _drop(url, target, schema)
        except Exception:
            pass

    # NaN / Infinity / NULL doubles
    if info["dialect"] in ("oracle", "postgresql"):
        con.execute("CREATE TABLE nan_edge AS SELECT * FROM (VALUES (1, 1.5::DOUBLE), (2, 'NaN'::DOUBLE), (3, 'Infinity'::DOUBLE), "
                    "(4, '-Infinity'::DOUBLE), (5, NULL::DOUBLE)) t(id, v)")
        t2 = "OMOP_RN_" + uuid.uuid4().hex[:8].upper()
        try:
            to_sql(con, {t2: "nan_edge"}, url, schema=schema)
            engine = _make_engine(url)
            try:
                with engine.connect() as c:
                    rows = c.execute(sa.text(f"SELECT id, v FROM {qual}{_server_name(t2, info['dialect'])} ORDER BY id")).fetchall()
            finally:
                engine.dispose()

            def kind(v):
                return None if v is None else ("nan" if v != v else ("+inf" if v == float("inf") else ("-inf" if v == float("-inf") else float(v))))

            got = [kind(v) for _, v in rows]
            want = [1.5, "nan", "+inf", "-inf", None]
            out.append(("NaN, +Infinity, -Infinity, NULL and 1.5 read back unchanged", got == want, f"read back: {got}"))
            bad = verify_transfer(con, {t2: "nan_edge"}, url, schema=schema)
            out.append(("verify_transfer on the NaN/Infinity table", bool(bad["match"].all()), "" if bad["match"].all() else "mismatching checks: " + ", ".join(bad.loc[~bad["match"], "column"])))
        except Exception as e:
            out.append(("NaN / Infinity checks ran to completion", False, type(e).__name__))
        finally:
            try:
                _drop(url, t2, schema)
            except Exception:
                pass
    else:
        out.append(("NaN / Infinity round trip", None, f"skipped: {info['dialect']} does not store NaN"))

    # credential scrubbing with the real driver
    if scrub_test:
        out.append(_scrub_check(url, con))
    return out


def _scrub_check(url: str, con) -> tuple[str, bool | None, str]:
    from sqlalchemy.engine import make_url

    name = "wrong-password error shows neither the wrong nor the real password"
    u = make_url(url)
    if not u.password:
        return name, None, "skipped: the URL has no password"
    marker = "WrongPw_marker_9f3a"
    try:
        to_sql(con, ["edge"], u.set(password=marker).render_as_string(hide_password=False), schema=None)
    except Exception as e:
        msg = str(e)
        codes = sorted(set(re.findall(r"(?:ORA|DPY|DPI)-\d+", msg)))
        leaked = [w for w, p in (("wrong", marker), ("real", u.password)) if p and p in msg]
        return name, not leaked, f"error codes: {', '.join(codes) or 'none'}; leaked: {', '.join(leaked) or 'none'}"
    return name, False, "the login with a wrong password did not fail"


def render(res: pd.DataFrame, info: dict, load: list, ok: bool, extended=None, schema_given=False) -> str:
    lines = ["# Server round-trip record", "", f"Result: **{'PASS' if ok else 'FAIL'}**", "", "## Server", ""]
    lines += [f"- {k}: {v}" for k, v in info.items()]
    lines += [f"- explicit schema= given: {'yes' if schema_given else 'no (account default)'}"]
    lines += ["", "## Load", ""] + [f"- {r['table']}: {r['rows']} rows in {r['seconds']} s" for r in load]
    lines += ["", "## Checks", "", "| column / statistic | local | remote | match |", "|---|---|---|---|"]
    for _, r in res.iterrows():
        tag = "known difference" if r["known_difference"] and not r["match"] else ("yes" if r["match"] else "NO")
        lines.append(f"| {r['column']} | {r['local']} | {r['remote']} | {tag} |")
    if "detail" in res.columns and res["detail"].notna().any():
        lines += ["", "## Differing values (synthetic data)", ""]
        lines += [f"- {r['column']}: {r['detail']}" for _, r in res[res["detail"].notna()].iterrows()]
    if extended:
        lines += ["", "## Extended checks", "", "| check | result | note |", "|---|---|---|"]
        lines += [f"| {n} | {'skipped' if p is None else ('PASS' if p else 'FAIL')} | {note} |" for n, p, note in extended]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url-env", default="OMOP_SERVER_URL", help="environment variable holding the SQLAlchemy URL (default OMOP_SERVER_URL)")
    ap.add_argument("--schema", help="schema to create the scratch table in (default: the account's own)")
    ap.add_argument("--keep", action="store_true", help="leave the scratch table in place")
    ap.add_argument("--nvarchar", action="store_true", help="create text columns as NVARCHAR2 (to_sql(nvarchar=True)); then the non-Latin column must match too")
    ap.add_argument("--extended", action="store_true", help="also check if_exists fail/append/replace and NaN / Infinity doubles")
    ap.add_argument("--scrub-test", action="store_true", help="make ONE failed login with a wrong password and check no password appears in the error (opt-in: the server logs a failed login)")
    ap.add_argument("--out", help="write the record (Markdown) to this file")
    a = ap.parse_args(argv)
    url = os.environ.get(a.url_env)
    if not url:
        ap.error(f"environment variable {a.url_env} is not set")
    res, info, load, ok = run(url, schema=a.schema, keep=a.keep, nvarchar=a.nvarchar)
    extended = run_extended(url, info, schema=a.schema, nvarchar=a.nvarchar, scrub_test=a.scrub_test) if (a.extended or a.scrub_test) else None
    if extended:
        ok = ok and all(p is not False for _, p, _ in extended)
    text = render(res, info, load, ok, extended=extended, schema_given=bool(a.schema))
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
