"""SDI / RUCA through extract_benchmark_features: a study's own dated address table, the deterministic tie-break and the
no-ZIP warning. Synthetic data only."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from omop_etl.benchmark_features import extract_benchmark_features  # noqa: E402
from omop_etl.geography import derive_geography_features  # noqa: E402

RUCA = pd.DataFrame({"zipcode": ["19101", "19102", "17001", "99999"], "ruca_code": [1, 2, 4.2, 10]})
SDI = pd.DataFrame({"zcta5_fips": ["19101", "19102", "17001"], "sdi_score": [90.0, 50.0, 10.0]})
CUTS = [20.0, 40.0, 60.0, 80.0]
KW = dict(include_elixhauser=False, include_medications=False, include_utilization=False, include_vitals=False, include_labs=False,
          include_disposition=False, include_demographics=False, vocabulary_db=None)


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id BIGINT, location_id BIGINT)")
    c.execute("INSERT INTO person VALUES (1, NULL), (2, NULL), (3, NULL)")
    c.execute("CREATE TABLE location (location_id BIGINT, zip VARCHAR)")
    c.execute("CREATE TABLE ederri_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR)")
    c.execute("""INSERT INTO ederri_address_history VALUES
        (1, 1, '17001', DATE '2015-01-01', DATE '2019-12-31', 'N'),      -- before the admission
        (1, 2, '19101', DATE '2020-01-01', NULL, 'N'),                   -- in effect at the admission
        (2, 3, '19102', DATE '2020-01-10', DATE '2020-01-20', 'N'),      -- two addresses that tie on every rule key:
        (2, 4, '17001', DATE '2020-01-10', DATE '2020-01-20', 'N'),      --   same start, both in effect, neither preferred
        (3, 5, NULL, DATE '2019-01-01', NULL, 'Y')                       -- an address without a ZIP
    """)
    c.execute("""CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0),
        (2, DATE '2020-01-12', DATE '2020-01-15', 102, 1), (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0))
        t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)""")
    yield c
    c.close()


def _sdoh(con, **extra):
    return extract_benchmark_features(con, "cohort_t", include_sdoh=True, ruca_crosswalk=RUCA, sdi_table=SDI, sdi_cut_points=CUTS, **extra, **KW)


def test_study_address_table_is_used_and_the_address_in_effect_wins(con):
    df = _sdoh(con, address_table="ederri_address_history")
    r1 = df[df.person_id == 1].iloc[0]
    assert r1["ruca_category"] == "RUCA: Metropolitan (1.0)" and r1["sdi_quintile"] == "SDI Quintile 5 (Most Deprived)"   # 19101, not the older 17001
    r3 = df[df.person_id == 3].iloc[0]
    assert r3["sdoh_missing"] == 1 and r3["ruca_category"] == "RUCA: Missing / Unknown"                                  # no ZIP at all
    assert df["sdoh_missing"].tolist() == [0, 0, 1]


def test_ties_are_broken_by_lowest_zip_every_time(con):
    stays = pd.DataFrame({"person_id": [2], "stay_id": [102], "admit_date": ["2020-01-12"]})
    picked = {derive_geography_features(con, RUCA, SDI, CUTS, stays=stays, address_table="ederri_address_history").loc[0, "zip5"] for _ in range(5)}
    assert picked == {"17001"}


def test_no_zip_found_anywhere_warns_with_the_fix(con):
    with pytest.warns(UserWarning, match="address_table"):
        df = _sdoh(con)                                               # 'auto': no person_address_history, empty person.location
    assert (df["sdoh_missing"] == 1).all()
    with warnings.catch_warnings():
        warnings.simplefilter("error")                                # with the study's table there is nothing to warn about
        _sdoh(con, address_table="ederri_address_history")
