"""Regenerates the scenario list and golden results for the define_study_cohort equivalence tests.

Run from the repository root:  PYTHONPATH=python python tests/fixtures/study_cohort_make_golden.py

Outputs
-------
tests/fixtures/study_cohort_scenarios.csv
    One row per parameter combination of the two legacy builders (``build_readmission_cohort`` and
    ``build_end_of_life_cohort``). Both the Python and the R test suites read this file, so the same
    combinations are exercised in both languages.
tests/fixtures/study_cohort_golden.csv
    The cohort each legacy builder returned for each scenario on the synthetic CDM in ``study_cohort_cdm/``:
    ``scenario_id, subject_id, visit_occurrence_id, outcome_flag, outcome_date``.

tests/fixtures/study_cohort_define_scenarios.csv and tests/fixtures/study_cohort_define_golden.csv
    The same idea for ``define_study_cohort`` capabilities the legacy builders do not have (emergency / outpatient /
    custom visit types, study windows, exact-age rule, cohort-only outcome, other death / follow-up conventions...).
    This golden is produced by the current Python ``define_study_cohort``; the Python test suite checks every
    scenario against an independent pure-Python re-implementation of the cohort rules (the oracle in
    tests/test_define_study_cohort.py), and the R test suite checks R against the same file.

PROVENANCE: the committed golden file was produced by the legacy builders as they stood BEFORE they were
refactored onto the shared study-cohort engine (repository commit 3d0ec7c), via

    git show 3d0ec7c:python/omop_etl/cohort.py > /tmp/legacy_cohort.py
    LEGACY_COHORT_PY=/tmp/legacy_cohort.py PYTHONPATH=python python tests/fixtures/study_cohort_make_golden.py

and was then confirmed identical when produced by the R implementation. It pins the legacy behaviour;
``define_study_cohort`` and the refactored legacy builders are both asserted against it. Without
``LEGACY_COHORT_PY`` the script uses the current builders, which only re-checks that the refactor still reproduces
the golden file; overwrite the committed golden file only when a change to the legacy semantics is intended, and
then re-verify R against it.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import random
import sys
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "python"))

from omop_etl import build_schema  # noqa: E402
import omop_etl.cohort as _current_cohort  # noqa: E402


def _legacy_module():
    path = os.environ.get("LEGACY_COHORT_PY")
    if not path:
        return _current_cohort
    spec = importlib.util.spec_from_file_location("legacy_cohort", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEGACY = _legacy_module()
build_end_of_life_cohort = LEGACY.build_end_of_life_cohort
build_readmission_cohort = LEGACY.build_readmission_cohort

CDM_DIR = HERE / "study_cohort_cdm"
TABLES = ["person", "visit_occurrence", "death", "observation_period", "measurement",
          "condition_occurrence", "drug_exposure"]
SCENARIO_COLUMNS = ["scenario_id", "builder", "visit_ids", "outcome_visit_ids", "window_days", "gap_days",
                    "washin_days", "min_age", "mortality_type", "verified", "rule", "seed"]
MORTALITY_TYPES = ["in_hospital", "post_discharge", "fixed_window", "composite_readmit_or_death"]


def load_cdm(con) -> None:
    build_schema(con)
    for t in TABLES:
        path = (CDM_DIR / f"{t}.csv").as_posix()
        con.execute(f"INSERT INTO {t} BY NAME SELECT * FROM read_csv('{path}', header = true)")


def make_scenarios() -> list[dict]:
    rng = random.Random(77)
    out: list[dict] = []

    def add(builder, **kw):
        row = dict(builder=builder, visit_ids="9201", outcome_visit_ids="9201", window_days=30, gap_days=0,
                   washin_days=365, min_age=18, mortality_type="", verified="TRUE", rule="first", seed=42)
        row.update(kw)
        if row["mortality_type"] != "in_hospital":  # an empty outcome window is not a valid study design
            assert int(row["gap_days"]) < int(row["window_days"]), row
        key = tuple(row[c] for c in SCENARIO_COLUMNS[1:])
        if all(tuple(o[c] for c in SCENARIO_COLUMNS[1:]) != key for o in out):
            row["scenario_id"] = f"{builder[:3]}{len(out) + 1:03d}"
            out.append(row)

    # ---- readmission builder (min_age is fixed at 18 in the builder)
    base = dict(min_age="")
    add("readmission", **base)
    for rule, seed in [("last", 42), ("random", 42), ("random", 7), ("random", 2024)]:
        add("readmission", rule=rule, seed=seed, **base)
    add("readmission", verified="FALSE", **base)
    add("readmission", verified="FALSE", rule="last", **base)
    for fu in (7, 14, 60, 90):
        add("readmission", window_days=fu, **base)
    for g in (1, 3, 7):
        add("readmission", gap_days=g, **base)
    for w in (0, 30, 730):
        add("readmission", washin_days=w, **base)
    for v in ("9201|9203", "9202", "9201|9202|9203", "9203"):
        add("readmission", visit_ids=v, **base)
    for o in ("9201|9203", "9202|9203", "9201|9202|9203"):
        add("readmission", outcome_visit_ids=o, **base)
    for _ in range(10):
        add("readmission", visit_ids=rng.choice(["9201", "9201|9203", "9201|9202|9203"]),
            outcome_visit_ids=rng.choice(["9201", "9201|9203", "9202"]),
            window_days=rng.choice([7, 30, 45, 60]), gap_days=rng.choice([0, 0, 2, 5]),
            washin_days=rng.choice([0, 90, 365]), verified=rng.choice(["TRUE", "FALSE"]),
            rule=rng.choice(["first", "last", "random"]), seed=rng.choice([1, 42, 99]), **base)

    # ---- end-of-life builder, every mortality paradigm
    for mt in MORTALITY_TYPES:
        kw = dict(mortality_type=mt)
        in_hosp = mt == "in_hospital"  # window / gap / verification / outcome visits do not apply in-hospital
        add("eol", **kw)
        for rule, seed in [("last", 42), ("random", 42), ("random", 7)]:
            add("eol", rule=rule, seed=seed, **kw)
        add("eol", verified="FALSE", **kw)
        add("eol", window_days=90, **kw)
        if not in_hosp:
            add("eol", window_days=7, **kw)
            add("eol", window_days=180, **kw)
            add("eol", gap_days=7, **kw)
            add("eol", gap_days=30, window_days=90, **kw)
        for a in (0, 65):
            add("eol", min_age=a, **kw)
        add("eol", washin_days=0, **kw)
        for v in ("ANY", "9201|9203"):
            add("eol", visit_ids=v, **kw)
        if not in_hosp:
            add("eol", outcome_visit_ids="9201|9203", **kw)
        for _ in range(2 if in_hosp else 5):
            add("eol", visit_ids=rng.choice(["9201", "ANY", "9201|9202|9203", "9202"]),
                outcome_visit_ids=rng.choice(["9201", "9201|9203", "9202"]),
                window_days=rng.choice([14, 30, 60, 120]), gap_days=rng.choice([0, 0, 3, 10]),
                washin_days=rng.choice([0, 30, 365]), min_age=rng.choice([0, 18, 40, 65]),
                verified=rng.choice(["TRUE", "FALSE"]), rule=rng.choice(["first", "last", "random"]),
                seed=rng.choice([1, 42, 99]), **kw)
    return out


DEFINE_COLUMNS = ["scenario_id", "visit_type", "outcome_visit_type", "study_start", "study_end", "min_age",
                  "age_method", "min_los_days", "washin_days", "followup_days", "gap_days", "exclude_death",
                  "target_outcome", "mortality_type", "rule", "seed", "verified", "death_ids", "death_sources",
                  "evidence"]


def make_define_scenarios() -> list[dict]:
    """Conventions: '' = argument not passed / None; 'auto' is the literal default sentinel; lists use '|'."""
    base = dict(visit_type="inpatient", outcome_visit_type="inpatient", study_start="", study_end="", min_age="18",
                age_method="year_difference", min_los_days="auto", washin_days="365", followup_days="30",
                gap_days="0", exclude_death="auto", target_outcome="all_cause_readmission",
                mortality_type="post_discharge", rule="first", seed="42", verified="TRUE", death_ids="",
                death_sources="", evidence="")
    out: list[dict] = []

    def add(**kw):
        row = dict(base)
        row.update({k: str(v) for k, v in kw.items()})
        row["scenario_id"] = f"def{len(out) + 1:03d}"
        out.append(row)

    add(study_start="2017-01-01", study_end="2020-12-31", min_los_days=1, exclude_death="TRUE",
        target_outcome="all_cause_readmission_30d", rule="random")  # the RFC example
    add(visit_type="emergency", min_los_days=0)
    add(visit_type="outpatient", min_los_days=0)
    add(visit_type="inpatient|emergency", min_los_days=0)
    add(visit_type="ANY", min_los_days=0)
    add(visit_type="9201|9203")
    add(study_start="2018-06-01")
    add(study_end="2019-06-30")
    add(study_start="2019-01-01", study_end="2019-06-30", washin_days=0)
    add(age_method="completed_years")
    add(age_method="completed_years", min_age=65)
    add(min_age=65)
    add(min_age="none")
    add(min_los_days=0)
    add(min_los_days=3)
    add(min_los_days="none")
    add(washin_days=0)
    add(washin_days=30)
    add(washin_days=730)
    add(followup_days=7)
    add(followup_days=60)
    add(gap_days=5)
    add(exclude_death="FALSE")
    add(target_outcome="none")
    add(target_outcome="none", verified="FALSE")
    add(target_outcome="none", followup_days=60)
    add(target_outcome="none", followup_days="", verified="FALSE")
    add(rule="last")
    add(rule="random", seed=7)
    add(rule="random", seed=123)
    add(verified="FALSE")
    add(outcome_visit_type="inpatient|emergency")
    add(target_outcome="mortality")
    add(target_outcome="mortality", death_ids="4216643")
    add(target_outcome="mortality", death_sources="death_table")
    add(target_outcome="mortality", death_sources="discharge_disposition")
    add(target_outcome="mortality", evidence="observation_period")
    add(target_outcome="mortality", evidence="death|visit")
    add(target_outcome="mortality", mortality_type="in_hospital")
    add(target_outcome="mortality", mortality_type="in_hospital", death_ids="4216643")
    add(target_outcome="mortality", mortality_type="fixed_window", exclude_death="TRUE")
    add(target_outcome="mortality", mortality_type="fixed_window", followup_days=90, gap_days=14)
    add(target_outcome="readmission_or_death", outcome_visit_type="inpatient|emergency")
    add(target_outcome="readmission_or_death", death_ids="4216643", followup_days=60)
    add(target_outcome="mortality", visit_type="emergency", min_los_days=0)
    add(death_sources="death_table|discharge_disposition", death_ids="4216643|4155309")
    add(evidence="observation_period|visit|measurement|condition|drug|death")
    add(study_start="2018-01-01", study_end="2019-12-31", age_method="completed_years", visit_type="emergency",
        min_los_days=0, outcome_visit_type="inpatient|emergency", rule="last")
    return out


def define_kwargs(row: dict) -> dict:
    """Maps a scenario row to define_study_cohort keyword arguments."""
    def lst(v):
        return [int(x) if x.isdigit() else x for x in v.split("|")]

    kw = dict(
        visit_type=None if row["visit_type"] == "ANY" else lst(row["visit_type"]),
        outcome_visit_type=lst(row["outcome_visit_type"]),
        study_window=(row["study_start"] or None, row["study_end"] or None),
        min_age=None if row["min_age"] == "none" else int(row["min_age"]),
        age_method=row["age_method"],
        min_los_days=("auto" if row["min_los_days"] == "auto" else None if row["min_los_days"] == "none"
                      else int(row["min_los_days"])),
        washin_days=int(row["washin_days"]),
        followup_days=int(row["followup_days"]) if row["followup_days"] != "" else None,
        gap_days=int(row["gap_days"]),
        exclude_in_hospital_death="auto" if row["exclude_death"] == "auto" else row["exclude_death"] == "TRUE",
        target_outcome=row["target_outcome"],
        mortality_type=row["mortality_type"],
        sampling_rule=row["rule"],
        random_state=int(row["seed"]),
        require_verified_followup=row["verified"] == "TRUE",
        materialise=False,
        attrition=False,
    )
    if row["death_ids"]:
        kw["death_discharge_concept_ids"] = [int(x) for x in row["death_ids"].split("|")]
    if row["death_sources"]:
        kw["death_sources"] = row["death_sources"].split("|")
    if row["evidence"]:
        kw["followup_evidence"] = row["evidence"].split("|")
    return kw


def ids(spec: str):
    return None if spec == "ANY" else [int(x) for x in spec.split("|")]


def run_legacy(con, s: dict):
    if s["builder"] == "readmission":
        return build_readmission_cohort(
            con, cohort_id=1, outcome_cohort_id=None,
            target_visit_concept_ids=ids(s["visit_ids"]), outcome_visit_concept_ids=ids(s["outcome_visit_ids"]),
            followup_window_days=int(s["window_days"]), grace_days=int(s["gap_days"]),
            washin_days=int(s["washin_days"]), index_selection_rule=s["rule"], random_state=int(s["seed"]),
            require_verified_followup=s["verified"] == "TRUE",
        )
    return build_end_of_life_cohort(
        con, cohort_id=1, outcome_cohort_id=None, mortality_type=s["mortality_type"],
        target_visit_concept_ids=ids(s["visit_ids"]), outcome_visit_concept_ids=ids(s["outcome_visit_ids"]),
        mortality_window_days=int(s["window_days"]), gap_days=int(s["gap_days"]), min_age=int(s["min_age"]),
        washin_days=int(s["washin_days"]), require_verified_followup=s["verified"] == "TRUE",
        index_selection_rule=s["rule"], random_state=int(s["seed"]),
    )


def main() -> None:
    scenarios = make_scenarios()
    with open(HERE / "study_cohort_scenarios.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SCENARIO_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(scenarios)

    con = duckdb.connect(":memory:")
    load_cdm(con)
    n_rows = 0
    with open(HERE / "study_cohort_golden.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["scenario_id", "subject_id", "visit_occurrence_id", "outcome_flag", "outcome_date"])
        for s in scenarios:
            df = run_legacy(con, s).sort_values("subject_id")
            for rec in df.itertuples(index=False):
                od = ""
                if s["builder"] == "eol" and getattr(rec, "outcome_date") is not None and str(rec.outcome_date) not in ("NaT", "None"):
                    od = str(rec.outcome_date)[:10]
                w.writerow([s["scenario_id"], int(rec.subject_id), int(rec.visit_occurrence_id),
                            int(rec.outcome_flag), od])
                n_rows += 1
            print(f"{s['scenario_id']}: {len(df):4d} rows, outcome rate "
                  f"{(df['outcome_flag'].mean() if len(df) else float('nan')):.2f}")
    print(f"{len(scenarios)} scenarios, {n_rows} golden rows")

    from omop_etl.cohort import define_study_cohort

    define_scenarios = make_define_scenarios()
    with open(HERE / "study_cohort_define_scenarios.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DEFINE_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(define_scenarios)
    n_rows = 0
    with open(HERE / "study_cohort_define_golden.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["scenario_id", "subject_id", "visit_occurrence_id", "outcome_flag", "outcome_date",
                    "followup_verified"])
        for s in define_scenarios:
            df = define_study_cohort(con, **define_kwargs(s)).sort_values("subject_id")
            for rec in df.itertuples(index=False):
                flag = "" if pd.isna(rec.outcome_flag) else int(rec.outcome_flag)
                od = "" if pd.isna(rec.outcome_date) else str(rec.outcome_date)[:10]
                fv = "" if pd.isna(rec.followup_verified) else int(rec.followup_verified)
                w.writerow([s["scenario_id"], int(rec.subject_id), int(rec.visit_occurrence_id), flag, od, fv])
                n_rows += 1
            print(f"{s['scenario_id']}: {len(df):4d} rows")
    print(f"{len(define_scenarios)} define scenarios, {n_rows} golden rows")


if __name__ == "__main__":
    main()
