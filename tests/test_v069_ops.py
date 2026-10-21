"""FR-A4/A5/C1/C2/C3: velocity features, run manifest, DQD date range, temporal split, persisted cohort keys."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema, define_study_cohort, run_dqd  # noqa: E402
from omop_etl.cli import main as cli_main  # noqa: E402
from omop_etl.run_manifest import ManifestVerificationError, RunManifest, verify_manifest  # noqa: E402
from omop_etl.temporal_split import split_by_date  # noqa: E402
from omop_etl.velocity import NO_PRIOR_VISIT_DAYS, extract_velocity_features  # noqa: E402


def _visit(con, vid, pid, concept, start, end):
    con.execute(
        "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
        f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, {concept}, DATE '{start}', DATE '{end}', 32827)")


def _person(con, pid):
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                f"VALUES ({pid}, 8507, 1960, 8527, 38003564)")


def _meas(con, mid, pid, concept, date, value):
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                f"measurement_type_concept_id, value_as_number) VALUES ({mid}, {pid}, {concept}, DATE '{date}', 44818702, {value})")


@pytest.fixture()
def cdm():
    con = duckdb.connect(":memory:")
    build_schema(con)
    return con


# ---- C3: persisted, seeded one-index-per-patient sampler ------------------------------------------------

SIMPLE = dict(washin_days=0, require_verified_followup=False, min_age=None, target_outcome="none", attrition=False,
              materialise=False)


def _multi_stay_cdm(cdm, n_patients=12, history=False):
    vid = 1
    for pid in range(1, n_patients + 1):
        _person(cdm, pid)
        if history:  # wash-in history and later follow-up evidence so every stay qualifies
            _visit(cdm, 1000 + pid, pid, 9202, "2018-01-01", "2018-01-01")
            _visit(cdm, 2000 + pid, pid, 9202, "2021-06-01", "2021-06-01")
        for k in range(4):
            _visit(cdm, vid, pid, 9201, f"2020-0{k + 1}-10", f"2020-0{k + 1}-14")
            vid += 1


def test_persist_keys_are_byte_identical_and_reused(cdm, tmp_path):
    _multi_stay_cdm(cdm)
    keys = tmp_path / "index_stays.csv"
    a = define_study_cohort(cdm, persist_keys=keys, random_state=1, **SIMPLE)
    first = keys.read_bytes()
    assert len(a) == 12 and keys.read_text().splitlines()[0] == "subject_id,visit_occurrence_id"
    # same seed, rerun: identical file
    define_study_cohort(cdm, persist_keys=keys, random_state=1, **SIMPLE)
    assert keys.read_bytes() == first
    # a different seed would resample, but the key file pins the selection
    c = define_study_cohort(cdm, persist_keys=keys, random_state=999, **SIMPLE)
    assert keys.read_bytes() == first
    assert sorted(zip(c.subject_id, c.visit_occurrence_id)) == sorted(zip(a.subject_id, a.visit_occurrence_id))
    # without the file the other seed does select differently
    other = define_study_cohort(cdm, random_state=999, **SIMPLE)
    assert list(other.visit_occurrence_id) != list(a.visit_occurrence_id)


def test_deleting_a_key_row_changes_only_that_patient(cdm, tmp_path):
    _multi_stay_cdm(cdm)
    keys = tmp_path / "k.csv"
    base = define_study_cohort(cdm, persist_keys=keys, random_state=7, **SIMPLE)
    text = keys.read_text().splitlines()
    keys.write_text("\n".join(l for l in text if not l.startswith("5,")) + "\n")
    again = define_study_cohort(cdm, persist_keys=keys, random_state=7, **SIMPLE)
    assert sorted(zip(again.subject_id, again.visit_occurrence_id)) == sorted(zip(base.subject_id, base.visit_occurrence_id))
    keys.write_text("\n".join(l for l in text if not l.startswith("5,")) + "\n")
    changed = define_study_cohort(cdm, persist_keys=keys, random_state=123, **SIMPLE)
    diff = set(zip(changed.subject_id, changed.visit_occurrence_id)) ^ set(zip(base.subject_id, base.visit_occurrence_id))
    assert {s for s, _ in diff} <= {5}
    assert keys.read_text().count("\n") == len(base) + 1  # patient 5 re-added


def test_one_per_patient_false_rejected(cdm):
    with pytest.raises(ValueError, match="one_per_patient"):
        define_study_cohort(cdm, one_per_patient=False, **SIMPLE)


# ---- C1: plausible date range ----------------------------------------------------------------------------

def test_plausible_date_range_flags_1900_and_2100(cdm):
    for pid in (1, 2, 3):
        _person(cdm, pid)
    _visit(cdm, 1, 1, 9201, "2020-01-01", "2020-01-03")
    _visit(cdm, 2, 2, 9201, "1913-05-01", "1913-05-03")
    _visit(cdm, 3, 3, 9201, "2100-01-01", "2100-01-02")
    res = run_dqd(cdm)
    chk = {(r["cdm_table_name"], r["cdm_field_name"]): r for r in res["CheckResults"] if r["check_name"] == "plausibleDateRange"}
    start = chk[("visit_occurrence", "visit_start_date")]
    assert start["failed"] and start["num_violated_rows"] == 2
    assert start["num_before_lower_bound"] == 1 and start["num_after_upper_bound"] == 1
    assert start["num_denominator_rows"] == 3
    # clean data passes, and a permissive range clears the early date
    ok = run_dqd(cdm, plausible_date_range=("1900-01-01", "2200-01-01"))
    assert not [r for r in ok["CheckResults"] if r["check_name"] == "plausibleDateRange" and r["failed"]]
    assert "concept" not in {t for t, _ in chk}  # vocabulary sentinel dates are not screened


# ---- C2: temporal split ------------------------------------------------------------------------------------

def test_split_by_date_union_equals_original(cdm, tmp_path):
    for pid in (1, 2):
        _person(cdm, pid)
    for i, d in enumerate(["2019-06-01", "2021-12-31", "2022-01-01", "2023-05-05"], start=1):
        _visit(cdm, i, 1 + i % 2, 9201, d, d)
        _meas(cdm, i, 1 + i % 2, 3027018, d, 70 + i)
    out = tmp_path / "prospective.duckdb"
    man = RunManifest("split")
    rep = split_by_date(cdm, "2022-01-01", out, manifest=man)
    v = rep["tables"]["visit_occurrence"]
    assert (v["kept"], v["moved"], v["source_before"]) == (2, 2, 4)
    assert v["kept_checksum"] + v["moved_checksum"] == v["source_checksum"]
    assert any(s["step"] == "split:visit_occurrence" and s["n"] == 2 for s in man.attrition)
    held = duckdb.connect(str(out), read_only=True)
    assert held.execute("SELECT min(visit_start_date) FROM visit_occurrence").fetchone()[0] == dt.date(2022, 1, 1)
    assert held.execute("SELECT count(*) FROM person").fetchone()[0] == 2  # self-contained
    with pytest.raises(FileExistsError):
        split_by_date(cdm, "2022-01-01", out)
    # the source is only modified on request
    assert cdm.execute("SELECT count(*) FROM visit_occurrence").fetchone()[0] == 4
    split_by_date(cdm, "2022-01-01", tmp_path / "p2.duckdb", remove_from_source=True)
    assert cdm.execute("SELECT count(*), max(visit_start_date) FROM visit_occurrence").fetchone() == (2, dt.date(2021, 12, 31))
    assert cdm.execute("SELECT count(*) FROM measurement").fetchone()[0] == 2


# ---- A4: velocity features -------------------------------------------------------------------------------------

def test_velocity_features_values_sentinel_and_zero_division(cdm):
    for pid in (1, 2, 3):
        _person(cdm, pid)
    # patient 1: 4 prior IP in a year (one within 90d, gap-readmission pair), 1 prior ED, then emergency index
    _visit(cdm, 1, 1, 9201, "2019-09-01", "2019-09-05")
    _visit(cdm, 2, 1, 9201, "2019-09-20", "2019-09-22")   # within 30d of discharge of visit 1 -> readmission
    _visit(cdm, 3, 1, 9201, "2020-02-01", "2020-02-03")
    _visit(cdm, 4, 1, 9201, "2020-03-20", "2020-03-25")   # within 90d of index
    _visit(cdm, 5, 1, 9203, "2020-02-10", "2020-02-10")
    _visit(cdm, 6, 1, 9203, "2020-04-10", "2020-04-12")   # index: emergency, LOS 2
    # patient 2: no prior visits at all
    _visit(cdm, 7, 2, 9201, "2020-04-10", "2020-04-12")
    # patient 3: index only; plus a visit AFTER the index that must not leak
    _visit(cdm, 8, 3, 9201, "2020-04-10", "2020-04-20")
    _visit(cdm, 9, 3, 9201, "2020-05-01", "2020-05-02")
    cdm.execute("CREATE TABLE coh AS SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, "
                "visit_end_date AS cohort_end_date FROM visit_occurrence WHERE visit_occurrence_id IN (6, 7, 8)")
    for mid, pid, c, v in [(1, 1, 3027018, 100), (2, 1, 3004249, 80), (3, 1, 3013682, 40), (4, 1, 3016723, 2.0),
                           (5, 2, 3027018, 90), (6, 2, 3004249, 0), (7, 2, 3013682, 10), (8, 2, 3016723, 0)]:
        _meas(cdm, mid, pid, c, "2020-04-10", v)
    f = extract_velocity_features(cdm, "coh").set_index("subject_id")
    p1 = f.loc[1]
    assert p1.ip_velocity == pytest.approx(4 * 30 / 365)
    assert p1.ed_velocity == pytest.approx(1 * 30 / 365)
    assert p1.recent_ip_ratio == pytest.approx(2 / 4)  # visits 3 and 4 start within 90 days
    assert p1.readmission_intensity == pytest.approx(1 / 4)
    assert p1.days_since_last_ip == (dt.date(2020, 4, 10) - dt.date(2020, 3, 25)).days
    assert p1.days_since_last_ed == (dt.date(2020, 4, 10) - dt.date(2020, 2, 10)).days
    assert p1.shock_index == pytest.approx(1.25) and p1.bun_to_creatinine == pytest.approx(20.0)
    assert p1.lace_proxy == 2 + 3 + 1  # LOS 2d + emergency + 1 prior ED
    p2 = f.loc[2]  # no prior visits: sentinel + zero-division guards
    assert p2.days_since_last_ip == NO_PRIOR_VISIT_DAYS == p2.days_since_last_ed == 365
    assert p2.ip_velocity == 0 and p2.recent_ip_ratio == 0 and p2.readmission_intensity == 0
    assert pd.isna(p2.shock_index) and pd.isna(p2.bun_to_creatinine)  # SBP = 0 and creatinine = 0
    p3 = f.loc[3]
    assert p3.days_since_last_ip == 365 and p3.ip_velocity == 0  # the later 2020-05 visit does not leak
    assert p3.lace_proxy == 5  # 10-day LOS = 5 points, not emergency, no prior ED


# ---- A5: run manifest -------------------------------------------------------------------------------------------

def test_manifest_records_and_verifies(tmp_path):
    f = tmp_path / "input.csv"
    f.write_text("a,b\n1,2\n")
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    with RunManifest("unit", seeds={"random_state": 42}, inputs=[f], feature_spec=spec, git_dir=REPO_ROOT) as m:
        m.add_step("start", 100)
        m.add_step("kept", 80)
    path = m.write(tmp_path / "run_manifest.json")
    d = json.loads(path.read_text())
    assert d["seeds"] == {"random_state": 42} and d["packages"]["duckdb"] and d["python"] and d["wall_time_seconds"] >= 0
    assert d["inputs"][str(f)] and d["feature_spec"]["sha256"] and [a["n"] for a in d["attrition"]] == [100, 80]
    verify_manifest(path, attrition=[{"step": "start", "n": 100}, {"step": "kept", "n": 80}])
    with pytest.raises(ManifestVerificationError, match="attrition"):
        verify_manifest(path, attrition=[{"step": "start", "n": 100}, {"step": "kept", "n": 79}])
    f.write_text("a,b\n1,3\n")
    with pytest.raises(ManifestVerificationError, match="input.csv"):
        verify_manifest(path)


def test_cli_cohort_manifest_roundtrip_and_verify(cdm, tmp_path, capsys):
    _multi_stay_cdm(cdm, 5, history=True)
    db = tmp_path / "site.duckdb"
    cdm.execute(f"EXPORT DATABASE '{(tmp_path / 'exp').as_posix()}'")
    con = duckdb.connect(str(db))
    con.execute(f"IMPORT DATABASE '{(tmp_path / 'exp').as_posix()}'")
    con.close()
    man = tmp_path / "m.json"
    keys = tmp_path / "keys.csv"
    args = ["cohort", "define", "--db", str(db), "--type", "inpatient", "--out", "my_cohort", "--seed", "3",
            "--persist-keys", str(keys), "--manifest", str(man)]
    assert cli_main(args) == 0
    m1 = json.loads(man.read_text())
    assert m1["seeds"]["random_state"] == 3 and m1["extra"]["out"] == "my_cohort"
    steps = {a["step"]: a["n"] for a in m1["attrition"]}
    assert steps["cohort_rows"] == 5 and steps["source_rows:person"] == 5
    assert cli_main(["verify-manifest", str(man)]) == 0
    first = keys.read_bytes()
    assert cli_main(args[:-2] + ["--verify-manifest", str(man)]) == 0  # inputs unchanged -> runs, same counts
    assert keys.read_bytes() == first
    keys.write_text(keys.read_text() + "999,999" + chr(10))  # tamper with an input recorded in the manifest
    with pytest.raises(ManifestVerificationError):
        cli_main(["verify-manifest", str(man)])
