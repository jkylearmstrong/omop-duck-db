"""Compare a rerun with a delivered manuscript (workbook, and optionally its PDF and DOCX) with a fixed, recorded method.

The comparison with the workbook delivered on 2026-11-11 used to be done by hand, or by a script written for the occasion, so two
reruns could give different counts for the same question (how many cells moved, how many quoted numbers changed). This module
writes the method down once:

* :func:`match_sites` pairs the site rows of a sheet by their numbers, not their letters (letters are reassigned on purpose between
  runs), and says whether the pairing is unambiguous.
* :func:`compare_with_delivered` answers the standard questions (sheet names, labels, titles, cohort N, per-sheet differing cells,
  Table 2 AUCs, LOSO mean, Table 3 selections, Table 6 direction and p < 0.05, site letters, quoted numbers in the delivered PDF)
  and returns two things: ``answers`` (aggregate only: counts and largest changes, no site letters and no per-site values, safe to
  paste into a public thread) and ``detail`` (Markdown with per-site pairings and values, which stays on the machine that holds the data).

Cells are compared exactly (every number, the last digit included, and the text): a CI bound that moved by 0.001 counts.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from omop_etl.manuscript_tables import _numbers, compare_workbooks

__all__ = ["DESCRIPTIVE_SHEETS", "MODEL_SHEETS", "match_sites", "compare_with_delivered", "quoted_numbers_changed", "write_comparison"]

#: Sheets with descriptive numbers (item 8): largest relative change is reported.
DESCRIPTIVE_SHEETS = ("Table 1 - Descriptive", "Table 6 - Multivariable LR", "Table S1 - LOS Distribution", "Table S2 - Data Completeness",
                      "Table S3 - CONSORT Attrition")
#: Sheets with model metrics (item 9): largest absolute change is reported.
MODEL_SHEETS = ("Table 2 - Model Performance", "Table 3 - Tuning AUC vs F1", "Table 4 - Stay Type Decomp", "Table 5 - Cross-Site LOSO",
                "Table S4 - All Tuning Grid")
SITE_SHEETS = ("Table 5 - Cross-Site LOSO", "Table S1 - LOS Distribution", "Table S3 - CONSORT Attrition")
_SITE = re.compile(r"^Site [A-Z]+$")
_HDR = 2  # title row, blank row, header row


def _read(path: str | Path) -> dict[str, pd.DataFrame]:
    return pd.read_excel(path, sheet_name=None, header=None)


def _table(raw: pd.DataFrame) -> pd.DataFrame:
    t = raw.iloc[_HDR + 1:].reset_index(drop=True)
    t.columns = [str(c) for c in raw.iloc[_HDR].tolist()]
    return t


def _title(raw: pd.DataFrame) -> str:
    return str(raw.iat[0, 0]) if raw.shape[0] and raw.shape[1] else ""


def _site_vectors(raw: pd.DataFrame) -> dict[str, dict[str, list[float]]]:
    """Site label -> {row key: numbers} for the site rows of one sheet. Table S3 has several rows per site, keyed by their stage
    (``Stage / Description``) and holding only the remaining stays and patients, so a step present in one workbook only, or a renumbered
    step, does not shift the other numbers. Other sheets have one row per site, keyed ``""``, holding all its numbers."""
    t = _table(raw)
    out: dict[str, dict[str, list[float]]] = {}
    first = t.columns[0]
    stage = next((c for c in t.columns if c.startswith("Stage")), None)
    remaining = [c for c in t.columns if c.startswith("Remaining")]
    for _, r in t.iterrows():
        lab = str(r[first]).strip()
        if not _SITE.match(lab):
            continue
        if stage is not None and remaining:
            out.setdefault(lab, {})[str(r[stage]).strip()] = [x for c in remaining for x in _numbers(r[c])]
        else:
            out.setdefault(lab, {}).setdefault("", []).extend(x for v in r.iloc[1:] for x in _numbers(v))
    return out


def _site_column(raw: pd.DataFrame, starts: str) -> dict[str, list[float]]:
    """Site label -> [the number in the first column whose header starts with ``starts``, from the site's last row]."""
    t = _table(raw)
    col = next((c for c in t.columns if c.startswith(starts)), None)
    out: dict[str, list[float]] = {}
    if col is None:
        return out
    for _, r in t.iterrows():
        lab = str(r[t.columns[0]]).strip()
        n = _numbers(r[col])
        if _SITE.match(lab) and n:
            out[lab] = n[:1]
    return out


def match_sites(new: Mapping[str, Any], old: Mapping[str, Any], *, margin: float = 0.5) -> tuple[dict[str, str], bool]:
    """Pairs new site labels with old ones by their numbers. Returns ``({new label: old label}, unambiguous)``.

    Each site holds a list of numbers or ``{row key: numbers}`` (see :func:`_site_vectors`). The cost of a pair is the mean relative
    difference of their numbers (position by position, over the row keys both have). The pairing is unambiguous when every
    old site's closest new site is a different one, each is also that new site's closest old site, and the runner-up costs at least
    ``1 / margin`` times as much (default: twice). Otherwise the closest pairs are taken greedily and ``unambiguous`` is False."""
    nl, ol = list(new), list(old)
    if not nl or not ol:
        return {}, False

    def cost_list(a: list[float], b: list[float]) -> list[float]:
        k = min(len(a), len(b))
        x, y = np.asarray(a[:k], dtype=float), np.asarray(b[:k], dtype=float)
        return list(np.abs(x - y) / np.maximum(np.abs(y), 1e-9))

    def cost(a: Any, b: Any) -> float:
        if isinstance(a, Mapping):  # {row key: numbers}: compare the rows both sites have
            d = [v for k in a if k in b for v in cost_list(a[k], b[k])]
        else:
            d = cost_list(a, b) + ([1.0] if len(a) != len(b) else [])
        return float(np.mean(d)) if d else float("inf")

    c = np.array([[cost(new[n], old[o]) for o in ol] for n in nl])
    ok = len(nl) == len(ol)
    if ok:
        for j in range(len(ol)):
            i = int(np.argmin(c[:, j]))
            col = np.sort(c[:, j])
            if int(np.argmin(c[i, :])) != j or (len(col) > 1 and not col[0] <= margin * col[1]):
                ok = False
                break
    pairs: dict[str, str] = {}
    taken_n, taken_o = set(), set()
    for flat in np.argsort(c, axis=None):
        i, j = divmod(int(flat), len(ol))
        if i in taken_n or j in taken_o:
            continue
        pairs[nl[i]] = ol[j]
        taken_n.add(i)
        taken_o.add(j)
    return pairs, ok


_BENCH = re.compile(r"\s*\(benchmark\)\s*$")


def _config(v: Any) -> Any:
    """A configuration label without the "(benchmark)" suffix, which the delivered workbook adds to the fixed configuration."""
    return _BENCH.sub("", v) if isinstance(v, str) else v


def _normalised_copy(src: Path, mappings: Mapping[str, Mapping[str, str]], out: Path) -> Path:
    """A copy of a workbook with the site labels of each sheet in ``mappings`` replaced (all at once), and the "(benchmark)" suffix
    taken off every cell of Tables 3 and S4 (a label, not a result)."""
    import openpyxl

    wb = openpyxl.load_workbook(src)
    for sheet, m in mappings.items():
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        for row in ws.iter_rows(min_row=_HDR + 2):
            cell = row[0]
            if isinstance(cell.value, str) and cell.value.strip() in m:
                cell.value = m[cell.value.strip()]
    for sheet in ("Table 3 - Tuning AUC vs F1", "Table S4 - All Tuning Grid"):
        if sheet in wb.sheetnames:
            for row in wb[sheet].iter_rows(min_row=_HDR + 2):
                for cell in row:
                    cell.value = _config(cell.value)
    wb.save(out)
    return out


def _pdf_pages(path: Path) -> list[str]:
    from pypdf import PdfReader

    return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))


