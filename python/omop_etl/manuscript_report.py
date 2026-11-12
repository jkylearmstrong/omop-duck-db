"""Results report from a manuscript workbook: the numbers the text quotes, every table, and the figures, in one folder.

A manuscript draft quotes the same numbers the tables hold (cohort size, readmission rate, AUC range, site range). Typing them
by hand is how a text and its tables drift apart, so :func:`manuscript_facts` reads them from the workbook that
:func:`omop_etl.run_study` wrote and :func:`render_results_report` writes ``manuscript_facts.json``, one CSV per sheet, a
``Results.md`` and a self-contained ``Results.html`` (tables, figures, and a sentence for each headline result). The facts file is
what a Quarto or Word template reads (``{{< meta >}}``, ``json.load``) so that the text is generated from the run and not
copied from an earlier one.
"""
from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = ["read_workbook_tables", "manuscript_facts", "render_results_report"]


def read_workbook_tables(path: str | Path) -> dict[str, tuple[str, pd.DataFrame]]:
    """``{sheet name: (title, table)}`` for every sheet of a manuscript workbook (title in row 1, header in row 3)."""
    out: dict[str, tuple[str, pd.DataFrame]] = {}
    for name, raw in pd.read_excel(path, sheet_name=None, header=None).items():
        if raw.shape[0] < 4:
            out[name] = (str(raw.iat[0, 0]) if raw.shape[0] else name, pd.DataFrame())
            continue
        t = raw.iloc[3:].reset_index(drop=True)
        t.columns = [str(c) for c in raw.iloc[2].tolist()]
        out[name] = (str(raw.iat[0, 0]), t.dropna(how="all"))
    return out


def _num(x: Any) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return float(m.group(0)) if m else float("nan")


def _find(tables: Mapping[str, tuple[str, pd.DataFrame]], prefix: str) -> pd.DataFrame | None:
    for k, (_, t) in tables.items():
        if k.startswith(prefix):
            return t
    return None


def manuscript_facts(path: str | Path, *, window: Sequence[str] | None = None, seed: int | None = None) -> dict[str, Any]:
    """The headline numbers of a manuscript workbook as a flat dictionary (None where the workbook does not hold them).

    Keys: ``n_patients``, ``n_readmitted``, ``n_not_readmitted``, ``readmission_rate`` (percent), ``models`` (name -> AUC text),
    ``auc_min``, ``auc_max``, ``best_model``, ``auc_range`` (text), ``sites`` (label -> test patients), ``loso_auc_min`` / ``loso_auc_max``,
    ``n_sites``, plus ``admit_first`` / ``admit_last`` from ``window`` and ``seed`` when given."""
    tabs = read_workbook_tables(path)
    f: dict[str, Any] = {"workbook": Path(path).name, "admit_first": window[0] if window else None, "admit_last": window[1] if window else None, "seed": seed}
    t1 = _find(tabs, "Table 1")
    if t1 is not None and len(t1) and "Whole cohort" in t1.columns:
        row = t1[t1.iloc[:, 0].astype(str).str.contains("Unique Patients", na=False)]
        if len(row):
            r = row.iloc[0]
            f["n_patients"] = int(_num(r["Whole cohort"]))
            f["n_readmitted"] = int(_num(r.iloc[2]))
            f["n_not_readmitted"] = int(_num(r.iloc[3]))
            f["readmission_rate"] = round(100 * f["n_readmitted"] / f["n_patients"], 2)
    t2 = _find(tabs, "Table 2")
    if t2 is not None and len(t2):
        col = [c for c in t2.columns if "AUC" in c][0]
        aucs = {str(m): str(v) for m, v in zip(t2.iloc[:, 0], t2[col])}
        vals = {m: _num(v) for m, v in aucs.items()}
        f["models"] = aucs
        f["auc_min"], f["auc_max"] = min(vals.values()), max(vals.values())
        f["best_model"] = max(vals, key=vals.get)
        f["auc_range"] = f"{f['auc_min']:.2f} to {f['auc_max']:.2f}"
    t5 = _find(tabs, "Table 5")
    if t5 is not None and len(t5):
        auc = [c for c in t5.columns if "AUC" in c][0]
        site = t5.iloc[:, 0].astype(str)
        keep = site.str.startswith("Site") | ~site.str.contains("Mean|Pooled|Overall", case=False, regex=True)
        a = t5.loc[keep, auc].map(_num)
        f["sites"] = {s: int(_num(n)) for s, n in zip(site[keep], t5.loc[keep, "Test patients"])} if "Test patients" in t5 else {}
        f["n_sites"] = int(keep.sum())
        f["loso_auc_min"], f["loso_auc_max"] = float(a.min()), float(a.max())
    return f


