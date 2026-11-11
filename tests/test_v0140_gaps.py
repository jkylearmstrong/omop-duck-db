"""ACS area features, concept-set features, zip3 geography and FeatureSet inside extract_benchmark_features. Synthetic data only."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_geography import _con  # noqa: E402
from _bf_fixture import LAB, make_db  # noqa: E402
from omop_etl import (  # noqa: E402
    FeatureSet, acs_feature, acs_zip3_table, concept_feature, define_study_cohort, extract_benchmark_features, fetch_acs_zcta,
)

HEAD = ["NAME", "B01003_001E", "B19013_001E", "B17001_001E", "B17001_002E", "B15003_001E"]


def fake_census(url):
    """Census API shaped JSON for three ZCTAs (all other education cells 0 or 1)."""
    from omop_etl.census_acs import _RAW
    assert "api.census.gov/data/2022/acs/acs5" in url and "zip%20code%20tabulation%20area" in url
    rows = [list(_RAW) + ["zip code tabulation area"]]
    spec = {"19104": (1000, 30000, 1000, 400, 800), "19103": (3000, 90000, 3000, 300, 2400), "15213": (500, -666666666, 500, 50, 400)}
    for z, (pop, inc, pv, pvb, adults) in spec.items():
        r = {c: "0" for c in _RAW}
        r.update({"B01003_001E": str(pop), "B19013_001E": str(inc), "B17001_001E": str(pv), "B17001_002E": str(pvb),
                  "B15003_001E": str(adults), "B15003_022E": str(adults // 4), "B15003_002E": str(adults // 8)})
        rows.append([r[c] for c in _RAW] + [z])
    return rows


def test_fetch_acs_cache_sentinels_and_rates(tmp_path):
    z = fetch_acs_zcta(2022, cache_dir=tmp_path, fetcher=fake_census).set_index("zip5")
    assert z.loc["19104", "poverty_rate"] == pytest.approx(0.4) and z.loc["19104", "pct_bachelors_plus"] == pytest.approx(0.25)
    assert np.isnan(z.loc["15213", "median_household_income"])                       # -666666666 is "no estimate"
    again = fetch_acs_zcta(2022, cache_dir=tmp_path, fetcher=lambda u: 1 / 0)         # served from the cache, no download
    assert len(again) == 3
    z3 = acs_zip3_table(z.reset_index()).set_index("zip3")
    assert z3.loc["191", "median_household_income"] == pytest.approx((30000 * 1000 + 90000 * 3000) / 4000)


def test_acs_feature_zip5_and_zip3(tmp_path):
    z = fetch_acs_zcta(2022, cache_dir=tmp_path, fetcher=fake_census)
    con5 = _con([(1, 10, "19104", "2010-01-01", None, "N"), (2, 11, "15213", "2010-01-01", None, "N")],
                [(1, 1, "2016-06-01"), (2, 2, "2017-02-01")])
    f = acs_feature("poverty_rate", table=z)
    out = FeatureSet([f]).compute(con5).set_index("stay_id")
    assert out.loc[1, "acs_poverty_rate"] == pytest.approx(0.4) and f.params["year"] == 2022
    inc = FeatureSet([acs_feature("median_household_income", table=z)]).compute(con5).set_index("stay_id")
    assert inc.loc[1, "acs_median_household_income"] == pytest.approx(np.log1p(30000)) and inc.loc[2, "acs_median_household_income_missing"] == 1
    # an extract that only keeps 3-digit ZIPs in location.zip, and no address history
    con3 = _con([], [(1, 1, "2016-06-01"), (2, 2, "2017-02-01")], location_zip="191")
    out3 = FeatureSet([acs_feature("poverty_rate", table=z, geography="zip3")]).compute(con3, address_table=None).set_index("stay_id")
    assert out3.loc[2, "acs_poverty_rate"] == pytest.approx(acs_zip3_table(z).set_index("zip3").loc["191", "poverty_rate"])
    assert out3.loc[1, "acs_poverty_rate_missing"] == 1                              # person 1 has no location
    with pytest.raises(ValueError):
        acs_feature("nope", table=z)


def _lab_con():
    con = _con([], [(1, 1, "2016-06-01"), (2, 2, "2016-06-01")])
    rows = [(1, 1, 111, "2016-05-01", 5.0), (2, 1, 111, "2016-05-20", 9.0), (3, 1, 222, "2016-05-25", 7.0),
            (4, 1, 111, "2016-06-02", 99.0),            # after admission: must never be used
            (5, 1, 111, "2015-01-01", 50.0),            # outside the 365-day window
            (6, 2, 111, "2016-05-30", 4.0)]
    for mid, pid, cid, d, v in rows:
        con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                    "measurement_type_concept_id, value_as_number) VALUES (?, ?, ?, ?, 0, ?)", [mid, pid, cid, d, v])
    return con


@pytest.mark.parametrize("agg,expected", [("last", 9.0), ("first", 5.0), ("mean", 7.0), ("max", 9.0), ("min", 5.0), ("count", 2.0)])
def test_concept_feature_aggregates_are_prior_to_admission(agg, expected):
    con = _lab_con()
    f = concept_feature("labs", {"lab_glucose": [111], "lab_other": [222, 223]}, agg=agg)
    out = FeatureSet([f]).compute(con).set_index("stay_id")
    assert out.loc[1, "lab_glucose"] == pytest.approx(expected)               # the 99 after admission and the 2015 value are excluded
    assert out.loc[2, "lab_glucose"] == pytest.approx({"count": 1.0}.get(agg, 4.0))
    assert out.loc[2, "lab_other"] == 0.0 if agg == "count" else np.isnan(out.loc[2, "lab_other"])


def test_concept_feature_missing_indicator_and_validation():
    con = _lab_con()
    out = FeatureSet([concept_feature("labs", {"lab_other": [222]}, missing_indicator=True)]).compute(con).set_index("stay_id")
    assert out.loc[1, "lab_other"] == 7.0 and out.loc[2, "lab_other_missing"] == 1.0 and np.isnan(out.loc[2, "lab_other"])
    with pytest.raises(ValueError, match="agg"):
        concept_feature("c", {"a": [1]}, domain="condition", agg="mean")
    with pytest.raises(ValueError, match="domain"):
        concept_feature("c", {"a": [1]}, domain="nope")


def test_feature_set_inside_benchmark_features():
    con = make_db()
    define_study_cohort(con, materialise=True, table_name="cohort_fs", min_age=18, washin_days=0, min_los_days=0, followup_days=30,
                        require_verified_followup=False)
    fs = FeatureSet([concept_feature("prior_labs", {"prior_creat_mean": [LAB["creatinine"]]}, agg="mean", lookback_days=3650),
                     acs_feature("poverty_rate", table=pd.DataFrame({"zip5": ["19104"], "poverty_rate": [0.2], "median_household_income": [1.0],
                                                                      "population": [1]}), missing_indicator=True)])
    base = extract_benchmark_features(con, "cohort_fs")
    out = extract_benchmark_features(con, "cohort_fs", feature_set=fs)
    assert len(out) == len(base)
    assert {"prior_creat_mean", "acs_poverty_rate", "acs_poverty_rate_missing"} <= set(out.columns)
    assert out.attrs["feature_groups"]["study_features"] == fs.columns
    assert out.attrs["feature_provenance"]["format"] == "omop-duck-db/study-feature-set"
    pd.testing.assert_frame_equal(out[base.columns], base)                   # the existing columns are untouched


def test_validation_paths(tmp_path, monkeypatch):
    from omop_etl import area_feature, export_concept_vocabulary
    from omop_etl.ml_features import FallbackConceptTokenizer
    tab = pd.DataFrame({"zip5": ["19104"], "v": [1.0]})
    with pytest.raises(ValueError, match="geography"):
        area_feature("x", tab, "v", geography="zip9")
    with pytest.raises(ValueError, match="geography"):
        acs_feature("poverty_rate", table=fetch_acs_zcta(2022, cache_dir=tmp_path, fetcher=fake_census), geography="zip9")
    with pytest.raises(ValueError, match="at least one"):
        concept_feature("c", {"a": []})
    with pytest.raises(ValueError, match="letters, digits"):
        concept_feature("c", {"bad name": [1]})
    bad = FallbackConceptTokenizer(["1 - condition - a"])
    bad.concept_list[0] = "[X]"
    with pytest.raises(ValueError, match="Unexpected special tokens"):
        export_concept_vocabulary(bad, tmp_path / "bad.json")


def test_default_cache_dir_and_http_fetch(tmp_path, monkeypatch):
    import io
    import json
    import urllib.request
    from omop_etl.census_acs import _http_fetch
    monkeypatch.setenv("OMOP_DUCK_DB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(json.dumps(fake_census(url)).encode()))
    assert _http_fetch("https://api.census.gov/data/2022/acs/acs5?for=zip%20code%20tabulation%20area:*")[0][0] == "B01003_001E"
    z = fetch_acs_zcta(2022, fetcher=_http_fetch)
    assert len(z) == 3 and (tmp_path / "cache" / "acs5_2022_zcta.csv").exists()


def test_concept_descendants_and_fit_needs_five_stays():
    con = _con([], [(1, 1, "2016-06-01"), (2, 2, "2016-06-01")])
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, "
                "value_as_number) VALUES (1, 1, 555, '2016-05-01', 0, 8.0)")
    con.execute("INSERT INTO concept_ancestor VALUES (111, 555, 1, 1)")
    out = FeatureSet([concept_feature("l", {"lab_x": [111]}, include_descendants=True)]).compute(con).set_index("stay_id")
    assert out.loc[1, "lab_x"] == 8.0                                           # found through the descendant concept
    plain = FeatureSet([concept_feature("l", {"lab_x": [111]})]).compute(con).set_index("stay_id")
    assert np.isnan(plain.loc[1, "lab_x"])
    from omop_etl import area_feature
    with pytest.raises(ValueError, match="at least 5"):
        FeatureSet([area_feature("a", pd.DataFrame({"zip5": ["19104"], "v": [1.0]}), "v", transform="zscore")]).compute(
            _con([(1, 10, "19104", "2010-01-01", None, "N")], [(1, 1, "2016-06-01")]), fit=True)


def test_benchmark_feature_set_column_clash_and_dataset_guards(tmp_path):
    from omop_etl import extract_multimodal_deep_dataset
    con = make_db()
    define_study_cohort(con, materialise=True, table_name="cohort_g", min_age=18, washin_days=0, min_los_days=0, followup_days=30,
                        require_verified_followup=False)
    with pytest.raises(ValueError, match="already exist"):
        extract_benchmark_features(con, "cohort_g", feature_set=FeatureSet([concept_feature("c", {"age_at_index": [LAB["creatinine"]]})]))
    f = extract_benchmark_features(con, "cohort_g").drop(columns=["discharge_date"], errors="ignore")
    with pytest.raises(ValueError, match="discharge_date"):
        extract_multimodal_deep_dataset(con, f, include_intra_measurements=True)
    with pytest.raises(ValueError, match="empty"):
        extract_multimodal_deep_dataset(con, f.iloc[0:0])
