#!/usr/bin/env python
"""Renders the manuscript text from a workbook with the package's versioned template and compares it, sentence by sentence, with a
delivered manuscript PDF.

Typical use: the workbook delivered on 2026-11-11 and its PDF. If the template reproduces the delivered text, every prose sentence of
the PDF comes back identical (the numbers are the workbook's), and what remains are the template's deliberate corrections and the
text that rests on numbers the workbook does not hold.

    python scripts/check_manuscript_text.py --workbook <delivered Tables.xlsx> --pdf <delivered PDF> \\
        --answers text_answers.json --detail text_detail.md --rendered text_rendered.md

``--answers`` holds counts only (sentences identical, same words with other numbers, reworded, absent; by section), safe to share.
``--detail`` and ``--rendered`` quote the delivered text and its numbers: keep them with the data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from omop_etl.manuscript_report import _suppress_small, read_workbook_tables  # noqa: E402
from omop_etl.manuscript_text import compare_prose, full_manuscript_markdown, load_params  # noqa: E402
from omop_etl.study_run import STUDY_PRESETS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument("--pdf", type=Path, required=True, help="the delivered manuscript PDF")
    p.add_argument("--run", type=Path, default=None, help="<prefix>_run.json of the run (optional)")
    p.add_argument("--summary", type=Path, default=None, help="<prefix>_summary.json of the run (optional)")
    p.add_argument("--params", type=Path, default=None, help="the study team's title, authors and review-board statement (JSON / YAML)")
    p.add_argument("--figures", type=Path, default=None, help="folder with the run's figure PNGs (default: the workbook's folder)")
    p.add_argument("--preset", default="delivered_2026_11_11", choices=sorted(STUDY_PRESETS),
                   help="decisions the text describes when --run is not given (default: the delivered preset)")
    p.add_argument("--window", nargs=2, default=["2017-01-01", "2022-09-30"], metavar=("START", "END"),
                   help="index-admission window the text states when --run is not given (default: the study protocol's)")
    p.add_argument("--min-cell", type=int, default=11)
    p.add_argument("--answers", type=Path, required=True)
    p.add_argument("--detail", type=Path, default=None)
    p.add_argument("--rendered", type=Path, default=None)
    a = p.parse_args(argv)

    from pypdf import PdfReader

    run = json.loads(a.run.read_text(encoding="utf-8")) if a.run else {"decisions": dict(STUDY_PRESETS[a.preset]), "preset": a.preset,
                                                                               "window": list(a.window)}
    summary = json.loads(a.summary.read_text(encoding="utf-8")) if a.summary else {}
    tabs = {k: v for k, v in read_workbook_tables(a.workbook).items() if not k.startswith("Manifest")}
    if a.min_cell:
        tabs = {k: (t, _suppress_small(df, a.min_cell)) for k, (t, df) in tabs.items()}
    fig_dir = a.figures or a.workbook.parent  # the run's figures sit next to its workbook
    imgs = sorted(fig_dir.glob("*.png")) if fig_dir.is_dir() else []
    body = full_manuscript_markdown(tabs, imgs, workbook=a.workbook, run=run, summary=summary, params=load_params(a.params))
    ref_text = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(a.pdf)).pages)
    res = compare_prose(ref_text, body["markdown"])
    answers = {"template": body["template"], "decisions_from": "run" if a.run else f"preset {a.preset}", **res["answers"],
               "values_not_in_inputs": body["missing"]}
    a.answers.parent.mkdir(parents=True, exist_ok=True)
    a.answers.write_text(json.dumps(answers, indent=1), encoding="utf-8")
    if a.rendered:
        a.rendered.write_text(body["markdown"], encoding="utf-8")
    if a.detail:
        lines = ["# Manuscript text against the delivered PDF (detail; quotes the delivered text, keep with the data)", ""]
        for kind in ("numbers_only", "reworded", "absent"):
            sel = [x for x in res["pairs"] if x["kind"] == kind]
            lines += [f"## {kind} ({len(sel)})", ""]
            for x in sel:
                lines += [f"- [{x['section']}, {x['similarity']}] DELIVERED: {x['reference']}"]
                if x["rendered"]:
                    lines += [f"  RENDERED: {x['rendered']}"]
            lines.append("")
        lines += [f"## added ({len(res['added_sentences'])})", "", *[f"- {s}" for s in res["added_sentences"]], ""]
        a.detail.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in answers.items() if k != "by_section"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
