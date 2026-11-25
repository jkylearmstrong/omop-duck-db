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

__all__ = ["TITLE", "EMPTY_SOURCE_TERMS", "MULTILEVEL_FEATURES", "final_draft_checks", "workbook_checks", "main"]

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
                       n_reference_rows: int = len(MULTILEVEL_FEATURES), last_reference: str = "Yuan",
                       footer_gap: float = 14.0) -> list[dict[str, Any]]:
    """Checks the text of a rendered manuscript: ``[{"check", "ok", "detail"}]``, one per check.

    1. page 1 starts with ``title`` (line breaks ignored);
    2. "pinned" appears nowhere;
    3. no ``*_last`` column and no "(last value)" label;
    4. Table 4 has ``n_reference_rows`` reference rows (one per multilevel feature);
    5. Table 3 (leave-one-site-out) reports F1 and not Brier;
    6. the last page ends the References with ``last_reference``;
    7. each of ``empty_terms`` appears exactly once (the Section 2.2 sentence on the empty source columns);
    8. no snake_case column name (``lab_...``, ``vital_...``, ``elix_...``) in the text;
    9. no collapsed bullet (".- ", a list item run into the sentence before it; C15);
    10. no text within ``footer_gap`` points above a page's number (a caption pushed into the footer; C17 Figure 1);
    11. no range that lost its upper end ("ranged from 0.73." / "ranged from 0.73 with"; C20 Section 4.1);
    12. the cohort is not called free of an external list of index stays while the text describes a saved draw (C20 Sections 2.1, 6);
    13. Table 1 has an "SDI Quintile: Missing / Unknown" row when it has SDI quintiles (C20);
    14. every Section 2.3 increment example ("10 mmHg of ...") is an increment that Table 4 prints (C20: "10 outpatient encounters");
    15. the stay-type table (Table S1) prints AUC-ROC to 2 decimals, as Tables 2 and 3 (C20 printed 0.698);
    16. the length-of-stay table (Table S3) prints length-of-stay mean ± SD to 1 decimal, as Table 1 (C20 printed 5.28 ± 6.81);
    17. the length-of-stay table ends on the page its caption is on (C20: its last row ran alone onto the next page)."""
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
    bullets = re.findall(r".{0,30}\.- .{0,30}", flat)
    add("no collapsed bullets", not bullets, "; ".join(bullets[:3]))
    crowded = _footer_collisions(pdf, footer_gap)
    add("nothing runs into the page number", not crowded, ", ".join(f"page {p}" for p in crowded[:10]))
    dangling = re.findall(r".{0,40}\branged? from -?\d+\.\d+(?!\d|\.\d|%)(?!\s*(?:to|–|-)\s*-?\d).{0,20}", flat)
    add("no range missing its upper end", not dangling, "; ".join(dangling[:3]))
    claims = "no externally supplied list" in flat
    draw = re.findall(r".{0,40}(?:saved random draw|original random draw|made once and reused|pin_stays).{0,30}", flat)
    add("self-contained cohort claim matches the draw", not (claims and draw), "; ".join(draw[:2]))
    # stop tokens anchored to the heading: a bare "3.2 " matches inside "27.0 ± 33.2" (C20: Table 1 cut before the SDI rows)
    t1 = _section(text, "Table 1:", ("Table 2:", "\n3.2 ", "3.2 Overall"))
    sdi_ok = "SDI Quintile 1" not in t1 or "SDI Quintile: Missing / Unknown" in re.sub(r"\s+", " ", t1)
    add("Table 1 SDI Missing / Unknown row", bool(t1) and sdi_ok, "Table 1 not found" if not t1 else ("" if sdi_ok else "row missing"))
    m = re.search(r"increment (?:Δ|D) \(e\.g\., (.*?)\)\s*or, for indicator", flat)
    t4flat = re.sub(r"\s+", " ", t4)
    wrong = []
    for ex in (m.group(1).split(", ") if m else []):
        inc = re.match(r"(\d+(?:\.\d+)?\s+\S+)", ex)
        if inc and inc.group(1) not in t4flat:
            wrong.append(ex)
    add("Section 2.3 increments are Table 4's", not wrong, "; ".join(wrong))
    # the supplementary tables by caption, not number: a run without one of them numbers the rest differently
    nxt = re.compile(r"Table S\d+:|Appendix A")

    def by_caption(words: str) -> str:
        i = text.find(words)
        if i < 0:
            return ""
        m_ = nxt.search(text, i + len(words))
        return text[i:m_.start() if m_ else len(text)]

    s1 = by_caption("Cohort Partition by Whether")
    s1_three = re.findall(r"\b0\.\d{3}\b", s1)
    add("Table S1 AUC-ROC to 2 decimals", not s1_three, ", ".join(s1_three[:4]))
    los_cap = "Distribution of Index Stay Length of Stay"
    s3 = by_caption(los_cap)
    s3_two = re.findall(r"\d+\.\d{2} (?:±|\+/-) \d+\.\d{2}", s3)
    add("Table S3 LOS mean ± SD to 1 decimal", not s3_two, ", ".join(s3_two[:3]))
    cap = next((i for i, p in enumerate(pages) if los_cap in re.sub(r"\s+", " ", p)), None)
    end = next((i for i, p in enumerate(pages) if cap is not None and i >= cap and "Whitney" in p), None)
    add("Table S3 on one page", cap is None or end is None or end == cap,
        "" if cap is None or end is None else f"caption on page {cap + 1}, last row on page {end + 1}")
    return out


