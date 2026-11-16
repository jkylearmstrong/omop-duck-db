"""Bundled OHDSI concept sets: files, resolver (Circe semantics), LOINC expansion and the coverage check (v0.29.0)."""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import benchmark_features as bf  # noqa: E402
from omop_etl import concept_sets as cs  # noqa: E402
from omop_etl.data_gaps import GAP_CHECKS, DataGapWarning  # noqa: E402
from omop_etl.table1 import DEFAULT_LAB_LOINCS, DEFAULT_MEDICATION_CONCEPTS  # noqa: E402

# A tiny LOINC vocabulary: total bilirubin in serum/plasma (mass), in blood (mass), in serum/plasma (moles), in urine and
# in cord blood, plus the LOINC parts that link them.
PARTS = {  # id: (name, class)
    900: ("Bilirubin.total", "LOINC Component"), 901: ("Mass concentration", "LOINC Property"),
    902: ("Moles/volume", "LOINC Property"), 903: ("Qn", "LOINC Scale"), 904: ("Point in time (spot)", "LOINC Time"),
    910: ("Serum or Plasma", "LOINC System"), 911: ("Blood", "LOINC System"), 912: ("Urine", "LOINC System"),
    913: ("Cord blood", "LOINC System"),
}
TESTS = {  # id: (code, name, property, system)
    3024128: ("1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma", 901, 910),
    3006140: ("42719-5", "Bilirubin.total [Mass/volume] in Blood", 901, 911),
    3013721: ("14631-6", "Bilirubin.total [Moles/volume] in Serum or Plasma", 902, 910),
    3002000: ("5770-3", "Bilirubin.total [Mass/volume] in Urine", 901, 912),
    3002001: ("99999-1", "Bilirubin.total [Mass/volume] in Cord blood", 901, 913),
}


def vocab(con, parts: bool = True):
    rows = [(i, n, "LOINC", f"LP{i}", cls, None, None) for i, (n, cls) in PARTS.items()]
    rows += [(i, n, "LOINC", code, "Lab Test", "S", None) for i, (code, n, _, _) in TESTS.items()]
    rows += [(21600712, "A10 DRUGS USED IN DIABETES", "ATC", "A10", "ATC 2nd", "C", None),
             (1503297, "metformin", "RxNorm", "6809", "Ingredient", "S", None),
             (44000001, "metformin source code", "Local", "MET", "Drug", None, None)]
    con.execute("CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, vocabulary_id VARCHAR, concept_code VARCHAR, "
                "concept_class_id VARCHAR, standard_concept VARCHAR, invalid_reason VARCHAR)")
    con.executemany("INSERT INTO concept VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    rel = [(44000001, 1503297, "Maps to")]
    if parts:
        for i, (_, _, prop, system) in TESTS.items():
            rel += [(i, 900, "Has component"), (i, prop, "Has property"), (i, 903, "Has scale type"), (i, 904, "Has time aspect"),
                    (i, system, "Has system")]
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR)")
    con.executemany("INSERT INTO concept_relationship VALUES (?, ?, ?)", rel)
    con.execute("CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT, "
                "min_levels_of_separation INT, max_levels_of_separation INT)")
    con.executemany("INSERT INTO concept_ancestor VALUES (?, ?, ?, ?)", [(21600712, 21600712, 0, 0), (21600712, 1503297, 2, 2),
                                                                         (1503297, 1503297, 0, 0)])
    return con


def test_bundled_files_are_valid_and_match_the_constants():
    m = cs.concept_set_manifest()
    assert m["version"] == cs.CONCEPT_SET_VERSION
    root = cs.concept_set_dir()
    on_disk = sorted(f"{p.parent.name}/{p.stem}" for p in root.glob("*/*.json"))
    assert sorted(cs.list_concept_sets()) == on_disk, "every file is listed in manifest.json once, and every listed file exists"
    for full in on_disk:
        data = cs.load_concept_set(full)
        assert data["name"] == full.split("/")[1] and data["domain"] == full.split("/")[0]
        for it in data["expression"]["items"]:
            assert set(it) == {"concept", "includeDescendants", "includeMapped", "isExcluded"}
    assert bf.BENCHMARK_LAB_LOINCS == cs.codes_by_name("labs")
    assert list(bf.BENCHMARK_LAB_LOINCS) == m["domains"]["labs"]
    assert bf.EDERRI_MEDICATION_SPECS == cs.ancestor_specs("medications_ederri")
    assert set(DEFAULT_LAB_LOINCS) <= set(bf.BENCHMARK_LAB_LOINCS) | {"egfr"}
    assert all(DEFAULT_LAB_LOINCS[n] == bf.BENCHMARK_LAB_LOINCS[n] for n in DEFAULT_LAB_LOINCS if n != "egfr")
    assert DEFAULT_MEDICATION_CONCEPTS["metformin"] == [1503297, 21600745]
    assert "concept_set_coverage" in GAP_CHECKS


