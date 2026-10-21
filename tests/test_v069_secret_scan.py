"""FR-C4: the offline secret scanner catches a planted dummy token and stays quiet on clean files."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "secret_scan.py"


def _run(*paths):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, paths)], capture_output=True, text=True)


def test_planted_token_fails_without_echoing_it(tmp_path):
    dummy = "pypi-" + "Age" + "A" * 60  # built at runtime so this file itself is clean
    f = tmp_path / "planted.txt"
    f.write_text(f'token = "{dummy}"\n')
    r = _run(f)
    assert r.returncode == 1 and "PyPI" in r.stdout and dummy not in r.stdout + r.stderr


def test_other_patterns_and_allow_marker(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("url = 'postgresql://user:" + "hunter22@db.example.org/omop'\n")
    assert _run(f).returncode == 1
    f.write_text("url = 'postgresql://user:" + "hunter22@db.example.org/omop'  # secret-scan:allow\n")
    assert _run(f).returncode == 0
    f.write_text("url = 'env:OMOP_SOURCE_URL'\nurl2 = 'sqlite:///x.db'\n")
    assert _run(f).returncode == 0


def test_repository_is_clean():
    assert _run().returncode == 0
