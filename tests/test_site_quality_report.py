"""scripts/site_quality_report.py: per-site data quality of a run (Dr. Rubin and Kyle, 2026-11-23). Made-up sites and counts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import site_quality_report as sq  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402


def _run(tmp_path, with_db=True):
    duckdb = pytest.importorskip("duckdb")
    priv, db = tmp_path / "priv", tmp_path / "db"
    priv.mkdir()
    db.mkdir()
    for i, s in enumerate(["s1", "s2", "s3"]):
        d = make_features(n=600, seed=i)
        if s == "s3":  # one site records no discharge status
            d[[c for c in d if c.startswith("disp_")]] = 0
        d.to_parquet(priv / f"features_{s}.parquet")
        pd.DataFrame({"step_name": ["All", "Follow-up", "Window"], "subjects_retained": [1000, 900 - 300 * (s == "s2"), 600]}).to_csv(
            priv / f"attrition_{s}.csv", index=False)
        con = duckdb.connect(str(db / f"omop_{s}.duckdb"))
        con.execute("CREATE TABLE measurement AS SELECT * FROM (VALUES (3004249, 8876, 120.0, DATE '2018-01-01', '8480-6'), "
                    f"({0 if s == 's1' else 3004249}, 8876, {999.0 if s == 's1' else 130.0}, DATE '2018-01-01', 'XYZ')) "
                    "t(measurement_concept_id, unit_concept_id, value_as_number, measurement_date, measurement_source_value)")
        con.execute("CREATE TABLE drug_exposure AS SELECT * FROM (VALUES (1, DATE '2018-02-01', 'rx1')) t(drug_concept_id, drug_exposure_start_date, drug_source_value)")
        con.execute("CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (201826, DATE '2018-02-01', 'E11.9')) "
                    "t(condition_concept_id, condition_start_date, condition_source_value)")
        con.close()
    (tmp_path / "labels.json").write_text(json.dumps({"s1": "Site A", "s2": "Site B", "s3": "Site C"}), encoding="utf-8")
    args = ["--private-dir", str(priv), "--labels", str(tmp_path / "labels.json"), "--detail", str(tmp_path / "out" / "detail.md"),
            "--flags", str(tmp_path / "out" / "flags.json")] + (["--db-dir", str(db)] if with_db else [])
    assert sq.main(args) == 0
    return (tmp_path / "out" / "detail.md").read_text(encoding="utf-8"), json.loads((tmp_path / "out" / "flags.json").read_text(encoding="utf-8"))


def test_flags_the_site_that_differs_without_naming_it(tmp_path):
    detail, fl = _run(tmp_path)
    measures = {f["measure"]: f for f in fl["flags"]}
    assert measures["Discharge Status: Missing / Unknown"]["max"] == 100.0 and measures["Discharge Status: Missing / Unknown"]["min"] < 50
    assert "Follow-up % excluded" in measures  # attrition step where one site loses far more
    assert "measurement % concept_id 0" in measures
    text = json.dumps(fl)
    assert "Site " not in text and "s1" not in text  # shareable: ranges only
    for section in ("## Table 1 categories", "## Attrition", "## Laboratories: % missing", "## Medications: % yes",
                    "## Prior utilization: mean", "## Unmapped rows", "## Measurements outside the physiologic limits", "Site C"):
        assert section in detail, section


def test_runs_without_the_databases(tmp_path):
    detail, fl = _run(tmp_path, with_db=False)
    assert "## Unmapped rows" not in detail and fl["sites"] == 3
