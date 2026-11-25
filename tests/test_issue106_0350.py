"""0.35.0 (issue 106): data-quality and clinical-fidelity follow-ups of runs C10 to C17. Synthetic data only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))


def _drug_vocab(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (21600001, 'ACE inhibitors, plain', 'ATC', 'ATC 4th', 'C', 'C09AA', NULL),
        (21600002, 'Thiazides, plain', 'ATC', 'ATC 4th', 'C', 'C03AA', NULL),
        (1308216, 'lisinopril', 'RxNorm', 'Ingredient', 'S', '29046', NULL),
        (974166, 'hydrochlorothiazide', 'RxNorm', 'Ingredient', 'S', '5487', NULL),
        (1308217, 'lisinopril 10 MG Oral Tablet', 'RxNorm', 'Clinical Drug', 'S', '314076', NULL),
        (40000010, 'old lisinopril / HCTZ tablet', 'RxNorm', 'Clinical Drug', NULL, '1', 'D'),
        (40000011, 'Zestoretic', 'RxNorm', 'Brand Name', NULL, '2', NULL),
        (40000012, 'old plain lisinopril product', 'RxNorm', 'Branded Drug', NULL, '3', 'D'))
        t(concept_id, concept_name, vocabulary_id, concept_class_id, standard_concept, concept_code, invalid_reason)""")
    con.execute("""CREATE TABLE concept_ancestor AS SELECT * FROM (VALUES
        (21600001, 1308216), (21600001, 1308217), (21600002, 974166), (1308216, 1308216), (1308216, 1308217), (974166, 974166))
        t(ancestor_concept_id, descendant_concept_id)""")
    con.execute("""CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
        (40000010, 1308216, 'RxNorm has ing', 'D'), (40000010, 974166, 'RxNorm has ing', 'D'),
        (40000011, 1308216, 'Brand name of', NULL), (40000011, 974166, 'Brand name of', NULL),
        (40000012, 1308217, 'Tradename of', 'D'))
        t(concept_id_1, concept_id_2, relationship_id, invalid_reason)""")


