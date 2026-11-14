"""Re-issue a finished study run under a new anonymous site mapping, without recomputing anything.

A consortium sometimes has to change the letters after results went out (a mapping leaked, a site joined or left, a journal asks for a
fresh assignment). Re-running the study for that costs hours and can move the numbers (a random draw, a model refit). :func:`relabel_outputs`
instead copies the shareable outputs of a run and swaps every old label for the new one, all at once so that ``Site A -> Site B`` and
``Site B -> Site A`` do not collide: text files (CSV, JSON, Markdown, HTML), every string cell and sheet name of the workbook, and the run
record. The DOCX / PDF manuscript is rebuilt from the relabelled workbook, so all three files carry the new letters and nothing else
changes. Rows keep their order (a table sorted by letter is no longer sorted after a relabel).

Images are copied unchanged: the package's figures (cohort flow chart, SHAP panels) carry no site label, and an image with one cannot be
relabelled here, so it is listed in ``"unchanged"``. Parquet and other binary files are not copied (the shareable folder should not hold
them). The new folder is scanned for real site names (:mod:`omop_etl.run_privacy`) like a run is.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from omop_etl.study_run import RunPrivacyError, _privacy_names, assign_site_labels, load_site_labels, save_site_labels, validate_site_labels

__all__ = ["label_translation", "relabel_text", "relabel_outputs"]

_TEXT = {".csv", ".tsv", ".json", ".md", ".txt", ".html", ".htm"}
_IMAGES = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".tif", ".tiff"}
_REBUILT = {".docx", ".pdf", ".qmd"}


def label_translation(old: Mapping[str, str], new: Mapping[str, str], sites: Sequence[str] | None = None) -> dict[str, str]:
    """``{old label: new label}`` for ``sites`` (default: every site in ``old``), checked with :func:`omop_etl.validate_site_labels` on both sides."""
    sites = list(sites if sites is not None else old)
    o, n = validate_site_labels(sites, old), validate_site_labels(sites, new)
    for lab in o.values():
        if re.fullmatch(r"[A-Za-z0-9]{1,2}", lab):
            raise ValueError("Old labels as short as a bare letter cannot be swapped safely in text (they match ordinary words); use labels like 'Site A'.")
    return {o[s]: n[s] for s in sites}


def _pattern(mapping: Mapping[str, str]) -> re.Pattern:
    alts = sorted(mapping, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(a) for a in alts) + r")(?![A-Za-z0-9])")


def relabel_text(text: str, mapping: Mapping[str, str]) -> str:
    """Swaps every whole-word old label in ``text`` for its new label in one pass (``A->B`` and ``B->A`` together are fine)."""
    if not mapping:
        return text
    return _pattern(mapping).sub(lambda m: mapping[m.group(1)], text)


def _relabel_xlsx(src: Path, dst: Path, mapping: Mapping[str, str]) -> int:
    from openpyxl import load_workbook

    wb = load_workbook(src)
    n = 0
    for ws in wb.worksheets:
        new_title = relabel_text(ws.title, mapping)
        if new_title != ws.title:
            ws.title, n = new_title, n + 1
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str):
                    v = relabel_text(c.value, mapping)
                    if v != c.value:
                        c.value, n = v, n + 1
    for attr in ("title", "subject", "description", "keywords"):
        v = getattr(wb.properties, attr, None)
        if isinstance(v, str):
            setattr(wb.properties, attr, relabel_text(v, mapping))
    wb.save(dst)
    return n


def relabel_outputs(run_dir: str | Path, out_dir: str | Path, *, old_labels: Mapping[str, str] | str | Path,
                    new_labels: Mapping[str, str] | str | Path, label_seed: int | None = None, labels_out: str | Path | None = None,
                    prefix: str | None = None, document: bool = True, document_formats: Sequence[str] = ("docx", "pdf"),
                    document_title: str | None = None, document_author: str = "", site_names: Sequence[str] | str | Path | None = None,
                    privacy_check: bool = True) -> dict[str, Any]:
    """Copies the shareable outputs of ``run_dir`` to ``out_dir`` with every site label changed from ``old_labels`` to ``new_labels``.

    ``old_labels``: the ``site -> label`` mapping the run used (the file ``labels_out`` / ``--labels-out`` saved, or a dict).
    ``new_labels``: a mapping or mapping file, or ``"random"`` (a new random letter per site, repeatable with ``label_seed``; save it with
    ``labels_out``). Both must cover the same sites, the run's labels (from ``<prefix>_run.json``) must be the old ones, and each side must
    give every site its own label. ``prefix`` defaults to the one of the single ``*_run.json`` in ``run_dir``. ``document`` rebuilds the
    DOCX / PDF from the relabelled workbook. The privacy scan works as in :func:`omop_etl.run_study` and raises :class:`RunPrivacyError`.

    Returns ``{"translation": {old label: new label}, "files": [...], "unchanged": [...], "skipped": [...], "document": {...},
    "privacy_check": {...}}``."""
    src, out = Path(run_dir), Path(out_dir)
    if src.resolve() == out.resolve():
        raise ValueError("out_dir must differ from run_dir: the original run is left as it was.")
    old = load_site_labels(old_labels) if isinstance(old_labels, (str, Path)) else dict(old_labels)
    if prefix is None:
        runs = sorted(src.glob("*_run.json"))
        if len(runs) != 1:
            raise ValueError(f"run_dir has {len(runs)} *_run.json files; pass prefix.")
        prefix = runs[0].name[: -len("_run.json")]
    record = json.loads((src / f"{prefix}_run.json").read_text(encoding="utf-8"))
    run_labels = set(record.get("labels", []))
    sites = [s for s, lab in old.items() if lab in run_labels]
    if {old[s] for s in sites} != run_labels:
        raise ValueError(f"old_labels does not hold every label of the run ({len(run_labels)} labels in {prefix}_run.json); "
                         "pass the mapping the run was made with.")
    if isinstance(new_labels, str) and new_labels in ("random", "ordered"):
        new = assign_site_labels(sites, mode=new_labels, seed=label_seed)
    else:
        new = load_site_labels(new_labels) if isinstance(new_labels, (str, Path)) else dict(new_labels)
    trans = label_translation(old, new, sites)
    if labels_out is not None:
        save_site_labels({s: new[s] for s in sites}, labels_out)
    out.mkdir(parents=True, exist_ok=True)
    files, unchanged, skipped = [], [], []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        dst = out / Path(*[relabel_text(part, trans) for part in rel.parts])
        dst.parent.mkdir(parents=True, exist_ok=True)
        suf = p.suffix.lower()
        if suf in _TEXT:
            dst.write_text(relabel_text(p.read_text(encoding="utf-8"), trans), encoding="utf-8")
        elif suf in (".xlsx", ".xlsm"):
            _relabel_xlsx(p, dst, trans)
        elif suf in _IMAGES:
            shutil.copy2(p, dst)
            unchanged.append(dst.name)
        elif suf in _REBUILT and document:
            continue
        else:
            skipped.append(rel.as_posix())
            continue
        files.append(dst)
    rec_out = out / f"{prefix}_run.json"
    rec = json.loads(rec_out.read_text(encoding="utf-8"))
    rec["labels"] = sorted(rec.get("labels", []))
    rec["relabelled"] = True  # the letters were changed after the run; the mapping itself is never recorded
    rec_out.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    doc_record: dict[str, Any] = {"requested": bool(document)}
    staged: list[Path] = []
    wb = out / f"{prefix}_Manuscript_Tables.xlsx"
    stage = out.parent / f".{out.name}_manuscript_build"
    if document and wb.exists():
        from omop_etl.manuscript_document import render_manuscript_document

        shutil.rmtree(stage, ignore_errors=True)
        figs = [f for f in files if f.suffix.lower() == ".png" and f.name.startswith(prefix + "_")]
        window = record.get("window")
        d = render_manuscript_document(wb, stage, prefix=prefix, figures=figs, window=window, title=document_title, author=document_author,
                                       formats=document_formats)
        staged = [d["qmd"], *d["files"].values()]
        doc_record.update({"renderer": d["renderer"], "files": [p.name for p in staged], "missing": d["missing"], "note": d["note"]})
    privacy: dict[str, Any] = {"checked": False}
    hits: list = []
    if privacy_check:
        from omop_etl.run_privacy import scan, scan_files

        names = _privacy_names(sites, dict(enumerate([*old.values(), *new.values()])), site_names)
        if names:
            hits, sk = scan(out, names)
            h2, sk2 = scan_files(staged, names)
            hits += h2
            sk += sk2
            unread = [x for x in sk if not x.split(" (")[0].lower().endswith(tuple(_IMAGES))]
            privacy = {"checked": True, "n_names": len(names), "hits": len(hits), "hit_files": sorted({h[0] for h in hits}), "not_scanned": sk, "unreadable": unread}
            if unread and not hits:
                hits = [(x, 0, "not readable, so not shown to be clean") for x in unread]
    if staged and not hits:
        for p_ in staged:
            shutil.move(str(p_), out / p_.name)
            files.append(out / p_.name)
    shutil.rmtree(stage, ignore_errors=True)
    if hits:
        where = "; ".join(sorted({f"{h[0]} ({h[2]})" for h in hits})[:20])
        raise RunPrivacyError(f"{len(hits)} privacy finding(s) in the relabelled outputs, reported by file only: {where}. Do not share {out}.")
    return {"translation": trans, "files": [str(f) for f in files], "unchanged": unchanged, "skipped": skipped, "document": doc_record,
            "privacy_check": privacy, "prefix": prefix}
