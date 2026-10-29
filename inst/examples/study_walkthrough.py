"""A whole small study on a synthetic OMOP DuckDB, using the package's tools in the order a downstream study meets them.

    python inst/examples/study_walkthrough.py

1. Cohort       Run the index-stay cohort (a Circe definition) with the native compiler.
2. Reconcile    Compare it with a legacy patient list and say why patients are missing.
3. Own feature  Add a site-specific score to the CDM so FeatureExtraction / PLP can read it, and register its concept id.
4. Ablation     Judge the new feature against noise; compare two model runs.
5. Bundle       Package the model with the feature's concept id, score with the check, and see the check refuse a site where
                the same feature has another id.

Everything is synthetic and seeded; nothing leaves the process. Each step is a few lines you can copy into a study. The same
walkthrough in R is ``inst/examples/study_walkthrough.R``.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from omop_etl import (ablate_feature_groups, compare_model_runs, explain_cohort_gaps, reconcile_cohort,  # noqa: E402
                      ridge_logistic_fit_predict, stamp_feature_spec_concepts, validate_bundle_concepts)
from omop_etl.build_omop_cdm import build_schema  # noqa: E402
from omop_etl.circe import execute_circe_cohort  # noqa: E402
from omop_etl.circe_definitions import target_cohort_circe  # noqa: E402
from omop_etl.custom_covariates import export_custom_concept_registry, write_custom_features  # noqa: E402
from omop_etl.model_bundle import score_cohort, write_model_bundle  # noqa: E402


def synthetic_cdm(n: int = 400, seed: int = 1) -> tuple[duckdb.DuckDBPyConnection, pd.DataFrame]:
    """A small CDM: people with 1-3 stays, some children, some deaths, a few short observation periods."""
    rng = np.random.default_rng(seed)
    con = duckdb.connect()
    build_schema(con)
    person = pd.DataFrame({"person_id": np.arange(1, n + 1), "gender_concept_id": 8507, "year_of_birth": rng.integers(1930, 2012, n),
                           "race_concept_id": 0, "ethnicity_concept_id": 0})
    person["person_source_value"] = [f"MRN{i:05d}" for i in person["person_id"]]
    con.register("_p", person)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) "
                "SELECT person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value FROM _p")
    short = rng.random(n) < 0.08
    obs_end = np.where(short, pd.Timestamp("2019-06-30"), pd.Timestamp("2022-12-31"))
    con.register("_o", pd.DataFrame({"person_id": person["person_id"], "s": pd.Timestamp("2015-01-01"), "e": obs_end}))
    con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, "
                "period_type_concept_id) SELECT person_id, person_id, s, e, 0 FROM _o")
    stays = []
    for pid in person["person_id"]:
        for _ in range(int(rng.integers(1, 4))):
            start = pd.Timestamp("2018-01-01") + pd.Timedelta(days=int(rng.integers(0, 900)))
            stays.append((int(pid), int(rng.choice([9201, 262], p=[0.85, 0.15])), start, start + pd.Timedelta(days=int(rng.integers(2, 10)))))
    visits = pd.DataFrame(stays, columns=["person_id", "visit_concept_id", "s", "e"])
    visits.insert(0, "visit_occurrence_id", np.arange(1, len(visits) + 1))
    con.register("_v", visits)
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                "visit_type_concept_id) SELECT visit_occurrence_id, person_id, visit_concept_id, s, e, 0 FROM _v")
    dead = person.sample(frac=0.06, random_state=seed)
    last = visits.groupby("person_id")["e"].max()
    con.register("_d", pd.DataFrame({"person_id": dead["person_id"], "d": [last[p] + pd.Timedelta(days=int(rng.integers(-3, 40))) for p in dead["person_id"]]}))
    con.execute("INSERT INTO death (person_id, death_date, death_type_concept_id) SELECT person_id, d, 0 FROM _d")
    return con, person


def main(out_dir: str | Path | None = None, verbose: bool = True) -> dict[str, Any]:
    say = print if verbose else (lambda *a, **k: None)
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="omop_walkthrough_"))
    rng = np.random.default_rng(7)
    con, person = synthetic_cdm()
    result: dict[str, Any] = {}

    # 1. Cohort: a Circe definition, evaluated by the native compiler (no Java, no WebAPI).
    definition = target_cohort_circe()
    cohort = execute_circe_cohort(con, definition)
    say(f"1. cohort: {cohort['subject_id'].nunique()} index stays from {len(person)} people")
    result["cohort_n"] = int(cohort["subject_id"].nunique())

    # 2. Reconcile with a legacy list: most of our cohort, some people the cohort rightly excludes, some ids that do not exist.
    members = person[person["person_id"].isin(cohort["subject_id"])]["person_source_value"].tolist()
    others = person[~person["person_id"].isin(cohort["subject_id"])]["person_source_value"].tolist()
    legacy = pd.DataFrame({"person_source_value": members[: int(0.9 * len(members))] + others[:25] + ["MRN99999", "MRN99998"]})
    rec = reconcile_cohort(con, legacy, definition=definition, min_cell=3)
    say(f"2. reconcile: {rec['counts']['both']} of {rec['counts']['n_reference']} legacy patients found; why the rest are missing:")
    say(rec["gaps"][["reason", "detail", "n", "pct"]].to_string(index=False))
    result["reconcile"] = rec["counts"]
    result["gaps"] = rec["gaps"]

    # 3. Your own feature: write it to the CDM so FeatureExtraction / PLP / this package read it like any other covariate.
    ref = cohort.merge(person[["person_id", "year_of_birth"]], left_on="subject_id", right_on="person_id")
    age = pd.to_datetime(ref["cohort_start_date"]).dt.year - ref["year_of_birth"]
    comorbidity = rng.poisson(np.clip((age - 30) / 25, 0.2, 3))            # the burden the site score measures, not just age
    charlson_site = np.clip(comorbidity + np.round(rng.normal(0, 0.5, len(ref))), 0, 10)
    feats = pd.DataFrame({"person_id": ref["subject_id"], "date": pd.to_datetime(ref["cohort_start_date"]).dt.date, "charlson_site": charlson_site})
    concepts = write_custom_features(con, feats, labels={"charlson_site": "Charlson index, site definition"})
    registry = export_custom_concept_registry(con, out / "custom_concepts.json")
    say(f"3. own feature: charlson_site is concept {concepts['charlson_site'][0]}; registry written to {out / 'custom_concepts.json'}")
    result["concept_id"] = int(concepts["charlson_site"][0])

    # 4. Is the feature worth keeping, and are two model runs really different?
    x = pd.DataFrame({"age": age.to_numpy(float), "charlson_site": charlson_site, "noise": rng.normal(size=len(ref))})
    z = 0.02 * (x["age"] - 60) + 0.6 * comorbidity - 1.2
    y = (rng.uniform(size=len(x)) < 1 / (1 + np.exp(-z))).astype(int)
    abl = ablate_feature_groups(x, y, {"charlson_site": ["charlson_site"], "noise": ["noise"]}, n_boot=100, seed=2)
    drop = abl[abl["mode"] == "drop"].set_index("group")
    say("4. ablation (change in AUROC when the group is removed; 95% interval):")
    for g, r in drop.iterrows():
        say(f"   {g:<14} {r['auroc_delta']:+.3f}  [{r['auroc_delta_lo']:+.3f}, {r['auroc_delta_hi']:+.3f}]")
    result["ablation"] = drop
    cut = int(0.7 * len(x))
    p_full = ridge_logistic_fit_predict(x.iloc[:cut].to_numpy(), y[:cut], x.iloc[cut:].to_numpy())
    p_small = ridge_logistic_fit_predict(x[["age"]].iloc[:cut].to_numpy(), y[:cut], x[["age"]].iloc[cut:].to_numpy())
    runs = [pd.DataFrame({"person_id": ref["subject_id"].iloc[cut:].to_numpy(), "pred": p, "outcome": y[cut:]}) for p in (p_small, p_full)]
    cmp = compare_model_runs(runs[0], runs[1], names=("age only", "with charlson_site"), n_boot=200)
    d = cmp["difference"].set_index("metric").loc["auroc"]
    say(f"   AUROC {d['age only']:.3f} -> {d['with charlson_site']:.3f}  (difference {d['delta']:+.3f}, interval [{d['delta_lo']:+.3f}, {d['delta_hi']:+.3f}])")
    result["model_difference"] = d

    # 5. Package the model with the concept id it was trained with; the check refuses a site where the id means something else.
    spec = stamp_feature_spec_concepts({"features": [{"name": "age", "dtype": "float", "bounds": [0, 120], "impute": 60.0},
                                                      {"name": "charlson_site", "dtype": "float", "bounds": [0, 30], "impute": 0.0}]}, con)
    bundle = write_model_bundle(out / "bundle", name="walkthrough", version="1", feature_spec=spec,
                                components=[{"name": "glm", "kind": "logistic", "filename": "glm.json",
                                             "content": {"intercept": -1.2, "coef": [0.03, 0.45]}}])
    new = pd.DataFrame({"person_id": ref["subject_id"], "age": x["age"], "charlson_site": x["charlson_site"]})
    scores = score_cohort(bundle, new, check_concepts=con)
    say(f"5. bundle: scored {len(scores)} patients at the training site (concept check passed)")
    other_site, _ = synthetic_cdm(seed=2)
    write_custom_features(other_site, feats.iloc[:5].assign(elixhauser_site=1.0).drop(columns="charlson_site"))     # another feature got 2001000000 first
    write_custom_features(other_site, feats.iloc[:5])
    try:
        score_cohort(bundle, new, check_concepts=other_site)
        refused = ""
    except ValueError as err:
        refused = str(err)
    say(f"   at a site where charlson_site has another id: {refused[:110]}...")
    result.update({"scored": len(scores), "refused": refused, "registry": registry})
    return result


if __name__ == "__main__":
    main()
