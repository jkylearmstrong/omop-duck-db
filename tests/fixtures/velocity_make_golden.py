"""Regenerates tests/fixtures/velocity_input.csv / velocity_golden.csv from scripts/engineer_clinical_features.py.

The golden file is whatever the reference script's ``engineer_features`` returns (minus its leaky section 6,
``site_baseline_risk``), so the packaged implementations (Python and R) are pinned to the script, not to this repo's reading
of it. Run from the repo root:  python tests/fixtures/velocity_make_golden.py
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ecf", ROOT / "scripts" / "engineer_clinical_features.py")
ecf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ecf)

rng = np.random.default_rng(7)
n = 120
los_edge = [0, 0.5, 1, 1.5, 2, 3, 3.5, 4, 6, 6.5, 7, 13, 13.5, 14, 30, np.nan]
df = pd.DataFrame({
    "person_id": np.arange(n),
    "days_since_prior_encounter": rng.choice([np.nan, 0, 0.5, 1, 7, 30, 90, 400], n),
    "days_since_prior_ip_ed": rng.choice([np.nan, 0, 2, 15, 60, 365, 500], n),
    "prior_ip_os_ed_count": rng.integers(0, 12, n),
    "prior_ip_count": rng.integers(0, 6, n),
    "prior_ed_count": rng.integers(0, 7, n),
    "prior_encounters_all_count": rng.integers(0, 20, n),
    "readmission_count_twelve_months": rng.integers(0, 4, n),
    "prior_ip_los_days": rng.integers(0, 40, n).astype(float),
    "index_los_days": [los_edge[i % len(los_edge)] for i in range(n)],
    "elix_pre_index_conditions_count": rng.choice([np.nan, 0, 1, 3, 5, 9], n),
    "systolic_bp": rng.choice([np.nan, 40, 90, 120, 180], n),
    "diastolic_bp": rng.choice([np.nan, 20, 60, 80, 110], n),
    "heart_rate": rng.choice([np.nan, 50, 75, 110, 140], n),
    "lab_bun": rng.choice([np.nan, 8, 15, 40, 90], n),
    "lab_creatinine": rng.choice([np.nan, 0.1, 0.6, 1.0, 3.5], n),
    "lab_hemoglobin": rng.choice([np.nan, 7.9, 8.5, 12, 15], n),
    "lab_albumin": rng.choice([np.nan, 2.5, 3.2, 4.0], n),
    "lab_egfr": rng.choice([np.nan, 15, 30, 60, 100], n),
    "lab_sodium": rng.choice([np.nan, 128, 135, 140], n),
    "lab_potassium": rng.choice([np.nan, 3.5, 5.1, 5.6], n),
    "lab_anion_gap": rng.choice([np.nan, 8, 14, 18], n),
    "elix_chf": rng.integers(0, 2, n), "elix_diabetes_complicated": rng.integers(0, 2, n),
    "elix_copd": rng.integers(0, 2, n), "elix_renal_failure": rng.integers(0, 2, n),
    "med_systemic_corticosteroids": rng.integers(0, 2, n), "med_raas": rng.integers(0, 2, n),
    "med_biguanides": rng.integers(0, 2, n), "med_biguanides_legacy": rng.integers(0, 2, n),
    "site_source": rng.choice(["A", "B", "C"], n), "readmitted_30d": rng.integers(0, 2, n),
})
out = ecf.engineer_features(df).drop(columns=["site_baseline_risk"])
df.to_csv(ROOT / "tests" / "fixtures" / "velocity_input.csv", index=False)
out.to_csv(ROOT / "tests" / "fixtures" / "velocity_golden.csv", index=False)
print(out.shape)
