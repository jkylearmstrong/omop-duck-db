"""v0.12.0: disclosure control, CONSORT from counts, parity audit, pooled Table 1, site summary packages,
mapping coverage, vital-sign loading and run manifests. Synthetic data only: every count and value is generated here."""

from __future__ import annotations

import json

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    DisclosureError, RunManifest, SiteSummaryError, assert_aggregate_only, assert_disclosure_safe, audit_matrix_parity,
    build_table1, consort_from_counts, export_site_summary, format_count, generate_consort_attrition, load_vital,
    mapping_coverage_report, pool_consort_tables, pool_site_packages, pool_table1_summaries, small_cell_mask,
    suppress_attrition_table, suppress_small_counts, summarize_table1_site, verify_manifest, vocabulary_info,
)
from omop_etl.build_omop_cdm import build_schema, load_macros
from omop_etl.run_manifest import ManifestVerificationError

# ---- ODD-5: disclosure control ------------------------------------------------------------------------------------------


def test_small_cell_mask_ignores_zero_and_missing():
    m = small_cell_mask([0, 1, 10, 11, 500, None], 11)
    assert m.tolist() == [False, True, True, False, False, False]


def test_format_count():
    assert format_count(1234) == "1,234"
    assert format_count(5) == "<11"
    assert format_count(float("nan")) == "<11"
    assert format_count(0) == "0"


def test_threshold_must_be_positive_int():
    with pytest.raises(ValueError):
        small_cell_mask([1], 0)
    with pytest.raises(ValueError):
        suppress_small_counts(pd.DataFrame({"n": [1]}), ["n"], True)


def test_suppress_small_counts_blanks_counts_and_dependents():
    df = pd.DataFrame({"g": ["a", "b", "c"], "n": [5, 0, 400], "pct": [1.2, 0.0, 98.8]})
    out = suppress_small_counts(df, ["n"], 11, also=["pct"])
    assert np.isnan(out.loc[0, "n"]) and np.isnan(out.loc[0, "pct"])
    assert out.loc[1, "n"] == 0 and out.loc[2, "n"] == 400          # zero is not a small cell
    assert out["suppressed"].tolist() == [True, False, False]
    assert df.loc[0, "n"] == 5                                      # the input is untouched


def test_complementary_suppression_blocks_subtraction():
    df = pd.DataFrame({"grp": ["x", "x", "x", "y", "y"], "n": [4, 60, 300, 80, 90]})
    out = suppress_small_counts(df, ["n"], 11, group_by="grp")
    assert np.isnan(out.loc[0, "n"])
    assert np.isnan(out.loc[1, "n"])                                # smallest remaining cell in group x also hidden
    assert out.loc[2, "n"] == 300 and out.loc[3, "n"] == 80         # group y untouched
    no_comp = suppress_small_counts(df, ["n"], 11, group_by="grp", complementary=False)
    assert no_comp.loc[1, "n"] == 60


def test_assert_disclosure_safe():
    with pytest.raises(DisclosureError):
        assert_disclosure_safe(pd.DataFrame({"n": [3, 50]}), ["n"], 11)
    assert_disclosure_safe(suppress_small_counts(pd.DataFrame({"n": [3, 50]}), ["n"], 11), ["n"], 11)


def _exact_unknowns(out: pd.DataFrame) -> bool:
    """True if any hidden retained/dropped value is pinned down by a visible equation retained[i-1] - dropped[i] = retained[i]."""
    r, d = out["subjects_retained"].to_numpy(), out["subjects_dropped"].to_numpy()
    for i in range(1, len(out)):
        if int(np.isnan(r[i - 1])) + int(np.isnan(d[i])) + int(np.isnan(r[i])) == 1:
            return True
    shown = [i for i in range(len(out)) if not np.isnan(r[i])]
    return any(b - a > 1 and 0 < r[a] - r[b] < 11 for a, b in zip(shown, shown[1:]))


