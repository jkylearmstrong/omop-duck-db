"""Keeps each site's DuckDB database (and, if wanted, its raw text files) in an embedded PostgreSQL as a backup, and checks or restores it.

Commands (all take ``--pg-dir``, the folder that holds the PostgreSQL data, and ``--backend``):

``backup``   Copy every site into PostgreSQL, or refresh the copy. Each site goes into a staging schema, is checked value by value, and
             only then replaces the previous copy, so a failed refresh leaves the old backup intact. ``--if-changed`` skips sites whose
             DuckDB file (and raw files) have not changed since the last backup.
``register`` Adopt schemas already stored by ``postgres_roundtrip_check.py`` (without ``--backup``) as backups, without copying again:
             each is checked value by value against its DuckDB file first.
``status``   List the backups: schema, kind, label, number of tables and views, when made and when last verified. No row counts.
``verify``   Check every backup against the fingerprints recorded when it was made (needs only PostgreSQL). With ``--db-dir`` (and
             ``--raw-dirs``) also compare each one value by value with the current DuckDB files (and raw files).
``restore``  Rebuild ``<out-db-dir>/omop_<site>.duckdb`` from the backup, with its views, and check it against the backup.
``drop``     Delete the backups of the given sites.

Sites are named by their keys (``--sites``) only to find the files; PostgreSQL and the printed output use the anonymous labels (from
``--labels-file``, or Site A, B, ... in ``--sites`` order) and schemas ``site_a``, ``raw_site_a``. Example::

    python scripts/postgres_backup.py backup  --pg-dir ../private/postgres --db-dir derived/omop_duckdb --sites site1 site2 site3 \
        --labels-file ../private/labels.json --raw-dirs ../raw/site1 ../raw/site2 ../raw/site3 --if-changed
    python scripts/postgres_backup.py status  --pg-dir ../private/postgres
    python scripts/postgres_backup.py verify  --pg-dir ../private/postgres
    python scripts/postgres_backup.py restore --pg-dir ../private/postgres --sites site1 site2 site3 --labels-file ../private/labels.json \
        --out-db-dir ../private/restored

Run as an ordinary user (PostgreSQL refuses root; in the DGX image use ``--user omop``). Keep ``--pg-dir`` and restored files with the
site's private files: they hold patient-level data. Exit code 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.embedded_postgres import (  # noqa: E402
    EmbeddedPostgres,
    RoundTripError,
    backup_raw_files,
    backup_site,
    backup_status,
    drop_backup,
    register_backup,
    restore_site,
    site_schema,
    verify_backup,
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["backup", "register", "status", "verify", "restore", "drop"])
    ap.add_argument("--pg-dir", required=True, help="folder with the PostgreSQL data (created on first backup)")
    ap.add_argument("--backend", default="auto", choices=["auto", "pg0", "pgembed"])
    ap.add_argument("--db-dir", help="folder with omop_<site>.duckdb (backup, register; verify against the current files)")
    ap.add_argument("--sites", nargs="+", help="site keys, used only to find files (not needed for status, or verify of all backups)")
    ap.add_argument("--labels-file", help="private JSON site -> anonymous label (default Site A, B, ... in --sites order)")
    ap.add_argument("--raw-dirs", nargs="*", help="raw text-file folder per site, same order as --sites (backup; verify against them)")
    ap.add_argument("--raw-pattern", default="*.csv")
    ap.add_argument("--tables", default="all", help="'all' (default, a full copy), 'cdm', or a comma-separated list")
    ap.add_argument("--if-changed", action="store_true", help="backup: skip sites whose files have not changed since the last backup")
    ap.add_argument("--out-db-dir", help="restore: folder for the restored DuckDB files (must not hold them yet)")
    a = ap.parse_args(argv)

    needs_sites = a.command in ("backup", "register", "restore", "drop") or (a.command == "verify" and (a.db_dir or a.raw_dirs))
    if needs_sites and not a.sites:
        ap.error(f"{a.command} needs --sites")
    if a.command in ("backup", "register") and not a.db_dir:
        ap.error(f"{a.command} needs --db-dir")
    if a.command == "restore" and not a.out_db_dir:
        ap.error("restore needs --out-db-dir")
    if a.raw_dirs and len(a.raw_dirs) != len(a.sites or []):
        ap.error("--raw-dirs needs one folder per site, in the order of --sites")
    if a.command == "restore":
        clash = [s for s in a.sites if (Path(a.out_db_dir) / f"omop_{s}.duckdb").exists()]
        if clash:
            ap.error(f"--out-db-dir already holds {len(clash)} of the output files; use an empty folder")
    labels = _labels(a.sites, a.labels_file) if a.sites else {}
    tables = a.tables if a.tables in ("all", "cdm") else [t.strip() for t in a.tables.split(",") if t.strip()]
    failed = False

    with EmbeddedPostgres(a.pg_dir, backend=a.backend) as pg:
        if a.command == "status":
            st = backup_status(pg)
            print("No backups recorded." if st.empty else st.to_string(index=False))
            return 0
        if a.command == "verify" and not a.sites:
            res = verify_backup(pg)
            for s, g in res.groupby("schema"):
                bad = g[~g["match"]]
                print(f"{s}: " + ("PASS" if bad.empty else "FAIL: " + ", ".join(f"{t} ({n})" for t, n in bad[["table", "note"]].itertuples(index=False))))
            return 0 if bool(res["match"].all()) else 1
        for i, site in enumerate(a.sites):
            label = labels[site]
            schema = site_schema(label)
            raw_schema = "raw_" + schema
            try:
                if a.command == "backup":
                    r = backup_site(pg, Path(a.db_dir) / f"omop_{site}.duckdb", schema, label=label, tables=tables, if_changed=a.if_changed)
                    info = r.attrs["backup"]
                    extra = f"; not copied: DuckDB schemas {', '.join(info['skipped_schemas'])}" if info["skipped_schemas"] else ""
                    print(f"[{label}] {schema}: {info['status']}" + (f", {info['tables']} tables" if info["tables"] else "") + extra)
                    if a.raw_dirs:
                        r = backup_raw_files(pg, a.raw_dirs[i], raw_schema, label=label, pattern=a.raw_pattern, if_changed=a.if_changed)
                        info = r.attrs["backup"]
                        print(f"[{label}] {raw_schema}: {info['status']}" + (f", {info['tables']} files" if info["tables"] else ""))
                elif a.command == "register":
                    r = register_backup(pg, schema, Path(a.db_dir) / f"omop_{site}.duckdb", label=label)
                    miss = r.attrs["backup"]["missing_tables"]
                    print(f"[{label}] {schema}: registered, {r.attrs['backup']['tables']} tables"
                          + (f"; {len(miss)} table(s) of the DuckDB file are not in it (run backup for a full copy)" if miss else ""))
                elif a.command == "verify":
                    checks = [(schema, Path(a.db_dir) / f"omop_{site}.duckdb" if a.db_dir else None)]
                    if a.raw_dirs:
                        checks.append((raw_schema, Path(a.raw_dirs[i])))
                    for s, against in checks:
                        res = verify_backup(pg, s, against=against)
                        bad = res[~res["match"]]
                        failed |= not bad.empty
                        print(f"[{label}] {s}: " + ("PASS" if bad.empty else
                                                     "FAIL: " + ", ".join(f"{t} ({n})" for t, n in bad[["table", "note"]].itertuples(index=False))))
                elif a.command == "restore":
                    res = restore_site(pg, schema, Path(a.out_db_dir) / f"omop_{site}.duckdb")
                    print(f"[{label}] {schema}: restored and checked, {len(res)} tables and views")
                elif a.command == "drop":
                    drop_backup(pg, schema)
                    drop_backup(pg, raw_schema)
                    print(f"[{label}] {schema}: dropped")
            except (RoundTripError, LookupError, FileNotFoundError, FileExistsError, ValueError) as exc:
                failed = True
                print(f"[{label}] {schema}: FAIL: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
