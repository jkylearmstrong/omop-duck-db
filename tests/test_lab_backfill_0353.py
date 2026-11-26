"""0.35.3 (run C21 / C22): backfill_lab_values (with SSID/crosswalk/ENCOUNTERID/.zip support), backfill_vital_tobacco,
backfill_med_admin, and OMOP v5.4 domain routing (route_drug_domains). All data are synthetic."""
from __future__ import annotations

import zipfile

import duckdb
import pytest

from omop_etl.build_omop_cdm import backfill_med_admin, backfill_vital_tobacco, route_drug_domains
from omop_etl.lab_text import backfill_lab_values

HBA1C = 3004410


def _db(path):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("CREATE TABLE concept_ancestor AS SELECT concept_id AS ancestor_concept_id, concept_id AS descendant_concept_id, "
                "0 AS min_levels_of_separation, 0 AS max_levels_of_separation FROM concept")
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR, "
                "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)")
    con.execute("CREATE TABLE person AS SELECT * FROM (VALUES (1, 'P1'), (2, 'P2'), (3, 'P3'), (4, 'P4'), (5, 'P5'), (6, 'P6')) t(person_id, person_source_value)")
    rows = [(1, 1, "2020-06-01", "2020-06-01 00:00:00", None, "4548-4"),
            (2, 2, "2020-06-02", "2020-06-02 00:00:00", None, "LOCAL"),
            (3, 3, "2020-06-03", "2020-06-03 00:00:00", None, "4548-4"),
            (4, 4, "2020-06-04", "2020-06-04 08:10:00", None, "4548-4"),
            (5, 5, "2020-06-05", "2020-06-05 00:00:00", None, "4548-4"),
            (6, 6, "2020-06-06", "2020-06-06 00:00:00", None, "4548-4"),
            (7, 1, "2020-06-07", "2020-06-07 00:00:00", 5.9, "4548-4"),
            (8, 2, "2021-06-01", "2021-06-01 00:00:00", None, "4548-4")]
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 1, 9201, DATE '2020-05-31', DATE '2020-06-03'), "
                "(2, 2, 9202, DATE '2020-06-02', DATE '2020-06-02')) t(visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date)")
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id BIGINT, measurement_date DATE, "
                "measurement_datetime TIMESTAMP, value_as_number DOUBLE, measurement_source_value VARCHAR, operator_concept_id BIGINT)")
    con.executemany("INSERT INTO measurement VALUES (?, ?, 3004410, ?, ?, ?, ?, 0)", [list(r) for r in rows])
    con.close()


RAW = """LAB_RESULT_CM_ID,PATID,LAB_LOINC,RESULT_DATE,RESULT_TIME,SPECIMEN_DATE,RESULT_NUM,RAW_RESULT
1,P1,4548-4,2020-06-01,,,,<4.0
2,P1,4548-4,06/01/2020,,,,< 4.0
3,P2,4548-4,02JUN2020,,,,>14.0
4,P3,4548-4,2020-06-03,,,,6.1
5,P3,4548-4,2020-06-03,,,,7.2
6,P4,4548-4,2020-06-04,8:10,,,6.6
7,P4,4548-4,2020-06-04,14:00,,,7.0
8,P5,4548-4,,,2020-06-05,,POSITIVE
9,P1,4548-4,2020-06-07,,,5.9,
10,P2,4548-4,2021-06-01,,,,6.0
"""


@pytest.fixture()
def site(tmp_path):
    _db(tmp_path / "omop.duckdb")
    (tmp_path / "LAB_RESULT_CM.csv").write_text(RAW)
    return tmp_path


def test_dry_run_counts_and_read_only(site):
    con = duckdb.connect(str(site / "omop.duckdb"), read_only=True)
    r = backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31",
                            dry_run=True).set_index("name").loc["hba1c"]
    assert (r["null_rows"], r["filled"], r["filled_censored"], r["filled_in_stay"], r["ambiguous"], r["not_parseable"], r["no_match"]) == (6, 3, 2, 1, 1, 1, 1)
    assert r["pct_filled"] == 50.0
    assert con.execute("SELECT count(*) FROM measurement WHERE value_as_number IS NULL").fetchone()[0] == 7
    con.close()


def test_fill_writes_value_and_operator(site):
    con = duckdb.connect(str(site / "omop.duckdb"))
    backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31")
    got = {i: (v, op) for i, v, op in con.execute(
        "SELECT measurement_id, value_as_number, operator_concept_id FROM measurement ORDER BY 1").fetchall()}
    assert got[1] == (4.0, 4171756) and got[2] == (14.0, 4172704) and got[4] == (6.6, 0)
    assert got[3] == (None, 0) and got[5] == (None, 0) and got[6] == (None, 0)
    assert got[7] == (5.9, 0) and got[8] == (None, 0)
    again = backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31")
    assert int(again["filled"].sum()) == 0
    assert not con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '_blv%'").fetchone()[0]
    con.close()


