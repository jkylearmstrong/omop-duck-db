"""FR-A4: velocity / LACE-proxy / hemodynamic features, pinned to scripts/engineer_clinical_features.py."""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema  # noqa: E402
from omop_etl.velocity import (  # noqa: E402
    NO_PRIOR_VISIT_DAYS, VELOCITY_FEATURES, engineer_clinical_features, extract_velocity_features, site_baseline_risk,
)

FIX = REPO_ROOT / "tests" / "fixtures"


def test_matches_reference_script_golden():
    inp = pd.read_csv(FIX / "velocity_input.csv")
    gold = pd.read_csv(FIX / "velocity_golden.csv")
    got = engineer_clinical_features(inp)
    assert set(gold.columns) <= set(got.columns)
    for col in gold.columns:
        if col == "site_source":  # the reference turns text columns into NaN; the port leaves them alone
            assert got[col].tolist() == inp[col].tolist()
        else:
            np.testing.assert_allclose(got[col].to_numpy(dtype="float64"), gold[col].to_numpy(dtype="float64"),
                                       rtol=1e-12, atol=1e-12, equal_nan=True, err_msg=col)
    assert set(VELOCITY_FEATURES) <= set(got.columns)


def test_matches_reference_script_live():
    pytest.importorskip("sklearn")
    spec = importlib.util.spec_from_file_location("ecf", REPO_ROOT / "scripts" / "engineer_clinical_features.py")
    ecf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ecf)
    inp = pd.read_csv(FIX / "velocity_input.csv")
    ref = ecf.engineer_features(inp)
    got = engineer_clinical_features(inp)
    for col in VELOCITY_FEATURES:
        np.testing.assert_allclose(got[col], ref[col], rtol=1e-12, atol=1e-12, equal_nan=True, err_msg=col)


def test_reference_quirks_are_preserved_and_documented():
    df = pd.DataFrame({"index_los_days": [0, 1, 1.5, 2, 6, 7, 14, np.nan]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = engineer_clinical_features(df)
    # fractional / zero LOS scores 0 (exact-match rule); missing LOS is imputed to 1 -> 1 point
    assert out["lace_los_score"].tolist() == [0, 1, 0, 2, 4, 5, 7, 1]


def test_absent_columns_warn_and_are_default_imputed():
    with pytest.warns(UserWarning, match="all-missing"):
        out = engineer_clinical_features(pd.DataFrame({"prior_ip_count": [2]}))
    assert out.loc[0, "shock_index"] == pytest.approx(75 / 120)      # defaults 75 bpm / 120 mmHg
    assert out.loc[0, "bun_to_creatinine"] == pytest.approx(15.0)    # BUN 15 / creatinine 1.0
    assert out.loc[0, "ip_velocity"] == pytest.approx(2 / (365 / 30 + 1))


def test_site_baseline_risk_uses_training_rows_only():
    df = pd.DataFrame({"site_source": list("AAAABBBB"), "readmitted_30d": [1, 1, 0, 0, 0, 0, 0, 1]})
    train = np.array([True, True, True, False, True, True, False, False])
    r = site_baseline_risk(df, train_mask=train)
    assert r.iloc[0] == pytest.approx(2 / 3)          # site A from its 3 training rows, not the validation row
    assert r.iloc[7] == pytest.approx(0.0)            # the validation row's own label (1) is not used
    unseen = pd.DataFrame({"site_source": ["A", "Z"], "readmitted_30d": [1, 0]})
    assert site_baseline_risk(unseen, train_mask=[True, False]).iloc[1] == 1.0  # unseen site -> training-wide rate


# ---- DuckDB extraction -------------------------------------------------------------------------------------------------

def _person(con, pid):
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                f"VALUES ({pid}, 8507, 1960, 8527, 38003564)")


def _visit(con, vid, pid, concept, start, end):
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, {concept}, DATE '{start}', DATE '{end}', 32827)")


def _meas(con, mid, pid, concept, value):
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                f"measurement_type_concept_id, value_as_number) VALUES ({mid}, {pid}, {concept}, DATE '2020-04-10', 44818702, {value})")


