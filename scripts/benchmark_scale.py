"""Times the package's heavier tools on a large synthetic OMOP DuckDB, to show where they stop being instant.

    python scripts/benchmark_scale.py [--persons 1000000] [--out benchmark.json]

The CDM is generated in SQL (no pandas), so the benchmark itself is cheap: people, observation periods, 2-4 inpatient stays each, a
condition and drug stream and a measurement stream. It then times, one after the other: the index-stay cohort (native Circe compiler),
the DQD catalog, a reconciliation against a legacy list, the gap explanation, the custom-feature write, and an ablation on a feature
frame. Nothing is returned except timings and row counts, so the numbers can be shared.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from omop_etl import ablate_feature_groups, explain_cohort_gaps, reconcile_cohort, write_custom_features  # noqa: E402
from omop_etl.build_omop_cdm import build_schema  # noqa: E402
from omop_etl.circe import execute_circe_cohort  # noqa: E402
from omop_etl.circe_definitions import target_cohort_circe  # noqa: E402
from omop_etl.dqd_catalog import run_dqd_catalog  # noqa: E402


def build(n: int, seed: int = 1) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    build_schema(con)
    con.execute(f"SELECT setseed({seed / 100.0})")
    con.execute(f"""INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value)
                    SELECT i, 8507, 1930 + CAST(random() * 80 AS INT), 0, 0, 'MRN' || lpad(CAST(i AS VARCHAR), 9, '0') FROM range(1, {n + 1}) t(i)""")
    con.execute("""INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
                   SELECT person_id, person_id, DATE '2015-01-01', CASE WHEN random() < 0.08 THEN DATE '2019-06-30' ELSE DATE '2022-12-31' END, 0 FROM person""")
    con.execute("""INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
                   SELECT row_number() OVER (), person_id, CASE WHEN random() < 0.85 THEN 9201 ELSE 262 END, s, s + CAST(2 + random() * 8 AS INT), 0
                   FROM (SELECT person_id, DATE '2018-01-01' + CAST(random() * 900 AS INT) AS s FROM person, range(1, 4) k(k) WHERE random() < 0.9)""")
    con.execute("""INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
                   SELECT row_number() OVER (), person_id, 201820 + CAST(random() * 20 AS INT), DATE '2018-01-01' + CAST(random() * 900 AS INT), 0
                   FROM person, range(1, 5) k(k) WHERE random() < 0.6""")
    con.execute("""INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_concept_id)
                   SELECT row_number() OVER (), person_id, 3004410, DATE '2018-01-01' + CAST(random() * 900 AS INT), 0, 4 + random() * 8, 8554
                   FROM person, range(1, 6) k(k) WHERE random() < 0.7""")
    con.execute("""INSERT INTO death (person_id, death_date, death_type_concept_id) SELECT person_id, DATE '2019-01-01' + CAST(random() * 900 AS INT), 0
                   FROM person WHERE random() < 0.05""")
    return con


def timed(results: list, name: str, fn, rows=None):
    t0 = time.perf_counter()
    out = fn()
    results.append({"step": name, "seconds": round(time.perf_counter() - t0, 2), "rows": rows(out) if rows else None})
    print(f"{name:<34}{results[-1]['seconds']:>9.2f} s   {results[-1]['rows'] if rows else ''}", flush=True)
    return out


def main(argv=None) -> list[dict]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persons", type=int, default=1_000_000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    results: list[dict] = []
    t0 = time.perf_counter()
    con = build(a.persons)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("person", "visit_occurrence", "condition_occurrence", "measurement")}
    print(f"built {counts} in {time.perf_counter() - t0:.1f} s", flush=True)
    definition = target_cohort_circe()
    cohort = timed(results, "index-stay cohort (Circe)", lambda: execute_circe_cohort(con, definition), len)
    legacy = pd.DataFrame({"person_source_value": [f"MRN{i:09d}" for i in range(1, min(a.persons, 200_000) + 1)]})
    timed(results, "reconcile 200k legacy patients", lambda: reconcile_cohort(con, legacy, definition=definition), lambda r: r["counts"]["both"])
    timed(results, "explain gaps (same list)", lambda: explain_cohort_gaps(con, legacy, definition=definition), lambda r: len(r["gaps"]))
    feats = cohort[["subject_id", "cohort_start_date"]].rename(columns={"subject_id": "person_id", "cohort_start_date": "date"})
    feats["charlson_site"] = np.random.default_rng(1).poisson(2, len(feats)).astype(float)
    timed(results, "write one custom feature", lambda: write_custom_features(con, feats), lambda r: len(feats))
    timed(results, "DQD catalog (all checks)", lambda: run_dqd_catalog(con), lambda r: r["Overview"]["count_total"])
    n = min(len(feats), 100_000)
    rng = np.random.default_rng(2)
    x = pd.DataFrame(rng.normal(size=(n, 20)), columns=[f"f{i}" for i in range(20)])
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-(x["f0"] + 0.5 * x["f1"] - 1)))).astype(int)
    timed(results, "ablation, 100k x 20, 4 groups", lambda: ablate_feature_groups(
        x, y, {f"g{k}": [f"f{k * 5 + j}" for j in range(5)] for k in range(4)}, n_boot=50), len)
    summary = {"persons": a.persons, "table_rows": counts, "total_seconds": round(time.perf_counter() - t0, 1), "steps": results}
    if a.out:
        Path(a.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    main()