def workbook_checks(workbook: str | Path) -> list[dict[str, Any]]:
    """Checks of the manuscript workbook: every number in a metric column of the metric sheets (Tables 2, 4, 5 and 6; a column holding
    a non-whole number) is shown with 2 decimals (C20: Excel showed Table 5's 0.60 as 0.6)."""
    import openpyxl

    from omop_etl.manuscript_tables import METRIC_DECIMALS, METRIC_SHEETS

    wb = openpyxl.load_workbook(str(workbook))
    want = "0." + "0" * METRIC_DECIMALS
    bad = []
    for name in METRIC_SHEETS:
        if name[:31] not in wb.sheetnames:
            continue
        for col in wb[name[:31]].iter_cols(min_row=4):
            nums = [c for c in col if isinstance(c.value, float)]
            if any(not c.value.is_integer() for c in nums):
                bad += [f"{name[:8]} {c.coordinate}" for c in nums if c.number_format != want]
    return [{"check": "workbook metrics shown to 2 decimals", "ok": not bad, "detail": ", ".join(bad[:6])}]


def _footer_collisions(pdf: str | Path, gap: float) -> list[int]:
    """Pages where a line of text sits less than ``gap`` points above the page number (the lowest run that is only a number)."""
    from pypdf import PdfReader

    bad = []
    for i, page in enumerate(PdfReader(str(pdf)).pages, start=1):
        runs: list[tuple[str, float]] = []

        def visit(text, cm, tm, *_):
            if text and text.strip():
                runs.append((text.strip(), cm[5] + tm[5] * cm[3] if cm else tm[5]))

        page.extract_text(visitor_text=visit)
        numbers = [y for t, y in runs if re.fullmatch(r"\d{1,3}", t)]
        if not numbers:
            continue
        foot = min(numbers)
        if any(foot <= y < foot + gap for t, y in runs if not (re.fullmatch(r"\d{1,3}", t) and y == foot)):
            bad.append(i)
    return bad


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        print("usage: python -m omop_etl.manuscript_checks <manuscript.pdf> [<Manuscript_Tables.xlsx>]")
        return 2
    res = final_draft_checks(args[0]) + (workbook_checks(args[1]) if len(args) == 2 else [])
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + (f"  ({r['detail']})" if r["detail"] and not r["ok"] else ""))
    print(f"{sum(r['ok'] for r in res)} of {len(res)} checks passed")
    return 0 if all(r["ok"] for r in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
