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

Sandboxed runs: Quarto keeps a cache under ``%LOCALAPPDATA%\\quarto`` (Windows) or ``$XDG_CACHE_HOME`` / ``$XDG_DATA_HOME`` (Linux). Where
that folder is not writable Quarto stops with ``PermissionDenied``. ``cache_dir`` (or the ``OMOP_QUARTO_CACHE_DIR`` environment variable)
points Quarto, Deno and Typst at a writable folder instead; without one, a render that fails on a permission error is retried once with a
temporary cache inside ``out_dir`` (removed afterwards). A failed render's note carries the start of Quarto's error output (where the
actual error is) and its last lines, with the home folder and account name replaced.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = ["render_manuscript_document", "find_renderer", "renderer_env", "stderr_summary"]

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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PERMISSION = re.compile(r"PermissionDenied|permission denied|access is denied|os error (5|13)\b|EACCES|EPERM", re.I)


def renderer_env(cache_dir: str | Path | None = None, base: dict[str, str] | None = None) -> dict[str, str] | None:
    """Environment for Quarto / Pandoc with their cache and data folders under ``cache_dir`` (created if missing).

    Sets ``LOCALAPPDATA`` and ``APPDATA`` (Windows), ``XDG_CACHE_HOME`` and ``XDG_DATA_HOME`` (Linux) and ``DENO_DIR`` to subfolders of
    ``cache_dir``. ``cache_dir=None`` uses ``OMOP_QUARTO_CACHE_DIR`` when set, otherwise returns ``None`` (inherit the environment).
    macOS keeps Quarto's cache under the home folder, which these variables do not move."""
    cache_dir = cache_dir or os.environ.get("OMOP_QUARTO_CACHE_DIR")
    if not cache_dir:
        return None
    root = Path(cache_dir).expanduser().resolve()
    env = dict(os.environ if base is None else base)
    for var, sub in (("LOCALAPPDATA", "local"), ("APPDATA", "roaming"), ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"),
                     ("DENO_DIR", "deno")):
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[var] = str(root / sub)
    return env


def _redact(text: str, extra: Sequence[str | Path] = ()) -> str:
    """Replaces the home folder, the account name and ``extra`` paths with placeholders (a note is written to the shareable run record)."""
    paths = [(str(p), "<dir>") for p in extra if p and len(str(p)) > 3]
    paths += [(os.environ[v], f"%{v}%") for v in ("LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME", "XDG_DATA_HOME") if len(os.environ.get(v, "")) > 3]
    home = os.path.expanduser("~")
    if home and home not in ("~", "/"):
        paths.append((home, "~"))
    for old, new in sorted(paths, key=lambda x: -len(x[0])):  # longest first; either slash matches, as tools print both on Windows
        pat = r"[\\/]+".join(re.escape(part) for part in re.split(r"[\\/]+", old))
        text = re.sub(pat, lambda _: new, text, flags=re.I)
    for var in ("USERNAME", "USER", "LOGNAME"):
        u = os.environ.get(var)
        if u and len(u) > 2:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(u)}(?![A-Za-z0-9])", "<user>", text, flags=re.I)
    return text


