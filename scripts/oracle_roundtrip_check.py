"""Copies a site's live Oracle tables into DuckDB (and optionally an embedded PostgreSQL backup) and proves every copy equals Oracle.

Reads Oracle only, inside one ``SET TRANSACTION READ ONLY`` transaction: the server refuses any write, and every query sees the same
snapshot while the site keeps loading data. For each table:

1. **Oracle -> DuckDB.** Oracle computes a fingerprint of the table (per column: counts, exact sums / min / max, and an order-free sum of
   MD5 hashes of every value; see ``omop_etl.oracle_roundtrip``), the rows are copied into ``<out-dir>/<site>.duckdb`` with exact types, and
   DuckDB computes the same fingerprint on the copy. ``--row-level`` also reads each table a second time through python-oracledb's own
   Arrow path and compares it with the copy row for row (``EXCEPT ALL`` both ways); it doubles the reading.
2. **DuckDB -> PostgreSQL** (``--pg-dir``, needs ``omop-duck-db[postgres-embedded]`` and an ordinary, non-admin user). The copy is stored in
   schema ``oracle_<site>`` of an embedded PostgreSQL kept in that folder (``store_site``, itself checked against DuckDB), then PostgreSQL
   computes the fingerprint and it is compared with **Oracle's**. ``--read-back`` also copies that schema back into a new DuckDB file and
   compares it with Oracle's fingerprint, so Oracle -> DuckDB -> PostgreSQL -> DuckDB is proved end to end. ``--skip-duckdb-pg-check``
   skips ``store_site``'s own DuckDB-vs-PostgreSQL check, which reads each column separately and took most of the PostgreSQL time on a
   real site; the Oracle-vs-PostgreSQL fingerprint, one scan per table, already covers it.
3. **DuckDB -> Oracle** (``--write-back``, WRITES to Oracle; off by default; use a scratch schema). Each table is loaded into a new
   ``OMOP_RT_<NAME>`` table with ``to_sql``, fingerprinted by Oracle, compared, and dropped.

Oracle's fingerprint is saved next to the copy (``<site>.duckdb.oracle-fingerprint.json``), so the DuckDB file and the PostgreSQL backup
can be re-checked later without connecting to Oracle (``omop_etl.oracle_roundtrip.verify_duckdb_copy`` / ``verify_postgres_copy``).

The connection URL is read from an environment variable, never from the command line, and never appears in output or the record::

    $env:OMOP_ORACLE_URL = "<oracle+oracledb URL: account, host:1521, ?service_name=...>"        # PowerShell
    python scripts/oracle_roundtrip_check.py --schema CDM_OWNER --tables all --label "Site A" \
        --out-dir ../private/oracle_mirror --pg-dir ../private/postgres --read-back --row-level --out oracle_roundtrip_record.md

``--tables`` takes ``all`` or names (case-insensitive, views allowed), each optionally ``out_name=ORACLE_TABLE``; ``--table-map`` (JSON or
CSV ``output,oracle``) renames many at once, for example to drop a site prefix from table names before anything is written.

The record (``--out``) names the site by its anonymous label only, never the schema, server, account or host, and has no row counts: per
table it lists PASS / FAIL and, on failure, which column statistic differed. Check it with ``scripts/check_run_privacy.py`` before sharing.
Keep ``--out-dir`` and ``--pg-dir`` with the site's private files: they hold patient-level data. Exit code 1 when any check fails.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.oracle_roundtrip import mirror_oracle, verify_duckdb_copy, verify_postgres_copy, write_back  # noqa: E402


def _slug(label: str) -> str:
    from omop_etl.embedded_postgres import site_schema

    return site_schema(label)


def _failures(rep) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rep.loc[~rep["match"]].itertuples(index=False):
        out.setdefault(r.table, []).append(f"{r.column}:{r.stat}")
    return out


def _section(title: str, rep, tables: list[str], extra: str = "") -> tuple[list[str], bool]:
    bad = _failures(rep)
    ok = not bad and len(rep) > 0
    lines = ["", f"### {title}", "", f"Result: **{'PASS' if ok else 'FAIL'}**{extra}", "", "| table | result | differing statistics |",
             "|---|---|---|"]
    for t in tables:
        lines.append(f"| {t} | {'FAIL' if t in bad else 'PASS'} | {', '.join(bad.get(t, [])[:12]) + (' ...' if len(bad.get(t, [])) > 12 else '')} |")
    return lines, ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url-env", default="OMOP_ORACLE_URL", help="environment variable holding the SQLAlchemy URL (default OMOP_ORACLE_URL)")
    ap.add_argument("--schema", help="Oracle owner of the tables (default: the account's current schema); never written to the record")
    ap.add_argument("--tables", nargs="+", default=["all"], help="'all' (default) or table names, each optionally out_name=ORACLE_TABLE")
    ap.add_argument("--table-map", help="JSON or CSV (output,oracle) renaming tables in the copy")
    ap.add_argument("--label", default="Site A", help="anonymous site label for file names and the record (default 'Site A')")
    ap.add_argument("--out-dir", required=True, help="private folder for <site>.duckdb and its Oracle fingerprint (the file must not exist)")
    ap.add_argument("--row-level", action="store_true", help="also re-read every table and compare row for row (reads Oracle twice)")
    ap.add_argument("--no-snapshot", action="store_true", help="do not use one read-only transaction (rows loaded meanwhile then show as differences)")
    ap.add_argument("--chunksize", type=int, default=100_000)
    ap.add_argument("--pg-dir", help="also store the copy in an embedded PostgreSQL kept here and compare it with Oracle")
    ap.add_argument("--pg-backend", default="auto", choices=["auto", "pg0", "pgembed"])
    ap.add_argument("--skip-duckdb-pg-check", action="store_true",
                    help="with --pg-dir: skip store_site's own DuckDB-vs-PostgreSQL check (slow: one scan per column); "
                         "the Oracle-vs-PostgreSQL fingerprint still runs")
    ap.add_argument("--read-back", action="store_true", help="with --pg-dir: copy PostgreSQL back into <site>_from_postgres.duckdb and compare with Oracle")
    ap.add_argument("--write-back", action="store_true", help="WRITES to Oracle: load the copy into OMOP_RT_<NAME> tables, compare, drop them")
    ap.add_argument("--write-back-schema", help="scratch schema for --write-back (default: the account's own)")
    ap.add_argument("--out", help="write the Markdown record here")
    a = ap.parse_args(argv)
    if not os.environ.get(a.url_env):
        ap.error(f"environment variable {a.url_env} is not set")
    if a.read_back and not a.pg_dir:
        ap.error("--read-back needs --pg-dir")
    tables = "all" if [t.lower() for t in a.tables] == ["all"] else a.tables
    slug = _slug(a.label)
    out_dir = Path(a.out_dir)
    mirror = out_dir / f"{slug}.duckdb"
    if mirror.exists():
        ap.error(f"{mirror.name} already exists in --out-dir; use a new folder so nothing is overwritten")

    t_all = time.time()
    lines: list[str] = []
    all_ok = True
    print(f"[{a.label}] Oracle -> DuckDB ...", flush=True)
    t0 = time.time()
    rep = mirror_oracle(f"env:{a.url_env}", mirror, schema=a.schema, tables=tables, table_map=a.table_map, chunksize=a.chunksize,
                        row_level=a.row_level, snapshot=not a.no_snapshot, progress=lambda s: print(f"  {s}", flush=True))
    names = [t["table"] for t in rep.attrs["tables"]]
    info = rep.attrs["info"]
    sec, ok = _section("Oracle -> DuckDB (fingerprint computed by each database" + (", plus row-level comparison" if a.row_level else "") + ")",
                       rep, names, f" ({len(names)} tables, {len(rep)} column statistics, {time.time() - t0:.0f} s)")
    lines += sec
    all_ok &= ok

    if a.pg_dir:
        from omop_etl.embedded_postgres import EmbeddedPostgres, RoundTripError, load_site, store_site

        schema = "oracle_" + slug
        print(f"[{a.label}] DuckDB -> PostgreSQL ({schema}) ...", flush=True)
        t0 = time.time()
        with EmbeddedPostgres(a.pg_dir, backend=a.pg_backend) as pg:
            server = pg.query("SHOW server_version").iloc[0, 0]
            try:
                store_site(pg, mirror, schema, tables=names, verify=not a.skip_duckdb_pg_check)
                stored = ("store_site check against DuckDB skipped (--skip-duckdb-pg-check)" if a.skip_duckdb_pg_check
                          else "store_site check against DuckDB passed")
            except RoundTripError as exc:
                stored = f"store_site check against DuckDB FAILED in {len(exc.report.loc[~exc.report['match']]) if exc.report is not None else '?'} statistics"
                all_ok = False
            vp = verify_postgres_copy(pg, schema, mirror)
            sec, ok = _section(f"Oracle vs PostgreSQL copy (embedded PostgreSQL {server}, schema `{schema}`)", vp, names,
                               f" ({stored}; {time.time() - t0:.0f} s)")
            lines += sec
            all_ok &= ok
            if a.read_back:
                back = out_dir / f"{slug}_from_postgres.duckdb"
                print(f"[{a.label}] PostgreSQL -> DuckDB ...", flush=True)
                t0 = time.time()
                if back.exists():
                    back.unlink()
                load_site(pg, schema, back, tables=names)
                vb = verify_duckdb_copy(back, mirror)
                sec, ok = _section("Oracle vs DuckDB read back from PostgreSQL", vb, names, f" ({time.time() - t0:.0f} s)")
                lines += sec
                all_ok &= ok

    if a.write_back:
        print(f"[{a.label}] DuckDB -> Oracle scratch tables (writes) ...", flush=True)
        t0 = time.time()
        wb = write_back(f"env:{a.url_env}", mirror, schema=a.write_back_schema)
        sec, ok = _section("DuckDB -> Oracle scratch tables, compared with the original (dropped afterwards)", wb, names,
                           f" ({time.time() - t0:.0f} s)")
        lines += sec
        all_ok &= ok

    import duckdb

    head = ["# Oracle round trip", "",
            f"- Date: {_dt.date.today().isoformat()}",
            f"- Site: {a.label}",
            f"- Oracle: {info.get('server_version')}; NLS_CHARACTERSET {info.get('NLS_CHARACTERSET')}; "
            f"read-only snapshot: {'yes' if info.get('read_only_snapshot') else 'no'}",
            f"- DuckDB {duckdb.__version__}; Python {platform.python_version()} on {platform.system()} {platform.machine()}",
            f"- Tables: {len(names)} ({'all of the owner' if tables == 'all' else 'listed'}); row-level re-read: {'yes' if a.row_level else 'no'}; "
            f"PostgreSQL: {'yes' if a.pg_dir else 'no'}; write-back: {'yes' if a.write_back else 'no'}",
            f"- Result: **{'PASS' if all_ok else 'FAIL'}** ({time.time() - t_all:.0f} s)"]
    legend = ["", "Statistics: `n` non-null count, `sum` / `min` / `max` exact (numbers; dates and times as microseconds since 1970), `hash` "
              "order-free sum of value hashes (any changed value changes it), `len` total characters, `sub_us` values with a fraction finer "
              "than a microsecond (DuckDB keeps microseconds, so a non-zero Oracle count is reported as a loss), `row_level` rows found on "
              "only one side when re-read. `(rows)` is the table's row count. No counts or values appear in this record."]
    text = "\n".join(head + lines + legend) + "\n"
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