def test_an_atlas_expression_file_loads(tmp_path):
    p = tmp_path / "mine.json"
    p.write_text(json.dumps({"items": [{"concept": {"CONCEPT_ID": 1503297}, "includeDescendants": True}]}))
    data = cs.load_concept_set(p)
    assert data["name"] == "mine" and cs.concept_set_ids(data["expression"]) == [1503297]
    p.write_text(json.dumps({"items": [{"concept": {"CONCEPT_NAME": "no id, no code"}}]}))
    with pytest.raises(ValueError, match="CONCEPT_ID or VOCABULARY_ID"):
        cs.load_concept_set(p)


def test_resolver_follows_circe_semantics():
    con = vocab(duckdb.connect())
    atc = cs.concept_set_from_ids([21600712])
    assert cs.resolve_concept_set(con, atc) == [1503297, 21600712]
    no_desc = cs.concept_set_from_ids([21600712], include_descendants=False)
    assert cs.resolve_concept_set(con, no_desc) == [21600712]
    mapped = {"items": [{"concept": {"CONCEPT_ID": 1503297}, "includeDescendants": False, "includeMapped": True, "isExcluded": False}]}
    assert cs.resolve_concept_set(con, mapped) == [1503297, 44000001]
    minus = {"items": atc["items"] + [{"concept": {"CONCEPT_ID": 1503297}, "isExcluded": True}]}
    assert cs.resolve_concept_set(con, minus) == [21600712]
    by_code = cs.concept_set_from_codes(["1975-2", "0000-0"])
    with pytest.warns(DataGapWarning, match="not in this vocabulary.*0000-0"):
        assert cs.resolve_concept_set(con, by_code) == [3024128]


def test_resolver_without_concept_ancestor_says_so():
    con = duckdb.connect()
    con.execute("CREATE TABLE concept (concept_id BIGINT, vocabulary_id VARCHAR, concept_code VARCHAR)")
    with pytest.warns(DataGapWarning, match="concept_ancestor not reachable"):
        assert cs.resolve_concept_set(con, cs.concept_set_from_ids([21600712])) == [21600712]


def test_expansion_joins_blood_and_serum_but_not_other_units_or_specimens():
    con = vocab(duckdb.connect())
    out = cs.expand_loinc_concept_set(con, cs.concept_set_from_codes(["1975-2"]))
    codes = cs.concept_set_codes(out)
    assert codes == ["1975-2", "42719-5"]          # blood joins serum/plasma; molar, urine and cord blood do not
    first = out["items"][0]["concept"]
    assert first["CONCEPT_ID"] == 3024128 and first["CONCEPT_NAME"].startswith("Bilirubin.total")
    assert "1975-2" in bf.BENCHMARK_LAB_LOINCS["bilirubin_total"]
    assert cs.expand_loinc_concept_set(con, out) == out   # idempotent
    with pytest.raises(ValueError, match="no LOINC part relationships"):
        cs.expand_loinc_concept_set(vocab(duckdb.connect(), parts=False), out)


def measurements(con, codes_per_person):
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id BIGINT, "
                "measurement_date DATE, measurement_datetime TIMESTAMP, value_as_number DOUBLE)")
    by_code = {code: i for i, (code, *_) in TESTS.items()}
    rows, k = [], 0
    for pid, codes in codes_per_person.items():
        for code in codes:
            k += 1
            rows.append((k, pid, by_code[code], "2024-01-02", None, 1.0))
    con.executemany("INSERT INTO measurement VALUES (?, ?, ?, ?, ?, ?)", rows)
    return pd.DataFrame({"row_id": range(len(codes_per_person)), "subject_id": list(codes_per_person),
                         "cohort_start_date": pd.Timestamp("2024-01-01"), "cohort_end_date": pd.Timestamp("2024-01-05")})


def test_coverage_names_the_missed_codes_with_their_share():
    con = vocab(duckdb.connect())
    cohort = measurements(con, {1: ["1975-2"], 2: ["42719-5", "42719-5"], 3: ["14631-6"], 4: ["1975-2"]})
    sets = {"bilirubin_total": cs.concept_set_from_codes(["1975-2"])}
    miss = cs.loinc_coverage(con, sets, cohort=cohort)
    got = {r.concept_code: (round(r.share, 2), r.same_property, r.blood) for r in miss.itertuples()}
    assert got == {"42719-5": (0.4, True, True), "14631-6": (0.2, False, True)}
    assert cs.loinc_coverage(vocab(duckdb.connect(), parts=False), sets) is None


