"""Post-0.7.0 follow-ups: strict JSON (null, never NaN) and DLL-resilient optional imports."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.jsonio import contains_nonfinite, sanitize, strict_dumps, strict_loads  # noqa: E402
from omop_etl.model_bundle import load_model_bundle, write_model_bundle  # noqa: E402
from omop_etl.run_manifest import RunManifest  # noqa: E402
from omop_etl.telemetry import site_telemetry  # noqa: E402


def _reject(token):  # what R / browsers / RFC 8259 parsers do with NaN
    raise ValueError(f"non-standard JSON token {token}")


def strict_parse(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject)


def test_strict_dumps_replaces_nonfinite_values_and_numpy_scalars():
    obj = {"a": float("nan"), "b": [float("inf"), -float("inf"), 1.5], "c": np.float64("nan"), "d": np.int64(3),
           "e": np.array([1.0, np.nan]), "f": {"g": np.float32("nan")}}
    text = strict_dumps(obj)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text, parse_constant=_reject) == {"a": None, "b": [None, None, 1.5], "c": None, "d": 3,
                                                       "e": [1.0, None], "f": {"g": None}}
    assert contains_nonfinite(obj) and not contains_nonfinite(sanitize(obj))
    assert strict_loads('{"x": NaN, "y": Infinity}') == {"x": None, "y": None}


def test_feature_spec_and_bundle_files_never_contain_nan(tmp_path):
    spec = {"features": [
        {"name": "creatinine", "dtype": "float", "bounds": [0.1, float("nan")], "impute": float("nan")},
        {"name": "heart_rate", "dtype": "float", "bounds": [20, 250], "impute": 80.0}]}
    out = write_model_bundle(tmp_path / "b", name="m", version="1", feature_spec=spec,
                             components=[{"kind": "logistic", "filename": "glm.json",
                                          "content": {"intercept": float("nan"), "coef": [0.1, 0.2]}}])
    for f in out.glob("*.json"):
        assert "NaN" not in f.read_text(), f.name
        strict_parse(f)  # valid RFC 8259
    b = load_model_bundle(out)
    assert b.feature_spec["features"][0]["impute"] is None
    # an existing third-party spec that already contains NaN tokens is read leniently as null
    bad = tmp_path / "spec.json"
    bad.write_text('{"features": [{"name": "x", "impute": NaN}]}')
    from omop_etl.feature_contract import load_feature_spec

    assert load_feature_spec(bad)["features"][0]["impute"] is None


def test_manifest_and_telemetry_files_are_strict_json(tmp_path):
    m = RunManifest("unit", extra={"rate": float("nan")}, seeds={"s": 1})
    strict_parse(m.write(tmp_path / "run_manifest.json"))
    r = np.random.default_rng(0)
    y = (r.random(3000) < 0.2).astype(int)
    p = 1 / (1 + np.exp(-r.normal(-2 + y, 1)))
    site_telemetry(p, y, "S01", output=tmp_path / "S01.json")
    strict_parse(tmp_path / "S01.json")


def test_import_or_skip_skips_on_oserror(monkeypatch):
    from conftest import import_or_skip

    def boom(name, *a, **k):
        raise OSError("[WinError 1114] A dynamic link library (DLL) initialization routine failed")

    monkeypatch.setattr("builtins.__import__", boom)
    with pytest.raises(pytest.skip.Exception):
        import_or_skip("torch")
    monkeypatch.undo()
    assert import_or_skip("json") is json
