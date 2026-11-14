"""Scans study outputs for real site names before they are shared (the core of ``scripts/check_run_privacy.py``).

A run's workbook, tables and manuscript use anonymous letters, but a folder can still name a site: per-site intermediates
(``features_<site>.parquet``), a ``site_source`` column, a log, a copied command line, or text that reached a Word or PDF file.

What is scanned: file and folder names; the text of csv / json / md / txt / html / log / yaml / sql / py / qmd / typ files; every part of an
xlsx, docx or other zip-based file (names are matched in the XML, and for a docx also in the paragraph text with the XML tags removed, so a
name Word split across runs is still found; embedded images and fonts are not read as text, only a PNG's text chunks); the page text, metadata and embedded file names of a PDF (needs ``pypdf``); column names and
string values of parquet files. Images and other binary files are listed as not scanned. Matching is case-insensitive on whole words, and a
hit is reported by the position of the name in the list, never by the name, so the report itself can be pasted into an issue. Exact
per-site counts can identify a site whatever its letter; this check does not look for them.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

__all__ = ["TEXT_SUFFIXES", "ZIP_SUFFIXES", "read_names_file", "scan", "scan_files", "docx_text", "pdf_text", "png_text"]

TEXT_SUFFIXES = {".csv", ".tsv", ".json", ".md", ".txt", ".html", ".htm", ".log", ".yaml", ".yml", ".sql", ".py", ".r", ".qmd", ".rmd", ".xml", ".typ"}
ZIP_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".pptx", ".zip"}


def read_names_file(path: str | Path) -> list[str]:
    """One real site name per line; blank lines and lines starting with ``#`` are skipped."""
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _pattern(names: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(r"(?<![A-Za-z0-9])" + re.escape(n.strip()) + r"(?![A-Za-z0-9])", re.IGNORECASE) for n in names if n.strip()]


def _hits(text: str, pats: Sequence[re.Pattern]) -> list[int]:
    return [i + 1 for i, p in enumerate(pats) if p.search(text)]


def docx_text(path: str | Path) -> str:
    """The text of a Word file (body, headers, footers, notes, comments, properties) with the XML tags removed, one paragraph per line."""
    parts = []
    with zipfile.ZipFile(path) as z:
        for part in z.namelist():
            if not part.endswith(".xml"):
                continue
            xml = z.read(part).decode("utf-8", errors="ignore")
            xml = re.sub(r"</w:p>|</a:p>|<w:br/>|<w:tab/>", "\n", xml)
            parts.append(re.sub(r"<[^>]+>", "", xml))
    return "\n".join(parts)


def pdf_text(path: str | Path) -> str:
    """Page text, document metadata, outline titles and embedded / attached file names of a PDF. Raises ImportError without ``pypdf``."""
    from pypdf import PdfReader

    r = PdfReader(str(path))
    out = [p.extract_text() or "" for p in r.pages]
    meta = r.metadata or {}
    out += [f"{k} {v}" for k, v in meta.items()]
    try:
        out += [str(k) for k in (r.attachments or {})]
    except Exception:  # an odd name tree should not stop the page-text scan
        pass
    try:
        out += [str(getattr(o, "title", "")) for o in r.outline if not isinstance(o, list)]
    except Exception:
        pass
    if r.xmp_metadata is not None:
        try:
            out.append(r.xmp_metadata.stream.get_data().decode("utf-8", errors="ignore"))
        except Exception:
            pass
    for p in r.pages:  # image alt text and annotation contents
        for a in p.get("/Annots") or []:
            try:
                out.append(str(a.get_object().get("/Contents", "")))
            except Exception:
                pass
    return "\n".join(out)


_BINARY_PARTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf", ".wmf", ".ico", ".webp", ".bin", ".odttf", ".ttf", ".otf", ".wdp")


