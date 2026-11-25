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

__all__ = ["TITLE", "EMPTY_SOURCE_TERMS", "MULTILEVEL_FEATURES", "PLACEHOLDERS", "final_draft_checks", "workbook_checks", "pdf_workbook_checks",
           "docx_checks", "run_record_checks", "increment_in_table", "release_checks", "main"]

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
    17. the length-of-stay table ends on the page its caption is on (C20: its last row ran alone onto the next page);
    18. the tuning-grid table (Table S4) ends on the page its caption is on (C20r: its 13th row ran alone onto page 18);
    19. no table caption sits at the foot of a page with its table on the next (C20r: "Table 1:" alone at the foot of page 5);
    20. no placeholder (``PLACEHOLDERS``, a value the run or the ``--manuscript-params`` file lacked) and no Section 2.3 increment that
        names its unit twice (C20r: "10 visits of prior outpatient visits").

    :func:`release_checks` adds the workbook, PDF-versus-workbook, Word and run-record checks."""
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
    examples = m.group(1).split(", ") if m else []
    wrong = [ex for ex in examples if not increment_in_table(ex, t4flat)]
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
    # 0.35.2 (Kyle's release gates after C20r)
    grid = _spills(pages, "Full Grid Evaluation Metrics")
    add("Table S4 on one page", grid is None, grid or "")
    orphans = _orphan_captions(pdf)
    add("no table caption alone at the foot of a page", not orphans, ", ".join(f"{c} on page {p}" for p, c in orphans[:5]))
    marks = sorted({x for x in PLACEHOLDERS if x in flat})
    repeated = [ex for ex in examples if _repeats_its_unit(ex)]
    add("no placeholders or repeated increment words", not marks and not repeated, "; ".join(marks + repeated))
    return out


#: text that only appears when the run or the study team's parameters lacked a value (``--manuscript-params``)
PLACEHOLDERS = ("[not in this run:", "[Review board statement:")


def increment_in_table(example: str, table_text: str) -> bool:
    """Whether a Section 2.3 example ("10 mmHg of blood pressure", "10 prior outpatient visits") names an increment that Table 4
    prints: the table has the example's number followed by a unit that the example names (plural or not)."""
    m = re.match(r"(\d+(?:\.\d+)?)\s+(.*)", example.strip())
    if not m:
        return True
    num, rest = m.groups()
    words = {w.rstrip("s").lower() for w in re.findall(r"[^\s,;()]+", rest)}
    units = re.findall(rf"(?<![\d.]){re.escape(num)}\s+([^\s,;()]+)", table_text)
    return any(u.rstrip("s").lower() in words for u in units)


def _repeats_its_unit(example: str) -> bool:
    """"10 visits of prior outpatient visits": the unit named again in its measure (C20r Section 2.3)."""
    m = re.match(r"\d+(?:\.\d+)?\s+(\S+) of (.*)", example.strip())
    return bool(m) and m.group(1).rstrip("s").lower() in {w.rstrip("s").lower() for w in m.group(2).split()}


def _spills(pages: Sequence[str], caption_words: str) -> str | None:
    """For a table kept on one page: ``None`` when its caption's page holds the whole table, else where it runs over. The table ends
    at the next caption or appendix; text before that marker on the next page is the spilled part."""
    cap = next((i for i, p in enumerate(pages) if caption_words in re.sub(r"\s+", " ", p)), None)
    if cap is None:
        return None
    marker = re.compile(r"Table S?\d+:|Figure \d+:|Appendix [A-Z]|References")
    rest = re.sub(r"\s+", " ", pages[cap])
    after = rest[rest.find(caption_words) + len(caption_words):]
    if marker.search(after) or cap + 1 >= len(pages):
        return None
    nxt = pages[cap + 1]
    m = marker.search(nxt)
    spilled = (nxt[:m.start()] if m else nxt).strip()
    return f"caption on page {cap + 1}, rows on page {cap + 2}" if spilled else None


