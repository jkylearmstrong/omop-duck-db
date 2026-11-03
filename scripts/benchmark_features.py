"""Times extract_patient_features on a stay-level cohort, so a run can be budgeted before it is started (F2).

Synthetic fixture (default): N stays, 3 per patient, with E events per patient in each of conditions, drugs, procedures
and measurements. Real database: pass --db (opened READ_ONLY) and --cohort-table (a table with visit_occurrence_id,
person_id, cohort_start_date, cohort_end_date); the first --limit stays are timed, and the cost extrapolates about
linearly in stays.

    python scripts/benchmark_features.py --stays 100000
    python scripts/benchmark_features.py --db site.duckdb --cohort-table stay_cohort --limit 20000
"""
import argparse
import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))
from omop_etl.features import extract_patient_features  # noqa: E402


def synthetic(n_stays, events):
    n_p = max(1, n_stays // 3)
    con = duckdb.connect()
    con.execute(f"""
        CREATE TABLE person AS SELECT i person_id, 1950 + (i % 50) year_of_birth, 1 month_of_birth, 1 day_of_birth,
            8507 gender_concept_id, 8527 race_concept_id, 0 ethnicity_concept_id FROM range({n_p}) t(i);
        CREATE TABLE cohort AS SELECT 1 cohort_definition_id, p.i subject_id, p.i person_id,
            DATE '2020-01-01' + CAST(s * 200 + p.i % 30 AS INT) cohort_start_date,
            DATE '2020-01-06' + CAST(s * 200 + p.i % 30 AS INT) cohort_end_date,
            p.i * 10 + s visit_occurrence_id FROM range({n_p}) p(i), range(3) q(s);""")
    for tbl, idc, dc in [("condition_occurrence", "condition_occurrence_id", "condition_start_date"),
                         ("drug_exposure", "drug_exposure_id", "drug_exposure_start_date"),
                         ("procedure_occurrence", "procedure_occurrence_id", "procedure_date"),
                         ("measurement", "measurement_id", "measurement_date")]:
        extra = ", 3 + (i % 7) measurement_concept_id, (i % 100) * 1.5 value_as_number" if tbl == "measurement" else ""
        con.execute(f"CREATE TABLE {tbl} AS SELECT i {idc}, i % {n_p} person_id, "
                    f"DATE '2020-01-01' + CAST(hash(i) % 700 AS INT) {dc}{extra} FROM range({n_p * events}) t(i)")
    return con, "cohort"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stays", type=int, default=100_000)
    ap.add_argument("--events", type=int, default=40, help="events per patient in each clinical table (synthetic)")
    ap.add_argument("--db")
    ap.add_argument("--cohort-table", default="cohort")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--measurement-concept-ids", type=int, nargs="*")
    a = ap.parse_args()
    if a.db:
        con = duckdb.connect(a.db, read_only=True)
        table = a.cohort_table
        if a.limit:
            con.execute(f"CREATE TEMP VIEW _bench_cohort AS SELECT * FROM {table} LIMIT {int(a.limit)}")
            table = "_bench_cohort"
    else:
        t0 = time.time()
        con, table = synthetic(a.stays, a.events)
        print(f"fixture built in {time.time() - t0:.1f}s")
    t0 = time.time()
    df = extract_patient_features(con, index_key="visit_occurrence_id", cohort_table=table,
                                  measurement_concept_ids=a.measurement_concept_ids or None)
    dt = time.time() - t0
    print(f"{len(df):,} stays x {df.shape[1]} columns in {dt:.1f}s ({len(df) / max(dt, 1e-9):,.0f} stays/s)")


if __name__ == "__main__":
    main()