@pytest.fixture()
def cdm():
    con = duckdb.connect()
    build_schema(con)
    for pid in (1, 2, 3):
        _person(con, pid)
    # patient 1: 4 prior IP (one a 30-day readmission), 1 prior ED, 1 prior outpatient; emergency index
    _visit(con, 1, 1, 9201, "2019-09-01", "2019-09-05")
    _visit(con, 2, 1, 9201, "2019-09-20", "2019-09-22")
    _visit(con, 3, 1, 9201, "2020-02-01", "2020-02-03")
    _visit(con, 4, 1, 9201, "2020-03-20", "2020-03-25")
    _visit(con, 5, 1, 9203, "2020-02-10", "2020-02-10")
    _visit(con, 6, 1, 9202, "2020-03-01", "2020-03-01")
    _visit(con, 7, 1, 9203, "2020-04-10", "2020-04-12")           # index
    _visit(con, 8, 2, 9201, "2020-04-10", "2020-04-12")           # patient 2: no prior visits
    _visit(con, 9, 3, 9201, "2020-04-10", "2020-04-20")           # patient 3: index + a later visit that must not leak
    _visit(con, 10, 3, 9201, "2020-05-01", "2020-05-02")
    con.execute("CREATE TABLE coh AS SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, "
                "visit_end_date AS cohort_end_date FROM visit_occurrence WHERE visit_occurrence_id IN (7, 8, 9)")
    for mid, pid, c, v in [(1, 1, 3027018, 100), (2, 1, 3004249, 80), (3, 1, 3012888, 50), (4, 1, 3013682, 40),
                           (5, 1, 3016723, 2.0), (6, 2, 3004249, 0)]:
        _meas(con, mid, pid, c, v)
    return con


def test_extract_raw_inputs_and_engineered_values(cdm):
    with pytest.warns(UserWarning, match="default-imputed"):
        f = extract_velocity_features(cdm, "coh").set_index("subject_id")
    p1, p2, p3 = f.loc[1], f.loc[2], f.loc[3]
    assert (p1.prior_ip_count, p1.prior_ed_count, p1.prior_encounters_all_count, p1.prior_ip_os_ed_count) == (4, 1, 6, 6)
    assert p1.prior_ip_los_days == 4 + 2 + 2 + 5 and p1.readmission_count_twelve_months == 1
    assert p1.days_since_prior_encounter == 16 and p1.days_since_prior_ip_ed == 16 and p1.index_los_days == 2
    # engineered with the reference formulas
    assert p1.ip_velocity == pytest.approx(4 / (16 / 30 + 1))
    assert p1.recent_ip_ratio == pytest.approx(4 / 7)
    assert p1.readmission_intensity == pytest.approx(1 / 5)
    assert p1.shock_index == pytest.approx(100 / 80) and p1.bun_to_creatinine == pytest.approx(20.0)
    assert p1.lace_proxy_total == 2 + 1 + 0 + 3
    # no prior visit: NULL days -> sentinel 365 (clipped >= 1), counts 0, no division blow-up
    assert pd.isna(p2.days_since_prior_encounter) and p2.prior_ip_count == 0
    assert p2.ip_velocity == 0 and p2.recent_ip_ratio == 0 and p2.readmission_intensity == 0
    assert p2.utilization_density == 0 and NO_PRIOR_VISIT_DAYS == 365
    assert p2.shock_index == pytest.approx(75 / 60)          # SBP 0 is clipped to 60 by the reference, HR default 75
    # nothing after the index leaks
    assert p3.prior_encounters_all_count == 0 and p3.lace_los_score == 5


def test_extract_engineer_false_and_extra_features(cdm):
    raw = extract_velocity_features(cdm, "coh", engineer=False)
    assert "ip_velocity" not in raw.columns and len(raw) == 3
    extra = pd.DataFrame({"subject_id": [1], "cohort_start_date": [pd.Timestamp("2020-04-10")], "lab_hemoglobin": [7.0],
                          "elix_pre_index_conditions_count": [9]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f = extract_velocity_features(cdm, "coh", extra_features=extra).set_index("subject_id")
    assert f.loc[1, "severe_anemia_flag"] == 1.0 and f.loc[1, "lace_comorb_score"] == 5
    assert f.loc[2, "severe_anemia_flag"] == 0.0