def test_attrition_suppression_leaves_nothing_recoverable():
    rng = np.random.default_rng(7)
    for _ in range(400):
        n = int(rng.integers(2, 8))
        ret = [int(rng.choice([rng.integers(0, 15), rng.integers(100, 4000)]))]
        for _ in range(n - 1):
            ret.append(max(ret[-1] - int(rng.choice([0, rng.integers(1, 12), rng.integers(20, 500)])), 0))
        ret = np.array(ret, float)
        df = pd.DataFrame({"subjects_retained": ret, "subjects_dropped": np.r_[0, ret[:-1] - ret[1:]],
                           "percent_retained": 100 * ret / max(ret[0], 1)})
        out = suppress_attrition_table(df, 11)
        assert not _exact_unknowns(out), (ret.tolist(), out.values.tolist())
        shown = out["subjects_retained"].dropna()
        assert not ((shown > 0) & (shown < 11)).any()


# ---- ODD-1: CONSORT attrition from counts ----------------------------------------------------------------------------------

REFERENCE = pd.DataFrame({
    "step_number": [1, 2, 3, 4, 5],
    "step_name": ["Source population", "Washout", "Age < 18", "Died in hospital", "Transferred out"],
    "subjects_retained": [1000, 900, 850, 820, 800],
    "subjects_dropped": [0, 100, 50, 30, 20],
    "percent_retained": [100.0, 90.0, 85.0, 82.0, 80.0],
})
EXCLUSIONS = [("Washout", 100), ("Age < 18", 50), ("Died in hospital", 30), ("Transferred out", 20)]


def test_consort_from_counts_matches_hand_built_reference():
    got = consort_from_counts(1000, EXCLUSIONS)
    pd.testing.assert_frame_equal(got[REFERENCE.columns].reset_index(drop=True), REFERENCE, check_dtype=False)
    assert got["exclusion_reason"].tolist()[1:] == [e[0] for e in EXCLUSIONS]


def test_consort_accepts_remaining_dict_and_reasons():
    got = consort_from_counts(1000, [{"label": "Washout", "remaining": 900, "reason": "Prior use in 12 months"},
                                     {"label": "Age", "excluded": 50}])
    assert got["subjects_retained"].tolist() == [1000, 900, 850]
    assert got["exclusion_reason"].iloc[1] == "Prior use in 12 months"
    assert consort_from_counts(10, {"A": 3})["subjects_retained"].tolist() == [10, 7]


def test_consort_rejects_impossible_counts():
    with pytest.raises(ValueError):
        consort_from_counts(10, [("too many", 11)])
    with pytest.raises(ValueError):
        consort_from_counts(10, [("negative", -1)])
    with pytest.raises(ValueError):
        consort_from_counts(10, [{"label": "x"}])


def test_generate_consort_attrition_from_counts_and_exports(tmp_path):
    att = generate_consort_attrition(initial_n=1000, exclusions=EXCLUSIONS)
    assert att.to_dataframe()["subjects_retained"].tolist() == [1000, 900, 850, 820, 800]
    assert "N = 800, 80.0%" in att.to_mermaid()
    assert "Excluded: 100" in att.to_mermaid()
    path = att.to_csv(tmp_path / "attrition.csv")
    back = pd.read_csv(path)
    assert back["subjects_retained"].tolist() == [1000, 900, 850, 820, 800]
    assert att.to_latex() and att.to_typst() and att.to_tikz()


def test_generate_consort_attrition_pools_sites_with_suppression(tmp_path):
    a = consort_from_counts(1000, EXCLUSIONS)
    b = consort_from_counts(500, [("Washout", 50), ("Age < 18", 20), ("Died in hospital", 4), ("Transferred out", 3)])
    pooled = generate_consort_attrition(sites={"site_a": a, "site_b": {"initial_n": 500, "exclusions": [
        ("Washout", 50), ("Age < 18", 20), ("Died in hospital", 4), ("Transferred out", 3)]}})
    df = pooled.to_dataframe()
    assert df["n_sites"].eq(2).all()
    assert df["subjects_retained"].iloc[:3].tolist() == [1500, 1350, 1280]       # 1000+500, 900+450, 850+430
    # pooled drops at the last two steps: 30+4 = 34 and 20+3 = 23 -> both >= 11, nothing hidden
    assert df["subjects_dropped"].tolist() == [0, 150, 70, 34, 23]
    assert not df["suppressed"].any()
    # a pooled step with a small exclusion is hidden, and so is anything that would give it back
    c = consort_from_counts(40, [("Washout", 5), ("Age < 18", 3)])
    small = generate_consort_attrition(sites={"s1": c, "s2": consort_from_counts(40, [("Washout", 2), ("Age < 18", 1)])})
    sdf = small.to_dataframe()
    assert sdf["suppressed"].any()
    assert not _exact_unknowns(sdf[["subjects_retained", "subjects_dropped"]])
    text = small.to_mermaid()
    assert "<11" in text and "nan" not in text.lower()
    csv = small.to_csv(tmp_path / "p.csv")
    assert "<11" in csv.read_text()
    assert "<11" in small.to_typst() and "<11" in small.to_tikz()


