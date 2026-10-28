"""RUCA / SDI geography features: address-in-effect rule, fixed SDI cut points, ZIP handling, dated address history."""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema, load_location, load_person
from omop_etl.build_omop_cdm import load_macros
from omop_etl.geography import (
    RUCA_MISSING, SDI_LABELS, SDI_MISSING, assign_sdi_quintile, derive_geography_features, fit_sdi_cut_points,
    read_ruca_crosswalk, read_sdi_table, ruca_category, validate_sdi_cut_points,
)

SAMPLE_DIR = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"
RUCA = pd.DataFrame({"ZIPCODE": ["19104", "15213", "08101", "17821", "00501"], "RUCA_CODE": ["1", "2.1", "4", "99", "1"]})
SDI = pd.DataFrame({"ZCTA5_FIPS": ["19104", "15213", "8101", "17821"], "SDI_score": [95, 40, 10, 70]})
CUTS = [20.0, 45.0, 65.0, 85.0]


def _con(addresses, visits, location_zip=None):
    c = duckdb.connect()
    build_schema(c)
    for pid in (1, 2):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, 1980, 0, 0)")
    c.execute("CREATE TABLE person_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, "
              "address_start DATE, address_end DATE, preferred VARCHAR)")
    for row in addresses:
        c.execute("INSERT INTO person_address_history VALUES (?, ?, ?, ?, ?, ?)", row)
    for vid, pid, d in visits:
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                  f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, 9201, DATE '{d}', DATE '{d}', 0)")
    if location_zip:
        c.execute(f"INSERT INTO location (location_id, zip) VALUES (1, '{location_zip}')")
        c.execute("UPDATE person SET location_id = 1 WHERE person_id = 2")
    return c


def test_ruca_category_and_code_99():
    assert ruca_category(1) == "RUCA: Metropolitan (1.0)"
    assert ruca_category("2.1") == "RUCA: Micropolitan (2.0)"
    assert ruca_category(4.2) == "RUCA: Small Town / Rural (3+)"
    assert ruca_category(99) == "RUCA: Small Town / Rural (3+)"          # reference coding
    assert ruca_category(99, code_99_missing=True) == RUCA_MISSING
    assert ruca_category(None) == ruca_category(float("nan")) == ruca_category("x") == ruca_category(0) == RUCA_MISSING


def test_cut_points_are_fixed_not_local():
    scores = np.arange(1, 101)
    fitted = fit_sdi_cut_points(scores)
    assert fitted == pytest.approx([20.8, 40.6, 60.4, 80.2])
    q = assign_sdi_quintile([5, 20.0, 44.9, 45.0, 99, None], CUTS)
    assert list(q[:5]) == [SDI_LABELS[0], SDI_LABELS[1], SDI_LABELS[1], SDI_LABELS[2], SDI_LABELS[4]]
    assert q.iloc[5] == SDI_MISSING
    # same score, same quintile whatever the local distribution looks like
    assert assign_sdi_quintile([52], CUTS).iloc[0] == SDI_LABELS[2]
    for bad in ([1, 2, 3], [1, 1, 2, 3], [4, 3, 2, 1], [1, 2, 3, float("nan")]):
        with pytest.raises(ValueError, match="sdi_cut_points must be four"):
            validate_sdi_cut_points(bad)
    with pytest.raises(ValueError, match="at least 5"):
        fit_sdi_cut_points([1, 2])


def test_readers_normalise_zip_and_headers(tmp_path):
    ruca, sdi = read_ruca_crosswalk(RUCA), read_sdi_table(SDI)
    assert set(sdi["zip5"]) == {"19104", "15213", "08101", "17821"}      # '8101' zero-padded
    assert ruca.set_index("zip5").loc["15213", "ruca_code"] == 2.1
    p = tmp_path / "sdi.csv"
    pd.DataFrame({"zcta5": ["02139"], "sdi": [33]}).to_csv(p, index=False)
    assert read_sdi_table(p).iloc[0].tolist() == ["02139", 33]
    with pytest.raises(ValueError, match="RUCA crosswalk needs"):
        read_ruca_crosswalk(pd.DataFrame({"a": [1]}))


