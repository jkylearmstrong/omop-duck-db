"""Scans a study output folder for real site names before it is shared; exit code 1 when any is found.

A run's workbook, tables and report use anonymous letters, but the folder can still name a site: per-site intermediates
(``features_<site>.parquet``, ``attrition_<site>.csv``), a ``site_source`` column, a log, a copied command line. Put the real names in a
text file (one per line, kept outside the repository) so they do not land in shell history:

    python scripts/check_run_privacy.py --dir out/run --names-file ../private/site_names.txt

What is scanned: file and folder names; the text of csv / json / md / txt / html / log / yaml / sql / py files; every part of an xlsx or other
zip-based file (names are matched in the XML); column names and string values of parquet files. Images and other binary files are listed as not
scanned. Matching is case-insensitive on whole words, and a hit is reported by the position of the name in the list, never by the name, so the
report itself can be pasted into an issue. Exact per-site counts can identify a site whatever its letter; this check does not look for them.
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path

TEXT_SUFFIXES = {".csv", ".tsv", ".json", ".md", ".txt", ".html", ".htm", ".log", ".yaml", ".yml", ".sql", ".py", ".r", ".qmd", ".rmd", ".xml"}
ZIP_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".pptx", ".zip"}


def _pattern(names):
    return [re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", re.IGNORECASE) for n in names]


def _hits(text, pats):
    return [i + 1 for i, p in enumerate(pats) if p.search(text)]


def _scan_parquet(path, pats):
    import pandas as pd

    out = []
    df = pd.read_parquet(path)
    where = " ".join(map(str, df.columns))
    for i in _hits(where, pats):
        out.append((i, "column name"))
    for c in df.columns:
        if df[c].dtype == object or str(df[c].dtype) in ("string", "category"):
            vals = " ".join(map(str, df[c].dropna().unique()[:100000]))
            for i in _hits(vals, pats):
                out.append((i, f"values of column '{c}'"))
    return out


def scan(folder, names):
    """Returns (hits, skipped): hits is a list of (relative path, name number, where); skipped lists files that were not scanned."""
    folder = Path(folder)
    pats = _pattern([n for n in names if n.strip()])
    hits, skipped = [], []
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        for i in _hits(rel, pats):
            hits.append((rel, i, "file or folder name"))
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        try:
            if suf in TEXT_SUFFIXES:
                for i in _hits(p.read_text(encoding="utf-8", errors="ignore"), pats):
                    hits.append((rel, i, "content"))
            elif suf in ZIP_SUFFIXES:
                with zipfile.ZipFile(p) as z:
                    for part in z.namelist():
                        for i in _hits(z.read(part).decode("utf-8", errors="ignore"), pats):
                            hits.append((rel, i, f"part {part}"))
            elif suf == ".parquet":
                hits.extend((rel, i, where) for i, where in _scan_parquet(p, pats))
            else:
                skipped.append(rel)
        except Exception as e:  # unreadable file: say so rather than call it clean
            skipped.append(f"{rel} ({type(e).__name__})")
    return hits, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the output folder to scan")
    ap.add_argument("--names-file", help="text file with one real site name per line")
    ap.add_argument("--names", nargs="*", default=[], help="real site names (they will be in shell history; prefer --names-file)")
    a = ap.parse_args(argv)
    names = list(a.names)
    if a.names_file:
        names += [ln.strip() for ln in Path(a.names_file).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    if not names:
        ap.error("give at least one site name (--names-file or --names)")
    hits, skipped = scan(a.dir, names)
    for rel, i, where in hits:
        print(f"HIT  name #{i}  {rel}  ({where})")
    for rel in skipped:
        print(f"not scanned: {rel}")
    print(f"{len(hits)} hit(s) for {len(names)} name(s); {len(skipped)} file(s) not scanned")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