def test_pool_consort_rejects_mismatched_steps():
    a = consort_from_counts(100, [("Washout", 10)])
    b = consort_from_counts(100, [("Different", 10)])
    with pytest.raises(ValueError, match="different steps"):
        pool_consort_tables({"a": a, "b": b})


def test_pool_consort_unknown_site_cell_makes_pool_unknown():
    a = suppress_attrition_table(consort_from_counts(200, [("Washout", 5), ("Age", 50)]), 11)
    b = consort_from_counts(200, [("Washout", 40), ("Age", 50)])
    pooled = pool_consort_tables({"a": a, "b": b}, min_cell=11)
    assert not _exact_unknowns(pooled)
    assert pooled["subjects_retained"].isna().any()


def test_consort_flowchart_renders_suppressed_table(tmp_path):
    pytest.importorskip("matplotlib")
    from omop_etl import export_consort_flowchart

    small = generate_consort_attrition(initial_n=30, exclusions=[("Washout", 5), ("Age", 3)], min_cell=11)
    out = export_consort_flowchart(small, tmp_path / "f.png")
    assert out.exists() and out.stat().st_size > 0


def test_generate_consort_attrition_from_database_steps_is_unchanged():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "SELECT i, 8507, 1960 + i, 0, 0 FROM range(1, 41) t(i)")
    att = generate_consort_attrition(con=con, steps=[("All", "SELECT person_id AS subject_id FROM person"),
                                                     ("Born after 1980", "SELECT person_id AS subject_id FROM person WHERE year_of_birth > 1980")])
    assert att.to_dataframe()["subjects_retained"].tolist() == [40, 20]


# ---- ODD-4: parity audit ---------------------------------------------------------------------------------------------------


def _parity_frames(n=400, seed=0):
    rng = np.random.default_rng(seed)
    legacy = pd.DataFrame({"key": np.arange(n), "age": rng.normal(60, 10, n), "sbp": rng.normal(125, 15, n),
                           "sex": rng.choice(["F", "M"], n), "site": rng.choice(["s1", "s2"], n)})
    omop = legacy.copy()
    omop["sbp"] = omop["sbp"] + 8                       # a systematic shift: should be flagged by SMD
    omop.loc[:59, "age"] = np.nan                       # 15% newly missing: should be flagged by the missingness gap
    omop.loc[:39, "sex"] = np.where(legacy.loc[:39, "sex"] == "F", "M", "F")  # 10% flipped
    return legacy, omop


def test_parity_audit_flags_known_differences():
    legacy, omop = _parity_frames()
    audit = audit_matrix_parity(legacy, omop, "key", columns=["age", "sbp", "sex"], min_cell=11)
    led = audit.ledger.set_index("column")
    assert led.loc["sbp", "flagged"] and abs(led.loc["sbp", "smd"]) > 0.4
    assert led.loc["sbp", "correlation"] > 0.999                       # a pure shift keeps the correlation
    assert led.loc["sbp", "ccc"] < 0.95                                # ...but not the concordance
    assert led.loc["age", "flagged"] and abs(led.loc["age", "missingness_gap"] - 0.15) < 1e-9
    assert led.loc["sex", "kind"] == "categorical"
    assert abs(led.loc["sex", "pct_agreement"] - 90.0) < 1e-9
    assert led.loc["sex", "kappa"] < 0.85
    assert set(audit.flagged) == {"age", "sbp", "sex"}
    assert set(audit.levels["level"]) == {"F", "M"}
    assert audit.keys["n_matched"].iloc[0] == 400