def _cohort(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE coh AS SELECT * FROM (VALUES
        (1, DATE '2020-06-01', DATE '2020-06-05'), (2, DATE '2020-06-01', DATE '2020-06-05'), (3, DATE '2020-06-01', DATE '2020-06-05'),
        (4, DATE '2020-06-01', DATE '2020-06-05')) t(subject_id, cohort_start_date, cohort_end_date)""")


def test_combination_products_count_for_every_ingredient_class():
    from omop_etl.medication_classes import extract_medication_classes

    con = duckdb.connect()
    _drug_vocab(con)
    _cohort(con)
    con.execute("""CREATE TABLE drug_exposure AS SELECT * FROM (VALUES
        (1, 1, 40000010, DATE '2020-03-01', 0),   -- deprecated combination drug: both classes
        (2, 2, 40000011, DATE '2020-03-01', 0),   -- brand name of a combination: both classes
        (3, 3, 40000012, DATE '2020-03-01', 0),   -- deprecated branded drug -> clinical drug -> its ancestors via Tradename of hop
        (4, 4, 1308217, DATE '2020-03-01', 0))    -- standard drug: as before
        t(drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_type_concept_id)""")
    specs = {"ace": [21600001], "thiazide": [21600002]}
    got = extract_medication_classes(con, "coh", specs, strategy="standard", person_col="subject_id", window="pre_admission")
    assert got["ace"].tolist() == [1, 1, 1, 1] and got["thiazide"].tolist() == [1, 1, 0, 0]
    assert got.attrs["combination_flags"] == 5
    assert sum(got.attrs["combination_flags_by_class"].values()) == 5 and set(got.attrs["combination_flags_by_class"]) == {"ace", "thiazide"}
    off = extract_medication_classes(con, "coh", specs, strategy="standard", person_col="subject_id", window="pre_admission",
                                     combination_ingredients=False)
    assert off["ace"].tolist() == [0, 0, 0, 1] and off["thiazide"].tolist() == [0, 0, 0, 0]
    combo = extract_medication_classes(con, "coh", specs, strategy="standard", person_col="subject_id", window="pre_admission",
                                       disaggregate_combinations=True)
    assert combo["ace_combination"].tolist()[:2] == [1, 1]


def _lab_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE coh AS SELECT 1 AS subject_id, DATE '2020-06-01' AS cohort_start_date, DATE '2020-06-03' AS cohort_end_date, 10 AS visit_occurrence_id")
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE, "
                "measurement_datetime TIMESTAMP, value_as_number DOUBLE, unit_concept_id INT, unit_source_value VARCHAR)")
    con.execute("""INSERT INTO measurement VALUES
        (1, 1, 3004501, DATE '2020-06-01', NULL, 5.55, 8753, 'mmol/L'),      -- glucose 5.55 mmol/L = 100 mg/dL
        (2, 1, 3004501, DATE '2020-06-02', NULL, 120, 8840, 'mg/dL'),
        (3, 1, 3016723, DATE '2020-06-01', NULL, 88.42, 0, 'µmol/L'),       -- creatinine by its source text
        (4, 1, 3000963, DATE '2020-06-01', NULL, 120, NULL, 'g/L')            -- hemoglobin 120 g/L = 12 g/dL""")
    return con


def test_ucum_conversions_before_bounds():
    from omop_etl.benchmark_features import _measurement_stats, DEFAULT_UNIT_CONVERSIONS

    con = _lab_db()
    cohort = pd.DataFrame({"row_id": [0], "subject_id": [1], "cohort_start_date": [pd.Timestamp("2020-06-01")],
                           "cohort_end_date": [pd.Timestamp("2020-06-03")]})
    cmap = pd.DataFrame({"name": ["glucose", "creatinine", "hemoglobin"], "concept_id": [3004501, 3016723, 3000963]})
    bounds = {"glucose": (10, 1500), "creatinine": (0.1, 30), "hemoglobin": (1, 25)}
    st = _measurement_stats(con, cohort, cmap, 0, bounds, None, DEFAULT_UNIT_CONVERSIONS).set_index("name")
    assert st.loc["glucose", "mean"] == pytest.approx((5.55 * 18.016 + 120) / 2)
    assert st.loc["glucose", "n_converted"] == 1
    assert st.loc["creatinine", "mean"] == pytest.approx(1.0)
    assert st.loc["hemoglobin", "mean"] == pytest.approx(12.0)
    off = _measurement_stats(con, cohort, cmap, 0, bounds, None, None).set_index("name")
    assert off.loc["glucose", "n"] == 1 and "hemoglobin" not in off.index   # 5.55 and 120 g/dL fall outside the bounds unconverted
    con.execute("ALTER TABLE measurement DROP COLUMN unit_source_value")     # a measurement table without the unit text still works
    assert _measurement_stats(con, cohort, cmap, 0, bounds, None, DEFAULT_UNIT_CONVERSIONS).set_index("name").loc["glucose", "n_converted"] == 1


def test_backfill_condition_concepts():
    from omop_etl.build_omop_cdm import backfill_condition_concepts

    con = duckdb.connect()
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (45591027, 'E11.9', 'ICD10CM', 'Condition', NULL, 'E11.9', NULL),
        (45542736, 'E11.65', 'ICD10CM', 'Condition', NULL, 'E11.65', NULL),
        (44821787, '250.00', 'ICD9CM', 'Condition', NULL, '250.00', NULL),
        (45595484, 'Z79.4', 'ICD10CM', 'Observation', NULL, 'Z79.4', NULL),
        (201826, 'Type 2 diabetes mellitus', 'SNOMED', 'Condition', 'S', '44054006', NULL),
        (4193704, 'T2DM without complication', 'SNOMED', 'Condition', 'S', '313436004', NULL),
        (4198, 'Hyperglycemia', 'SNOMED', 'Condition', 'S', '80394007', NULL),
        (4300, 'old SNOMED concept', 'SNOMED', 'Condition', NULL, '1', 'U'),
        (46226, 'Long-term insulin use', 'SNOMED', 'Observation', 'S', '2', NULL))
        t(concept_id, concept_name, vocabulary_id, domain_id, standard_concept, concept_code, invalid_reason)""")
    con.execute("""CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
        (45591027, 4193704, 'Maps to', NULL), (44821787, 201826, 'Maps to', NULL),
        (45542736, 201826, 'Maps to', NULL), (45542736, 4198, 'Maps to', NULL),       -- two targets: one more row
        (45595484, 46226, 'Maps to', NULL),                                              -- Observation domain: left
        (4300, 201826, 'Concept replaced by', NULL))
        t(concept_id_1, concept_id_2, relationship_id, invalid_reason)""")
    con.execute("""CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES
        (1, 1, 0, DATE '2020-01-01', 'E11.9', 45591027),        -- concept 0, source concept set
        (2, 1, 0, DATE '2014-01-01', '25000', 0),               -- no source concept: undotted ICD-9 code by date
        (3, 1, 45542736, DATE '2020-01-01', 'E11.65', 0),       -- non-standard concept id itself
        (4, 1, 0, DATE '2020-01-01', 'Z79.4', 45595484),        -- maps to another domain
        (5, 1, 4300, DATE '2020-01-01', 'x', 0),                -- deprecated, replaced by a standard concept
        (6, 1, 201826, DATE '2020-01-01', 'E11.9', 45591027),   -- already standard: untouched
        (7, 1, 0, DATE '2020-01-01', 'XYZ', 0))                 -- nothing to map
        t(condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_source_value, condition_source_concept_id)""")
    got = backfill_condition_concepts(con)
    rows = con.execute("SELECT condition_occurrence_id, condition_concept_id, condition_source_concept_id FROM condition_occurrence ORDER BY 1").fetchall()
    assert rows == [(1, 4193704, 45591027), (2, 201826, 44821787), (3, 4198, 45542736), (4, 0, 45595484), (5, 201826, 4300),
                    (6, 201826, 45591027), (7, 0, 0), (8, 201826, 45542736)]
    assert got == {"non_standard_before": 6, "by_maps_to": 4, "rows_added": 1, "other_domain": 1, "no_target": 1, "non_standard_after": 2}
    assert backfill_condition_concepts(con)["by_maps_to"] == 0   # idempotent


