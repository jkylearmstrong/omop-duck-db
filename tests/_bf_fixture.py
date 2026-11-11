"""Synthetic OMOP micro-database shared by the v0.13.0 tests (generated here, no real data)."""
from __future__ import annotations
import duckdb, numpy as np, pandas as pd
from omop_etl import build_schema

LAB = {"creatinine": 3016723, "sodium": 3019550, "chloride": 3014576, "bicarbonate": 3015632}
VITAL = {"systolic_bp": 3004249, "diastolic_bp": 3012888, "bmi": 3038553}

def make_db(n_people: int = 40, seed: int = 3) -> duckdb.DuckDBPyConnection:
    rng = np.random.default_rng(seed)
    con = duckdb.connect(":memory:")
    build_schema(con)
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES "
                "(3016723,'Creatinine','Measurement','LOINC','Lab Test','2160-0','1970-01-01','2099-12-31'),"
                "(3019550,'Sodium','Measurement','LOINC','Lab Test','2951-2','1970-01-01','2099-12-31'),"
                "(3014576,'Chloride','Measurement','LOINC','Lab Test','2075-0','1970-01-01','2099-12-31'),"
                "(3015632,'Bicarbonate','Measurement','LOINC','Lab Test','1963-8','1970-01-01','2099-12-31')")
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES "
                "(21600713,'Insulins','Drug','ATC','ATC 3rd','A10A','1970-01-01','2099-12-31'),(1516766,'Insulin X','Drug','RxNorm','Ingredient','1','1970-01-01','2099-12-31')")
    con.execute("INSERT INTO concept_ancestor VALUES (21600713,21600713,0,0),(21600713,1516766,1,1)")
    vid = mid = cid = did = 0
    for pid in range(1, n_people + 1):
        yob = int(rng.integers(1940, 1990)); female = pid % 2
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (?,?,?,?,?)",
                    [pid, 8532 if female else 8507, yob, 8527, 38003564])
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) "
                    "VALUES (?,?,?,?,?)", [pid, pid, "2015-01-01", "2024-12-31", 32817])
        starts = sorted(pd.Timestamp("2018-01-01") + pd.to_timedelta(rng.integers(0, 1500, size=3), "D"))
        for k, s in enumerate(starts):
            vid += 1
            e = s + pd.Timedelta(days=int(rng.integers(2, 7)))
            disp = int(rng.choice([8536, 581476, 8863, 32216, 8717]))
            con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, discharged_to_concept_id) "
                        "VALUES (?,?,?,?,?,?,?)", [vid, pid, 9201, s.date(), e.date(), 32817, disp])
            for name, c in {**LAB, **VITAL}.items():
                for d in range(0, (e - s).days + 1):
                    mid += 1
                    base = {"creatinine": 1.0, "sodium": 140, "chloride": 104, "bicarbonate": 24, "systolic_bp": 125, "diastolic_bp": 75, "bmi": 28}[name]
                    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES (?,?,?,?,?,?,?)",
                                [mid, pid, c, (s + pd.Timedelta(days=d)).date(), 32817, base + d + float(rng.normal(0, .1)), vid])
            # a measurement AFTER discharge (must never reach the features)
            mid += 1
            con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES (?,?,?,?,?,?,?)",
                        [mid, pid, LAB["creatinine"], (e + pd.Timedelta(days=3)).date(), 32817, 99.0, None])
            if pid % 3 == 0:   # a drug in the year before the admission
                did += 1
                con.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id) VALUES (?,?,?,?,?,?)",
                            [did, pid, 1516766, (s - pd.Timedelta(days=30)).date(), (s - pd.Timedelta(days=1)).date(), 32817])
            cid += 1
            con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, visit_occurrence_id) VALUES (?,?,?,?,?,?)",
                        [cid, pid, 201826, s.date(), 32817, vid])
    return con