def test_parity_identical_tables_are_clean():
    legacy, _ = _parity_frames()
    audit = audit_matrix_parity(legacy, legacy.copy(), "key", columns=["age", "sbp", "sex"])
    assert audit.flagged == []
    led = audit.ledger.set_index("column")
    assert led.loc["sbp", "smd"] == 0 and led.loc["sbp", "ccc"] == pytest.approx(1.0)
    assert led.loc["sex", "kappa"] == pytest.approx(1.0)


def test_parity_by_site_and_key_mismatch_counts():
    legacy, omop = _parity_frames()
    omop = omop.iloc[:-20]                              # 20 patients only in the legacy table
    audit = audit_matrix_parity(legacy, omop, "key", columns=["sbp"], by="site", min_cell=11)
    assert set(audit.ledger["stratum"]) == {"ALL", "s1", "s2"}
    assert audit.keys["n_only_legacy"].iloc[0] == 20 and audit.keys["n_only_omop"].iloc[0] == 0
    assert "key" not in audit.ledger.columns                            # key values are never written


def test_parity_rejects_duplicate_keys_and_suppresses_small_strata():
    legacy, omop = _parity_frames(n=50)
    with pytest.raises(ValueError, match="duplicate"):
        audit_matrix_parity(pd.concat([legacy, legacy.iloc[:1]]), omop, "key")
    tiny = audit_matrix_parity(legacy.iloc[:8], omop.iloc[:8], "key", columns=["sbp"])
    assert tiny.ledger["suppressed"].all() and tiny.ledger["smd"].isna().all()
    assert tiny.keys["suppressed"].all()
    exact = audit_matrix_parity(legacy.iloc[:8], omop.iloc[:8], "key", columns=["sbp"], min_cell=None)
    assert exact.ledger["smd"].notna().all()


def test_parity_to_csv(tmp_path):
    legacy, omop = _parity_frames()
    paths = audit_matrix_parity(legacy, omop, "key", columns=["sbp", "sex"]).to_csv(tmp_path)
    assert all(p.exists() for p in paths.values())


# ---- ODD-6: pooled Table 1 ------------------------------------------------------------------------------------------------


def _t1_site(n, shift, seed):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "exposed": rng.choice(["No", "Yes"], n), "age": rng.normal(60 + shift, 10, n),
        "creatinine": np.where(rng.random(n) < 0.1, np.nan, rng.normal(1.0, 0.3, n)),
        "sex": rng.choice(["F", "M"], n), "diabetes": rng.random(n) < 0.3,
    })


KW = dict(continuous=["age", "creatinine"], categorical=["sex"], binary=["diabetes"], group_col="exposed")


def test_pooled_summary_equals_summary_of_concatenated_data():
    a, b = _t1_site(600, 0, 1), _t1_site(900, 4, 2)
    pooled = pool_table1_summaries({"a": summarize_table1_site(a, **KW), "b": summarize_table1_site(b, **KW)})
    single = summarize_table1_site(pd.concat([a, b]), **KW)
    m = pooled.merge(single, on=["group", "variable", "kind", "level"], suffixes=("_p", "_s"))
    assert len(m) == len(single) == len(pooled)
    for c in ["n", "n_missing", "mean", "sd", "count"]:
        assert np.allclose(m[c + "_p"].astype(float), m[c + "_s"].astype(float), equal_nan=True), c