def test_ssid_crosswalk_encounterid_and_zip(tmp_path):
    """backfill_lab_values resolves ssid, crosswalk.csv, ENCOUNTERID fallback, and .zip archives."""
    _db(tmp_path / "omop.duckdb")
    con = duckdb.connect(str(tmp_path / "omop.duckdb"))
    con.execute("ALTER TABLE visit_occurrence ADD COLUMN visit_source_value VARCHAR")
    con.execute("UPDATE visit_occurrence SET visit_source_value = 'ENC1' WHERE visit_occurrence_id = 1")
    # Person 2 uses PATID 'P2' in person_source_value, mapped via crosswalk.csv to ssid 'S2'
    (tmp_path / "crosswalk.csv").write_text("PATID,ssid\nP2,S2\n")
    # Person 1 has blank ssid in raw file -> resolved via ENCOUNTERID='ENC1'; Person 2 resolved via crosswalk S2 -> P2
    raw_ssid = """LAB_RESULT_CM_ID,ENCOUNTERID,LAB_LOINC,RESULT_DATE,RESULT_NUM,RAW_RESULT,ssid
1,ENC1,4548-4,01JUN2020,,<4.0,
2,,4548-4,02JUN2020,,>14.0,S2
"""
    zpath = tmp_path / "lab_result_cm.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("lab_result_cm.csv", raw_ssid)

    r = backfill_lab_values(con, zpath, names=["hba1c"], start="2020-01-01", end="2020-12-31").set_index("name").loc["hba1c"]
    assert (r["filled"], r["filled_censored"]) == (2, 2)
    got = dict(con.execute("SELECT measurement_id, value_as_number FROM measurement WHERE measurement_id IN (1, 2)").fetchall())
    assert got == {1: 4.0, 2: 14.0}
    con.close()


def test_backfill_vital_tobacco_and_idempotence(tmp_path):
    con = duckdb.connect(str(tmp_path / "tob.duckdb"))
    con.execute("CREATE TABLE person AS SELECT * FROM (VALUES (1, 'S1'), (2, 'S2'), (3, 'S3')) t(person_id, person_source_value)")
    con.execute("""CREATE TABLE observation (
        observation_id INTEGER, person_id BIGINT, observation_concept_id INTEGER, observation_date DATE,
        observation_datetime TIMESTAMP, observation_type_concept_id INTEGER, value_as_concept_id INTEGER,
        visit_occurrence_id BIGINT, observation_source_value VARCHAR, observation_source_concept_id INTEGER,
        value_source_value VARCHAR, obs_event_field_concept_id INTEGER)""")
    # Person 1 already has an observation on 2020-05-01
    con.execute("""INSERT INTO observation VALUES
        (1, 1, 43054909, DATE '2020-05-01', TIMESTAMP '2020-05-01 00:00:00', 32817, 45883458, NULL, '72166-2', 0, '03', 0)""")

    vital_csv = """VITALID,MEASURE_DATE,MEASURE_TIME,SMOKING,TOBACCO,ssid
V1,01MAY2020,08:00,OT,03,S1
V2,02MAY2020,09:30,OT,01,S2
V3,03MAY2020,,NI,02,S3
V4,04MAY2020,,OT,04,S3
V5,05MAY2020,,01,03,S2
"""
    (tmp_path / "vital.csv").write_text(vital_csv)
    con.close()

    ro = duckdb.connect(str(tmp_path / "tob.duckdb"), read_only=True)
    dry = backfill_vital_tobacco(ro, tmp_path / "vital.csv", dry_run=True)
    assert dry == {"candidates": 3, "inserted": 2, "already_present": 1}
    ro.close()

    rw = duckdb.connect(str(tmp_path / "tob.duckdb"))
    res1 = backfill_vital_tobacco(rw, tmp_path / "vital.csv")
    assert res1 == {"candidates": 3, "inserted": 2, "already_present": 1}
    rows = rw.execute("SELECT person_id, value_as_concept_id, value_source_value FROM observation ORDER BY observation_id").fetchall()
    assert rows == [(1, 45883458, "03"), (2, 45881518, "TOBACCO 01"), (3, 45879404, "TOBACCO 02")]
    # Idempotent second run
    res2 = backfill_vital_tobacco(rw, tmp_path / "vital.csv")
    assert res2["inserted"] == 0 and res2["already_present"] == 3
    rw.close()


