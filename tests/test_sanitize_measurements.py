"""Tests for sanitize_measurements() (RFC 6.1).

The shared fixture ``tests/fixtures/sanitize_measurements.csv`` holds 69 hand-built measurement rows (68
with a value) that target one failure mode each: systolic BP of 0, 999, -5 and exactly on both bounds,
NaN / +Inf / -Inf, a NULL value, a creatinine in mg/dL and umol/L next to NULL, 0 and unrelated units, a
concept with no limit row, groups of 2 rows (too small), zero IQR, zero variance and an unmapped concept 0.
``tests/fixtures/sanitize_expected.csv`` is the golden result for the three methods
(``winsorize_iqr`` with iqr_multiplier = 1.5, ``z_score_cutoff`` with z_threshold = 2.0). It was derived with
an independent pure-Python/numpy model and the headline rows were re-derived by hand, e.g. for potassium
(sorted 0.5, 4.0, 4.0, 4.1, 4.2, 4.2, 4.3, 4.4, 4.5, 15): Q1 = 4.025, Q3 = 4.375, IQR = 0.35, fences 3.5 and
4.9. The R tests (tests/testthat/test-sanitize-measurements.R) assert the same golden file.
"""

import csv
import hashlib
import math
import os
import subprocess
import sys
import types
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from omop_etl import build_schema
from omop_etl.dqd import _sanitize_bundled_limits, sanitize_measurements

FIX = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent
LIMITS_CSV = REPO_ROOT / "inst" / "extdata" / "physiologic_limits.csv"

METHODS = ["dqd_biologic_limits", "winsorize_iqr", "z_score_cutoff"]
ACTIONS = ["nullify", "clamp", "drop_row"]
PARAMS = {"dqd_biologic_limits": {}, "winsorize_iqr": {"iqr_multiplier": 1.5}, "z_score_cutoff": {"z_threshold": 2.0}}
FLAGGED = {"below_min", "above_max", "invalid_number"}
CORE_COLUMNS = ["measurement_id", "person_id", "measurement_concept_id", "measurement_date", "unit_concept_id",
                "value_as_number", "value_as_number_raw", "sanitize_status"]
REPORT_COLUMNS = ["measurement_concept_id", "unit_concept_id", "n", "n_ok", "n_below", "n_above", "n_invalid",
                  "n_changed", "n_dropped", "n_no_limit", "n_unit_skipped", "n_insufficient_data"]

SBP, CREAT, POTASSIUM, X_CONCEPT, ZI, Z0, SMALL = 3004249, 3016723, 3023103, 2000000001, 2000000002, 2000000003, 2000000004


# ---------------------------------------------------------------------------------- fixture helpers
def _num(text):
    return None if text == "" else float(text)


def _fixture_rows():
    rows = []
    with open(FIX / "sanitize_measurements.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "measurement_id": int(r["measurement_id"]), "person_id": int(r["person_id"]),
                "concept": int(r["measurement_concept_id"]),
                "unit": int(r["unit_concept_id"]) if r["unit_concept_id"] != "" else None,
                "value": _num(r["value_as_number"]),
            })
    return rows


def _golden():
    out = {}
    with open(FIX / "sanitize_expected.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            out.setdefault(r["method"], {})[int(r["measurement_id"])] = (
                r["sanitize_status"], _num(r["value_nullify"]), _num(r["value_clamp"]))
    return out


ROWS = _fixture_rows()
RAW = {r["measurement_id"]: r["value"] for r in ROWS}
BY_ID = {r["measurement_id"]: r for r in ROWS}
GOLDEN = _golden()


def _load_fixture(con):
    """Real CDM schema + the CSV fixture + a small cohort table (persons 1 and 2, person 1 twice, and 99)."""
    build_schema(con)
    path = (FIX / "sanitize_measurements.csv").as_posix().replace("'", "''")
    con.execute(f"""
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,
                                 measurement_type_concept_id, value_as_number, unit_concept_id, visit_occurrence_id)
        SELECT measurement_id, person_id, measurement_concept_id, measurement_date, 32817,
               TRY_CAST(value_as_number AS DOUBLE), unit_concept_id, measurement_id * 10
        FROM read_csv('{path}', header = true, columns = {{
            'measurement_id': 'INTEGER', 'person_id': 'INTEGER', 'measurement_concept_id': 'INTEGER',
            'measurement_date': 'DATE', 'unit_concept_id': 'INTEGER', 'value_as_number': 'VARCHAR'}})""")
    con.execute("""CREATE TABLE sanitize_cohort AS SELECT * FROM (VALUES
        (1, DATE '2021-01-01'), (2, DATE '2021-01-01'), (1, DATE '2021-02-01'), (99, DATE '2021-01-01'))
        AS t(subject_id, cohort_start_date)""")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("sanitize") / "cdm.duckdb"
    con = duckdb.connect(str(path))
    _load_fixture(con)
    con.close()
    return path


@pytest.fixture(scope="module")
def ro(db_path):
    """Read-only connection: any write would raise, so every test using it also proves read-only safety."""
    con = duckdb.connect(str(db_path), read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="module")
def rw():
    """Writable in-memory copy for tests that create their own objects (use unique names)."""
    con = duckdb.connect(":memory:")
    _load_fixture(con)
    yield con
    con.close()


def _measurement_table(rows, extra_cols=True):
    """A fresh in-memory connection with a measurement table holding ``rows``.

    rows: (measurement_id, person_id, concept, unit, value) tuples; value None = NULL.
    """
    con = duckdb.connect(":memory:")
    cols = ("measurement_id INTEGER, person_id INTEGER, measurement_concept_id INTEGER, measurement_date DATE, "
            "unit_concept_id INTEGER, value_as_number DOUBLE")
    if extra_cols:
        cols += ", visit_occurrence_id INTEGER, measurement_datetime TIMESTAMP"
    con.execute(f"CREATE TABLE measurement ({cols})")
    for mid, pid, cid, uid, val in rows:
        v = "NULL" if val is None else ("'NaN'::DOUBLE" if (isinstance(val, float) and math.isnan(val))
                                        else "'Infinity'::DOUBLE" if val == math.inf
                                        else "'-Infinity'::DOUBLE" if val == -math.inf else repr(float(val)))
        u = "NULL" if uid is None else uid
        extra = ", NULL, NULL" if extra_cols else ""
        con.execute(f"INSERT INTO measurement VALUES ({mid}, {pid}, {cid}, DATE '2021-01-01', {u}, {v}{extra})")
    return con


def _same(got, want):
    """want None = NULL; NaN and +/-Inf compare as themselves; otherwise a tight float tolerance."""
    if want is None:
        assert got is None or pd.isna(got), f"expected NULL, got {got!r}"
    elif math.isnan(want):
        assert got is not None and math.isnan(got), f"expected NaN, got {got!r}"
    elif math.isinf(want):
        assert got == want, f"expected {want}, got {got!r}"
    else:
        assert pd.notna(got) and got == pytest.approx(want, rel=1e-9, abs=1e-9), f"expected {want}, got {got!r}"


