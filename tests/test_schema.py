"""Schema-build test for the Python ETL path.

Checks that build_omop_cdm.py's build_schema() creates the expected core CDM
tables from the shared DDL (inst/extdata/5.4/duckdb/*.sql), independent of R.
"""

import sys
import tempfile
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.build_omop_cdm import build_schema  # noqa: E402


def test_build_schema_creates_core_tables():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "schema_test.duckdb"
        con = duckdb.connect(str(db_path))
        build_schema(con)
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        con.close()

    expected = {
        "person",
        "visit_occurrence",
        "condition_occurrence",
        "procedure_occurrence",
        "measurement",
        "drug_exposure",
        "provider",
        "concept",
        "concept_relationship",
    }
    assert expected <= tables
