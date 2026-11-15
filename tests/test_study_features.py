"""FeatureSet / study-design features: RUCA, SDI and census-style area features, fitted state, OMOP write-back, manifest."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_geography import CUTS, RUCA, SDI, _con  # noqa: E402
from omop_etl import FeatureSet, area_feature, custom_feature, feature_list, ruca_feature, sdi_feature, sql_feature  # noqa: E402
from omop_etl.geography import GEOGRAPHY_CONCEPT_IDS, derive_geography_features  # noqa: E402

ADDR = [(1, 10, "19104", "2010-01-01", None, "N"), (2, 11, "15213", "2010-01-01", None, "N")]
VISITS = [(1, 1, "2016-06-01"), (2, 2, "2017-02-01")]
INCOME = pd.DataFrame({"zcta": ["19104", "15213", "08101"], "median_income": [30000, 90000, 60000]})
TRAIN_INCOME = pd.DataFrame({"zcta": [f"{10000 + i}" for i in range(50)], "median_income": np.linspace(20000, 200000, 50)})


def _wide_con():
    """Ten stays, one per person, ZIPs 19100-19109 (enough mapped stays to fit a transform)."""
    addr = [(i, 100 + i, f"{19099 + i:05d}", "2010-01-01", None, "N") for i in range(1, 11)]
    c = _con(addr, [(i, i, "2016-06-01") for i in range(1, 11)])
    for pid in range(3, 11):
        c.execute(f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES ({pid}, 8507, 1980, 0, 0)")
    return c


@pytest.fixture()
def con():
    return _con(ADDR, VISITS)


def test_geography_features_match_existing_extractor(con):
    fs = FeatureSet().add(ruca_feature(RUCA)).add(sdi_feature(SDI, CUTS))
    out = fs.compute(con).set_index("stay_id")
    ref = derive_geography_features(con, RUCA, SDI, CUTS).set_index("stay_id")
    assert out.loc[1, "sdi_score"] == ref.loc[1, "sdi_score"] and out.loc[1, "ruca_code"] == 1
    assert out.loc[1, "sdi_quintile"] == 5 and out.loc[2, "sdi_quintile"] == 2
    assert out.loc[2, "ruca_group"] == 2
    assert fs.columns == ["ruca_code", "ruca_group", "sdi_score", "sdi_quintile"]


def test_sdi_requires_fixed_cuts_or_fit(con):
    with pytest.raises(ValueError, match="cut_points"):
        FeatureSet([sdi_feature(SDI)]).compute(con)
    sdi = sdi_feature(pd.DataFrame({"zip": ["19104", "15213"], "sdi": [95.0, 40.0]}))
    fs = FeatureSet([sdi])
    with pytest.raises(ValueError, match="at least 5"):
        fs.compute(con, fit=True)


def test_area_feature_log_missing_and_unmapped(con):
    f = area_feature("income", INCOME, "median_income", transform="log1p")
    out = FeatureSet([f]).compute(con).set_index("stay_id")
    assert out.loc[1, "income"] == pytest.approx(np.log1p(30000)) and out.loc[1, "income_missing"] == 0
    c2 = _con([(1, 10, "99999", "2010-01-01", None, "N"), (2, 11, "15213", "2010-01-01", None, "N")], VISITS)
    o2 = FeatureSet([area_feature("income", INCOME, "median_income", transform="log1p")]).compute(c2).set_index("stay_id")
    assert np.isnan(o2.loc[1, "income"]) and o2.loc[1, "income_missing"] == 1


def test_fitted_transform_travels_between_sites(con):
    # train: fit percentile grid + median on a training reference
    train_fs = FeatureSet([area_feature("income", TRAIN_INCOME, "median_income", transform="percentile", impute=True)])
    with pytest.raises(ValueError, match="training fit"):
        FeatureSet([area_feature("income", INCOME, "median_income", transform="percentile")]).compute(con)
    # the training table is keyed by ZIPs of the training cohort: build such a cohort
    addr = [(i, 100 + i, f"{10000 + i}", "2010-01-01", None, "N") for i in range(1, 11)]
    train_con = _con(addr, [], None)
    for pid in range(3, 11):
        train_con.execute(f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES ({pid}, 8507, 1980, 0, 0)")
    for i in range(1, 11):
        train_con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                          f"visit_type_concept_id) VALUES ({i}, {i}, 9201, DATE '2016-01-01', DATE '2016-01-02', 0)")
    train_out = train_fs.compute(train_con, fit=True)
    state = train_fs.state()
    assert len(state["income"]["percentile_grid"]) == 101 and "fill_value" in state["income"]
    assert train_out["income"].between(0, 1).all()
    # replicate: state loaded from a manifest file, no local fitting
    other = FeatureSet([area_feature("income", INCOME, "median_income", transform="percentile", impute=True)])
    other.load_state(json.loads(json.dumps(train_fs.manifest())))
    o = other.compute(con).set_index("stay_id")
    assert o.loc[1, "income"] == pytest.approx(np.interp(30000, state["income"]["percentile_grid"], np.linspace(0, 1, 101)))


def test_unknown_feature_and_duplicate_columns():
    fs = FeatureSet([ruca_feature(RUCA)])
    with pytest.raises(ValueError, match="already in the set"):
        fs.add(ruca_feature(RUCA))
    with pytest.raises(ValueError, match="duplicate"):
        fs.add(custom_feature("other", ["ruca_code"], lambda c, s: s))
    with pytest.raises(TypeError):
        fs.add("ruca")  # type: ignore[arg-type]
    assert fs.remove("ruca").names == []


def test_sql_and_custom_features(con):
    n = sql_feature("n_visits", "SELECT s.stay_id, count(v.visit_occurrence_id)::DOUBLE AS n_visits FROM _stays s "
                                "LEFT JOIN visit_occurrence v ON v.person_id = s.person_id AND v.visit_start_date <= s.admit_date GROUP BY 1",
                    ["n_visits"])
    cu = custom_feature("flag", ["is_2017"], lambda c, s: pd.DataFrame({"stay_id": s["stay_id"], "is_2017": (s["admit_date"].dt.year == 2017).astype(float)}))
    out = FeatureSet([n, cu]).compute(con).set_index("stay_id")
    assert out["n_visits"].tolist() == [1.0, 1.0] and out["is_2017"].tolist() == [0.0, 1.0]
    assert FeatureSet([sql_feature("x", "SELECT 1", ["x"], requires=("no_such_table",))]).check(con) == {"x": ["no_such_table"]}


def test_write_measurements_and_r_snippet(con):
    fs = FeatureSet([ruca_feature(RUCA), sdi_feature(SDI, CUTS), area_feature("income", INCOME, "median_income", transform="log1p")])
    wide = fs.compute(con)
    concepts = fs.write(con, wide)
    assert concepts["sdi_score"][0] == GEOGRAPHY_CONCEPT_IDS["sdi_score"]           # geography ids kept
    assert concepts["income"][0] >= 2_001_000_000
    n = con.execute("SELECT count(*) FROM measurement WHERE measurement_concept_id = ?", [concepts["income"][0]]).fetchone()[0]
    assert n == 2
    assert con.execute("SELECT count(*) FROM measurement WHERE visit_occurrence_id IS NULL AND measurement_concept_id = ?",
                       [concepts["income"][0]]).fetchone()[0] == 0
    r = fs.feature_extraction_r(concepts)
    assert "useMeasurementValueLongTerm = TRUE" in r and str(concepts["income"][0]) in r
    assert fs.manifest(concepts=concepts)["features"][2]["concept_ids"]["income"] == concepts["income"][0]
    with pytest.raises(ValueError, match="no concept id"):
        fs.feature_extraction_r()


def test_feature_list_geography(con):
    fs = feature_list("geography", ruca={"crosswalk": RUCA}, sdi={"table": SDI, "cut_points": CUTS})
    assert fs.names == ["ruca", "sdi"] and len(fs.compute(con)) == 2
    with pytest.raises(ValueError, match="needs arguments"):
        feature_list("geography", ruca={"crosswalk": RUCA})


def test_zscore_cohort_stays_manifest_and_registry(con, tmp_path):
    from omop_etl import stays_from_cohort
    from omop_etl.study_features import (FEATURE_LISTS, MANIFEST_FORMAT, StudyFeature, available_features, make_feature,
                                         register_feature_factory)
    con.execute("CREATE TABLE cohort_x AS SELECT 1 AS cohort_definition_id, person_id AS subject_id, visit_start_date AS cohort_start_date, "
                "visit_end_date AS cohort_end_date, visit_occurrence_id FROM visit_occurrence")
    stays = stays_from_cohort(con, "cohort_x", cohort_id=1)
    assert stays["stay_id"].tolist() == [1, 2] and list(stays.columns) == ["person_id", "stay_id", "admit_date"]
    with pytest.raises(ValueError, match="invalid cohort table"):
        stays_from_cohort(con, "x; DROP")
    con.execute("CREATE TABLE bad_cohort (a INT)")
    with pytest.raises(ValueError, match="needs subject_id"):
        stays_from_cohort(con, "bad_cohort")

    tbl = pd.DataFrame({"zcta": ["19104", "15213"], "v": [10.0, 30.0]})
    big = pd.DataFrame({"zip5": [f"{i:05d}" for i in range(19100, 19110)] + ["15213"], "value": np.arange(11.0)})
    z = area_feature("zv", big, "value", transform="zscore", missing_indicator=False)
    fs = FeatureSet([z])
    wide_con = _wide_con()
    stays = wide_con.execute("SELECT person_id, visit_occurrence_id AS stay_id, visit_start_date AS admit_date FROM visit_occurrence").df()
    with pytest.raises(ValueError, match="training fit"):
        fs.compute(wide_con, stays)
    out = fs.compute(wide_con, stays, fit=["zv"])
    assert set(fs.state()["zv"]) == {"mean", "sd"} and out.attrs["feature_state"] == fs.state()
    path = tmp_path / "m.json"
    m = fs.export_manifest(path)
    assert m["format"] == MANIFEST_FORMAT and json.loads(path.read_text())["features"][0]["name"] == "zv"
    other = FeatureSet([area_feature("zv", big, "value", transform="zscore", missing_indicator=False)]).load_state(path)
    assert other.compute(wide_con, stays)["zv"].tolist() == out["zv"].tolist()
    with pytest.raises(ValueError, match="unknown features"):
        fs.compute(con, stays, fit=["nope"])
    with pytest.raises(KeyError):
        fs.remove("nope")
    assert "zv" in fs and len(fs) == 1 and [f.name for f in fs] == ["zv"] and fs["zv"].name == "zv"

    # constructor / argument validation
    with pytest.raises(ValueError, match="letters, digits"):
        StudyFeature("bad name", ("c",), lambda *a: None)
    with pytest.raises(ValueError, match="distinct column"):
        StudyFeature("f", ("c", "c"), lambda *a: None)
    with pytest.raises(ValueError, match="unknown columns"):
        StudyFeature("f", ("c",), lambda *a: None, concept_ids={"d": 1})
    with pytest.raises(ValueError, match="transform must be"):
        area_feature("a", tbl, "v", transform="sqrt")
    with pytest.raises(ValueError, match="ZIP column"):
        area_feature("a", pd.DataFrame({"x": [1], "v": [2]}), "v")
    with pytest.raises(ValueError, match="value_column"):
        area_feature("a", pd.DataFrame({"zip": ["1"], "u": [1], "v": [2]}))
    with pytest.raises(ValueError, match="stays is missing"):
        FeatureSet([z]).compute(con, pd.DataFrame({"person_id": [1]}))
    with pytest.raises(ValueError, match="unique"):
        FeatureSet([z]).compute(con, pd.DataFrame({"person_id": [1, 2], "stay_id": [1, 1], "admit_date": ["2016-01-01"] * 2}))
    with pytest.raises(ValueError, match="invalid address table"):
        FeatureSet([z]).compute(con, stays, address_table="x; DROP", fit=True)
    bad = custom_feature("bad", ["b"], lambda c, s: pd.DataFrame({"stay_id": s["stay_id"]}))
    with pytest.raises(ValueError, match="returned"):
        FeatureSet([bad]).compute(con, stays)
    dup = custom_feature("dup", ["d"], lambda c, s: pd.DataFrame({"stay_id": [1, 1], "d": [0.0, 1.0]}))
    with pytest.raises(ValueError, match="more than one row"):
        FeatureSet([dup]).compute(con, stays)
    with pytest.raises(ValueError, match="required OMOP table"):
        FeatureSet([sql_feature("x", "SELECT 1", ["x"], requires=("no_such",))]).compute(con, stays)
    with pytest.raises(ValueError, match="missing column"):
        fs.write(con, stays)

    # registry
    with pytest.raises(ValueError, match="already registered"):
        register_feature_factory("ruca", ruca_feature)
    register_feature_factory("mine", lambda: custom_feature("mine", ["m"], lambda c, s: pd.DataFrame({"stay_id": s["stay_id"], "m": 1.0})))
    assert "mine" in available_features() and make_feature("mine").name == "mine"
    with pytest.raises(KeyError):
        make_feature("nope")
    with pytest.raises(KeyError):
        feature_list("nope")
    FEATURE_LISTS["tmp_list"] = ("mine",)
    assert feature_list("tmp_list", mine={}).names == ["mine"]
    del FEATURE_LISTS["tmp_list"]


def test_person_location_fallback_and_sdi_fit(tmp_path):
    c = _con([], VISITS, location_zip="19104")                   # person 2 only has person.location_id
    sdi = pd.DataFrame({"zip": [f"{i:05d}" for i in range(19100, 19110)], "sdi": np.arange(10.0, 110.0, 10)})
    fs = FeatureSet([sdi_feature(sdi)])
    c.execute("INSERT INTO location (location_id, zip) SELECT 1000 + i, printf('%05d', 19099 + i) FROM range(1, 11) t(i)")
    for pid in range(3, 11):
        c.execute(f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, location_id) VALUES ({pid}, 8507, 1980, 0, 0, {1000 + pid})")
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id) VALUES ({pid}, {pid}, 9201, DATE '2016-06-01', DATE '2016-06-02', 0)")
    out = fs.compute(c, fit=True)
    assert len(fs.state()["sdi"]["cut_points"]) == 4 and len(out) == 10
