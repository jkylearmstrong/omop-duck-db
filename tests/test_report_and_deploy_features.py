"""Per-variable Table 1 decimals + section headers, Table 3 odds ratios, ONNX export, FHIR inpatient medications."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.fhir import score_fhir_bundle  # noqa: E402
from omop_etl.model_bundle import export_to_onnx, load_model_bundle, write_model_bundle  # noqa: E402
from omop_etl.odds_ratios import export_table3, generate_table3_odds_ratios  # noqa: E402
from omop_etl.table1 import generate_table1  # noqa: E402


def _df(n=200):
    rng = np.random.default_rng(0)
    return pd.DataFrame({"outcome_flag": rng.integers(0, 2, n), "lab_creatinine": rng.normal(1.4, 1.4, n).round(3),
                         "age": rng.normal(65, 12, n), "female": rng.integers(0, 2, n)})


def _decimals(cell):
    return len(cell.split(" ")[0].split(".")[1])


def test_table1_per_variable_decimals_and_default():
    t = generate_table1(_df(), continuous_vars=["lab_creatinine", "age"], categorical_vars=["female"],
                        decimal_places={"lab_creatinine": 2})["table1"]
    cr = t[(t.Variable == "lab_creatinine") & (t.Category == "Mean (SD)")]["Overall (N=200)"].iloc[0]
    age = t[(t.Variable == "age") & (t.Category == "Mean (SD)")]["Overall (N=200)"].iloc[0]
    assert _decimals(cr) == 2 and _decimals(age) == 1


def test_table1_groups_order_headers_and_other():
    base = generate_table1(_df(), continuous_vars=["lab_creatinine", "age"], categorical_vars=["female"])["table1"]
    assert list(base.Variable[1:3]) == ["lab_creatinine", "lab_creatinine"]
    g = generate_table1(_df(), continuous_vars=["lab_creatinine", "age"], categorical_vars=["female"],
                        groups={"Demographics": ["age", "female"], "Laboratory Values": ["lab_creatinine", "nope"]})["table1"]
    heads = [i for i, v in enumerate(g.Variable) if v in ("Demographics", "Laboratory Values")]
    assert [g.Variable[i] for i in heads] == ["Demographics", "Laboratory Values"]
    assert (g.loc[heads[0]].drop("Variable") == "").all()
    assert list(g.Variable[heads[0] + 1:heads[1]]).count("age") == 2
    rest = generate_table1(_df(), continuous_vars=["age", "lab_creatinine"], groups={"Demographics": ["age"]})["table1"]
    assert "Other" in set(rest.Variable)


def test_table3_scaling_and_export():
    rng = np.random.default_rng(1)
    n = 4000
    X = pd.DataFrame({"systolic_bp": rng.normal(130, 20, n), "diabetes": rng.integers(0, 2, n)})
    p = 1 / (1 + np.exp(-(-3 + 0.03 * (X.systolic_bp - 130) + 0.7 * X.diabetes)))
    y = (rng.random(n) < p).astype(int)
    per_unit = generate_table3_odds_ratios(None, X, y, continuous_scale=None)
    sd = generate_table3_odds_ratios(None, X, y)
    ten = generate_table3_odds_ratios(None, X, y, continuous_scale={"systolic_bp": 10.0})
    assert ten.loc[0, "Increment"] == "per 10-unit" and sd.loc[0, "Increment"].startswith("per 1 SD")
    assert ten.loc[0, "or"] == pytest.approx(per_unit.loc[0, "or"] ** 10)
    assert sd.loc[1, "Increment"] == "Yes vs No" and 1.5 < sd.loc[1, "or"] < 3.0
    assert sd.loc[0, "p_value"] == "<0.001"
    sk = pytest.importorskip("sklearn.linear_model")
    m = sk.LogisticRegression(C=1e8, max_iter=1000).fit(X, y)
    assert generate_table3_odds_ratios(m, X, y, continuous_scale=None).loc[0, "or"] == pytest.approx(per_unit.loc[0, "or"], rel=1e-3)
    for fmt, token in (("markdown", "| Variable"), ("quarto", "tbl-table3"), ("typst", "#figure")):
        assert token in export_table3(sd, fmt)


SPEC = {"features": [{"name": "a", "dtype": "float", "impute": 0.0}, {"name": "b", "dtype": "float", "impute": 0.0}]}


def test_export_to_onnx_matches_bundle(tmp_path):
    pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    b = write_model_bundle(tmp_path / "b", name="m", version="1", feature_spec=SPEC, components=[
        {"kind": "logistic", "filename": "g1.json", "weight": 2.0,
         "content": {"intercept": -1.0, "coef": [0.5, -0.2], "mean": [1.0, 2.0], "scale": [2.0, 4.0]}},
        {"kind": "logistic", "filename": "g2.json", "content": {"intercept": 0.3, "coef": [0.1, 0.4]}}],
        calibration={"method": "platt", "slope": 0.9, "intercept": -0.1})
    bundle = load_model_bundle(b)
    path = export_to_onnx(bundle, tmp_path / "out" / "m.onnx")
    X = np.random.default_rng(2).normal(size=(50, 2))
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    got = sess.run(["probabilities"], {"X": X.astype("float32")})[0].ravel()
    np.testing.assert_allclose(got, bundle.predict_proba(X), atol=1e-5)
    assert (tmp_path / "out" / "m.onnx.features.json").is_file()


def test_export_to_onnx_refuses_isotonic(tmp_path):
    pytest.importorskip("onnx")
    b = write_model_bundle(tmp_path / "b", name="m", version="1", feature_spec=SPEC,
                           components=[{"kind": "logistic", "filename": "g.json", "content": {"intercept": 0, "coef": [1, 1]}}],
                           calibration={"method": "isotonic", "x": [0, 1], "y": [0, 1]})
    with pytest.raises(NotImplementedError):
        export_to_onnx(b, tmp_path / "m.onnx")


def _fhir(extra):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in [
        {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "1950-03-02"},
        {"resourceType": "Encounter", "id": "e1", "class": {"code": "IMP"}, "period": {"start": "2024-01-01", "end": "2024-01-05"}},
        *extra]]}


def test_fhir_inpatient_medication_resources_become_drug_exposure(monkeypatch):
    import omop_etl.encounter as enc
    seen = {}

    def fake_score(con, pid, mb, **k):
        seen["rows"] = con.execute("SELECT drug_concept_id, drug_exposure_start_date, drug_exposure_end_date "
                                   "FROM drug_exposure ORDER BY drug_exposure_id").fetchall()
        return {}

    monkeypatch.setattr(enc, "score_encounter", fake_score)
    rx = "http://www.nlm.nih.gov/research/umls/rxnorm"
    code = lambda c: {"coding": [{"system": rx, "code": c}]}
    r = score_fhir_bundle(_fhir([
        {"resourceType": "MedicationRequest", "medicationCodeableConcept": code("1"), "authoredOn": "2024-01-02"},
        {"resourceType": "MedicationAdministration", "status": "completed", "medicationCodeableConcept": code("2"),
         "effectivePeriod": {"start": "2024-01-03T08:00:00Z", "end": "2024-01-04T08:00:00Z"}},
        {"resourceType": "MedicationAdministration", "status": "not-done", "medicationCodeableConcept": code("3"),
         "effectiveDateTime": "2024-01-03"},
        {"resourceType": "MedicationStatement", "status": "active", "medicationCodeableConcept": code("4"),
         "dateAsserted": "2024-01-04"},
        {"resourceType": "MedicationStatement", "status": "active", "medicationCodeableConcept": code("99")}]),
        None, concept_map={f"{rx}|{c}": 1000 + int(c) for c in "1234"})
    assert [x[0] for x in seen["rows"]] == [1001, 1002, 1004]
    assert str(seen["rows"][1][2])[:10] == "2024-01-04"
    assert r["fhir"]["unmapped_codes"] == [f"{rx}|99"]
