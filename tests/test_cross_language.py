"""The Python and R implementations give the same answers on the same databases.

The package ships the Circe compiler, the DQD catalog, cohort reconciliation, custom concept ids, the bundle concept check and the model
metrics in both languages. Each has its own tests; this one feeds the same fixtures to both and compares the results, so a change to one
side that the other misses fails here. It needs ``Rscript`` and the package's R dependencies; it is skipped otherwise.
"""
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from omop_etl.bundle_concepts import stamp_feature_spec_concepts, validate_bundle_concepts
from omop_etl.circe import compile_circe_to_duckdb
from omop_etl.circe_definitions import target_cohort_circe
from omop_etl.cohort_reconcile import reconcile_cohort
from omop_etl.custom_covariates import write_custom_features
from omop_etl.dqd_catalog import run_dqd_catalog
from omop_etl.model_ablation import _auprc, _auroc, _brier, ridge_logistic_fit_predict
from omop_etl.model_ablation import _WeightedMetrics as WeightedMetrics

import test_circe_constructs as cc
import test_dqd_catalog as dq
import test_cohort_reconcile as rc

RSCRIPT = shutil.which("Rscript")
pytestmark = pytest.mark.skipif(RSCRIPT is None, reason="Rscript is not available")


def _save(con, path):
    """Copies an in-memory database to a file so that both languages open the same data."""
    path = str(path)
    con.execute(f"ATTACH '{path}' AS out")
    con.execute("COPY FROM DATABASE memory TO out")
    con.execute("DETACH out")


def _rows(con, definition):
    with __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore")
        sql = compile_circe_to_duckdb(definition, cdm_schema="main")
    df = con.execute(sql).df()
    return sorted(f"{int(r.subject_id)}@{str(r.cohort_start_date)[:10]}" for r in df.itertuples())


def _close(a, b, tol=1e-6):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)
    return a == b