# ---------------------------------------------------------------------------------- reference model
def reference(rows, method, limits=None, k_iqr=3.0, z_thr=4.0):
    """Independent (pure Python / numpy) statement of the rules -> {measurement_id: (status, lo, hi)}.

    rows: dicts with measurement_id, concept, unit (None allowed) and value (float; None = out of scope).
    method: 'dqd' | 'iqr' | 'z'. limits: [(concept, unit | None, min | None, max | None)].
    """
    rows = [r for r in rows if r["value"] is not None]
    out = {}
    if method == "dqd":
        by_unit = {(c, u): (lo, hi) for c, u, lo, hi in limits if u is not None}
        any_unit = {c: (lo, hi) for c, u, lo, hi in limits if u is None}
        concepts = {c for c, *_ in limits}
        for r in rows:
            v, c, u = r["value"], r["concept"], r["unit"]
            b = by_unit.get((c, u)) if u is not None else None
            if b is None:
                b = any_unit.get(c)
            lo, hi = b if b else (None, None)
            if not math.isfinite(v):
                st = "invalid_number"
            elif b is None:
                st = "unit_skipped" if c in concepts else "no_limit"
            elif lo is not None and v < lo:
                st = "below_min"
            elif hi is not None and v > hi:
                st = "above_max"
            else:
                st = "ok"
            out[r["measurement_id"]] = (st, lo, hi)
        return out
    groups = {}
    for r in rows:
        if math.isfinite(r["value"]) and r["concept"] != 0:
            groups.setdefault((r["concept"], r["unit"]), []).append(r["value"])
    fences = {}
    for g, vals in groups.items():
        a = np.array(vals, dtype=float)
        if len(a) < 3:
            continue
        if method == "iqr":
            q1, q3 = np.percentile(a, [25, 75])
            if not q3 - q1 > 0:
                continue
            fences[g] = (q1 - k_iqr * (q3 - q1), q3 + k_iqr * (q3 - q1))
        else:
            sd = a.std(ddof=1)
            if not sd > 0:
                continue
            fences[g] = (a.mean() - z_thr * sd, a.mean() + z_thr * sd)
    for r in rows:
        v, c = r["value"], r["concept"]
        lo, hi = fences.get((c, r["unit"]), (None, None))
        if not math.isfinite(v):
            st = "invalid_number"
        elif c == 0:
            st = "no_limit"
        elif lo is None:
            st = "insufficient_data"
        elif v < lo:
            st = "below_min"
        elif v > hi:
            st = "above_max"
        else:
            st = "ok"
        out[r["measurement_id"]] = (st, lo, hi)
    return out


def reference_clean(v, st, lo, hi, action):
    if st in ("below_min", "above_max"):
        return (lo if st == "below_min" else hi) if action == "clamp" else None
    if st == "invalid_number":
        if action == "clamp" and math.isinf(v):
            return hi if v > 0 else lo
        return None
    return v


def bundled_limits():
    return [(c, u, lo, hi) for c, u, lo, hi in _sanitize_bundled_limits()]


def golden_rows():
    """The golden table rebuilt from the reference model (it never calls sanitize_measurements()).

    ``tests/fixtures/sanitize_make_golden.py`` writes exactly these rows to ``sanitize_expected.csv``.
    """
    out = []
    for method, key in (("dqd_biologic_limits", "dqd"), ("winsorize_iqr", "iqr"), ("z_score_cutoff", "z")):
        ref = reference(ROWS, key, limits=bundled_limits(), k_iqr=1.5, z_thr=2.0)
        for i in sorted(ref):
            st, lo, hi = ref[i]
            out.append((method, i, st, reference_clean(RAW[i], st, lo, hi, "nullify"),
                        reference_clean(RAW[i], st, lo, hi, "clamp")))
    return out


def test_golden_file_matches_the_independent_reference_model():
    """Guards against a stale golden file, e.g. after the bundled limits table changes (then re-run
    ``python tests/fixtures/sanitize_make_golden.py`` and review the diff)."""
    regenerated = golden_rows()
    assert len(regenerated) == sum(len(v) for v in GOLDEN.values()) == 3 * 68
    for method, i, st, nullified, clamped in regenerated:
        g_status, g_nullify, g_clamp = GOLDEN[method][i]
        assert g_status == st, (method, i)
        _same(g_nullify, nullified)
        _same(g_clamp, clamped)


# ---------------------------------------------------------------------------------- golden matrix
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("action", ACTIONS)
def test_golden_matrix(ro, method, action):
    exp = GOLDEN[method]
    df = sanitize_measurements(ro, method=method, action=action, **PARAMS[method])

    kept = set(exp) - {i for i, (st, _, _) in exp.items() if st in FLAGGED} if action == "drop_row" else set(exp)
    assert list(df["measurement_id"]) == sorted(kept), "wrong rows / not ordered by measurement_id"
    for r in df.itertuples():
        st, nullified, clamped = exp[r.measurement_id]
        assert r.sanitize_status == st, f"id {r.measurement_id}"
        want = {"nullify": nullified, "clamp": clamped, "drop_row": RAW[r.measurement_id]}[action]
        _same(r.value_as_number, want)
        _same(r.value_as_number_raw, RAW[r.measurement_id])
        assert r.person_id == BY_ID[r.measurement_id]["person_id"]
        assert r.measurement_concept_id == BY_ID[r.measurement_id]["concept"]
        unit = BY_ID[r.measurement_id]["unit"]
        assert (pd.isna(r.unit_concept_id) if unit is None else r.unit_concept_id == unit)
        assert r.visit_occurrence_id == r.measurement_id * 10


def test_output_shape_and_columns(ro):
    df = sanitize_measurements(ro)
    assert list(df.columns) == CORE_COLUMNS + ["measurement_datetime", "visit_occurrence_id"]
    # the NULL-valued row (id 10) is out of scope; everything else is returned
    assert 10 not in set(df["measurement_id"]) and len(df) == 68
    assert str(df["measurement_date"].dtype).startswith("datetime64")
    rep = df.attrs["sanitization_report"]
    assert list(rep.columns) == REPORT_COLUMNS


