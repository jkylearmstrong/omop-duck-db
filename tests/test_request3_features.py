"""Tests for feature request 3: staged wide loader, vectorized datetime normalizer, dual-strategy medication
classifier + auditor, path sanitizer, RAM/disk governor, and the phenotype cohort DSL."""

import math
import os
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from omop_etl import (
    ConditionCriteria,
    DrugCriteria,
    Everyone,
    MeasurementCriteria,
    PhenotypeIntersect,
    PhenotypeNot,
    PhenotypeUnion,
    audit_medication_strategies,
    build_schema,
    compile_phenotype,
    configure_resources,
    execute_phenotype,
    extract_medication_classes,
    install_vectorized_strptime,
    load_wide_measurement,
    phenotype_from_dict,
    sanitize_sql_path,
    vectorized_strptime,
)
from omop_etl.paths import attach_database, copy_to, read_csv_sql, sql_path_literal
from omop_etl.wide_measurement import DEFAULT_VITAL_CONCEPT_MAPPING, DEFAULT_VITAL_PLAUSIBILITY_FILTERS


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    build_schema(c)
    return c


def _winify(path):
    """Backslash form of a path on Windows; unchanged elsewhere (a backslash is a legal filename char there)."""
    return str(path).replace("/", "\\") if os.name == "nt" else str(path)


def _add_people(c, n):
    for i in range(1, n + 1):
        c.execute(
            "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, "
            "ethnicity_concept_id, person_source_value) VALUES (?, 8507, 1960, 8527, 38003563, ?)",
            [i, f"P{i}"])


# --------------------------------------------------------------------------------------------
# Feature 4: path sanitizer
# --------------------------------------------------------------------------------------------
def test_sanitize_sql_path_forward_slashes_and_absolute(tmp_path):
    raw = str(tmp_path / "sub dir" / "x.csv").replace("/", "\\")
    out = sanitize_sql_path(raw)
    assert "\\" not in out
    assert Path(out).is_absolute() or out.startswith("/")


def test_sanitize_sql_path_preserves_uris():
    assert sanitize_sql_path("s3://bucket/key/*.parquet") == "s3://bucket/key/*.parquet"


def test_sql_path_literal_escapes_quotes(tmp_path):
    p = tmp_path / "o'brien.csv"
    p.write_text("a,b\n1,2\n")
    c = duckdb.connect()
    assert c.execute(f"SELECT COUNT(*) FROM {read_csv_sql(p)}").fetchone()[0] == 1
    assert sql_path_literal(p).startswith("'") and "''" in sql_path_literal(p)


def test_attach_and_copy_with_backslash_paths(tmp_path):
    db = tmp_path / "other.duckdb"
    duckdb.connect(str(db)).execute("CREATE TABLE t AS SELECT 1 AS x")
    c = duckdb.connect()
    attach_database(c, _winify(db), "other", read_only=True)
    assert c.execute("SELECT x FROM other.t").fetchone()[0] == 1
    out = tmp_path / "out.parquet"
    copy_to(c, "(SELECT * FROM other.t)", _winify(out))
    assert out.exists()


# --------------------------------------------------------------------------------------------
# Feature 5: resources
# --------------------------------------------------------------------------------------------
def test_configure_resources_sets_limits(tmp_path):
    c = duckdb.connect()
    cfg = configure_resources(c, memory_fraction=0.5, temp_directory=tmp_path / "spill", threads=2)
    assert cfg.threads == 2
    assert c.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    assert Path(cfg.temp_directory).is_dir()
    assert c.execute("SELECT current_setting('temp_directory')").fetchone()[0].replace("\\", "/").endswith("spill")
    if cfg.available_ram_bytes:
        assert cfg.memory_limit.endswith("MB")


def test_configure_resources_auto_defaults():
    c = duckdb.connect()
    cfg = configure_resources(c)
    assert cfg.threads >= 1
    assert Path(cfg.temp_directory).is_dir()


