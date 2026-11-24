"""Text checks on a rendered manuscript PDF before it goes to a reviewer (Kyle's C9 list, 2026-11-23).

:func:`final_draft_checks` reads the PDF text with pypdf (a package dependency) and returns one result per check; run it as
``python -m omop_etl.manuscript_checks Study_Manuscript.pdf`` for a PASS / FAIL list and an exit code. The checks read the text
layer only: Figures 1 and 2 are images, so their wording is checked where they are drawn (tests of
:func:`omop_etl.study_report.plot_cohort_flowchart`) and by looking at the rendered pages.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Sequence

__all__ = ["TITLE", "EMPTY_SOURCE_TERMS", "MULTILEVEL_FEATURES", "final_draft_checks", "main"]

TITLE = "Multi-Center Benchmark of Traditional Machine Learning Models for 30-Day Hospital Readmission Risk Prediction"
# the ten measures no site's source extract had (C8); each is named once, in the Section 2.2 sentence, and nowhere else
EMPTY_SOURCE_TERMS = ("potassium", "chloride", "calcium", "platelets", "bilirubin", "alkaline phosphatase", "heart rate",
                      "respiratory rate", "temperature", "oxygen saturation")
MULTILEVEL_FEATURES = ("Age", "Race", "SDI", "RUCA", "Discharge Status", "Tobacco", "BMI")


def _section(text: str, start: str, ends: Sequence[str]) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    stops = [j for j in (text.find(e, i + len(start)) for e in ends) if j > 0]
    return text[i:min(stops)] if stops else text[i:]


def final_draft_checks(pdf: str | Path, *, title: str = TITLE, empty_terms: Sequence[str] = EMPTY_SOURCE_TERMS,
                       n_reference_rows: int = len(MULTILEVEL_FEATURES), last_reference: str = "Yuan") -> list[dict[str, Any]]:
    """Checks the text of a rendered manuscript: ``[{"check", "ok", "detail"}]``, one per check.

    1. page 1 starts with ``title`` (line breaks ignored);
    2. "pinned" appears nowhere;
    3. no ``*_last`` column and no "(last value)" label;
    4. Table 4 has ``n_reference_rows`` reference rows (one per multilevel feature);
    5. Table 3 (leave-one-site-out) reports F1 and not Brier;
    6. the last page ends the References with ``last_reference``;
    7. each of ``empty_terms`` appears exactly once (the Section 2.2 sentence on the empty source columns);
    8. no snake_case column name (``lab_...``, ``vital_...``, ``elix_...``) in the text."""
    from pypdf import PdfReader

    # the page number printed at the foot of each page is dropped, so a phrase that runs onto the next page reads as one ("heart 2 rate")
    pages = [re.sub(r"\s*\b\d{1,3}\s*$", "", p.extract_text() or "") for p in PdfReader(str(pdf)).pages]
    text = "\n".join(pages)
    flat = re.sub(r"\s+", " ", text)
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    first = re.sub(r"\s+", " ", pages[0] if pages else "").strip()
    add("title on page 1", first.startswith(title), first[:len(title) + 20])
    add("no 'pinned'", "pinned" not in text.lower(), "; ".join(m for m in re.findall(r".{30}pinned.{30}", flat, re.I)[:3]))
    hits = re.findall(r".{30}(?:last value|_last\b).{10}", flat, re.I)
    add("no *_last labs", not hits, "; ".join(hits[:3]))
    t4 = _section(text, "Table 4:", ("3.5 SHAP", "\n3.5 ", "Figure 2:", "4 Discussion"))
    refs = len(re.findall(r"\bReference\b", t4))  # the reference level of each multilevel feature (case as printed)
    add("Table 4 reference rows", bool(t4) and refs == n_reference_rows, f"{refs} found, {n_reference_rows} expected" if t4 else "Table 4 not found")
    t3 = _section(text, "Table 3:", ("3.4 Multivariable", "Table 4:"))
    add("Table 3 has F1, no Brier", bool(t3) and "F1" in t3 and "Brier" not in t3, "Table 3 not found" if not t3 else
        f"F1 {'present' if 'F1' in t3 else 'missing'}, Brier {'present' if 'Brier' in t3 else 'absent'}")
    last = pages[-1] if pages else ""
    add("references end on the last page", last_reference in last, re.sub(r"\s+", " ", last)[-120:])
    counts = {t: len(re.findall(re.escape(t), flat, re.I)) for t in empty_terms}
    add("empty source columns named once", all(v == 1 for v in counts.values()),
        ", ".join(f"{k} {v}" for k, v in counts.items() if v != 1))
    snake = sorted(set(re.findall(r"\b(?:lab|vital|elix|charlson|sdoh|dx|enc)_[a-z0-9_]+\b", flat)))
    add("no snake_case column names", not snake, ", ".join(snake[:10]))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m omop_etl.manuscript_checks <manuscript.pdf>")
        return 2
    res = final_draft_checks(args[0])
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + (f"  ({r['detail']})" if r["detail"] and not r["ok"] else ""))
    print(f"{sum(r['ok'] for r in res)} of {len(res)} checks passed")
    return 0 if all(r["ok"] for r in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