# ---------------------------------------------------------------------------------- report
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("action", ACTIONS)
def test_report_reconciles_with_rows_and_golden(ro, method, action):
    df = sanitize_measurements(ro, method=method, action=action, **PARAMS[method])
    rep = df.attrs["sanitization_report"]
    assert list(rep.columns) == REPORT_COLUMNS

    status_cols = ["n_ok", "n_below", "n_above", "n_invalid", "n_no_limit", "n_unit_skipped", "n_insufficient_data"]
    assert (rep["n"] == rep[status_cols].sum(axis=1)).all()
    assert (rep["n_changed"] + rep["n_dropped"] == rep["n_below"] + rep["n_above"] + rep["n_invalid"]).all()
    if action == "drop_row":
        assert (rep["n_changed"] == 0).all()
        assert rep["n_dropped"].sum() == 68 - len(df)
    else:
        assert (rep["n_dropped"] == 0).all()
        assert len(df) == 68
    assert rep["n"].sum() == 68, "the report counts every in-scope row, including dropped ones"
    if method != "dqd_biologic_limits":
        assert (rep["n_unit_skipped"] == 0).all()
    else:
        assert (rep["n_insufficient_data"] == 0).all()

    # independent tally straight from the golden statuses
    want = {}
    for i, (st, _, _) in GOLDEN[method].items():
        r = BY_ID[i]
        key = (r["concept"], r["unit"])
        want.setdefault(key, {}).setdefault(st, 0)
        want[key][st] += 1
    got = {(int(r.measurement_concept_id), None if pd.isna(r.unit_concept_id) else int(r.unit_concept_id)): r
           for r in rep.itertuples()}
    assert set(got) == set(want)
    for key, counts in want.items():
        r = got[key]
        assert r.n == sum(counts.values())
        assert (r.n_ok, r.n_below, r.n_above, r.n_invalid) == (
            counts.get("ok", 0), counts.get("below_min", 0), counts.get("above_max", 0),
            counts.get("invalid_number", 0))
        assert (r.n_no_limit, r.n_unit_skipped, r.n_insufficient_data) == (
            counts.get("no_limit", 0), counts.get("unit_skipped", 0), counts.get("insufficient_data", 0))
    # report rows are ordered by concept, then unit with NULL last
    keys = [(int(r.measurement_concept_id), math.inf if pd.isna(r.unit_concept_id) else r.unit_concept_id)
            for r in rep.itertuples()]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------------- dqd_biologic_limits
def test_bounds_are_inclusive(ro):
    df = sanitize_measurements(ro).set_index("measurement_id")
    # SBP 40 and 300 sit exactly on the bundled bounds; creatinine mg/dL 0.1 and 30 as well
    for i in (4, 5, 16, 17):
        assert df.loc[i, "sanitize_status"] == "ok" and df.loc[i, "value_as_number"] == RAW[i]
    # one unit beyond is flagged
    assert df.loc[12, "sanitize_status"] == "below_min" and df.loc[13, "sanitize_status"] == "above_max"
    # a limits row sitting on a value keeps it too
    lim = pd.DataFrame({"concept_id": [SBP], "min_value": [120.0], "max_value": [125.0]})
    own = sanitize_measurements(ro, limits=lim, measurement_concept_ids=[SBP]).set_index("measurement_id")
    assert own.loc[1, "sanitize_status"] == "ok" and own.loc[14, "sanitize_status"] == "ok"


def test_unit_awareness_never_misapplies_bounds(ro):
    df = sanitize_measurements(ro, action="nullify").set_index("measurement_id")
    # umol/L creatinine is judged against the umol/L row, not the mg/dL one (88 and 100 would be far
    # above 30 mg/dL)
    assert df.loc[20, "value_as_number"] == 88 and df.loc[20, "sanitize_status"] == "ok"
    assert df.loc[23, "value_as_number"] == 100 and df.loc[23, "sanitize_status"] == "ok"
    assert df.loc[22, "sanitize_status"] == "above_max"          # 3000 umol/L > 2650
    # NULL unit, unit 0 and an unlisted unit: untouched and counted as unit_skipped
    for i in (24, 25, 26, 62):
        assert df.loc[i, "sanitize_status"] == "unit_skipped"
        assert df.loc[i, "value_as_number"] == df.loc[i, "value_as_number_raw"]
    # no limit row for the concept at all
    assert (df.loc[37:49, "sanitize_status"] == "no_limit").all()
    rep = sanitize_measurements(ro).attrs["sanitization_report"]
    creat = rep[rep["measurement_concept_id"] == CREAT]
    assert creat["n_unit_skipped"].sum() == 3 and creat["n_no_limit"].sum() == 0
    # 13 rows of X have a finite value (id 65 is +Inf, i.e. invalid_number, not no_limit)
    assert rep[rep["measurement_concept_id"] == X_CONCEPT]["n_no_limit"].sum() == 13


def test_concept_zero_is_never_pooled_by_statistical_methods(ro):
    for method in ("winsorize_iqr", "z_score_cutoff"):
        df = sanitize_measurements(ro, method=method, action="drop_row", **PARAMS[method])
        zero = df[df["measurement_concept_id"] == 0]
        assert list(zero["sanitize_status"]) == ["no_limit"] * 4
        assert list(zero["value_as_number"]) == [12345, 1, 2, -1000]


# ---------------------------------------------------------------------------------- override limits
def _run_with_limits(con, limits, **kw):
    return sanitize_measurements(con, limits=limits, **kw).set_index("measurement_id")


def test_limits_without_unit_replace_the_whole_concept(ro):
    lim = pd.DataFrame({"concept_id": [SBP], "min_value": [100], "max_value": [200]})
    df = _run_with_limits(ro, lim, action="clamp")
    expected = {1: ("ok", 120), 2: ("below_min", 100), 3: ("above_max", 200), 4: ("below_min", 100),
                5: ("above_max", 200), 6: ("below_min", 100), 11: ("ok", 135), 12: ("below_min", 100),
                13: ("above_max", 200), 14: ("ok", 125)}
    for i, (st, val) in expected.items():
        assert df.loc[i, "sanitize_status"] == st and df.loc[i, "value_as_number"] == val
    # nothing else changed relative to the bundled table
    base = sanitize_measurements(ro, action="clamp").set_index("measurement_id")
    other = [i for i in base.index if BY_ID[i]["concept"] != SBP]
    assert (df.loc[other, "sanitize_status"] == base.loc[other, "sanitize_status"]).all()


def test_limits_with_unit_replace_only_that_unit(ro):
    lim = pd.DataFrame({"concept_id": [CREAT], "unit_concept_id": [8749], "min_value": [90], "max_value": [2000]})
    df = _run_with_limits(ro, lim, action="nullify")
    assert df.loc[20, "sanitize_status"] == "below_min"       # 88 < 90
    assert df.loc[23, "sanitize_status"] == "ok"              # 100
    assert df.loc[21, "sanitize_status"] == "above_max"       # 2650 > 2000
    assert df.loc[18, "sanitize_status"] == "above_max"       # mg/dL row still the bundled 0.1-30
    assert df.loc[19, "sanitize_status"] == "below_min"


