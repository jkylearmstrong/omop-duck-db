#!/usr/bin/env python3
"""
Unified Check, Test, Build, and Release Automation Script for omop-duck-db.

This script coordinates the dual-language lifecycle (R package 'omopduckdb' and Python package 'omop-duck-db'):
  1. Version check & sync (DESCRIPTION <-> pyproject.toml)
  2. README.Rmd rendering (quarto render)
  3. R test suite & coverage (testthat + covr)
  4. Python test suite & coverage (pytest + pytest-cov)
  5. Documentation & pkgdown site build
  6. Distribution builds & twine checks (python -m build + twine check)
  7. Git commit, tagging, and pushing (optional)
  8. PyPI publication (optional)

Usage:
  python scripts/release.py --check-only
  python scripts/release.py --bump 0.2.0
  python scripts/release.py --bump 0.2.0 --tag --push
  python scripts/release.py --bump 0.2.0 --tag --push --publish
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DESCRIPTION_FILE = ROOT_DIR / "DESCRIPTION"
PYPROJECT_FILE = ROOT_DIR / "pyproject.toml"
README_RMD = ROOT_DIR / "README.Rmd"


def log(msg, symbol="INFO"):
    print(f"\n[{symbol}] {msg}")


def run_cmd(cmd, cwd=ROOT_DIR, check=True):
    print(f"  $ {' '.join(cmd)}")
    use_shell = os.name == "nt"
    result = subprocess.run(cmd, cwd=cwd, text=True, shell=use_shell)
    if check and result.returncode != 0:
        print(f"[ERROR] Command failed with exit code {result.returncode}")
        sys.exit(result.returncode)
    return result.returncode


def get_r_version():
    content = DESCRIPTION_FILE.read_text(encoding="utf-8")
    match = re.search(r"^Version:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


def get_py_version():
    content = PYPROJECT_FILE.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(.*?)"', content, re.MULTILINE)
    return match.group(1).strip() if match else None


def set_version(new_version):
    log(f"Bumping version to {new_version} in DESCRIPTION and pyproject.toml...")

    # Update DESCRIPTION
    desc_text = DESCRIPTION_FILE.read_text(encoding="utf-8")
    desc_text = re.sub(r"^Version:\s*.+$", f"Version: {new_version}", desc_text, flags=re.MULTILINE)
    DESCRIPTION_FILE.write_text(desc_text, encoding="utf-8")

    # Update pyproject.toml
    py_text = PYPROJECT_FILE.read_text(encoding="utf-8")
    py_text = re.sub(r'^version\s*=\s*".*?"', f'version = "{new_version}"', py_text, flags=re.MULTILINE)
    PYPROJECT_FILE.write_text(py_text, encoding="utf-8")
    log("Version files updated successfully.", symbol="SUCCESS")


def main():
    parser = argparse.ArgumentParser(description="Automated Check, Build, and Release pipeline for omop-duck-db.")
    parser.add_argument("--bump", help="Bump version to new version (e.g. 0.2.0)")
    parser.add_argument("--check-only", action="store_true", help="Run tests and checks only without building/releasing")
    parser.add_argument("--skip-r", action="store_true", help="Skip R package steps")
    parser.add_argument("--skip-py", action="store_true", help="Skip Python package steps")
    parser.add_argument("--publish", action="store_true", help="Publish Python wheel to PyPI via twine")
    parser.add_argument("--tag", action="store_true", help="Create git commit and version tag vX.Y.Z")
    parser.add_argument("--push", action="store_true", help="Push commit and tags to origin main")
    args = parser.parse_args()

    log("Starting omop-duck-db validation and release pipeline...")

    # Step 1: Version Check / Sync
    r_ver = get_r_version()
    py_ver = get_py_version()
    log(f"Current Versions -> R (DESCRIPTION): {r_ver} | Python (pyproject.toml): {py_ver}")

    if args.bump:
        set_version(args.bump)
        current_version = args.bump
    else:
        if r_ver != py_ver:
            log(f"WARNING: Version mismatch detected! R={r_ver}, Python={py_ver}", symbol="WARN")
        current_version = r_ver

    # Step 2: Render README.Rmd if quarto is available
    if README_RMD.exists() and not args.skip_r:
        log("Step 2: Rendering README.Rmd with Quarto...")
        run_cmd(["quarto", "render", "README.Rmd", "--to", "gfm"], check=False)

    # Step 3: R Documentation & Tests
    if not args.skip_r:
        log("Step 3: Running R documentation (roxygen2) and tests...")
        run_cmd(["Rscript", "-e", "devtools::document(); devtools::test()"])

        log("Step 4: Running R coverage...")
        run_cmd(["Rscript", "-e", "if (requireNamespace('covr', quietly=TRUE)) print(covr::package_coverage()) else message('covr not installed')"], check=False)

        if not args.check_only:
            log("Step 5: Building pkgdown site...")
            run_cmd(["Rscript", "-e", "options(pkgdown.internet=FALSE); if (requireNamespace('pkgdown', quietly=TRUE)) tryCatch(pkgdown::build_site(new_process=FALSE, install=FALSE, preview=FALSE), error=function(e) message('pkgdown build notice: ', e$message)) else message('pkgdown not installed')"], check=False)

    # Step 6: Python Tests & Coverage
    if not args.skip_py:
        log("Step 6: Running Python test suite with pytest & coverage...")
        cov_check = subprocess.run([sys.executable, "-m", "pytest", "--help"], capture_output=True, text=True).stdout
        cov_args = ["--cov=python/omop_etl", "--cov-report=term"] if "--cov" in cov_check else []
        run_cmd([sys.executable, "-m", "pytest", "tests/"] + cov_args)

    # Step 7: Build & Check Python Distribution
    if not args.skip_py and not args.check_only:
        log("Step 7: Building Python wheel/sdist distribution...")
        run_cmd([sys.executable, "-m", "build"])

        log("Step 8: Checking distribution with twine...")
        run_cmd([sys.executable, "-m", "twine", "check", "dist/*"])

    # Step 9: Git Commit & Tagging
    if args.tag:
        tag_name = f"v{current_version}"
        log(f"Step 9: Committing changes and tagging {tag_name}...")
        run_cmd(["git", "add", "."])
        status_check = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
        if status_check:
            run_cmd(["git", "commit", "-m", f"release: bump version to {current_version}"])
        else:
            log("Working tree clean; skipping git commit.")
        run_cmd(["git", "tag", "-a", "-f", tag_name, "-m", f"Release {tag_name}"])

        if args.push:
            log("Step 10: Pushing commits and tags to origin...")
            run_cmd(["git", "push", "origin", "main"])
            run_cmd(["git", "push", "origin", tag_name, "--force"])

    # Step 11: PyPI Upload
    if args.publish and not args.skip_py:
        log("Step 11: Uploading distribution to PyPI...")
        run_cmd([sys.executable, "-m", "twine", "upload", "dist/*"])

    log("Pipeline completed successfully!", symbol="SUCCESS")


if __name__ == "__main__":
    main()