def _stay_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT, "
                "birth_datetime TIMESTAMP, race_concept_id INT, ethnicity_concept_id INT)")
    con.execute("INSERT INTO person VALUES (1, 8507, 1950, 1, 1, NULL, 0, 0), (2, 8532, 1950, 1, 1, NULL, 0, 0)")
    con.execute("CREATE TABLE observation_period (observation_period_id BIGINT, person_id BIGINT, observation_period_start_date DATE, "
                "observation_period_end_date DATE, period_type_concept_id INT)")
    con.execute("INSERT INTO observation_period VALUES (1, 1, DATE '2015-01-01', DATE '2023-01-01', 0), (2, 2, DATE '2015-01-01', DATE '2023-01-01', 0)")
    con.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE, "
                "visit_end_date DATE, discharged_to_concept_id INT, discharged_to_source_value VARCHAR)")
    con.execute("""INSERT INTO visit_occurrence VALUES
        (10, 1, 9201, DATE '2020-01-01', DATE '2020-01-05', 0, 'A'),
        (11, 1, 9201, DATE '2020-01-05', DATE '2020-01-09', 8536, 'HO'),   -- starts on the discharge day: not a readmission as recorded
        (12, 1, 9202, DATE '2020-03-01', DATE '2020-03-01', 0, NULL),      -- follow-up evidence
        (20, 2, 9201, DATE '2020-01-01', DATE '2020-01-05', 0, 'A'),
        (21, 2, 9201, DATE '2020-01-20', DATE '2020-01-25', 0, 'A'),       -- a counted readmission
        (22, 2, 9202, DATE '2020-03-01', DATE '2020-03-01', 0, NULL)""")
    con.execute("CREATE TABLE death (person_id BIGINT, death_date DATE)")
    for t in ("condition_occurrence (person_id BIGINT, condition_start_date DATE)", "drug_exposure (person_id BIGINT, drug_exposure_start_date DATE)",
              "measurement (person_id BIGINT, measurement_date DATE)"):
        con.execute(f"CREATE TABLE {t}")
    return con


