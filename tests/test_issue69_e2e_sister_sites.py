"""Issue 69 end-to-end sister-site walkthrough test (0.36.0, M5): 7 synthetic sister sites from raw PCORnet CSVs ->
etl_pcornet() -> reproduce_study.py -> anonymous privacy check -> relabel_study.py -> privacy check.
All data and site names are completely synthetic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_run_privacy  # noqa: E402
import relabel_study  # noqa: E402
import reproduce_study  # noqa: E402
from omop_etl import build_schema, etl_pcornet, study_report as sr  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

SISTER_KEYS = ["northvale", "eastbrook", "westfield", "southport", "midland", "crestview", "harborview"]
SISTER_NAMES = [
    "Northvale Regional Health",
    "Eastbrook Medical Center",
    "Westfield University Hospital",
    "Southport General Hospital",
    "Midland Memorial Health",
    "Crestview Clinic System",
    "Harborview Academic Health",
]


def _build_central_vocab(vocab_path: Path) -> None:
    con = duckdb.connect(str(vocab_path))
    build_schema(con)
    con.execute("""
        INSERT INTO concept VALUES
        (201826, 'Type 2 diabetes mellitus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (45581352, 'Type 2 diabetes mellitus without complications', 'Condition', 'ICD10CM', '4-char billing code', NULL, 'E11.9', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (316866, 'Hypertensive disorder', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '38341003', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (45576876, 'Essential hypertension', 'Condition', 'ICD10CM', '3-char billing code', NULL, 'I10', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (21600716, 'Biguanides', 'Drug', 'ATC', 'ATC 4th', 'C', 'A10BA', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900001, 'NDC syringe', 'Device', 'NDC', '11-digit NDC', NULL, '00001000203', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4200001, 'Insulin syringe', 'Device', 'SNOMED', 'Physical Object', 'S', 'SYR1', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3036277, 'Body height', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8302-2', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3025315, 'Body weight', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '29463-7', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3038553, 'Body mass index (BMI) [Ratio]', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '39156-5', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004249, 'Systolic blood pressure', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8480-6', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3012888, 'Diastolic blood pressure', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8462-4', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004501, 'Glucose [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3016723, 'Creatinine [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2160-0', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3019550, 'Sodium [Moles/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2951-2', DATE '1970-01-01', DATE '2099-12-31', NULL)
    """)
    con.execute("""
        INSERT INTO concept_relationship VALUES
        (45581352, 201826, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (45576876, 316866, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900001, 4200001, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL)
    """)
    con.execute("""
        INSERT INTO concept_ancestor
        SELECT concept_id, concept_id, 0, 0 FROM concept WHERE standard_concept = 'S'
        UNION ALL SELECT 21600716, 1503297, 1, 1
    """)
    con.close()


def _write_pcornet_sister_site(raw_dir: Path, site_idx: int, n_patients: int = 36) -> None:
    rng = np.random.default_rng(100 + site_idx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    demo_rows = ["PATID,BIRTH_DATE,SEX,RACE,HISPANIC"]
    enc_rows = ["ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,DISCHARGE_STATUS"]
    dx_rows = ["DIAGNOSISID,PATID,ENCOUNTERID,DX_TYPE,DX,DX_DATE,PDX"]
    lab_rows = ["LAB_RESULT_CM_ID,PATID,ENCOUNTERID,LAB_LOINC,RESULT_DATE,RESULT_NUM,RAW_RESULT,RESULT_UNIT"]
    vit_rows = ["VITALID,PATID,ENCOUNTERID,MEASURE_DATE,HT,WT,BMI,SYSTOLIC,DIASTOLIC,SMOKING,TOBACCO"]
    med_rows = ["MEDADMINID,PATID,ENCOUNTERID,MEDADMIN_START_DATE,MEDADMIN_TYPE,MEDADMIN_CODE,MEDADMIN_DOSE_ADMIN,MEDADMIN_DOSE_ADMIN_UNIT"]
    addr_rows = ["ADDRESSID,PATID,ADDRESS_STATE,ADDRESS_ZIP5,ADDRESS_PREFERRED,ADDRESS_PERIOD_START"]

    for i in range(1, n_patients + 1):
        pid = f"S{site_idx}_P{i:03d}"
        byr = int(rng.integers(1940, 1995))
        sex = "F" if rng.random() < 0.5 else "M"
        race = "05" if rng.random() < 0.65 else "03"
        demo_rows.append(f"{pid},{byr}-06-15,{sex},{race},N")
        addr_rows.append(f"A_{pid},{pid},PA,19140,Y,2015-01-01")

        # Wash-in outpatient visit >= 400 days before index stay + prior-year inpatient/outpatient visits
        enc_rows.append(f"E0_{pid},{pid},AV,2018-01-10,09:00,2018-01-10,10:00,HO")
        if rng.random() < 0.4:
            enc_rows.append(f"E_PIP_{pid},{pid},IP,2018-09-10,09:00,2018-09-13,14:00,HO")
        if rng.random() < 0.7:
            enc_rows.append(f"EU_{pid},{pid},AV,2019-03-15,09:00,2019-03-15,10:00,HO")
        # Index inpatient stay in 2019-06
        los = int(rng.integers(2, 9))
        enc_rows.append(f"E1_{pid},{pid},IP,2019-06-01,08:00,2019-06-0{los},15:00,HO")
        readm = rng.random() < 0.25
        if readm:
            # 30-day readmission post-discharge
            enc_rows.append(f"E2_{pid},{pid},IP,2019-06-17,11:00,2019-06-20,14:00,HO")
        else:
            # Verified follow-up outpatient visit > 30 days after discharge
            enc_rows.append(f"E2_{pid},{pid},AV,2019-08-15,10:00,2019-08-15,11:00,HO")

        dx_rows.append(f"DX1_{pid},{pid},E1_{pid},10,E11.9,2019-06-01,P")
        if rng.random() < 0.55:
            dx_rows.append(f"DX2_{pid},{pid},E1_{pid},10,I10,2019-06-02,S")

        # Labs including censored HbA1c (>14.0) to exercise 0.36.0 converter rules
        hba1c_num = "" if i % 5 == 0 else f"{float(rng.uniform(6.6, 10.5)):.1f}"
        hba1c_raw = ">14.0" if i % 5 == 0 else hba1c_num
        lab_rows.append(f"L1_{pid},{pid},E1_{pid},4548-4,2019-06-02,{hba1c_num},{hba1c_raw},%")
        lab_rows.append(f"L2_{pid},{pid},E1_{pid},2345-7,2019-06-02,{int(rng.integers(110, 220))},,mg/dL")
        lab_rows.append(f"L3_{pid},{pid},E1_{pid},2160-0,2019-06-02,{float(rng.uniform(0.7, 2.1)):.2f},,mg/dL")
        lab_rows.append(f"L4_{pid},{pid},E1_{pid},2951-2,2019-06-02,{int(rng.integers(135, 144))},,mmol/L")

        tob = "01" if i % 3 == 0 else "02"
        vit_rows.append(f"V1_{pid},{pid},E1_{pid},2019-06-01,68,185,{float(rng.uniform(22, 36)):.1f},{int(rng.integers(115, 155))},{int(rng.integers(68, 92))},NI,{tob}")

        # MED_ADMIN: pre-admission + in-stay RxNorm drug (metformin 6809) and NDC device (syringe 00001000203 -> routed to device_exposure)
        med_rows.append(f"M0_{pid},{pid},EU_{pid},2019-03-15,RX,6809,500,mg")
        med_rows.append(f"M1_{pid},{pid},E1_{pid},2019-06-02,RX,6809,500,mg")
        if i % 3 == 0:
            med_rows.append(f"M2_{pid},{pid},E1_{pid},2019-06-02,ND,00001000203,1,each")

    (raw_dir / "demographic.csv").write_text("\n".join(demo_rows) + "\n", encoding="utf-8")
    (raw_dir / "encounter.csv").write_text("\n".join(enc_rows) + "\n", encoding="utf-8")
    (raw_dir / "diagnosis.csv").write_text("\n".join(dx_rows) + "\n", encoding="utf-8")
    (raw_dir / "lab_result_cm.csv").write_text("\n".join(lab_rows) + "\n", encoding="utf-8")
    (raw_dir / "vital.csv").write_text("\n".join(vit_rows) + "\n", encoding="utf-8")
    (raw_dir / "med_admin.csv").write_text("\n".join(med_rows) + "\n", encoding="utf-8")
    (raw_dir / "lds_address_history.csv").write_text("\n".join(addr_rows) + "\n", encoding="utf-8")


def test_issue69_seven_sister_sites_end_to_end_from_raw_pcornet(tmp_path, monkeypatch):
    """M5: 7 synthetic sister sites built from raw PCORnet -> reproduce_study -> check_run_privacy -> relabel_study -> check_run_privacy."""
    # Keep tuning grid fast for CI while exercising the real ETL, cohort, feature extraction, LOSO, workbook, document & privacy pipeline
    monkeypatch.setattr(
        sr, "default_model_grid",
        lambda *a, **kw: {"Logistic Regression": (True, [("C=1.0 (benchmark)", lambda: LogisticRegression(max_iter=200))])},
    )

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    vocab_path = db_dir / "central_vocabulary.duckdb"
    _build_central_vocab(vocab_path)

    for idx, key in enumerate(SISTER_KEYS, start=1):
        raw_site = tmp_path / "raw" / key
        _write_pcornet_sister_site(raw_site, idx, n_patients=36)
        site_db = db_dir / f"omop_{key}.duckdb"
        con = duckdb.connect(str(site_db))
        build_schema(con)
        con.close()
        etl_pcornet(str(raw_site), str(site_db), central_vocab=str(vocab_path), data_cutoff="2020-12-31", key_check="ignore")

        # Verify M2 domain routing occurred during the fresh build (Device rows moved out of drug_exposure)
        check_con = duckdb.connect(str(site_db), read_only=True)
        assert check_con.execute("SELECT count(*) FROM device_exposure WHERE device_concept_id = 4200001").fetchone()[0] > 0
        assert check_con.execute("SELECT count(*) FROM drug_exposure WHERE drug_concept_id = 4200001").fetchone()[0] == 0
        check_con.close()

    priv_dir = tmp_path / "private"
    priv_dir.mkdir()
    names_file = priv_dir / "site_names.txt"
    names_file.write_text("\n".join(SISTER_NAMES + SISTER_KEYS) + "\n", encoding="utf-8")
    labels_v1 = priv_dir / "labels_v1.json"
    out_v1 = tmp_path / "out_shareable"

    rc_study = reproduce_study.main([
        "--db-dir", str(db_dir),
        "--out", str(out_v1),
        "--private-dir", str(priv_dir / "work"),
        "--sites", *SISTER_KEYS,
        "--label-seed", "7",
        "--labels-out", str(labels_v1),
        "--start", "2019-06-01",
        "--end", "2019-06-05",
        "--profile", "none",
        "--bootstraps", "5",
        "--allow-data-gaps", "feature_skipped", "column_missing", "vitals",
        "--names-file", str(names_file),
    ])
    assert rc_study == 0
    run_meta = json.loads((out_v1 / "Study_run.json").read_text(encoding="utf-8"))
    assert run_meta["n_sites"] == 7
    assert run_meta["labels"] == [f"Site {c}" for c in "ABCDEFG"]
    assert run_meta["privacy_check"]["hits"] == 0

    # Standalone privacy scan as an outside auditor would run it
    assert check_run_privacy.main(["--dir", str(out_v1), "--names-file", str(names_file)]) == 0

    # Relabel with relabel_study.py under a fresh random permutation and re-run privacy scan
    labels_v2 = priv_dir / "labels_v2.json"
    out_v2 = tmp_path / "out_relabelled"
    rc_relabel = relabel_study.main([
        "--run-dir", str(out_v1),
        "--out", str(out_v2),
        "--old-labels", str(labels_v1),
        "--new-labels", "random",
        "--label-seed", "42",
        "--labels-out", str(labels_v2),
        "--names-file", str(names_file),
    ])
    assert rc_relabel == 0
    map1 = json.loads(labels_v1.read_text(encoding="utf-8"))
    map2 = json.loads(labels_v2.read_text(encoding="utf-8"))
    assert map1 != map2 and set(map2.values()) == {f"Site {c}" for c in "ABCDEFG"}
    assert check_run_privacy.main(["--dir", str(out_v2), "--names-file", str(names_file)]) == 0
