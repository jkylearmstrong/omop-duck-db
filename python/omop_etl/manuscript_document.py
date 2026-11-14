"""The manuscript as a Word and a PDF document, built from one run's workbook and figures (anonymous site labels only).

:func:`render_manuscript_document` reads ``<prefix>_Manuscript_Tables.xlsx`` (the workbook :func:`omop_etl.run_study` writes), so the
document's numbers and site letters are the workbook's: there is no second site map to keep in step. It writes ``<prefix>_Manuscript.qmd``
(Quarto markdown: headline sentences, every table, every figure) and renders it with Quarto to ``<prefix>_Manuscript.docx`` and, through
Typst (bundled with Quarto, no LaTeX needed), ``<prefix>_Manuscript.pdf``. Without Quarto it falls back to Pandoc for the DOCX (and for the
PDF when a ``typst`` binary is on the PATH); with neither it keeps the ``.qmd`` and says why in the result, it does not raise.

Privacy: the document carries no author unless one is given, the Word file's creator / last-modified-by fields are rewritten to that author
(empty by default) so the account that rendered it is not recorded, figure alt text is the figure caption, and tables follow the same
aggregate-only and small-cell rules as :func:`omop_etl.render_results_report`. :func:`omop_etl.run_study` scans the finished files for real
site names before it moves them into the output folder.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

__all__ = ["render_manuscript_document", "find_renderer"]

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#@$|~^])")


def _esc(x: Any) -> str:
    """Markdown-safe cell text: no Quarto citation (@), math ($), raw HTML (<) or table break (|) by accident."""
    s = "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)
    return _MD_SPECIAL.sub(r"\\\1", s.replace("\r", " ").replace("\n", " "))


def _table_md(t: pd.DataFrame) -> str:
    if t.empty:
        return "_(empty)_"
    cols = [_esc(c) or " " for c in t.columns]
    rows = [[_esc(v) for v in r] for r in t.astype(object).where(t.notna(), "").values.tolist()]
    head = "| " + " | ".join(cols) + " |\n|" + "|".join([":---"] + ["---:"] * (len(cols) - 1)) + "|\n"
    return head + "\n".join("| " + " | ".join(r) + " |" for r in rows)


def find_renderer() -> dict[str, str | None]:
    """``{"quarto": path or None, "pandoc": path or None, "typst": path or None}``. ``QUARTO_PATH`` / ``PANDOC_PATH`` override the PATH lookup."""
    return {"quarto": os.environ.get("QUARTO_PATH") or shutil.which("quarto"),
            "pandoc": os.environ.get("PANDOC_PATH") or shutil.which("pandoc"),
            "typst": shutil.which("typst")}


def _caption(png: Path, prefix: str) -> str:
    stem = png.stem[len(prefix) + 1:] if png.stem.startswith(prefix + "_") else png.stem
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip()


def _scrub_docx(path: Path, author: str) -> None:
    """Sets the Word core properties that name a person (creator, last modified by) to ``author`` and removes custom properties."""
    tmp = path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/core.xml":
                xml = data.decode("utf-8")
                for tag in ("dc:creator", "cp:lastModifiedBy"):
                    xml = re.sub(rf"<{tag}>.*?</{tag}>|<{tag}/>", f"<{tag}>{_xml(author)}</{tag}>", xml, flags=re.S)
                data = xml.encode("utf-8")
            elif item.filename == "docProps/custom.xml":
                data = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
                        b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
            zout.writestr(item, data)
    tmp.replace(path)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_manuscript_document(workbook: str | Path, out_dir: str | Path, *, prefix: str = "Study", figures: Sequence[str | Path] | None = None,
                               window: Sequence[str] | None = None, title: str | None = None, author: str = "",
                               formats: Sequence[str] = ("docx", "pdf"), min_cell: int | None = 11, max_rows: int = 500,
                               timeout: int = 600) -> dict[str, Any]:
    """Writes ``<prefix>_Manuscript.qmd`` and renders ``.docx`` / ``.pdf`` (``formats``) under ``out_dir``.

    ``figures``: image files to include (default: ``<prefix>_*.png`` next to the workbook). ``title`` defaults to ``"<prefix> manuscript"``;
    ``author`` (default empty) is the only name the document carries. Returns ``{"qmd": Path, "files": {fmt: Path}, "renderer": str | None,
    "missing": [fmt, ...], "note": str}``: a format that could not be rendered is listed in ``missing`` with the reason in ``note``."""
    from omop_etl.manuscript_report import _check_aggregate, _sentences, _suppress_small, manuscript_facts, read_workbook_tables

    bad = set(formats) - {"docx", "pdf"}
    if bad:
        raise ValueError(f"Unknown formats {sorted(bad)}; use 'docx' and/or 'pdf'.")
    wb, out = Path(workbook), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    for k, (_, t) in tabs.items():
        _check_aggregate(k, t, max_rows)
    if min_cell:
        tabs = {k: (ttl, _suppress_small(t, min_cell)) for k, (ttl, t) in tabs.items()}
    facts = manuscript_facts(wb, window=window)
    imgs = [Path(f) for f in figures] if figures is not None else sorted(wb.parent.glob(f"{prefix}_*.png"))
    title = title or f"{prefix} manuscript"
    yaml = ["---", f'title: "{title.replace(chr(34), "")}"']
    if author:
        yaml.append(f'author: "{author.replace(chr(34), "")}"')
    yaml += ["format:", "  docx: default", "  typst:", "    papersize: us-letter", "    margin:", "      x: 1.5cm", "      y: 2cm", "    fontsize: 8pt",
             "lang: en", "---", ""]
    md = list(yaml)
    sent = _sentences(facts)
    if sent:
        md += ["## Results", "", *[_esc(s) for s in sent], ""]
    md += ["## Tables", ""]
    for name, (ttl, t) in tabs.items():
        md += [f"### {_esc(ttl)}", "", _table_md(t), ""]
    copied = []
    if imgs:
        md += ["## Figures", ""]
        for p in imgs:
            if p.parent.resolve() != out.resolve():
                shutil.copy2(p, out / p.name)
                copied.append(out / p.name)
            cap = _esc(_caption(p, prefix))
            md += [f'![{cap}]({p.name}){{fig-alt="{cap.replace(chr(34), "")}" width="100%"}}', ""]
    qmd = out / f"{prefix}_Manuscript.qmd"
    qmd.write_text("\n".join(md), encoding="utf-8")
    tools = find_renderer()
    files: dict[str, Path] = {}
    notes: list[str] = []
    renderer = None
    for fmt in formats:
        target = out / f"{prefix}_Manuscript.{fmt}"
        target.unlink(missing_ok=True)
        try:
            if tools["quarto"]:
                renderer = "quarto"
                subprocess.run([tools["quarto"], "render", qmd.name, "--to", "typst" if fmt == "pdf" else "docx", "--output", target.name],
                               cwd=out, check=True, capture_output=True, timeout=timeout)
            elif tools["pandoc"] and (fmt == "docx" or tools["typst"]):
                renderer = "pandoc"
                cmd = [tools["pandoc"], qmd.name, "-f", "markdown", "-o", target.name, "--resource-path", "."]
                if fmt == "pdf":
                    cmd += ["--pdf-engine", tools["typst"]]
                subprocess.run(cmd, cwd=out, check=True, capture_output=True, timeout=timeout)
            else:
                notes.append(f"{fmt}: no renderer found (install Quarto, https://quarto.org); the .qmd was written")
                continue
        except subprocess.CalledProcessError as e:  # report, do not fail the study: the workbook is already written
            err = (e.stderr or b"").decode("utf-8", errors="ignore").strip().splitlines()[-3:]
            notes.append(f"{fmt}: {renderer} failed: {' / '.join(err)}")
            continue
        except subprocess.TimeoutExpired:
            notes.append(f"{fmt}: {renderer} timed out after {timeout} s")
            continue
        if target.exists():
            if fmt == "docx":
                _scrub_docx(target, author)
            files[fmt] = target
        else:
            notes.append(f"{fmt}: {renderer} ran but wrote no file")
    for side in (out / f"{prefix}_Manuscript_files", out / f"{prefix}_Manuscript.typ"):  # Quarto's intermediates
        if side.is_dir():
            shutil.rmtree(side, ignore_errors=True)
        elif side.exists():
            side.unlink()
    return {"qmd": qmd, "files": files, "figures": imgs, "copied": copied, "renderer": renderer, "missing": [f for f in formats if f not in files],
            "note": "; ".join(notes)}