def _orphan_captions(pdf: str | Path, *, max_lines_below: int = 1) -> list[tuple[int, str]]:
    """``[(page, "Table 1")]`` for each table caption with at most ``max_lines_below`` text lines under it on its page (the page number
    left out): a caption alone at the foot of a page, its table starting on the next (C20r Table 1)."""
    from pypdf import PdfReader

    bad = []
    for i, page in enumerate(PdfReader(str(pdf)).pages, start=1):
        runs: list[tuple[str, float]] = []

        def visit(text, cm, tm, *_):
            if text and text.strip():
                runs.append((text.strip(), cm[5] + tm[5] * cm[3] if cm else tm[5]))

        page.extract_text(visitor_text=visit)
        numbers = [y for t, y in runs if re.fullmatch(r"\d{1,3}", t)]
        foot = min(numbers) if numbers else None
        for t, y in runs:
            m = re.match(r"(Table S?\d+):", t)
            if m:
                below = {round(yy) for tt, yy in runs if yy < y - 1 and not (yy == foot and re.fullmatch(r"\d{1,3}", tt))}
                if len(below) <= max_lines_below:
                    bad.append((i, m.group(1)))
    return bad


def _num(x: Any) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return float(m.group()) if m else None


def _pct(x: Any) -> float | None:
    m = re.search(r"\((-?\d+(?:\.\d+)?)%\)", str(x))
    return float(m.group(1)) if m else None


def _count(x: Any) -> str:
    """The count of an "n (pct%)" cell as text without separators ("1,473 (49.1%)" -> "1473"; "<11 (2.7%)" -> "<11")."""
    return str(x).split("(")[0].replace(",", "").strip()


def _sheet(tabs: dict, number: str):
    return next(((title, t) for k, (title, t) in tabs.items() if re.match(rf"{re.escape(number)}\b", k)), (None, None))


def _blocks(t1) -> dict[str, list]:
    """Table 1 rows of each multilevel feature (``MULTILEVEL_FEATURES``), keyed by feature."""
    out: dict[str, list] = {}
    for _, row in t1.iterrows():
        lab = str(row.iloc[0])
        for f in MULTILEVEL_FEATURES:
            if lab.startswith(f + ":") or (f == "SDI" and lab.startswith("SDI Quintile")):
                out.setdefault(f, []).append(row)
                break
    return out


def workbook_checks(workbook: str | Path, *, tolerance: float = 0.3) -> list[dict[str, Any]]:
    """Checks of the manuscript workbook.

    1. every number in a metric column of the metric sheets (Tables 2, 4, 5 and 6; a column holding a non-whole number; whole numbers
       stored as int included, bool not) is shown with 2 decimals (C20: Excel showed Table 5's 0.60 as 0.6);
    2. every such number is stored with no more places than it shows, so Excel and the PDF cannot round a tie differently (C20r:
       Table S1 stored 0.695, Excel showed 0.70 and the PDF 0.69);
    3. the categories of each multilevel feature in Table 1 sum to 100% (within ``tolerance`` points) in every column (a dropped
       category row, e.g. C20's missing "SDI Quintile: Missing / Unknown");
    4. every Table S2 row that Table 1 also has (the "Missing / Unknown" rows) has the same counts there."""
    import openpyxl

    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_tables import METRIC_DECIMALS, METRIC_SHEETS

    wb = openpyxl.load_workbook(str(workbook))
    want = "0." + "0" * METRIC_DECIMALS
    bad, long_ = [], []
    for name in METRIC_SHEETS:
        if name[:31] not in wb.sheetnames:
            continue
        for col in wb[name[:31]].iter_cols(min_row=4):
            nums = [c for c in col if isinstance(c.value, (int, float)) and not isinstance(c.value, bool)]
            if any(not float(c.value).is_integer() for c in nums):
                bad += [f"{name[:8]} {c.coordinate}" for c in nums if c.number_format != want]
                long_ += [f"{name[:8]} {c.coordinate} = {c.value}" for c in nums if abs(c.value - round(c.value, METRIC_DECIMALS)) > 1e-9]
    out = [{"check": "workbook metrics shown to 2 decimals", "ok": not bad, "detail": ", ".join(bad[:6])},
           {"check": "workbook metrics stored to 2 decimals", "ok": not long_, "detail": ", ".join(long_[:6])}]
    tabs = read_workbook_tables(workbook)
    _, t1 = _sheet(tabs, "Table 1")
    off = []
    if t1 is not None and len(t1):
        for f, rows in _blocks(t1).items():
            if len(rows) < 2:
                continue
            for j, col in enumerate(t1.columns[1:], start=1):
                vals = [_pct(r.iloc[j]) for r in rows]
                if any(v is None for v in vals):
                    continue
                if abs(sum(vals) - 100) > tolerance:
                    off.append(f"{f} {col}: {sum(vals):.1f}%")
    out.append({"check": "Table 1 categories sum to 100%", "ok": t1 is not None and not off,
                "detail": "Table 1 not found" if t1 is None else "; ".join(off[:6])})
    _, s2 = _sheet(tabs, "Table S2")
    diff = []
    if t1 is not None and s2 is not None and len(s2):
        rows1 = {str(r.iloc[0]): r for _, r in t1.iterrows()}
        for _, r in s2.iterrows():
            r1 = rows1.get(str(r.iloc[0]))
            if r1 is None:
                continue
            for j in range(1, min(len(r), len(r1))):
                if _count(r.iloc[j]) != _count(r1.iloc[j]):
                    diff.append(f"{r.iloc[0]} ({s2.columns[j]}): {r1.iloc[j]} vs {r.iloc[j]}")
    out.append({"check": "Table 1 Missing / Unknown counts match Table S2", "ok": not diff, "detail": "; ".join(diff[:4])})
    return out


