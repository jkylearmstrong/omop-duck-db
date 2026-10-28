"""Writes a small DuckDB OMOP CDM (the same five persons / seven visits as the cross-dialect cohort tests) for ATLAS.

    python scripts/atlas/make_cdm.py scripts/atlas/data/cdm.duckdb
"""
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from omop_etl.build_omop_cdm import build_schema  # noqa: E402


def main(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).unlink(missing_ok=True)
    con = duckdb.connect(path)
    build_schema(con)
    con.execute("INSERT INTO cdm_source (cdm_source_name, cdm_source_abbreviation, cdm_holder, source_description, cdm_etl_reference, source_release_date, "
                "cdm_release_date, cdm_version, cdm_version_concept_id, vocabulary_version) VALUES ('omop-duck-db fixture', 'odd', 'x', 'x', 'x', "
                "DATE '2020-01-01', DATE '2020-01-01', 'v5.4', 756265, 'v5.0')")
    for cid, name, dom, vocab, cls in ((9201, "Inpatient Visit", "Visit", "Visit", "Visit"), (262, "Emergency Room and Inpatient Visit", "Visit", "Visit", "Visit"),
                                       (9203, "Emergency Room Visit", "Visit", "Visit", "Visit"), (8507, "MALE", "Gender", "Gender", "Gender"),
                                       (0, "No matching concept", "Metadata", "None", "Undefined")):
        con.execute("INSERT INTO concept VALUES (?, ?, ?, ?, ?, 'S', ?, DATE '1970-01-01', DATE '2099-12-31', NULL)",
                    [cid, name, dom, vocab, cls, str(cid)])
    for pid, yob in {1: 1980, 2: 1980, 3: 2015, 4: 1980, 5: 1980}.items():
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                    f"VALUES ({pid}, 8507, {yob}, 0, 0)")
        end = "2020-02-01" if pid == 4 else "2020-12-31"
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, "
                    f"period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '{end}', 0)")
    for vid, pid, cid, s, e, dc in ((1, 1, 9201, "2020-03-01", "2020-03-05", None), (2, 1, 9201, "2020-02-01", "2020-02-04", None),
                                    (3, 2, 9201, "2020-03-01", "2020-03-05", 4216643), (4, 3, 9201, "2020-03-01", "2020-03-05", None),
                                    (5, 4, 9201, "2020-01-10", "2020-01-20", None), (6, 5, 262, "2020-03-01", "2020-03-05", None),
                                    (7, 1, 9203, "2020-01-01", "2020-01-01", None)):
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                    f"visit_type_concept_id, discharged_to_concept_id) VALUES ({vid}, {pid}, {cid}, DATE '{s}', DATE '{e}', 0, {dc if dc else 'NULL'})")
    con.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scripts/atlas/data/cdm.duckdb")