def stderr_summary(stderr: bytes | str | None, *, head: int = 6, tail: int = 2, max_chars: int = 1200,
                   redact: Sequence[str | Path] = ()) -> str:
    """The first ``head`` and last ``tail`` non-empty lines of a tool's error output, joined with ``" / "``.

    Quarto prints the error first and a Deno stack trace after it, so the tail alone shows only the stack (``core/cri/cri.ts``) and hides
    the cause. Colour codes are removed, and the home folder, the account name and ``redact`` paths are replaced (:func:`_redact`)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in _ANSI.sub("", stderr or "").splitlines() if ln.strip()]
    if not lines:
        return "no error output"
    keep = lines if len(lines) <= head + tail else [*lines[:head], f"... ({len(lines) - head - tail} more line(s)) ...", *lines[-tail:]]
    out = _redact(" / ".join(keep), redact)
    return out if len(out) <= max_chars else out[: max_chars - 3] + "..."


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
                               timeout: int = 600, cache_dir: str | Path | None = None, cite_software: bool = True,
                               software_versions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Writes ``<prefix>_Manuscript.qmd`` and renders ``.docx`` / ``.pdf`` (``formats``) under ``out_dir``.

    ``figures``: image files to include (default: ``<prefix>_*.png`` next to the workbook). ``title`` defaults to ``"<prefix> manuscript"``;
    ``author`` (default empty) is the only name the document carries. Returns ``{"qmd": Path, "files": {fmt: Path}, "renderer": str | None,
    "missing": [fmt, ...], "note": str}``: a format that could not be rendered is listed in ``missing`` with the reason in ``note`` (the start
    and end of the renderer's error output, :func:`stderr_summary`). ``cache_dir`` (default ``OMOP_QUARTO_CACHE_DIR``): a writable folder
    for Quarto's cache, for sandboxes where the user's cache folder is read only (:func:`renderer_env`); without it a render that fails on
    a permission error is retried once with a temporary cache under ``out_dir``. ``"cache"`` in the result says which was used.

    ``cite_software`` (default True): add a *Software* section that cites omop-duck-db with its installed version, Python and the study's
    libraries through grateful_py (:func:`omop_etl.package_citations.software_citations`), write ``<prefix>_Manuscript.bib`` and end the
    document with a *References* list. ``software_versions`` (e.g. the ``environment`` block of ``<prefix>_run.json``) overrides the
    installed versions. The result's ``"bib"`` is the ``.bib`` path (None without citations) and ``"software"`` the cited omop-duck-db
    version; without grateful_py the section is left out and ``note`` says so."""
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
    software: dict[str, Any] | None = None
    if cite_software:
        from omop_etl.package_citations import software_citations

        try:
            software = software_citations(out / f"{prefix}_Manuscript.bib", versions=software_versions)
        except ImportError:
            software = None
        if software:
            yaml.insert(-2, f"bibliography: {software['bib'].name}")
    md = list(yaml)
    if software:
        md += ["## Software", "", software["paragraph"], ""]
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
    if software:
        md += ["## References", "", "::: {#refs}", ":::", ""]
    qmd = out / f"{prefix}_Manuscript.qmd"
    qmd.write_text("\n".join(md), encoding="utf-8")
    tools = find_renderer()
    files: dict[str, Path] = {}
    notes: list[str] = ["software citations left out: grateful_py is not installed (pip install grateful_py)"] if cite_software and not software else []
    renderer = None
    env = renderer_env(cache_dir)
    cache_used = "configured" if env is not None else "default"
    tmp_cache = out / ".quarto_cache"
    hide = [out.resolve(), *([Path(cache_dir).expanduser().resolve()] if cache_dir else []),
            *([Path(os.environ["OMOP_QUARTO_CACHE_DIR"]).expanduser().resolve()] if os.environ.get("OMOP_QUARTO_CACHE_DIR") else [])]

    def _run(cmd: list[str]) -> None:
        nonlocal env, cache_used
        try:
            subprocess.run(cmd, cwd=out, check=True, capture_output=True, timeout=timeout, env=env)
        except subprocess.CalledProcessError as e:
            if cache_used != "default" or not _PERMISSION.search((e.stderr or b"").decode("utf-8", errors="ignore")):
                raise
            env, cache_used = renderer_env(tmp_cache), "temporary (default cache not writable)"  # sandbox: retry once with a writable cache
            subprocess.run(cmd, cwd=out, check=True, capture_output=True, timeout=timeout, env=env)

    for fmt in formats:
        target = out / f"{prefix}_Manuscript.{fmt}"
        target.unlink(missing_ok=True)
        try:
            if tools["quarto"]:
                renderer = "quarto"
                _run([tools["quarto"], "render", qmd.name, "--to", "typst" if fmt == "pdf" else "docx", "--output", target.name])
            elif tools["pandoc"] and (fmt == "docx" or tools["typst"]):
                renderer = "pandoc"
                cmd = [tools["pandoc"], qmd.name, "-f", "markdown", "-o", target.name, "--resource-path", ".", *(["--citeproc"] if software else [])]
                if fmt == "pdf":
                    cmd += ["--pdf-engine", tools["typst"]]
                _run(cmd)
            else:
                notes.append(f"{fmt}: no renderer found (install Quarto, https://quarto.org); the .qmd was written")
                continue
        except subprocess.CalledProcessError as e:  # report, do not fail the study: the workbook is already written
            hint = (" (the cache folder is not writable: pass a writable folder as cache_dir / --quarto-cache-dir or set OMOP_QUARTO_CACHE_DIR)"
                    if _PERMISSION.search((e.stderr or b"").decode("utf-8", errors="ignore")) else "")
            notes.append(f"{fmt}: {renderer} failed (exit {e.returncode}): {stderr_summary(e.stderr, redact=hide)}{hint}")
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
    shutil.rmtree(tmp_cache, ignore_errors=True)
    for side in (out / f"{prefix}_Manuscript_files", out / f"{prefix}_Manuscript.typ"):  # Quarto's intermediates
        if side.is_dir():
            shutil.rmtree(side, ignore_errors=True)
        elif side.exists():
            side.unlink()
    return {"qmd": qmd, "bib": software["bib"] if software else None, "software": software["omop_duck_db_version"] if software else None,
            "files": files, "figures": imgs, "copied": copied, "renderer": renderer, "missing": [f for f in formats if f not in files],
            "note": "; ".join(notes), "cache": cache_used}