def test_address_in_effect_then_fallbacks():
    addr = [
        (1, 10, "19104", "2010-01-01", "2014-12-31", "N"),     # moved away before the 2016 stay
        (1, 11, "15213", "2015-01-01", None, "N"),              # in effect at 2016 stay
        (1, 12, "08101", "2020-01-01", None, "Y"),              # later address (preferred) must not win
        (2, 13, None, "2000-01-01", None, "Y"),                 # no ZIP never beats one with a ZIP
        (2, 14, "17821", "2030-01-01", None, "N"),              # only address starts after the stay -> earliest_after
    ]
    c = _con(addr, [(1, 1, "2016-06-01"), (2, 1, "2012-06-01"), (3, 1, "2008-01-01"), (4, 2, "2021-03-03")])
    g = derive_geography_features(c, RUCA, SDI, CUTS).set_index("stay_id")
    assert g.loc[1, ["zip5", "rule"]].tolist() == ["15213", "in_effect"]
    assert g.loc[2, ["zip5", "rule"]].tolist() == ["19104", "in_effect"]
    assert g.loc[3, ["zip5", "rule"]].tolist() == ["08101", "earliest_after"]      # preferred 'Y' outranks a nearer start
    assert g.loc[4, ["zip5", "rule"]].tolist() == ["17821", "earliest_after"]
    assert g.loc[1, "ruca_category"] == "RUCA: Micropolitan (2.0)" and g.loc[1, "sdi_quintile"] == SDI_LABELS[1]
    assert g.loc[2, "sdi_quintile"] == SDI_LABELS[4]
    assert g.loc[4, "ruca_category"] == "RUCA: Small Town / Rural (3+)"
    assert g.attrs["sdi_cut_points"] == CUTS
    assert derive_geography_features(c, RUCA, SDI, CUTS, code_99_missing=True).set_index("stay_id").loc[4, "ruca_category"] == RUCA_MISSING


def test_latest_before_and_preference_tiebreak():
    addr = [(1, 1, "19104", "2001-01-01", "2005-01-01", "N"), (1, 2, "15213", "2006-01-01", "2008-01-01", "N"),
            (2, 3, "19104", "2000-01-01", None, "N"), (2, 4, "15213", "2000-01-01", None, "Y")]
    c = _con(addr, [(1, 1, "2012-01-01"), (2, 2, "2012-01-01")])
    g = derive_geography_features(c, RUCA, SDI, CUTS).set_index("stay_id")
    assert g.loc[1, ["zip5", "rule"]].tolist() == ["15213", "latest_before"]
    assert g.loc[2, "zip5"] == "15213"                                  # both in effect, preferred 'Y' wins


def test_missing_zip_and_unmatched_zip_are_unknown():
    c = _con([(1, 1, None, "2000-01-01", None, "Y"), (2, 2, "99999", "2000-01-01", None, "Y")],
             [(1, 1, "2012-01-01"), (2, 2, "2012-01-01")])
    g = derive_geography_features(c, RUCA, SDI, CUTS).set_index("stay_id")
    assert g.loc[1, "rule"] == "no_zip" and pd.isna(g.loc[1, "zip5"])
    assert g.loc[1, "ruca_category"] == RUCA_MISSING and g.loc[1, "sdi_quintile"] == SDI_MISSING
    assert g.loc[2, "zip5"] == "99999" and g.loc[2, "ruca_category"] == RUCA_MISSING and g.loc[2, "sdi_quintile"] == SDI_MISSING


def test_cut_points_required_unless_fitting():
    c = _con([(1, 1, "19104", None, None, "Y")], [(1, 1, "2012-01-01")])
    with pytest.raises(ValueError, match="sdi_cut_points is required"):
        derive_geography_features(c, RUCA, SDI)
    sdi = pd.DataFrame({"zip5": [f"{i:05d}" for i in range(1, 11)], "sdi": range(1, 11)})
    ruca = pd.DataFrame({"zip": [f"{i:05d}" for i in range(1, 11)], "ruca": [1] * 10})
    c = _con([(1, 1, "00001", None, None, "Y")], [])
    stays = pd.DataFrame({"person_id": [1] * 10, "admit_date": ["2012-01-01"] * 10})
    c.execute("DELETE FROM person_address_history")
    for i in range(1, 11):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({100 + i}, 8507, 1980, 0, 0)")
        c.execute("INSERT INTO person_address_history VALUES (?, 1, ?, NULL, NULL, 'Y')", [100 + i, f"{i:05d}"])
    stays = pd.DataFrame({"person_id": range(101, 111), "admit_date": ["2012-01-01"] * 10})
    g = derive_geography_features(c, ruca, sdi, fit_cut_points=True, stays=stays)
    assert g.attrs["sdi_cut_points"] == pytest.approx([2.8, 4.6, 6.4, 8.2])
    assert g["sdi_quintile"].nunique() == 5


