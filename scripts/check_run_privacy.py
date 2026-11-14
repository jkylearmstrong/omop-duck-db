"""Scans a study output folder for real site names before it is shared; exit code 1 when any is found.

A run's workbook, tables and report use anonymous letters, but the folder can still name a site: per-site intermediates
(``features_<site>.parquet``, ``attrition_<site>.csv``), a ``site_source`` column, a log, a copied command line. Put the real names in a
text file (one per line, kept outside the repository) so they do not land in shell history:

    python scripts/check_run_privacy.py --dir out/run --names-file ../private/site_names.txt

What is scanned: file and folder names; the text of csv / json / md / txt / html / log / yaml / sql / py / qmd files; every part of an xlsx,
docx or other zip-based file (names are matched in the XML and, for a docx, in the paragraph text); the page text and metadata of a PDF
(needs pypdf); column names and string values of parquet files. Images and other binary files are listed as not
scanned. Matching is case-insensitive on whole words, and a hit is reported by the position of the name in the list, never by the name, so the
report itself can be pasted into an issue. Exact per-site counts can identify a site whatever its letter; this check does not look for them.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.run_privacy import TEXT_SUFFIXES, ZIP_SUFFIXES, read_names_file, scan, scan_files  # noqa: E402,F401


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the output folder to scan")
    ap.add_argument("--names-file", help="text file with one real site name per line")
    ap.add_argument("--names", nargs="*", default=[], help="real site names (they will be in shell history; prefer --names-file)")
    a = ap.parse_args(argv)
    names = list(a.names)
    if a.names_file:
        names += read_names_file(a.names_file)
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