def test_limits_for_a_new_concept_and_open_bounds(ro):
    # new concept with a unit: other units of that concept become unit_skipped
    lim = pd.DataFrame({"concept_id": [X_CONCEPT], "unit_concept_id": [8840], "min_value": [0], "max_value": [20]})
    df = _run_with_limits(ro, lim)
    assert df.loc[37, "sanitize_status"] == "below_min" and df.loc[47, "sanitize_status"] == "above_max"
    assert df.loc[38, "sanitize_status"] == "ok"
    assert df.loc[48, "sanitize_status"] == "unit_skipped" and df.loc[49, "sanitize_status"] == "unit_skipped"
    # a missing min leaves the lower side open (NaN and None both mean missing); any unit applies
    lim = pd.DataFrame({"concept_id": [X_CONCEPT], "min_value": [np.nan], "max_value": [20]})
    df = _run_with_limits(ro, lim)
    assert df.loc[37, "sanitize_status"] == "ok" and df.loc[47, "sanitize_status"] == "above_max"
    assert df.loc[48, "sanitize_status"] == "ok" and df.loc[49, "sanitize_status"] == "above_max"
    lim = pd.DataFrame({"concept_id": [X_CONCEPT], "min_value": [0], "max_value": [None]})
    df = _run_with_limits(ro, lim, action="clamp")
    assert df.loc[37, "value_as_number"] == 0 and df.loc[47, "value_as_number"] == 100
    # clamp of +Inf with an open upper bound falls back to NULL
    assert pd.isna(df.loc[65, "value_as_number"])


def test_unit_specific_row_wins_over_any_unit_row(ro):
    lim = pd.DataFrame({"concept_id": [X_CONCEPT, X_CONCEPT], "unit_concept_id": [None, 8840],
                        "min_value": [0, 11], "max_value": [1000, 17]})
    df = _run_with_limits(ro, lim)
    assert df.loc[38, "sanitize_status"] == "below_min"       # 10 < 11 (unit-specific row)
    assert df.loc[46, "sanitize_status"] == "above_max"       # 18 > 17
    assert df.loc[49, "sanitize_status"] == "ok"              # 8749 falls back to the any-unit row


def test_limits_columns_are_case_insensitive_and_extras_ignored(ro):
    lim = pd.DataFrame({"Concept_ID": [SBP], "MIN_VALUE": [100], "Max_Value": [200], "name": ["SBP"], "x": [1]})
    df = _run_with_limits(ro, lim)
    assert df.loc[2, "sanitize_status"] == "below_min"
    # also accepts dict-of-lists
    df2 = _run_with_limits(ro, {"concept_id": [SBP], "min_value": [100], "max_value": [200]})
    assert df2.loc[2, "sanitize_status"] == "below_min"


def test_limits_ignored_with_warning_for_statistical_methods(ro):
    lim = pd.DataFrame({"concept_id": [SBP], "min_value": [100], "max_value": [200]})
    with pytest.warns(UserWarning, match="limits is only used"):
        df = sanitize_measurements(ro, method="winsorize_iqr", limits=lim, iqr_multiplier=1.5)
    assert df.set_index("measurement_id").loc[2, "sanitize_status"] == "ok"


def test_overrides_do_not_leak_between_calls(ro):
    sanitize_measurements(ro, limits=pd.DataFrame({"concept_id": [SBP], "min_value": [100], "max_value": [200]}))
    df = sanitize_measurements(ro).set_index("measurement_id")
    assert df.loc[2, "sanitize_status"] == "below_min" and df.loc[1, "sanitize_status"] == "ok"
    assert df.loc[12, "sanitize_status"] == "below_min"     # 39 < 40 (bundled bound, not 100)
    assert df.loc[4, "sanitize_status"] == "ok"


# ---------------------------------------------------------------------------------- scope
def test_cohort_restriction_row_wise_method(ro):
    df = sanitize_measurements(ro, cohort_table="sanitize_cohort", action="nullify")
    want = sorted(r["measurement_id"] for r in ROWS if r["person_id"] in (1, 2) and r["value"] is not None)
    assert list(df["measurement_id"]) == want, "persons 1 and 2 only, each measurement once (person 1 listed twice)"
    assert set(df["person_id"]) == {1, 2}
    for r in df.itertuples():
        assert r.sanitize_status == GOLDEN["dqd_biologic_limits"][r.measurement_id][0]
    assert df.attrs["sanitization_report"]["n"].sum() == len(want)


def test_cohort_restriction_with_drop_row_and_report(ro):
    df = sanitize_measurements(ro, cohort_table="sanitize_cohort", action="drop_row")
    scope = [r for r in ROWS if r["person_id"] in (1, 2) and r["value"] is not None]
    flagged = {i for i, (st, _, _) in GOLDEN["dqd_biologic_limits"].items() if st in FLAGGED}
    kept = sorted(r["measurement_id"] for r in scope if r["measurement_id"] not in flagged)
    assert list(df["measurement_id"]) == kept
    rep = df.attrs["sanitization_report"]
    assert rep["n"].sum() == len(scope)
    assert rep["n_dropped"].sum() == len(scope) - len(kept) > 0
    assert (rep["n_changed"] == 0).all()


def test_method_and_action_are_trimmed_and_case_insensitive(ro):
    want = sanitize_measurements(ro, method="winsorize_iqr", action="clamp")
    got = sanitize_measurements(ro, method="  Winsorize_IQR ", action="CLAMP")
    pd.testing.assert_frame_equal(got, want)


def test_cohort_restriction_scopes_the_statistics_too(ro):
    rows = [r for r in ROWS if r["person_id"] in (1, 2)]
    for method, key in (("winsorize_iqr", "iqr"), ("z_score_cutoff", "z")):
        df = sanitize_measurements(ro, cohort_table="sanitize_cohort", method=method, action="clamp", **PARAMS[method])
        ref = reference(rows, key, limits=bundled_limits(), k_iqr=1.5, z_thr=2.0)
        assert list(df["measurement_id"]) == sorted(ref)
        for r in df.itertuples():
            st, lo, hi = ref[r.measurement_id]
            assert r.sanitize_status == st, (method, r.measurement_id)
            _same(r.value_as_number, reference_clean(RAW[r.measurement_id], st, lo, hi, "clamp"))
    # restricting really changes the answer for the statistical methods (otherwise this test proves nothing)
    full = sanitize_measurements(ro, method="winsorize_iqr", iqr_multiplier=1.5).set_index("measurement_id")
    sub = sanitize_measurements(ro, cohort_table="sanitize_cohort", method="winsorize_iqr",
                                iqr_multiplier=1.5).set_index("measurement_id")
    assert (full.loc[sub.index, "sanitize_status"] != sub["sanitize_status"]).any()


def test_cohort_person_column_is_validated_with_no_fallback(rw):
    rw.execute("CREATE TABLE sanitize_cohort_pid AS SELECT person_id FROM (VALUES (1), (3)) t(person_id)")
    with pytest.raises(ValueError, match=r"person_col 'subject_id' not found.*Available columns: \['person_id'\]"):
        sanitize_measurements(rw, cohort_table="sanitize_cohort_pid")
    df = sanitize_measurements(rw, cohort_table="sanitize_cohort_pid", person_col="PERSON_ID")  # case-insensitive
    assert set(df["person_id"]) == {1, 3}
    # schema-qualified names work too
    df = sanitize_measurements(rw, cohort_table="main.sanitize_cohort")
    assert set(df["person_id"]) == {1, 2}