def _md_table(t: pd.DataFrame) -> str:
    if t.empty:
        return "_(empty)_"
    cells = t.astype(object).where(t.notna(), "").astype(str).values.tolist()
    head = "| " + " | ".join(str(c) for c in t.columns) + " |\n|" + "---|" * len(t.columns) + "\n"
    return head + "\n".join("| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in cells)


def _sentences(f: Mapping[str, Any]) -> list[str]:
    s = []
    if f.get("n_patients"):
        win = f" between {f['admit_first']} and {f['admit_last']}" if f.get("admit_first") else ""
        s.append(f"The cohort has {f['n_patients']:,} patients, each with one randomly drawn qualifying inpatient stay{win}; "
                 f"{f['n_readmitted']:,} ({f['readmission_rate']:.1f}%) were readmitted within 30 days.")
    if f.get("auc_range"):
        s.append(f"AUC-ROC ranged from {f['auc_range']} across the benchmark models (highest: {f['best_model']}).")
    if f.get("loso_auc_min") is not None:
        s.append(f"Leave-one-site-out AUC-ROC ranged from {f['loso_auc_min']:.2f} to {f['loso_auc_max']:.2f} across {f['n_sites']} held-out sites.")
    return s


_ID_COLUMNS = re.compile(r"^(person_id|patient_id|patid|mrn|visit_occurrence_id|encounter_?id|visit_id|zip|zip_?code|dob|birth_datetime|ssn)$", re.I)
_COUNT_COLUMNS = re.compile(r"\b(n|count|patients?|stays?|subjects?)\b|\(n\)", re.I)
_N_PCT = re.compile(r"^\s*(\d+)\s*\(\s*[\d.]+\s*%?\s*\)\s*$")


def _check_aggregate(name: str, t: pd.DataFrame, max_rows: int) -> None:
    """Refuses a sheet that looks like patient-level data (an identifier column, or more rows than a summary table has)."""
    bad = [c for c in t.columns if _ID_COLUMNS.match(str(c).strip())]
    if bad:
        raise ValueError(f"Sheet {name!r} has identifier column(s) {bad}; the report holds aggregate tables only.")
    if len(t) > max_rows:
        raise ValueError(f"Sheet {name!r} has {len(t)} rows (> max_rows={max_rows}); the report holds aggregate tables only. Raise max_rows or exclude the sheet.")


def _suppress_small(t: pd.DataFrame, min_cell: int) -> pd.DataFrame:
    """Replaces counts of 1..min_cell-1 in count-like columns (and "n (%)" text cells anywhere) with ``<min_cell``."""
    label = f"<{min_cell}"
    t = t.copy().astype(object)

    def small(v) -> bool:
        s = str(v).strip().replace(",", "")
        return bool(re.fullmatch(r"\d+", s)) and 0 < int(s) < min_cell

    for c in t.columns:
        if _COUNT_COLUMNS.search(str(c)):
            t[c] = t[c].map(lambda v: label if small(v) else v)
    return t.map(lambda v: label if isinstance(v, str) and (m := _N_PCT.match(v)) and 0 < int(m.group(1)) < min_cell else v)


def render_results_report(workbook: str | Path, out_dir: str | Path, *, figures: Sequence[str | Path] | str | Path | None = None,
                          window: Sequence[str] | None = None, seed: int | None = None, title: str = "Results",
                          sheets: Sequence[str] | None = None, exclude_sheets: Sequence[str] = (), formats: Sequence[str] = ("md", "html"),
                          write_csv: bool = True, write_facts: bool = True, sentences: bool = True, min_cell: int | None = 11,
                          max_rows: int = 500, embed_figures: bool = True, css: str | None = None) -> dict[str, Path]:
    """Writes ``manuscript_facts.json``, ``tables/<sheet>.csv``, ``Results.md`` and ``Results.html`` under ``out_dir``.

    The report holds **aggregate tables only**: a sheet with an identifier column (``person_id``, ``mrn``, ``zip``, ...) or more than
    ``max_rows`` rows is refused, counts below ``min_cell`` (default 11; ``None`` = off) are shown as ``<11``, the facts file names the
    workbook by file name (no local path), and nothing is read from the patient-level feature table. Site names are whatever labels the
    workbook carries (use :func:`omop_etl.assign_site_labels`).

    Parameters: ``figures`` (a list of image files or a folder of ``*.png``; linked in the Markdown, embedded in the HTML unless
    ``embed_figures=False``), ``sheets`` / ``exclude_sheets`` (sheet-name prefixes to keep / drop, e.g. ``["Table 1", "Table 5"]``),
    ``formats`` (any of ``"md"``, ``"html"``), ``write_csv``, ``write_facts``, ``sentences`` (the headline sentences), ``css``
    (replaces the default HTML style). Returns the paths written."""
    bad_fmt = set(formats) - {"md", "html"}
    if bad_fmt:
        raise ValueError(f"Unknown formats {sorted(bad_fmt)}; use 'md' and/or 'html'.")
    tabs = read_workbook_tables(workbook)
    if sheets is not None:
        tabs = {k: v for k, v in tabs.items() if any(k.startswith(x) for x in sheets)}
    tabs = {k: v for k, v in tabs.items() if not any(k.startswith(x) for x in exclude_sheets)}
    for k, (_, t) in tabs.items():
        if not k.startswith("Manifest"):
            _check_aggregate(k, t, max_rows)
    if min_cell:
        tabs = {k: (ttl, t if k.startswith("Manifest") else _suppress_small(t, min_cell)) for k, (ttl, t) in tabs.items()}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if write_csv:
        (out / "tables").mkdir(parents=True, exist_ok=True)
    facts = manuscript_facts(workbook, window=window, seed=seed)
    paths: dict[str, Path] = {}
    if write_facts:
        paths["facts"] = out / "manuscript_facts.json"
        paths["facts"].write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    sent = _sentences(facts) if sentences else []
    imgs: list[Path] = []
    if figures is not None:
        fp = Path(figures) if isinstance(figures, (str, Path)) else None
        imgs = sorted(fp.glob("*.png")) if fp is not None and fp.is_dir() else [Path(x) for x in ([figures] if fp is not None else figures)]
    md = [f"# {title}", "", *sent, ""]
    body = ["<h1>%s</h1>" % html.escape(title), *["<p>%s</p>" % html.escape(x) for x in sent]]
    for name, (ttl, t) in tabs.items():
        if name.startswith("Manifest"):
            continue
        if write_csv:
            t.to_csv(out / "tables" / (re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + ".csv"), index=False)
        md += [f"## {ttl}", "", _md_table(t), ""]
        body += ["<h2>%s</h2>" % html.escape(ttl), t.astype(object).where(t.notna(), "").to_html(index=False, border=0, classes="t", escape=True)]
    for p in imgs:
        md += [f"![{p.stem}]({p.name})", ""]
        src = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode() if embed_figures else html.escape(p.name)
        body.append(f'<figure><img style="max-width:100%" src="{src}"><figcaption>{html.escape(p.stem)}</figcaption></figure>')
    if "md" in formats:
        paths["markdown"] = out / "Results.md"
        paths["markdown"].write_text("\n".join(md), encoding="utf-8")
    style = css or "body{font-family:sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}table.t{border-collapse:collapse;font-size:.85rem}.t td,.t th{border-bottom:1px solid #ccc;padding:.2rem .5rem;text-align:left}"
    if "html" in formats:
        paths["html"] = out / "Results.html"
        paths["html"].write_text(f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><style>{style}</style>" + "\n".join(body), encoding="utf-8")
    return paths