def test_python_and_r_agree(tmp_path):
    work = tmp_path
    (work / "r_copy").mkdir()
    # fixtures, saved to files; each language works on its own copy
    cons, dqd, target = cc._cdm(), dq._gap_cdm(), rc._cdm()
    paths = {"cons.duckdb": cons, "dqd.duckdb": dqd, "target.duckdb": target}
    for name, con in paths.items():
        _save(con, work / name)
    py_dir, r_dir = work / "py_copy", work / "r_copy"
    py_dir.mkdir()
    for name in paths:
        shutil.copy(work / name, py_dir / name)
        shutil.copy(work / name, r_dir / name)

    definitions = {name: spec[0] for name, spec in cc.CASES.items()}
    reference = ["P1", "P2", "P5", "P6", "P7", "P99"]
    rng = np.random.default_rng(4)
    y = (rng.uniform(size=60) < 0.35).astype(int)
    p = np.clip(0.3 + 0.3 * (y - 0.35) + rng.normal(0, 0.2, 60), 0.02, 0.98)
    x = rng.normal(size=(40, 3))
    y_train = (rng.uniform(size=30) < 1 / (1 + np.exp(-x[:30, 0]))).astype(int)
    p_tied = np.round(p, 1)                                       # many tied scores
    weights = rng.integers(0, 4, 60)
    inputs = {"p_tied": p_tied.tolist(), "weights": weights.tolist(), "definitions": definitions, "today": dq.TODAY.isoformat(), "target_definition": target_cohort_circe(), "reference": reference,
              "y": y.tolist(), "p": p.tolist(), "x": x.tolist(), "y_train": y_train.tolist(), "cut": 30}
    (work / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")

    # Python side, on its own copies
    out = {}
    con = duckdb.connect(str(py_dir / "cons.duckdb"))
    out["circe"] = {n: _rows(con, d) for n, d in definitions.items()}
    con.close()
    con = duckdb.connect(str(py_dir / "dqd.duckdb"))
    res = run_dqd_catalog(con, today=dq.TODAY)
    out["dqd"] = [{"name": r["check_name"], "table": r["cdm_table_name"], "field": r["cdm_field_name"] or "", "violated": r["num_violated_rows"],
                   "denominator": r["num_denominator_rows"], "status": r["execution_status"]} for r in res["CheckResults"]]
    con.close()
    con = duckdb.connect(str(py_dir / "target.duckdb"))
    rec = reconcile_cohort(con, pd.DataFrame({"person_source_value": reference}), definition=target_cohort_circe())
    out["reconcile_counts"] = rec["counts"]
    out["reconcile_gaps"] = [{"reason": r.reason, "detail": r.detail, "n": int(r.n)} for r in rec["gaps"].itertuples()]
    write_custom_features(con, pd.DataFrame({"person_id": [1, 2], "date": [pd.Timestamp("2020-01-01").date()] * 2, "elixhauser_site": [1.0, 2.0]}))
    ids = write_custom_features(con, pd.DataFrame({"person_id": [1, 2], "date": [pd.Timestamp("2020-01-01").date()] * 2,
                                                   "charlson_site": [3.0, 4.0], "frailty": [0.1, 0.2]}))
    out["concept_ids"] = {k: float(v[0]) for k, v in ids.items()}
    spec = stamp_feature_spec_concepts({"features": [{"name": "charlson_site"}, {"name": "age"}]}, con)
    out["stamped"] = [f.get("concept_id") for f in spec["features"]]
    stale = {"features": [{"name": "charlson_site", "concept_id": 2001000000}, {"name": "frailty", "concept_id": 2001000099},
                          {"name": "nothere", "concept_id": 5_000_000_000}]}
    out["bundle_check"] = validate_bundle_concepts(stale, con, raise_on_conflict=False)["status"].tolist()
    con.close()
    out["metrics"] = {"auroc": _auroc(y, p), "auprc": _auprc(y, p), "brier": _brier(y, p)}
    out["ridge"] = ridge_logistic_fit_predict(x[:30], y_train, x[30:]).tolist()
    out["weighted"] = WeightedMetrics(y, p_tied).metrics(weights)

    # R side
    env = dict(os.environ, R_PROFILE_USER="", R_ENVIRON_USER="")
    r = subprocess.run([RSCRIPT, "--vanilla", str(REPO_ROOT / "tests" / "cross_language" / "run_r.R"), str(work)], cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        message = f"the R side could not run (missing R packages?): {r.stderr[-600:]}"
        if os.environ.get("OMOP_CROSS_LANGUAGE_REQUIRED"):         # CI: a skipped comparison would hide the failure
            pytest.fail(message)
        pytest.skip(message)
    rr = json.loads((work / "r_results.json").read_text(encoding="utf-8"))

    # compare
    as_list = lambda v: v if isinstance(v, list) else [v]          # jsonlite writes a one-element vector as a scalar
    assert {k: as_list(v) for k, v in rr["circe"].items()} == out["circe"]
    key = lambda d: (d["name"], d["table"], d["field"])
    py_dqd, r_dqd = {key(d): d for d in out["dqd"]}, {key(d): d for d in rr["dqd"]}
    assert set(py_dqd) == set(r_dqd), (sorted(set(py_dqd) ^ set(r_dqd))[:8])
    for k in py_dqd:
        a, b = py_dqd[k], r_dqd[k]
        assert (a["violated"], a["denominator"], a["status"]) == (b["violated"], b["denominator"], b["status"]), k
    assert {k: (float(v) if isinstance(v, float) else v) for k, v in out["reconcile_counts"].items()} == {k: v for k, v in rr["reconcile_counts"].items()}
    assert out["reconcile_gaps"] == (rr["reconcile_gaps"] if isinstance(rr["reconcile_gaps"], list) else [rr["reconcile_gaps"]])
    assert out["concept_ids"] == rr["concept_ids"]
    assert [None if v is None else float(v) for v in out["stamped"]] == [None if v is None else float(v) for v in rr["stamped"]]
    assert out["bundle_check"] == rr["bundle_check"]
    for m in ("auroc", "auprc", "brier"):
        assert _close(out["metrics"][m], rr["metrics"][m]), m
    assert all(_close(a, b, 1e-5) for a, b in zip(out["ridge"], rr["ridge"])) and len(out["ridge"]) == len(rr["ridge"])
    for m in ("auroc", "auprc", "brier"):
        assert _close(out["weighted"][m], rr["weighted"][m], 1e-9), ("weighted", m)