def _section_sites(text: str, heading: str = "Table 5") -> list[str]:
    """Site labels in the order they appear in the section that starts at the last ``heading:`` caption (the table, not a mention)."""
    starts = [m.start() for m in re.finditer(re.escape(heading) + r"\s*:", text)]
    if not starts:
        return []
    body = text[starts[-1]:]
    nxt = re.search(r"Table [0-9S]+\w*\s*:", body[len(heading) + 1:])
    body = body[: len(heading) + 1 + nxt.start()] if nxt else body
    return [re.sub(r"\s+", " ", s) for s in re.findall(r"Site\s+[A-Z]+\b", body)]


def _prose_lines(text: str) -> list[str]:
    """The lines of a page that read as sentences: at least 6 words of letters, which are at least 60% of the tokens. A row of a
    printed table (a label then numbers) is not prose."""
    out = []
    for line in text.splitlines():
        toks = line.split()
        words = [t for t in toks if re.fullmatch(r"[A-Za-z][A-Za-z'()\-]*[.,;:]?", t)]
        if len(words) >= 6 and len(words) >= 0.6 * len(toks):
            out.append(line)
    return out


def quoted_numbers_changed(pdf_pages: list[str], differences: pd.DataFrame, *, min_chars: int = 4) -> dict[str, Any]:
    """Item 17, with a fixed method. From every differing cell, the old value's numbers that changed (same position, different value)
    are written as the old cell printed them; each one with at least ``min_chars`` characters (so "0.731", "1,234", "23.4", not "5")
    is searched as a whole token in the delivered PDF. ``count`` and ``pages`` count hits in prose lines only (:func:`_prose_lines`):
    the numbers the text quotes, not the tables printed in the PDF, which hold every changed cell by construction.
    ``count_all_text`` / ``pages_all_text`` count every line. ``distinct_numbers`` is the number of changed numbers searched for."""
    tokens: set[str] = set()
    for r in differences.itertuples(index=False):
        old_s, new_s = str(r.old), str(r.new)
        old_raw = re.findall(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?", old_s)
        new_n = _numbers(new_s)
        for k, tok in enumerate(old_raw):
            v = float(tok.replace(",", ""))
            if k < len(new_n) and new_n[k] == v:
                continue
            tok = tok.lstrip("-")
            if len(tok) >= min_chars:
                tokens.add(tok)
    pats = [re.compile(r"(?<![\d.,])" + re.escape(tok) + r"(?![\d]|[.,]\d)") for tok in tokens]

    def count(texts: list[str]) -> tuple[int, list[int]]:
        hits, pages = 0, set()
        for i, text in enumerate(texts, start=1):
            n = sum(len(p.findall(text)) for p in pats)
            if n:
                hits += n
                pages.add(i)
        return hits, sorted(pages)

    prose, prose_pages = count(["\n".join(_prose_lines(t)) for t in pdf_pages])
    every, every_pages = count(pdf_pages)
    return {"count": prose, "pages": prose_pages, "count_all_text": every, "pages_all_text": every_pages, "distinct_numbers": len(tokens)}


def _max_rel(d: pd.DataFrame) -> float | None:
    best = None
    for r in d.itertuples(index=False):
        x, y = _numbers(r.new), _numbers(r.old)
        for p, q in zip(x, y):
            if q:
                v = abs(p - q) / abs(q)
                best = v if best is None else max(best, v)
    return None if best is None else round(best, 4)


def _max_abs(d: pd.DataFrame, column_hint: str | None = None) -> float | None:
    best = None
    for r in d.itertuples(index=False):
        if column_hint and column_hint.lower() not in str(r.column).lower():
            continue
        for p, q in zip(_numbers(r.new), _numbers(r.old)):
            best = abs(p - q) if best is None else max(best, abs(p - q))
    return None if best is None else round(best, 4)


def _compare_s4(new: Path, old: Path) -> tuple[int, pd.DataFrame, list[str] | None]:
    """Table S4 by model and configuration, whatever the other columns: the delivered workbook lists each configuration once per
    selection rule (three times), the package once. Rows are keyed ``Model | Configuration`` (first occurrence kept) and the columns
    both workbooks have are compared exactly. Returns (cells compared, differing cells, columns compared)."""
    empty = pd.DataFrame(columns=["sheet", "row", "column", "new", "old"])
    sheet = "Table S4 - All Tuning Grid"
    a_all, b_all = _read(new), _read(old)
    if sheet not in a_all or sheet not in b_all:
        return 0, empty, None
    a, b = _table(a_all[sheet]), _table(b_all[sheet])
    if not {"Model", "Configuration"} <= set(a.columns) & set(b.columns):
        return 0, empty, []

    def keyed(t: pd.DataFrame) -> pd.DataFrame:
        k = t["Model"].astype(str) + " | " + t["Configuration"].map(_config).astype(str)
        return t.assign(_k=k).drop_duplicates("_k").set_index("_k")

    a, b = keyed(a), keyed(b)
    cols = [c for c in b.columns if c in a.columns and c not in ("Model", "Configuration", "Selection")]
    cells, out = 0, []
    for k in b.index:
        if k not in a.index:
            continue
        for c in cols:
            x, y = _numbers(a.at[k, c]), _numbers(b.at[k, c])
            if not x and not y:
                continue
            cells += 1
            if x != y:
                out.append({"sheet": sheet, "row": k, "column": c, "new": a.at[k, c], "old": b.at[k, c]})
    return cells, pd.DataFrame(out, columns=empty.columns) if out else empty, cols


def _cohort_n(raw: pd.DataFrame | None) -> dict[str, int | None]:
    if raw is None:
        return {"n": None, "readmitted": None}
    m = re.search(r"N\s*=\s*([\d,]+)", _title(raw))
    t = _table(raw)
    readm = None
    if t.shape[1] > 1 and len(t):
        nums = _numbers(t.iat[0, 2]) if t.shape[1] > 2 else []
        readm = int(nums[0]) if nums else None
    return {"n": int(m.group(1).replace(",", "")) if m else None, "readmitted": readm}


def _p(v: Any) -> float | None:
    s = str(v).strip()
    if s.startswith("<"):
        n = _numbers(s[1:])
        return n[0] / 2 if n else None
    n = _numbers(s)
    return n[0] if n else None


def _labels(raw: pd.DataFrame, n_key: int) -> list[str]:
    t = _table(raw)
    return [" | ".join(str(v) for v in r) for r in t.iloc[:, :n_key].itertuples(index=False)]


def _generic(label: str) -> str:
    return re.sub(r"\bSite [A-Z]+\b", "Site ?", label)


def compare_with_delivered(new_workbook: str | Path, delivered_workbook: str | Path, *, new_pdf: str | Path | None = None,
                           delivered_pdf: str | Path | None = None, new_docx: str | Path | None = None,
                           delivered_pdf_sha256: str | None = None, previous: Mapping[str, Any] | None = None,
                           new_only_rows: tuple[str, ...] = ("Lactate", "pCO2", "pO2", "Troponin I")) -> dict[str, Any]:
    """Answers the standard questions for a rerun against a delivered manuscript. Returns ``{"answers": {...}, "detail": str}``.

    ``answers`` holds aggregates only (safe to share): see the module docstring. ``detail`` is Markdown with the per-site pairings and
    per-site values; keep it with the data. ``previous`` is an earlier ``answers`` dict (for example the last clean run's), and item 18
    lists the answers that changed since. ``new_only_rows`` are row labels the rerun must not have when it is meant to line up with the
    delivered workbook (item 19)."""
    new_workbook, delivered_workbook = Path(new_workbook), Path(delivered_workbook)
    A, B = _read(new_workbook), _read(delivered_workbook)
    ans: dict[str, Any] = {}
    det: list[str] = ["# Comparison with the delivered manuscript (detail; keep with the data)", ""]

    # 1-3 files
    if delivered_pdf is not None:
        h = hashlib.sha256(Path(delivered_pdf).read_bytes()).hexdigest()
        ans["1_delivered_pdf_hash_matches"] = (h == delivered_pdf_sha256) if delivered_pdf_sha256 else None
        ans["1_delivered_pdf_sha256"] = h
    if new_pdf is not None and Path(new_pdf).exists():
        h2 = hashlib.sha256(Path(new_pdf).read_bytes()).hexdigest()
        ans["2_new_pdf_differs"] = h2 != ans.get("1_delivered_pdf_sha256")
        ans["2_new_pdf_sha256"] = h2
    ans["3_files_present"] = all(p is not None and Path(p).exists() for p in (new_workbook, new_pdf, new_docx))

    # 4 sheet names
    ans["4_same_sheets"] = list(A) == list(B)
    ans["4_sheet_differences"] = sorted(set(A) ^ set(B))

    # site pairing per sheet (by numbers)
    mappings: dict[str, dict[str, str]] = {}
    unamb: dict[str, bool] = {}
    det += ["## Site pairing (new label -> delivered label)", ""]
    for sheet in SITE_SHEETS:
        if sheet in A and sheet in B:
            m, ok = match_sites(_site_vectors(A[sheet]), _site_vectors(B[sheet]))
            mappings[sheet], unamb[sheet] = m, ok
            det.append(f"- {sheet}: " + ", ".join(f"{k} -> {v}" for k, v in sorted(m.items())) + ("" if ok else "  (AMBIGUOUS)"))
    ans["14_site_matching_unambiguous"] = bool(unamb) and all(unamb.values())
    ans["14_ambiguous_sheets"] = [k for k, v in unamb.items() if not v]
    det.append("")

    # 15 same letter per site across Table 5, S1, S3 in the new run: pair the new sheets with each other by numbers too
    ans["15_same_letter_across_sheets"] = None
    if all(s in A for s in SITE_SHEETS):
        n5 = _site_column(A["Table 5 - Cross-Site LOSO"], "Test patients")
        s1 = _site_column(A["Table S1 - LOS Distribution"], "Total Cohort")
        s3 = _site_column(A["Table S3 - CONSORT Attrition"], "Remaining Patients")
        # by size order: Table S3's last step need not be the final cohort (it is not under the delivered layout), but the sites keep
        # their order of size; a tie leaves the answer open (None)
        def order(d: dict[str, list[float]]) -> list[str] | None:
            vals = [v[0] for v in d.values()]
            return None if len(set(vals)) < len(vals) else sorted(d, key=lambda k: -d[k][0])

        o5, o1, o3 = order(n5), order(s1), order(s3)
        if None in (o5, o1, o3) or not (len(o5) == len(o1) == len(o3)):
            ans["15_same_letter_across_sheets"] = None
        else:
            ans["15_same_letter_across_sheets"] = o5 == o1 == o3
            ans["15_sheets_out_of_step"] = [n for n, o in (("Table S1 - LOS Distribution", o1), ("Table S3 - CONSORT Attrition", o3)) if o != o5]

    # 5, 6 labels and titles, ignoring site letters
    kc = {"Table 3 - Tuning AUC vs F1": 2, "Table S4 - All Tuning Grid": 2, "Table S3 - CONSORT Attrition": 2}
    lab_diff, order_diff, title_diff = [], [], []
    for sheet in B:
        if sheet not in A or sheet.startswith("Manifest"):
            continue
        la = [_generic(x) for x in _labels(A[sheet], kc.get(sheet, 1))]
        lb = [_generic(x) for x in _labels(B[sheet], kc.get(sheet, 1))]
        ha, hb = [str(x) for x in A[sheet].iloc[_HDR]], [str(x) for x in B[sheet].iloc[_HDR]]
        if sorted(set(la)) != sorted(set(lb)) or len(la) != len(lb) or ha != hb:
            lab_diff.append(sheet)
        elif la != lb:
            order_diff.append(sheet)
        if _title(A[sheet]) != _title(B[sheet]):
            title_diff.append(sheet)
    ans["5_same_labels_and_headers"] = not lab_diff
    ans["5_sheets_with_label_or_header_differences"] = lab_diff
    ans["5_sheets_with_row_order_differences_only"] = order_diff
    ans["6_same_titles"] = not title_diff
    ans["6_sheets_with_title_differences"] = title_diff

    # 7 cohort N
    na, nb = _cohort_n(A.get("Table 1 - Descriptive")), _cohort_n(B.get("Table 1 - Descriptive"))
    ans["7_same_cohort_n"] = na == nb
    ans["7_cohort_n"] = {"delivered": nb, "new": na}

    # 8, 9 exact cell comparison after relabelling sites
    with tempfile.TemporaryDirectory() as td:
        rel = _normalised_copy(new_workbook, mappings, Path(td) / "new_normalised.xlsx")
        dlv = _normalised_copy(delivered_workbook, {}, Path(td) / "delivered_normalised.xlsx")
        summary, diffs = compare_workbooks(rel, dlv, atol=0.0, rtol=0.0, text=True)
        s4_cells, s4_diffs, s4_cols = _compare_s4(rel, dlv)
    diffs = pd.concat([diffs[diffs["sheet"] != "Table S4 - All Tuning Grid"], s4_diffs], ignore_index=True)
    compared = {r.sheet: int(r.cells or 0) for r in summary.itertuples(index=False)}
    compared["Table S4 - All Tuning Grid"] = s4_cells
    per8, per9 = {}, {}
    for sheet in DESCRIPTIVE_SHEETS:
        d = diffs[diffs["sheet"] == sheet]
        if sheet in B:
            per8[sheet] = {"cells_compared": compared.get(sheet, 0), "differing_cells": int(len(d)), "max_rel_change": _max_rel(d)}
    for sheet in MODEL_SHEETS:
        d = diffs[diffs["sheet"] == sheet]
        if sheet in B:
            per9[sheet] = {"cells_compared": compared.get(sheet, 0), "differing_cells": int(len(d)), "max_abs_auc_change": _max_abs(d, "AUC")}
    if s4_cols is not None:
        per9.setdefault("Table S4 - All Tuning Grid", {})["columns_compared"] = s4_cols
    ans["8_descriptive_identical"] = all(v["differing_cells"] == 0 for v in per8.values())
    ans["8_per_sheet"] = per8
    ans["9_model_tables_identical"] = all(v["differing_cells"] == 0 for v in per9.values())
    ans["9_per_sheet"] = per9

    # 10 Table 2 AUCs, 11 LOSO mean (aggregate, model-level)
    t2a, t2b = (_table(X["Table 2 - Model Performance"]) if "Table 2 - Model Performance" in X else None for X in (A, B))
    if t2a is not None and t2b is not None and "AUC-ROC (95% CI)" in t2a and "AUC-ROC (95% CI)" in t2b:
        ans["10_table2_auc"] = {str(m): {"delivered": str(t2b.set_index("Model").at[m, "AUC-ROC (95% CI)"]),
                                         "new": str(t2a.set_index("Model").at[m, "AUC-ROC (95% CI)"]) if m in set(t2a["Model"]) else None}
                                for m in t2b["Model"]}

    def loso_mean(X: dict[str, pd.DataFrame]) -> float | None:
        if "Table 5 - Cross-Site LOSO" not in X:
            return None
        t = _table(X["Table 5 - Cross-Site LOSO"])
        row = t[t.iloc[:, 0].astype(str).str.startswith("Mean")]
        col = [c for c in t.columns if c.startswith("AUC")]
        return _numbers(row.iloc[0][col[0]])[0] if len(row) and col and _numbers(row.iloc[0][col[0]]) else None

    ans["11_loso_mean_auc"] = {"delivered": loso_mean(B), "new": loso_mean(A)}

    # 12 Table 3 selections
    s3a, s3b = (_table(X["Table 3 - Tuning AUC vs F1"]) if "Table 3 - Tuning AUC vs F1" in X else None for X in (A, B))
    if s3a is not None and s3b is not None and {"Model", "Selection", "Configuration"} <= set(s3a.columns) & set(s3b.columns):
        ka = {(r.Model, r.Selection): _config(r.Configuration) for r in s3a.itertuples(index=False)}
        kb = {(r.Model, r.Selection): _config(r.Configuration) for r in s3b.itertuples(index=False)}
        changed = {f"{m} / {s}": {"delivered": kb[(m, s)], "new": ka.get((m, s))} for (m, s) in kb if ka.get((m, s)) != kb[(m, s)]}
        ans["12_selections_unchanged"] = not changed
        ans["12_changed_selections"] = changed

    # 13 Table 6 direction and p < 0.05
    if "Table 6 - Multivariable LR" in A and "Table 6 - Multivariable LR" in B:
        a6, b6 = (_table(X["Table 6 - Multivariable LR"]).set_index("Feature") for X in (A, B))
        orc = [c for c in b6.columns if c.startswith("Adjusted OR")]
        flips, cross = [], []
        for k in b6.index:
            if k not in a6.index or not orc:
                continue
            x, y = _numbers(a6.at[k, orc[0]]) if not isinstance(a6.at[k, orc[0]], pd.Series) else [], _numbers(b6.at[k, orc[0]]) \
                if not isinstance(b6.at[k, orc[0]], pd.Series) else []
            if x and y and (x[0] > 1) != (y[0] > 1) and not (x[0] == 1 or y[0] == 1):
                flips.append(k)
            pa, pb = _p(a6.at[k, "P-Value"]) if "P-Value" in a6 else None, _p(b6.at[k, "P-Value"]) if "P-Value" in b6 else None
            if pa is not None and pb is not None and (pa < 0.05) != (pb < 0.05):
                cross.append(k)
        ans["13_no_direction_or_significance_change"] = not flips and not cross
        ans["13_direction_changes"], ans["13_p_crossings"] = flips, cross

    # 16 DOCX / PDF Table 5 letters match the workbook
    wb5 = list(_site_vectors(A["Table 5 - Cross-Site LOSO"])) if "Table 5 - Cross-Site LOSO" in A else []
    doc_ok = []
    if new_docx is not None and Path(new_docx).exists():
        doc_ok.append(_section_sites(_docx_text(Path(new_docx))) == wb5)
    if new_pdf is not None and Path(new_pdf).exists():
        doc_ok.append(_section_sites("\n".join(_pdf_pages(Path(new_pdf)))) == wb5)
    ans["16_document_table5_letters_match"] = all(doc_ok) if doc_ok else None

    # 17 quoted numbers that changed
    if delivered_pdf is not None and Path(delivered_pdf).exists():
        q = quoted_numbers_changed(_pdf_pages(Path(delivered_pdf)), diffs)
        ans["17_quoted_numbers_changed"] = q["count"] > 0
        ans["17_detail"] = q

    # 19 no rows the delivered workbook lacks
    extra = [lab for s in ("Table 1 - Descriptive", "Table 6 - Multivariable LR", "Table S2 - Data Completeness") if s in A
             for lab in _labels(A[s], 1) if any(lab == r or lab.startswith(r + " ") or lab.startswith(r + ":") for r in new_only_rows)]
    ans["19_rows_line_up"] = not extra

    # 18 changes since a previous answer set
    if previous is not None:
        ans["18_changed_since_previous"] = sorted(k for k in ans if k in previous and previous[k] != ans[k] and not k.startswith("18"))

    det += ["## Differing cells (sites paired as above)", ""]
    if len(diffs):
        det.append(diffs.to_markdown(index=False))
    else:
        det.append("None.")
    return {"answers": ans, "detail": "\n".join(det) + "\n"}


def write_comparison(result: Mapping[str, Any], answers_json: str | Path, detail_md: str | Path) -> None:
    """Writes ``answers`` as JSON (shareable) and ``detail`` as Markdown (keep with the data)."""
    Path(answers_json).write_text(json.dumps(result["answers"], indent=2, default=str), encoding="utf-8")
    Path(detail_md).write_text(result["detail"], encoding="utf-8")