def test_table1_values_and_smd_against_hand_calculation():
    df = pd.DataFrame({"g": ["A"] * 100 + ["B"] * 100,
                       "x": list(np.arange(100.0)) + list(np.arange(100.0) + 10),   # means 49.5 and 59.5, equal SD
                       "flag": [True] * 30 + [False] * 70 + [True] * 50 + [False] * 50})
    t = build_table1(summarize_table1_site(df, continuous=["x"], binary=["flag"], group_col="g"), group_order=["A", "B"])
    sd = np.arange(100.0).std(ddof=1)
    row = t[t.variable == "x"].iloc[0]
    assert row["A"] == f"49.5 ({sd:.1f})" and row["B"] == f"59.5 ({sd:.1f})"
    assert float(row["SMD"]) == pytest.approx(-10 / sd, abs=1e-3)
    frow = t[t.variable == "flag"].iloc[0]
    assert frow["A"] == "30 (30.0%)" and frow["B"] == "50 (50.0%)"
    p1, p0 = 0.3, 0.5
    assert float(frow["SMD"]) == pytest.approx((p1 - p0) / np.sqrt((p1 * (1 - p1) + p0 * (1 - p0)) / 2), abs=1e-3)
    assert t.iloc[0]["variable"] == "N" and t.iloc[0]["A"] == "100"


def test_table1_pooled_matches_single_site_table():
    a, b = _t1_site(600, 0, 3), _t1_site(900, 4, 4)
    pooled = pool_table1_summaries([summarize_table1_site(a, **KW), summarize_table1_site(b, **KW)])
    single = summarize_table1_site(pd.concat([a, b]), **KW)
    pd.testing.assert_frame_equal(build_table1(pooled, group_order=["No", "Yes"], min_cell=None),
                                  build_table1(single, group_order=["No", "Yes"], min_cell=None))


def test_table1_suppresses_small_cells_and_their_smd():
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"g": rng.choice(["A", "B"], 400), "sex": rng.choice(["F", "M", "X"], 400, p=[0.5, 0.49, 0.01])})
    df.loc[df.index[:4], "sex"] = "X"
    t = build_table1(summarize_table1_site(df, categorical=["sex"], group_col="g"), group_order=["A", "B"], min_cell=11)
    levels = t[(t.variable == "sex") & (t.level != "")]
    x = levels[levels.level == "X"].iloc[0]
    assert x["A"].startswith("<11") or x["B"].startswith("<11")
    flat = " ".join(map(str, t.values.ravel()))
    assert "nan" not in flat.lower()
    header = t[(t.variable == "sex") & (t.level == "")].iloc[0]
    assert header["SMD"] == ""                                       # a hidden level hides the variable's SMD


def test_table1_needs_two_groups_for_smd():
    df = pd.DataFrame({"g": ["A", "B", "C"] * 20, "x": np.arange(60.0)})
    s = summarize_table1_site(df, continuous=["x"], group_col="g")
    with pytest.raises(ValueError):
        build_table1(s)
    assert "SMD" not in build_table1(s, smd=False).columns


# ---- ODD-7: aggregate-only site export -----------------------------------------------------------------------------------


def test_assert_aggregate_only_rejects_patient_level_tables():
    with pytest.raises(SiteSummaryError, match="identifiers"):
        assert_aggregate_only(pd.DataFrame({"person_id": [1], "n": [20]}))
    with pytest.raises(SiteSummaryError, match="identifiers"):
        assert_aggregate_only(pd.DataFrame({"measurement_date": ["2020-01-01"], "n": [20]}))
    with pytest.raises(SiteSummaryError, match="date"):
        assert_aggregate_only(pd.DataFrame({"when": pd.to_datetime(["2020-01-01"]), "n": [20]}))
    with pytest.raises(SiteSummaryError, match="rows"):
        assert_aggregate_only(pd.DataFrame({"n": range(6000)}))
    assert_aggregate_only(pd.DataFrame({"concept_id": [3004249], "n": [20]}))