def test_falls_back_to_person_location_and_explicit_stays():
    c = _con([], [(1, 2, "2016-01-01")], location_zip="19104-1234")
    g = derive_geography_features(c, RUCA, SDI, CUTS)
    assert g.iloc[0][["zip5", "rule", "sdi_quintile"]].tolist() == ["19104", "person_location", SDI_LABELS[4]]
    stays = pd.DataFrame({"person_id": [2, 1], "admit_date": ["2016-01-01", "2016-01-01"]})
    g = derive_geography_features(c, RUCA, SDI, CUTS, stays=stays, address_table=None)
    assert g["rule"].tolist() == ["person_location", "no_zip"]          # person 1 has no location
    with pytest.raises(ValueError, match="missing column"):
        derive_geography_features(c, RUCA, SDI, CUTS, stays=pd.DataFrame({"person_id": [1]}))
    with pytest.raises(ValueError, match="unique"):
        derive_geography_features(c, RUCA, SDI, CUTS, stays=stays.assign(stay_id=1))
    with pytest.raises(ValueError, match="invalid address table"):
        derive_geography_features(c, RUCA, SDI, CUTS, address_table="x; DROP")


def test_load_location_writes_dated_address_history():
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(SAMPLE_DIR))
    load_location(con, str(SAMPLE_DIR))
    n = con.execute("SELECT count(*) FROM person_address_history").fetchone()[0]
    assert n >= 3
    assert con.execute("SELECT count(*) FROM person_address_history WHERE person_id NOT IN (SELECT person_id FROM person)").fetchone()[0] == 0
    load_location(con, str(SAMPLE_DIR))                                  # idempotent: rows are replaced per person
    assert con.execute("SELECT count(*) FROM person_address_history").fetchone()[0] == n
    assert con.execute("SELECT count(*) FROM person_address_history WHERE zip5 IS NOT NULL AND length(zip5) <> 5").fetchone()[0] == 0


_ADDR_HEADER = ("ADDRESSID,PATID,ADDRESS_USE,ADDRESS_TYPE,ADDRESS_PREFERRED,ADDRESS_CITY,ADDRESS_STATE,ADDRESS_ZIP5,"
                "ADDRESS_ZIP9,ADDRESS_PERIOD_START,ADDRESS_PERIOD_END\n")


def test_load_location_repairs_unquoted_comma_in_city(tmp_path):
    import shutil

    src = tmp_path / "src"
    shutil.copytree(SAMPLE_DIR, src)
    pat = next(c for c in duckdb.connect().execute(f"SELECT PATID FROM read_csv_auto('{(src / 'demographic.csv').as_posix()}', all_varchar=true)").fetchall())[0]
    (src / "lds_address_history.csv").write_text(
        _ADDR_HEADER + f"1,{pat},HO,PO,Y,Philadelphia,PA,19104,,01JAN2010,31DEC2012\n"
        f"2,{pat},HO,PO,N,St. Louis, MO,MO,63101,,01JAN2013,\n", encoding="utf-8")
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    load_location(con, str(src))
    assert con.execute("SELECT city, state, zip FROM location WHERE state = 'MO'").fetchall() == [("St. Louis, MO", "MO", "63101")]
    row = con.execute("SELECT zip5, address_start, address_end FROM person_address_history WHERE zip5 = '63101'").fetchall()
    assert len(row) == 1 and str(row[0][1]) == "2013-01-01" and row[0][2] is None


def test_pcornet_discharge_status_home_is_not_hospice(tmp_path):
    import shutil

    src = tmp_path / "src"
    shutil.copytree(SAMPLE_DIR, src)
    (src / "encounter.csv").write_text(
        "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\n"
        + "".join(f"E{i},FAKE-P001,IP,2024-01-0{i},08:00,2024-01-0{i},11:00,,,{s}\n"
                  for i, s in enumerate(["HO", "HS", "AM", "EX", "SN", "A", "NH", "HH", "RH"], start=1)), encoding="utf-8")
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    from omop_etl import load_visit_occurrence

    load_visit_occurrence(con, str(src))
    got = dict(con.execute("SELECT discharged_to_source_value, discharged_to_concept_id FROM visit_occurrence").fetchall())
    assert got["HO"] == 8536 and got["A"] == 8536 and got["HS"] == 8546
    assert got["EX"] == 4216643 and got["AM"] == 32216 and got["SN"] == 8863
    assert got["NH"] == 8676 and got["HH"] == 32215 and got["RH"] == 32228