def test_episode_merging_sensitivity():
    from omop_etl.cohort import define_study_cohort

    con = _stay_db()
    kw = dict(materialise=False, sample_stay_rule="all", washin_days=0, min_admit_date="2019-01-01", max_admit_date="2020-12-31")
    base = define_study_cohort(con, **kw).sort_values(["subject_id", "cohort_start_date"])
    assert base["visit_occurrence_id"].tolist() == [10, 11, 20, 21]
    assert base.loc[base["visit_occurrence_id"] == 10, "outcome_flag"].item() == 0   # the day-0 stay is not counted
    merged = define_study_cohort(con, merge_episodes_days=0, **kw).sort_values(["subject_id", "cohort_start_date"])
    assert merged["visit_occurrence_id"].tolist() == [10, 20, 21]
    ep = merged[merged["visit_occurrence_id"] == 10].iloc[0]
    assert str(pd.Timestamp(ep["cohort_end_date"]).date()) == "2020-01-09"           # one episode, discharge of the later stay
    assert merged.attrs["definition"]["merge_episodes_days"] == 0
    assert con.execute("SELECT count(*) FROM visit_occurrence").fetchone()[0] == 6    # the CDM is unchanged


def test_run_study_sensitivity_cap_and_known_gaps(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import json

    from test_multisite_anonymous import _study

    from omop_etl.study_run import run_study

    db, keys = _study(tmp_path, monkeypatch, 3)
    for s in keys:
        c = duckdb.connect(str(db / f"omop_{s}.duckdb"))
        c.execute("CREATE TABLE observation_period AS SELECT person_id, DATE '2015-01-01' AS observation_period_start_date, "
                  "DATE '2026-11-01' AS observation_period_end_date FROM visit_occurrence")
        c.close()
    gaps = tmp_path / "known.txt"
    gaps.write_text(f"lab_ldl   # the sites did not extract LDL\n{keys[0]}:vitals\n", encoding="utf-8")
    out = tmp_path / "o"
    with pytest.raises(ValueError, match="follow-up"):
        run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
                  observation_period_cap="2022-10-15")
    r = run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
                  observation_period_cap="2023-12-31", known_source_gaps=gaps, sensitivity=True, sensitivity_seeds=[1, 2])
    rec = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    sens = rec["sensitivity"]
    assert sens["n_seeds"] == 2 and sens["n_seeds_word"] == "two" and sens["all_stays_n"] == len(r["data"])
    assert 0 < sens["all_stays_auc"] < 1 and len(sens["seed_auc_range"]) == 2
    tab = pd.read_csv(out / "Study_Sensitivity.csv")
    assert len(tab) == 3 and tab["Analysis"].iloc[-1].startswith("All eligible stays")
    assert rec["observation_period_cap"]["cap"] == "2023-12-31"
    assert {v["share_shortened"] for v in rec["observation_period_cap"]["by_site"].values()} == {1.0}
    assert set(rec["data_checks"]["by_cause"]) == {"source_gap", "intentional", "etl_mapping", "unexplained"}
    assert "medication_combination_share" in rec
    assert all(f.get("cause") for f in r["data_checks"])
    for s in keys:
        assert (Path(str(out) + "_private") / f"features_all_{s}.parquet").exists()
    from omop_etl.manuscript_text import manuscript_context, render_manuscript_body

    body = render_manuscript_body(manuscript_context(out / "Study_Manuscript_Tables.xlsx"))
    assert body["template"] == "0.35.0"
    text = " ".join(body["markdown"].split())
    assert "repeated random draws the AUC-ROC ranged from" in text and "repeat the draw with two further seeds" in text
    # reuse: the saved all-stays features are read back
    r2 = run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
                   observation_period_cap="2023-12-31", sensitivity=True, sensitivity_seeds=[1, 2], reuse_features=True)
    assert len(r2["data"]) == len(r["data"])
    # features saved under older default feature rules (0.34.2: no feature_rules, no unit_converted) are not reused silently
    for s in keys:
        mp = Path(str(out) + "_private") / f"features_{s}.meta.json"
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["settings"].pop("feature_rules")
        meta.pop("unit_converted", None)
        mp.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(Exception, match="feature_rules"):
        run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
                  observation_period_cap="2023-12-31", sensitivity=True, sensitivity_seeds=[1, 2], reuse_features=True)


