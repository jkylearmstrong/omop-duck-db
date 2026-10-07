"""Cross-language integration test: R and Python PCORnet ETLs must produce
identical output against the shared fixture.

Usage:
    pytest tests/test_etl_parity.py

Builds a fresh schema-only DuckDB for each implementation, runs both against
tests/fixtures/pcornet_sample/, and asserts matching row counts and matching
person_id/concept_id values across every mapped table. Does not require the
Athena vocabulary to be loaded (concept mapping will resolve to 0/unmapped in
that case) -- this test checks ETL mechanics and R/Python parity, not mapping
accuracy against real vocabulary content.

Requires the omopduckdb R package to be installed (`R CMD INSTALL .` or
`Rscript -e "devtools::install('.')"`), since it runs the CLI wrapper under
inst/scripts/, which does `library(omopduckdb)`.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest

# On Windows, Rscript resolves to Rscript.bat, which subprocess can only launch
# through the shell; list2cmdline still handles quoting correctly for a list
# argument in that case. Not used on POSIX where shell=True has different (and
# unsafe) semantics for a list argument.
RSCRIPT_SHELL = os.name == "nt"

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"
DDL_PATH = REPO_ROOT / "inst" / "extdata" / "5.4" / "duckdb" / "OMOPCDM_duckdb_5.4_ddl.sql"

TABLES = [
    "person",
    "provider",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "drug_exposure",
    "observation_period",
    "death",
    "cdm_source",
    "condition_era",
    "drug_era",
    "location",
]


def _build_schema(db_path):
    ddl = DDL_PATH.read_text()
    ddl = ddl.replace("@cdmDatabaseSchema.", "").replace(" NUMERIC ", " DOUBLE ")
    con = duckdb.connect(str(db_path))
    con.execute(ddl)
    con.close()


def _run_r_etl(db_path):
    subprocess.run(
        [
            "Rscript",
            "--vanilla",
            str(REPO_ROOT / "inst" / "scripts" / "etl_pcornet.R"),
            "--source-dir",
            str(FIXTURE_DIR),
            "--db-path",
            str(db_path),
        ],
        check=True,
        cwd=REPO_ROOT,
        shell=RSCRIPT_SHELL,
    )


def _run_python_etl(db_path):
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "python" / "omop_etl" / "build_omop_cdm.py"),
            "--source-dir",
            str(FIXTURE_DIR),
            "--db-path",
            str(db_path),
        ],
        check=True,
        cwd=REPO_ROOT,
    )


def _snapshot(db_path):
    con = duckdb.connect(str(db_path), read_only=True)
    result = {}
    for table in TABLES:
        result[table] = con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    con.close()
    return result


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="R/Rscript not available on this machine")
def test_r_and_python_etl_produce_identical_output():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        r_db = tmp / "r_test.duckdb"
        py_db = tmp / "py_test.duckdb"

        _build_schema(r_db)
        _build_schema(py_db)

        _run_r_etl(r_db)
        _run_python_etl(py_db)

        r_snapshot = _snapshot(r_db)
        py_snapshot = _snapshot(py_db)

    for table in TABLES:
        r_rows, py_rows = r_snapshot[table], py_snapshot[table]
        assert len(r_rows) == len(py_rows), f"{table}: row count mismatch (R={len(r_rows)}, Python={len(py_rows)})"
        assert r_rows == py_rows, f"{table}: row content mismatch between R and Python output"
