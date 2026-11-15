"""The worked example (inst/examples/study_walkthrough.py) runs end to end, so it cannot rot."""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("study_walkthrough", REPO_ROOT / "inst" / "examples" / "study_walkthrough.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_walkthrough_runs_and_every_step_gives_a_meaningful_result(tmp_path):
    r = _load().main(tmp_path, verbose=False)
    assert r["cohort_n"] > 200
    c = r["reconcile"]
    assert c["n_reference"] == c["both"] + c["reference_only"] and c["both"] > 0.8 * c["n_reference"]
    reasons = set(r["gaps"]["reason"])
    assert {"in_cohort", "not_in_cdm", "no_entry_event"} <= reasons               # the three kinds of patient the legacy list contains
    assert r["concept_id"] == 2001000000 and (tmp_path / "custom_concepts.json").exists()
    abl = r["ablation"]
    assert abl.loc["charlson_site", "auroc_delta_hi"] < 0                          # removing the useful feature hurts, beyond noise
    assert abl.loc["noise", "auroc_delta_lo"] <= 0.01                              # removing noise does not
    assert r["scored"] == r["cohort_n"] and "different_id" in r["refused"]         # the concept check passes at home, refuses elsewhere
