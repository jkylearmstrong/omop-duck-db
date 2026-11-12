"""Checks a new manuscript workbook against a golden (delivered) one; exit code 1 when any check fails.

Example:

    python scripts/check_golden.py --new out/Study_Manuscript_Tables.xlsx --golden deliverables/eDERRI_Traditional_ML_Models_Manuscript_Tables.xlsx

Prints the cell-by-cell summary and the similarity table (thresholds in omop_etl.SIMILARITY_TOLERANCES; override with --tol name=value).
--strict requires zero differing cells instead of the similarity thresholds.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.manuscript_tables import SIMILARITY_TOLERANCES, compare_workbooks, delta_summary, workbook_deltas, workbook_similarity  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--new", required=True)
    p.add_argument("--golden", required=True)
    p.add_argument("--tol", nargs="*", default=[], metavar="NAME=VALUE", help=f"tolerances; names: {', '.join(SIMILARITY_TOLERANCES)}")
    p.add_argument("--strict", action="store_true", help="fail when any cell differs")
    p.add_argument("--differences", help="write every differing cell to this CSV")
    p.add_argument("--deltas", help="write the numeric deltas (sorted by relative change) to this CSV; extra/missing rows go to <name>_rows.csv")
    a = p.parse_args(argv)
    tol = {k: float(v) for k, v in (t.split("=", 1) for t in a.tol)}
    bad = set(tol) - set(SIMILARITY_TOLERANCES)
    if bad:
        p.error(f"unknown tolerance(s): {sorted(bad)}")
    summary, diffs = compare_workbooks(a.new, a.golden)
    sim = workbook_similarity(a.new, a.golden, tolerances=tol)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(summary.to_string(index=False)); print(); print(sim.to_string(index=False))
    if a.deltas:
        cells, rows = workbook_deltas(a.new, a.golden)
        cells.to_csv(a.deltas, index=False)
        rows.to_csv(str(Path(a.deltas).with_suffix("")) + "_rows.csv", index=False)
        print(); print(delta_summary(cells).to_string(index=False)); print(f"rows in only one workbook: {len(rows)}")
    if a.differences:
        diffs.to_csv(a.differences, index=False)
    ok = bool(sim["passed"].all()) and (diffs.empty or not a.strict)
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