def test_known_source_gaps_and_intentional_exclusions():
    from omop_etl.data_gaps import assess, classify_findings, collapse_concept_set_exclusions, load_known_source_gaps

    known = load_known_source_gaps({"lab_potassium": "not extracted", "siteX:vitals": ""})
    f = [{"site": "Site A", "check": "column_missing", "column": "lab_potassium_last", "severity": "error", "message": "m"},
         {"site": "Site A", "check": "group_missing", "group": "vitals", "severity": "error", "message": "m"},
         {"site": "Site B", "check": "group_missing", "group": "vitals", "severity": "error", "message": "m"},
         {"site": "Site A", "check": "concept_set_coverage", "column": "lab_ast", "severity": "error", "message": "m"},
         {"site": "Site A", "check": "concept_set_exclusion", "column": "lab_pco2", "codes": ["2019-8"], "severity": "info", "message": "m"},
         {"site": "Site B", "check": "concept_set_exclusion", "column": "lab_pco2", "codes": ["2019-8", "11557-6"], "severity": "info", "message": "m"}]
    classify_findings(f, known, {"Site A": "siteX", "Site B": "siteY"})
    assert [x["cause"] for x in f] == ["source_gap", "source_gap", "unexplained", "etl_mapping", "intentional", "intentional"]
    assert f[0]["known_reason"] == "not extracted"
    out = collapse_concept_set_exclusions(f)
    exc = [x for x in out if x["check"] == "concept_set_exclusion"]
    assert len(exc) == 1 and exc[0]["site"] == "all" and exc[0]["codes"] == ["11557-6", "2019-8"] and exc[0]["n_sites"] == 2
    with pytest.raises(Exception):   # the unexplained vitals gap at Site B and the ETL gap still fail
        assess(out)
    assess(out, ["vitals", "lab_ast"])
    assert out[0]["allowed"] and out[1]["allowed"]


def _sq():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import site_quality_report as sq

    return sq


