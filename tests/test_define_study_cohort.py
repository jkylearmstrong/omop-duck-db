"""Tests for define_study_cohort, the unified study-cohort template engine.

Layers of evidence, from strongest to most specific:

* Golden equivalence: ``build_readmission_cohort`` / ``build_end_of_life_cohort`` (now thin wrappers over the shared
  engine) and ``define_study_cohort`` with equivalent parameters reproduce, row for row, results that were produced by the
  legacy builders BEFORE they were refactored (tests/fixtures/study_cohort_golden.csv, 110 parameter combinations on a
  randomized adversarial CDM). The R suite asserts the same file.
* Independent oracle: every ``define_study_cohort`` scenario (tests/fixtures/study_cohort_define_scenarios.csv) is also
  recomputed by a plain-Python re-implementation of the documented rules and compared; the committed define golden
  (asserted by R too) pins the results.
* Hand-built micro-CDMs, one property per test: window / age / length-of-stay / wash-in / follow-up boundaries, death
  ascertainment, sampling, visit types, materialisation (incl. read-only connections), idempotence, validation.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import itertools
import os
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import (  # noqa: E402
    build_end_of_life_cohort,
    build_readmission_cohort,
    build_schema,
    compute_attrition,
)
from omop_etl.cohort import define_study_cohort  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
CDM_DIR = FIXTURES / "study_cohort_cdm"
CDM_TABLES = ["person", "visit_occurrence", "death", "observation_period", "measurement", "condition_occurrence",
              "drug_exposure"]
EXPECTED_COLUMNS = [
    "cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date", "visit_occurrence_id",
    "visit_concept_id", "outcome_flag", "outcome_date", "followup_verified", "age_at_index", "los_days",
    "discharged_to_concept_id", "target_outcome",
]
# the RFC 2.2 example, verbatim apart from the connection and the repository-wide default of materialising
RFC_KWARGS = dict(
    visit_type="inpatient",
    study_window=("2017-01-01", "2020-12-31"),
    min_age=18,
    min_los_days=1,
    washin_days=365,
    followup_days=30,
    exclude_in_hospital_death=True,
    target_outcome="all_cause_readmission_30d",
)
# CONSORT subject counts of the RFC example on the randomized fixture; the R suite asserts the same numbers
RFC_ATTRITION_SUBJECTS = [216, 204, 189, 180, 107, 96, 90]


# ======================================================================================================
# helpers
# ======================================================================================================

def read_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_fixture_cdm(con) -> None:
    build_schema(con)
    for t in CDM_TABLES:
        path = (CDM_DIR / f"{t}.csv").as_posix()
        con.execute(f"INSERT INTO {t} BY NAME SELECT * FROM read_csv('{path}', header = true)")


@pytest.fixture(scope="module")
def cdm():
    """The shared randomized adversarial CDM (260 persons); tests only write cohort ids they own."""
    con = duckdb.connect(":memory:")
    load_fixture_cdm(con)
    yield con
    con.close()


def day(base: str, n: int = 0) -> str:
    return (dt.date.fromisoformat(base) + dt.timedelta(days=n)).isoformat()


class Cdm:
    """A micro CDM for hand-checkable scenarios (in-memory, or a file when ``path`` is given)."""

    def __init__(self, path=None):
        self.con = duckdb.connect(str(path) if path else ":memory:")
        build_schema(self.con)
        self._seq = itertools.count(1)

    def person(self, pid, born="1960-01-01"):
        y, m, d = (int(x) for x in born.split("-"))
        self.con.execute(
            "INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, "
            f"race_concept_id, ethnicity_concept_id) VALUES ({pid}, 8507, {y}, {m}, {d}, 8527, 38003564)")
        return self

    def visit(self, vid, pid, start, end=None, concept=9201, disch=None):
        end = end or start
        self.con.execute(
            "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
            "visit_end_date, visit_type_concept_id, discharged_to_concept_id) VALUES "
            f"({vid}, {pid}, {concept}, DATE '{start}', DATE '{end}', 32827, {'NULL' if disch is None else disch})")
        return self

    def death(self, pid, date):
        self.con.execute(f"INSERT INTO death (person_id, death_date) VALUES ({pid}, DATE '{date}')")
        return self

    def obs(self, pid, start, end):
        self.con.execute(
            "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
            f"observation_period_end_date, period_type_concept_id) VALUES ({next(self._seq)}, {pid}, DATE '{start}', "
            f"DATE '{end}', 32817)")
        return self

    def measurement(self, pid, date):
        self.con.execute(
            "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
            f"measurement_type_concept_id) VALUES ({next(self._seq)}, {pid}, 3004249, DATE '{date}', 32817)")
        return self

    def condition(self, pid, date):
        self.con.execute(
            "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
            f"condition_start_date, condition_type_concept_id) VALUES ({next(self._seq)}, {pid}, 201826, DATE '{date}', 32020)")
        return self

    def drug(self, pid, date):
        self.con.execute(
            "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
            f"drug_exposure_end_date, drug_type_concept_id) VALUES ({next(self._seq)}, {pid}, 1503297, DATE '{date}', DATE '{date}', 32838)")
        return self

    def define(self, **kw):
        kw.setdefault("materialise", False)
        kw.setdefault("attrition", False)
        return define_study_cohort(self.con, **kw)

    def ids(self, **kw) -> list[int]:
        return [int(x) for x in self.define(**kw).subject_id]


# Settings that isolate one rule at a time: no wash-in, no follow-up filter, readmission outcome
SIMPLE = dict(washin_days=0, require_verified_followup=False)


def _nz(x):
    return None if pd.isna(x) else x


def _date_str(x):
    return None if pd.isna(x) else str(pd.Timestamp(x).date())


def _int_or_none(x):
    return None if pd.isna(x) else int(x)


def _dates(series) -> list:
    """ISO date strings, None for missing (NaT) values."""
    return [_date_str(x) for x in series]


# ======================================================================================================
# 1. golden equivalence with the legacy builders
# ======================================================================================================

LEGACY_SCENARIOS = read_csv_rows(FIXTURES / "study_cohort_scenarios.csv")
LEGACY_GOLDEN = defaultdict(list)
for _r in read_csv_rows(FIXTURES / "study_cohort_golden.csv"):
    LEGACY_GOLDEN[_r["scenario_id"]].append(
        (int(_r["subject_id"]), int(_r["visit_occurrence_id"]), int(_r["outcome_flag"]), _r["outcome_date"] or None))


def _id_list(spec: str):
    return None if spec == "ANY" else [int(x) for x in spec.split("|")]


def run_legacy(con, s: dict) -> pd.DataFrame:
    if s["builder"] == "readmission":
        return build_readmission_cohort(
            con, cohort_id=1, outcome_cohort_id=None,
            target_visit_concept_ids=_id_list(s["visit_ids"]), outcome_visit_concept_ids=_id_list(s["outcome_visit_ids"]),
            followup_window_days=int(s["window_days"]), grace_days=int(s["gap_days"]),
            washin_days=int(s["washin_days"]), index_selection_rule=s["rule"], random_state=int(s["seed"]),
            require_verified_followup=s["verified"] == "TRUE")
    return build_end_of_life_cohort(
        con, cohort_id=1, outcome_cohort_id=None, mortality_type=s["mortality_type"],
        target_visit_concept_ids=_id_list(s["visit_ids"]), outcome_visit_concept_ids=_id_list(s["outcome_visit_ids"]),
        mortality_window_days=int(s["window_days"]), gap_days=int(s["gap_days"]), min_age=int(s["min_age"]),
        washin_days=int(s["washin_days"]), require_verified_followup=s["verified"] == "TRUE",
        index_selection_rule=s["rule"], random_state=int(s["seed"]))


def define_equivalent(con, s: dict) -> pd.DataFrame:
    """define_study_cohort with the parameters equivalent to a legacy scenario (see its docstring)."""
    kw = dict(
        visit_type=_id_list(s["visit_ids"]), outcome_visit_type=_id_list(s["outcome_visit_ids"]),
        followup_days=int(s["window_days"]), gap_days=int(s["gap_days"]), washin_days=int(s["washin_days"]),
        sampling_rule=s["rule"], random_state=int(s["seed"]), require_verified_followup=s["verified"] == "TRUE",
        materialise=False, attrition=False)
    if s["builder"] == "readmission":
        kw.update(target_outcome="all_cause_readmission", min_age=18)
    else:
        mt = s["mortality_type"]
        kw["min_age"] = int(s["min_age"])
        if mt == "composite_readmit_or_death":
            kw["target_outcome"] = "readmission_or_death"
        else:
            kw.update(target_outcome="mortality", mortality_type=mt)
        if mt == "in_hospital":
            kw["gap_days"] = 0  # the window does not exist in-hospital; the legacy builder ignores it
    return define_study_cohort(con, **kw)


def legacy_signature(df: pd.DataFrame, with_date: bool):
    out = []
    for r in df.sort_values("subject_id").itertuples(index=False):
        out.append((int(r.subject_id), int(r.visit_occurrence_id), int(r.outcome_flag),
                    _date_str(r.outcome_date) if with_date else None))
    return out


@pytest.mark.parametrize("sc", LEGACY_SCENARIOS, ids=[s["scenario_id"] for s in LEGACY_SCENARIOS])
def test_legacy_builders_reproduce_pre_refactor_results(cdm, sc):
    with_date = sc["builder"] == "eol"
    got = legacy_signature(run_legacy(cdm, sc), with_date)
    expected = [(a, b, c, d if with_date else None) for a, b, c, d in LEGACY_GOLDEN[sc["scenario_id"]]]
    assert got == expected


@pytest.mark.parametrize("sc", LEGACY_SCENARIOS, ids=[s["scenario_id"] for s in LEGACY_SCENARIOS])
def test_define_study_cohort_equals_legacy_builder_row_for_row(cdm, sc):
    with_date = sc["builder"] == "eol"
    df = define_equivalent(cdm, sc)
    expected = [(a, b, c, d if with_date else None) for a, b, c, d in LEGACY_GOLDEN[sc["scenario_id"]]]
    assert legacy_signature(df, with_date) == expected
    # the dates / age / LOS columns are the index stay's own values
    vis = cdm.execute("SELECT visit_occurrence_id, visit_start_date, visit_end_date FROM visit_occurrence").df()
    vis = vis.set_index("visit_occurrence_id")
    for r in df.itertuples(index=False):
        v = vis.loc[r.visit_occurrence_id]
        assert pd.Timestamp(r.cohort_start_date) == v.visit_start_date
        assert pd.Timestamp(r.cohort_end_date) == v.visit_end_date
        assert r.los_days == (v.visit_end_date - v.visit_start_date).days


def test_legacy_builder_outputs_keep_their_documented_columns(cdm):
    r = build_readmission_cohort(cdm, cohort_id=1, washin_days=0)
    assert list(r.columns) == ["cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date",
                               "visit_occurrence_id", "outcome_flag", "los_days", "age_at_admission",
                               "discharged_to_concept_id"]
    e = build_end_of_life_cohort(cdm, cohort_id=1, outcome_cohort_id=None, washin_days=0)
    assert list(e.columns) == ["cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date",
                               "visit_occurrence_id", "outcome_flag", "outcome_date", "mortality_type",
                               "age_at_index", "los_days", "discharged_to_concept_id"]
    assert set(e.mortality_type) == {"post_discharge"}


# ======================================================================================================
# 2. independent oracle for the define scenarios
# ======================================================================================================

DEFINE_SCENARIOS = read_csv_rows(FIXTURES / "study_cohort_define_scenarios.csv")
DEFINE_GOLDEN = defaultdict(list)
for _r in read_csv_rows(FIXTURES / "study_cohort_define_golden.csv"):
    DEFINE_GOLDEN[_r["scenario_id"]].append((
        int(_r["subject_id"]), int(_r["visit_occurrence_id"]),
        int(_r["outcome_flag"]) if _r["outcome_flag"] != "" else None,
        _r["outcome_date"] or None,
        int(_r["followup_verified"]) if _r["followup_verified"] != "" else None))


def define_kwargs(row: dict) -> dict:
    """Scenario row -> define_study_cohort keyword arguments (same mapping as the R suite)."""
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


class OracleCdm:
    """The fixture CSVs as plain Python structures."""

    def __init__(self):
        d = lambda s: dt.date.fromisoformat(s) if s else None  # noqa: E731
        self.persons = {int(r["person_id"]): (int(r["year_of_birth"]), int(r["month_of_birth"] or 1),
                                              int(r["day_of_birth"] or 1))
                        for r in read_csv_rows(CDM_DIR / "person.csv")}
        self.visits = [dict(id=int(r["visit_occurrence_id"]), pid=int(r["person_id"]), concept=int(r["visit_concept_id"]),
                            start=d(r["visit_start_date"]), end=d(r["visit_end_date"]),
                            disch=int(r["discharged_to_concept_id"]) if r["discharged_to_concept_id"] else None)
                       for r in read_csv_rows(CDM_DIR / "visit_occurrence.csv")]
        self.by_person = defaultdict(list)
        for v in self.visits:
            self.by_person[v["pid"]].append(v)
        self.deaths = defaultdict(list)
        for r in read_csv_rows(CDM_DIR / "death.csv"):
            self.deaths[int(r["person_id"])].append(d(r["death_date"]))
        self.obs = defaultdict(list)
        for r in read_csv_rows(CDM_DIR / "observation_period.csv"):
            self.obs[int(r["person_id"])].append((d(r["observation_period_start_date"]), d(r["observation_period_end_date"])))
        self.events = {}
        for name, table, col in [("measurement", "measurement", "measurement_date"),
                                 ("condition", "condition_occurrence", "condition_start_date"),
                                 ("drug", "drug_exposure", "drug_exposure_start_date")]:
            self.events[name] = defaultdict(list)
            for r in read_csv_rows(CDM_DIR / f"{table}.csv"):
                self.events[name][int(r["person_id"])].append(d(r[col]))


@pytest.fixture(scope="module")
def oracle_cdm():
    return OracleCdm()


def completed_years(dob, on: dt.date) -> int:
    y, m, d = dob
    anniversary = dt.date(on.year, m, min(d, calendar.monthrange(on.year, m)[1]))
    return on.year - y - (1 if on < anniversary else 0)


def resolve_oracle_params(sc: dict) -> dict:
    """Re-derives the effective settings from the documented defaults (independently of the engine)."""
    t = sc["target_outcome"]
    if t.startswith("all_cause_readmission"):
        fam = "readmission"
    elif t == "none":
        fam = "none"
    elif t == "readmission_or_death":
        fam = "composite"
    else:
        fam = sc["mortality_type"]
    mortality = fam in ("in_hospital", "post_discharge", "fixed_window", "composite")
    auto_los = {"none": 1, "readmission": 1, "post_discharge": 1, "composite": 1, "in_hospital": 0, "fixed_window": None}
    auto_excl = {"none": True, "readmission": True, "post_discharge": True, "composite": True, "in_hospital": False,
                 "fixed_window": False}
    visit = sc["visit_type"]
    names = {"inpatient": 9201, "emergency": 9203, "outpatient": 9202}
    visit_ids = None if visit == "ANY" else {names[x] if x in names else int(x) for x in visit.split("|")}
    outcome_ids = {names[x] if x in names else int(x) for x in sc["outcome_visit_type"].split("|")}
    window = int(sc["followup_days"]) if sc["followup_days"] != "" else None
    return dict(
        fam=fam, visit_ids=visit_ids, outcome_ids=outcome_ids,
        start=dt.date.fromisoformat(sc["study_start"]) if sc["study_start"] else None,
        end=dt.date.fromisoformat(sc["study_end"]) if sc["study_end"] else None,
        min_age=None if sc["min_age"] == "none" else int(sc["min_age"]), age_method=sc["age_method"],
        min_los=auto_los[fam] if sc["min_los_days"] == "auto" else None if sc["min_los_days"] == "none" else int(sc["min_los_days"]),
        washin=int(sc["washin_days"]), W=None if fam == "in_hospital" else window, gap=int(sc["gap_days"]),
        exclude=auto_excl[fam] if sc["exclude_death"] == "auto" else sc["exclude_death"] == "TRUE",
        death_ids={int(x) for x in sc["death_ids"].split("|")} if sc["death_ids"] else ({4216643, 4155309} if mortality else {4216643}),
        death_sources=set(sc["death_sources"].split("|")) if sc["death_sources"] else ({"death_table", "discharge_disposition"} if mortality else {"death_table"}),
        evidence=set(sc["evidence"].split("|")) if sc["evidence"] else (
            {"observation_period", "visit", "measurement", "condition", "drug", "death"} if mortality
            else {"visit", "measurement", "condition", "drug"}),
        verified=sc["verified"] == "TRUE", rule=sc["rule"], seed=int(sc["seed"]),
    )


def oracle_cohort(cdm: OracleCdm, con, p: dict):
    """Plain-Python cohort definition: rows (subject, visit, outcome_flag, outcome_date, verified, age, los)."""
    td = lambda n: dt.timedelta(days=n)  # noqa: E731
    W, gap, fam = p["W"], p["gap"], p["fam"]

    death = {}
    for pid in cdm.persons:
        dates = []
        if "death_table" in p["death_sources"]:
            dates += cdm.deaths.get(pid, [])
        if "discharge_disposition" in p["death_sources"]:
            dates += [v["end"] for v in cdm.by_person[pid] if v["disch"] in p["death_ids"]]
        death[pid] = min(dates) if dates else None

    eligible = defaultdict(list)
    for v in cdm.visits:
        pid, D = v["pid"], death[v["pid"]]
        if p["visit_ids"] is not None and v["concept"] not in p["visit_ids"]:
            continue
        if p["start"] is not None and v["start"] < p["start"]:
            continue
        if p["end"] is not None and v["start"] > p["end"]:
            continue
        dob = cdm.persons[pid]
        age = v["start"].year - dob[0] if p["age_method"] == "year_difference" else completed_years(dob, v["start"])
        if p["min_age"] is not None and age < p["min_age"]:
            continue
        los = (v["end"] - v["start"]).days
        if p["min_los"] is not None and los < p["min_los"]:
            continue
        if p["exclude"]:
            if (v["disch"] or 0) in p["death_ids"] or (D is not None and not D > v["end"]):
                continue
        elif D is not None and not D >= v["start"]:
            continue
        if p["washin"] > 0:
            covered = any(s <= v["start"] - td(p["washin"]) and e >= v["start"] for s, e in cdm.obs.get(pid, []))
            earlier = any(o["id"] != v["id"] and o["start"] <= v["start"] - td(p["washin"]) for o in cdm.by_person[pid])
            if not (covered or earlier):
                continue

        def readmits():
            return [o["start"] for o in cdm.by_person[pid]
                    if o["id"] != v["id"] and o["concept"] in p["outcome_ids"]
                    and v["end"] + td(gap) < o["start"] <= v["end"] + td(W)]

        flag = odate = None
        if fam == "readmission":
            r = readmits()
            flag, odate = int(bool(r)), (min(r) if r else None)
        elif fam == "in_hospital":
            hit = (v["disch"] or 0) in p["death_ids"] or (D is not None and v["start"] <= D <= v["end"])
            flag, odate = int(hit), ((D or v["end"]) if hit else None)
        elif fam in ("post_discharge", "fixed_window", "composite"):
            anchor = v["start"] if fam == "fixed_window" else v["end"]
            in_window = D is not None and anchor + td(gap) < D <= anchor + td(W)
            if fam == "composite":
                r = readmits()
                cands = ([D] if in_window else []) + r
                flag, odate = int(in_window or bool(r)), (min(cands) if cands else None)
            else:
                flag, odate = int(in_window), (D if in_window else None)

        verified = None
        if fam != "in_hospital" and W is not None:
            threshold = (v["start"] if fam == "fixed_window" else v["end"]) + td(W)
            has = (
                ("observation_period" in p["evidence"] and any(e >= threshold for _, e in cdm.obs.get(pid, [])))
                or ("visit" in p["evidence"] and any(o["id"] != v["id"] and o["start"] >= threshold for o in cdm.by_person[pid]))
                or any(name in p["evidence"] and any(x >= threshold for x in cdm.events[name].get(pid, []))
                       for name in ("measurement", "condition", "drug"))
                or ("death" in p["evidence"] and D is not None and D >= threshold)
            )
            verified = int(has) if fam == "none" else int(flag == 1 or has)
            if p["verified"] and not verified:
                continue
        eligible[pid].append(dict(id=v["id"], start=v["start"], flag=flag, date=odate, verified=verified, age=age, los=los))

    out = []
    for pid, stays in sorted(eligible.items()):
        if p["rule"] == "first":
            pick = min(stays, key=lambda s: (s["start"], s["id"]))
        elif p["rule"] == "last":
            pick = max(stays, key=lambda s: (s["start"], s["id"]))
        else:  # random: argmin of DuckDB's hash(visit_occurrence_id, seed), ties by id
            hashed = {s["id"]: con.execute(f"SELECT hash(CAST({s['id']} AS INTEGER), {p['seed']})").fetchone()[0] for s in stays}
            pick = min(stays, key=lambda s: (hashed[s["id"]], s["id"]))
        out.append((pid, pick["id"], pick["flag"], str(pick["date"]) if pick["date"] else None, pick["verified"],
                    pick["age"], pick["los"]))
    return out


def define_signature(df: pd.DataFrame):
    return [(int(r.subject_id), int(r.visit_occurrence_id), _int_or_none(r.outcome_flag), _date_str(r.outcome_date),
             _int_or_none(r.followup_verified), int(r.age_at_index), int(r.los_days))
            for r in df.sort_values("subject_id").itertuples(index=False)]


@pytest.mark.parametrize("sc", DEFINE_SCENARIOS, ids=[s["scenario_id"] for s in DEFINE_SCENARIOS])
def test_define_study_cohort_matches_independent_oracle(cdm, oracle_cdm, sc):
    got = define_signature(define_study_cohort(cdm, **define_kwargs(sc)))
    assert got == oracle_cohort(oracle_cdm, cdm, resolve_oracle_params(sc))


@pytest.mark.parametrize("sc", DEFINE_SCENARIOS, ids=[s["scenario_id"] for s in DEFINE_SCENARIOS])
def test_define_study_cohort_matches_committed_golden(cdm, sc):
    df = define_study_cohort(cdm, **define_kwargs(sc)).sort_values("subject_id")
    got = [(int(r.subject_id), int(r.visit_occurrence_id), _int_or_none(r.outcome_flag), _date_str(r.outcome_date),
            _int_or_none(r.followup_verified)) for r in df.itertuples(index=False)]
    assert got == DEFINE_GOLDEN[sc["scenario_id"]]


def test_define_scenarios_exercise_every_outcome_and_a_mix_of_flags(cdm):
    """Guards the fixture itself: the scenario set must not degenerate into empty or constant cohorts."""
    seen_labels, informative = set(), 0
    for sc in DEFINE_SCENARIOS:
        df = define_study_cohort(cdm, **define_kwargs(sc))
        seen_labels.add(df.target_outcome.iloc[0] if len(df) else sc["target_outcome"])
        assert len(df) > 5, sc["scenario_id"]
        if df.outcome_flag.notna().any() and df.outcome_flag.nunique() == 2:
            informative += 1
    assert {"all_cause_readmission", "none", "mortality_post_discharge", "mortality_in_hospital",
            "mortality_fixed_window", "readmission_or_death"} <= seen_labels
    assert informative >= 30


# ======================================================================================================
# 3. the RFC example, verbatim
# ======================================================================================================

def test_rfc_example_runs_verbatim_on_the_randomized_cdm(cdm):
    cohort = define_study_cohort(cdm, **RFC_KWARGS)
    assert list(cohort.columns) == EXPECTED_COLUMNS
    assert cohort.subject_id.is_unique and cohort.subject_id.is_monotonic_increasing
    assert cohort.target_outcome.eq("all_cause_readmission").all()
    assert cohort.cohort_start_date.between("2017-01-01", "2020-12-31").all()
    assert (cohort.age_at_index >= 18).all() and (cohort.los_days >= 1).all()
    assert cohort.outcome_flag.isin([0, 1]).all() and cohort.followup_verified.eq(1).all()
    attrition = cohort.attrs["attrition"]
    assert list(attrition.subjects_retained) == RFC_ATTRITION_SUBJECTS
    assert attrition.subjects_retained.iloc[-1] == len(cohort)
    assert list(attrition.columns) == ["step_number", "step_name", "subjects_retained", "subjects_dropped",
                                       "percent_retained"]
    assert attrition.step_name.tolist() == [
        "Index visit type in (9201)",
        "Index visit start between 2017-01-01 and 2020-12-31 (inclusive)",
        "Age >= 18 years at index",
        "Length of stay >= 1 day(s)",
        "Prior observation >= 365 days (wash-in)",
        "No in-hospital death at index stay",
        "Verified follow-up (evidence >= 30 days after discharge)",
    ]
    summary = cohort.attrs["summary"]
    assert int(summary.total_subjects.iloc[0]) == len(cohort)
    # materialised exactly as the legacy builders do
    n = cdm.execute("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 1").fetchone()[0]
    assert n == len(cohort)
    assert cohort.attrs["definition"]["target_outcome"] == "all_cause_readmission"


def test_rfc_example_equals_explicit_equivalent_parameters(cdm):
    rfc = define_study_cohort(cdm, materialise=False, attrition=False, **RFC_KWARGS)
    explicit = define_study_cohort(
        cdm, visit_type=[9201], study_window=("2017-01-01", "2020-12-31"), min_age=18, min_los_days=1, washin_days=365,
        followup_days=30, exclude_in_hospital_death=True, target_outcome="all_cause_readmission",
        outcome_visit_type=9201, gap_days=0, sampling_rule="random", random_state=42, require_verified_followup=True,
        materialise=False, attrition=False)
    pd.testing.assert_frame_equal(rfc, explicit)


def test_rfc_example_study_window_only_restricts_the_index_date(cdm):
    """Readmissions after study_end still count (outcomes are ascertained relative to each index stay)."""
    cohort = define_study_cohort(cdm, materialise=False, attrition=False, **{**RFC_KWARGS, "washin_days": 0})
    late = cohort[(pd.to_datetime(cohort.cohort_end_date) + pd.Timedelta(days=30)) > pd.Timestamp("2020-12-31")]
    assert len(late) > 0
    unrestricted = define_study_cohort(
        cdm, materialise=False, attrition=False, **{**RFC_KWARGS, "washin_days": 0, "study_window": None})
    # same stays, same labels, wherever the unrestricted run picked the same index stay
    both = late.merge(unrestricted, on="subject_id", suffixes=("", "_u"))
    same_stay = both[both.visit_occurrence_id == both.visit_occurrence_id_u]
    assert len(same_stay) > 0 and (same_stay.outcome_flag == same_stay.outcome_flag_u).all()


# ======================================================================================================
# 4. boundaries, one property per test
# ======================================================================================================

def test_study_window_is_inclusive_at_both_ends_and_either_end_may_be_open():
    c = Cdm()
    for pid, start in [(1, "2016-12-31"), (2, "2017-01-01"), (3, "2020-12-31"), (4, "2021-01-01")]:
        c.person(pid).visit(pid * 10, pid, start, day(start, 2))
    kw = dict(target_outcome="none", **SIMPLE)
    assert c.ids(study_window=("2017-01-01", "2020-12-31"), **kw) == [2, 3]
    assert c.ids(study_window=("2017-01-01", None), **kw) == [2, 3, 4]
    assert c.ids(study_window=(None, "2020-12-31"), **kw) == [1, 2, 3]
    assert c.ids(study_window=(None, None), **kw) == [1, 2, 3, 4]
    assert c.ids(study_window=None, **kw) == [1, 2, 3, 4]
    # a single-day window, and dates / datetimes instead of strings
    assert c.ids(study_window=("2017-01-01", "2017-01-01"), **kw) == [2]
    assert c.ids(study_window=(dt.date(2017, 1, 1), dt.datetime(2020, 12, 31, 23, 59)), **kw) == [2, 3]
    assert c.ids(study_window=(pd.Timestamp("2017-01-01"), pd.Timestamp("2020-12-31")), **kw) == [2, 3]


def test_min_age_on_the_boundary_day():
    c = Cdm()
    c.person(1, "2003-06-15").visit(11, 1, "2021-06-15", "2021-06-17")  # turns 18 on the index day
    c.person(2, "2003-06-16").visit(21, 2, "2021-06-15", "2021-06-17")  # one day short of 18
    c.person(3, "2003-12-31").visit(31, 3, "2021-01-01", "2021-01-03")  # 17 years and 1 day
    c.person(4, "2004-02-29").visit(41, 4, "2022-02-28", "2022-03-02")  # leap-day birthday: anniversary on 28 Feb
    c.person(5, "2004-02-29").visit(51, 5, "2022-02-27", "2022-03-02")  # the day before it
    kw = dict(target_outcome="none", min_age=18, **SIMPLE)
    assert c.ids(age_method="completed_years", **kw) == [1, 4]
    exact = c.define(age_method="completed_years", min_age=17, **{k: v for k, v in kw.items() if k != "min_age"})
    assert dict(zip(exact.subject_id, exact.age_at_index)) == {1: 18, 2: 17, 3: 17, 4: 18, 5: 17}
    # the legacy default (year_difference) counts calendar-year boundaries, so everyone above passes
    legacy = c.define(**kw)
    assert list(legacy.subject_id) == [1, 2, 3, 4, 5]
    assert dict(zip(legacy.subject_id, legacy.age_at_index)) == {1: 18, 2: 18, 3: 18, 4: 18, 5: 18}
    # min_age=None disables the rule; min_age=0 keeps everyone with a birth year
    c.person(6, "2020-01-01").visit(61, 6, "2021-01-01", "2021-01-03")
    assert c.ids(target_outcome="none", min_age=None, **SIMPLE)[-1] == 6
    assert c.ids(target_outcome="none", min_age=0, **SIMPLE)[-1] == 6
    assert 6 not in c.ids(target_outcome="none", min_age=18, **SIMPLE)


def test_min_age_missing_birth_month_and_day_count_as_the_first():
    c = Cdm()
    c.con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  "VALUES (1, 8507, 2000, 8527, 38003564)")
    c.visit(11, 1, "2018-01-01", "2018-01-03").visit(12, 1, "2017-12-31", "2018-01-01")
    assert c.ids(age_method="completed_years", min_age=18, target_outcome="none", **SIMPLE) == [1]  # born 2000-01-01
    assert c.ids(age_method="completed_years", min_age=19, target_outcome="none", **SIMPLE) == []


def test_length_of_stay_at_exactly_min_los_days_and_one_less():
    c = Cdm()
    for i, los in enumerate([0, 1, 2, 3], start=1):
        c.person(i).visit(i * 10, i, "2021-03-01", day("2021-03-01", los))
    c.person(5).visit(50, 5, "2021-03-01", "2021-02-28")  # end before start: a data error
    kw = dict(target_outcome="none", **SIMPLE)
    assert c.ids(min_los_days=1, **kw) == [2, 3, 4]
    assert c.ids(min_los_days=2, **kw) == [3, 4]
    assert c.ids(min_los_days=3, **kw) == [4]
    assert c.ids(min_los_days=0, **kw) == [1, 2, 3, 4]
    assert c.ids(min_los_days=None, **kw) == [1, 2, 3, 4, 5]
    assert c.ids(**kw) == [2, 3, 4]  # "auto" is 1 day for a cohort-only template
    los = c.define(min_los_days=0, **kw)
    assert list(los.los_days) == [0, 1, 2, 3]


def test_washin_boundary_observation_period_and_prior_visit_fallback():
    index = "2021-06-01"
    c = Cdm()
    for pid in range(1, 8):
        c.person(pid).visit(pid * 10, pid, index, day(index, 3))
    c.obs(1, day(index, -365), day(index, 30))   # starts exactly washin_days before the index: qualifies
    c.obs(2, day(index, -364), day(index, 30))   # one day too late
    c.obs(3, day(index, -900), day(index, -1))   # long enough but ends the day before the index: not covering
    c.obs(4, day(index, -900), index)            # ends on the index day: covering
    c.visit(51, 5, day(index, -365), day(index, -365), concept=9202)  # no observation period; prior visit 365 d before
    c.visit(61, 6, day(index, -364), day(index, -364), concept=9202)  # 364 d before: too late
    # person 7: no observation period and no other visit
    kw = dict(target_outcome="none", require_verified_followup=False)
    assert c.ids(washin_days=365, **kw) == [1, 4, 5]
    assert c.ids(washin_days=364, **kw) == [1, 2, 4, 5, 6]
    assert c.ids(washin_days=0, **kw) == [1, 2, 3, 4, 5, 6, 7]
    assert c.ids(washin_days=None, **kw) == [1, 2, 3, 4, 5, 6, 7]


def test_in_hospital_death_via_the_death_table_only():
    c = Cdm()
    stay = ("2021-01-01", "2021-01-05")
    for pid in range(1, 6):
        c.person(pid).visit(pid * 10, pid, *stay)
    c.death(1, "2021-01-03")   # during the stay
    c.death(2, "2021-01-05")   # on the discharge day
    c.death(3, "2021-01-06")   # the day after discharge: alive at discharge
    c.death(4, "2020-12-20")   # before the admission (inconsistent source data)
    kw = dict(target_outcome="all_cause_readmission", **SIMPLE)
    assert c.ids(exclude_in_hospital_death=True, **kw) == [3, 5]
    assert c.ids(**kw) == [3, 5]  # "auto" excludes for readmission
    # retained when the exclusion is switched off, except a stay that starts after the death
    kept = c.define(exclude_in_hospital_death=False, **kw)
    assert list(kept.subject_id) == [1, 2, 3, 5]
    assert kept.outcome_flag.eq(0).all()  # a patient who died cannot be readmitted: the label is a non-event


def test_in_hospital_death_via_the_discharge_disposition_only():
    c = Cdm()
    stay = ("2021-01-01", "2021-01-05")
    c.person(1).visit(10, 1, *stay, disch=4216643)   # 'Patient died'
    c.person(2).visit(20, 2, *stay, disch=4155309)   # legacy end-of-life code (see the docstring)
    c.person(3).visit(30, 3, *stay, disch=8536)
    kw = dict(target_outcome="all_cause_readmission", **SIMPLE)
    assert c.ids(**kw) == [2, 3]                                                  # readmission convention: 4216643 only
    assert c.ids(death_discharge_concept_ids=[4216643, 4155309], **kw) == [3]
    assert c.ids(death_discharge_concept_ids=4216643, **kw) == [2, 3]
    assert c.ids(death_discharge_concept_ids=[8536], **kw) == [1, 2]
    # the mortality convention is the legacy end-of-life one unless overridden
    mort = dict(target_outcome="mortality", mortality_type="post_discharge", **SIMPLE)
    assert c.ids(**mort) == [3]
    assert c.ids(death_discharge_concept_ids=4216643, **mort) == [2, 3]


def test_in_hospital_death_via_both_sources_is_excluded_once():
    c = Cdm()
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-05", disch=4216643).death(1, "2021-01-05")
    c.person(2).visit(20, 2, "2021-01-01", "2021-01-05", disch=8536)
    kw = dict(target_outcome="all_cause_readmission", **SIMPLE)
    assert c.ids(**kw) == [2]
    assert c.ids(death_sources=["death_table", "discharge_disposition"], **kw) == [2]


def test_death_sources_decide_where_a_patients_death_date_comes_from():
    c = Cdm()
    # person 1: table death in the stay; person 2: a prior visit discharged dead, then a later stay (post-death activity)
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-05").death(1, "2021-01-03")
    c.person(2).visit(19, 2, "2020-12-01", "2020-12-05", concept=9202, disch=4216643)
    c.visit(20, 2, "2021-01-01", "2021-01-05")
    kw = dict(target_outcome="all_cause_readmission", **SIMPLE)
    assert c.ids(**kw) == [2]                                                       # table only: person 2's stay is kept
    assert c.ids(death_sources="death_table", **kw) == [2]
    assert c.ids(death_sources="discharge_disposition", **kw) == [1]                # table ignored, disposition propagated
    assert c.ids(death_sources=["death_table", "discharge_disposition"], **kw) == []


def test_verified_follow_up_boundaries_and_evidence_types():
    f = "2021-02-04"  # discharge 2021-01-05 + 30 days
    c = Cdm()
    for pid in range(1, 12):
        c.person(pid).visit(pid * 10, pid, "2021-01-01", "2021-01-05")
    c.visit(101, 1, f, f, concept=9202)                                  # 1: later visit exactly at discharge + 30
    c.visit(201, 2, day(f, -1), day(f, -1), concept=9202)                # 2: one day too early
    c.visit(301, 3, "2021-01-20", "2021-01-22")                          # 3: readmitted inside the window, nothing later
    c.measurement(4, f)                                                  # 4: measurement at the boundary
    c.condition(5, f)                                                    # 5: condition at the boundary
    c.drug(6, f)                                                         # 6: drug exposure at the boundary
    c.obs(7, "2020-01-01", f)                                            # 7: only an observation period reaching the boundary
    c.death(8, day(f, 6))                                                # 8: died after the window, no other record
    c.measurement(9, day(f, -1))                                         # 9: measurement one day early
    # 10: no records at all; 11: observation period one day short
    c.obs(11, "2020-01-01", day(f, -1))
    kw = dict(washin_days=0, target_outcome="all_cause_readmission", sampling_rule="first", min_los_days=1)
    kept = c.define(**kw)
    assert list(kept.subject_id) == [1, 3, 4, 5, 6]
    assert dict(zip(kept.subject_id, kept.outcome_flag)) == {1: 0, 3: 1, 4: 0, 5: 0, 6: 0}
    assert kept.followup_verified.eq(1).all()
    # switched off: everyone stays, followup_verified tells who would have been dropped
    everyone = c.define(require_verified_followup=False, **kw)
    verified = dict(zip(everyone.subject_id, everyone.followup_verified))
    assert [p for p, v in verified.items() if v == 1] == [1, 3, 4, 5, 6]
    assert [p for p, v in verified.items() if v == 0] == [2, 7, 8, 9, 10, 11]
    # evidence sources are selectable: an observation period and a death both verify when allowed
    assert c.ids(followup_evidence="observation_period", **kw) == [3, 7]
    assert c.ids(followup_evidence=["death"], death_sources=["death_table"], **kw) == [3, 8]
    assert c.ids(followup_evidence=["observation_period", "visit", "measurement", "condition", "drug", "death"], **kw) == [
        1, 3, 4, 5, 6, 7, 8]


def test_verified_follow_up_is_anchored_on_the_index_start_for_fixed_window_mortality():
    c = Cdm()
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-20")
    c.person(2).visit(20, 2, "2021-01-01", "2021-01-20")
    c.measurement(1, "2021-06-30")  # 180 days after the index start
    c.measurement(2, "2021-06-29")
    kw = dict(target_outcome="mortality", mortality_type="fixed_window", followup_days=180, washin_days=0)
    assert c.ids(**kw) == [1]
    assert c.ids(require_verified_followup=False, **kw) == [1, 2]


def test_sampling_first_last_and_random_with_a_fixed_seed():
    c = Cdm()
    c.person(1)
    starts = ["2021-01-10", "2021-04-10", "2021-08-10", "2021-11-10"]
    for i, s in enumerate(starts):
        c.visit(100 + i, 1, s, day(s, 4))
    c.visit(900, 1, "2022-06-01", "2022-06-01", concept=9202)  # later evidence so that all stays are verified
    kw = dict(target_outcome="none", **{**SIMPLE, "require_verified_followup": True})
    assert c.define(sampling_rule="first", **kw).visit_occurrence_id.tolist() == [100]
    assert c.define(sampling_rule="last", **kw).visit_occurrence_id.tolist() == [103]

    def expected_random(seed):
        rows = [(c.con.execute(f"SELECT hash(CAST({v} AS INTEGER), {seed})").fetchone()[0], v) for v in (100, 101, 102, 103)]
        return min(rows)[1]

    picks = set()
    for seed in range(1, 25):
        a = c.define(sampling_rule="random", random_state=seed, **kw).visit_occurrence_id.tolist()
        b = c.define(sampling_rule="random", random_state=seed, **kw).visit_occurrence_id.tolist()
        assert a == b == [expected_random(seed)]  # reproducible, and exactly the documented argmin
        picks.add(a[0])
    assert len(picks) > 1  # the seed matters
    assert c.define(sampling_rule=" Random ", **kw).visit_occurrence_id.tolist() == [expected_random(42)]


def test_visit_types_inpatient_emergency_outpatient_custom_and_any():
    c = Cdm()
    for pid, concept in [(1, 9201), (2, 9203), (3, 9202), (4, 9999), (5, 262)]:
        c.person(pid).visit(pid * 10, pid, "2021-01-01", "2021-01-03", concept=concept)
    kw = dict(target_outcome="none", **SIMPLE)
    assert c.ids(visit_type="inpatient", **kw) == [1]
    assert c.ids(visit_type="emergency", **kw) == [2]
    assert c.ids(visit_type="outpatient", **kw) == [3]
    assert c.ids(visit_type=["inpatient", "emergency"], **kw) == [1, 2]
    assert c.ids(visit_type=[9201, 9202], **kw) == [1, 3]
    assert c.ids(visit_type=9999, **kw) == [4]
    assert c.ids(visit_type=["emergency", 262, "9999"], **kw) == [2, 4, 5]
    assert c.ids(visit_type=" Inpatient ", **kw) == [1]
    assert c.ids(visit_type=None, **kw) == [1, 2, 3, 4, 5]
    assert c.define(visit_type="emergency", **kw).visit_concept_id.tolist() == [9203]


def test_standard_visit_type_concept_ids_are_the_documented_ones():
    from omop_etl.cohort import _VISIT_TYPE_CONCEPTS

    assert _VISIT_TYPE_CONCEPTS == {"inpatient": (9201,), "emergency": (9203,), "outpatient": (9202,)}


@pytest.mark.skipif(not os.environ.get("OMOP_VOCAB_DB"), reason="set OMOP_VOCAB_DB to an Athena-loaded DuckDB file")
def test_visit_type_concepts_against_a_real_vocabulary():
    from omop_etl.cohort import _VISIT_TYPE_CONCEPTS

    con = duckdb.connect(os.environ["OMOP_VOCAB_DB"], read_only=True)
    try:
        names = {9201: "Inpatient Visit", 9202: "Outpatient Visit", 9203: "Emergency Room Visit"}
        for label, ids in _VISIT_TYPE_CONCEPTS.items():
            for cid in ids:
                row = con.execute(
                    "SELECT concept_name, domain_id, standard_concept FROM concept WHERE concept_id = ?", [cid]).fetchone()
                assert row == (names[cid], "Visit", "S"), (label, cid, row)
        assert con.execute("SELECT COUNT(*) FROM concept WHERE concept_id = 4216643").fetchone()[0] == 1
    finally:
        con.close()


def test_cohort_only_template_leaves_outcome_columns_null():
    c = Cdm()
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-05").visit(11, 1, "2021-01-20", "2021-01-22")
    c.person(2).visit(20, 2, "2021-01-01", "2021-01-05").visit(21, 2, "2021-03-01", "2021-03-01", concept=9202)
    df = c.define(target_outcome="none", washin_days=0)
    assert df.outcome_flag.isna().all() and df.outcome_date.isna().all()
    assert df.target_outcome.eq("none").all()
    # verification is still on by default: person 1 has a later inpatient stay (>= discharge + 30? no) and person 2 a visit
    assert list(df.subject_id) == [2]
    assert list(df.followup_verified) == [1]
    assert c.ids(target_outcome="none", washin_days=0, require_verified_followup=False) == [1, 2]
    assert c.ids(target_outcome="none", washin_days=0, followup_days=None, require_verified_followup=False) == [1, 2]
    with pytest.raises(ValueError, match="outcome_cohort_id cannot be used"):
        c.define(target_outcome="none", outcome_cohort_id=2)


# ---- mortality paradigms and the composite -----------------------------------------------------------

def test_post_discharge_mortality_window_boundaries():
    e = "2021-01-05"
    c = Cdm()
    for pid in range(1, 8):
        c.person(pid).visit(pid * 10, pid, "2021-01-01", e)
    c.death(1, day(e, 30))   # last day of the window: inside
    c.death(2, day(e, 31))   # one day later: outside, but verified by the death itself
    c.death(3, day(e, 1))    # first day: inside
    c.death(4, e)            # on the discharge day: an in-hospital death, excluded from the index stays
    c.death(5, day(e, 10))
    c.visit(61, 6, day(e, 40), day(e, 40), concept=9202)   # survivor with verified follow-up
    kw = dict(target_outcome="mortality", mortality_type="post_discharge", washin_days=0)
    df = c.define(**kw)
    assert dict(zip(df.subject_id, df.outcome_flag)) == {1: 1, 2: 0, 3: 1, 5: 1, 6: 0}
    assert _dates(df.outcome_date) == [day(e, 30), None, day(e, 1), day(e, 10), None]
    gap = c.define(gap_days=5, require_verified_followup=False, **kw)  # window (e+5, e+30]
    assert dict(zip(gap.subject_id, gap.outcome_flag)) == {1: 1, 2: 0, 3: 0, 5: 1, 6: 0, 7: 0}
    assert c.define(gap_days=5, **kw).followup_verified.eq(1).all()
    with pytest.raises(ValueError, match="exclude_in_hospital_death=True"):
        c.define(exclude_in_hospital_death=False, **kw)


def test_in_hospital_mortality_via_both_sources_and_the_disposition_override():
    c = Cdm()
    c.person(1).visit(10, 1, "2021-05-01", "2021-05-05", disch=4216643)
    c.person(2).visit(20, 2, "2021-05-01", "2021-05-06").death(2, "2021-05-06")
    c.person(3).visit(30, 3, "2021-05-01", "2021-05-04", disch=8536)
    c.person(4).visit(40, 4, "2021-05-01", "2021-05-01", disch=4216643)          # same-day death: min LOS auto is 0
    c.person(5).visit(50, 5, "2021-05-01", "2021-05-03", disch=4155309)          # legacy code, see docstring
    kw = dict(target_outcome="mortality", mortality_type="in_hospital", washin_days=0)
    df = c.define(**kw)
    assert dict(zip(df.subject_id, df.outcome_flag)) == {1: 1, 2: 1, 3: 0, 4: 1, 5: 1}
    assert _dates(df.outcome_date) == ["2021-05-05", "2021-05-06", None, "2021-05-01", "2021-05-03"]
    assert dict(zip(df.subject_id, df.followup_verified.isna())) == {i: True for i in range(1, 6)}
    narrow = c.define(death_discharge_concept_ids=4216643, **kw)
    assert dict(zip(narrow.subject_id, narrow.outcome_flag)) == {1: 1, 2: 1, 3: 0, 4: 1, 5: 0}
    assert c.ids(min_los_days=1, **kw) == [1, 2, 3, 5]
    with pytest.raises(ValueError, match="in-hospital mortality"):
        c.define(exclude_in_hospital_death=True, **kw)
    with pytest.raises(ValueError, match="gap_days has no effect"):
        c.define(gap_days=3, **kw)


def test_fixed_window_mortality_is_measured_from_the_index_start():
    s = "2021-01-01"
    c = Cdm()
    for pid in range(1, 7):
        c.person(pid).visit(pid * 10, pid, s, day(s, 3))
    c.death(1, day(s, 30))    # on the gap boundary: outside (s + 30, s + 180]
    c.death(2, day(s, 31))    # first day inside
    c.death(3, day(s, 180))   # last day inside
    c.death(4, day(s, 181))   # first day beyond
    c.death(5, day(s, 2))     # during the index stay: allowed here, and inside the gap
    kw = dict(target_outcome="mortality", mortality_type="fixed_window", followup_days=180, gap_days=30,
              washin_days=0, require_verified_followup=False)
    df = c.define(**kw)
    assert dict(zip(df.subject_id, df.outcome_flag)) == {1: 0, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0}
    assert dict(zip(df.subject_id, df.los_days)) == {i: 3 for i in range(1, 7)}
    assert c.ids(exclude_in_hospital_death=True, **kw) == [1, 2, 3, 4, 6]  # an explicit opt-in drops the in-stay death


def test_readmission_or_death_reports_the_first_event():
    e = "2021-05-05"
    c = Cdm()
    for pid in range(1, 5):
        c.person(pid).visit(pid * 10, pid, "2021-05-01", e)
    c.visit(11, 1, day(e, 10), day(e, 13))                 # 1: readmission only
    c.death(2, day(e, 12))                                   # 2: death only
    c.visit(31, 3, day(e, 10), day(e, 13)).death(3, day(e, 20))   # 3: readmission first
    c.visit(41, 4, day(e, 45), day(e, 45), concept=9202)     # 4: verified survivor
    df = c.define(target_outcome="readmission_or_death", washin_days=0, sampling_rule="first")
    assert dict(zip(df.subject_id, df.outcome_flag)) == {1: 1, 2: 1, 3: 1, 4: 0}
    assert _dates(df.outcome_date) == [day(e, 10), day(e, 12), day(e, 10), None]
    assert df.target_outcome.eq("readmission_or_death").all()
    alias = c.define(target_outcome="composite_readmit_or_death", washin_days=0, sampling_rule="first")
    pd.testing.assert_frame_equal(df, alias)
    via_mortality = c.define(target_outcome="mortality", mortality_type="composite_readmit_or_death", washin_days=0,
                             sampling_rule="first")
    pd.testing.assert_frame_equal(df, via_mortality)


def test_overlapping_adjacent_and_same_day_transfer_stays():
    c = Cdm()
    c.person(1)
    c.visit(1, 1, "2021-01-01", "2021-01-10")   # A
    c.visit(2, 1, "2021-01-10", "2021-01-15")   # B: same-day transfer, starts on A's discharge day
    c.visit(3, 1, "2021-01-16", "2021-01-20")   # C: adjacent, starts the day after B's discharge
    c.visit(4, 1, "2021-01-18", "2021-01-25")   # D: overlaps C
    c.person(2).visit(21, 2, "2021-03-01", "2021-03-05").visit(22, 2, "2021-03-06", "2021-03-07")  # adjacent pair
    kw = dict(target_outcome="all_cause_readmission", require_verified_followup=False, washin_days=0)

    first = c.define(sampling_rule="first", **kw)
    # A's readmission window (Jan 10, Feb 9] holds B? no: B starts ON the discharge day, so it is not a readmission;
    # C (Jan 16) and D (Jan 18) are
    assert first.visit_occurrence_id.tolist()[0] == 1 and first.outcome_flag.tolist()[0] == 1
    assert _dates(first.outcome_date)[0] == "2021-01-16"
    assert _dates(c.define(sampling_rule="first", gap_days=6, **kw).outcome_date)[0] == "2021-01-18"
    assert c.define(sampling_rule="first", gap_days=6, **kw).outcome_flag.tolist()[0] == 1
    assert c.define(sampling_rule="first", gap_days=6, followup_days=7, **kw).outcome_flag.tolist()[0] == 0  # (Jan 16, Jan 17]
    # a same-day transfer is never a readmission, even with no gap
    only_transfer = Cdm()
    only_transfer.person(1).visit(1, 1, "2021-01-01", "2021-01-05").visit(2, 1, "2021-01-05", "2021-01-09")
    assert only_transfer.define(sampling_rule="first", **kw).outcome_flag.tolist() == [0]
    assert only_transfer.define(sampling_rule="last", **kw).outcome_flag.tolist() == [0]
    # an overlapping stay that starts inside the index stay is not a readmission either
    overlap = Cdm()
    overlap.person(1).visit(1, 1, "2021-01-01", "2021-01-10").visit(2, 1, "2021-01-05", "2021-01-12")
    assert overlap.define(sampling_rule="first", **kw).outcome_flag.tolist() == [0]
    # adjacent stays: the day after discharge counts with no gap, not with a one-day gap
    adj = c.define(sampling_rule="first", **kw)
    assert adj[adj.subject_id == 2].outcome_flag.tolist() == [1]
    one_day_gap = c.define(sampling_rule="first", gap_days=1, **kw)
    assert one_day_gap[one_day_gap.subject_id == 2].outcome_flag.tolist() == [0]
    # sampling among overlapping stays: last picks the latest start (D), then the next-latest id breaks ties
    last = c.define(sampling_rule="last", **kw)
    assert last.visit_occurrence_id.tolist()[0] == 4


# ======================================================================================================
# 5. materialisation, read-only connections, idempotence
# ======================================================================================================

def _cohort_rows(con, cohort_id):
    return con.execute(
        f"SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = {cohort_id} "
        "ORDER BY subject_id, cohort_start_date").fetchall()


def _two_patient_cdm(path=None):
    c = Cdm(path)
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-05").visit(11, 1, "2021-01-20", "2021-01-23")      # readmitted
    c.person(2).visit(20, 2, "2021-01-01", "2021-01-04").visit(21, 2, "2021-03-01", "2021-03-01", concept=9202)
    return c


def test_materialise_true_writes_cohort_definition_and_outcome_cohorts():
    c = _two_patient_cdm()
    df = c.define(materialise=True, washin_days=0, cohort_definition_id=7, outcome_cohort_id=8, sampling_rule="first",
                  cohort_name="It's a study", attrition=True)
    assert df.subject_id.tolist() == [1, 2] and df.outcome_flag.tolist() == [1, 0]
    assert _cohort_rows(c.con, 7) == [(1, dt.date(2021, 1, 1), dt.date(2021, 1, 5)), (2, dt.date(2021, 1, 1), dt.date(2021, 1, 4))]
    # the readmission outcome cohort holds the readmission visit itself
    assert _cohort_rows(c.con, 8) == [(1, dt.date(2021, 1, 20), dt.date(2021, 1, 23))]
    defs = c.con.execute("SELECT cohort_definition_id, cohort_definition_name, cohort_definition_description "
                         "FROM cohort_definition ORDER BY 1").fetchall()
    assert [d[0] for d in defs] == [7, 8]
    assert defs[0][1] == "It's a study" and defs[1][1] == "It's a study - Outcome (all_cause_readmission)"
    assert "outcome all_cause_readmission" in defs[0][2] and "sampling first" in defs[0][2]
    assert int(df.attrs["summary"].total_subjects.iloc[0]) == 2
    assert df.attrs["attrition"].subjects_retained.tolist()[-1] == 2


def test_mortality_outcome_cohort_is_a_point_event_on_the_outcome_date():
    c = Cdm()
    c.person(1).visit(10, 1, "2021-01-01", "2021-01-05").death(1, "2021-01-20")
    c.person(2).visit(20, 2, "2021-01-01", "2021-01-05").measurement(2, "2021-03-01")
    c.define(materialise=True, target_outcome="mortality", washin_days=0, cohort_definition_id=1, outcome_cohort_id=2)
    assert _cohort_rows(c.con, 2) == [(1, dt.date(2021, 1, 20), dt.date(2021, 1, 20))]


def test_materialise_false_does_not_touch_the_cohort_tables():
    c = _two_patient_cdm()
    c.define(materialise=False, washin_days=0, cohort_definition_id=7, outcome_cohort_id=8)
    assert c.con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0] == 0
    assert c.con.execute("SELECT COUNT(*) FROM cohort_definition").fetchone()[0] == 0
    df = c.define(materialise=False, washin_days=0)
    assert "summary" not in df.attrs  # no cohort rows to summarise
    # no temp tables left behind, including by the attrition pass
    c.define(materialise=False, washin_days=0, attrition=True)
    leftovers = c.con.execute("SELECT table_name FROM duckdb_tables() WHERE temporary").fetchall()
    assert leftovers == []


def test_rerunning_the_same_cohort_id_replaces_it_cleanly():
    c = _two_patient_cdm()
    kw = dict(materialise=True, washin_days=0, cohort_definition_id=7, outcome_cohort_id=8, sampling_rule="first")
    c.define(**kw)
    c.define(**kw)
    assert len(_cohort_rows(c.con, 7)) == 2 and len(_cohort_rows(c.con, 8)) == 1
    assert c.con.execute("SELECT COUNT(*) FROM cohort_definition WHERE cohort_definition_id IN (7, 8)").fetchone()[0] == 2
    # a different definition under the same id fully replaces the old rows
    c.define(materialise=True, washin_days=0, cohort_definition_id=7, outcome_cohort_id=8, sampling_rule="first",
             visit_type="outpatient", target_outcome="mortality", min_los_days=0, require_verified_followup=False)
    rows = _cohort_rows(c.con, 7)
    assert [r[0] for r in rows] == [2] and rows[0][1] == dt.date(2021, 3, 1)
    assert _cohort_rows(c.con, 8) == []  # no deaths: the old readmission outcome rows are gone, not left behind
    assert c.con.execute("SELECT COUNT(*) FROM cohort_definition WHERE cohort_definition_id = 7").fetchone()[0] == 1
    # other cohort ids are untouched
    c.define(materialise=True, washin_days=0, cohort_definition_id=3)
    c.define(materialise=True, washin_days=0, cohort_definition_id=7)
    assert len(_cohort_rows(c.con, 3)) == 2


def test_table_name_persists_the_full_result(tmp_path):
    c = _two_patient_cdm()
    df = c.define(materialise=False, washin_days=0, table_name="main.my_index_cohort")
    saved = c.con.execute("SELECT * FROM my_index_cohort ORDER BY subject_id").df()
    assert list(saved.columns) == EXPECTED_COLUMNS
    assert saved.subject_id.tolist() == df.subject_id.tolist()
    c.define(materialise=False, washin_days=0, table_name="my_index_cohort", sampling_rule="last")  # replaces
    assert c.con.execute("SELECT COUNT(*) FROM my_index_cohort").fetchone()[0] == 2


def test_read_only_connection_supports_materialise_false_but_not_writing(tmp_path):
    path = tmp_path / "ro.duckdb"
    c = _two_patient_cdm(path)
    c.con.close()
    ro = duckdb.connect(str(path), read_only=True)
    try:
        df = define_study_cohort(ro, materialise=False, washin_days=0, sampling_rule="first")
        assert df.subject_id.tolist() == [1, 2] and df.outcome_flag.tolist() == [1, 0]
        assert df.attrs["attrition"].subjects_retained.tolist()[-1] == 2   # attrition also works read-only
        with pytest.raises(RuntimeError, match="read-only"):
            define_study_cohort(ro, materialise=True, washin_days=0)
        with pytest.raises(RuntimeError, match="read-only"):
            define_study_cohort(ro, materialise=False, washin_days=0, table_name="persisted")
        # the failed attempts left nothing behind
        assert ro.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE temporary").fetchone()[0] == 0
    finally:
        ro.close()
    rw = duckdb.connect(str(path))
    try:
        assert rw.execute("SELECT COUNT(*) FROM cohort").fetchone()[0] == 0
    finally:
        rw.close()


def test_default_materialise_auto_writes_when_writable_and_warns_when_read_only(tmp_path, recwarn):
    path = tmp_path / "auto.duckdb"
    c = _two_patient_cdm(path)
    df = define_study_cohort(c.con, washin_days=0, sampling_rule="first", cohort_definition_id=4)
    assert len(_cohort_rows(c.con, 4)) == 2 and "summary" in df.attrs   # writable: materialised like the builders
    assert not [w for w in recwarn.list if issubclass(w.category, UserWarning)]
    c.con.close()
    ro = duckdb.connect(str(path), read_only=True)
    try:
        with pytest.warns(UserWarning, match="read-only"):
            res = define_study_cohort(ro, washin_days=0, sampling_rule="first", cohort_definition_id=4)
        assert res.subject_id.tolist() == [1, 2] and "summary" not in res.attrs
        with pytest.warns(UserWarning, match="read-only"):   # the RFC example needs no change on a read-only connection
            define_study_cohort(ro, **RFC_KWARGS)
        with pytest.raises(ValueError, match="materialise must be"):
            define_study_cohort(ro, materialise="maybe")
    finally:
        ro.close()


def test_schema_argument_qualifies_the_cdm_tables():
    c = _two_patient_cdm()
    a = c.define(washin_days=0, schema="main", sampling_rule="first")
    b = c.define(washin_days=0, sampling_rule="first")
    pd.testing.assert_frame_equal(a, b)
    with pytest.raises(duckdb.Error):
        c.define(washin_days=0, schema="no_such_schema")


# ======================================================================================================
# 6. attrition and metadata
# ======================================================================================================

def test_attrition_steps_follow_the_active_criteria_and_end_at_the_cohort_size(cdm):
    df = define_study_cohort(cdm, materialise=False, visit_type=["inpatient", "emergency"], study_window=(None, "2019-12-31"),
                             min_age=None, min_los_days=None, washin_days=0, target_outcome="mortality",
                             mortality_type="post_discharge", cohort_definition_id=5)
    steps = df.attrs["attrition"]
    assert steps.step_name.tolist() == [
        "Index visit type in (9201, 9203)",
        "Index visit start on or before 2019-12-31",
        "No in-hospital death at index stay",
        "Verified follow-up (evidence >= 30 days after discharge)",
    ]
    assert steps.subjects_retained.iloc[-1] == len(df)
    assert steps.subjects_retained.is_monotonic_decreasing
    none = define_study_cohort(cdm, materialise=False, target_outcome="mortality", mortality_type="in_hospital",
                               attrition=False)
    assert "attrition" not in none.attrs


def test_definition_attribute_records_the_resolved_settings():
    c = _two_patient_cdm()
    mort = c.define(target_outcome="mortality", mortality_type="fixed_window", washin_days=0)
    d = mort.attrs["definition"]
    assert d["target_outcome"] == "mortality_fixed_window"
    assert d["min_los_days"] is None and d["exclude_in_hospital_death"] is False       # "auto" resolved
    assert d["death_discharge_concept_ids"] == [4216643, 4155309]                       # end-of-life convention
    assert d["death_sources"] == ["death_table", "discharge_disposition"]
    assert d["followup_evidence"] == ["observation_period", "visit", "measurement", "condition", "drug", "death"]
    readm = c.define(washin_days=0).attrs["definition"]
    assert readm["death_discharge_concept_ids"] == [4216643] and readm["death_sources"] == ["death_table"]
    assert readm["followup_evidence"] == ["visit", "measurement", "condition", "drug"]
    assert readm["min_los_days"] == 1 and readm["exclude_in_hospital_death"] is True
    assert readm["visit_concept_ids"] == [9201] and readm["outcome_visit_concept_ids"] == [9201]


def test_all_cause_readmission_alias_must_agree_with_followup_days():
    c = _two_patient_cdm()
    a = c.define(target_outcome="all_cause_readmission_30d", washin_days=0)
    b = c.define(target_outcome="all_cause_readmission", washin_days=0)
    pd.testing.assert_frame_equal(a, b)
    c.define(target_outcome="all_cause_readmission_45d", followup_days=45, washin_days=0)
    with pytest.raises(ValueError, match="implies a 45-day window"):
        c.define(target_outcome="all_cause_readmission_45d", followup_days=30)


# ======================================================================================================
# 7. input validation and SQL safety
# ======================================================================================================

VALIDATION_CASES = [
    (dict(visit_type="surgery"), "Unknown visit_type 'surgery'"),
    (dict(visit_type=["inpatient", "daycase"]), "Unknown visit_type 'daycase'"),
    (dict(visit_type=[]), "must not be empty"),
    (dict(visit_type=[9201.5]), "whole number"),
    (dict(visit_type=[True]), "whole number"),
    (dict(outcome_visit_type=None, target_outcome="none", followup_days=None, require_verified_followup=False), None),
    (dict(outcome_visit_type="clinic"), "Unknown outcome_visit_type 'clinic'"),
    (dict(study_window="2017-01-01"), "study_window must be None or a"),
    (dict(study_window=("2017-01-01",)), "study_window must be None or a"),
    (dict(study_window=("2017-01-01", "2018-01-01", "2019-01-01")), "study_window must be None or a"),
    (dict(study_window=("2021-01-01", "2020-12-31")), "must not be after"),
    (dict(study_window=("2017-13-01", None)), "valid date"),
    (dict(study_window=("2017-02-30", None)), "valid date"),
    (dict(study_window=("01/02/2017", None)), "valid date"),
    (dict(study_window=(20170101, None)), "valid date"),
    (dict(min_age=-1), "min_age must be >= 0"),
    (dict(min_age=17.5), "min_age must be a whole number"),
    (dict(min_age="18"), "min_age must be a whole number"),
    (dict(min_los_days=-1), "min_los_days must be >= 0"),
    (dict(min_los_days="two"), "min_los_days must be a whole number"),
    (dict(washin_days=-365), "washin_days must be >= 0"),
    (dict(followup_days=0), "followup_days must be >= 1"),
    (dict(followup_days=-30), "followup_days must be >= 1"),
    (dict(followup_days=None), "followup_days is required"),
    (dict(gap_days=-1), "gap_days must be >= 0"),
    (dict(gap_days=30), "Inconsistent windows"),
    (dict(gap_days=45), "Inconsistent windows"),
    (dict(sampling_rule="median"), "Unsupported sampling_rule 'median'"),
    (dict(target_outcome="readmit"), "Unsupported target_outcome 'readmit'"),
    (dict(target_outcome=None), "target_outcome must be a string"),
    (dict(target_outcome="mortality", mortality_type="eventual"), "Unsupported mortality_type 'eventual'"),
    (dict(age_method="exact"), "Unsupported age_method 'exact'"),
    (dict(exclude_in_hospital_death="yes"), "exclude_in_hospital_death must be True or False"),
    (dict(require_verified_followup="yes"), "require_verified_followup must be True or False"),
    (dict(materialise=1), "materialise must be True, False or 'auto'"),
    (dict(materialise="yes"), "materialise must be True, False or 'auto'"),
    (dict(random_state=1.5), "random_state must be a whole number"),
    (dict(cohort_definition_id=None), "must not be None"),
    (dict(cohort_definition_id=3, outcome_cohort_id=3), "must differ from cohort_definition_id"),
    (dict(death_sources="graveyard"), "death_sources must be a non-empty subset"),
    (dict(death_sources=[]), "death_sources must be a non-empty subset"),
    (dict(followup_evidence=["visit", "rumour"]), "followup_evidence must be a non-empty subset"),
    (dict(death_discharge_concept_ids=[]), "must not be empty"),
    (dict(death_discharge_concept_ids=["x"]), "whole number"),
    (dict(schema="main; DROP TABLE person"), "Invalid schema"),
    (dict(schema="a.b.c"), "Invalid schema"),
    (dict(schema=""), "schema must be a non-empty string"),
    (dict(table_name="t; DROP TABLE person"), "Invalid table_name"),
    (dict(table_name="x y"), "Invalid table_name"),
    (dict(target_outcome="mortality", mortality_type="post_discharge", exclude_in_hospital_death=False), "exclude_in_hospital_death=True"),
    (dict(target_outcome="readmission_or_death", exclude_in_hospital_death=False), "exclude_in_hospital_death=True"),
    (dict(target_outcome="mortality", mortality_type="in_hospital", exclude_in_hospital_death=True), "in-hospital mortality"),
    (dict(target_outcome="none", gap_days=2), "gap_days has no effect"),
    (dict(target_outcome="none", followup_days=None), "needs followup_days"),
    (dict(target_outcome="none", outcome_cohort_id=2), "outcome_cohort_id cannot be used"),
]


@pytest.mark.parametrize("kwargs,message", VALIDATION_CASES,
                         ids=[f"{i:02d}" for i in range(len(VALIDATION_CASES))])
def test_invalid_inputs_raise_clear_errors_and_change_nothing(kwargs, message):
    c = _two_patient_cdm()
    if message is None:  # the one valid entry: outcome_visit_type=None falls back to inpatient
        c.define(**kwargs)
        return
    with pytest.raises(ValueError, match=message):
        c.define(**{"materialise": True, **kwargs})
    assert c.con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0] == 0
    assert c.con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE temporary").fetchone()[0] == 0


def test_all_arguments_after_the_connection_are_keyword_only():
    c = _two_patient_cdm()
    with pytest.raises(TypeError):
        define_study_cohort(c.con, "inpatient")


def test_hostile_strings_are_never_executed_as_sql():
    c = _two_patient_cdm()
    evil = "x'); DROP TABLE person; --"
    c.define(materialise=True, washin_days=0, cohort_name=evil, cohort_description=evil, outcome_cohort_id=2)
    assert c.con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 2
    name = c.con.execute("SELECT cohort_definition_name FROM cohort_definition WHERE cohort_definition_id = 1").fetchone()[0]
    assert name == evil
    for bad in ("1; DROP TABLE person", "9201) OR (1=1"):
        with pytest.raises(ValueError):
            c.define(visit_type=bad)
    assert c.con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 2


def test_legacy_builders_validate_before_running():
    c = _two_patient_cdm()
    with pytest.raises(ValueError, match="Unsupported index_selection_rule 'sometimes'"):
        build_readmission_cohort(c.con, index_selection_rule="sometimes")
    with pytest.raises(ValueError, match="Unsupported mortality_type 'forever'"):
        build_end_of_life_cohort(c.con, mortality_type="forever")
    with pytest.raises(ValueError, match="Invalid table_name"):
        build_readmission_cohort(c.con, table_name="a b")
    with pytest.raises(ValueError, match="Invalid schema"):
        build_end_of_life_cohort(c.con, schema="main; DROP TABLE person")
    assert c.con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 2


def test_legacy_builders_store_apostrophes_in_cohort_names_verbatim():
    # the pre-refactor end-of-life builder escaped the quotes of the outcome definition's name twice ("O''Brien Outcome")
    c = _two_patient_cdm()
    build_end_of_life_cohort(c.con, cohort_id=1, outcome_cohort_id=2, cohort_name="O'Brien", washin_days=0)
    build_readmission_cohort(c.con, cohort_id=3, outcome_cohort_id=4, cohort_name="Alice's", washin_days=0)
    names = dict(c.con.execute("SELECT cohort_definition_id, cohort_definition_name FROM cohort_definition").fetchall())
    assert names == {1: "O'Brien", 2: "O'Brien Outcome", 3: "Alice's", 4: "Alice's - Readmission Outcome"}


def test_compute_attrition_helper_is_reused_not_reimplemented(cdm):
    df = define_study_cohort(cdm, materialise=False, washin_days=0)
    direct = compute_attrition(cdm, 1, [("everyone", "SELECT person_id AS subject_id FROM person")])
    assert list(df.attrs["attrition"].columns) == list(direct.columns)
