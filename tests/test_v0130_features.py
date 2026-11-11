"""v0.13.0: admit-date bounds and sample_stay_rule, benchmark feature extractor, deep-learning dataset, frozen vocabulary,
intra-stay tokens, pre-training targets, LOSO transport, recalibration and capacity thresholds.
Synthetic data only (tests/_bf_fixture.py generates every row)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bf_fixture import LAB, VITAL, make_db  # noqa: E402
from conftest import import_or_skip  # noqa: E402
from omop_etl import (  # noqa: E402
    adjust_for_prevalence, capacity_risk_tiers, capacity_threshold, define_study_cohort, evaluate_at_capacity,
    evaluate_loso_transport, export_concept_vocabulary, extract_benchmark_features, extract_intra_stay_tokens,
    extract_multimodal_deep_dataset, fit_recalibrator, import_concept_vocabulary, isotonic_apply, isotonic_fit,
    make_pretraining_targets, map_risk_tiers, platt_scaling_apply, platt_scaling_fit,
)

COHORT = dict(min_age=18, washin_days=0, min_los_days=0, followup_days=30, require_verified_followup=False)


@pytest.fixture(scope="module")
def con():
    c = make_db()
    define_study_cohort(c, materialise=True, table_name="stays_all", sample_stay_rule="all", **COHORT)
    define_study_cohort(c, materialise=True, table_name="cohort_t", **COHORT)
    return c


# ---- cohort: admit-date bounds, sample_stay_rule ---------------------------------------------------------------------------

def test_admit_date_bounds_equal_study_window_and_show_in_attrition(con):
    a = define_study_cohort(con, materialise=False, min_admit_date="2019-01-01", max_admit_date="2020-12-31", **COHORT)
    b = define_study_cohort(con, materialise=False, study_window=("2019-01-01", "2020-12-31"), **COHORT)
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True), check_like=True)
    assert a["cohort_start_date"].min() >= pd.Timestamp("2019-01-01") and a["cohort_start_date"].max() <= pd.Timestamp("2020-12-31")
    full = define_study_cohort(con, materialise=False, **COHORT)
    assert len(a) < len(full)
    assert a.attrs["definition"]["study_window"] == ["2019-01-01", "2020-12-31"]
    steps = [r["step_name"] for r in a.attrs["attrition"]]
    assert any("2019-01-01" in s and "2020-12-31" in s for s in steps)
    stays = define_study_cohort(con, materialise=False, min_admit_date="2019-01-01", sample_stay_rule="all", **COHORT)
    assert any(r["stays_dropped"] > 0 for r in stays.attrs["attrition"])       # dropped stays are logged too


def test_admit_date_conflict_raises(con):
    with pytest.raises(ValueError, match="disagrees"):
        define_study_cohort(con, materialise=False, study_window=("2019-01-01", None), min_admit_date="2018-01-01", **COHORT)
    with pytest.raises(ValueError):
        define_study_cohort(con, materialise=False, min_admit_date="2021-01-01", max_admit_date="2019-01-01", **COHORT)


def test_sample_stay_rule(con):
    first = define_study_cohort(con, materialise=False, sample_stay_rule="earliest", **COHORT)
    last = define_study_cohort(con, materialise=False, sample_stay_rule="latest", **COHORT)
    assert (first["cohort_start_date"].to_numpy() <= last["cohort_start_date"].to_numpy()).all()
    r1 = define_study_cohort(con, materialise=False, sample_stay_rule="random", random_seed=7, **COHORT)
    r2 = define_study_cohort(con, materialise=False, sample_stay_rule="random", random_seed=7, **COHORT)
    assert r1["visit_occurrence_id"].tolist() == r2["visit_occurrence_id"].tolist()
    assert r1.attrs["definition"]["sampling_rule"] == "stable_random"
    seeds = {tuple(define_study_cohort(con, materialise=False, sample_stay_rule="random", random_seed=s, **COHORT)["visit_occurrence_id"])
             for s in range(6)}
    assert len(seeds) > 1
    everything = define_study_cohort(con, materialise=False, sample_stay_rule="all", **COHORT)
    assert len(everything) == 120 and everything.groupby("subject_id").size().eq(3).all()
    assert (everything["stay_rank"] == 1).sum() == 40
    with pytest.raises(ValueError):
        define_study_cohort(con, materialise=False, sample_stay_rule="newest", **COHORT)


# ---- benchmark features ------------------------------------------------------------------------------------------------------

KW = dict(vital_concept_ids={k: [v] for k, v in VITAL.items()})


def test_extract_benchmark_features_groups_and_dual_labs(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    groups = df.attrs["feature_groups"]
    assert set(groups) == {"demographics", "elixhauser", "medications", "utilization", "vitals", "labs", "disposition"}
    assert len(df) == 40 and df["person_id"].is_unique
    assert len([c for c in groups["elixhauser"] if c.startswith("elix_")]) >= 31
    assert len(groups["medications"]) == 8
    assert {"vital_systolic_bp", "vital_diastolic_bp", "vital_bmi"} <= set(df.columns)
    for lab in ("creatinine", "sodium"):
        assert df[f"lab_{lab}_last"].notna().all()
        assert (df[f"lab_{lab}_last"] >= df[f"lab_{lab}"]).all()        # synthetic values rise day by day
    assert {"lab_egfr", "lab_egfr_last", "lab_anion_gap", "lab_anion_gap_last"} <= set(df.columns)
    assert (df["lab_egfr_last"] <= df["lab_egfr"]).all()                # creatinine rises -> eGFR falls
    assert df[[c for c in df.columns if c.startswith("disp_")]].sum(axis=1).eq(1).all()
    assert "y" in df and set(df["y"].unique()) <= {0, 1}


def test_features_never_use_data_after_discharge(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    # the fixture plants creatinine = 99 three days after every discharge; no creatinine may reach it
    assert df["lab_creatinine"].max() < 20 and df["lab_creatinine_last"].max() < 20
    assert not any(c.lower() in ("outcome_date", "outcome_flag") for c in df.columns)


def test_benchmark_features_toggles_and_order(con):
    df = extract_benchmark_features(con, "cohort_t", include_elixhauser=False, include_medications=False, include_vitals=False,
                                    include_labs=False, include_utilization=False, include_disposition=False)
    assert set(df.attrs["feature_groups"]) == {"demographics"}
    stays = extract_benchmark_features(con, "stays_all", include_elixhauser=False, include_medications=False, **KW)
    ids = con.execute("SELECT visit_occurrence_id FROM stays_all").df()["visit_occurrence_id"]
    assert stays["visit_occurrence_id"].tolist() == ids.tolist()        # cohort order preserved
    assert "prior_ip_count" in stays and stays["prior_ip_count"].max() >= 1


def test_include_sdoh_requires_inputs_or_falls_back_to_an_indicator(con):
    with pytest.raises(ValueError, match="sdi_cut_points"):
        extract_benchmark_features(con, "cohort_t", include_sdoh=True, sdoh_fallback="raise", **KW)
    with pytest.warns(UserWarning, match="sdoh_missing"):
        df = extract_benchmark_features(con, "cohort_t", include_sdoh=True, **KW)
    assert (df["sdoh_missing"] == 1).all() and df["sdi_quintile"].isna().all() and df.attrs["feature_groups"]["sdoh"]
    with pytest.raises(ValueError, match="sdoh_fallback"):
        extract_benchmark_features(con, "cohort_t", include_sdoh=True, sdoh_fallback="ignore", **KW)


def test_impute_uses_frozen_training_values(con):
    raw = extract_benchmark_features(con, "cohort_t", **KW)
    raw.loc[raw.index[:5], "lab_sodium"] = np.nan
    assert raw["lab_sodium"].isna().any()
    train = extract_benchmark_features(con, "cohort_t", impute="median", **KW)
    assert not train[[c for c in train.columns if c.startswith(("lab_", "vital_"))]].isna().any().any()
    vals = train.attrs["imputation_values"]
    assert set(vals) == set(train.attrs["feature_groups"]["vitals"] + train.attrs["feature_groups"]["labs"])
    other = extract_benchmark_features(con, "cohort_t", impute_values={**vals, "lab_sodium": 123.0}, **KW)
    assert other.attrs["imputation_values"]["lab_sodium"] == 123.0
    with pytest.raises(ValueError, match="impute"):
        extract_benchmark_features(con, "cohort_t", impute="mode", **KW)


def test_no_fragmentation_warning(con):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        extract_benchmark_features(con, "cohort_t", **KW)


def test_adjacent_central_vocabulary_is_attached(tmp_path):
    import duckdb
    from _bf_fixture import make_db
    main = make_db()
    main.execute("COPY concept_ancestor TO '" + str(tmp_path / "anc.parquet").replace("\\", "/") + "'")
    main.execute("DELETE FROM concept_ancestor")
    # build a database file whose vocabulary lives in a neighbouring central_vocabulary.duckdb
    vocab = duckdb.connect(str(tmp_path / "central_vocabulary.duckdb"))
    vocab.execute("CREATE TABLE concept_ancestor AS SELECT * FROM read_parquet('" + str(tmp_path / "anc.parquet").replace("\\", "/") + "')")
    vocab.execute("CREATE TABLE concept AS SELECT * FROM (SELECT 1 AS concept_id, 'x' AS concept_name, 'LOINC' AS vocabulary_id, 'a' AS concept_code)")
    vocab.close()
    main.execute(f"EXPORT DATABASE '{str(tmp_path / 'exp').replace(chr(92), '/')}'")
    db = duckdb.connect(str(tmp_path / "site.duckdb"))
    db.execute(f"IMPORT DATABASE '{str(tmp_path / 'exp').replace(chr(92), '/')}'")
    db.execute("DELETE FROM concept_ancestor")
    define_study_cohort(db, materialise=True, table_name="cohort_t", **COHORT)
    with pytest.warns(UserWarning, match="attached the central vocabulary"):
        df = extract_benchmark_features(db, "cohort_t", include_vitals=False, include_labs=False)
    assert len(df.attrs["feature_groups"]["medications"]) == 8
    db.close()                                                                      # the ATTACH lives with the instance
    off = duckdb.connect(str(tmp_path / "site.duckdb"))
    with pytest.raises(ValueError, match="not reachable"):
        extract_benchmark_features(off, "cohort_t", include_vitals=False, include_labs=False, include_elixhauser=False, vocabulary_db=None)


# ---- vocabulary --------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def features(con):
    return extract_benchmark_features(con, "cohort_t", **KW)


def test_vocabulary_round_trip_and_tamper(con, features, tmp_path):
    ds = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=6)
    tok = ds["tokenizer"]
    manifest = export_concept_vocabulary(tok, tmp_path / "vocab.json")
    assert manifest["vocab_size"] == tok.vocab_size == ds["vocab_size"] and ds["mask_token_id"] == tok.vocab_size
    again = import_concept_vocabulary(tmp_path / "vocab.json")
    assert list(again.concept_list) == list(tok.concept_list)
    ds2 = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=6, tokenizer=again)
    np.testing.assert_array_equal(ds["concept_tensor"], ds2["concept_tensor"])
    bad = (tmp_path / "vocab.json").read_text().replace('"201826 - condition', '"201827 - condition')
    (tmp_path / "bad.json").write_text(bad)
    with pytest.raises(ValueError, match="corrupted"):
        import_concept_vocabulary(tmp_path / "bad.json")
    (tmp_path / "other.json").write_text('{"format": "x"}')
    with pytest.raises(ValueError, match="not a concept vocabulary"):
        import_concept_vocabulary(tmp_path / "other.json")


def test_frozen_vocabulary_drops_unknown_concepts(con, features):
    from omop_etl.ml_features import build_concept_tokenizer
    tok = build_concept_tokenizer(["999999 - condition - nothing"])
    ds = extract_multimodal_deep_dataset(con, features, max_visits=3, max_tokens_per_visit=4, tokenizer=tok)
    assert (ds["concept_tensor"] == 3).all()                           # nothing in the frozen vocabulary matches


# ---- deep dataset ------------------------------------------------------------------------------------------------------------

def test_multimodal_numpy_dict(con, features):
    ds = extract_multimodal_deep_dataset(con, features, max_visits=5, max_tokens_per_visit=8, domains=("condition",))
    n = len(features)
    assert ds["concept_tensor"].shape == (n, 5, 8) and ds["times"].shape == (n, 5)
    assert ds["dense_covariates"].shape[0] == n and ds["dense_covariates"].shape[1] == len(ds["dense_columns"])
    assert "y" not in ds["dense_columns"] and "person_id" not in ds["dense_columns"]
    np.testing.assert_array_equal(ds["person_id"], features["person_id"].to_numpy())
    np.testing.assert_array_equal(ds["y"], features["y"].to_numpy())
    # a patient with prior stays has prior conditions tokenized; first-stay patients have none
    prior = features["prior_ip_count"].to_numpy()
    np.testing.assert_array_equal(ds["n_visits"], np.minimum(prior, 5))   # one condition per prior stay, same 365-day window
    assert ds["dense_missing"].shape == ds["dense_covariates"].shape


def test_torch_dict(con, features):
    torch = import_or_skip("torch")
    ds = extract_multimodal_deep_dataset(con, features, max_visits=3, max_tokens_per_visit=4, output_format="torch_dict")
    assert isinstance(ds["concept_tensor"], torch.Tensor) and ds["concept_tensor"].dtype == torch.long
    assert {"concept_tensor", "times", "dense_covariates", "y", "person_id"} <= set(ds)


def test_memmap_chunks_equal_in_memory(con, features, tmp_path):
    mem = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, pretraining_targets=True, seed=11)
    mm = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, pretraining_targets=True, seed=11,
                                         output_format="memmap", output_dir=tmp_path / "mm", chunk_size=1)   # rounds up to 1024
    for key in ("concept_tensor", "times", "dense_covariates", "masked_concept_tensor", "mlm_labels", "next_visit_flag", "person_id"):
        np.testing.assert_array_equal(np.asarray(mm[key]), mem[key])
    assert isinstance(mm["concept_tensor"], np.memmap)
    assert (tmp_path / "mm" / "manifest.json").exists() and (tmp_path / "mm" / "vocabulary.json").exists()


def test_arrow_fixed_size_lists(con, features, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, output_format="arrow", output_dir=tmp_path / "a")
    t = pq.read_table(out["files"][0])
    assert pa.types.is_fixed_size_list(t.schema.field("concept_tensor").type)
    assert pa.types.is_fixed_size_list(t.schema.field("concept_tensor").type.value_type)
    assert t.num_rows == len(features)


def test_multi_chunk_vocabulary_is_shared(con, features, tmp_path, monkeypatch):
    import omop_etl.deep_dataset as dd
    monkeypatch.setattr(dd, "_BLOCK", 8)             # force several chunks on 40 rows
    mem = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5)
    mm = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, output_format="memmap",
                                         output_dir=tmp_path / "m", chunk_size=8)
    np.testing.assert_array_equal(np.asarray(mm["concept_tensor"]), mem["concept_tensor"])


def test_missing_columns_and_formats(con, features, tmp_path):
    with pytest.raises(ValueError, match="person_id"):
        extract_multimodal_deep_dataset(con, features.drop(columns=["person_id"]))
    with pytest.raises(ValueError, match="output_dir"):
        extract_multimodal_deep_dataset(con, features, output_format="memmap")
    with pytest.raises(ValueError, match="output_format"):
        extract_multimodal_deep_dataset(con, features, output_format="parquet")


def test_zarr_equals_in_memory(con, features, tmp_path):
    pytest.importorskip("zarr")
    mem = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, pretraining_targets=True, seed=3)
    z = extract_multimodal_deep_dataset(con, features, max_visits=4, max_tokens_per_visit=5, pretraining_targets=True, seed=3,
                                        output_format="zarr", output_dir=tmp_path / "z", chunk_size=1)
    for key in ("concept_tensor", "times", "dense_covariates", "masked_concept_tensor", "mlm_labels", "person_id"):
        np.testing.assert_array_equal(z[key][:], mem[key])
    assert z["concept_tensor"].shape == mem["concept_tensor"].shape


# ---- self-supervised targets -------------------------------------------------------------------------------------------------

def test_pretraining_targets_properties():
    rng = np.random.default_rng(0)
    t = rng.integers(0, 60, size=(3000, 4, 6)).astype("int32")
    t[t < 5] = 3
    nv = rng.integers(0, 5, size=3000)
    r = make_pretraining_targets(t, nv, vocab_size=60, mask_prob=0.15, seed=1)
    sel = r["mlm_labels"] != -100
    assert (r["mlm_labels"][sel] == t[sel]).all() and (t[sel] >= 5).all()        # padding / specials are never selected
    assert abs(sel.sum() / (t >= 5).sum() - 0.15) < 0.01
    masked_share = (r["masked_concept_tensor"][sel] == 60).mean()
    assert abs(masked_share - 0.8) < 0.03
    assert (r["masked_concept_tensor"][~sel] == t[~sel]).all()
    r2 = make_pretraining_targets(t, nv, vocab_size=60, mask_prob=0.15, seed=1)
    np.testing.assert_array_equal(r["masked_concept_tensor"], r2["masked_concept_tensor"])
    assert r["next_visit_flag"].shape == (3000, 4)
    assert (r["next_visit_flag"].sum(axis=1) == np.maximum(nv - 1, 0)).all()
    with pytest.raises(ValueError):
        make_pretraining_targets(t, nv, vocab_size=60, row_offset=5)


# ---- intra-stay tokens -------------------------------------------------------------------------------------------------------

def test_intra_stay_tokens_stay_inside_the_admission(con, features):
    res = extract_intra_stay_tokens(con, features, max_days=10, max_tokens_per_day=12)
    tens, day = res["intra_tensor"], res["intra_day"]
    los = (pd.to_datetime(features["discharge_date"]) - pd.to_datetime(features["index_date"])).dt.days.to_numpy()
    assert tens.shape == (len(features), 10, 12)
    assert (day.max(axis=1) <= los).all()                              # nothing after the discharge day
    used = (tens != 3).any(axis=2)
    assert (used.sum(axis=1) > 0).all()
    toks = res["tokenizer"].concept_list
    assert any("lab:" in t for t in toks) and any("labtrend:up" in t for t in toks)
    # the planted post-discharge creatinine (99.0) would be flagged 'H' on a day after discharge; days beyond los stay padding
    for i in range(len(features)):
        assert not used[i, los[i] + 1:].any()


def test_intra_stay_in_dataset(con, features):
    ds = extract_multimodal_deep_dataset(con, features, max_visits=3, max_tokens_per_visit=4, include_intra_stay=True, intra_max_days=6)
    assert ds["intra_tensor"].shape == (len(features), 6, 30) and "intra_tokenizer" in ds


def test_intra_stay_measurements_are_continuous_and_inside_the_stay(con, features):
    from omop_etl import extract_intra_stay_measurements
    res = extract_intra_stay_measurements(con, features, concept_ids=list(LAB.values()), max_days=8, max_obs_per_day=6)
    obs, mask = res["intra_obs"], res["intra_obs_mask"]
    n = len(features)
    assert obs.shape == (n, 8, 6, 3) and mask.shape == (n, 8, 6) and res["concepts"] == list(LAB.values())
    los = (pd.to_datetime(features["discharge_date"]) - pd.to_datetime(features["index_date"])).dt.days.to_numpy()
    t = np.where(mask, obs[..., 0], -1)
    assert (t.max(axis=(1, 2)) <= los + 1).all() and (t[mask] >= 0).all()          # nothing after the discharge day
    assert (obs[..., 1][mask] >= 1).all() and (obs[~mask] == np.array([-1, 0, 0])).all()
    z = obs[..., 2][mask]
    cidx = obs[..., 1][mask].astype(int)
    for k in range(1, 5):                                                         # z-scores: mean 0 and sd 1 per concept
        assert abs(z[cidx == k].mean()) < 1e-3 and abs(z[cidx == k].std() - 1) < 1e-3
    assert abs(float(res["normalization"][LAB["creatinine"]][0]) - 3.5) < 3         # planted 99 is not included
    again = extract_intra_stay_measurements(con, features, concept_ids=list(LAB.values()), max_days=8, max_obs_per_day=6,
                                            normalization=res["normalization"])
    np.testing.assert_array_equal(again["intra_obs"], obs)
    top = extract_intra_stay_measurements(con, features, top_concepts=2)
    assert len(top["concepts"]) == 2


# ---- recalibration, capacity -------------------------------------------------------------------------------------------------

def _sim(n=20000, prev=0.15, seed=0):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < prev).astype(float)
    s = rng.normal(loc=y * 1.2, scale=1.0)
    return y, s


def test_prior_shift_matches_base_rate():
    y, s = _sim()
    p_bal = 1 / (1 + np.exp(-(s - 0.6)))                                 # a "balanced-trained" score
    p = adjust_for_prevalence(p_bal, 0.5, float(y.mean()))
    assert p.mean() < p_bal.mean()
    assert abs(p.mean() - y.mean()) < 0.05
    with pytest.raises(ValueError):
        adjust_for_prevalence(p_bal, 0.0, 0.1)


def test_platt_and_isotonic_calibrate():
    y, s = _sim()
    p = 1 / (1 + np.exp(-(s - 0.6)))
    pl = platt_scaling_apply(p, platt_scaling_fit(y, p))
    iso = isotonic_apply(p, isotonic_fit(y, p))
    for q in (pl, iso):
        assert abs(q.mean() - y.mean()) < 0.01
        assert abs(((q - y) ** 2).mean() - ((p - y) ** 2).mean()) < 0.2 and ((q - y) ** 2).mean() < ((p - y) ** 2).mean()
    assert np.all(np.diff(isotonic_fit(y, p)["y"]) >= -1e-12)            # monotone
    assert np.corrcoef(iso, p)[0, 1] > 0.9
    with pytest.raises(ValueError):
        platt_scaling_fit(np.zeros(10), np.linspace(0, 1, 10))
    assert fit_recalibrator("none").predict([0.2, 0.4]).tolist() == [0.2, 0.4]


def test_isotonic_is_pool_adjacent_violators():
    fit = isotonic_fit([0, 1, 0, 1, 1, 0, 1], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    out = isotonic_apply([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7], fit)
    assert np.allclose(out[:3], [0, 0.5, 0.5]) and np.all(np.diff(out) >= 0)


def test_capacity_threshold_and_tiers():
    y, s = _sim()
    p = 1 / (1 + np.exp(-s))
    cut = capacity_threshold(p, 0.10)
    assert abs((p >= cut).mean() - 0.10) < 0.001
    ev = evaluate_at_capacity(y, p, 0.10)
    assert ev["ppv"] > y.mean() and ev["lift"] > 1 and 0 < ev["sensitivity"] < 1
    tiers = capacity_risk_tiers(p, 0.10)
    names = map_risk_tiers(p, tiers)
    assert abs((names == "high_risk").mean() - 0.10) < 0.001
    frozen = evaluate_at_capacity(y, p, 0.10, cutoff=cut)
    assert frozen["cutoff"] == cut
    with pytest.raises(ValueError):
        capacity_threshold(p, 1.5)


# ---- LOSO --------------------------------------------------------------------------------------------------------------------

def _sites(seed=2):
    rng = np.random.default_rng(seed)
    rows = []
    for site, prev, n in (("A", 0.10, 3000), ("B", 0.18, 2500), ("C", 0.14, 2800)):
        x1, x2 = rng.normal(size=n), rng.normal(size=n)
        logit = -2.4 + 0.9 * x1 + 0.5 * x2 + (prev - 0.14) * 6
        y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
        rows.append(pd.DataFrame({"site_anon": site, "y": y, "x1": x1, "x2": x2, "person_id": np.arange(n)}))
    return pd.concat(rows, ignore_index=True)


def test_loso_logistic_balanced_training_natural_scoring():
    df = _sites()
    res = evaluate_loso_transport(df, "site_anon", model_type="logistic", balance_ratio=1.0, score_natural=True, n_bootstraps=20,
                                  keep_predictions=True)
    ps = res.per_site
    assert sorted(ps["site"]) == ["A", "B", "C"]
    assert (ps["train_prevalence_balanced"].round(3) == 0.5).all()
    assert (ps["auroc"] > 0.65).all() and (ps["auroc_lo"] <= ps["auroc"]).all() and (ps["auroc"] <= ps["auroc_hi"]).all()
    assert ps["n"].tolist() == [3000, 2500, 2800]                         # held-out site scored at natural prevalence (every row)
    assert (ps["oe_ratio"].between(0.6, 1.6)).all()                      # O/E tracks (held-out prevalence / training-site prevalence): no outcome data of the held-out site is used
    uncal = evaluate_loso_transport(df, "site_anon", calibration="none", n_bootstraps=0)
    assert (uncal.per_site["oe_ratio"] < 0.6).all()                       # balanced scores overpredict (O/E well below 1)
    assert "top10_ppv" in ps and (ps["top10_lift"] > 1).all()
    assert len(res.predictions) == len(df) and res.summary["metric"].str.contains("auroc").any()
    assert "person_id" not in res.settings["features"]


def test_loso_is_deterministic_and_validates():
    df = _sites()
    a = evaluate_loso_transport(df, "site_anon", n_bootstraps=10, random_state=5).per_site
    b = evaluate_loso_transport(df, "site_anon", n_bootstraps=10, random_state=5).per_site
    pd.testing.assert_frame_equal(a, b)
    with pytest.raises(ValueError, match="at least two sites"):
        evaluate_loso_transport(df[df["site_anon"] == "A"], "site_anon")
    with pytest.raises(ValueError, match="calibration"):
        evaluate_loso_transport(df, "site_anon", calibration="magic", n_bootstraps=0)
    with pytest.raises(ValueError, match="model_type"):
        evaluate_loso_transport(df, "site_anon", model_type="forest", n_bootstraps=0)


@pytest.mark.parametrize("cal", ["platt", "isotonic"])
def test_loso_fitted_calibrators(cal):
    res = evaluate_loso_transport(_sites(), "site_anon", calibration=cal, n_bootstraps=0)
    assert (res.per_site["oe_ratio"].between(0.6, 1.6)).all()


def test_loso_xgboost_when_installed():
    pytest.importorskip("xgboost")
    res = evaluate_loso_transport(_sites(), "site_anon", model_type="xgboost", n_bootstraps=0,
                                  model_params={"n_estimators": 40})
    assert (res.per_site["auroc"] > 0.65).all()


@pytest.mark.parametrize("model", ["hist_gbm", "random_forest"])
def test_loso_sklearn_models(model):
    pytest.importorskip("sklearn")
    df = _sites()
    df.loc[df.index[::7], "x1"] = np.nan                                             # NaN handling
    res = evaluate_loso_transport(df, "site_anon", model_type=model, n_bootstraps=0,
                                  model_params={"max_iter": 40} if model == "hist_gbm" else {"n_estimators": 40})
    assert (res.per_site["auroc"] > 0.65).all()


# ---- v0.14 deep-dataset additions ------------------------------------------------------------------------------------------

def test_torch_y_is_float_and_targets(con, features):
    torch = import_or_skip("torch")
    f = features.assign(los=3.5, tte=10.0, ev=1)
    ds = extract_multimodal_deep_dataset(con, f, max_visits=3, max_tokens_per_visit=4, output_format="torch_dict",
                                         target_cols={"los_days": "los", "time_to_event": "tte", "event_observed": "ev"})
    assert ds["y"].dtype == torch.float32
    torch.nn.BCEWithLogitsLoss()(torch.zeros(len(f)), ds["y"])           # used to raise on int64
    assert ds["los_days"].shape == (len(f),) and float(ds["event_observed"][0]) == 1.0
    with pytest.raises(ValueError, match="target_cols"):
        extract_multimodal_deep_dataset(con, features, target_cols={"a": "nope"})


def test_time_deltas_and_ragged_and_measurements(con, features):
    ds = extract_multimodal_deep_dataset(con, features, max_visits=5, max_tokens_per_visit=8, domains=("condition",),
                                         time_deltas=True, ragged=True, include_intra_measurements=True)
    d = ds["visit_days_before_index"]
    np.testing.assert_allclose(ds["years_before_index"], np.where(d < 0, 0, d / 365.25), rtol=1e-5)
    assert (ds["delta_years"] >= 0).all() and (ds["delta_years"][d < 0] == 0).all()
    ct = ds["concept_tensor"]
    assert len(ds["ragged_tokens"]) == int((ct != 3).sum()) and ds["ragged_row_offsets"][-1] == len(ds["ragged_tokens"])
    assert ds["intra_obs"].shape[0] == len(features) and "intra_obs_concepts" in ds


def test_ragged_rejects_streaming_formats(con, features, tmp_path):
    with pytest.raises(ValueError, match="ragged"):
        extract_multimodal_deep_dataset(con, features, output_format="memmap", output_dir=tmp_path, ragged=True)
