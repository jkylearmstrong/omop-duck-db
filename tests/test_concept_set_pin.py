"""Presets pin the lab / vital definitions, and the 0.29.0 labs reach the manuscript tables and document.

Synthetic data only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.concept_sets import (CONCEPT_SET_VERSION, LEGACY_CONCEPT_SETS, codes_by_name, concept_set_fingerprint,  # noqa: E402
                                   concept_set_version_fingerprint, legacy_feature_codes)
from omop_etl.manuscript_tables import CLINICAL_INCREMENTS, MANUSCRIPT_LABS, manuscript_completeness, manuscript_regression, manuscript_table1  # noqa: E402
from omop_etl.study_run import STUDY_DECISIONS, resolve_decisions, run_study  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402

NEW_LABS = {"lab_troponin_i": "Troponin I", "lab_lactate": "Lactate", "lab_pco2": "pCO2", "lab_po2": "pO2"}


def test_presets_pin_concept_sets():
    assert resolve_decisions("delivered_2026_11_11")["concept_sets"] == LEGACY_CONCEPT_SETS
    assert resolve_decisions("package")["concept_sets"] == "bundled"
    assert resolve_decisions("delivered_2026_11_11", concept_sets="bundled")["concept_sets"] == "bundled"
    assert CONCEPT_SET_VERSION in STUDY_DECISIONS["concept_sets"]["options"]
    with pytest.raises(ValueError):
        resolve_decisions("package", concept_sets="2020.01.1")


def test_legacy_codes_are_the_pre_0_29_lists():
    legacy = legacy_feature_codes()
    assert len(legacy["labs"]) == 20 and not set(legacy["labs"]) & {"troponin_i", "lactate", "pco2", "po2"}
    assert legacy["labs"]["alt"] == ["1742-6", "1743-4", "1744-2"] and legacy["labs"]["anion_gap"] == ["33037-3", "41276-7", "1863-0"]
    assert legacy["vitals"] == codes_by_name("vitals")  # vitals did not change in 0.29.0
    legacy["labs"]["alt"].append("x")  # a copy: the frozen lists cannot be changed by a caller
    assert legacy_feature_codes()["labs"]["alt"] == ["1742-6", "1743-4", "1744-2"]


def test_fingerprints():
    assert concept_set_version_fingerprint("bundled") == concept_set_fingerprint()
    assert concept_set_version_fingerprint(CONCEPT_SET_VERSION) == concept_set_fingerprint()
    leg = concept_set_version_fingerprint(LEGACY_CONCEPT_SETS)
    assert leg["version"] == LEGACY_CONCEPT_SETS and leg["sha256"] != concept_set_fingerprint()["sha256"]
    with pytest.raises(ValueError, match="not in this release"):
        concept_set_version_fingerprint("1999.01.1")


def _with_new_labs(df, seed=0):
    rng = np.random.default_rng(seed)
    df = df.copy()
    for c in NEW_LABS:
        v = rng.normal(5, 2, len(df))
        v[rng.random(len(df)) < 0.6] = np.nan  # measured in a minority of stays
        df[c] = v
    return df


def test_new_labs_in_manuscript_tables():
    assert set(NEW_LABS.values()) <= {lab for _, lab in MANUSCRIPT_LABS}
    df = _with_new_labs(make_features(n=600, seed=3))
    t1 = set(manuscript_table1(df)["Feature"])
    s2 = set(manuscript_completeness(df)["Feature"])
    t6 = manuscript_regression(df, increments="clinical").set_index("Feature")
    for lab in NEW_LABS.values():
        assert lab in t1 and f"{lab} not measured at index admission" in s2 and lab in t6.index
    assert t6.loc["Lactate", "Increment"] == CLINICAL_INCREMENTS["Lactate"][0]
    assert t6.loc["Troponin I", "Increment"] == "Per 1 SD"  # unit differs between assays
    # without the columns (legacy concept sets) there are no rows
    plain = make_features(n=600, seed=3).drop(columns=[c for c in NEW_LABS], errors="ignore")
    assert not set(NEW_LABS.values()) & set(manuscript_table1(plain)["Feature"])


@pytest.mark.parametrize("preset,expected", [("package", "bundled"), ("delivered_2026_11_11", LEGACY_CONCEPT_SETS)])
def test_run_study_uses_the_pinned_definitions(tmp_path, monkeypatch, preset, expected):
    from test_multisite_anonymous import _study

    import omop_etl.benchmark_features as bf

    db, keys = _study(tmp_path, monkeypatch, 3)
    fake = bf.extract_benchmark_features
    seen: list[dict] = []

    def capture(con, table, **kw):
        seen.append(kw)
        return fake(con, table, **kw)

    monkeypatch.setattr(bf, "extract_benchmark_features", capture)
    r = run_study(db, tmp_path / "o", sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, preset=preset, document=False)
    rec = json.loads((tmp_path / "o" / "Study_run.json").read_text(encoding="utf-8"))
    assert rec["decisions"]["concept_sets"] == expected
    assert rec["concept_sets"]["version"] == (LEGACY_CONCEPT_SETS if expected == LEGACY_CONCEPT_SETS else CONCEPT_SET_VERSION)
    assert len(seen) == 3
    if expected == LEGACY_CONCEPT_SETS:
        assert all(kw["lab_loincs"] == legacy_feature_codes()["labs"] and kw["vital_loincs"] == legacy_feature_codes()["vitals"] for kw in seen)
        assert "concept_set_coverage" in rec["data_checks"]["allowed"]
    else:
        assert all("lab_loincs" not in kw for kw in seen)
        assert "concept_set_coverage" not in rec["data_checks"]["allowed"]
    assert r["workbook"].exists()