def test_backfill_med_admin_and_domain_routing(tmp_path):
    con = duckdb.connect(str(tmp_path / "med.duckdb"))
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900001, 'NDC syringe', 'Device', 'NDC', '11-digit NDC', NULL, '00001000203', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4200001, 'Insulin syringe', 'Device', 'SNOMED', 'Physical Object', 'S', 'SYR1', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (900002, 'IV infusion code', 'Procedure', 'HCPCS', 'HCPCS', 'S', 'J9999', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4300001, 'Care observation', 'Observation', 'SNOMED', 'Context-dependent', 'S', 'OBS1', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("""CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
        (900001, 4200001, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id_1, concept_id_2, relationship_id, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("CREATE TABLE person AS SELECT * FROM (VALUES (10, 'S10'), (20, 'S20')) t(person_id, person_source_value)")
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (100, 10, 9201, DATE '2021-01-01', DATE '2021-01-05', 'E100')) "
                "t(visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_source_value)")
    con.execute("""CREATE TABLE drug_exposure (
        drug_exposure_id INTEGER, person_id BIGINT, drug_concept_id INTEGER, drug_exposure_start_date DATE,
        drug_exposure_start_datetime TIMESTAMP, drug_exposure_end_date DATE, drug_exposure_end_datetime TIMESTAMP,
        drug_type_concept_id INTEGER, quantity DOUBLE, route_concept_id INTEGER, provider_id BIGINT,
        visit_occurrence_id BIGINT, visit_detail_id BIGINT, drug_source_value VARCHAR, drug_source_concept_id INTEGER,
        route_source_value VARCHAR, dose_unit_source_value VARCHAR)""")
    con.execute("""CREATE TABLE device_exposure (
        device_exposure_id INTEGER, person_id BIGINT, device_concept_id INTEGER, device_exposure_start_date DATE,
        device_exposure_start_datetime TIMESTAMP, device_exposure_end_date DATE, device_exposure_end_datetime TIMESTAMP,
        device_type_concept_id INTEGER, quantity INTEGER, provider_id BIGINT, visit_occurrence_id BIGINT,
        visit_detail_id BIGINT, device_source_value VARCHAR, device_source_concept_id INTEGER,
        unit_concept_id INTEGER, unit_source_value VARCHAR, unit_source_concept_id INTEGER)""")
    con.execute("""CREATE TABLE procedure_occurrence (
        procedure_occurrence_id INTEGER, person_id BIGINT, procedure_concept_id INTEGER, procedure_date DATE,
        procedure_datetime TIMESTAMP, procedure_end_date DATE, procedure_end_datetime TIMESTAMP,
        procedure_type_concept_id INTEGER, modifier_concept_id INTEGER, quantity INTEGER, provider_id BIGINT,
        visit_occurrence_id BIGINT, visit_detail_id BIGINT, procedure_source_value VARCHAR, procedure_source_concept_id INTEGER)""")
    con.execute("""CREATE TABLE observation (
        observation_id INTEGER, person_id BIGINT, observation_concept_id INTEGER, observation_date DATE,
        observation_datetime TIMESTAMP, observation_type_concept_id INTEGER, value_as_number DOUBLE,
        value_as_concept_id INTEGER, provider_id BIGINT, visit_occurrence_id BIGINT, visit_detail_id BIGINT,
        observation_source_value VARCHAR, observation_source_concept_id INTEGER, unit_source_value VARCHAR, obs_event_field_concept_id INTEGER)""")

    # Seed one misplaced Observation row inside drug_exposure (from PRESCRIBING) to verify route_drug_domains()
    con.execute("""INSERT INTO drug_exposure VALUES
        (1, 10, 4300001, DATE '2021-01-01', TIMESTAMP '2021-01-01 00:00:00', DATE '2021-01-01', NULL,
         32838, 1.0, 0, NULL, 100, NULL, 'OBS1', 4300001, NULL, NULL)""")

    r_route = route_drug_domains(con)
    assert r_route["observation"] == 1 and r_route["total_routed"] == 1
    assert con.execute("SELECT count(*) FROM drug_exposure").fetchone()[0] == 0
    assert con.execute("SELECT observation_concept_id FROM observation").fetchall() == [(4300001,)]

    # MED_ADMIN zip with 1 RxNorm drug, 1 hyphenated 10-digit NDC device (0001-0002-03 -> 00001000203 -> 4200001 Device),
    # 1 HCPCS procedure (J9999 -> 900002 Procedure), and 1 unmapped drug (stays in drug_exposure with concept_id=0)
    ma_csv = """MEDADMINID,ENCOUNTERID,MEDADMIN_START_DATE,MEDADMIN_START_TIME,MEDADMIN_STOP_DATE,MEDADMIN_TYPE,MEDADMIN_CODE,RAW_MEDADMIN_MED_NAME,MEDADMIN_DOSE_ADMIN,ssid
M1,E100,02JAN2021,10:15,03JAN2021,RX,6809,metformin,500,S10
M2,E100,02JAN2021,11:00,,ND,0001-0002-03,syringe,1,S10
M3,,03JAN2021,,,CH,J9999,infusion,1,S20
M4,,04JAN2021,,,RX,,unknown local compounding,1,S20
"""
    zpath = tmp_path / "med_admin.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("med_admin.csv", ma_csv)

    res = backfill_med_admin(con, zpath)
    assert res["source_rows"] == 4
    assert res["drug_exposure"] == 2 and res["unmapped_drug"] == 1
    assert res["device_exposure"] == 1 and res["procedure_occurrence"] == 1
    assert con.execute("SELECT drug_concept_id FROM drug_exposure ORDER BY drug_exposure_id").fetchall() == [(1503297,), (0,)]
    assert con.execute("SELECT device_concept_id FROM device_exposure").fetchall() == [(4200001,)]
    assert con.execute("SELECT procedure_concept_id FROM procedure_occurrence").fetchall() == [(900002,)]

    # Idempotent second call
    res2 = backfill_med_admin(con, zpath)
    assert res2["already_loaded"] is True and res2["drug_exposure"] == 0
    con.close()