def _make_package(path, label, seed, n=600):
    df = _t1_site(n, seed, seed)
    att = consort_from_counts(n, [("Washout", n // 10), ("Age < 18", n // 20), ("Died", 6)])
    return export_site_summary(path, label, attrition=att, table1_summary=summarize_table1_site(df, **KW))


def test_export_and_pool_site_packages(tmp_path):
    files_a = _make_package(tmp_path / "a", "site_a", 1)
    _make_package(tmp_path / "b", "site_b", 2, n=800)
    assert {"attrition", "table1_summary", "manifest"} <= set(files_a)
    man = json.loads(files_a["manifest"].read_text())
    assert man["extra"]["site_label"] == "site_a" and set(man["outputs"]) == {"attrition.csv", "table1_summary.csv"}
    assert str(tmp_path) not in files_a["manifest"].read_text()           # no local paths in the package
    att = pd.read_csv(files_a["attrition"])
    assert att["subjects_dropped"].isna().iloc[3]                          # the 6 who died are suppressed at the site
    pooled = pool_site_packages({"a": tmp_path / "a", "b": tmp_path / "b"}, group_order=["No", "Yes"])
    assert pooled["sites"] == ["site_a", "site_b"]
    assert pooled["attrition"]["subjects_retained"].iloc[0] == 1400
    assert pooled["table1"].iloc[0]["variable"] == "N"
    assert not _exact_unknowns(pooled["attrition"][["subjects_retained", "subjects_dropped"]])


def test_pool_detects_tampering_and_duplicate_labels(tmp_path):
    _make_package(tmp_path / "a", "site_a", 1)
    _make_package(tmp_path / "b", "site_a", 2)
    with pytest.raises(SiteSummaryError, match="same|two packages"):
        pool_site_packages({"a": tmp_path / "a", "b": tmp_path / "b"})
    _make_package(tmp_path / "c", "site_c", 3)
    with open(tmp_path / "c" / "attrition.csv", "a") as fh:
        fh.write("9,Extra,1,0,0.0,\n")
    with pytest.raises(SiteSummaryError, match="changed"):
        pool_site_packages({"a": tmp_path / "a", "c": tmp_path / "c"})


def test_export_validates_label_threshold_and_content(tmp_path):
    att = consort_from_counts(100, [("Washout", 20)])
    for bad in ("/data/site", "../x", "Real Hospital Name", "x" * 40, ""):
        with pytest.raises(SiteSummaryError):
            export_site_summary(tmp_path / "o", bad, attrition=att)
    with pytest.raises(SiteSummaryError):
        export_site_summary(tmp_path / "o", "site_a", attrition=att, min_cell=1)
    with pytest.raises(SiteSummaryError, match="nothing to export"):
        export_site_summary(tmp_path / "o", "site_a")
    with pytest.raises(SiteSummaryError, match="identifiers"):
        export_site_summary(tmp_path / "o", "site_a", tables={"cohort": pd.DataFrame({"person_id": [1, 2], "n": [30, 40]})})
    legacy, omop = _parity_frames()
    with pytest.raises(SiteSummaryError, match="not suppressed"):
        export_site_summary(tmp_path / "o", "site_a", parity=audit_matrix_parity(legacy, omop, "key", min_cell=None))


def test_export_with_parity_extra_tables_inputs_and_vocabulary(tmp_path):
    legacy, omop = _parity_frames()
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO vocabulary (vocabulary_id, vocabulary_name, vocabulary_version, vocabulary_concept_id) "
                "VALUES ('None', 'OMOP Standardized Vocabularies', 'v5.0 01-JAN-99', 44819096)")
    src = tmp_path / "input.csv"
    src.write_text("a,b\n1,2\n")
    counts = pd.DataFrame({"concept_id": [1, 2, 3], "n": [4, 50, 600]})
    out = export_site_summary(tmp_path / "pkg", "site_z", parity=audit_matrix_parity(legacy, omop, "key", columns=["sbp"]),
                              tables={"concept_counts": counts}, count_cols={"concept_counts": ["n"]}, con=con, inputs=[src])
    assert pd.read_csv(out["concept_counts"])["n"].isna().tolist() == [True, False, False]
    man = json.loads(out["manifest"].read_text())
    assert man["vocabulary"]["vocabulary_version"] == "v5.0 01-JAN-99"
    assert list(man["inputs"]) == ["input.csv"]


# ---- ODD-8: mapping coverage -------------------------------------------------------------------------------------------------


def test_mapping_coverage_report_counts_rows_and_codes():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1970, 0, 0)")
    rows = ([("A01", 201826)] * 40 + [("B02", 0)] * 25 + [("C03", 4329847)] * 20 + [("C03", 0)] * 15 + [("D04", 0)] * 3)
    for i, (code, cid) in enumerate(rows, 1):
        con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                    "condition_type_concept_id, condition_source_value) VALUES (?, 1, ?, DATE '2020-01-01', 32020, ?)", [i, cid, code])
    rep = mapping_coverage_report(con, {"condition_occurrence": ("condition_concept_id", "condition_source_value"),
                                        "procedure_occurrence": ("procedure_concept_id", "procedure_source_value")}, min_cell=None)
    s = rep["summary"].set_index("domain")
    c = s.loc["condition_occurrence"]
    assert (c.n_rows, c.n_rows_mapped, c.n_rows_unmapped) == (103, 60, 43)
    assert (c.n_source_codes, c.n_codes_fully_mapped, c.n_codes_partially_mapped, c.n_codes_unmapped) == (4, 1, 1, 2)
    assert c.pct_rows_mapped == pytest.approx(100 * 60 / 103)
    assert s.loc["procedure_occurrence", "status"] == "empty"
    un = rep["unmapped"]
    assert un.iloc[0].to_dict() == {"domain": "condition_occurrence", "source_value": "B02", "n_rows_unmapped": 25}
    assert set(un["source_value"]) == {"B02", "C03", "D04"}