def test_concept_restriction(ro):
    df = sanitize_measurements(ro, measurement_concept_ids=[SBP, POTASSIUM, SBP])
    assert set(df["measurement_concept_id"]) == {SBP, POTASSIUM}
    assert len(df) == 13 + 10
    scalar = sanitize_measurements(ro, measurement_concept_ids=POTASSIUM)
    assert len(scalar) == 10
    both = sanitize_measurements(ro, cohort_table="sanitize_cohort", measurement_concept_ids=[SBP])
    assert set(both["person_id"]) == {1, 2} and set(both["measurement_concept_id"]) == {SBP}
    # concept ids that are absent simply match nothing
    none = sanitize_measurements(ro, measurement_concept_ids=[123456789])
    assert len(none) == 0 and list(none.columns) == CORE_COLUMNS + ["measurement_datetime", "visit_occurrence_id"]
    rep = none.attrs["sanitization_report"]
    assert len(rep) == 0 and list(rep.columns) == REPORT_COLUMNS


# ---------------------------------------------------------------------------------- statistical edge cases
def test_group_size_threshold_is_three():
    rows = [(1, 1, 7, 8840, 1.0), (2, 1, 7, 8840, 2.0), (3, 1, 7, 8840, 100.0),          # exactly 3 -> screened
            (4, 1, 8, 8840, 1.0), (5, 1, 8, 8840, 1000.0)]                              # 2 -> insufficient
    con = _measurement_table(rows)
    for method in ("winsorize_iqr", "z_score_cutoff"):
        df = sanitize_measurements(con, method=method).set_index("measurement_id")
        assert list(df.loc[[1, 2, 3], "sanitize_status"]) == ["ok"] * 3
        assert list(df.loc[[4, 5], "sanitize_status"]) == ["insufficient_data"] * 2
        assert list(df["value_as_number"]) == [1, 2, 100, 1, 1000]
    # the three values do get fences: with k tiny the 100 is above the upper fence
    df = sanitize_measurements(con, method="winsorize_iqr", iqr_multiplier=0.01).set_index("measurement_id")
    assert df.loc[3, "sanitize_status"] == "above_max"


def test_zero_iqr_and_zero_variance_are_insufficient_data(ro):
    iqr = sanitize_measurements(ro, method="winsorize_iqr", iqr_multiplier=1.5).set_index("measurement_id")
    assert set(iqr.loc[50:54, "sanitize_status"]) == {"insufficient_data"}       # 7, 7, 7, 7, 9: IQR is 0
    z = sanitize_measurements(ro, method="z_score_cutoff", z_threshold=2.0).set_index("measurement_id")
    assert set(z.loc[50:54, "sanitize_status"]) == {"ok"}                        # sd > 0, 9 has z = 1.79
    for df in (iqr, z):
        assert set(df.loc[55:58, "sanitize_status"]) == {"insufficient_data"}    # 5, 5, 5, 5
        assert df.loc[59, "sanitize_status"] == "invalid_number"                 # NaN in a degenerate group
        assert list(df.loc[55:58, "value_as_number"]) == [5, 5, 5, 5]
    # a tighter z threshold does flag the 9 (z = 1.79 > 1.5)
    tight = sanitize_measurements(ro, method="z_score_cutoff", z_threshold=1.5).set_index("measurement_id")
    assert tight.loc[54, "sanitize_status"] == "above_max"


def test_default_thresholds_and_methods_disagree_as_designed(ro):
    # default k = 3 / z = 4: the 999 SBP is far outside the IQR fences but |z| < 4 with 10 values
    iqr = sanitize_measurements(ro, method="winsorize_iqr").set_index("measurement_id")
    z = sanitize_measurements(ro, method="z_score_cutoff").set_index("measurement_id")
    assert iqr.loc[3, "sanitize_status"] == "above_max"
    assert z.loc[3, "sanitize_status"] == "ok"
    assert (z["sanitize_status"].isin(["above_max", "below_min"])).sum() == 0


def test_random_data_matches_the_reference_model():
    rng = np.random.default_rng(11)
    rows = []
    for i in range(1, 601):
        concept = int(rng.choice([SBP, SBP, X_CONCEPT, 0]))
        unit = [8876, None, 0][int(rng.integers(0, 3))] if concept != SBP else 8876
        roll = rng.random()
        if roll < 0.02:
            val = float("nan")
        elif roll < 0.04:
            val = float(rng.choice([math.inf, -math.inf]))
        elif roll < 0.06:
            val = None
        elif roll < 0.12:
            val = float(rng.choice([-20.0, 0.0, 350.0, 999.0, 5000.0]))
        else:
            val = float(np.round(rng.normal(120, 18), 3))
        rows.append((i, i % 7 + 1, concept, unit, val))
    con = _measurement_table(rows)
    model_rows = [{"measurement_id": i, "concept": c, "unit": u, "value": v} for i, _, c, u, v in rows]
    for method, key in (("dqd_biologic_limits", "dqd"), ("winsorize_iqr", "iqr"), ("z_score_cutoff", "z")):
        for action in ("nullify", "clamp"):
            kw = {"iqr_multiplier": 1.5, "z_threshold": 2.0}
            df = sanitize_measurements(con, method=method, action=action, **kw)
            ref = reference(model_rows, key, limits=bundled_limits(), k_iqr=1.5, z_thr=2.0)
            assert list(df["measurement_id"]) == sorted(ref)
            for r in df.itertuples():
                st, lo, hi = ref[r.measurement_id]
                assert r.sanitize_status == st, (method, r.measurement_id)
                raw = next(m["value"] for m in model_rows if m["measurement_id"] == r.measurement_id)
                _same(r.value_as_number, reference_clean(raw, st, lo, hi, action))
        drop = sanitize_measurements(con, method=method, action="drop_row", **kw)
        ref = reference(model_rows, key, limits=bundled_limits(), k_iqr=1.5, z_thr=2.0)
        assert set(drop["measurement_id"]) == {i for i, (st, _, _) in ref.items() if st not in FLAGGED}


# ---------------------------------------------------------------------------------- invalid numbers
@pytest.mark.parametrize("method", METHODS)
def test_nan_and_infinity_are_invalid_under_every_method(ro, method):
    kw = PARAMS[method]
    nullified = sanitize_measurements(ro, method=method, action="nullify", **kw).set_index("measurement_id")
    for i in (7, 8, 9, 59, 65):
        assert nullified.loc[i, "sanitize_status"] == "invalid_number"
        assert pd.isna(nullified.loc[i, "value_as_number"])
    dropped = sanitize_measurements(ro, method=method, action="drop_row", **kw)
    assert not {7, 8, 9, 59, 65} & set(dropped["measurement_id"])
    clamped = sanitize_measurements(ro, method=method, action="clamp", **kw).set_index("measurement_id")
    assert pd.isna(clamped.loc[7, "value_as_number"]) and pd.isna(clamped.loc[59, "value_as_number"]), "NaN -> NULL"
    assert math.isnan(clamped.loc[7, "value_as_number_raw"]) and clamped.loc[8, "value_as_number_raw"] == math.inf