def test_benchmark_labs_fail_loudly_on_a_missed_code_and_use_the_expanded_set():
    con = vocab(duckdb.connect())
    cohort = measurements(con, {1: ["1975-2"], 2: ["42719-5"]})
    sets = {"bilirubin_total": cs.concept_set_from_codes(["1975-2"])}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bf._check_lab_coverage(con, sets, cohort, 0)
    gaps = [w.message for w in caught if isinstance(w.message, DataGapWarning)]
    assert len(gaps) == 1 and gaps[0].check == "concept_set_coverage" and gaps[0].column == "lab_bilirubin_total"
    assert "42719-5 50.0%" in str(gaps[0])

    expanded = {"bilirubin_total": cs.expand_loinc_concept_set(con, sets["bilirubin_total"])}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bf._check_lab_coverage(con, expanded, cohort, 0)
    assert not [w for w in caught if isinstance(w.message, DataGapWarning)]
    cmap = bf._concept_map(con, None, None, expanded)
    stats = bf._measurement_stats(con, cohort, cmap, 0)
    assert sorted(stats["row_id"]) == [0, 1]       # both stays now have a total bilirubin


def test_write_round_trip(tmp_path):
    expr = cs.concept_set_from_codes(["1975-2"])
    p = cs.write_concept_set(tmp_path / "labs" / "x.json", "x", "labs", expr)
    assert cs.load_concept_set(p)["expression"] == expr
    assert p.read_text().endswith("}\n")


def test_builder_script_reports_and_writes(tmp_path, monkeypatch, capsys):
    import importlib.util
    import shutil

    db = tmp_path / "vocab.duckdb"
    con = vocab(duckdb.connect(str(db)))
    con.close()
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("LAB_LOINC,RAW_NAME\n42719-5,TBILI BLOOD\n14631-6,TBILI UMOL\n")
    # the builder starts from the bundled files: pin bilirubin to its serum/plasma code for this test
    monkeypatch.setattr(cs, "load_concept_set", lambda n, _orig=cs.load_concept_set: (
        {**_orig(n), "expression": cs.concept_set_from_codes(["1975-2"])} if str(n) == "labs/bilirubin_total" else _orig(n)))
    spec = importlib.util.spec_from_file_location("build_concept_sets", REPO_ROOT / "scripts" / "build_concept_sets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--db", str(db), "--legacy", str(legacy), "--legacy-name-col", "RAW_NAME"]) == 0
    out = capsys.readouterr().out
    assert "labs/bilirubin_total: 1 -> 2 codes" in out and "+ 42719-5" in out
    assert "42719-5    bilirubin_total" in out and "14631-6    NOT IN ANY SET" in out
    # --write needs a version and leaves the repository's files alone here (a copy of the tree is written)
    fake = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "inst" / "extdata" / "concept_sets", fake / "inst" / "extdata" / "concept_sets")
    (fake / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "build_concept_sets.py", fake / "scripts")
    spec = importlib.util.spec_from_file_location("bcs2", fake / "scripts" / "build_concept_sets.py")
    mod2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod2)
    assert mod2.main(["--db", str(db), "--write"]) == 2
    assert mod2.main(["--db", str(db), "--write", "--version", "test.1"]) == 0
    written = json.loads((fake / "inst/extdata/concept_sets/labs/bilirubin_total.json").read_text())
    assert [it["concept"]["CONCEPT_CODE"] for it in written["expression"]["items"]] == ["1975-2", "42719-5"]
    assert json.loads((fake / "inst/extdata/concept_sets/manifest.json").read_text())["version"] == "test.1"


