#!/usr/bin/env python3
"""Dependency-free pre-release secret scan (FR-C4).

Used by the pre-commit hook and CI next to gitleaks (which also scans history); it needs only the standard library, so it
works offline. Exit status 1 when a likely credential is found. A line containing ``secret-scan:allow`` is ignored.

    python scripts/secret_scan.py [paths...]        # default: all git-tracked files
    python scripts/secret_scan.py --staged          # only staged files (pre-commit)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "PyPI / TestPyPI API token": re.compile(r"pypi-Age[A-Za-z0-9_-]{40,}", re.I),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    "Database URL with password": re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:(?!\*\*\*|\$|%|<|\{)[^\s@'\"]{4,}@[^\s'\"]+"),
    "Hard-coded credential": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"][A-Za-z0-9/+_=.-]{20,}['\"]"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".parquet", ".duckdb", ".pdf", ".zip", ".gz", ".ico", ".rds"}
ALLOW_MARK = "secret-scan:allow"
SKIP_DIRS = {".git", "renv", "node_modules", ".venv", "venv", "__pycache__", ".Rproj.user"}


def _files(args: list[str]) -> list[Path]:
    if "--staged" in args:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"], capture_output=True,
                             text=True, check=True).stdout.split("\n")
    elif [a for a in args if not a.startswith("--")]:
        return [Path(a) for a in args if not a.startswith("--")]
    else:
        try:
            out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
        except (OSError, subprocess.CalledProcessError):
            return _walk_tree()  # no git or no .git (e.g. a Docker build context): scan the files on disk
    return [Path(p) for p in out if p]


def _walk_tree() -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    return [p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)]


def scan_text(text: str) -> list[tuple[int, str]]:
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        for name, pat in PATTERNS.items():
            if pat.search(line):
                hits.append((i, name))
    return hits


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    found = 0
    for path in _files(args):
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, name in scan_text(text):
            print(f"{path}:{lineno}: possible {name} (value not shown)")  # never echo the match
            found += 1
    if found:
        print(f"\n{found} potential secret(s) found. Revoke any real credential, remove it, and see NOTES.md "
              "('Secret scanning and token revocation').", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
