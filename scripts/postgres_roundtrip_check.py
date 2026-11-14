"""Stores each site's DuckDB database in an embedded PostgreSQL (no install, no admin rights) and proves the round trip is lossless.

For every site, in one local PostgreSQL server kept in ``--pg-dir``:

1. **DuckDB -> PostgreSQL.** Every table of ``<db-dir>/omop_<site>.duckdb`` is copied into schema ``site_a`` (from the site's anonymous
   label) and compared column by column: row count, non-null count, sum / min / max of numbers, and a value fingerprint of text, dates,
   timestamps and booleans.
2. **PostgreSQL -> DuckDB.** The schema is copied back into ``<out-db-dir>/omop_<site>.duckdb`` (same file names, so
   ``reproduce_study.py --db-dir <out-db-dir>`` runs unchanged) and compared with the original **row for row** (``EXCEPT ALL`` both ways).
3. **Raw text files** (``--raw-dirs``, one folder per site, same order as ``--sites``): loaded into schema ``raw_site_a`` by PostgreSQL's own
   ``COPY`` and compared value by value with DuckDB's ``read_csv`` of the same files, so two independent CSV readers agree on the raw data.
4. **Delta Lake** (``--delta-dir``, needs ``deltalake``): each table is written as version 0 (original DuckDB) and version 1 (after the
   PostgreSQL round trip) of one Delta table, then the two versions are read back and compared row for row. A lossless round trip leaves
   no delta between the versions.

The record (``--out``, Markdown) names sites by their anonymous labels only and lists PASS / FAIL per table, with no row counts. Exit code 1
when any check fails. Then compare study results the usual way::

    python scripts/reproduce_study.py --db-dir <out-db-dir> --out run_pg ... (same arguments as the baseline run)
    python scripts/compare_study_deltas.py --new run_pg/<prefix>_Manuscript_Tables.xlsx --baseline run_base/<prefix>_Manuscript_Tables.xlsx --out-dir run_pg

Example (PowerShell or bash; run as an ordinary user, PostgreSQL refuses to run as root or Administrator-elevated):

    pip install "omop-duck-db[postgres-embedded,delta]"
    python scripts/postgres_roundtrip_check.py --db-dir derived/omop_duckdb --sites site1 site2 site3 \
        --labels-file ../private/labels.json --pg-dir ../private/postgres --out-db-dir ../private/from_postgres \
        --raw-dirs ../raw/site1 ../raw/site2 ../raw/site3 --delta-dir ../private/delta --out pg_roundtrip_record.md

``--labels-file`` is the private site -> label JSON from ``reproduce_study.py --labels-out``; without it the sites are labelled Site A, B, ...
in the order given. Keep ``--pg-dir``, ``--out-db-dir`` and ``--delta-dir`` with the site's private files: they hold patient-level data.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.embedded_postgres import (  # noqa: E402
    EmbeddedPostgres,
    RoundTripError,
    compare_duckdb,
    load_site,
    load_text_files,
    site_schema,
    store_site,
)


def _labels(sites, labels_file):
    if labels_file:
        mapping = json.loads(Path(labels_file).read_text(encoding="utf-8"))
        missing = [s for s in sites if s not in mapping]
        if missing:
            raise SystemExit(f"{len(missing)} site(s) have no label in --labels-file (positions {[sites.index(s) + 1 for s in missing]}).")
        return {s: mapping[s] for s in sites}
    from omop_etl.study_run import assign_site_labels

    return assign_site_labels(sites, mode="ordered")


def delta_versions(original, roundtrip, uri: Path, tables) -> list[tuple[str, bool, str]]:
    """Writes each table of ``original`` as Delta version 0 and of ``roundtrip`` as version 1, then compares the two versions."""
    import duckdb
    from deltalake import DeltaTable

    from omop_etl.lakehouse import export_to_delta_lake

    out = []
    a, b = duckdb.connect(str(original), read_only=True), duckdb.connect(str(roundtrip), read_only=True)
    try:
        for t in tables:
            target = uri / t
            if target.exists():
                import shutil

                shutil.rmtree(target)
            types = a.execute(f'DESCRIBE "{t}"').fetchall()
            cols = ", ".join(f'CAST("{c}" AS {ty}) AS "{c}"' for c, ty, *_ in types)
            export_to_delta_lake(a.sql(f'SELECT {cols} FROM "{t}"'), target)                 # version 0
            export_to_delta_lake(b.sql(f'SELECT {cols} FROM "{t}"'), target, mode="overwrite")  # version 1
            v0 = DeltaTable(str(target), version=0).to_pyarrow_table()
            v1 = DeltaTable(str(target), version=1).to_pyarrow_table()
            con = duckdb.connect()
            con.register("v0", v0)
            con.register("v1", v1)
            d01 = con.execute("SELECT count(*) FROM (SELECT * FROM v0 EXCEPT ALL SELECT * FROM v1)").fetchone()[0]
            d10 = con.execute("SELECT count(*) FROM (SELECT * FROM v1 EXCEPT ALL SELECT * FROM v0)").fetchone()[0]
            con.close()
            ok = d01 == 0 and d10 == 0 and v0.num_rows == v1.num_rows
            out.append((t, ok, "" if ok else "versions 0 and 1 differ"))
    finally:
        a.close()
        b.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-dir", required=True, help="folder with omop_<site>.duckdb")
    ap.add_argument("--sites", nargs="+", required=True)
    ap.add_argument("--labels-file", help="private JSON site -> anonymous label (default Site A, B, ... in --sites order)")
    ap.add_argument("--pg-dir", required=True, help="folder for the PostgreSQL data (created; kept between runs)")
    ap.add_argument("--out-db-dir", required=True, help="folder for the DuckDB files read back from PostgreSQL (must not hold them yet)")
    ap.add_argument("--raw-dirs", nargs="*", help="raw text-file folder per site, same order as --sites")
    ap.add_argument("--raw-pattern", default="*.csv", help="file pattern in each raw folder (default *.csv)")
    ap.add_argument("--delta-dir", help="also compare Delta Lake versions 0 (original) and 1 (round trip) here")
    ap.add_argument("--delta-tables", nargs="*", help="tables for the Delta check (default every table with rows; each is read into memory)")
    ap.add_argument("--tables", default="all", help="'all' (default), 'cdm', or a comma-separated list")
    ap.add_argument("--backend", default="auto", choices=["auto", "pg0", "pgembed"])
    ap.add_argument("--method", default="copy", choices=["copy", "insert"])
    ap.add_argument("--no-content", action="store_true", help="skip the value-level comparison in PostgreSQL (faster)")
    ap.add_argument("--keep-running", action="store_true", help="leave the server running afterwards (connect with the printed command)")
    ap.add_argument("--out", help="write the Markdown record here")
    a = ap.parse_args(argv)
    if a.raw_dirs and len(a.raw_dirs) != len(a.sites):
        ap.error("--raw-dirs needs one folder per site, in the order of --sites")
    tables = a.tables if a.tables in ("all", "cdm") else [t.strip() for t in a.tables.split(",") if t.strip()]
    labels = _labels(a.sites, a.labels_file)
    out_dir = Path(a.out_db_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clash = [s for s in a.sites if (out_dir / f"omop_{s}.duckdb").exists()]
    if clash:
        ap.error(f"--out-db-dir already holds {len(clash)} of the output files; use an empty folder")

    lines: list[str] = []
    failed = False
    t_all = time.time()
    pg = EmbeddedPostgres(a.pg_dir, backend=a.backend, stop_on_exit=not a.keep_running)
    with pg:
        server = pg.query("SHOW server_version").iloc[0, 0]
        for i, site in enumerate(a.sites):
            label = labels[site]
            schema = site_schema(label)
            src = Path(a.db_dir) / f"omop_{site}.duckdb"
            back = out_dir / f"omop_{site}.duckdb"
            lines += ["", f"## {label} (schema `{schema}`)", ""]
            print(f"[{label}] DuckDB -> PostgreSQL ...", flush=True)
            t0 = time.time()
            try:
                rep = store_site(pg, src, schema, tables=tables, content=not a.no_content, method=a.method)
                n_tab = len(rep.attrs["load"])
                lines.append(f"- DuckDB -> PostgreSQL: **PASS**, {n_tab} tables, {len(rep)} column checks, all equal ({time.time() - t0:.0f} s)")
            except RoundTripError as exc:
                failed = True
                bad = [] if exc.report is None else \
                    list(exc.report.loc[~exc.report["match"], ["table", "column"]].drop_duplicates().itertuples(index=False))
                lines.append(f"- DuckDB -> PostgreSQL: **FAIL** in {len(bad)} column check(s): " + ", ".join(f"`{t}.{c}`" for t, c in bad))
                continue
            print(f"[{label}] PostgreSQL -> DuckDB ...", flush=True)
            t0 = time.time()
            load_site(pg, schema, back, tables=tables)
            cmp = compare_duckdb(src, back, tables=None if tables in ("all", "cdm") else tables)
            if tables == "cdm":
                cmp = cmp[cmp["table"].isin([x["table"] for x in rep.attrs["load"]])]
            bad = cmp[~cmp["match"]]
            failed |= len(bad) > 0
            lines.append(f"- PostgreSQL -> DuckDB, compared row for row with the original: "
                         + ("**PASS**, " + f"{len(cmp)} tables identical" if bad.empty else
                            "**FAIL**: " + ", ".join(f"`{t}` ({n or 'rows differ'})" for t, n in bad[["table", "note"]].itertuples(index=False)))
                         + f" ({time.time() - t0:.0f} s)")
            if a.raw_dirs:
                print(f"[{label}] raw text files ...", flush=True)
                try:
                    r = load_text_files(pg, a.raw_dirs[i], "raw_" + schema, pattern=a.raw_pattern)
                    lines.append(f"- Raw text files: **PASS**, {len(r.attrs['files'])} files; PostgreSQL COPY and DuckDB read_csv agree on every value")
                except (RoundTripError, FileNotFoundError, ValueError) as exc:
                    failed = True
                    lines.append(f"- Raw text files: **FAIL**: {exc}")
            if a.delta_dir:
                print(f"[{label}] Delta Lake versions ...", flush=True)
                dt = a.delta_tables or [r["table"] for r in rep.attrs["load"] if r["rows"]]
                res = delta_versions(src, back, Path(a.delta_dir) / schema, dt)
                bad_d = [t for t, ok, _ in res if not ok]
                failed |= bool(bad_d)
                lines.append(f"- Delta Lake, version 0 (original) vs version 1 (round trip): "
                             + (f"**PASS**, no delta in {len(res)} tables" if not bad_d else f"**FAIL**: {', '.join(bad_d)}"))
        if a.keep_running:
            print(f"Server left running. Connect with psql or any client using the URL from EmbeddedPostgres({a.pg_dir!r}).dsn")

    import duckdb

    head = ["# DuckDB <-> PostgreSQL round trip", "",
            f"- Date: {_dt.date.today().isoformat()}",
            f"- PostgreSQL: {server} (embedded, backend `{pg.backend}`, load method `{a.method}`)",
            f"- DuckDB: {duckdb.__version__}; Python {platform.python_version()} on {platform.system()} {platform.machine()}",
            f"- Sites: {len(a.sites)}; tables: `{a.tables}`; value-level comparison: {'no' if a.no_content else 'yes'}",
            f"- Result: **{'FAIL' if failed else 'PASS'}** ({time.time() - t_all:.0f} s)"]
    text = "\n".join(head + lines) + "\n"
    print(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
