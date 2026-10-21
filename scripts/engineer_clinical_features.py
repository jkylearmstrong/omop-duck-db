#!/usr/bin/env python3
"""
eDERRI Clinical Velocity & Multi-Morbidity Interaction Feature Engineering
==========================================================================
Engineers high-leverage clinical risk biomarkers and velocity metrics from OMOP CDM:
  1. LACE Index Proxies & Acute Utilization Velocity
  2. Hemodynamic Instability & Acute Organ Dysfunction Markers
  3. High-Risk Cardio-Renal-Metabolic Interactions
  4. Medication Polypharmacy & Volatility Indices
"""

import sys
import time
from pathlib import Path
from typing import Tuple, List

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import StratifiedKFold
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss

ROOT_DIR = Path(__file__).resolve().parent.parent


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Computes clinical velocity, hemodynamic, and multi-morbidity interaction features."""
    feat = df.copy()

    # Convert any Decimal or object columns to float32
    for col in feat.columns:
        if feat[col].dtype == object or "decimal" in str(feat[col].dtype).lower():
            try:
                feat[col] = pd.to_numeric(feat[col], errors="coerce")
            except Exception:
                pass

    # 1. Acute Utilization Velocity & Density
    days_enc = feat["days_since_prior_encounter"].fillna(365.0).clip(lower=1.0)
    days_ip_ed = feat["days_since_prior_ip_ed"].fillna(365.0).clip(lower=1.0)

    feat["utilization_density"] = feat["prior_ip_os_ed_count"] / (days_enc / 30.0 + 1.0)
    feat["ip_velocity"] = feat["prior_ip_count"] / (days_ip_ed / 30.0 + 1.0)
    feat["ed_velocity"] = feat["prior_ed_count"] / (days_ip_ed / 30.0 + 1.0)
    feat["recent_ip_ratio"] = feat["prior_ip_count"] / (feat["prior_encounters_all_count"] + 1.0)
    feat["readmission_intensity"] = feat["readmission_count_twelve_months"] / (feat["prior_ip_count"] + 1.0)
    feat["mean_prior_ip_los"] = feat["prior_ip_los_days"] / (feat["prior_ip_count"] + 1.0)

    # 2. LACE Index Proxy
    los = feat["index_los_days"].fillna(1.0)
    lace_los = pd.Series(0, index=feat.index)
    lace_los[los == 1] = 1
    lace_los[los == 2] = 2
    lace_los[los == 3] = 3
    lace_los[(los >= 4) & (los <= 6)] = 4
    lace_los[(los >= 7) & (los <= 13)] = 5
    lace_los[los >= 14] = 7
    feat["lace_los_score"] = lace_los

    ed = feat["prior_ed_count"].fillna(0)
    lace_ed = pd.Series(0, index=feat.index)
    lace_ed[ed == 1] = 1
    lace_ed[ed == 2] = 2
    lace_ed[ed == 3] = 3
    lace_ed[ed >= 4] = 4
    feat["lace_ed_score"] = lace_ed

    comorb_cnt = feat["elix_pre_index_conditions_count"].fillna(0).clip(upper=5)
    feat["lace_comorb_score"] = comorb_cnt
    feat["lace_proxy_total"] = lace_los + lace_ed + comorb_cnt + 3.0  # +3 for acute inpatient index

    # 3. Hemodynamics & Acute Decompensation Biomarkers
    sbp = feat["systolic_bp"].fillna(120.0).clip(lower=60.0)
    dbp = feat["diastolic_bp"].fillna(80.0).clip(lower=30.0)
    hr = feat["heart_rate"].fillna(75.0)
    feat["shock_index"] = hr / sbp
    feat["pulse_pressure"] = sbp - dbp
    feat["mean_arterial_pressure"] = dbp + (feat["pulse_pressure"] / 3.0)

    # 4. Critical Lab Trajectory & Organ Dysfunction Ratios
    bun = feat["lab_bun"].fillna(15.0)
    creat = feat["lab_creatinine"].fillna(1.0).clip(lower=0.2)
    feat["bun_to_creatinine"] = bun / creat
    feat["bun_creat_azotemia_flag"] = (feat["bun_to_creatinine"] > 20.0).astype(float)

    hgb = feat["lab_hemoglobin"].fillna(13.0)
    feat["severe_anemia_flag"] = (hgb < 8.5).astype(float)

    alb = feat["lab_albumin"].fillna(3.8)
    feat["hypoalbuminemia_flag"] = (alb < 3.2).astype(float)

    egfr = feat["lab_egfr"].fillna(85.0)
    feat["egfr_stage4_5_flag"] = (egfr < 30.0).astype(float)

    na = feat["lab_sodium"].fillna(140.0)
    feat["hyponatremia_flag"] = (na < 135.0).astype(float)

    k = feat["lab_potassium"].fillna(4.2)
    feat["hyperkalemia_flag"] = (k > 5.1).astype(float)

    agap = feat["lab_anion_gap"].fillna(10.0)
    feat["elevated_anion_gap_flag"] = (agap > 14.0).astype(float)

    # 5. Polypharmacy & Cardio-Renal-Metabolic Interactions
    med_cols = [c for c in feat.columns if c.startswith("med_") and not c.endswith("_legacy")]
    feat["polypharmacy_count"] = feat[med_cols].sum(axis=1)

    if "elix_chf" in feat.columns and "elix_diabetes_complicated" in feat.columns:
        feat["combo_chf_dm"] = feat["elix_chf"] * feat["elix_diabetes_complicated"]
    if "elix_copd" in feat.columns and "med_systemic_corticosteroids" in feat.columns:
        feat["combo_copd_steroids"] = feat["elix_copd"] * feat["med_systemic_corticosteroids"]
    if "elix_renal_failure" in feat.columns:
        feat["combo_ckd_hyperk"] = feat["elix_renal_failure"] * feat["hyperkalemia_flag"]
    if "elix_chf" in feat.columns and "med_raas" in feat.columns:
        feat["combo_chf_raas"] = feat["elix_chf"] * feat["med_raas"]

    # 6. Site-Specific Base Risk (Target Encoding)
    site_rates = feat.groupby("site_source")["readmitted_30d"].transform("mean")
    feat["site_baseline_risk"] = site_rates.fillna(feat["readmitted_30d"].mean())

    return feat


def prepare_engineered_feature_matrix(
    parquet_path: Path,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Loads raw parquet, computes engineered features, and returns standardized matrix."""
    print(f"Loading {parquet_path.name} ...")
    df = pd.read_parquet(parquet_path)
    print(f"Engineering clinical biomarkers on {len(df):,} patients ...")
    t0 = time.time()
    df_eng = engineer_features(df)
    print(f"Feature engineering complete in {time.time() - t0:.1f}s.")

    y = df_eng["readmitted_30d"].values.astype(np.float32)

    id_cols = [
        "person_id", "admit_date", "discharge_date", "encounterid", "patid", "ssid",
        "site_source", "site_anon", "site_id",
        "med_biguanides_legacy", "med_insulins_legacy", "med_sulfonylureas_legacy",
        "med_raas_legacy", "med_antihypertensives_legacy", "med_beta_blockers_legacy",
        "med_systemic_corticosteroids_legacy", "med_lipid_modifying_legacy",
        "readmitted_30d"
    ]
    X_raw = df_eng.drop(columns=[c for c in id_cols if c in df_eng.columns])

    # Convert object to numeric or dummies
    cat_cols = X_raw.select_dtypes(include=["object", "category"]).columns.tolist()
    X_dummy = pd.get_dummies(X_raw, columns=cat_cols, drop_first=True)

    all_nan = [c for c in X_dummy.columns if X_dummy[c].isna().all()]
    if all_nan:
        X_dummy = X_dummy.drop(columns=all_nan)

    medians = X_dummy.median(numeric_only=True)
    X_imputed = X_dummy.fillna(medians).fillna(0.0)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imputed).astype(np.float32)
    feature_names = X_dummy.columns.tolist()

    print(f"Engineered feature matrix: {X_scaled.shape[1]} features (Added {X_scaled.shape[1] - 85} new biomarkers)")
    return X_scaled, y, feature_names