def test_mapping_coverage_report_suppresses_small_cells():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1970, 0, 0)")
    rows = [("A01", 201826)] * 40 + [("D04", 0)] * 3 + [("E05", 0)] * 2
    for i, (code, cid) in enumerate(rows, 1):
        con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                    "condition_type_concept_id, condition_source_value) VALUES (?, 1, ?, DATE '2020-01-01', 32020, ?)", [i, cid, code])
    rep = mapping_coverage_report(con, {"condition_occurrence": ("condition_concept_id", "condition_source_value")})
    row = rep["summary"].iloc[0]
    assert row["suppressed"] and np.isnan(row["n_rows_unmapped"]) and np.isnan(row["pct_rows_mapped"])
    assert rep["unmapped"].empty                                       # D04 and E05 have fewer than 11 rows each and together 5


def test_mapping_coverage_rejects_bad_identifiers():
    con = duckdb.connect()
    with pytest.raises(ValueError):
        mapping_coverage_report(con, {"measurement; DROP TABLE person": ("a", "b")})


# ---- ODD-3: PCORnet vital -> OMOP measurement ------------------------------------------------------------------------------


def _vital_db(tmp_path, csv_text):
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) "
                "VALUES (1, 8507, 1990, 8527, 38003564, 'P1')")
    (tmp_path / "vital.csv").write_text(csv_text)
    return con


def test_load_vital_uses_standard_concept_ids_without_a_vocabulary(tmp_path):
    con = _vital_db(tmp_path, "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,HT,WT,ORIGINAL_BMI,SYSTOLIC,DIASTOLIC\n"
                              "V1,P1,E1,2024-01-01,10:00,70,180,25.8,120,80\n")
    summary = load_vital(con, str(tmp_path))
    got = dict(con.execute("SELECT measurement_source_value, measurement_concept_id FROM measurement").fetchall())
    assert got == {"8480-6": 3004249, "8462-4": 3012888, "8302-2": 3036277, "29463-7": 3025315, "39156-5": 3038553}
    assert summary["rows_loaded"] == {"HT": 1, "WT": 1, "BMI": 1, "SYSTOLIC": 1, "DIASTOLIC": 1, "BMI_DERIVED": 0}


def test_load_vital_prefers_the_vocabulary_mapping(tmp_path):
    con = _vital_db(tmp_path, "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,SYSTOLIC\nV1,P1,E1,2024-01-01,10:00,120\n")
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, "
                "valid_start_date, valid_end_date) VALUES (999001, 'Systolic', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8480-6', "
                "DATE '1970-01-01', DATE '2099-12-31')")
    load_vital(con, str(tmp_path))
    assert con.execute("SELECT measurement_concept_id FROM measurement").fetchone()[0] == 999001


