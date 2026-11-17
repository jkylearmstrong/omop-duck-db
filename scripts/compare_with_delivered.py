#!/usr/bin/env python3
"""Compare a study rerun with a delivered manuscript, with the method fixed in omop_etl.delivered_compare.

Two outputs: an answers JSON with aggregates only (counts and largest changes; no site letters, no per-site values), which can be
shared, and a detail Markdown with the per-site pairings and every differing cell, which stays on the machine that holds the data.
Site rows are paired by their numbers, since letters are reassigned between runs. Cells are compared exactly.

Example:
    python scripts/compare_with_delivered.py --new run_v0300/Study_Manuscript_Tables.xlsx \\
        --delivered nov11/Tables.xlsx --delivered-pdf nov11/Manuscript.pdf \\
        --new-pdf run_v0300/Study_Manuscript.pdf --new-docx run_v0300/Study_Manuscript.docx \\
        --delivered-sha256 b79c8712... --previous clean_v0281/answers.json \\
        --answers <PRIVATE>/answers.json --detail <PRIVATE>/compare_detail.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.delivered_compare import compare_with_delivered, write_comparison  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--new", required=True, type=Path, help="the rerun's manuscript workbook")
    p.add_argument("--delivered", required=True, type=Path, help="the delivered manuscript workbook")
    p.add_argument("--new-pdf", type=Path)
    p.add_argument("--new-docx", type=Path)
    p.add_argument("--delivered-pdf", type=Path, help="needed for the quoted-numbers count (item 17)")
    p.add_argument("--delivered-sha256", help="expected sha256 of the delivered PDF (item 1)")
    p.add_argument("--previous", type=Path, help="an earlier answers JSON; item 18 lists what changed since")
    p.add_argument("--answers", required=True, type=Path, help="where to write the answers JSON (aggregates only)")
    p.add_argument("--detail", required=True, type=Path, help="where to write the detail Markdown (per-site; keep it private)")
    a = p.parse_args(argv)
    prev = json.loads(a.previous.read_text(encoding="utf-8")) if a.previous else None
    r = compare_with_delivered(a.new, a.delivered, new_pdf=a.new_pdf, delivered_pdf=a.delivered_pdf, new_docx=a.new_docx,
                               delivered_pdf_sha256=a.delivered_sha256, previous=prev)
    for f in (a.answers, a.detail):
        f.parent.mkdir(parents=True, exist_ok=True)
    write_comparison(r, a.answers, a.detail)
    print(json.dumps(r["answers"], indent=2, default=str))
    print(f"\nanswers: {a.answers}\ndetail (keep private): {a.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
