"""0.36.0 hardening tests (M1, M2, M3, S1, S4): all data are synthetic."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from omop_etl import (  # noqa: E402
    backfill_all,
    backfill_med_admin,
    build_schema,
    etl_pcornet,
    repair_routed_med_admin_values,
    route_drug_domains,
)

RSCRIPT_SHELL = os.name == "nt"


def _seed_vocab_for_routing(con) -> None:
    """Seeds standard concepts across Drug, Device, Procedure, Observation, and Measurement domains."""
    con.execute("""
        INSERT INTO concept VALUES
        (1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900001, 'NDC syringe', 'Device', 'NDC', '11-digit NDC', NULL, '00001000203', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4200001, 'Insulin syringe', 'Device', 'SNOMED', 'Physical Object', 'S', 'SYR1', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900002, 'IV infusion code', 'Procedure', 'HCPCS', 'HCPCS', 'S', 'J9999', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900003, 'Care obs Rx', 'Observation', 'RxNorm', 'Clinical Drug', NULL, '99901', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4300001, 'Care observation', 'Observation', 'SNOMED', 'Context-dependent', 'S', 'OBS1', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900004, 'Glucose test strip NDC', 'Measurement', 'NDC', '11-digit NDC', NULL, '00009000807', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004501, 'Glucose serum/plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004410, 'Hemoglobin A1c', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (45581352, 'Type 2 diabetes mellitus', 'Condition', 'ICD10CM', '4-char billing code', NULL, 'E11.9', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (201826, 'Type 2 diabetes mellitus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', DATE '1970-01-01', DATE '2099-12-31', NULL)
    """)
    con.execute("""
        INSERT INTO concept_relationship VALUES
        (900001, 4200001, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900003, 4300001, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900004, 3004501, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (45581352, 201826, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL)
    """)
    con.execute("""
        INSERT INTO concept_ancestor
        SELECT concept_id, concept_id, 0, 0 FROM concept WHERE standard_concept = 'S'
    """)


def test_m1_dose_kept_out_of_value_as_number_and_repair(tmp_path):
    """M1: routed MED_ADMIN rows set value_as_number=NULL on measurement/observation, keeping dose in value_source_value."""
    db = tmp_path / "m1.duckdb"
    con = duckdb.connect(str(db))
    build_schema(con)
    _seed_vocab_for_routing(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) "
                "VALUES (10, 8507, 1970, 0, 0, 'P10')")

    # 1. Seed a misplaced Observation row and Measurement row in drug_exposure (with quantity + unit) and call route_drug_domains
    con.execute("""
        INSERT INTO drug_exposure (
            drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
            drug_type_concept_id, quantity, drug_source_value, drug_source_concept_id, dose_unit_source_value
        ) VALUES
        (1, 10, 4300001, DATE '2021-02-01', DATE '2021-02-01', 38000180, 25.0, '99901', 900003, 'mg'),
        (2, 10, 3004501, DATE '2021-02-02', DATE '2021-02-02', 38000180, 12.5, '00009000807', 900004, 'strip')
    """)
    r = route_drug_domains(con)
    assert r["observation"] == 1 and r["measurement"] == 1 and r["total_routed"] == 2
    obs_row = con.execute("SELECT value_as_number, value_source_value, unit_source_value FROM observation").fetchone()
    meas_row = con.execute("SELECT value_as_number, value_source_value, unit_source_value FROM measurement").fetchone()
    assert obs_row == (None, "25", "mg")
    assert meas_row == (None, "12.5", "strip")

    # 2. Also verify backfill_med_admin directly sets value_as_number=NULL and preserves raw dose text
    con.execute("DELETE FROM observation; DELETE FROM measurement; DELETE FROM drug_exposure")
    ma = tmp_path / "med_admin.csv"
    ma.write_text(
        "MEDADMINID,PATID,MEDADMIN_START_DATE,MEDADMIN_TYPE,MEDADMIN_CODE,MEDADMIN_DOSE_ADMIN,MEDADMIN_DOSE_ADMIN_UNIT\n"
        "M1,P10,2021-03-01,RX,99901,50,mL\n"
        "M2,P10,2021-03-02,ND,00009000807,2,unit\n",
        encoding="utf-8",
    )
    res = backfill_med_admin(con, ma)
    assert res["observation"] == 1 and res["measurement"] == 1
    assert con.execute("SELECT value_as_number, value_source_value, unit_source_value FROM observation").fetchone() == (None, "50", "mL")
    assert con.execute("SELECT value_as_number, value_source_value, unit_source_value FROM measurement").fetchone() == (None, "2", "unit")

    # 3. Simulate a 0.35.3 database that stored dose in value_as_number and verify repair_routed_med_admin_values()
    con.execute("UPDATE observation SET value_as_number = 50.0, value_source_value = NULL WHERE observation_type_concept_id = 38000180")
    con.execute("UPDATE measurement SET value_as_number = 2.0, value_source_value = NULL WHERE measurement_type_concept_id = 38000180")
    con.close()

    ro = duckdb.connect(str(db), read_only=True)
    dry = repair_routed_med_admin_values(ro, dry_run=True)
    assert dry == {"measurement_cleared": 1, "observation_cleared": 1, "total_cleared": 2}
    ro.close()

    rw = duckdb.connect(str(db))
    rep = repair_routed_med_admin_values(rw)
    assert rep == {"measurement_cleared": 1, "observation_cleared": 1, "total_cleared": 2}
    assert rw.execute("SELECT value_as_number, value_source_value FROM observation").fetchone() == (None, "50")
    assert rw.execute("SELECT value_as_number, value_source_value FROM measurement").fetchone() == (None, "2")
    assert repair_routed_med_admin_values(rw)["total_cleared"] == 0
    rw.close()


def _r_has_updated_omopduckdb() -> bool:
    if shutil.which("Rscript") is None:
        return False
    try:
        r = subprocess.run(
            ["Rscript", "--vanilla", "-e",
             "quit(status = !(requireNamespace('omopduckdb', quietly = TRUE) && 'route_drug_domains' %in% getNamespaceExports('omopduckdb')))"],
            capture_output=True, timeout=60, shell=RSCRIPT_SHELL,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _write_synthetic_med_admin_source(src_dir: Path) -> None:
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "demographic.csv").write_text(
        "PATID,BIRTH_DATE,SEX,RACE,HISPANIC\nP1,1965-04-10,F,05,N\nP2,1972-09-18,M,03,Y\n",
        encoding="utf-8",
    )
    (src_dir / "encounter.csv").write_text(
        "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,DISCHARGE_STATUS\n"
        "E1,P1,IP,2021-05-01,09:00,2021-05-04,14:00,HO\n",
        encoding="utf-8",
    )
    (src_dir / "med_admin.csv").write_text(
        "MEDADMINID,PATID,ENCOUNTERID,MEDADMIN_START_DATE,MEDADMIN_START_TIME,MEDADMIN_STOP_DATE,MEDADMIN_TYPE,MEDADMIN_CODE,"
        "RAW_MEDADMIN_MED_NAME,MEDADMIN_DOSE_ADMIN,MEDADMIN_DOSE_ADMIN_UNIT,MEDADMIN_ROUTE\n"
        "MA1,P1,E1,2021-05-01,10:00,2021-05-02,RX,6809,metformin,500,mg,PO\n"
        "MA2,P1,E1,2021-05-01,11:00,,ND,00001000203,syringe,1,each,SC\n"
        "MA3,P1,E1,2021-05-02,08:30,,CH,J9999,IV infusion,1,bag,IV\n"
        "MA4,P2,,2021-05-03,09:15,,RX,99901,care obs,15,mL,PO\n"
        "MA5,P2,,2021-05-03,12:00,,ND,00009000807,glucose strip,2,strip,OTH\n"
        "MA6,P2,,2021-05-04,07:00,,RX,000000,unmapped compound,10,mg,PO\n",
        encoding="utf-8",
    )


def test_m2_python_converter_routes_med_admin_across_all_domains(tmp_path):
    """M2 (Python side): etl_pcornet routes MED_ADMIN into all 5 OMOP v5.4 clinical domains with value_as_number=NULL."""
    src_dir = tmp_path / "pcornet"
    _write_synthetic_med_admin_source(src_dir)
    py_db = tmp_path / "py.duckdb"
    con = duckdb.connect(str(py_db))
    build_schema(con)
    _seed_vocab_for_routing(con)
    con.close()

    etl_pcornet(str(src_dir), str(py_db), key_check="ignore")
    con = duckdb.connect(str(py_db), read_only=True)
    assert sorted(con.execute("SELECT drug_concept_id FROM drug_exposure").fetchall()) == [(0,), (1503297,)]
    assert con.execute("SELECT device_concept_id, quantity, unit_source_value FROM device_exposure").fetchall() == [(4200001, 1, "each")]
    assert con.execute("SELECT procedure_concept_id, quantity FROM procedure_occurrence").fetchall() == [(900002, 1)]
    assert con.execute("SELECT observation_concept_id, value_as_number, value_source_value, unit_source_value FROM observation").fetchall() == [
        (4300001, None, "15", "mL")
    ]
    assert con.execute("SELECT measurement_concept_id, value_as_number, value_source_value, unit_source_value FROM measurement").fetchall() == [
        (3004501, None, "2", "strip")
    ]
    con.close()


@pytest.mark.skipif(not _r_has_updated_omopduckdb(), reason="Updated omopduckdb R package with route_drug_domains not installed")
def test_m2_python_and_r_etl_parity_across_all_routed_domains(tmp_path):
    """M2 (Py vs R parity): R and Python etl_pcornet produce identical rows across all 5 domain tables on MED_ADMIN."""
    src_dir = tmp_path / "pcornet"
    _write_synthetic_med_admin_source(src_dir)
    py_db = tmp_path / "py.duckdb"
    r_db = tmp_path / "r.duckdb"
    for p in (py_db, r_db):
        con = duckdb.connect(str(p))
        build_schema(con)
        _seed_vocab_for_routing(con)
        con.close()

    etl_pcornet(str(src_dir), str(py_db), key_check="ignore")
    r_code = (
        f"omopduckdb::etl_pcornet(source_dir = {json.dumps(str(src_dir))}, "
        f"db_path = {json.dumps(str(r_db))}, key_check = 'ignore')"
    )
    subprocess.run(["Rscript", "--vanilla", "-e", r_code], check=True, cwd=REPO_ROOT, shell=RSCRIPT_SHELL)

    py_con = duckdb.connect(str(py_db), read_only=True)
    r_con = duckdb.connect(str(r_db), read_only=True)
    for tbl in ("drug_exposure", "device_exposure", "procedure_occurrence", "observation", "measurement", "drug_era"):
        py_rows = py_con.execute(f"SELECT * FROM {tbl} ORDER BY 1").fetchall()
        r_rows = r_con.execute(f"SELECT * FROM {tbl} ORDER BY 1").fetchall()
        assert py_rows == r_rows, f"Mismatch in {tbl}: py={py_rows} vs r={r_rows}"
    py_con.close()
    r_con.close()


def test_m3_check_env_script_policies():
    """M3: scripts/check_env.py validates reference_env.json under report, pinned, compatible, and exact policies."""
    import check_env

    ref = json.loads((REPO_ROOT / "config" / "reference_env.json").read_text(encoding="utf-8"))
    assert set(ref["versions"]) == {"python", "duckdb", "pandas", "pyarrow", "scikit-learn", "xgboost", "numpy", "R", "quarto"}

    exact_actual = dict(ref["versions"])
    rows_exact = check_env.compare_environment(exact_actual, ref)
    assert all(r["exact"] and r["compatible"] and r["status"] == "match" for r in rows_exact)

    drifted = {**exact_actual, "duckdb": "1.4.2", "python": "3.12.8"}
    rows_pinned = check_env.compare_environment(drifted, ref)
    by_name = {r["component"]: r for r in rows_pinned}
    assert by_name["duckdb"]["compatible"] is True and by_name["duckdb"]["status"] == "compatible_drift"
    assert by_name["python"]["compatible"] is False and by_name["python"]["status"] == "incompatible"

    # DGX compatible mode skips R and allows NGC base-image Python
    rows_dgx = check_env.compare_environment(drifted, ref, skip_r=True, skip_pinned_python=True)
    assert "R" not in {r["component"] for r in rows_dgx}
    assert all(r["compatible"] for r in rows_dgx)
    assert check_env.main(["--policy", "report"]) == 0


def test_s1_backfill_all_dry_run_apply_and_idempotence(tmp_path):
    """S1: backfill_all runs all 5 steps in fixed order on read_only=True, applies cleanly, and reports 0 changes afterwards."""
    db = tmp_path / "site.duckdb"
    raw = tmp_path / "raw"
    raw.mkdir()

    con = duckdb.connect(str(db))
    build_schema(con)
    _seed_vocab_for_routing(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) "
                "VALUES (1, 8532, 1968, 0, 0, 'S1')")
    # 1. Non-standard condition (45581352 -> 201826)
    con.execute("""INSERT INTO condition_occurrence (
        condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id,
        condition_source_value, condition_source_concept_id
    ) VALUES (1, 1, 45581352, DATE '2020-04-01', 32020, 'E11.9', 45581352)""")
    # 2. Unmapped drug with RxNorm code '6809' -> 1503297
    con.execute("""INSERT INTO drug_exposure (
        drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
        drug_type_concept_id, drug_source_value, drug_source_concept_id
    ) VALUES (1, 1, 0, DATE '2020-04-01', DATE '2020-04-10', 32838, '6809', 0)""")
    # 3. Null lab measurement for HbA1c 4548-4
    con.execute("""INSERT INTO measurement (
        measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id,
        operator_concept_id, value_as_number, measurement_source_value
    ) VALUES (1, 1, 3004410, DATE '2020-04-02', 32817, 0, NULL, '4548-4')""")
    con.close()

    (raw / "lab_result_cm.csv").write_text(
        "LAB_RESULT_CM_ID,ssid,LAB_LOINC,RESULT_DATE,RESULT_NUM,RAW_RESULT\nL1,S1,4548-4,2020-04-02,,<5.4\n",
        encoding="utf-8",
    )
    (raw / "vital.csv").write_text(
        "VITALID,ssid,MEASURE_DATE,SMOKING,TOBACCO\nV1,S1,2020-04-02,NI,02\n",
        encoding="utf-8",
    )
    (raw / "med_admin.csv").write_text(
        "MEDADMINID,ssid,MEDADMIN_START_DATE,MEDADMIN_TYPE,MEDADMIN_CODE,MEDADMIN_DOSE_ADMIN,MEDADMIN_DOSE_ADMIN_UNIT\n"
        "M1,S1,2020-04-02,ND,00001000203,1,each\n",
        encoding="utf-8",
    )

    ro = duckdb.connect(str(db), read_only=True)
    dry = backfill_all(ro, raw, dry_run=True)
    assert list(dry["step"]) == ["condition_concepts", "drug_concepts", "lab_values", "vital_tobacco", "med_admin_and_routing"]
    assert list(dry["changes"]) == [1, 1, 1, 1, 1]
    ro.close()

    rw = duckdb.connect(str(db))
    applied = backfill_all(rw, raw, dry_run=False)
    assert list(applied["changes"]) == [1, 1, 1, 1, 1]
    rw.close()

    ro2 = duckdb.connect(str(db), read_only=True)
    dry_after = backfill_all(ro2, raw, dry_run=True)
    assert int(dry_after["changes"].sum()) == 0
    ro2.close()


def test_s4_template_0360_censored_sentence_and_optional_params(tmp_path):
    """S4: template v0.36.0 includes censored-lab Methods sentence and renders optional ORCID, funding, and COI."""
    from test_final_release_checks_0352 import _wb
    from omop_etl.manuscript_document import _param_authors
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_text import full_manuscript_markdown

    wb = _wb(tmp_path)
    params = {
        "authors": [{"name": "A. Researcher, MD", "orcid": "0000-0002-1825-0097", "role": "Design."}],
        "funding": "Supported by NIH Grant R01-000000.",
        "conflicts_of_interest": "The authors declare no competing interests.",
        "bmi_by_age_note": "Obesity was more prevalent among younger adults than older adults.",
        "discharge_limitation_note": "Supplemental discharge dispositions were harmonized across sites.",
    }
    authors = _param_authors(params)
    assert authors[0]["orcid"] == "0000-0002-1825-0097"

    out = full_manuscript_markdown(read_workbook_tables(wb), [], workbook=wb, run={"n_sites": 5, "bootstraps": 1000}, params=params)
    md_txt = out["markdown"]
    assert out["template"] == "0.36.0"
    assert "stored at their reported numeric limit with their OMOP operator concept recorded" in md_txt
    assert "Supported by NIH Grant R01-000000." in md_txt
    assert "The authors declare no competing interests." in md_txt
    assert "Obesity was more prevalent among younger adults" in md_txt
    assert "Supplemental discharge dispositions were harmonized" in md_txt


def test_converter_date_fallbacks_and_undated_row_guards(tmp_path):
    """0.36.0 sister-site hardening: PROCEDURES/PRESCRIBING/MED_ADMIN fall back to ADMIT_DATE/RX_ORDER_DATE/visit_start_date and skip undated rows."""
    src_dir = tmp_path / "pcornet_dates"
    src_dir.mkdir()
    (src_dir / "demographic.csv").write_text("PATID,BIRTH_DATE,SEX,RACE,HISPANIC\nP1,1970-01-01,F,05,N\n", encoding="utf-8")
    (src_dir / "encounter.csv").write_text(
        "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE,DISCHARGE_STATUS\n"
        "E1,P1,IP,2021-07-10,2021-07-12,HO\n"
        "E_BAD,P1,IP,NI,,HO\n",
        encoding="utf-8",
    )
    (src_dir / "procedures.csv").write_text(
        "PROCEDURESID,PATID,ENCOUNTERID,PX_TYPE,PX,PX_DATE,ADMIT_DATE\n"
        "PX1,P1,E1,02,J9999,,2021-07-11\n"
        "PX2,P1,E1,02,J9999,,\n"
        "PX_UNDATED,P1,,02,J9999,NI,\n",
        encoding="utf-8",
    )
    (src_dir / "prescribing.csv").write_text(
        "PRESCRIBINGID,PATID,ENCOUNTERID,RXNORM_CUI,RX_START_DATE,RX_ORDER_DATE\n"
        "RX1,P1,E1,6809,,2021-07-10\n"
        "RX_UNDATED,P1,,6809,,\n",
        encoding="utf-8",
    )
    db = tmp_path / "dates.duckdb"
    con = duckdb.connect(str(db))
    build_schema(con)
    _seed_vocab_for_routing(con)
    con.close()

    etl_pcornet(str(src_dir), str(db), key_check="ignore")
    ro = duckdb.connect(str(db), read_only=True)
    assert [str(r[0]) for r in ro.execute("SELECT procedure_date FROM procedure_occurrence ORDER BY procedure_occurrence_id").fetchall()] == [
        "2021-07-11", "2021-07-10"
    ]
    assert [str(r[0]) for r in ro.execute("SELECT drug_exposure_start_date FROM drug_exposure").fetchall()] == ["2021-07-10"]
    assert ro.execute("SELECT count(*) FROM visit_occurrence").fetchone()[0] == 1
    ro.close()