def test_load_vital_derives_bmi_only_where_missing(tmp_path):
    con = _vital_db(tmp_path, "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,HT,WT,ORIGINAL_BMI\n"
                              "V1,P1,E1,2024-01-01,10:00,70,180,\n"          # derive: 703 * 180 / 4900 = 25.82
                              "V2,P1,E1,2024-01-02,10:00,70,180,30.0\n"       # reported BMI wins
                              "V3,P1,E1,2024-01-03,10:00,,180,\n")            # no height: nothing to derive
    load_vital(con, str(tmp_path))
    bmi = con.execute("SELECT measurement_date, value_as_number FROM measurement WHERE measurement_source_value = '39156-5' "
                      "ORDER BY measurement_date").fetchall()
    assert [round(v, 2) for _, v in bmi] == [25.82, 30.0]
    con2 = _vital_db(tmp_path, "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,HT,WT,ORIGINAL_BMI\nV1,P1,E1,2024-01-01,10:00,70,180,\n")
    load_vital(con2, str(tmp_path), derive_bmi=False)
    assert con2.execute("SELECT COUNT(*) FROM measurement WHERE measurement_source_value = '39156-5'").fetchone()[0] == 0


def test_load_vital_plausible_ranges_drop_and_count(tmp_path):
    csv = ("VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,SYSTOLIC,DIASTOLIC\n"
           "V1,P1,E1,2024-01-01,10:00,120,80\nV2,P1,E1,2024-01-02,10:00,1200,80\nV3,P1,E1,2024-01-03,10:00,0,-5\n")
    con = _vital_db(tmp_path, csv)
    summary = load_vital(con, str(tmp_path), plausible_ranges=True)
    assert con.execute("SELECT COUNT(*) FROM measurement WHERE measurement_concept_id = 3004249").fetchone()[0] == 1
    assert summary["rows_dropped_implausible"]["SYSTOLIC"] == 1
    con2 = _vital_db(tmp_path, csv)
    load_vital(con2, str(tmp_path))                                        # default: only non-positive values dropped
    assert con2.execute("SELECT COUNT(*) FROM measurement WHERE measurement_concept_id = 3004249").fetchone()[0] == 2


# ---- ODD-10: run manifest -----------------------------------------------------------------------------------------------


def test_run_manifest_records_vocabulary_and_output_hashes(tmp_path):
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO vocabulary (vocabulary_id, vocabulary_name, vocabulary_version, vocabulary_concept_id) VALUES "
                "('None', 'OMOP', 'v5.0 01-JAN-99', 44819096), ('LOINC', 'LOINC', 'LOINC 2.99', 44819102)")
    info = vocabulary_info(con)
    assert info["vocabulary_version"] == "v5.0 01-JAN-99" and info["vocabularies"] == {"LOINC": "LOINC 2.99"}
    assert vocabulary_info(duckdb.connect())["vocabulary_version"] is None            # no vocabulary table: no error
    inp, outp = tmp_path / "in.csv", tmp_path / "out.csv"
    inp.write_text("a\n1\n")
    outp.write_text("n\n42\n")
    m = RunManifest("test run", inputs=[inp], outputs=[outp], con=con)
    path = m.write(tmp_path / "run_manifest.json")
    data = json.loads(path.read_text())
    assert data["vocabulary"]["vocabulary_version"] == "v5.0 01-JAN-99"
    assert str(outp) in data["outputs"] and "scipy" in data["packages"]
    verify_manifest(path)
    outp.write_text("n\n43\n")
    with pytest.raises(ManifestVerificationError, match="out.csv"):
        verify_manifest(path)


def test_run_manifest_without_connection_is_unchanged(tmp_path):
    data = RunManifest("plain").to_dict()
    assert data["vocabulary"] is None and data["outputs"] == {}


# ---- ODD-2: BigQuery demo entry point --------------------------------------------------------------------------------------


def test_run_bigquery_demo_alias_validates_project():
    from omop_etl import run_bigquery_demo

    with pytest.raises(ValueError, match="billing_project"):
        run_bigquery_demo("REPLACE_WITH_YOUR_GCP_PROJECT_ID")
