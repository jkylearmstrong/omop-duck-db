import numpy as np
import pandas as pd
import pytest

from omop_etl.categorical_encoding import encode_categoricals, encoded_column_names

LV = {"sex": {"levels": ["Female", "Male", "Other"], "reference": "Female"},
      "sdi": {"levels": ["Q1", "Q2", "SDI: Missing / Unknown"], "reference": None}}


def test_columns_are_fixed_regardless_of_levels_present():
    names = encoded_column_names(LV)
    assert names == ["sex_Male", "sex_Other", "sdi_Q1", "sdi_Q2", "sdi_SDI: Missing / Unknown"]
    df = pd.DataFrame({"sex": ["Male", "Male"], "sdi": ["Q2", "Q2"], "x": [1, 2]})        # no Female/Other/Q1 present
    out = encode_categoricals(df, LV)
    assert list(out.columns) == ["x"] + names
    assert out[names].dtypes.eq("float32").all()
    assert out["sex_Male"].tolist() == [1, 1] and out["sdi_Q2"].tolist() == [1, 1] and out["sex_Other"].sum() == 0


def test_matches_get_dummies_when_all_levels_present():
    df = pd.DataFrame({"sex": ["Female", "Male", "Other", "Male"], "sdi": ["Q1", "Q2", "SDI: Missing / Unknown", "Q1"]})
    ref = pd.get_dummies(df, columns=["sex", "sdi"], drop_first=True).astype("float32")
    ours = encode_categoricals(df, {"sex": ["Female", "Male", "Other"], "sdi": ["Q1", "Q2", "SDI: Missing / Unknown"]})
    # explicit lists without a reference keep every level; dropping the first matches get_dummies(drop_first=True)
    first = {c: {"levels": v, "reference": v[0]} for c, v in {"sex": ["Female", "Male", "Other"], "sdi": ["Q1", "Q2", "SDI: Missing / Unknown"]}.items()}
    pd.testing.assert_frame_equal(encode_categoricals(df, first)[ref.columns], ref)
    assert ours.shape[1] == 6


def test_missing_unknown_and_errors():
    df = pd.DataFrame({"sex": [None, "Alien", " Male "], "sdi": ["Q1", "Q1", "Q1"]})
    with pytest.raises(ValueError, match="not in the model's levels"):
        encode_categoricals(df, LV)
    out = encode_categoricals(df, LV, unknown="zero")
    assert out["sex_Male"].tolist() == [0, 0, 1] and out["sex_Other"].sum() == 0       # null/unknown -> all zeros; whitespace trimmed
    with pytest.raises(KeyError):
        encode_categoricals(df.drop(columns="sdi"), LV)
    with pytest.raises(ValueError, match="reference"):
        encode_categoricals(df, {"sex": {"levels": ["a"], "reference": "b"}})
    with pytest.raises(ValueError, match="unique"):
        encode_categoricals(df, {"sex": ["a", "a"]})
    with pytest.raises(ValueError, match="unknown must"):
        encode_categoricals(df, LV, unknown="x")
    assert "sex" in encode_categoricals(df, LV, unknown="zero", keep=True).columns
