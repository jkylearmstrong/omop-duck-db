"""Shared pytest fixtures for the ML-feature / omop-learn bridge tests.

Optional-dependency hook: set ``OMOP_LEARN_SRC`` to a checkout's ``src`` directory (e.g. a plain
``git clone https://github.com/clinicalml/omop-learn``) to run the omop-learn integration tests without
installing it. omop-learn's subpackages have no ``__init__.py``, so a source path is the most reliable way to
import them.
"""

import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))
if os.environ.get("OMOP_LEARN_SRC"):
    sys.path.append(os.environ["OMOP_LEARN_SRC"])

from omop_etl import build_schema  # noqa: E402


def _insert_person(con, ids):
    for pid in ids:
        con.execute(
            "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, "
            f"ethnicity_concept_id) VALUES ({pid}, {8532 if pid == 2 else 8507}, 1970, 8527, 38003564)"
        )


def _cond(con, oid, pid, cid, date, visit):
    v = "NULL" if visit is None else visit
    con.execute(
        "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
        f"condition_start_date, condition_type_concept_id, visit_occurrence_id) VALUES ({oid}, {pid}, {cid}, DATE '{date}', 32020, {v})"
    )


def _drug(con, oid, pid, cid, date, visit):
    con.execute(
        "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
        f"drug_exposure_end_date, drug_type_concept_id, visit_occurrence_id) VALUES ({oid}, {pid}, {cid}, DATE '{date}', DATE '{date}', 32838, {visit})"
    )


def _proc(con, oid, pid, cid, date, visit):
    con.execute(
        "INSERT INTO procedure_occurrence (procedure_occurrence_id, person_id, procedure_concept_id, "
        f"procedure_date, procedure_type_concept_id, visit_occurrence_id) VALUES ({oid}, {pid}, {cid}, DATE '{date}', 32817, {visit})"
    )


def _visit(con, vid, pid, date):
    con.execute(
        "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
        f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, 9202, DATE '{date}', DATE '{date}', 32827)"
    )


@pytest.fixture(scope="module")
def cdm_and_cohort(tmp_path_factory):
    """A tiny CDM plus a cohort parquet, small enough to verify by hand.

    Each record targets a failure mode: an event ON the index date, one AFTER it (leakage), one older than
    365 days, concept 0 (collides with the PAD token), a record with no visit id, a patient with two index
    rows, a patient with no events, and a cohort file deliberately not sorted by person.
    """
    tmp_path = tmp_path_factory.mktemp("mlfeat")
    con = duckdb.connect(":memory:")
    build_schema(con)
    _insert_person(con, [1, 2, 3])

    for cid, name in [(201, "Type 2 diabetes"), (301, "Metformin")]:
        con.execute(
            "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, "
            "standard_concept, concept_code, valid_start_date, valid_end_date) VALUES "
            f"({cid}, '{name}', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'X{cid}', DATE '1970-01-01', DATE '2099-12-31')"
        )

    _visit(con, 11, 1, "2021-05-20")
    _visit(con, 12, 1, "2021-05-25")
    _visit(con, 21, 2, "2021-05-30")
    _visit(con, 13, 1, "2021-06-10")

    # person 1: index rows 2021-06-01 and 2021-09-01
    _cond(con, 1, 1, 201, "2021-05-20", 11)
    _cond(con, 2, 1, 201, "2021-05-25", 12)
    _drug(con, 1, 1, 301, "2021-05-25", 12)
    _cond(con, 3, 1, 202, "2021-06-01", 12)      # ON index date of row 1 -> excluded by default
    _cond(con, 4, 1, 203, "2021-06-10", 13)      # AFTER index of row 1 (leakage) / inside row 3 window
    _cond(con, 5, 1, 204, "2020-01-01", 11)      # outside 365d for both of person 1's index rows
    _cond(con, 6, 1, 0, "2021-05-01", 11)        # concept 0 -> must be dropped (collides with PAD)
    _proc(con, 1, 1, 401, "2021-05-22", 11)
    # person 2
    _cond(con, 7, 2, 201, "2021-05-30", 21)
    _drug(con, 2, 2, 301, "2021-05-30", 21)
    _cond(con, 8, 2, 205, "2021-05-10", None)    # no visit id -> pseudo-visit grouped by date

    # Deliberately not sorted by person: parquet position, not person id, defines row order.
    cohort = pd.DataFrame({
        "subject_id": [2, 1, 3, 1],
        "cohort_start_date": pd.to_datetime(["2021-06-01", "2021-06-01", "2021-06-01", "2021-09-01"]),
        "outcome_flag": [1, 0, 0, 1],
        "extra_feature": [0.1, 0.2, 0.3, 0.4],
    })
    path = tmp_path / "cohort.parquet"
    duckdb.connect().register("cohort", cohort).execute(
        f"COPY (SELECT * FROM cohort) TO '{path.as_posix()}' (FORMAT PARQUET)")
    return con, path
