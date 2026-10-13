"""``python -m omop_etl.quality_check --db-path omop_cdm.duckdb --output dq_report.json``

Zero-dependency Duck-DQD entry point (the same checks as ``omop-duckdb dqd`` / :func:`omop_etl.dqd.run_dqd`):
concept mapping rate, orphan foreign keys, temporal validity, and plausible value ranges.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from omop_etl.dqd import run_dqd

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omop_etl.quality_check", description=__doc__)
    parser.add_argument("--db-path", required=True, help="OMOP CDM DuckDB file")
    parser.add_argument("--output", default=None, help="Write the report here (.json, else Markdown); default stdout")
    args = parser.parse_args(argv)

    con = duckdb.connect(args.db_path, read_only=True)
    try:
        results = run_dqd(con)
    finally:
        con.close()

    if not args.output:
        print(json.dumps(results, indent=2, default=str))
        return 0
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    else:
        lines = ["# Duck-DQD Data Quality Report\n"]
        lines += [f"## {k}\n```\n{v}\n```\n" for k, v in results.items()]
        out.write_text("\n".join(lines), encoding="utf-8")
    print(f"DQD report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