def test_clamp_sends_infinity_to_the_nearest_finite_bound(ro):
    dqd = sanitize_measurements(ro, action="clamp").set_index("measurement_id")
    assert dqd.loc[8, "value_as_number"] == 300 and dqd.loc[9, "value_as_number"] == 40
    assert pd.isna(dqd.loc[65, "value_as_number"]), "no limit row -> no bound -> NULL"
    iqr = sanitize_measurements(ro, method="winsorize_iqr", action="clamp", iqr_multiplier=1.5).set_index("measurement_id")
    assert iqr.loc[8, "value_as_number"] == pytest.approx(588) and iqr.loc[9, "value_as_number"] == pytest.approx(-290)
    assert iqr.loc[65, "value_as_number"] == pytest.approx(24)


def test_clamp_moves_values_to_the_violated_bound(ro):
    df = sanitize_measurements(ro, action="clamp").set_index("measurement_id")
    assert (df.loc[2, "value_as_number"], df.loc[3, "value_as_number"], df.loc[6, "value_as_number"]) == (40, 300, 40)
    assert df.loc[19, "value_as_number"] == pytest.approx(0.1) and df.loc[22, "value_as_number"] == 2650
    assert df.loc[2, "value_as_number_raw"] == 0 and df.loc[3, "value_as_number_raw"] == 999


# ---------------------------------------------------------------------------------- safety
def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_read_only_connection_and_database_file_untouched(db_path, ro):
    before = _sha256(db_path)
    listing = sorted(p.name for p in Path(db_path).parent.iterdir())
    for method in METHODS:
        for action in ACTIONS:
            sanitize_measurements(ro, cohort_table="sanitize_cohort", method=method, action=action,
                                  limits=(pd.DataFrame({"concept_id": [SBP], "min_value": [1], "max_value": [2]})
                                          if method == "dqd_biologic_limits" else None))
    assert _sha256(db_path) == before
    assert sorted(p.name for p in Path(db_path).parent.iterdir()) == listing, "no WAL/temp file next to the database"
    with pytest.raises(duckdb.Error):
        ro.execute("CREATE TABLE should_fail AS SELECT 1")     # the connection really is read-only


def _catalog_snapshot(con):
    return {
        "tables": con.execute("SELECT database_name, schema_name, table_name, temporary FROM duckdb_tables() "
                              "ORDER BY ALL").fetchall(),
        "views": con.execute("SELECT database_name, schema_name, view_name, temporary FROM duckdb_views() "
                             "WHERE NOT internal ORDER BY ALL").fetchall(),
        "functions": con.execute("SELECT function_name, function_type FROM duckdb_functions() "
                                 "WHERE NOT internal ORDER BY ALL").fetchall(),
        "sequences": con.execute("SELECT sequence_name FROM duckdb_sequences() ORDER BY ALL").fetchall(),
        "data": con.execute("SELECT md5(string_agg(CAST(t AS VARCHAR), '|' ORDER BY measurement_id)) "
                            "FROM measurement AS t").fetchone(),
    }


def test_creates_no_objects_and_changes_no_data(rw):
    before = _catalog_snapshot(rw)
    for method in METHODS:
        for action in ACTIONS:
            for fmt in ("df", "arrow"):
                sanitize_measurements(rw, cohort_table="sanitize_cohort", method=method, action=action, format=fmt)
    assert _catalog_snapshot(rw) == before


def test_identifiers_are_quoted_not_spliced(rw):
    for bad in ("sanitize_cohort; DROP TABLE measurement", 'sanitize_cohort" --', "sanitize_cohort) UNION SELECT"):
        with pytest.raises(ValueError, match="could not be read"):
            sanitize_measurements(rw, cohort_table=bad)
    with pytest.raises(ValueError, match="not found"):
        sanitize_measurements(rw, cohort_table="sanitize_cohort", person_col='subject_id") FROM x --')
    assert rw.execute("SELECT count(*) FROM measurement").fetchone()[0] == 69


# ---------------------------------------------------------------------------------- argument validation
@pytest.mark.parametrize("kwargs, match", [
    ({"method": "bogus"}, "method must be one of"),
    ({"method": None}, "method must be one of"),
    ({"action": "delete"}, "action must be one of"),
    ({"format": "csv"}, "format must be one of"),
    ({"format": "sparse"}, "format must be one of"),
    *[({"iqr_multiplier": v}, "iqr_multiplier") for v in (0, -1.5, float("nan"), float("inf"), "3", True, None)],
    *[({"z_threshold": v}, "z_threshold") for v in (0, -2, float("nan"), float("inf"), "4", False, None)],
    ({"measurement_concept_ids": []}, "is empty"),
    ({"measurement_concept_ids": ["abc"]}, "whole number"),
    ({"measurement_concept_ids": [1.5]}, "whole number"),
    ({"measurement_concept_ids": [True]}, "whole number"),
    ({"measurement_concept_ids": [float("nan")]}, "whole number"),
    ({"measurement_concept_ids": "3004249"}, "sequence of integer"),
    ({"cohort_table": ""}, "non-empty table"),
    ({"cohort_table": 5}, "non-empty table"),
    ({"cohort_table": "a.b.c.d"}, "schema.table"),
    ({"cohort_table": "a..b"}, "schema.table"),
    ({"cohort_table": "no_such_table"}, "could not be read"),
    ({"cohort_table": "sanitize_cohort", "person_col": ""}, "person_col"),
    ({"cohort_table": "sanitize_cohort", "person_col": "person_id"}, "person_col 'person_id' not found"),
    ({"limits": pd.DataFrame({"concept_id": [1], "min_value": [0]})}, "missing required column"),
    ({"limits": 5}, "limits must be a DataFrame"),
    ({"limits": pd.DataFrame({"concept_id": [1], "min_value": [5], "max_value": [1]})}, "greater than max_value"),
    ({"limits": pd.DataFrame({"concept_id": [1], "min_value": [np.nan], "max_value": [np.nan]})}, "both missing"),
    ({"limits": pd.DataFrame({"concept_id": [1], "min_value": [0], "max_value": [np.inf]})}, "must be finite"),
    ({"limits": pd.DataFrame({"concept_id": [1], "min_value": ["abc"], "max_value": [3]})}, "numeric or missing"),
    ({"limits": pd.DataFrame({"concept_id": [1.5], "min_value": [0], "max_value": [3]})}, "whole number"),
    ({"limits": pd.DataFrame({"concept_id": [None], "min_value": [0], "max_value": [3]})}, "concept_id is missing"),
    ({"limits": pd.DataFrame({"concept_id": [1, 1], "min_value": [0, 1], "max_value": [3, 4]})}, "duplicate"),
    ({"limits": pd.DataFrame({"concept_id": [1, 1], "unit_concept_id": [8876, 8876], "min_value": [0, 1],
                              "max_value": [3, 4]})}, "duplicate"),
])
def test_argument_validation(rw, kwargs, match):
    with pytest.raises(ValueError, match=match):
        sanitize_measurements(rw, **kwargs)