def test_site_report_readmission_rule_units_and_causes(tmp_path):
    """Issue 106 items 3, 4 and 7 in scripts/site_quality_report.py. Made-up sites and rows."""
    sq = _sq()
    priv, db = tmp_path / "priv", tmp_path / "db"
    priv.mkdir()
    db.mkdir()
    con = duckdb.connect(str(db / "omop_s1.duckdb"))
    con.execute("""CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES
        (1, 10, 9201, DATE '2020-01-01', DATE '2020-01-05'),
        (1, 11, 9201, DATE '2020-01-05', DATE '2020-01-07'),   -- starts on the discharge day: day 0
        (2, 20, 9201, DATE '2020-02-01', DATE '2020-02-10'),
        (2, 21, 9201, DATE '2020-02-03', DATE '2020-02-04'),   -- starts inside the stay
        (3, 30, 9201, DATE '2020-03-01', DATE '2020-03-03'),
        (3, 31, 9201, DATE '2020-03-10', DATE '2020-03-12'))   -- a counted readmission
        t(person_id, visit_occurrence_id, visit_concept_id, visit_start_date, visit_end_date)""")
    rows = ", ".join([f"(3004501, 8840, {100 + i}, DATE '2020-06-01', 'mg/dL')" for i in range(20)]
                     + [f"(3004501, 8753, {5.5 + i / 100}, DATE '2020-06-01', 'mmol/L')" for i in range(20)]
                     + [f"(3000905, 0, {7 + i / 10}, DATE '2020-06-01', '10*3/uL')" for i in range(20)]
                     + [f"(3000905, 0, {7000 + i}, DATE '2020-06-01', '/uL')" for i in range(20)])
    con.execute(f"CREATE TABLE measurement AS SELECT * FROM (VALUES {rows}) "
                "t(measurement_concept_id, unit_concept_id, value_as_number, measurement_date, unit_source_value)")
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (3004501, 'Glucose [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3000905, 'Leukocytes [#/volume] in Blood by Automated count', 'Measurement', 'LOINC', 'Lab Test', 'S', '6690-2', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("CREATE TABLE concept_ancestor AS SELECT concept_id AS ancestor_concept_id, concept_id AS descendant_concept_id, "
                "0 AS min_levels_of_separation, 0 AS max_levels_of_separation FROM concept")
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR, "
                "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)")
    con.close()
    pd.DataFrame({"subject_id": [1, 2, 3], "visit_occurrence_id": [10, 20, 30],
                  "cohort_start_date": pd.to_datetime(["2020-01-01", "2020-02-01", "2020-03-01"]),
                  "cohort_end_date": pd.to_datetime(["2020-01-05", "2020-02-10", "2020-03-03"]),
                  "outcome_flag": [0, 0, 1]}).to_parquet(priv / "cohort_s1.parquet")
    sites = {"s1": "Site A"}
    ov = sq.readmission_overlap(priv, db, sites, deadline=float("inf")).iloc[0]
    assert ov["pct_stays_day0"] == pytest.approx(100 / 3, abs=0.01) and ov["pct_stays_overlapping"] == pytest.approx(100 / 3, abs=0.01)
    assert ov["pct_of_readmissions"] == 200.0 and ov["pct_stays_not_counted"] == pytest.approx(200 / 3, abs=0.01)
    um = sq.unit_medians(db, sites, "2020-01-01", "2020-12-31", deadline=float("inf")).set_index("name")
    assert not um.loc["glucose", "flag"]            # mmol/L converted to mg/dL: medians agree
    assert um.loc["wbc", "flag"] and um.loc["wbc", "ratio"] > 500   # /uL vs 10*3/uL: not converted, flagged

    funnel = pd.DataFrame({"site": ["Site A", "Site B", "Site C", "Site D"], "name": ["wbc", "albumin", "ast", "potassium"],
                           "source_rows": [10, 10, 0, 0], "concept_rows": [10, 1, 0, 0], "pct_mapped": [100.0, 10.0, None, None],
                           "pct_no_number": [80.0, 0.0, None, None]})
    causes = sq.funnel_causes(funnel, {"lab_potassium": "not extracted"}, {}).tolist()
    assert causes[0].startswith("no numeric value") and causes[1].startswith("ETL mapping gap")
    assert causes[2].startswith("no rows") and causes[3] == "source gap (confirmed)"
    funnel["cause"] = causes
    for c in ("pct_no_date", "pct_out_of_bounds"):
        funnel[c] = 0.0
    empty = pd.DataFrame()
    fl = sq.flags(empty, empty, empty, empty, empty, 10, 2, funnel=funnel, overlap=pd.DataFrame([ov]),
                  units=um.reset_index().assign(site="Site A"), known={"lab_potassium": "not extracted"})
    measures = {f["measure"]: f for f in fl}
    assert any(f["domain"] == "Readmission rule" for f in fl) and any(f["domain"] == "Lab units" for f in fl)
    assert measures["lab_potassium: source gap (confirmed)"].get("expected") is True
    assert "Site" not in json.dumps(fl)


