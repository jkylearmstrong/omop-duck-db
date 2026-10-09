"""Deterministic Synthetic Multi-Site PCORnet Test Fixtures Generator (RFC-8).

Generates realistic mock PCORnet datasets across multiple hospital sites,
simulating real-world variations:
- Site 1: Standard PCORnet v4.0 schema (PATID, ENCOUNTERID).
- Site 2: Non-standard column headers (ssid, enc_id), missing optional columns
  (PROVIDERID, FACILITYID), unmapped LOINC codes, and outpatient/inpatient mix.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd


def generate_mock_pcornet_datasets(
    output_dir: str | Path,
    n_patients: int = 1000,
    seed: int = 42,
) -> dict[str, Path]:
    """Generates synthetic multi-site PCORnet CSV files.

    Args:
        output_dir: Destination directory.
        n_patients: Total patients across sites (default 1000).
        seed: Random seed for deterministic generation.

    Returns:
        dict[str, Path]: Mapping of site names to generated directory paths.
    """
    rng = np.random.default_rng(seed)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    n_site1 = n_patients // 2
    n_site2 = n_patients - n_site1

    # --- Site 1: Standard PCORnet ---
    site1_dir = root / "site1"
    site1_dir.mkdir(parents=True, exist_ok=True)

    # Demographic
    s1_patids = [f"P1_{i:04d}" for i in range(1, n_site1 + 1)]
    s1_birth_years = rng.integers(1940, 2005, size=n_site1)
    s1_genders = rng.choice(["M", "F"], size=n_site1)
    s1_races = rng.choice(["05", "03", "01", "NI"], size=n_site1)
    s1_ethnicities = rng.choice(["NH", "H", "NI"], size=n_site1)

    s1_demo = pd.DataFrame({
        "PATID": s1_patids,
        "BIRTH_DATE": [f"{yr}-05-15" for yr in s1_birth_years],
        "SEX": s1_genders,
        "RACE": s1_races,
        "HISPANIC": s1_ethnicities,
    })
    s1_demo.to_csv(site1_dir / "demographic.csv", index=False)

    # Encounter
    s1_encids = []
    s1_enc_patids = []
    s1_enc_types = []
    s1_admit_dates = []
    s1_disch_dates = []

    for pat in s1_patids:
        n_encs = rng.integers(1, 4)
        for e_idx in range(n_encs):
            enc_id = f"E1_{pat}_{e_idx}"
            s1_encids.append(enc_id)
            s1_enc_patids.append(pat)
            etype = rng.choice(["IP", "AV", "ED"])
            s1_enc_types.append(etype)
            admit_m = rng.integers(1, 10)
            admit_d = rng.integers(1, 28)
            admit_dt = f"2020-{admit_m:02d}-{admit_d:02d}"
            s1_admit_dates.append(admit_dt)
            # LOS: 1-5 days for IP, 0 for AV/ED
            los = rng.integers(1, 6) if etype == "IP" else 0
            disch_dt = f"2020-{admit_m:02d}-{(admit_d + los):02d}"
            s1_disch_dates.append(disch_dt)

    s1_enc = pd.DataFrame({
        "PATID": s1_enc_patids,
        "ENCOUNTERID": s1_encids,
        "ADMIT_DATE": s1_admit_dates,
        "DISCHARGE_DATE": s1_disch_dates,
        "ENC_TYPE": s1_enc_types,
        "PROVIDERID": [f"PR1_{i % 10}" for i in range(len(s1_encids))],
        "FACILITYID": ["FAC1"] * len(s1_encids),
        "DISCHARGE_STATUS": ["EX"] * len(s1_encids),
    })
    s1_enc.to_csv(site1_dir / "encounter.csv", index=False)

    # Diagnosis
    s1_dx = pd.DataFrame({
        "PATID": s1_enc_patids,
        "ENCOUNTERID": s1_encids,
        "DX": rng.choice(["I50.9", "E11.9", "I10", "J44.9", "N18.9"], size=len(s1_encids)),
        "DX_TYPE": ["10"] * len(s1_encids),
        "DX_DATE": s1_admit_dates,
        "DX_SOURCE": ["DI"] * len(s1_encids),
    })
    s1_dx.to_csv(site1_dir / "diagnosis.csv", index=False)

    # Vital
    s1_vital = pd.DataFrame({
        "PATID": s1_enc_patids,
        "ENCOUNTERID": s1_encids,
        "VITALID": [f"V1_{i}" for i in range(len(s1_encids))],
        "MEASURE_DATE": s1_admit_dates,
        "HT": rng.uniform(60, 75, size=len(s1_encids)).round(1),
        "WT": rng.uniform(130, 240, size=len(s1_encids)).round(1),
        "SYSTOLIC": rng.integers(100, 160, size=len(s1_encids)),
        "DIASTOLIC": rng.integers(60, 100, size=len(s1_encids)),
        "ORIGINAL_BMI": rng.uniform(20, 38, size=len(s1_encids)).round(1),
    })
    s1_vital.to_csv(site1_dir / "vital.csv", index=False)

    # --- Site 2: Non-standard headers & missing columns ---
    site2_dir = root / "site2"
    site2_dir.mkdir(parents=True, exist_ok=True)

    s2_patids = [f"P2_{i:04d}" for i in range(1, n_site2 + 1)]
    s2_birth_years = rng.integers(1945, 2002, size=n_site2)

    # Site 2 uses 'ssid' instead of 'PATID'
    s2_demo = pd.DataFrame({
        "ssid": s2_patids,
        "BIRTH_DATE": [f"{yr}-08-20" for yr in s2_birth_years],
        "SEX": rng.choice(["M", "F"], size=n_site2),
        "RACE": rng.choice(["05", "03", "NI"], size=n_site2),
        "HISPANIC": ["NH"] * n_site2,
    })
    s2_demo.to_csv(site2_dir / "demographic.csv", index=False)

    # Site 2 uses 'enc_id' instead of 'ENCOUNTERID' and omits PROVIDERID and FACILITYID
    s2_encids = [f"E2_{pat}_0" for pat in s2_patids]
    s2_admit = [f"2020-03-{(i % 25) + 1:02d}" for i in range(n_site2)]

    s2_enc = pd.DataFrame({
        "ssid": s2_patids,
        "enc_id": s2_encids,
        "ADMIT_DATE": s2_admit,
        "DISCHARGE_DATE": s2_admit,
        "ENC_TYPE": rng.choice(["IP", "AV"], size=n_site2),
        # Note: PROVIDERID and FACILITYID are intentionally omitted (testing null-padding)
    })
    s2_enc.to_csv(site2_dir / "encounter.csv", index=False)

    s2_dx = pd.DataFrame({
        "ssid": s2_patids,
        "enc_id": s2_encids,
        "DX": rng.choice(["I50.2", "E11.0", "I10", "428.0"], size=n_site2),
        "DX_TYPE": ["10"] * n_site2,
        "DX_DATE": s2_admit,
    })
    s2_dx.to_csv(site2_dir / "diagnosis.csv", index=False)

    s2_vital = pd.DataFrame({
        "ssid": s2_patids,
        "enc_id": s2_encids,
        "VITALID": [f"V2_{i}" for i in range(n_site2)],
        "MEASURE_DATE": s2_admit,
        "SYSTOLIC": rng.integers(105, 155, size=n_site2),
        "DIASTOLIC": rng.integers(65, 95, size=n_site2),
    })
    s2_vital.to_csv(site2_dir / "vital.csv", index=False)

    return {
        "site1": site1_dir,
        "site2": site2_dir,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate mock multi-site PCORnet test datasets.")
    parser.add_argument("--output-dir", "-o", default="tests/fixtures/mock_pcornet", help="Destination path.")
    parser.add_argument("--n-patients", "-n", type=int, default=1000, help="Total number of synthetic patients.")
    args = parser.parse_args()

    paths = generate_mock_pcornet_datasets(args.output_dir, n_patients=args.n_patients)
    print(f"Generated synthetic PCORnet data at {paths['site1']} and {paths['site2']}.")


if __name__ == "__main__":
    main()
