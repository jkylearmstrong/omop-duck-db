"""A bundle refuses to score custom concept ids that mean another feature at this site."""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.build_omop_cdm import build_schema
from omop_etl.bundle_concepts import stamp_feature_spec_concepts, validate_bundle_concepts
from omop_etl.custom_covariates import export_custom_concept_registry, write_custom_features
from omop_etl.model_bundle import load_model_bundle, score_cohort, write_model_bundle

SPEC = {"features": [{"name": "age", "dtype": "float", "bounds": [0, 120], "impute": 60.0},
                     {"name": "charlson_site", "dtype": "float", "bounds": [0, 30], "impute": 0.0}]}


def _site(first=None):
    """A CDM whose custom concept ids were assigned after registering ``first`` (so ids differ between sites)."""
    c = duckdb.connect()
    build_schema(c)
    day = pd.Timestamp("2020-01-01").date()
    if first:
        write_custom_features(c, pd.DataFrame({"person_id": [1], "date": [day], first: [1.0]}))
    write_custom_features(c, pd.DataFrame({"person_id": [1], "date": [day], "charlson_site": [3.0]}))
    return c


def _bundle(tmp_path, spec):
    return write_model_bundle(tmp_path / "b", name="demo", version="1", feature_spec=spec,
                              components=[{"name": "glm", "kind": "logistic", "filename": "glm.json",
                                           "content": {"intercept": -1.0, "coef": [0.01, 0.3]}}])


def test_stamping_records_the_ids_and_the_same_site_validates():
    a = _site()
    spec = stamp_feature_spec_concepts(SPEC, a)
    assert "concept_id" not in spec["features"][0] and spec["features"][1]["concept_id"] == 2001000000
    assert SPEC["features"][1].get("concept_id") is None                          # the input is not modified
    out = validate_bundle_concepts(spec, a)
    assert out["status"].tolist() == ["ok"] and out.loc[0, "local_concept_id"] == 2001000000


def test_a_site_with_a_different_id_is_refused_with_the_reason():
    spec = stamp_feature_spec_concepts(SPEC, _site())
    b = _site(first="elixhauser")                                                # here charlson_site is 2001000001
    with pytest.raises(ValueError, match="different_id"):
        validate_bundle_concepts(spec, b)
    out = validate_bundle_concepts(spec, b, raise_on_conflict=False)
    assert out.loc[0, "status"] == "different_id" and out.loc[0, "local_concept_id"] == 2001000001


def test_missing_feature_and_id_used_by_another_feature():
    spec = {"features": [{"name": "frailty", "concept_id": 2001000000}, {"name": "age"}]}
    out = validate_bundle_concepts(spec, {"charlson_site": 2001000000}, raise_on_conflict=False)
    assert out.loc[0, "status"] == "id_used_by_other_feature" and "charlson_site" in out.loc[0, "detail"]
    out = validate_bundle_concepts(spec, {}, raise_on_conflict=False)
    assert out.loc[0, "status"] == "not_registered_here"
    assert len(validate_bundle_concepts({"features": [{"name": "age"}]}, {})) == 0                  # nothing recorded: nothing to check
    keyed = {"features": [{"name": "charlson_v2", "concept_key": "charlson_site", "concept_id": 2001000000}]}
    assert validate_bundle_concepts(keyed, {"charlson_site": 2001000000})["status"].tolist() == ["ok"]


def test_source_can_be_a_registry_file_and_a_bundle_directory(tmp_path):
    a = _site()
    path = tmp_path / "registry.json"
    export_custom_concept_registry(a, path)
    d = _bundle(tmp_path, stamp_feature_spec_concepts(SPEC, a))
    assert validate_bundle_concepts(d, path)["status"].tolist() == ["ok"]
    assert validate_bundle_concepts(load_model_bundle(d), a)["status"].tolist() == ["ok"]


def test_score_cohort_checks_concepts_before_scoring(tmp_path):
    a = _site()
    d = _bundle(tmp_path, stamp_feature_spec_concepts(SPEC, a))
    df = pd.DataFrame({"person_id": [1, 2], "age": [50.0, 70.0], "charlson_site": [2.0, 5.0]})
    ok = score_cohort(d, df, check_concepts=a)
    np.testing.assert_allclose(ok["prob"], 1 / (1 + np.exp(-(-1.0 + 0.01 * df["age"] + 0.3 * df["charlson_site"]))), atol=1e-6)
    assert len(score_cohort(d, df)) == 2                                          # no check requested: unchanged behaviour
    with pytest.raises(ValueError, match="do not match this site"):
        score_cohort(d, df, check_concepts=_site(first="elixhauser"))