def test_configure_resources_rejects_bad_fraction():
    with pytest.raises(ValueError):
        configure_resources(duckdb.connect(), memory_fraction=1.5)


# --------------------------------------------------------------------------------------------
# Feature 2: vectorized datetime
# --------------------------------------------------------------------------------------------
@pytest.fixture
def messy():
    c = duckdb.connect()
    c.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('05JAN2020','4:15'),('2020-01-05','16:30:15'),('01/05/2020','4:15 PM'),('01/05/2020','12:05 AM'),
        ('2020-01-05',NULL),('2020-01-05','7'),('05JAN2020','0415'),('garbage','1:00'),(NULL,'1:00')
    ) v(d, t)""")
    return c


def test_vectorized_strptime_normalizes_times(messy):
    got = [r[0] for r in messy.execute(f"SELECT {vectorized_strptime('d', 't')} FROM t").fetchall()]
    ts = pd.Timestamp
    assert got[:7] == [ts(2020, 1, 5, 4, 15), ts(2020, 1, 5, 16, 30, 15), ts(2020, 1, 5, 16, 15),
                       ts(2020, 1, 5, 0, 5), ts(2020, 1, 5), ts(2020, 1, 5, 7), ts(2020, 1, 5, 4, 15)]
    assert got[7] is None and got[8] is None


def test_vectorized_strptime_on_error_modes(messy):
    dflt = [r[0] for r in messy.execute(
        f"SELECT {vectorized_strptime('d', 't', on_error='default_date')} FROM t").fetchall()]
    assert dflt[7] == pd.Timestamp(1900, 1, 1) and dflt[8] is None
    with pytest.raises(duckdb.Error, match="garbage"):
        messy.execute(f"SELECT {vectorized_strptime('d', 't', on_error='raise')} FROM t").fetchall()
    with pytest.raises(ValueError):
        vectorized_strptime("d", "t", on_error="explode")


def test_vectorized_strptime_custom_formats_and_macro(messy):
    only_iso = [r[0] for r in messy.execute(
        f"SELECT {vectorized_strptime('d', None, formats=['%Y-%m-%d'])} FROM t").fetchall()]
    assert only_iso[0] is None and only_iso[1] == pd.Timestamp(2020, 1, 5)
    install_vectorized_strptime(messy)
    assert messy.execute("SELECT vectorized_strptime(d, t) FROM t LIMIT 1").fetchone()[0] == pd.Timestamp(2020, 1, 5, 4, 15)
    assert messy.execute("SELECT normalize_time('4:15')").fetchone()[0] == "04:15"


# --------------------------------------------------------------------------------------------
# Feature 1: wide measurement loader
# --------------------------------------------------------------------------------------------
def _vital_csv(tmp_path):
    p = tmp_path / "vital.csv"
    p.write_text(
        "PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,SYSTOLIC,DIASTOLIC,BMI,ORIGINAL_BMI\n"
        "P1,E1,05JAN2020,4:15,120,80,25.5,\n"
        "P1,E1,06JAN2020,,999,85,2,\n"       # SBP and BMI implausible
        "P2,E2,2020-02-01,13:00,130,,31.0,31.0\n"
        "NOPE,E3,2020-02-01,13:00,140,90,22,\n")  # unmatched person
    return p


def test_load_wide_measurement_sequential(con, tmp_path):
    _add_people(con, 2)
    report = load_wide_measurement(
        con, _vital_csv(tmp_path), DEFAULT_VITAL_CONCEPT_MAPPING,
        value_plausibility_filters=DEFAULT_VITAL_PLAUSIBILITY_FILTERS, missing_columns="skip")
    by = report.set_index("source_column")
    assert by.loc["SYSTOLIC", ["rows_non_null", "rows_out_of_range", "rows_unmatched_person", "rows_inserted"]].tolist() == [4, 1, 1, 2]
    assert by.loc["DIASTOLIC", "rows_inserted"] == 2
    assert by.loc["BMI", "rows_out_of_range"] == 1
    # BMI / ORIGINAL_BMI share concept 3038553 -> coalesced: P2 has both populated but yields ONE row
    assert by.loc["BMI", "rows_inserted"] == 2
    assert by.loc["ORIGINAL_BMI", "rows_inserted"] == 0 and by.loc["ORIGINAL_BMI", "coalesced_into"] == "BMI"
    assert con.execute("SELECT COUNT(*) FROM measurement WHERE measurement_concept_id = 3038553").fetchone()[0] == 2
    total = con.execute("SELECT COUNT(*) FROM measurement").fetchone()[0]
    assert total == int(report["rows_inserted"].sum())
    ids = con.execute("SELECT COUNT(DISTINCT measurement_id), COUNT(*) FROM measurement").fetchone()
    assert ids[0] == ids[1]
    row = con.execute("SELECT measurement_concept_id, value_as_number, measurement_datetime, unit_concept_id, "
                      "measurement_source_value FROM measurement WHERE value_as_number = 120").fetchone()
    assert row == (3004249, 120.0, pd.Timestamp(2020, 1, 5, 4, 15), 8876, "8480-6")
    # staging table is cleaned up
    assert not con.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = '_staged_wide_measurement'").fetchall()


def test_load_wide_measurement_coalesce_precedence_and_opt_out(con, tmp_path):
    _add_people(con, 2)
    p = tmp_path / "bmi.csv"
    p.write_text("PATID,MEASURE_DATE,BMI,ORIGINAL_BMI\n"
                 "P1,2020-01-01,25,26\n"      # both: precedence wins
                 "P1,2020-01-02,2,27\n"       # primary implausible -> falls back
                 "P2,2020-01-03,,28\n"        # only fallback
                 "P2,2020-01-04,,\n")
    mapping = {k: DEFAULT_VITAL_CONCEPT_MAPPING[k] for k in ("BMI", "ORIGINAL_BMI")}
    load_wide_measurement(con, p, mapping, value_plausibility_filters=DEFAULT_VITAL_PLAUSIBILITY_FILTERS,
                          coalesce_columns=[("ORIGINAL_BMI", "BMI")], time_col=None, visit_col=None)
    assert sorted(r[0] for r in con.execute("SELECT value_as_number FROM measurement").fetchall()) == [26, 27, 28]
    con.execute("DELETE FROM measurement")
    rep = load_wide_measurement(con, p, mapping, coalesce_columns=None, time_col=None, visit_col=None)
    assert rep["rows_inserted"].sum() == 5  # no coalescing: BMI x2 + ORIGINAL_BMI x3
    with pytest.raises(ValueError, match="more than one concept_id"):
        load_wide_measurement(con, p, {"BMI": 3038553, "ORIGINAL_BMI": 1}, coalesce_columns=[("BMI", "ORIGINAL_BMI")],
                              time_col=None, visit_col=None)


def test_load_wide_measurement_single_mode_and_keep_staged(con, tmp_path):
    _add_people(con, 2)
    mapping = {"SYSTOLIC": 3004249}
    report = load_wide_measurement(con, _vital_csv(tmp_path), mapping, batch_execution_mode="single",
                                   staged_table_name="stg", keep_staged=True)
    assert report.loc[0, "rows_inserted"] == 3
    assert con.execute("SELECT COUNT(*) FROM stg").fetchone()[0] == 4
    assert con.execute("SELECT data_type FROM information_schema.columns WHERE table_name='stg' "
                       "AND column_name='stage_id'").fetchone()[0] == "BIGINT"


def test_load_wide_measurement_from_table_and_errors(con):
    _add_people(con, 1)
    con.execute("CREATE TABLE wide AS SELECT 'P1' AS patid, '2021-03-04' AS dt, 70.0 AS hr")
    rep = load_wide_measurement(con, "wide", {"hr": {"concept_id": 3027018, "min": 20, "max": 250}},
                                person_col="patid", date_col="dt", time_col=None, visit_col=None)
    assert rep.loc[0, "rows_inserted"] == 1
    with pytest.raises(ValueError, match="not found"):
        load_wide_measurement(con, "wide", {"hr": 1}, person_col="missing", date_col="dt")
    with pytest.raises(ValueError):
        load_wide_measurement(con, "wide", {"hr": 1}, batch_execution_mode="parallel", person_col="patid", date_col="dt")


def test_load_wide_measurement_multiple_columns_unique_ids(con):
    _add_people(con, 1)
    con.execute("CREATE TABLE wide AS SELECT 'P1' AS patid, '2021-03-04' AS dt, 70.0 AS a, 80.0 AS b")
    rep = load_wide_measurement(con, "wide", {"a": 1, "b": 2}, person_col="patid", date_col="dt",
                                time_col=None, visit_col=None)
    assert rep["rows_inserted"].tolist() == [1, 1]


# --------------------------------------------------------------------------------------------
# Feature 3: medication classifier + auditor
# --------------------------------------------------------------------------------------------
@pytest.fixture
def med_con(con):
    # ATC class 1000 -> ingredient 2000 -> drug 2001 ; ATC class 1001 -> ingredient 2100 -> drug 2101
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, "
                "standard_concept, concept_code, valid_start_date, valid_end_date) VALUES "
                "(1000,'Statins','Drug','ATC','ATC 4th','C','C10AA','1970-01-01','2099-12-31'),"
                "(9001,'Legacy statin code','Drug','NDC','11-digit NDC',NULL,'00093-1234','1970-01-01','2099-12-31')")
    for a, d in [(1000, 1000), (1000, 2000), (1000, 2001), (2000, 2000), (2000, 2001), (2001, 2001)]:
        con.execute("INSERT INTO concept_ancestor VALUES (?, ?, 0, 0)", [a, d])
    _add_people(con, 12)
    # cohort across 3 sites x 4 people
    con.execute("CREATE TABLE med_cohort (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, "
                "cohort_end_date DATE, site_anon VARCHAR)")
    for i in range(1, 13):
        site = "A" if i <= 4 else "B" if i <= 8 else "C"
        con.execute("INSERT INTO med_cohort VALUES (1, ?, '2020-06-01', '2020-06-01', ?)", [i, site])
    def rx(pid, concept, src, start="2020-05-01"):
        con.execute(
            "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
            "drug_exposure_start_datetime, drug_exposure_end_date, drug_type_concept_id, drug_source_value, "
            "drug_source_concept_id) VALUES (?, ?, ?, ?, ?, ?, 32817, ?, 0)",
            [pid * 100 + (hash((pid, src)) % 90), pid, concept, start, start, start, src])
    # Site A: both strategies agree (persons 1,2 on statin by both; 3,4 none)
    for p in (1, 2):
        rx(p, 2001, "LIPITOR")
    # Site B: standardized only (source strings empty/unknown) -> source blind
    for p in (5, 6):
        rx(p, 2001, "unmapped-local-string")
    # Site C: mixed disagreement
    rx(9, 2001, "LIPITOR")
    rx(10, 0, "LIPITOR")
    rx(11, 2001, "other")
    return con


SPECS = {"statin": {"ancestor_concept_ids": [1000]}}
XWALK = pd.DataFrame({"source_value": ["lipitor", "00093-1234"], "drug_class": ["statin", "statin"]})


def test_extract_medication_classes_both(med_con):
    out = extract_medication_classes(med_con, "med_cohort", SPECS, strategy="both", crosswalk=XWALK)
    out = out.set_index("person_id")
    assert out.loc[1, ["statin_source", "statin_standard", "statin"]].tolist() == [1, 1, 1]
    assert out.loc[5, ["statin_source", "statin_standard", "statin"]].tolist() == [0, 1, 1]
    assert out.loc[10, ["statin_source", "statin_standard", "statin"]].tolist() == [1, 0, 1]
    assert out.loc[3, "statin"] == 0
    assert set(out["site"]) == {"A", "B", "C"}


def test_extract_medication_classes_single_strategies_and_window(med_con):
    std = extract_medication_classes(med_con, "med_cohort", SPECS, strategy="standard")
    assert "statin_standard" in std and "statin_source" not in std
    assert int(std["statin"].sum()) == 6
    src = extract_medication_classes(med_con, "med_cohort", SPECS, strategy="source", crosswalk=XWALK)
    assert int(src["statin"].sum()) == 4
    # exposure 31 days before index is outside a 10-day lookback
    none = extract_medication_classes(med_con, "med_cohort", SPECS, strategy="standard", lookback_days=10)
    assert int(none["statin"].sum()) == 0


def test_extract_medication_classes_crosswalk_csv_and_inline_atc(med_con, tmp_path):
    csv = tmp_path / "xw.csv"
    csv.write_text("cui,medication_class\nLIPITOR,statin\n")
    specs = {"statin": {"atc_codes": ["C10AA"], "source_values": ["other"]}}
    out = extract_medication_classes(med_con, "med_cohort", specs, strategy="both", crosswalk=str(csv))
    row = out.set_index("person_id")
    assert row.loc[11, "statin_source"] == 1      # inline source value
    assert row.loc[5, "statin_standard"] == 1     # ATC code resolved to ancestor concept


def test_extract_medication_classes_validation(med_con):
    with pytest.raises(ValueError):
        extract_medication_classes(med_con, "med_cohort", SPECS, strategy="nope")
    with pytest.raises(ValueError, match="crosswalk"):
        extract_medication_classes(med_con, "med_cohort", SPECS, strategy="source")
    with pytest.raises(ValueError):
        extract_medication_classes(med_con, "med_cohort", SPECS, strategy="standard", return_audit=True)


def test_audit_flags_discordant_sites(med_con):
    flags, audit = extract_medication_classes(med_con, "med_cohort", SPECS, strategy="both", crosswalk=XWALK,
                                              return_audit=True)
    a = audit.set_index(["site", "medication_class"])
    assert a.loc[("A", "statin"), "kappa"] == pytest.approx(1.0)
    assert not a.loc[("A", "statin"), "flagged"]
    assert "source_blind" in a.loc[("B", "statin"), "reason"]
    assert a.loc[("B", "statin"), "concordance"] == pytest.approx(0.5)
    assert a.loc[("C", "statin"), "flagged"]
    assert ("ALL", "statin") in a.index
    assert a.loc[("ALL", "statin"), "n"] == 12


def test_audit_kappa_matches_hand_calculation():
    # 2x2: both=20, source_only=5, standard_only=10, neither=65 -> po=.85, pe=(25*30+75*70)/10000=.60
    frame = pd.DataFrame({
        "site": ["X"] * 100,
        "m_source": [1] * 25 + [0] * 75,
        "m_standard": [1] * 20 + [0] * 5 + [1] * 10 + [0] * 65,
    })
    audit = audit_medication_strategies(frame, site_col="site")
    row = audit[audit["site"] == "X"].iloc[0]
    assert row["concordance"] == pytest.approx(0.85)
    assert row["kappa"] == pytest.approx((0.85 - 0.60) / (1 - 0.60))


def test_audit_peer_outlier_and_empty_classes():
    rows = []
    for site, agree in [("S1", True), ("S2", True), ("S3", True), ("S4", False)]:
        for i in range(40):
            s = int(i % 2 == 0)
            rows.append({"site": site, "x_source": s, "x_standard": s if agree else 1 - s})
    audit = audit_medication_strategies(pd.DataFrame(rows))
    a = audit.set_index("site")
    assert "peer_outlier" in a.loc["S4", "reason"]
    assert not a.loc["S1", "flagged"]
    never = audit_medication_strategies(pd.DataFrame({"x_source": [0, 0], "x_standard": [0, 0]}))
    assert never.loc[0, "kappa"] == 1.0 and not never.loc[0, "flagged"]
    with pytest.raises(ValueError):
        audit_medication_strategies(pd.DataFrame({"a": [1]}))


# --------------------------------------------------------------------------------------------
# Feature 6: phenotype DSL
# --------------------------------------------------------------------------------------------
@pytest.fixture
def pheno(con):
    _add_people(con, 8)
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, "
                "standard_concept, concept_code, valid_start_date, valid_end_date) VALUES "
                "(7001,'E11.9','Condition','ICD10CM','5-char billing code',NULL,'E11.9','1970-01-01','2099-12-31')")
    for a, d in [(201826, 201826), (201826, 4000), (4000, 4000), (21600, 21600), (21600, 21601), (21601, 21601)]:
        con.execute("INSERT INTO concept_ancestor VALUES (?, ?, 0, 0)", [a, d])
    for i in range(1, 9):
        con.execute("INSERT INTO observation_period VALUES (?, ?, '2015-01-01', '2024-12-31', 32817)", [i, i])
    cid = [0]

    def cond(p, concept, src, date, status=0):
        cid[0] += 1
        con.execute(
            "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
            "condition_start_date, condition_type_concept_id, condition_status_concept_id, "
            "condition_source_value, condition_source_concept_id) VALUES (?, ?, ?, ?, 32817, ?, ?, 0)",
            [cid[0], p, concept, date, status, src])

    def lab(p, value, date, concept=3004410):
        cid[0] += 1
        con.execute(
            "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
            "measurement_type_concept_id, value_as_number, measurement_source_value) "
            "VALUES (?, ?, ?, ?, 32817, ?, '4548-4')", [cid[0], p, concept, date, value])

    def rx(p, concept, date, days):
        cid[0] += 1
        con.execute(
            "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
            "drug_exposure_end_date, drug_type_concept_id, days_supply) VALUES (?, ?, ?, ?, ?, 32817, ?)",
            [cid[0], p, concept, date, date, days])

    cond(1, 4000, "x", "2020-03-01", 32902)       # descendant concept, primary
    cond(2, 0, "E11.9", "2020-04-01", 32908)      # ICD prefix, secondary
    lab(3, 7.0, "2020-05-01")                     # HbA1c high
    lab(4, 5.5, "2020-05-01")                     # HbA1c normal
    rx(5, 21601, "2020-06-01", 60)                # descendant drug, 60 days
    rx(6, 21601, "2020-06-01", 5)                 # short supply
    cond(7, 4000, "x", "2020-02-01")
    lab(7, 7.2, "2020-01-01")
    lab(7, 6.8, "2020-09-01")
    cond(8, 4000, "x", "2020-02-01")              # also type-1-like exclusion marker
    cond(8, 0, "E10.1", "2020-02-05")
    return con


T2DM_COND = ConditionCriteria(concept_ids=[201826], icd_codes=["E11"])
HBA1C = MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5)
DRUG = DrugCriteria(concept_ids=[21600], min_days_supply=30)


def _ids(df):
    return sorted(df["subject_id"].tolist())


def test_union_of_modalities(pheno):
    df = execute_phenotype(pheno, T2DM_COND | HBA1C | DRUG, "cohort", 1)
    assert _ids(df) == [1, 2, 3, 5, 7, 8]
    assert df.set_index("subject_id").loc[7, "cohort_start_date"] == pd.Timestamp(2020, 1, 1)  # earliest event


def test_intersect_index_is_latest_first_satisfaction(pheno):
    df = execute_phenotype(pheno, T2DM_COND & HBA1C, "cohort", 2)
    assert _ids(df) == [7]
    assert df.iloc[0]["cohort_start_date"] == pd.Timestamp(2020, 2, 1)  # max(first condition, first high lab)


def test_not_and_top_level_not(pheno):
    df = execute_phenotype(pheno, T2DM_COND & ~ConditionCriteria(icd_codes=["E10"]), "cohort", 3)
    assert _ids(df) == [1, 2, 7]
    everyone_else = execute_phenotype(pheno, ~T2DM_COND, "cohort", 4)
    assert _ids(everyone_else) == [3, 4, 5, 6]


def test_diagnosis_position_and_occurrences(pheno):
    primary = execute_phenotype(pheno, ConditionCriteria(concept_ids=[201826], icd_codes=["E11"], diagnosis="primary"),
                                "cohort", 5)
    assert _ids(primary) == [1]
    secondary = execute_phenotype(pheno, ConditionCriteria(icd_codes=["E11"], diagnosis="secondary"), "cohort", 6)
    assert _ids(secondary) == [2]
    twice = execute_phenotype(pheno, MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5, min_occurrences=2),
                              "cohort", 7)
    assert _ids(twice) == [7]
    assert twice.iloc[0]["cohort_start_date"] == pd.Timestamp(2020, 9, 1)  # the 2nd qualifying record


def test_no_descendants_flag(pheno):
    df = execute_phenotype(pheno, ConditionCriteria(concept_ids=[201826], include_descendants=False), "cohort", 8)
    assert df.empty


def test_threshold_boundaries(pheno):
    assert _ids(execute_phenotype(pheno, MeasurementCriteria(loinc_codes=["4548-4"], value_gt=7.0), "cohort", 9)) == [7]
    assert _ids(execute_phenotype(pheno, MeasurementCriteria(loinc_codes=["4548-4"], value_lt=6.0), "cohort", 10)) == [4]


def test_lookback_relative_to_index_table(pheno):
    pheno.execute("CREATE TABLE admits (subject_id BIGINT, cohort_start_date DATE)")
    pheno.execute("INSERT INTO admits VALUES (7, '2020-03-01'), (3, '2020-05-20'), (4, '2020-05-20'), (1, '2020-03-02')")
    # high HbA1c in the 90 days before admission
    crit = MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5, lookback_days=90)
    df = execute_phenotype(pheno, crit, "cohort", 11, index_table="admits")
    assert _ids(df) == [3, 7]                       # P7's Jan lab is 60 days before; P3's May lab is 19 days
    assert df.set_index("subject_id").loc[7, "cohort_start_date"] == pd.Timestamp(2020, 3, 1)  # index = admit date
    # lookahead window, and Everyone & ~criterion within the admitted population
    ahead = MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5, lookahead_days=365)
    assert _ids(execute_phenotype(pheno, ahead, "cohort", 12, index_table="admits")) == [7]
    rest = execute_phenotype(pheno, Everyone() & ~crit, "cohort", 13, index_table="admits")
    assert _ids(rest) == [1, 4]


def test_window_requires_index_table():
    with pytest.raises(ValueError, match="index_table"):
        compile_phenotype(MeasurementCriteria(concept_ids=[1], lookback_days=5))


def test_single_pass_sql_shape():
    expr = T2DM_COND | (HBA1C & ~DRUG)
    sql = compile_phenotype(expr, cohort_definition_id=3)
    assert sql.count("GROUP BY person_id") == 1          # one aggregation over all leaves
    assert sql.count("UNION ALL") == 2                   # three leaf scans in one events CTE
    assert "condition_occurrence" in sql and sql.count("FROM measurement") == 1


def test_validation_errors():
    with pytest.raises(ValueError):
        compile_phenotype(ConditionCriteria())
    with pytest.raises(ValueError):
        compile_phenotype(DrugCriteria())
    with pytest.raises(ValueError):
        compile_phenotype(ConditionCriteria(concept_ids=[1], min_occurrences=0))
    with pytest.raises(ValueError):
        compile_phenotype(ConditionCriteria(concept_ids=[1], diagnosis="tertiary"))
    with pytest.raises(ValueError):
        PhenotypeUnion(T2DM_COND)


def test_operators_flatten_and_dict_roundtrip(pheno):
    expr = T2DM_COND | HBA1C | DRUG
    assert len(expr.children) == 3
    spec = (T2DM_COND & ~HBA1C).to_dict()
    rebuilt = phenotype_from_dict(spec)
    assert isinstance(rebuilt, PhenotypeIntersect) and isinstance(rebuilt.children[1], PhenotypeNot)
    a = execute_phenotype(pheno, T2DM_COND & ~HBA1C, "cohort", 20)
    b = execute_phenotype(pheno, rebuilt, "cohort", 21)
    assert _ids(a) == _ids(b)
    with pytest.raises(ValueError):
        phenotype_from_dict({"type": "wat"})


def test_execute_replaces_same_definition(pheno):
    execute_phenotype(pheno, T2DM_COND, "cohort", 30)
    again = execute_phenotype(pheno, T2DM_COND, "cohort", 30)
    assert len(again) == pheno.execute("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 30").fetchone()[0]


def test_nan_safe_kappa_nonexposure():
    frame = pd.DataFrame({"x_source": [1, 1], "x_standard": [1, 1]})
    k = audit_medication_strategies(frame).loc[0, "kappa"]
    assert not math.isnan(k)


def test_prepare_source_view_logs_aliases_and_padding(tmp_path, capsys):
    from omop_etl.build_omop_cdm import prepare_source_view

    p = tmp_path / "demographic.csv"
    p.write_text("ssid,BIRTH_DATE\nP1,2000-01-01\n")
    c = duckdb.connect()
    prepare_source_view(c, "v", _winify(p), ["PATID", "BIRTH_DATE", "PROVIDERID"])
    out = capsys.readouterr().out
    assert "ssid -> PATID" in out and "PROVIDERID" in out
    assert c.execute("SELECT PATID, PROVIDERID FROM v").fetchone() == ("P1", None)


def test_load_vital_maps_smoking_to_observation(tmp_path):
    from omop_etl import load_vital
    from omop_etl.build_omop_cdm import load_macros

    c = duckdb.connect(":memory:")
    build_schema(c)
    load_macros(c)
    _add_people(c, 1)
    (tmp_path / "vital.csv").write_text(
        "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,SYSTOLIC,SMOKING\n"
        "V1,P1,E1,2024-01-01,10:00,120,04\n"      # never smoker
        "V2,P1,E1,2024-02-01,10:00,,1\n"          # '1' -> current every day smoker
        "V3,P1,E1,2024-03-01,10:00,,NI\n")        # no information -> no row
    load_vital(c, str(tmp_path))
    rows = c.execute("SELECT observation_concept_id, value_as_concept_id, value_source_value FROM observation "
                     "ORDER BY observation_date").fetchall()
    assert rows == [(43054909, 45879404, "04"), (43054909, 45881517, "1")]
    assert c.execute("SELECT COUNT(*) FROM measurement").fetchone()[0] == 1


def test_quality_check_module_cli(tmp_path):
    from omop_etl.quality_check import main

    db = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.close()
    out = tmp_path / "dq" / "report.json"
    assert main(["--db-path", str(db), "--output", str(out)]) == 0
    import json
    assert "CheckResults" in json.loads(out.read_text())


def test_load_vocabulary_table_subset_and_safe_view(tmp_path):
    import shutil
    from omop_etl import build_safe_source_view, load_vocabulary

    vocab = tmp_path / "vocab"
    shutil.copytree(Path(__file__).parent / "testthat" / "testdata" / "tiny_vocab", vocab)
    db = tmp_path / "v.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.close()
    assert load_vocabulary(str(vocab), db_path=str(db), tables=["concept"], drop_indexes=True) is True
    c = duckdb.connect(str(db))
    assert c.execute("SELECT COUNT(*) FROM concept").fetchone()[0] > 0
    assert c.execute("SELECT COUNT(*) FROM concept_relationship").fetchone()[0] == 0
    c.close()
    with pytest.raises(ValueError, match="Unknown vocabulary tables"):
        load_vocabulary(str(vocab), db_path=str(db), tables=["nope"])

    csv = tmp_path / "demographic.csv"
    csv.write_text("ssid,BIRTH_DATE\nP1,2000-01-01\n")
    c = duckdb.connect()
    name = build_safe_source_view(c, "demographic", str(csv), ["PATID", "BIRTH_DATE", "PROVIDERID"])
    assert name == "src_demographic"
    assert c.execute("SELECT PATID, PROVIDERID FROM src_demographic").fetchone() == ("P1", None)