def test_coverage_script_prints_shares_only(tmp_path, monkeypatch, capsys):
    import importlib.util

    db = tmp_path / "site.duckdb"
    con = vocab(duckdb.connect(str(db)))
    cohort = measurements(con, {1: ["1975-2"], 2: ["42719-5"], 3: ["14631-6"]})
    con.close()
    cohort.rename(columns={"subject_id": "person_id"}).to_parquet(tmp_path / "cohort.parquet")
    spec = importlib.util.spec_from_file_location("ccsc", REPO_ROOT / "scripts" / "check_concept_set_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(cs, "load_concept_sets", lambda d: {"bilirubin_total": cs.concept_set_from_codes(["1975-2"])})
    assert mod.main(["--db", str(db), "--cohort", str(tmp_path / "cohort.parquet")]) == 0
    out = capsys.readouterr().out
    assert "42719-5" in out and "33.3%" in out
    assert mod.main(["--db", str(db), "--cohort", str(tmp_path / "cohort.parquet"), "--expand"]) == 0
    out = capsys.readouterr().out
    assert "42719-5" not in out and "14631-6" in out and "33.3%" in out   # share of all the analyte's rows


def test_other_unit_types_warn_but_do_not_fail():
    con = vocab(duckdb.connect())
    cohort = measurements(con, {1: ["1975-2"], 2: ["14631-6"]})        # molar bilirubin: another property
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bf._check_lab_coverage(con, {"bilirubin_total": cs.concept_set_from_codes(["1975-2"])}, cohort, 0)
    assert not [w for w in caught if isinstance(w.message, DataGapWarning)]
    assert any("another unit type" in str(w.message) and "14631-6 50.0%" in str(w.message) for w in caught)


def test_codes_are_looked_up_before_stored_ids():
    con = vocab(duckdb.connect())
    item = {"concept": {"CONCEPT_ID": 999, "CONCEPT_CODE": "1975-2", "VOCABULARY_ID": "LOINC"},
            "includeDescendants": False, "includeMapped": False, "isExcluded": False}
    assert cs.resolve_concept_set(con, {"items": [item]}) == [3024128]         # this vocabulary's id for 1975-2
    gone = {**item, "concept": {**item["concept"], "CONCEPT_CODE": "0000-0"}}
    assert cs.resolve_concept_set(con, {"items": [gone]}, warn=False) == []     # neither the code nor the id is here
    known = {**item, "concept": {**item["concept"], "CONCEPT_ID": 3006140, "CONCEPT_CODE": "0000-0"}}
    assert cs.resolve_concept_set(con, {"items": [known]}, warn=False) == [3006140]   # the stored id is in this vocabulary
    bare = duckdb.connect()                                                     # no concept table: ids are used as they are
    assert cs.resolve_concept_set(bare, {"items": [item]}, warn=False) == [999]


def test_whole_blood_sets_do_not_take_serum_or_plasma_codes():
    con = duckdb.connect()
    con.execute("CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, vocabulary_id VARCHAR, concept_code VARCHAR, "
                "concept_class_id VARCHAR, standard_concept VARCHAR, invalid_reason VARCHAR)")
    con.executemany("INSERT INTO concept VALUES (?, ?, 'LOINC', ?, ?, ?, NULL)", [
        (1, "Platelets", "LP1", "LOINC Component", None), (2, "Number concentration", "LP2", "LOINC Property", None),
        (3, "Qn", "LP3", "LOINC Scale", None), (4, "Point in time (spot)", "LP4", "LOINC Time", None),
        (5, "Blood", "LP5", "LOINC System", None), (6, "Plasma", "LP6", "LOINC System", None),
        (7, "Capillary blood", "LP7", "LOINC System", None),
        (10, "Platelets [#/volume] in Blood", "777-3", "Lab Test", "S"), (11, "Platelets [#/volume] in Plasma", "26516-5", "Lab Test", "S"),
        (12, "Platelets [#/volume] in Capillary blood", "74464-9", "Lab Test", "S")])
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR)")
    for t, system in ((10, 5), (11, 6), (12, 7)):
        con.executemany("INSERT INTO concept_relationship VALUES (?, ?, ?)", [
            (t, 1, "Has component"), (t, 2, "Has property"), (t, 3, "Has scale type"), (t, 4, "Has time aspect"), (t, system, "Has system")])
    out = cs.expand_loinc_concept_set(con, cs.concept_set_from_codes(["777-3"]))
    assert cs.concept_set_codes(out) == ["777-3", "74464-9"]


def test_expansion_modes():
    con = vocab(duckdb.connect())
    base = cs.concept_set_from_codes(["1975-2"])
    assert cs.concept_set_codes(cs.expand_loinc_concept_set(con, base, mode="none")) == ["1975-2"]
    assert cs.concept_set_codes(cs.expand_loinc_concept_set(con, base, mode="seed_systems")) == ["1975-2"]
    both = cs.concept_set_from_codes(["1975-2", "14631-6"])
    assert cs.concept_set_codes(cs.expand_loinc_concept_set(con, both, expand_from=["1975-2"])) == ["1975-2", "14631-6", "42719-5"]
    with pytest.raises(ValueError, match="expansion mode"):
        cs.expand_loinc_concept_set(con, base, mode="all")
    assert cs.load_concept_set("labs/po2")["expansion"] == "seed_systems"
    assert cs.load_concept_set("labs/hemoglobin")["expand_from"] == ["718-7"]
    assert "1558-6" in bf.BENCHMARK_LAB_LOINCS["glucose"]
    assert set(bf.BENCHMARK_LAB_LOINCS) <= set(bf.DEFAULT_BOUNDS)


def test_bundled_lab_sets_hold_no_red_cell_urine_or_cord_codes():
    assert not cs._blood("Red Blood Cells") and not cs._blood("Urine and Serum or Plasma") and not cs._blood("Cord blood")
    for name in cs.concept_set_manifest()["domains"]["labs"]:
        for it in cs.load_concept_set(f"labs/{name}")["expression"]["items"]:
            label = it["concept"].get("CONCEPT_NAME") or ""
            assert not re.search(r"red blood cell|urine|cord blood", label, re.I), (name, label)
