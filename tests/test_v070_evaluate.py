"""omop-duckdb evaluate: check_source -> features -> validate -> score -> telemetry in one command."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema  # noqa: E402
from omop_etl.cli import main as cli_main  # noqa: E402
from omop_etl.evaluate import EvaluationError, evaluate_site  # noqa: E402
from omop_etl.model_bundle import write_model_bundle  # noqa: E402
from omop_etl.telemetry import validate_packet  # noqa: E402

SAMPLE = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"
SPEC = {"features": [
    {"name": "prior_ip_count", "dtype": "int", "impute": 0},
    {"name": "ip_velocity", "dtype": "float", "impute": 0.0},
    {"name": "lace_proxy_total", "dtype": "float", "impute": 3.0},
]}


def make_cdm(path: Path, n: int = 160, seed: int = 3) -> None:
    """n patients: prior IP history, one index stay in 2020, later follow-up evidence, readmission more likely with history."""
    r = np.random.default_rng(seed)
    con = duckdb.connect(str(path))
    build_schema(con)
    persons, visits = [], []
    vid = 1

    def add(pid, concept, s, e):
        nonlocal vid
        visits.append((vid, pid, concept, s, e))
        vid += 1

    for pid in range(1, n + 1):
        persons.append((pid, 8507, 1955, 8527, 38003564))
        k = int(r.integers(0, 4))
        for j in range(k):
            add(pid, 9201, f"2019-0{j + 2}-10", f"2019-0{j + 2}-13")
        add(pid, 9202, "2018-01-05", "2018-01-05")                 # wash-in history
        add(pid, 9201, "2020-06-01", "2020-06-04")                 # index stay (LOS 3)
        add(pid, 9202, "2021-03-01", "2021-03-01")                 # follow-up evidence
        if r.random() < 0.08 + 0.12 * k:                           # 30-day readmission
            add(pid, 9201, "2020-06-15", "2020-06-17")
    con.executemany("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                    "VALUES (?, ?, ?, ?, ?)", persons)
    con.executemany("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                    "visit_end_date, visit_type_concept_id) VALUES (?, ?, ?, CAST(? AS DATE), CAST(? AS DATE), 32827)", visits)
    con.close()


@pytest.fixture(scope="module")
def site_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("site") / "site.duckdb"
    make_cdm(p)
    return p


@pytest.fixture()
def bundle(tmp_path):
    return write_model_bundle(tmp_path / "bundle", name="demo model", version="1.0+rc1", feature_spec=SPEC,
                              components=[{"kind": "logistic", "filename": "glm.json",
                                           "content": {"intercept": -2.2, "coef": [0.5, 0.3, 0.02]}}])


def test_evaluate_runs_the_whole_chain_and_writes_only_aggregates(site_db, bundle, tmp_path):
    logs: list[str] = []
    out = tmp_path / "packet.json"
    pk = evaluate_site(bundle, site_db, out, site_id="S01", source_dir=SAMPLE, log=logs.append)
    assert [l for l in logs if l.startswith("[")][0].startswith("[1] check_source")
    assert [l.split("] ")[1] for l in logs if l.startswith("[")] == [
        "check_source", "load model bundle", "cohort", "extract_velocity_features", "validate_feature_frame",
        "score_cohort", "site_telemetry"]
    written = json.loads(out.read_text(), parse_constant=lambda t: (_ for _ in ()).throw(ValueError(t)))
    validate_packet(written)
    assert written["site_id"] == "S01" and written["n"] == 160 and not written["suppressed"]
    assert written["model"] == {"name": "demo model", "version": "1.0+rc1"}
    assert 0.3 < written["metrics"]["auroc"] < 0.95 and len(written["deciles"]) == 10
    text = out.read_text()
    assert "subject_id" not in text and "person_id" not in text            # aggregate-only
    man = json.loads((tmp_path / "run_manifest.json").read_text())
    steps = {a["step"]: a["n"] for a in man["attrition"]}
    assert steps["cohort"] == 160 and steps["scored"] == 160 and man["extra"]["site_id"] == "S01"
    assert pk["feature_validation"]["n_missing_features"] == 0


def test_evaluate_is_deterministic(site_db, bundle, tmp_path):
    a = evaluate_site(bundle, site_db, tmp_path / "a.json", site_id="S01", log=lambda *_: None)
    b = evaluate_site(bundle, site_db, tmp_path / "b.json", site_id="S01", log=lambda *_: None)
    assert a == b


def test_missing_feature_column_names_the_column(site_db, tmp_path):
    spec = {"features": SPEC["features"] + [{"name": "lab_lactate_peak", "dtype": "float", "impute": 1.0}]}
    b = write_model_bundle(tmp_path / "b2", name="m", version="1", feature_spec=spec,
                           components=[{"kind": "logistic", "filename": "g.json",
                                        "content": {"intercept": 0.0, "coef": [0.1, 0.1, 0.1, 0.1]}}])
    with pytest.raises(EvaluationError, match="lab_lactate_peak"):
        evaluate_site(b, site_db, tmp_path / "p.json", site_id="S01", log=lambda *_: None)
    extra = pd.DataFrame({"subject_id": range(1, 161), "cohort_start_date": pd.Timestamp("2020-06-01"),
                          "lab_lactate_peak": 2.0})
    pk = evaluate_site(b, site_db, tmp_path / "p.json", site_id="S01", extra_features=extra, log=lambda *_: None)
    assert pk["n"] == 160


def test_source_preflight_failure_stops_before_scoring(site_db, bundle, tmp_path):
    bad = tmp_path / "src"
    shutil.copytree(SAMPLE, bad)
    pd.read_csv(bad / "encounter.csv", dtype=str).drop(columns=["ENC_TYPE"]).to_csv(bad / "encounter.csv", index=False)
    with pytest.raises(EvaluationError, match="ENC_TYPE"):
        evaluate_site(bundle, site_db, tmp_path / "p.json", site_id="S01", source_dir=bad, log=lambda *_: None)
    assert not (tmp_path / "p.json").exists()


def test_small_site_yields_a_suppressed_packet(tmp_path, bundle):
    db = tmp_path / "tiny.duckdb"
    make_cdm(db, n=25, seed=5)
    pk = evaluate_site(bundle, db, tmp_path / "p.json", site_id="S02", log=lambda *_: None)
    assert pk["suppressed"] and pk["metrics"] is None


def test_cli_evaluate_and_error_exit_code(site_db, bundle, tmp_path, capsys):
    out = tmp_path / "telemetry_packet.json"
    assert cli_main(["evaluate", "--bundle", str(bundle), "--db", str(site_db), "--site-id", "S09", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["site_id"] == "S09" and (tmp_path / "run_manifest.json").exists()
    assert cli_main(["evaluate", "--bundle", str(bundle), "--db", str(site_db), "--site-id", "Temple University",
                     "--out", str(tmp_path / "x.json")]) == 1
    assert "opaque code" in capsys.readouterr().err and not (tmp_path / "x.json").exists()