@pytest.mark.parametrize("name", ["base", "lim", "stats", "joined", "cls", "fin", "zscale", "c", "m"])
def test_cohort_table_named_like_an_internal_cte_still_resolves(rw, name):
    """The generated WITH chain uses short CTE names; a user table of the same name must not be shadowed."""
    rw.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM (VALUES (1), (2)) t(subject_id)')
    try:
        for method in METHODS:
            df = sanitize_measurements(rw, cohort_table=name, method=method, **PARAMS[method])
            assert set(df["person_id"]) == {1, 2}, (name, method)
    finally:
        rw.execute(f'DROP TABLE IF EXISTS "{name}"')


def test_validation_happens_before_any_query_runs():
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("must not query the database for invalid arguments")

    with pytest.raises(ValueError):
        sanitize_measurements(Boom(), method="nope")
    with pytest.raises(ValueError):
        sanitize_measurements(Boom(), measurement_concept_ids=[])


def test_missing_measurement_table_or_columns():
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="needs a CDM 'measurement' table"):
        sanitize_measurements(empty)
    empty.execute("CREATE TABLE measurement (measurement_id INTEGER, person_id INTEGER, value_as_number DOUBLE)")
    with pytest.raises(ValueError, match=r"missing required column\(s\) \['measurement_concept_id', "
                                         r"'measurement_date', 'unit_concept_id'\]"):
        sanitize_measurements(empty)


# ---------------------------------------------------------------------------------- formats and schemas
def test_arrow_format_has_the_same_rows_and_no_report(ro):
    for alias in ("arrow", "pyarrow", "ARROW "):
        tbl = sanitize_measurements(ro, action="clamp", format=alias)
        assert isinstance(tbl, pa.Table)
        assert not hasattr(tbl, "attrs"), "the report is only attached to the pandas format"
    df = sanitize_measurements(ro, action="clamp")
    got = tbl.to_pandas()
    assert list(got.columns) == list(df.columns)
    skip = ["measurement_date", "measurement_datetime"]      # arrow and pandas differ only in date dtypes
    pd.testing.assert_frame_equal(got.drop(columns=skip).reset_index(drop=True),
                                  df.drop(columns=skip).reset_index(drop=True), check_dtype=False)
    assert list(got["measurement_date"].astype(str)) == [str(d.date()) for d in df["measurement_date"]]


def test_polars_format(ro):
    pl = pytest.importorskip("polars")
    out = sanitize_measurements(ro, format="polars")
    assert isinstance(out, pl.DataFrame) and out.columns == CORE_COLUMNS + ["measurement_datetime", "visit_occurrence_id"]
    assert out.height == 68


def test_polars_format_hands_the_arrow_table_to_polars(ro, monkeypatch):
    """Dispatch check that does not need polars installed: a stub module records what it was given."""
    seen = []
    stub = types.ModuleType("polars")
    stub.from_arrow = lambda table: seen.append(table) or "polars-frame"
    monkeypatch.setitem(sys.modules, "polars", stub)
    assert sanitize_measurements(ro, format="polars") == "polars-frame"
    assert len(seen) == 1 and isinstance(seen[0], pa.Table) and seen[0].num_rows == 68
    assert not hasattr(seen[0], "attrs"), "no report on the polars/arrow formats"


def test_polars_missing_warns_and_returns_arrow(ro, monkeypatch):
    monkeypatch.setitem(sys.modules, "polars", None)       # makes `import polars` raise ImportError
    with pytest.warns(UserWarning, match="polars is not installed"):
        out = sanitize_measurements(ro, format="polars")
    assert isinstance(out, pa.Table) and out.num_rows == 68


def test_minimal_measurement_table_and_decimal_values():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE measurement (measurement_id INTEGER, person_id INTEGER, measurement_concept_id INTEGER, "
                "measurement_date DATE, unit_concept_id INTEGER, value_as_number DECIMAL(18, 3))")
    con.execute(f"INSERT INTO measurement VALUES (1, 1, {SBP}, DATE '2021-01-01', 8876, 120.5), "
                f"(2, 1, {SBP}, DATE '2021-01-02', 8876, 999.0), (3, 1, {SBP}, DATE '2021-01-03', 8876, NULL)")
    df = sanitize_measurements(con)
    assert list(df.columns) == CORE_COLUMNS, "optional columns are passed through only when the table has them"
    assert list(df["sanitize_status"]) == ["ok", "above_max"]
    assert df["value_as_number"].tolist()[0] == 120.5 and pd.isna(df["value_as_number"].tolist()[1])


def test_empty_measurement_table_returns_empty_frames():
    con = _measurement_table([])
    for method in METHODS:
        df = sanitize_measurements(con, method=method)
        assert len(df) == 0 and list(df.columns)[:8] == CORE_COLUMNS
        assert list(df.attrs["sanitization_report"].columns) == REPORT_COLUMNS
        assert len(df.attrs["sanitization_report"]) == 0


# ---------------------------------------------------------------------------------- report object, warnings, extremes
def test_report_is_a_dataframe_and_the_documented_pandas_workaround_works(ro, tmp_path):
    df = sanitize_measurements(ro)
    assert isinstance(df.attrs["sanitization_report"], pd.DataFrame)
    # pandas cannot serialize or concat a DataFrame kept in attrs; the docstring says to pop it first
    other = sanitize_measurements(ro, action="clamp")
    reports = [d.attrs.pop("sanitization_report") for d in (df, other)]
    assert all(isinstance(r, pd.DataFrame) for r in reports) and df.attrs == {}
    df.to_parquet(tmp_path / "clean.parquet")
    both = pd.concat([df, other])
    assert len(both) == 2 * 68
    # slicing and copying keep the report
    again = sanitize_measurements(ro)
    assert isinstance(again[again["person_id"] == 1].copy().attrs["sanitization_report"], pd.DataFrame)


def _sbp_rows(units):
    return [(i, 1, SBP, u, 120.0 + i) for i, u in enumerate(units, start=1)]


def test_warns_when_most_limit_concept_rows_have_no_unit():
    # 3 of 4 blood pressures have unit 0 / NULL: they are unit_skipped and the user must hear about it
    con = _measurement_table(_sbp_rows([8876, 0, None, 0]) + [(9, 1, POTASSIUM, 8753, 4.0)])
    with pytest.warns(UserWarning, match=r"more than half of the measurements of 1 concept\(s\).*concept_id 3004249"):
        df = sanitize_measurements(con)
    assert list(df["sanitize_status"]).count("unit_skipped") == 3