def _flat(s: str) -> str:
    return re.sub(r"\s+", "", s).replace("−", "-").replace("–", "-").replace("—", "-")


def pdf_workbook_checks(pdf: str | Path, workbook: str | Path) -> list[dict[str, Any]]:
    """The PDF prints the workbook's numbers at the printed precision: the cohort N and the readmitted count (Table 1), the
    readmission rate, the AUC-ROC cells of Table 2 and of the stay-type table, and the leave-one-site-out mean AUC-ROC. Tables are
    found by their caption text, not their number (the printed numbering differs from the workbook's). Spaces are ignored, as
    the PDF text breaks lines inside a cell."""
    from pypdf import PdfReader

    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_tables import excel_text

    pages = [p.extract_text() or "" for p in PdfReader(str(pdf)).pages]
    whole = _flat("\n".join(pages))
    tabs = read_workbook_tables(workbook)
    missing: list[str] = []

    def section(title: str | None) -> str:
        if not title:
            return ""
        words = _flat(re.sub(r"^Table S?\d+:\s*", "", title))[:40]
        i = whole.find(words)
        if i < 0:
            return ""
        m = re.compile(r"TableS?\d+:|Appendix[A-Z]").search(whole, i + len(words))
        return whole[i:m.start() if m else len(whole)]

    def need(where: str, value: str, label: str) -> None:
        if _flat(value) not in where:
            missing.append(f"{label} {value}")

    title, t1 = _sheet(tabs, "Table 1")
    if t1 is not None and len(t1):
        n = re.search(r"N = ([\d,]+)", title or "")
        sec = section(title)
        if n:
            need(sec, f"N = {n.group(1)}", "Table 1")
        first = t1.iloc[0]
        if str(first.iloc[0]).startswith("# Unique Patients") and len(first) > 2:
            tot, rd = _num(first.iloc[1]), _num(first.iloc[2])
            if rd is not None:
                if _flat(f"{int(rd):,}") not in sec and _flat(f"{int(rd)}") not in sec:
                    missing.append(f"Table 1 readmitted {int(rd):,}")
                if tot:
                    need(whole, f"{100 * rd / tot:.1f}%", "readmission rate")
    title, t2 = _sheet(tabs, "Table 2")
    if t2 is not None and len(t2):
        sec = section(title)
        for c in [c for c in t2.columns if "AUC" in str(c)]:
            for v in t2[c].dropna():
                need(sec, str(v), "Table 2")
    title, t4 = _sheet(tabs, "Table 4")
    if t4 is not None and len(t4):
        sec = section(title)
        for c in [c for c in t4.columns if "AUC" in str(c)]:
            for v in t4[c].dropna():
                need(sec, excel_text(v) if isinstance(v, (int, float)) else str(v), "stay-type table")
    title, t5 = _sheet(tabs, "Table 5")
    if t5 is not None and len(t5):
        mean = t5[t5.iloc[:, 0].astype(str).str.startswith("Mean")]
        auc = [c for c in t5.columns if "AUC" in str(c)]
        if len(mean) and auc:
            need(section(title), str(mean.iloc[0][auc[0]]), "LOSO mean")
    return [{"check": "PDF numbers match the workbook", "ok": not missing, "detail": "; ".join(missing[:6])}]


