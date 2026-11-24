"""0.34.2: drug rows on a non-standard or deprecated concept take its single valid 'Maps to' target (C15 close-out). Synthetic data only."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from omop_etl.build_omop_cdm import backfill_drug_concepts  # noqa: E402


def test_non_standard_drug_concepts_follow_maps_to():
    con = duckdb.connect()
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (45000001, '00002771501', 'NDC', '11-digit NDC', NULL, '00002771501', NULL),
        (40000002, 'old insulin product', 'RxNorm', 'Branded Drug', NULL, '1242215', 'U'),
        (45000003, 'combo NDC', 'NDC', '11-digit NDC', NULL, '00000000003', NULL),
        (45000004, 'NDC to a deprecated target', 'NDC', '11-digit NDC', NULL, '00000000004', NULL),
        (19078461, 'metformin 500 MG Oral Tablet', 'RxNorm', 'Clinical Drug', 'S', '861007', NULL),
        (1567198, 'insulin lispro', 'RxNorm', 'Ingredient', 'S', '86009', NULL),
        (1503297, 'metformin', 'RxNorm', 'Ingredient', 'S', '6809', NULL),
        (1567000, 'retired drug', 'RxNorm', 'Clinical Drug', 'S', '1', 'D'))
        t(concept_id, concept_name, vocabulary_id, concept_class_id, standard_concept, concept_code, invalid_reason)""")
    con.execute("""CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
        (45000001, 19078461, 'Maps to', NULL), (40000002, 1567198, 'Maps to', NULL),
        (45000003, 1567198, 'Maps to', NULL), (45000003, 1503297, 'Maps to', NULL),   -- two targets: left as is
        (45000004, 1567000, 'Maps to', NULL),                                          -- target not valid: left as is
        (45000001, 1503297, 'Maps to', 'D'))                                           -- a retired mapping is ignored
        t(concept_id_1, concept_id_2, relationship_id, invalid_reason)""")
    con.execute("""CREATE TABLE drug_exposure AS SELECT * FROM (VALUES
        (1, 45000001, 0, 'x'), (2, 40000002, NULL, 'y'), (3, 45000003, 0, 'z'), (4, 45000004, 0, 'w'), (5, 19078461, 0, 'v'),
        (6, 45000001, 555, 'u'))
        t(drug_exposure_id, drug_concept_id, drug_source_concept_id, drug_source_value)""")
    got = backfill_drug_concepts(con)
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT drug_exposure_id, drug_concept_id, drug_source_concept_id FROM drug_exposure").fetchall()}
    assert rows == {1: (19078461, 45000001), 2: (1567198, 40000002), 3: (45000003, 0), 4: (45000004, 0), 5: (19078461, 0),
                    6: (19078461, 555)}   # an existing source concept is kept
    assert got["non_standard_before"] == 5 and got["by_maps_to"] == 3 and got["unmapped_before"] == 0


def test_n_secondary_dx_counts_the_index_visit_only():
    import pandas as pd

    from omop_etl.benchmark_features import extract_protocol_history_features

    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,"
              " visit_end_date DATE)")
    c.execute("INSERT INTO visit_occurrence VALUES (100, 1, 9201, DATE '2020-01-10', DATE '2020-01-15'), "
              "(101, 1, 9202, DATE '2020-01-12', DATE '2020-01-12')")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
              " condition_source_value VARCHAR, visit_occurrence_id BIGINT, condition_status_source_value VARCHAR)")
    c.execute("""INSERT INTO condition_occurrence VALUES
        (1, 1, DATE '2020-01-10', 'I50.9', 100, 'P'),   -- principal
        (1, 2, DATE '2020-01-10', 'E11.9', 100, NULL),  -- secondary on the index visit
        (1, 3, DATE '2020-01-12', 'M54.5', 101, NULL),  -- outpatient visit inside the stay: left out since 0.34.2
        (1, 4, DATE '2020-01-13', 'N18.3', NULL, NULL)  -- no visit id: by date""")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE)")
    cohort = pd.DataFrame({"row_id": [0], "subject_id": [1], "cohort_start_date": pd.to_datetime(["2020-01-10"]),
                           "cohort_end_date": pd.to_datetime(["2020-01-15"])})
    assert extract_protocol_history_features(c, cohort).loc[0, "n_secondary_dx"] == 3            # no visit id in the cohort: by date
    assert extract_protocol_history_features(c, cohort.assign(visit_occurrence_id=[100])).loc[0, "n_secondary_dx"] == 2