def main():
    parquet_path = Path("data/ederri_features_federated.parquet")
    if not parquet_path.exists():
        parquet_path = Path("derived/ederri_features/ederri_features_federated.parquet")
    if not parquet_path.exists():
        parquet_path = Path("data/ederri_features_clean_omop_federated.parquet")

    X, y, feature_names = prepare_engineered_feature_matrix(parquet_path)

    n_total = len(y)
    n_train = int(0.8 * n_total)
    indices = np.random.RandomState(42).permutation(n_total)
    train_idx, val_idx = indices[:n_train], indices[n_train:]

    X_train, y_train = X[train_idx], y[train_idx]
    X_val, y_val = X[val_idx], y[val_idx]

    print("\n" + "=" * 80)
    print("      BENCHMARKING ENGINEERED CLINICAL BIOMARKERS (GBDT ON DGX)")
    print("=" * 80)
    t0 = time.time()
    hgbm = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_leaf_nodes=45,
        min_samples_leaf=40,
        l2_regularization=1.5,
        random_state=42,
    )
    hgbm.fit(X_train, y_train)
    val_probs = hgbm.predict_proba(X_val)[:, 1]

    auc = roc_auc_score(y_val, val_probs)
    ap = average_precision_score(y_val, val_probs)
    brier = brier_score_loss(y_val, val_probs)

    print(f"Fit complete in {time.time() - t0:.1f}s")
    print(f"  Validation AUROC : {auc:.4f}")
    print(f"  Validation AUPRC : {ap:.4f}")
    print(f"  Validation Brier : {brier:.4f}")
    print(f"  Delta vs 0.7100 Baseline : {'+' if auc >= 0.71 else ''}{auc - 0.7100:.4f}")
    print("=" * 80)


if __name__ == "__main__":
    main()