def docx_checks(docx: str | Path, *, title: str = TITLE, params: dict | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Checks of the Word manuscript (the file a co-author edits for the journal).

    1. the title is in the text;
    2. no placeholder (``PLACEHOLDERS``: a value the run or the study team's parameters lacked);
    3. every author and the review-board statement of ``params`` (the ``--manuscript-params`` file) are in the text;
    4. the table captions run Table 1, 2, ... and Table S1, S2, ... without a gap, and each has a Word table;
    5. the file's properties (creator, last modified by, company, custom properties) name no one in ``names`` (the private list of
       site and institution names; never stored in the package) and not this computer's account name."""
    import getpass
    import zipfile

    with zipfile.ZipFile(str(docx)) as z:
        doc = z.read("word/document.xml").decode("utf-8", "replace")
        core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in z.namelist() else ""
        app = z.read("docProps/app.xml").decode("utf-8", "replace") if "docProps/app.xml" in z.namelist() else ""
        custom = "docProps/custom.xml" in z.namelist() and "<property" in z.read("docProps/custom.xml").decode("utf-8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", doc, flags=re.S)
    text = "\n".join("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p)) for p in paras)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
    flat = re.sub(r"\s+", " ", text)
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    add("DOCX title", title in flat, flat[:len(title) + 20])
    marks = sorted({x for x in PLACEHOLDERS if x in flat})
    add("DOCX has no placeholders", not marks, "; ".join(marks))
    p = params or {}
    want = [a.get("name", "") if isinstance(a, dict) else str(a) for a in p.get("authors", [])]
    if p.get("irb_statement"):
        want.append(str(p["irb_statement"]))
    absent = [w[:40] for w in want if w and re.sub(r"\s+", " ", w).strip() not in flat]
    add("DOCX has the authors and review-board statement", not absent, "; ".join(absent[:4]))
    caps = re.findall(r"^\s*(Table (S?)(\d+)):", text, flags=re.M)
    main = [int(n) for _, s, n in caps if not s]
    supp = [int(n) for _, s, n in caps if s]
    gaps = []
    if main != list(range(1, len(main) + 1)):
        gaps.append(f"main {main}")
    if supp != list(range(1, len(supp) + 1)):
        gaps.append(f"supplementary {supp}")
    n_tbl = len(re.findall(r"<w:tbl>", doc))
    add("DOCX tables numbered and present", bool(caps) and not gaps and n_tbl >= len(caps),
        "; ".join(gaps) + (f" {n_tbl} tables for {len(caps)} captions" if n_tbl < len(caps) else ""))
    props = " ".join(re.findall(r"<(?:dc:creator|cp:lastModifiedBy|Company|Manager)>([^<]*)<", core + app))
    user = getpass.getuser() if hasattr(getpass, "getuser") else ""
    leak = [n for n in names if n and n.lower() in props.lower()]
    if user and len(user) > 2 and user.lower() in props.lower() and not any(user.lower() in w.lower() for w in want):
        leak.append("this computer's account name")
    add("DOCX properties name no site or account", not leak and not custom,
        f"{len(leak)} name(s) in the properties" + ("; custom properties present" if custom else "") if leak or custom else "")
    return out


def run_record_checks(run_json: str | Path | dict, *, final: bool = False, min_bootstraps: int = 1000) -> list[dict[str, Any]]:
    """Checks of ``<prefix>_run.json`` before the outputs leave the site: the cohort was drawn inside OMOP (no saved stay list); the
    privacy scan ran and found nothing; no unexpected data-check finding; the document lacks no format and no value; the
    manuscript cites the omop-duck-db version that ran. ``final=True`` (the submission run) also needs at least ``min_bootstraps``
    bootstrap replicates and an observation-period cap."""
    import json

    r = run_json if isinstance(run_json, dict) else json.loads(Path(run_json).read_text(encoding="utf-8"))
    doc, priv, dc = r.get("document") or {}, r.get("privacy_check") or {}, r.get("data_checks") or {}
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    add("run: no saved stay list", r.get("pinned") is False, f"pinned = {r.get('pinned')}")
    add("run: privacy scan clean", priv.get("checked") is True and priv.get("hits") == 0 and not priv.get("unreadable"),
        f"checked = {priv.get('checked')}, hits = {priv.get('hits')}")
    add("run: no unexpected data findings", dc.get("n_unexpected") == 0, f"n_unexpected = {dc.get('n_unexpected')}")
    add("run: document complete", bool(doc) and not doc.get("withheld") and doc.get("missing") == [] and doc.get("text_missing") == [],
        f"missing = {doc.get('missing')}, text_missing = {doc.get('text_missing')}, withheld = {doc.get('withheld', False)}")
    env = (r.get("environment") or {}).get("omop-duck-db")
    add("run: cites the version that ran", bool(env) and env == doc.get("cites_omop_duck_db"), f"ran {env}, cites {doc.get('cites_omop_duck_db')}")
    if final:
        add("run: final bootstraps", (r.get("bootstraps") or 0) >= min_bootstraps, f"{r.get('bootstraps')} < {min_bootstraps}")
        add("run: final observation-period cap", bool((r.get("observation_period_cap") or {}).get("cap")), "no cap")
    for x in out:
        if x["ok"]:
            x["detail"] = ""
    return out


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


def release_checks(pdf: str | Path, workbook: str | Path | None = None, *, docx: str | Path | None = None,
                   run_json: str | Path | None = None, final: bool = False, params: dict | None = None,
                   names: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Every check that applies to the files given: :func:`final_draft_checks` on the PDF; with the workbook, :func:`workbook_checks`
    and :func:`pdf_workbook_checks`; with the Word file, :func:`docx_checks`; with the run record, :func:`run_record_checks`
    (``final`` for the submission run)."""
    res = final_draft_checks(pdf)
    if workbook:
        res += workbook_checks(workbook) + pdf_workbook_checks(pdf, workbook)
    if docx:
        res += docx_checks(docx, params=params, names=names)
    if run_json:
        res += run_record_checks(run_json, final=final)
    return res


def _names(files: Sequence[str]) -> list[str]:
    """Names to keep out of the Word properties: one per line of a text file, or the ``sites`` of a ``<prefix>_run_private.json``."""
    import json

    out: list[str] = []
    for f in files:
        p = Path(f)
        if p.suffix.lower() == ".json":
            out += [str(s) for s in json.loads(p.read_text(encoding="utf-8")).get("sites", [])]
        else:
            out += [x.split("#")[0].strip() for x in p.read_text(encoding="utf-8").splitlines() if x.split("#")[0].strip()]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m omop_etl.manuscript_checks",
                                 description="Release checks of a rendered manuscript (PDF, and optionally its workbook, Word file and run record).")
    ap.add_argument("pdf")
    ap.add_argument("workbook", nargs="?", help="<prefix>_Manuscript_Tables.xlsx")
    ap.add_argument("--docx", help="<prefix>_Manuscript.docx")
    ap.add_argument("--run-json", help="<prefix>_run.json")
    ap.add_argument("--final", action="store_true", help="the submission run: also needs 1,000+ bootstraps and an observation-period cap")
    ap.add_argument("--params", help="the --manuscript-params file (JSON or YAML: authors, review-board statement) the Word file must carry")
    ap.add_argument("--names", action="append", default=[],
                    help="private names the Word properties must not carry: a text file (one per line) or <prefix>_run_private.json; repeatable")
    a = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    from omop_etl.manuscript_text import load_params

    params = load_params(a.params) if a.params else None
    res = release_checks(a.pdf, a.workbook, docx=a.docx, run_json=a.run_json, final=a.final, params=params, names=_names(a.names))
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + (f"  ({r['detail']})" if r["detail"] and not r["ok"] else ""))
    print(f"{sum(r['ok'] for r in res)} of {len(res)} checks passed")
    return 0 if all(r["ok"] for r in res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