def test_unscreened_warning_lists_at_most_five_concepts():
    concepts = sorted({c for c, *_ in _sanitize_bundled_limits()})[:7]
    con = _measurement_table([(i, 1, c, 0, 1.0) for i, c in enumerate(concepts, start=1)])
    with pytest.warns(UserWarning, match=r"7 concept\(s\).*\.\.\. \(7 in all\)"):
        sanitize_measurements(con)


def test_no_unit_warning_when_half_or_fewer_rows_lack_a_unit_or_the_user_supplied_limits(recwarn):
    half = _measurement_table(_sbp_rows([8876, 0, 8876, 0]))                  # exactly half: no warning
    sanitize_measurements(half)
    ok = _measurement_table(_sbp_rows([8876, 8876, 8876, 0]))
    sanitize_measurements(ok)
    # an any-unit limit screens unit-less rows, so nothing is unscreened
    allnull = _measurement_table(_sbp_rows([None, None, 0, 0]))
    sanitize_measurements(allnull, limits=pd.DataFrame({"concept_id": [SBP], "min_value": [40], "max_value": [300]}))
    # the statistical methods never need units and never warn
    sanitize_measurements(allnull, method="winsorize_iqr")
    sanitize_measurements(allnull, method="z_score_cutoff")
    # concepts without any limit row are no_limit, not unit_skipped: nothing to warn about
    sanitize_measurements(_measurement_table([(1, 1, X_CONCEPT, 0, 1.0), (2, 1, X_CONCEPT, 0, 2.0)]))
    assert [w for w in recwarn if "NOT screened" in str(w.message)] == []


def test_the_unscreened_warning_is_also_raised_for_the_arrow_format():
    """The arrow format carries no report, but the warning is still raised for the limits method."""
    con = _measurement_table(_sbp_rows([0, 0, 0]))
    with pytest.warns(UserWarning, match="NOT screened"):
        tbl = sanitize_measurements(con, format="arrow")
    assert isinstance(tbl, pa.Table) and tbl.num_rows == 3


@pytest.mark.parametrize("big", [1e160, 1e300, 1.7e308, -1.7e308])
@pytest.mark.parametrize("method", ["winsorize_iqr", "z_score_cutoff"])
def test_extreme_magnitudes_neither_overflow_nor_abort(method, big):
    """One absurd artifact must not make DuckDB raise (STDDEV_SAMP overflows on squares above ~1e154)."""
    rows = [(i, 1, 7, 8840, float(i)) for i in range(1, 11)] + [(11, 1, 7, 8840, big)]
    con = _measurement_table(rows)
    kw = {"z_threshold": 2.0} if method == "z_score_cutoff" else {}
    nullified = sanitize_measurements(con, method=method, action="nullify", **kw).set_index("measurement_id")
    assert nullified.loc[11, "sanitize_status"] == ("above_max" if big > 0 else "below_min")
    assert pd.isna(nullified.loc[11, "value_as_number"]) and nullified.loc[11, "value_as_number_raw"] == big
    assert list(nullified.loc[1:10, "sanitize_status"]) == ["ok"] * 10
    clamped = sanitize_measurements(con, method=method, action="clamp", **kw).set_index("measurement_id")
    assert math.isfinite(clamped.loc[11, "value_as_number"]) and abs(clamped.loc[11, "value_as_number"]) < abs(big)
    # a threshold so wide that the fences overflow flags nothing instead of failing
    huge = sanitize_measurements(con, method=method, action="clamp",
                                 **{"iqr_multiplier": 1e308} if method == "winsorize_iqr" else {"z_threshold": 1e308})
    assert (huge["sanitize_status"] == "ok").all()


# ---------------------------------------------------------------------------------- bundled limits table
def test_bundled_limits_table_is_well_formed():
    with open(LIMITS_CSV, newline="", encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))
    assert list(raw[0].keys()) == ["concept_id", "name", "category", "loinc_code", "unit", "unit_concept_id",
                                   "min_value", "max_value", "source", "note"]
    keys = [(int(r["concept_id"]), int(r["unit_concept_id"])) for r in raw]
    assert len(keys) == len(set(keys)), "one row per (concept_id, unit_concept_id)"
    assert all(float(r["min_value"]) < float(r["max_value"]) for r in raw)
    assert {r["category"] for r in raw} <= {"vital", "lab"}
    loaded = _sanitize_bundled_limits()
    assert len(loaded) == len(raw) == 136
    assert {c for c, *_ in loaded} >= {SBP, CREAT, POTASSIUM, 3012888, 3027018}


@pytest.mark.skipif(not os.environ.get("OMOP_VOCAB_DB") or not Path(os.environ.get("OMOP_VOCAB_DB", "")).exists(),
                    reason="set OMOP_VOCAB_DB to an Athena-loaded DuckDB to validate the limits table's concept ids")
def test_bundled_limits_concepts_exist_in_the_vocabulary():
    con = duckdb.connect(os.environ["OMOP_VOCAB_DB"], read_only=True)
    try:
        lim = pd.DataFrame(_sanitize_bundled_limits(), columns=["concept_id", "unit_concept_id", "lo", "hi"])
        ids = lim["concept_id"].drop_duplicates().tolist()
        found = con.execute("SELECT concept_id, domain_id, standard_concept, invalid_reason FROM concept "
                            "WHERE concept_id IN (SELECT unnest(?::BIGINT[]))", [ids]).df()
        assert set(found["concept_id"]) == set(ids), "concept ids missing from the vocabulary"
        assert (found["domain_id"] == "Measurement").all() and (found["standard_concept"] == "S").all()
        assert found["invalid_reason"].isna().all()
        units = lim["unit_concept_id"].drop_duplicates().tolist()
        found = con.execute("SELECT concept_id, domain_id FROM concept WHERE concept_id IN (SELECT unnest(?::BIGINT[]))",
                            [units]).df()
        assert set(found["concept_id"]) == set(units) and (found["domain_id"] == "Unit").all()
    finally:
        con.close()


def test_resource_lookup_works_from_the_wheel_layout(tmp_path):
    """Mimic ``pip install``: the CSV under omop_etl/resources/extdata/, no repository ``inst/`` anywhere."""
    pkg = tmp_path / "site" / "omop_etl"
    (pkg / "resources" / "extdata").mkdir(parents=True)
    for name in ("dqd.py", "build_omop_cdm.py"):
        (pkg / name).write_bytes((REPO_ROOT / "python" / "omop_etl" / name).read_bytes())
    (pkg / "__init__.py").write_text("")
    probe = ("from omop_etl.dqd import _sanitize_bundled_limits; "
             "rows = _sanitize_bundled_limits(); print(len(rows))")
    env = {**os.environ, "PYTHONPATH": str(pkg.parent)}

    missing = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    assert missing.returncode != 0 and "Bundled physiologic limits table not found" in missing.stderr

    (pkg / "resources" / "extdata" / "physiologic_limits.csv").write_bytes(LIMITS_CSV.read_bytes())
    found = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == "136"
