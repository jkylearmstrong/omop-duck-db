"""Results report from a manuscript workbook: facts JSON, one CSV per table, Results.md and Results.html.

Example (after reproduce_study.py):

    python scripts/render_manuscript.py --workbook out/Study_Manuscript_Tables.xlsx --out out/report --figures out \
        --start 2017-01-01 --end 2022-09-30
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.manuscript_report import render_results_report  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workbook", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--figures", help="folder of *.png (or a single image) to include")
    p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--seed", type=int)
    p.add_argument("--title", default="Results")
    a = p.parse_args(argv)
    r = render_results_report(a.workbook, a.out, figures=a.figures, window=(a.start, a.end) if a.start and a.end else None, seed=a.seed, title=a.title)
    for k, v in r.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