def _transfer_cdm():
    from omop_etl import define_study_cohort  # noqa: F401  (import check)

    c = duckdb.connect()
    build_schema(c)
    for pid in (1, 2, 3):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, 1960, 0, 0)")
        c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                  f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '2022-01-01', 0)")
    for vid, pid, dis in ((1, 1, 8536), (2, 2, 8717), (3, 3, 4155309)):       # home, acute transfer, AMA
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id, discharged_to_concept_id) VALUES ({vid}, {pid}, 9201, DATE '2020-03-01', DATE '2020-03-05', 0, {dis})")
    return c


def test_transfer_exclusion_is_opt_in_and_keeps_ama():
    from omop_etl import define_study_cohort

    kw = dict(visit_type="inpatient", target_outcome="none", washin_days=0, require_verified_followup=False)
    default = define_study_cohort(_transfer_cdm(), **kw)
    assert sorted(default["subject_id"]) == [1, 2, 3]                       # unchanged unless asked
    out = define_study_cohort(_transfer_cdm(), transfer_discharge_concept_ids=(8717,), **kw)
    assert sorted(out["subject_id"]) == [1, 3]                              # transfer dropped, AMA kept
    steps = out.attrs["attrition"].step_name.tolist()
    assert "Not transferred to another acute-care hospital" in steps
    keep = define_study_cohort(_transfer_cdm(), transfer_discharge_concept_ids=(), **kw)
    assert sorted(keep["subject_id"]) == [1, 2, 3]


def test_ederri_diabetes_cohort_excludes_transfers_by_default():
    import inspect

    from omop_etl import build_ederri_diabetes_cohort

    assert inspect.signature(build_ederri_diabetes_cohort).parameters["transfer_discharge_concept_ids"].default == (8717,)


def test_pcornet_observation_and_acute_transfer_mapping(tmp_path):
    import shutil

    src = tmp_path / "src"
    shutil.copytree(SAMPLE_DIR, src)
    (src / "encounter.csv").write_text(
        "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\n"
        "E1,FAKE-P001,OS,2024-01-01,08:00,2024-01-02,11:00,,,IP\n", encoding="utf-8")
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    from omop_etl import load_visit_occurrence

    load_visit_occurrence(con, str(src))
    assert con.execute("SELECT visit_concept_id, discharged_to_concept_id FROM visit_occurrence").fetchone() == (581385, 8717)


def test_diabetes_phenotype_uses_every_hba1c_loinc_of_the_sas_extraction():
    from omop_etl import build_ederri_diabetes_cohort

    c = duckdb.connect()
    build_schema(c)
    # one person per HbA1c concept the SAS extraction lists (LOINC 17855-8, 41995-2, 62388-4, 71875-9, 4549-2) plus a normal value
    cases = {1: (3007263, 7.0), 2: (3034639, 7.0), 3: (40765129, 7.0), 4: (42869630, 7.0), 5: (3003309, 7.0), 6: (3004410, 5.0)}
    for pid, (cid, val) in cases.items():
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, 1960, 0, 0)")
        c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                  f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '2022-01-01', 0)")
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id) VALUES ({pid}, {pid}, 9201, DATE '2020-03-01', DATE '2020-03-05', 0)")
        c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, "
                  f"measurement_type_concept_id) VALUES ({pid}, {pid}, {cid}, DATE '2020-01-01', {val}, 0)")
    out = build_ederri_diabetes_cohort(c)
    assert sorted(out["subject_id"]) == [1, 2, 3, 4, 5]


def test_emergency_to_inpatient_is_an_index_stay_for_the_ederri_cohort(tmp_path):
    import shutil

    from omop_etl import build_ederri_diabetes_cohort, load_visit_occurrence

    src = tmp_path / "src"
    shutil.copytree(SAMPLE_DIR, src)
    (src / "encounter.csv").write_text(
        "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\n"
        "E1,FAKE-P001,EI,2020-03-01,08:00,2020-03-05,11:00,,,HO\n", encoding="utf-8")
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    load_visit_occurrence(con, str(src))
    assert con.execute("SELECT visit_concept_id FROM visit_occurrence").fetchone()[0] == 262
    pid = con.execute("SELECT person_id FROM visit_occurrence").fetchone()[0]
    con.execute("UPDATE person SET year_of_birth = 1960 WHERE person_id = ?", [pid])
    con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                "observation_period_end_date, period_type_concept_id) VALUES (1, ?, DATE '2015-01-01', DATE '2022-01-01', 0)", [pid])
    con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                "condition_type_concept_id, condition_source_value) VALUES (1, ?, 0, DATE '2019-01-01', 0, 'E12.9')", [pid])
    out = build_ederri_diabetes_cohort(con)
    assert list(out["subject_id"]) == [pid]                                  # EI stay, qualified through an E12 code