def test_pr107_review_fixes(tmp_path):
    """PR 107 review: indexed backfill, NULL-end episodes, Path spec, ETL checks not masked, hemoglobin = hematocrit / 3, seed Brier."""
    from omop_etl.build_omop_cdm import backfill_condition_concepts
    from omop_etl.cohort import episode_view_sql
    from omop_etl.data_gaps import classify_findings, load_known_source_gaps
    from omop_etl.study_run import drop_duplicate_predictors, hemoglobin_is_hematocrit

    # one UPDATE: an index on the source concept column must not make the concept update miss the row
    con = duckdb.connect()
    con.execute("CREATE TABLE concept AS SELECT * FROM (VALUES (45591027, 'E11.9', 'ICD10CM', 'Condition', NULL, 'E11.9', NULL), "
                "(4193704, 'T2DM', 'SNOMED', 'Condition', 'S', '313436004', NULL)) "
                "t(concept_id, concept_name, vocabulary_id, domain_id, standard_concept, concept_code, invalid_reason)")
    con.execute("CREATE TABLE concept_relationship AS SELECT * FROM (VALUES (45591027, 4193704, 'Maps to', NULL)) "
                "t(concept_id_1, concept_id_2, relationship_id, invalid_reason)")
    con.execute("CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (1, 1, 0, DATE '2020-01-01', 'E11.9', 0)) "
                "t(condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_source_value, condition_source_concept_id)")
    con.execute("CREATE INDEX ix_src ON condition_occurrence (condition_source_concept_id)")
    got = backfill_condition_concepts(con)
    assert con.execute("SELECT condition_concept_id, condition_source_concept_id FROM condition_occurrence").fetchall() == [(4193704, 45591027)]
    assert got["non_standard_after"] == 0

    # an open stay (no end date) merged into an episode does not become its "last" stay
    v = duckdb.connect()
    v.execute("""CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES
        (10, 1, 9201, TIMESTAMP '2020-01-01', TIMESTAMP '2020-01-05', 8536, 'HO'),
        (11, 1, 9201, TIMESTAMP '2020-01-03', NULL, 0, 'SH'))
        t(visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, discharged_to_source_value)""")
    ep = v.execute(episode_view_sql(v, "visit_occurrence", [9201], 0)).df()
    assert len(ep) == 1 and ep["discharged_to_source_value"].item() == "HO" and ep["visit_occurrence_id"].item() == 10

    with pytest.raises(ValueError, match="does not exist"):
        load_known_source_gaps(tmp_path / "missing.txt")
    f = classify_findings([{"site": "Site A", "check": "concept_set_coverage", "column": "lab_potassium"},
                           {"site": "Site A", "check": "column_missing", "column": "lab_potassium"}], {"lab_potassium": ""}, {})
    assert [x["cause"] for x in f] == ["etl_mapping", "source_gap"]

    d = pd.DataFrame({"lab_hematocrit": [30.0, 36.0, None], "lab_hemoglobin": [10.0, 12.0, None], "lab_hematocrit_last": [30.0, 33.0, None]})
    assert hemoglobin_is_hematocrit(d)
    assert list(drop_duplicate_predictors(d).columns) == ["lab_hemoglobin"]
    from omop_etl.manuscript_tables import _manuscript_labs

    labs = dict((lab, names) for names, lab in _manuscript_labs(drop_duplicate_predictors(d)))
    assert labs.get("Hemoglobin") == ("lab_hemoglobin",) and "Hematocrit" not in labs   # Tables 1 and 4 keep a row for it
    d.loc[1, "lab_hemoglobin"] = 11.0      # one measured hemoglobin: two predictors
    assert not hemoglobin_is_hematocrit(d) and "lab_hematocrit" in drop_duplicate_predictors(d)


def test_stay_sensitivity_brier_and_no_seeds():
    pytest.importorskip("sklearn")
    import numpy as np

    from omop_etl.study_report import stay_sensitivity

    rng = np.random.default_rng(0)
    n = 300
    d = pd.DataFrame({"site_source": "s1", "person_id": rng.integers(0, 120, n), "visit_occurrence_id": np.arange(n),
                      "x": rng.normal(size=n)})
    d["readmitted_30d"] = (d["x"] + rng.normal(size=n) > 1).astype(int)
    tab, summ = stay_sensitivity(d, [1, 2], n_splits=3)
    assert tab["Brier"].notna().all()
    tab0, summ0 = stay_sensitivity(d, [], n_splits=3)
    assert len(tab0) == 1 and summ0["seed_auc_range"] is None
