#!/usr/bin/env python3
"""CLI utility to compare study manuscript workbooks and generate attribution deltas.

Examples:
    # Compare new run to golden workbook and write Study_Deltas.csv
    python scripts/compare_study_deltas.py --new run_021/Del021_Manuscript_Tables.xlsx \
                                           --baseline deliverables/Golden_Tables.xlsx \
                                           --out-dir run_021 --prefix Del021
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Ensure repository python package is imported
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.manuscript_tables import workbook_deltas, delta_summary


def parse_args(args=None):
    p = argparse.ArgumentParser(
        description="Compare two study manuscript workbooks and generate cell/row delta reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--new", "-n", required=True, type=Path,
        help="Path to new/evaluated workbook (.xlsx).",
    )
    p.add_argument(
        "--baseline", "-b", required=True, type=Path,
        help="Path to baseline or golden comparison workbook (.xlsx).",
    )
    p.add_argument(
        "--out-dir", "-o", type=Path, default=None,
        help="Output directory for delta CSVs (default: directory containing --new).",
    )
    p.add_argument(
        "--prefix", "-p", type=str, default="Study",
        help="Filename prefix for output CSVs (e.g. <prefix>_Deltas.csv).",
    )
    p.add_argument(
        "--markdown", "-m", type=Path, default=None,
        help="Optional path to write a Markdown summary report.",
    )
    p.add_argument(
        "--fail-on-diff", action="store_true",
        help="Exit with non-zero status if any cell differences are detected.",
    )
    return p.parse_args(args)


def main():
    a = parse_args()
    if not a.new.exists():
        raise FileNotFoundError(f"New workbook does not exist: {a.new}")
    if not a.baseline.exists():
        raise FileNotFoundError(f"Baseline workbook does not exist: {a.baseline}")

    out_dir = a.out_dir if a.out_dir is not None else a.new.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Comparing workbooks:\n  New:      {a.new}\n  Baseline: {a.baseline}")
    cells, rows = workbook_deltas(a.new, a.baseline)

    deltas_path = out_dir / f"{a.prefix}_Deltas.csv"
    delta_rows_path = out_dir / f"{a.prefix}_Delta_Rows.csv"

    cells.to_csv(deltas_path, index=False)
    rows.to_csv(delta_rows_path, index=False)
    print(f"Wrote cell deltas: {deltas_path} ({len(cells)} cells)")
    print(f"Wrote row deltas:  {delta_rows_path} ({len(rows)} rows)")

    summary = delta_summary(cells)
    print("\n" + "=" * 30 + " DELTA SUMMARY " + "=" * 30)
    if summary.empty:
        print("All sheets and cells matched identically (0 differing cells).")
    else:
        print(summary.to_string(index=False))

    if a.markdown is not None:
        md_lines = [
            f"# Manuscript Workbook Delta Summary: {a.prefix}",
            "",
            f"- **Evaluated Workbook**: `{a.new.name}`",
            f"- **Baseline Workbook**: `{a.baseline.name}`",
            f"- **Total Differing Cells**: {len(cells):,}",
            f"- **Total Differing Rows**: {len(rows):,}",
            "",
            "## Sheet-Level Summary",
            "",
        ]
        if summary.empty:
            md_lines.append("All cells match baseline with 0 differences.")
        else:
            md_lines.append(summary.to_markdown(index=False))
        a.markdown.parent.mkdir(parents=True, exist_ok=True)
        a.markdown.write_text("\n".join(md_lines), encoding="utf-8")
        print(f"\nWrote markdown summary to {a.markdown}")

    if a.fail_on_diff and len(cells) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
