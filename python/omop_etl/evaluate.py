"""One-shot consortium evaluation (``omop-duckdb evaluate``).

Chains the partner steps that were separate commands:

1. ``check_source``           - preflight of a raw source directory (optional, ``source_dir``)
2. cohort                     - ``define_study_cohort`` (index inpatient stay, 30-day readmission) or an existing cohort table
3. ``extract_velocity_features`` (+ any ``extra_features``)
4. ``validate_feature_frame`` - against the bundle's feature contract (``on_violation`` raise | clip | nullify)
5. ``score_cohort``           - hash-verified bundle
6. ``site_telemetry``         - aggregate-only packet (small-cell suppressed), written to ``out``
7. ``run_manifest.json``      - next to the packet (versions, seeds, input hashes, step counts)

Only the packet and manifest leave the site; no row-level value is written. Outcomes come from the cohort's
``outcome_flag`` column.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from omop_etl.cohort import define_study_cohort
from omop_etl.feature_contract import validate_feature_frame
from omop_etl.model_bundle import load_model_bundle, score_cohort
from omop_etl.run_manifest import RunManifest
from omop_etl.source_check import check_source, format_report
from omop_etl.telemetry import site_telemetry
from omop_etl.velocity import extract_velocity_features

__all__ = ["evaluate_site", "EvaluationError"]


class EvaluationError(RuntimeError):
    """A stage of the evaluation failed; the message names the stage."""


def evaluate_site(
    bundle: str | Path,
    db: str | Path,
    out: str | Path,
    *,
    site_id: str,
    source_dir: str | Path | None = None,
    source_format: str | None = None,
    cohort_table: str | None = None,
    extra_features: str | Path | pd.DataFrame | None = None,
    on_violation: str = "raise",
    min_cell: int = 11,
    seed: int = 42,
    followup_days: int = 30,
    persist_keys: str | Path | None = None,
    manifest: str | Path | None = None,
    log=print,
) -> dict[str, Any]:
    """Runs the whole chain and returns the telemetry packet (also written to ``out``)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(site_id)):
        raise EvaluationError("site_id must be an opaque code matching [A-Za-z0-9_-]{1,32} (not a facility name).")
    out = Path(out)
    man_path = Path(manifest) if manifest else out.with_name("run_manifest.json")
    m = RunManifest("evaluate", seeds={"random_state": seed}, inputs=[Path(bundle) / "manifest.json"],
                    feature_spec=Path(bundle) / "feature_spec.json", extra={"site_id": site_id})

    counter = {"n": 0}

    def stage(name: str):
        counter["n"] += 1
        log(f"[{counter['n']}] {name}")

    # 1. preflight
    if source_dir is not None:
        stage("check_source")
        rep = check_source(source_dir, source_format)
        log(format_report(rep))
        if not rep["ok"]:
            raise EvaluationError("check_source failed:\n  " + "\n  ".join(rep["errors"]))
        m.add_step("check_source", len(rep["tables"]))

    stage("load model bundle")
    b = load_model_bundle(bundle)

    con = duckdb.connect(str(db), read_only=True)
    try:
        # 2. cohort
        stage("cohort")
        if cohort_table:
            cohort = con.execute(f"SELECT * FROM {cohort_table}").df()
            if "outcome_flag" not in cohort.columns:
                raise EvaluationError(f"cohort table {cohort_table!r} needs an outcome_flag column for the telemetry labels.")
        else:
            cohort = define_study_cohort(con, visit_type="inpatient", followup_days=followup_days, random_state=seed,
                                         materialise=False, attrition=True, persist_keys=persist_keys)
            m.add_attrition(cohort.attrs.get("attrition", []))
        cohort = cohort.dropna(subset=["outcome_flag"]).reset_index(drop=True)
        if cohort.empty:
            raise EvaluationError("the cohort is empty; nothing to evaluate.")
        m.add_step("cohort", len(cohort))
        con.register("_eval_cohort", cohort[[c for c in ("subject_id", "cohort_start_date", "cohort_end_date",
                                                         "visit_occurrence_id") if c in cohort.columns]])
        # 3. features
        stage("extract_velocity_features")
        extra = None
        if extra_features is not None:
            extra = extra_features if isinstance(extra_features, pd.DataFrame) else (
                pd.read_parquet(extra_features) if str(extra_features).lower().endswith(".parquet") else pd.read_csv(extra_features))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # absent optional inputs are surfaced by the contract check below
            feats = extract_velocity_features(con, "_eval_cohort", extra_features=extra)
    finally:
        con.close()
    keep = ["subject_id", "cohort_start_date"] + [c for c in ("visit_occurrence_id",) if c in cohort.columns]
    frame = cohort[keep + ["outcome_flag"]].merge(feats, on=["subject_id", "cohort_start_date"], how="left")
    m.add_step("features", len(frame))

    # 4. contract
    stage("validate_feature_frame")
    try:
        report = validate_feature_frame(frame, b.feature_spec, on_violation)
    except Exception as exc:
        rep_obj = getattr(exc, "report", None)
        raise EvaluationError("feature contract violated (supply missing columns with --extra-features, or choose "
                              f"--on-violation clip|nullify for out-of-range values):\n{rep_obj.summary() if rep_obj else exc}") from exc
    log(report.summary())

    # 5. score
    stage("score_cohort")
    scores = score_cohort(b, frame, on_violation=on_violation)
    if len(scores) != len(frame):
        raise EvaluationError("scored row count differs from the cohort.")
    m.add_step("scored", len(scores))

    # 6. telemetry
    stage("site_telemetry")
    packet = site_telemetry(scores["prob"].to_numpy(), frame["outcome_flag"].to_numpy(), site_id, min_cell=min_cell,
                            validation=report, model={"name": re.sub(r"[^A-Za-z0-9_. -]", "_", str(b.manifest["name"]))[:64],
                                   "version": re.sub(r"[^A-Za-z0-9_.+-]", "_", str(b.manifest["version"]))[:32]},
                            output=out)
    m.add_input(out)
    m.write(man_path)
    log(f"packet -> {out}\nmanifest -> {man_path}" + (" (suppressed: too few events/non-events)" if packet["suppressed"] else ""))
    return packet