def png_text(data: bytes) -> str:
    """The text chunks of a PNG (``tEXt``, ``zTXt``, ``iTXt``: title, author, software, description), not its pixels."""
    import struct
    import zlib

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    out, i = [], 8
    while i + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        i += 12 + n
        try:
            if kind == b"tEXt":
                out.append(body.replace(b"\x00", b" ").decode("latin-1"))
            elif kind == b"zTXt":
                key, _, rest = body.partition(b"\x00")
                out.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1"))
            elif kind == b"iTXt":
                key, _, rest = body.partition(b"\x00")
                flag, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _tkey, _, text = rest.partition(b"\x00")
                out.append(key.decode("latin-1") + " " + (zlib.decompress(text) if flag else text).decode("utf-8", errors="ignore"))
            elif kind == b"IEND":
                break
        except Exception:  # a damaged chunk: keep what was read
            continue
    return "\n".join(out)


def _part_text(name: str, data: bytes) -> str:
    """Text of one part of a zip-based file. Images, fonts and other binary parts are not read as text: compressed bytes match a
    short name by chance (a false positive that withheld a real manuscript). For a PNG its text chunks are scanned instead."""
    low = name.lower()
    if low.endswith(_BINARY_PARTS) or b"\x00" in data[:4096]:
        return png_text(data)
    return data.decode("utf-8", errors="ignore")


def _scan_parquet(path: Path, pats: Sequence[re.Pattern]) -> list[tuple[int, str]]:
    import pandas as pd

    out = []
    df = pd.read_parquet(path)
    for i in _hits(" ".join(map(str, df.columns)), pats):
        out.append((i, "column name"))
    for c in df.columns:
        if df[c].dtype == object or str(df[c].dtype) in ("string", "str", "category"):
            vals = " ".join(map(str, df[c].dropna().unique()[:100000]))
            for i in _hits(vals, pats):
                out.append((i, f"values of column '{c}'"))
    return out


def _scan_one(p: Path, rel: str, pats: Sequence[re.Pattern]) -> tuple[list[tuple[str, int, str]], str | None]:
    """Hits in one file's content, and the reason it was not scanned (None when it was)."""
    hits: list[tuple[str, int, str]] = []
    suf = p.suffix.lower()
    try:
        if suf in TEXT_SUFFIXES:
            hits += [(rel, i, "content") for i in _hits(p.read_text(encoding="utf-8", errors="ignore"), pats)]
        elif suf in ZIP_SUFFIXES:
            with zipfile.ZipFile(p) as z:
                for part in z.namelist():
                    hits += [(rel, i, f"part {part}") for i in _hits(part + "\n" + _part_text(part, z.read(part)), pats)]
            if suf == ".docx":
                hits += [(rel, i, "document text") for i in _hits(docx_text(p), pats)]
        elif suf == ".pdf":
            hits += [(rel, i, "PDF text or metadata") for i in _hits(pdf_text(p), pats)]
        elif suf == ".png":  # the text chunks are scanned; the pixels cannot be, so the file is still listed as not scanned
            hits += [(rel, i, "PNG text chunk") for i in _hits(png_text(p.read_bytes()), pats)]
            return hits, rel
        elif suf == ".parquet":
            hits += [(rel, i, where) for i, where in _scan_parquet(p, pats)]
        else:
            return hits, rel
    except ImportError as e:
        return hits, f"{rel} (needs {e.name or 'a missing package'})"
    except Exception as e:  # unreadable file: say so rather than call it clean
        return hits, f"{rel} ({type(e).__name__})"
    return hits, None


def scan_files(files: Iterable[str | Path], names: Sequence[str], root: str | Path | None = None) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Like :func:`scan` for a list of files: ``(hits, skipped)``, paths shown relative to ``root`` (default: file name only)."""
    pats = _pattern(names)
    hits, skipped = [], []
    for f in files:
        p = Path(f)
        rel = p.relative_to(root).as_posix() if root is not None else p.name
        hits += [(rel, i, "file or folder name") for i in _hits(rel, pats)]
        if not p.is_file():
            continue
        h, s = _scan_one(p, rel, pats)
        hits += h
        if s is not None:
            skipped.append(s)
    return hits, skipped


def scan(folder: str | Path, names: Sequence[str]) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Returns ``(hits, skipped)``: hits is a list of (relative path, name number, where); skipped lists files that were not scanned."""
    folder = Path(folder)
    return scan_files(sorted(folder.rglob("*")), names, root=folder)
