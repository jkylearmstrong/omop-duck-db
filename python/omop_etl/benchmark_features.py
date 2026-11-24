"""One-call, leakage-free tabular feature table for the readmission benchmark (v0.13.0).

:func:`extract_benchmark_features` chains the existing extractors (Elixhauser, prior utilization, medication classes,
geography) and adds index-admission vitals and labs (mean **and** latest in-stay value), derived eGFR / anion gap and
the discharge disposition, so a site needs one call instead of five modules.

Leakage policy (all windows are fixed by the code, not by the caller):

* history (Elixhauser, medications, utilization) ends at or before the index admission, except Elixhauser, which is
  anchored at discharge and includes the index stay's own diagnoses (they are coded by discharge);
* vitals and labs use measurements dated between the admission (minus ``lab_lookback_days``, default 0) and the
  discharge date, never later;
* nothing after the discharge date enters any column, and the outcome is returned as ``y`` only.
"""

from __future__ import annotations

import os
import re
import warnings
from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.concept_sets import ancestor_specs, codes_by_name, concept_set_from_codes, load_concept_set, load_concept_sets, loinc_coverage, resolve_concept_sets
from omop_etl.data_gaps import DataGapWarning

__all__ = ["extract_benchmark_features", "ederri_disposition_level", "extract_protocol_history_features", "DEFAULT_DISPOSITION_MAP", "BENCHMARK_LAB_LOINCS", "BENCHMARK_VITAL_LOINCS", "BENCHMARK_LAB_CONCEPT_SETS", "BENCHMARK_VITAL_CONCEPT_SETS",
           "DEFAULT_BOUNDS", "DEFAULT_RESCALE", "EDERRI_MEDICATION_SPECS"]

#: Lab concept sets (feature ``lab_<name>``), read from the bundled OHDSI concept-set files ``concept_sets/labs/<name>.json``
#: (see :mod:`omop_etl.concept_sets`); ``BENCHMARK_LAB_LOINCS`` is the same as ``{name: [LOINC code, ...]}``. A superset of
#: ``features.CORE_14_LAB_PANEL``: sites code one test under several LOINC codes (blood vs serum/plasma, method variants), and
#: a code outside the set drops the lab, which the ``concept_set_coverage`` check reports. A name with no concept in the
#: database is skipped and reported in ``attrs["skipped"]``.
BENCHMARK_LAB_CONCEPT_SETS: dict[str, dict[str, Any]] = load_concept_sets("labs")
BENCHMARK_LAB_LOINCS: dict[str, list[str]] = codes_by_name("labs")
#: Each lab set's expansion mode (``concept_sets.EXPANSION_MODES``); only ``blood`` sets fail on a missed same-unit code.
BENCHMARK_LAB_EXPANSION: dict[str, str] = {n: load_concept_set(f"labs/{n}").get("expansion", "blood") for n in BENCHMARK_LAB_LOINCS}

#: LOINC codes per vital sign (feature ``vital_<name>``), from ``concept_sets/vitals``.
BENCHMARK_VITAL_CONCEPT_SETS: dict[str, dict[str, Any]] = load_concept_sets("vitals")
BENCHMARK_VITAL_LOINCS: dict[str, list[str]] = codes_by_name("vitals")

#: Physiologic plausibility bounds ``{feature name: (min, max)}`` in conventional US units (mg/dL, g/dL, 10^3/uL, degrees Celsius).
#: A single measurement outside its bounds is dropped before the per-stay mean / latest value is taken, so one artifact such as a
#: BMI of 562000 cannot dominate a stay. Pass ``bounds=`` to change or extend them, ``bounds=None`` to switch them off.
DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "bmi": (10, 100), "systolic_bp": (50, 260), "diastolic_bp": (30, 160), "heart_rate": (20, 250), "respiratory_rate": (4, 60),
    "body_temperature": (30, 45), "oxygen_saturation": (50, 100),
    "glucose": (10, 1500), "bun": (1, 250), "creatinine": (0.1, 30), "sodium": (100, 180), "potassium": (1.5, 10),
    "chloride": (60, 150), "bicarbonate": (5, 60), "calcium": (4, 16), "hematocrit": (10, 75), "hemoglobin": (1, 25),
    "wbc": (0.1, 200), "platelets": (5, 2000), "albumin": (0.5, 7), "bilirubin_total": (0.1, 40),
    "alkaline_phosphatase": (5, 2500), "ast": (1, 5000), "alt": (1, 5000), "hba1c": (3, 25), "ldl": (10, 500), "anion_gap": (1, 45),
    "troponin_i": (0, 1000), "lactate": (0.1, 30), "pco2": (5, 200), "po2": (10, 700),
}

#: ``{feature name: (above, up_to, divisor)}``: a value in ``(above, up_to]`` is divided by ``divisor`` before the bounds are
#: applied. WBC is reported as a count per uL (7500) by some sites and in 10^3/uL (7.5) by others.
DEFAULT_RESCALE: dict[str, tuple[float, float, float]] = {"wbc": (200.0, 200000.0, 1000.0)}

#: Medication classes of the eDERRI protocol: the ATC class concepts, flag ``med_<name>``. Differs from the generic defaults
#: (``antihypertensives`` is ATC C02 only, ``lipid_modifying`` is all of C10, ``biguanides`` is A10BA); use it with
#: ``profile="ederri"`` or ``medication_specs=EDERRI_MEDICATION_SPECS`` and ``medication_window="pre_discharge"``.
EDERRI_MEDICATION_SPECS: dict[str, dict[str, list[int]]] = ancestor_specs("medications_ederri")  # concept_sets/medications_ederri

#: discharge disposition concept -> benchmark category. 8536 Home, 581476 Home Health, 8863 SNF, 32216 AMA (as written by
#: the PCORnet ETL; HH is written as 32215 and RH as 32228 since 0.11.1). Everything else is "other"; a missing disposition is its own
#: indicator.
DEFAULT_DISPOSITION_MAP: dict[int, str] = {8536: "home", 581476: "home_health", 32215: "home_health", 8863: "snf", 32228: "snf",
                                            32216: "ama"}
_DISPOSITION_LEVELS = ("home", "home_health", "snf", "ama", "other", "missing")
#: The study protocol's discharge-status rule (concept sets, or the source value HO / HH / SN, RH / AM): ``disposition_map="ederri"``.
#: Since 0.33.0 each set also holds the concept this package's PCORnet ETL writes for that status (HH 32215, RH 32228, AM 32216), so
#: the class no longer depends on the source value alone.
_EDERRI_DISPOSITION_CONCEPTS = {"home": {8536, 4161979}, "home_health": {8844, 4038234, 32215}, "snf": {8863, 8676, 8827, 4163901, 32228},
                                "ama": {4155309, 44814650, 32216}}
#: PCORnet DISCHARGE_STATUS values that carry no information (no information, unknown, blank): the stay's status is missing,
#: not "other". Any other unmatched value (hospice, assisted living, other, ...) is "other".
_MISSING_DISPOSITION_SOURCE = {"", "NI", "UN", "UNKNOWN", "NULL"}

def ederri_disposition_level(concept_id: Any, source_value: Any) -> str:
    """The study protocol's discharge-status class of one stay: ``home``, ``home_health``, ``snf``, ``ama``, ``other`` or ``missing``,
    from its ``discharged_to_concept_id`` or, where that is not in the protocol's concept sets, its ``discharged_to_source_value``
    (trimmed, upper case). ``missing`` is a stay with no concept and no informative source value (PCORnet NI, UN or blank)."""
    c = -1 if concept_id is None or pd.isna(concept_id) else int(concept_id)
    sv = None if source_value is None or (not isinstance(source_value, str) and pd.isna(source_value)) else str(source_value).strip().upper()
    if c in _EDERRI_DISPOSITION_CONCEPTS["home"] or sv in ("HO", "HOME"):
        return "home"
    if c in _EDERRI_DISPOSITION_CONCEPTS["home_health"] or sv in ("HH", "HOME HEALTH"):
        return "home_health"
    if c in _EDERRI_DISPOSITION_CONCEPTS["snf"] or sv in ("SN", "RH", "SNF", "REHAB"):
        return "snf"
    if c in _EDERRI_DISPOSITION_CONCEPTS["ama"] or sv in ("AM", "AMA"):
        return "ama"
    if c <= 0 and (sv is None or sv in _MISSING_DISPOSITION_SOURCE):
        return "missing"  # no concept and no informative source value
    return "other"


def _default_medication_specs() -> dict[str, dict[str, list[int]]]:
    from omop_etl.table1 import DEFAULT_MEDICATION_CONCEPTS
    return {k: {"ancestor_concept_ids": list(v)} for k, v in DEFAULT_MEDICATION_CONCEPTS.items()}


def _vocab_reachable(con) -> bool:
    try:
        return con.execute("SELECT 1 FROM concept_ancestor LIMIT 1").fetchone() is not None
    except (duckdb.CatalogException, duckdb.BinderException):
        return False


def _ensure_vocabulary(con, vocabulary_db) -> None:
    """Attach an adjacent central vocabulary file when ``concept_ancestor`` cannot be read (multi-database setups)."""
    if vocabulary_db is None or _vocab_reachable(con):
        return
    path = None
    if vocabulary_db == "auto":
        try:
            files = [r[2] for r in con.execute("PRAGMA database_list").fetchall() if r[2]]
        except duckdb.Error:
            files = []
        for f in files:
            for name in ("central_vocabulary.duckdb", "central_vocab.duckdb"):
                cand = os.path.join(os.path.dirname(os.path.abspath(f)), name)
                if os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(f):
                    path = cand
                    break
            if path:
                break
    else:
        path = os.fspath(vocabulary_db)
    if path is None:
        return
    from omop_etl.build_omop_cdm import attach_central_vocabulary
    try:
        attach_central_vocabulary(con, path)
        warnings.warn(f"concept_ancestor was not reachable; attached the central vocabulary {path!r}.", UserWarning, stacklevel=3)
    except Exception as exc:   # an already attached catalog, a locked file: carry on and let the extractors report it
        warnings.warn(DataGapWarning(f"Could not attach the central vocabulary {path!r}: {exc}", "vocabulary"), stacklevel=3)


def _cohort_frame(con, cohort_table: str, cohort_id: int | None) -> pd.DataFrame:
    cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()]
    pick = lambda *names: next((n for n in names if n in cols), None)  # noqa: E731
    person = pick("subject_id", "person_id")
    start = pick("cohort_start_date", "visit_start_date", "index_date")
    end = pick("cohort_end_date", "visit_end_date")
    if person is None or start is None:
        raise ValueError(f"{cohort_table!r} needs a person column (subject_id / person_id) and an admission date "
                         f"(cohort_start_date / visit_start_date); found {cols}.")
    sel = [f"{person} AS subject_id", f"CAST({start} AS DATE) AS cohort_start_date",
           f"CAST({end or start} AS DATE) AS cohort_end_date"]
    for extra in ("visit_occurrence_id", "outcome_flag", "discharged_to_concept_id", "site", "site_anon", "stay_rank"):
        if extra in cols:
            sel.append(extra)
    where = f" WHERE cohort_definition_id = {int(cohort_id)}" if cohort_id is not None and "cohort_definition_id" in cols else ""
    df = con.execute(f"SELECT {', '.join(sel)} FROM {cohort_table}{where}").df()
    if df.empty:
        raise ValueError(f"{cohort_table!r} has no rows" + (f" for cohort_definition_id {cohort_id}" if where else "") + ".")
    df["cohort_start_date"] = pd.to_datetime(df["cohort_start_date"])
    df["cohort_end_date"] = pd.to_datetime(df["cohort_end_date"]).fillna(df["cohort_start_date"])
    if (df["cohort_end_date"] < df["cohort_start_date"]).any():
        raise ValueError("Some cohort rows are discharged before they are admitted.")
    df.insert(0, "row_id", np.arange(len(df), dtype="int64"))
    return df


def _concept_map(con, loincs: Mapping[str, Sequence[str]] | None, overrides: Mapping[str, Sequence[int]] | None,
                 sets: Mapping[str, Mapping[str, Any]] | None = None) -> pd.DataFrame:
    """name -> concept_id rows: explicit ids first; every other name is resolved as a concept set (``sets``, or LOINC codes
    from ``loincs`` turned into one) through the vocabulary (:func:`omop_etl.concept_sets.resolve_concept_sets`)."""
    rows: list[tuple[str, int]] = []
    for name, ids in (overrides or {}).items():
        rows += [(name, int(i)) for i in ids]
    exprs = dict(sets) if sets is not None else {n: concept_set_from_codes(cs) for n, cs in (loincs or {}).items() if cs}
    exprs = {n: e for n, e in exprs.items() if n not in (overrides or {})}
    if exprs and bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'concept'").fetchone()[0]):
        got = resolve_concept_sets(con, exprs, warn=False)
        rows += [(n, int(i)) for n, i in got.itertuples(index=False)]
    return pd.DataFrame(rows, columns=["name", "concept_id"]).drop_duplicates()


def _check_lab_coverage(con, sets: Mapping[str, Mapping[str, Any]], cohort: pd.DataFrame, pre_days: int,
                        min_share: float = 0.01) -> None:
    """Fails loudly (``concept_set_coverage``) when a lab's in-stay rows sit under LOINC codes its concept set does not hold.

    Uses the LOINC part links of the vocabulary; without them it says the check could not run (a plain warning)."""
    if not sets:
        return
    try:
        miss = loinc_coverage(con, sets, cohort=cohort, pre_days=pre_days, min_share=min_share)
    except duckdb.Error as exc:  # an unusual vocabulary layout must not stop feature extraction
        warnings.warn(f"lab concept-set coverage not checked: {str(exc).splitlines()[0]}", stacklevel=3)
        return
    if miss is None:
        warnings.warn("lab concept-set coverage not checked: the vocabulary has no LOINC part links (Has component ...), so "
                      "lab codes outside the bundled concept sets cannot be found", stacklevel=3)
        return
    def fmt(g: pd.DataFrame) -> str:
        return "; ".join(f"{r.concept_code} {r.share:.1%}" for r in g.itertuples())

    # an error only for a code the set should hold: same component, property and scale, blood / serum / plasma.
    # A code of another property (molar units, a partial pressure such as pCO2 next to total CO2) or another specimen
    # is reported as a plain warning: it needs a unit conversion or is a different test, never a silent loss.
    blood_sets = {n for n in sets if BENCHMARK_LAB_EXPANSION.get(n, "blood") == "blood"}
    hard = miss["same_property"] & miss["blood"] & miss["name"].isin(blood_sets)
    for name, g in miss[hard].groupby("name", sort=False):
        warnings.warn(DataGapWarning(f"lab_{name}: in-stay rows for this analyte use LOINC codes outside its concept set ({fmt(g)}); "
                                     f"add them to concept_sets/labs/{name}.json or accept with --allow-data-gaps lab_{name}",
                                     "concept_set_coverage", f"lab_{name}"), stacklevel=3)
    for name, g in miss[~hard].groupby("name", sort=False):
        warnings.warn(f"lab_{name}: in-stay rows use codes of the same analyte in another unit type or specimen, not counted "
                      f"({fmt(g)}); the set leaves them out on purpose or they need a unit conversion", stacklevel=3)

#: Visit concepts of an outpatient visit (0.34.0): outpatient (9202; PCORnet AV and OA), office visit (581477) and telehealth (5083).
OUTPATIENT_VISIT_CONCEPTS: tuple[int, ...] = (9202, 581477, 5083)
_OUTPATIENT_SQL = ", ".join(str(i) for i in OUTPATIENT_VISIT_CONCEPTS)
#: Condition type / status concepts of a principal (primary) diagnosis: primary condition (44786627, written by the PCORnet ETL for
#: PDX = "P"), first position condition (44786628) and primary diagnosis (32902).
PRINCIPAL_DX_CONCEPTS: tuple[int, ...] = (44786627, 44786628, 32902)


def _principal_sql(con) -> str:
    """SQL that is true for a principal diagnosis row ``co``, using the columns this condition_occurrence has."""
    cols = {r[0].lower() for r in con.execute("DESCRIBE condition_occurrence").fetchall()}
    ids = ", ".join(str(i) for i in PRINCIPAL_DX_CONCEPTS)
    parts = [f"coalesce(co.{c}, 0) IN ({ids})" for c in ("condition_type_concept_id", "condition_status_concept_id") if c in cols]
    if "condition_status_source_value" in cols:
        parts.append("upper(coalesce(co.condition_status_source_value, '')) = 'P'")
    return "(" + " OR ".join(parts or ["FALSE"]) + ")"


_DX_CODE_SQL = ("coalesce(nullif(upper(replace(trim(co.condition_source_value), '.', '')), ''), "
                "'concept:' || CAST(co.condition_concept_id AS VARCHAR))")


def _bounds_frame(cmap: pd.DataFrame, bounds: Mapping[str, Sequence[float | None]] | None,
                  rescale: Mapping[str, Sequence[float]] | None) -> pd.DataFrame:
    """Adds per-name ``lo`` / ``hi`` / ``rs_lo`` / ``rs_hi`` / ``rs_div`` columns (open bounds as +-1e300; no rescale as div 1)."""
    out = cmap.copy()
    big = 1e300
    lo, hi, r_lo, r_hi, r_div = [], [], [], [], []
    for name in out["name"]:
        b = (bounds or {}).get(name)
        lo.append(-big if b is None or b[0] is None else float(b[0]))
        hi.append(big if b is None or b[1] is None else float(b[1]))
        r = (rescale or {}).get(name)
        r_lo.append(big if r is None else float(r[0]))
        r_hi.append(big if r is None else float(r[1]))
        r_div.append(1.0 if r is None else float(r[2]))
    return out.assign(lo=lo, hi=hi, rs_lo=r_lo, rs_hi=r_hi, rs_div=r_div)


def _measurement_stats(con, cohort: pd.DataFrame, cmap: pd.DataFrame, pre_days: int,
                       bounds: Mapping[str, Sequence[float | None]] | None = None,
                       rescale: Mapping[str, Sequence[float]] | None = None) -> pd.DataFrame:
    """Per (row_id, name): mean, latest value and count of plausible measurements dated in [admit - pre_days, discharge].

    A value in a name's rescale range is divided first; a value outside the name's bounds is dropped (not clipped)."""
    if cmap.empty:
        return pd.DataFrame(columns=["row_id", "name", "mean", "last", "n"])
    con.register("_bf_c", cohort[["row_id", "subject_id", "cohort_start_date", "cohort_end_date"]])
    con.register("_bf_map", _bounds_frame(cmap, bounds, rescale))
    try:
        return con.execute(f"""
            SELECT t.row_id, t.name, avg(t.v) AS mean,
                   arg_max(t.v, struct_pack(t := t.ts, id := t.mid)) AS last,
                   count(*) AS n
            FROM (
                SELECT c.row_id, f.name, m.measurement_id AS mid,
                       COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)) AS ts,
                       CASE WHEN m.value_as_number > f.rs_lo AND m.value_as_number <= f.rs_hi
                            THEN m.value_as_number / f.rs_div ELSE m.value_as_number END AS v,
                       f.lo AS lo, f.hi AS hi
                FROM _bf_c c
                JOIN measurement m ON m.person_id = c.subject_id
                     AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - {int(pre_days)}
                     AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)
                JOIN _bf_map f ON f.concept_id = m.measurement_concept_id
                WHERE m.value_as_number IS NOT NULL
            ) t
            WHERE t.v BETWEEN t.lo AND t.hi
            GROUP BY t.row_id, t.name
        """).df()
    finally:
        con.unregister("_bf_c")
        con.unregister("_bf_map")


#: Body height and body weight concepts used to compute a BMI where none was recorded (LOINC 8302-2 / 3137-5 height,
#: 29463-7 / 3141-9 weight). Units: cm (8582) or inch (9330) for height, kg (9529) or pound (8739) for weight; a value without a
#: unit is read as inches / pounds (the PCORnet VITAL convention), except a height of 100 or more, which can only be centimetres.
BMI_HEIGHT_CONCEPTS: tuple[int, ...] = (3036277, 3023540)
BMI_WEIGHT_CONCEPTS: tuple[int, ...] = (3025315, 3013762)
BMI_FILL_OPTIONS = ("derive", "derive_lookback", "none")


def _fill_bmi(con, cohort: pd.DataFrame, bmi_ids: Sequence[int], measured: pd.Series, *, mode: str, lookback_days: int,
              bounds: Sequence[float | None] | None) -> tuple[pd.Series, dict[str, int]]:
    """Fills a missing in-stay BMI, in order: (1) BMI computed from the stay's mean height and weight, then, with
    ``mode="derive_lookback"``, (2) the latest BMI recorded in the ``lookback_days`` before the admission, (3) BMI from the latest
    height and weight in that window. Every value must lie within ``bounds``. Returns the filled series (by ``row_id``) and the
    number of stays filled by each step."""
    lo, hi = (bounds or (None, None))
    lo, hi = (-1e300 if lo is None else float(lo)), (1e300 if hi is None else float(hi))
    con.register("_bmi_c", cohort[["row_id", "subject_id", "cohort_start_date", "cohort_end_date"]])
    ids = lambda xs: ", ".join(str(int(x)) for x in xs) or "NULL"  # noqa: E731
    cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'measurement'").fetchall()}
    unit = "m.unit_concept_id" if "unit_concept_id" in cols else "CAST(NULL AS INTEGER)"
    # the unit as written, for a database whose unit_concept_id is 0 but whose source says "kg" or "cm" (0.34.1 review)
    usv = "lower(trim(m.unit_source_value))" if "unit_source_value" in cols else "CAST(NULL AS VARCHAR)"
    try:
        got = con.execute(f"""
            WITH m AS (
                SELECT c.row_id, m.measurement_date AS d, COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)) AS ts,
                       m.measurement_id AS mid,
                       CAST(m.measurement_date AS DATE) >= CAST(c.cohort_start_date AS DATE) AS in_stay,
                       CASE WHEN m.measurement_concept_id IN ({ids(BMI_HEIGHT_CONCEPTS)}) THEN 'h'
                            WHEN m.measurement_concept_id IN ({ids(BMI_WEIGHT_CONCEPTS)}) THEN 'w' ELSE 'b' END AS kind,
                       CASE WHEN m.measurement_concept_id IN ({ids(BMI_HEIGHT_CONCEPTS)}) THEN
                                 CASE WHEN {unit} = 8582 OR {usv} IN ('cm', 'centimeter', 'centimeters')
                                           OR (COALESCE({unit}, 0) NOT IN (9326, 9330) AND COALESCE({usv}, '') NOT IN ('in', 'inch', 'inches', '[in_i]')
                                               AND m.value_as_number >= 100)
                                      THEN m.value_as_number / 100.0 ELSE m.value_as_number * 0.0254 END
                            WHEN m.measurement_concept_id IN ({ids(BMI_WEIGHT_CONCEPTS)}) THEN
                                 CASE WHEN {unit} = 9529 OR {usv} IN ('kg', 'kgs', 'kilogram', 'kilograms') THEN m.value_as_number
                                      ELSE m.value_as_number * 0.45359237 END
                            ELSE m.value_as_number END AS v
                FROM _bmi_c c
                JOIN measurement m ON m.person_id = c.subject_id
                     AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - {int(lookback_days) if mode == "derive_lookback" else 0}
                     AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)
                WHERE m.value_as_number > 0
                  AND m.measurement_concept_id IN ({ids([*BMI_HEIGHT_CONCEPTS, *BMI_WEIGHT_CONCEPTS, *bmi_ids])})
            ), m2 AS (  -- plausible heights (0.9 to 2.5 m) and weights (20 to 400 kg) only
                SELECT * FROM m WHERE (kind = 'h' AND v BETWEEN 0.9 AND 2.5) OR (kind = 'w' AND v BETWEEN 20 AND 400)
                                   OR (kind = 'b' AND v BETWEEN {lo} AND {hi})
            )
            SELECT row_id,
                   avg(v) FILTER (WHERE in_stay AND kind = 'w') / (avg(v) FILTER (WHERE in_stay AND kind = 'h') ^ 2) AS stay_derived,
                   arg_max(v, struct_pack(t := ts, id := mid)) FILTER (WHERE NOT in_stay AND kind = 'b') AS prior_bmi,
                   arg_max(v, struct_pack(t := ts, id := mid)) FILTER (WHERE NOT in_stay AND kind = 'w')
                     / (arg_max(v, struct_pack(t := ts, id := mid)) FILTER (WHERE NOT in_stay AND kind = 'h') ^ 2) AS prior_derived
            FROM m2 GROUP BY row_id
        """).df().set_index("row_id")
    finally:
        con.unregister("_bmi_c")
    ok = lambda x: x.where((x >= lo) & (x <= hi))  # noqa: E731
    out, counts = measured.copy(), {}
    steps = [("derived_in_stay", "stay_derived")]
    if mode == "derive_lookback":
        steps += [("recorded_prior", "prior_bmi"), ("derived_prior", "prior_derived")]
    for label, col in steps:
        cand = ok(got[col].reindex(out.index).astype(float)).round(2) if col in got else pd.Series(np.nan, index=out.index)
        fill = out.isna() & cand.notna()
        out[fill] = cand[fill]
        counts[label] = int(fill.sum())
    return out, counts


def _wide(stats: pd.DataFrame, prefix: str, names: Sequence[str], n_rows: int, with_last: bool) -> pd.DataFrame:
    out = pd.DataFrame({"row_id": np.arange(n_rows, dtype="int64")}).set_index("row_id")
    for name in names:
        s = stats[stats["name"] == name].set_index("row_id")
        out[f"{prefix}{name}"] = s["mean"].reindex(out.index)
        if with_last:
            out[f"{prefix}{name}_last"] = s["last"].reindex(out.index)
    return out.reset_index()


def _derive_labs(frame: pd.DataFrame, age: pd.Series, female: pd.Series, *, hematocrit_fallback: bool = False) -> list[str]:
    """CKD-EPI 2021 eGFR, anion gap and (optionally) hemoglobin from hematocrit, for the mean and the latest representation.

    A measured anion gap is kept; ``Na - Cl - HCO3`` only fills stays without one. ``hematocrit_fallback`` fills a missing
    hemoglobin with hematocrit / 3 (the usual rule of thumb) for stays with a hematocrit but no hemoglobin.
    Returns the new column names."""
    from omop_etl.velocity import compute_ckd_epi_2021
    new: list[str] = []
    for suffix in ("", "_last"):
        creat, ok = f"lab_creatinine{suffix}", age.notna() & female.notna()
        if creat in frame:
            ok = ok & frame[creat].notna()
            egfr = pd.Series(np.nan, index=frame.index)
            if ok.any():
                egfr[ok] = compute_ckd_epi_2021(frame.loc[ok, creat], age[ok], female[ok].astype(bool))
            frame[f"lab_egfr{suffix}"] = egfr
            new.append(f"lab_egfr{suffix}")
        parts = [f"lab_{n}{suffix}" for n in ("sodium", "chloride", "bicarbonate")]
        if all(p in frame for p in parts):
            calc = frame[parts[0]] - frame[parts[1]] - frame[parts[2]]
            col = f"lab_anion_gap{suffix}"
            if col in frame:
                frame[col] = frame[col].fillna(calc)
            else:
                frame[col] = calc
                new.append(col)
        if hematocrit_fallback and f"lab_hematocrit{suffix}" in frame:
            hgb = f"lab_hemoglobin{suffix}"
            fill = frame[f"lab_hematocrit{suffix}"] / 3.0
            if hgb in frame:
                frame[hgb] = frame[hgb].fillna(fill)
            else:
                frame[hgb] = fill
                new.append(hgb)
    return new


def extract_benchmark_features(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    *,
    cohort_id: int | None = None,
    include_demographics: bool = True,
    include_sdoh: bool = False,
    include_elixhauser: bool = True,
    include_medications: bool = True,
    include_vitals: bool = True,
    include_labs: bool = True,
    include_utilization: bool = True,
    include_disposition: bool = True,
    lookback_days: int = 365,
    lab_lookback_days: int = 0,
    medication_specs: Mapping[str, Any] | None = None,
    medication_window: str | None = None,
    lab_loincs: Mapping[str, Sequence[str]] | None = None,
    vital_loincs: Mapping[str, Sequence[str]] | None = None,
    lab_concept_ids: Mapping[str, Sequence[int]] | None = None,
    vital_concept_ids: Mapping[str, Sequence[int]] | None = None,
    derive_labs: bool = True,
    bounds: Mapping[str, Sequence[float | None]] | str | None = "default",
    profile: str | None = None,
    hematocrit_fallback: bool | None = None,
    elixhauser_lookback_days: int | None | str = "lookback_days",
    elixhauser_code_table: str | None = None,
    medication_set: str | None = None,
    include_protocol_history: bool | None = None,
    ruca_crosswalk: Any = None,
    sdi_table: Any = None,
    sdi_cut_points: Sequence[float] | None = None,
    disposition_map: Mapping[int, str] | str | None = None,
    sdoh_fallback: str = "indicator",
    address_table: str | None = "auto",
    address_tie_break: str = "deterministic",
    address_undated: str = "last",
    impute: str | None = None,
    impute_values: Mapping[str, float] | None = None,
    vocabulary_db: str | os.PathLike | None = "auto",
    feature_set: Any = None,
    feature_fit: bool | Sequence[str] = False,
    bmi_fill: str | None = None,
    bmi_lookback_days: int = 365,
) -> pd.DataFrame:
    """Leakage-free modelling table, one row per cohort row, in cohort order.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        OMOP CDM v5.4 database. Read-only is fine; nothing is written.
    cohort_table : str, default "cohort"
        Table / view with ``subject_id`` (or ``person_id``), ``cohort_start_date`` (admission) and ``cohort_end_date``
        (discharge); optional ``visit_occurrence_id``, ``outcome_flag``, ``discharged_to_concept_id``, ``site`` /
        ``site_anon``. A ``define_study_cohort()`` result persisted with ``table_name=`` has all of them.
    include_sdoh : bool, default False
        SDI quintile and RUCA category of the admission ZIP (:func:`derive_geography_features`); needs ``ruca_crosswalk``,
        ``sdi_table`` and the frozen ``sdi_cut_points``. Off by default because it needs those external files.
        ``sdoh_fallback="indicator"`` (default): when any of the three is missing the group becomes ``sdi_quintile`` /
        ``ruca_category`` = NaN plus ``sdoh_missing`` = 1 and a warning, instead of an error; ``"raise"`` keeps the error.
        No national cut points are bundled: quintiles must use the training cohort's frozen cut points.
    address_table : str or None, default "auto"
        Dated address history used for ``include_sdoh`` (columns ``person_id``, ``zip5``, ``address_start``, ``address_end``,
        ``preferred``): ``"auto"`` uses ``person_address_history`` when it exists, else ``person.location_id``; pass a table
        name (for example a study's own ``ederri_address_history``) to use another one, ``None`` for ``person.location_id``.
    address_undated : {"last", "first"}, default "last"
        Rank of an address with no start date (see :func:`omop_etl.geography.derive_geography_features`).
    address_tie_break : {"deterministic", "highest_zip", "ambiguous_missing", "arbitrary", "first_listed"}, default "deterministic"
        Which address wins when several tie on every rule key (see :func:`derive_geography_features`).
    impute : {"median", "mean"} or None, default None
        Fills missing vital and lab values (``vital_*``, ``lab_*``, ``lab_*_last``) for models that cannot take NaN. The
        statistics are the ones of **this** table unless ``impute_values`` ({column: value}) is given, so fit them on the
        training sites (``attrs["imputation_values"]`` returns them) and pass them to every other table. Tree models
        handle NaN natively and need no imputation.
    vocabulary_db : path, "auto" or None, default "auto"
        When ``concept_ancestor`` is not reachable, attach this central vocabulary DuckDB file (temporary zero-copy views,
        :func:`attach_central_vocabulary`). ``"auto"`` looks for ``central_vocabulary.duckdb`` / ``central_vocab.duckdb`` next
        to the connection's database file; ``None`` never attaches.
    include_elixhauser : bool
        31 Elixhauser flags and the van Walraven score. Anchored at **discharge** and including the index stay's
        diagnoses ("discharge-inclusive lookback", ``lookback_days`` back from the discharge date).
    include_medications : bool
        Exposure flags for 8 drug classes (``DEFAULT_MEDICATION_CONCEPTS`` unless ``medication_specs`` is given),
        window ``medication_window``: ``"pre_admission"`` (default; the ``lookback_days`` before the admission), ``"pre_discharge"``
        (the ``lookback_days`` up to and including the discharge date, as the eDERRI protocol does) or ``"inpatient"`` /
        ``"discharge"`` (see :func:`omop_etl.medication_classes.extract_medication_classes`).
    include_vitals, include_labs : bool
        Index-admission mean ``vital_<name>`` / ``lab_<name>`` **and** the latest in-stay value ``lab_<name>_last`` over
        ``[admission - lab_lookback_days, discharge]``. LOINC codes are resolved through ``concept``; pass
        ``lab_concept_ids`` / ``vital_concept_ids`` ``{name: [concept_id]}`` for databases without the vocabulary.
    bmi_fill : {"derive", "derive_lookback", "none"} or None
        How a stay with no BMI recorded during the stay gets one (``vital_bmi``): ``"derive"`` computes it from the stay's mean
        height and weight (kg / m^2; :data:`BMI_HEIGHT_CONCEPTS`, :data:`BMI_WEIGHT_CONCEPTS`); ``"derive_lookback"`` also takes,
        where that finds none, the latest BMI recorded in the ``bmi_lookback_days`` before the admission, then one computed from the
        latest height and weight in that window; ``"none"`` leaves it missing. Default: ``"derive_lookback"`` with
        ``profile="ederri"`` or ``"both"`` (as the legacy study pipeline did), else ``"derive"``. ``attrs["bmi_filled"]`` counts the
        stays each step filled.
    bmi_lookback_days : int, default 365
        Window before the admission used by ``bmi_fill="derive_lookback"``.
    derive_labs : bool, default True
        Adds ``lab_egfr`` (CKD-EPI 2021) and fills ``lab_anion_gap`` (Na - Cl - HCO3) where no anion gap was measured, each for the
        mean and ``_last``.
    bounds : "default", mapping or None, default "default"
        Physiologic plausibility bounds applied to every single vital / lab measurement before the stay's mean and latest value
        are taken: values outside ``(min, max)`` are dropped, not clipped. ``"default"`` uses :data:`DEFAULT_BOUNDS` (conventional
        US units); a mapping ``{name: (min, max)}`` overrides those names (``None`` as a limit leaves that side open); ``None`` turns
        bounds off. WBC counts reported per uL (200 to 200000) are rescaled to 10^3/uL first (:data:`DEFAULT_RESCALE`, off with
        ``bounds=None``).
    profile : {"ederri", "both", None}, default None
        Where the eDERRI protocol's definitions differ from the generic ones, choose which to use. ``None``: generic only.
        ``"ederri"``: the protocol's only: its ATC medication classes (:data:`EDERRI_MEDICATION_SPECS`) over
        [discharge - ``lookback_days``, discharge], its Elixhauser ICD prefix lists over all history up to discharge, hemoglobin
        filled from hematocrit / 3, and the protocol's history columns (``include_protocol_history``). ``"both"``: the generic
        columns as usual **plus** the protocol's Elixhauser flags and medication classes under an ``ederri_`` prefix
        (``ederri_elix_*``, ``ederri_med_*``) and the history columns, so the two definitions can be compared or used side by
        side. Each choice can be set on its own with ``elixhauser_code_table``, ``medication_set``, ``medication_window``,
        ``hematocrit_fallback`` and ``include_protocol_history``; an explicit argument always wins over the profile. ``profile="ederri"`` also classifies the discharge disposition by the protocol's rule (concept sets or the source value HO / HH / SN / RH / AM; ``disposition_map="ederri"``), because the default concept map matches nothing at sites that store the disposition as a source value.
    hematocrit_fallback : bool or None
        Fill a missing hemoglobin with hematocrit / 3. Default: off, on with ``profile="ederri"``.
    elixhauser_lookback_days : int, None or "lookback_days"
        History window of the Elixhauser flags (up to and including the discharge date). ``"lookback_days"`` (default) uses
        ``lookback_days``; ``None`` is all history, which is what ``profile="ederri"`` uses.
    elixhauser_code_table : {"quan", "ederri", "both", None}
        ICD prefix table for the 31 flags (:func:`omop_etl.comorbidity.extract_elixhauser_comorbidities` ``code_table``).
        ``"both"`` returns the Quan flags as ``elix_*`` and the protocol's as ``ederri_elix_*``. Default: from ``profile``.
    medication_set : {"generic", "ederri", "both", None}
        Medication classes: the generic ``medication_specs`` (``DEFAULT_MEDICATION_CONCEPTS``), the protocol's
        :data:`EDERRI_MEDICATION_SPECS` over [discharge - ``lookback_days``, discharge], or both (the protocol's as
        ``ederri_med_*``). Default: from ``profile``. An explicit ``medication_specs`` replaces the generic classes.
    include_protocol_history : bool or None
        Adds the protocol's history columns (:func:`extract_protocol_history_features`): ``prior_encounters_all_count``,
        ``prior_ip_os_ed_count``, ``days_since_prior_encounter``, ``days_since_prior_ip_ed``,
        ``readmission_count_twelve_months``, ``prior_outpatient_count``, ``n_secondary_dx``, ``elix_pre_index_conditions_count`` and ``tobacco``.
        Default: on with ``profile="ederri"`` or ``"both"``.
    include_utilization : bool
        Prior IP / ED / OS counts, length of stay, days since last encounter, prior readmissions in ``lookback_days``
        (strictly before the admission).
    include_disposition : bool
        One-hot ``disp_home``, ``disp_home_health``, ``disp_snf``, ``disp_ama``, ``disp_other``, ``disp_missing`` from
        ``discharged_to_concept_id`` (cohort column, else the stay's ``visit_occurrence``).

    Returns
    -------
    pandas.DataFrame
        Key columns (``person_id``, ``visit_occurrence_id`` when present, ``index_date``, ``discharge_date``, ``y`` when the
        cohort has ``outcome_flag``, ``site`` when present) followed by the requested feature columns.
        ``attrs["feature_groups"]`` maps each group to its columns; ``attrs["skipped"]`` lists features that could not be
        built (for example a lab with no concept in the database).
    """
    if lookback_days < 0 or lab_lookback_days < 0:
        raise ValueError("lookback_days and lab_lookback_days must be non-negative.")
    if profile not in (None, "ederri", "both"):
        raise ValueError("profile must be 'ederri', 'both' or None.")
    if medication_set is None:
        medication_set = {"ederri": "ederri", "both": "both"}.get(profile, "generic")
    if medication_set not in ("generic", "ederri", "both"):
        raise ValueError("medication_set must be 'generic', 'ederri' or 'both'.")
    if medication_window is None:
        medication_window = "pre_discharge" if medication_set == "ederri" else "pre_admission"
    if hematocrit_fallback is None:
        hematocrit_fallback = profile == "ederri"
    if bmi_fill is None:
        bmi_fill = "derive_lookback" if profile in ("ederri", "both") else "derive"
    if bmi_fill not in BMI_FILL_OPTIONS:
        raise ValueError(f"bmi_fill must be one of {BMI_FILL_OPTIONS}, got {bmi_fill!r}.")
    if bmi_lookback_days < 0:
        raise ValueError("bmi_lookback_days must be non-negative.")
    bmi_filled: dict[str, int] = {}
    if elixhauser_code_table is None:
        elixhauser_code_table = {"ederri": "ederri", "both": "both"}.get(profile, "quan")
    if elixhauser_code_table not in ("quan", "ederri", "both"):
        raise ValueError("elixhauser_code_table must be 'quan', 'ederri' or 'both'.")
    if elixhauser_lookback_days != "lookback_days" and elixhauser_lookback_days is not None and elixhauser_lookback_days < 0:
        raise ValueError("elixhauser_lookback_days must be non-negative or None.")
    if include_protocol_history is None:
        include_protocol_history = profile in ("ederri", "both")
    if isinstance(bounds, str):
        if bounds != "default":
            raise ValueError("bounds must be 'default', a mapping {name: (min, max)} or None.")
        bounds = DEFAULT_BOUNDS
    elif bounds is not None:
        bounds = {**DEFAULT_BOUNDS, **{k: tuple(v) for k, v in bounds.items()}}
    rescale = DEFAULT_RESCALE if bounds is not None else None
    cohort = _cohort_frame(con, cohort_table, cohort_id)
    n = len(cohort)
    out = pd.DataFrame({"person_id": cohort["subject_id"].to_numpy()})
    if "visit_occurrence_id" in cohort:
        out["visit_occurrence_id"] = cohort["visit_occurrence_id"].to_numpy()
    out["index_date"] = cohort["cohort_start_date"].to_numpy()
    out["discharge_date"] = cohort["cohort_end_date"].to_numpy()
    if "outcome_flag" in cohort:
        out["y"] = cohort["outcome_flag"].to_numpy()
    for site in ("site", "site_anon"):
        if site in cohort:
            out[site] = cohort[site].to_numpy()
    keys = list(out.columns)
    groups: dict[str, list[str]] = {}
    skipped: list[str] = []
    address_used: str | None = None
    geography: dict[str, Any] | None = None

    blocks: list[pd.DataFrame] = []     # joined once at the end: 100+ single-column inserts fragment a DataFrame

    def add(group: str, frame: pd.DataFrame) -> None:
        blocks.append(pd.DataFrame({c: frame[c].to_numpy() for c in frame.columns}, index=out.index))
        groups.setdefault(group, []).extend(frame.columns)

    if include_elixhauser or include_medications:
        _ensure_vocabulary(con, vocabulary_db)

    age = female = None
    if include_demographics or (include_labs and derive_labs):
        con.register("_bf_c", cohort[["row_id", "subject_id", "cohort_start_date"]])
        try:
            demo = con.execute("""
                SELECT c.row_id, date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)),
                                           CAST(c.cohort_start_date AS DATE)) AS age_at_index,
                       p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id
                FROM _bf_c c LEFT JOIN person p ON p.person_id = c.subject_id ORDER BY c.row_id""").df()
        finally:
            con.unregister("_bf_c")
        age = demo["age_at_index"].astype("float64")
        female = (demo["gender_concept_id"] == 8532).where(demo["gender_concept_id"].notna())
        if include_demographics:
            d = demo.drop(columns=["row_id"]).copy()
            d["female"] = female.astype("float64").to_numpy()
            add("demographics", d[["age_at_index", "female", "race_concept_id", "ethnicity_concept_id"]])

    # The history extractors read a normalized temp view, so any cohort column naming works.
    view = "_bf_cohort_view"
    con.register(view, cohort.drop(columns=["row_id"]))
    try:
        if include_elixhauser:
            from omop_etl.comorbidity import extract_elixhauser_comorbidities
            tables = ["quan", "ederri"] if elixhauser_code_table == "both" else [elixhauser_code_table]
            for table in tables:
                lb = (None if table == "ederri" else lookback_days) if elixhauser_lookback_days == "lookback_days" else elixhauser_lookback_days
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    el = extract_elixhauser_comorbidities(con, view, person_col="subject_id", index_date_col="cohort_end_date",
                                                          lookback_days=lb, exclude_index_diagnoses=False, end_inclusive=True,
                                                          code_table=table, source_and_standard=table != "ederri")
                prefix = "ederri_" if elixhauser_code_table == "both" and table == "ederri" else ""
                add("elixhauser" if not prefix else "elixhauser_ederri", _align(cohort, el, prefix=prefix))
        if include_utilization:
            from omop_etl.utilization import extract_prior_utilization
            ut = extract_prior_utilization(con, view, lookback_days=lookback_days)
            add("utilization", _align(cohort, ut))
        if include_medications:
            from omop_etl.medication_classes import extract_medication_classes
            runs = []
            if medication_set in ("generic", "both"):
                runs.append((dict(medication_specs) if medication_specs else _default_medication_specs(), medication_window, ""))
            if medication_set in ("ederri", "both"):
                if medication_set == "ederri" and medication_specs:
                    runs.append((dict(medication_specs), medication_window, ""))
                else:
                    runs.append((dict(EDERRI_MEDICATION_SPECS), "pre_discharge" if medication_set == "both" else medication_window,
                                 "ederri_" if medication_set == "both" else ""))
            for specs, window, prefix in runs:
                if window == "pre_discharge":
                    window_kw = dict(window=None, offset_anchor="visit_end_date", lookahead_days=0)
                else:
                    window_kw = dict(window=window)
                with warnings.catch_warnings(record=True) as med_warnings:
                    warnings.simplefilter("always", UserWarning)
                    med = extract_medication_classes(con, view, specs, strategy="standard", person_col="subject_id",
                                                     index_date_col="cohort_start_date", end_date_col="cohort_end_date",
                                                     lookback_days=lookback_days, **window_kw)
                # a class that resolves to no concept is an all-zero column: keep the reason (it used to be silenced)
                for w in sorted({str(w.message) for w in med_warnings if issubclass(w.category, UserWarning)}):
                    cls = re.search(r"Medication class '([^']+)'", w)
                    warnings.warn(DataGapWarning(f"medication classes: {w}", "feature_skipped",
                                                 f"{prefix}med_{cls.group(1)}" if cls else None), stacklevel=2)
                add("medications" if not prefix else "medications_ederri", _align(cohort, med, prefix=prefix + "med_", only=list(specs)))
        if include_protocol_history:
            try:
                add("protocol_history", extract_protocol_history_features(con, cohort, lookback_days=lookback_days))
            except duckdb.CatalogException as exc:     # a CDM without visit_occurrence / condition_occurrence / observation
                warnings.warn(DataGapWarning(f"protocol history columns skipped: {str(exc).splitlines()[0]}", "group_skipped"), stacklevel=2)
                skipped.append("protocol_history")
    finally:
        con.unregister(view)

    if include_vitals:
        vmap = _concept_map(con, vital_loincs, vital_concept_ids, None if vital_loincs else BENCHMARK_VITAL_CONCEPT_SETS)
        stats = _measurement_stats(con, cohort, vmap, lab_lookback_days, bounds, rescale)
        names = list(vital_loincs or BENCHMARK_VITAL_LOINCS)
        skipped += [f"vital_{x}" for x in names if x not in set(vmap["name"])]
        vit = _wide(stats, "vital_", [x for x in names if x in set(vmap["name"])], n, with_last=False)
        if bmi_fill != "none" and "vital_bmi" in vit:
            bmi_bounds = (bounds or {}).get("bmi") if bounds is not None else None
            vit["vital_bmi"], bmi_filled = _fill_bmi(con, cohort, vmap.loc[vmap["name"] == "bmi", "concept_id"].tolist(), vit["vital_bmi"],
                                                     mode=bmi_fill, lookback_days=bmi_lookback_days, bounds=bmi_bounds)
        add("vitals", vit.drop(columns=["row_id"]))
    if include_labs:
        loincs = dict(lab_loincs or BENCHMARK_LAB_LOINCS)
        lab_sets = None if lab_loincs else BENCHMARK_LAB_CONCEPT_SETS
        lmap = _concept_map(con, loincs, lab_concept_ids, lab_sets)
        _check_lab_coverage(con, lab_sets or {n: concept_set_from_codes(c) for n, c in loincs.items() if c and n not in (lab_concept_ids or {})},
                            cohort, lab_lookback_days)
        stats = _measurement_stats(con, cohort, lmap, lab_lookback_days, bounds, rescale)
        present = [x for x in loincs if x in set(lmap["name"])]
        skipped += [f"lab_{x}" for x in loincs if x not in present]
        labs = _wide(stats, "lab_", present, n, with_last=True).drop(columns=["row_id"])
        cols = list(labs.columns)
        if derive_labs:
            cols += _derive_labs(labs, age if age is not None else pd.Series(np.nan, index=labs.index),
                                 female if female is not None else pd.Series(np.nan, index=labs.index),
                                 hematocrit_fallback=bool(hematocrit_fallback))
        add("labs", labs[cols])

    if include_disposition:
        disp = cohort["discharged_to_concept_id"] if "discharged_to_concept_id" in cohort else None
        if disp is None and "visit_occurrence_id" in cohort:
            disp = pd.Series(con.execute("SELECT v.discharged_to_concept_id FROM (SELECT unnest(?) AS vid, generate_subscripts(?, 1) AS ord) k "
                                         "LEFT JOIN visit_occurrence v ON v.visit_occurrence_id = k.vid ORDER BY k.ord",
                                         [cohort["visit_occurrence_id"].tolist()] * 2).df().iloc[:, 0].to_numpy())
        rule = disposition_map if isinstance(disposition_map, str) else ("ederri" if (disposition_map is None and profile == "ederri") else None)
        if rule not in (None, "ederri", "generic"):
            raise ValueError("disposition_map must be a {concept_id: level} mapping, 'ederri', 'generic' or None.")
        mapping = {int(k): v for k, v in ((disposition_map if isinstance(disposition_map, Mapping) else None) or DEFAULT_DISPOSITION_MAP).items()}
        if disp is None:
            skipped.append("disposition")
        elif rule == "ederri":
            # the protocol's rule: concept sets OR the source value, anything else is other / unknown
            src = pd.Series([None] * len(cohort), dtype=object)
            if "visit_occurrence_id" in cohort:
                try:
                    src = pd.Series(con.execute("SELECT upper(trim(v.discharged_to_source_value)) FROM (SELECT unnest(?) AS vid, generate_subscripts(?, 1) AS ord) k "
                                                "LEFT JOIN visit_occurrence v ON v.visit_occurrence_id = k.vid ORDER BY k.ord",
                                                [cohort["visit_occurrence_id"].tolist()] * 2).df().iloc[:, 0].to_numpy(), dtype=object)
                except duckdb.Error as exc:  # no source value column: concept ids only
                    warnings.warn(f"disposition: discharged_to_source_value could not be read ({str(exc).splitlines()[0]}); the "
                                  "eDERRI disposition uses concept ids only.", UserWarning, stacklevel=2)
            d = disp.reset_index(drop=True)

            cat = pd.Series([ederri_disposition_level(c, sv) for c, sv in zip(d, src)])
            add("disposition", pd.DataFrame({f"disp_{lv}": (cat == lv).astype("int8").to_numpy() for lv in _DISPOSITION_LEVELS}))
        else:
            cat = disp.map(lambda c: "missing" if pd.isna(c) else mapping.get(int(c), "other"))
            levels = list(dict.fromkeys(list(_DISPOSITION_LEVELS) + sorted(set(mapping.values()) - set(_DISPOSITION_LEVELS))))
            add("disposition", pd.DataFrame({f"disp_{lv}": (cat == lv).astype("int8").to_numpy() for lv in levels}))

    if include_sdoh:
        if sdoh_fallback not in ("indicator", "raise"):
            raise ValueError("sdoh_fallback must be 'indicator' or 'raise'.")
        if ruca_crosswalk is None or sdi_table is None or sdi_cut_points is None:
            if sdoh_fallback == "raise":
                raise ValueError("include_sdoh=True needs ruca_crosswalk, sdi_table and the frozen sdi_cut_points.")
            warnings.warn(DataGapWarning("include_sdoh=True but ruca_crosswalk, sdi_table or sdi_cut_points is missing: sdi_quintile and "
                                         "ruca_category are NaN and sdoh_missing = 1 for every row.", "sdoh"), stacklevel=2)
            add("sdoh", pd.DataFrame({"sdi_quintile": np.full(n, np.nan), "ruca_category": np.full(n, np.nan, dtype=object),
                                      "sdoh_missing": np.ones(n, dtype="int8")}))
        else:
            from omop_etl.geography import SDI_MISSING, derive_geography_features
            stays = pd.DataFrame({"person_id": cohort["subject_id"], "stay_id": cohort["row_id"], "admit_date": cohort["cohort_start_date"]})
            geo = derive_geography_features(con, ruca_crosswalk, sdi_table, sdi_cut_points, stays=stays, address_table=address_table,
                                         tie_break=address_tie_break, undated=address_undated)
            if geo["zip5"].isna().all():
                warnings.warn(DataGapWarning(
                    f"include_sdoh: no ZIP code was found for any stay (address source: {geo.attrs.get('address_table')}), so "
                    "sdi_quintile and ruca_category are all missing. Pass address_table= the name of your dated address table.", "sdoh"),
                    stacklevel=2)
            address_used = geo.attrs.get("address_table")
            has_zip = geo["zip5"].notna()
            n_zip = int(has_zip.sum())
            geography = {"address_table": address_used, "share_with_zip": round(float(has_zip.mean()), 4) if len(geo) else None,
                         "rule_shares": {str(k): round(float(v), 4) for k, v in geo["rule"].value_counts(normalize=True, dropna=False).items()},
                         "zip_not_in_sdi": round(float((has_zip & geo["sdi_score"].isna()).sum() / n_zip), 4) if n_zip else None,
                         "zip_not_in_ruca": round(float((has_zip & geo["ruca_code"].isna()).sum() / n_zip), 4) if n_zip else None}
            geo = geo.drop_duplicates("stay_id").set_index("stay_id").reindex(cohort["row_id"])
            q = geo["sdi_quintile"].to_numpy()
            add("sdoh", pd.DataFrame({"sdi_quintile": q, "ruca_category": geo["ruca_category"].to_numpy(),
                                      "sdoh_missing": (pd.isna(q) | (q == SDI_MISSING)).astype("int8")}))

    if feature_set is not None:
        # study-design features (ACS income, concept-set labs, RUCA/SDI ...) computed for the same stays, appended as one group
        stays = pd.DataFrame({"person_id": cohort["subject_id"].to_numpy(), "stay_id": cohort["row_id"].to_numpy(),
                              "admit_date": cohort["cohort_start_date"].to_numpy()})
        sf = feature_set.compute(con, stays, fit=feature_fit)
        sf = sf.set_index("stay_id").reindex(cohort["row_id"].to_numpy())
        clash = [c for c in feature_set.columns if c in out.columns or any(c in b.columns for b in blocks)]
        if clash:
            raise ValueError(f"feature_set columns {clash} already exist in the benchmark table; rename them.")
        add("study_features", sf[feature_set.columns].reset_index(drop=True))
        feature_attrs = {"feature_state": sf.attrs.get("feature_state"), "feature_provenance": sf.attrs.get("feature_provenance")}
    else:
        feature_attrs = {}
    out = pd.concat([out, *blocks], axis=1)
    out.attrs.update(feature_attrs)
    imputation: dict[str, float] = {}
    if impute is not None or impute_values is not None:
        method = (impute or "median").lower()
        if method not in ("median", "mean"):
            raise ValueError("impute must be 'median', 'mean' or None.")
        for col in groups.get("vitals", []) + groups.get("labs", []):
            val = float(impute_values[col]) if impute_values is not None and col in impute_values else                 float(getattr(out[col], method)()) if out[col].notna().any() else np.nan
            imputation[col] = val
            if not np.isnan(val):
                out[col] = out[col].fillna(val)

    leaked = [c for c in out.columns if c not in keys and c.lower() in ("y", "outcome", "outcome_flag", "outcome_date")]
    if leaked:
        raise RuntimeError(f"Outcome-like columns in the feature table: {leaked}.")
    out.attrs["feature_groups"] = groups
    out.attrs["skipped"] = skipped
    out.attrs["key_columns"] = keys
    out.attrs["imputation_values"] = imputation
    out.attrs["bmi_filled"] = bmi_filled
    out.attrs["address_table"] = address_used
    out.attrs["geography"] = geography
    return out


#: PCORnet VITAL.SMOKING as loaded by the converter: observation 43054909 (LOINC 72166-2) with the LOINC answer concept, and the
#: PCORnet code in value_source_value (0.34.1). Current: 01, 02, 05, 07, 08; former: 03; never: 04 (06 "unknown if ever" is no status).
SMOKING_CURRENT = {"01": 45881517, "02": 45884037, "05": 45881518, "07": 45884038, "08": 45878118}
SMOKING_FORMER = {"03": 45883458}
SMOKING_NEVER = {"04": 45879404}
TOBACCO_UNKNOWN = "Missing / Unknown"
#: SNOMED "never smoked tobacco" (4144272) and "non-smoker" (4052039) observations
NEVER_SMOKER_CONCEPTS = (4144272, 4052039)
#: a diagnosis code without dots or case, for the tobacco ICD codes
_TOB_DX = "upper(replace(trim(co.condition_source_value), '.', ''))"


def _smoking_is(codes: Mapping[str, int], cols: set[str] | None = None) -> str:
    """SQL: the observation row ``o`` is a VITAL smoking status in ``codes`` (by answer concept, else by the PCORnet code).
    ``cols`` are the observation table's columns; a column it lacks reads as NULL."""
    ids = ", ".join(str(v) for v in codes.values())
    pc = ", ".join(f"'{k}'" for k in codes)
    vc = "o.value_as_concept_id" if cols is None or "value_as_concept_id" in cols else "CAST(NULL AS INTEGER)"
    vs = "o.value_source_value" if cols is None or "value_source_value" in cols else "CAST(NULL AS VARCHAR)"
    return (f"(o.observation_concept_id = 43054909 AND ({vc} IN ({ids}) OR (COALESCE({vc}, 0) = 0 "
            f"AND lpad(trim(COALESCE({vs}, '')), 2, '0') IN ({pc}))))")


def extract_protocol_history_features(con: duckdb.DuckDBPyConnection, cohort: pd.DataFrame, *, lookback_days: int = 365) -> pd.DataFrame:
    """The eDERRI protocol's history columns, one row per cohort row in cohort order (what the study's pipeline computed).

    ``cohort`` is the frame :func:`extract_benchmark_features` builds (``row_id``, ``subject_id``, ``cohort_start_date``,
    ``cohort_end_date``). Windows follow the protocol: visits in ``[admission - lookback_days, admission)``; diagnoses and
    observations dated up to the discharge date.

    * ``prior_encounters_all_count`` - all prior visits; ``prior_ip_os_ed_count`` - prior inpatient (9201, 262), ED (9203) and
      observation (581385; 0.34.1, before only the custom concept 2000000048) visits; ``prior_outpatient_count`` - prior outpatient (9202, 581477) and telehealth (5083) visits only
      (0.34.0; "all minus inpatient / ED" also counted visits of unknown or other type);
    * ``days_since_prior_encounter`` / ``days_since_prior_ip_ed`` - days from the latest such prior visit to the admission
      (``lookback_days`` when there is none; for the second, inpatient, observation and ED visits only, as ``prior_ip_os_ed_count``
      counts them - observation stays since 0.34.1);
    * ``readmission_count_twelve_months`` - inpatient visits in the 365 days before the admission;
    * ``n_secondary_dx`` - secondary diagnoses of the index stay (0.34.0): distinct diagnosis codes as recorded (ICD-9-CM /
      ICD-10-CM source value, dots and case ignored; the standard concept when there is none) dated from admission to discharge,
      leaving out the code(s) of the principal diagnosis (PCORnet PDX = "P", or a primary condition type / status concept), even
      when the same code is also recorded as a secondary diagnosis. Until 0.33.7 the
      column was ``n_secondary_dx_ccs`` and counted distinct standard concepts with the principal diagnosis, so one ICD code that
      maps to two SNOMED concepts counted twice;
    * ``elix_pre_index_conditions_count`` - distinct condition concepts dated before the admission (all history);
    * ``tobacco`` - ``"Current user"`` (tobacco observation concepts 4005823 / 4276526, or ICD F17, Z72.0, 305.1), ``"Former user"``
      (4310280, Z87.891, V15.82), ``"Never user"`` (a recorded never-smoker status: VITAL code 04 or concepts 4144272 / 4052039), else ``"Missing / Unknown"``. The smoking
      status of PCORnet VITAL (observation 43054909, LOINC 72166-2, with its answer concept or the PCORnet code) counts too, and
      the ICD codes match with or without dots (0.34.1). Until 0.34.0 a stay with no status was "Never user", and VITAL was not read.
    """
    if lookback_days < 0:
        raise ValueError("lookback_days must be non-negative.")
    lb = int(lookback_days)
    con.register("_bf_ph", cohort[["row_id", "subject_id", "cohort_start_date", "cohort_end_date"]])
    try:
        util = con.execute(f"""
            SELECT c.row_id,
                   COUNT(pv.visit_occurrence_id) AS prior_encounters_all_count,
                   COUNT(CASE WHEN pv.visit_concept_id IN (9201, 9203, 262, 581385, 2000000048) THEN 1 END) AS prior_ip_os_ed_count,
                   COUNT(CASE WHEN pv.visit_concept_id IN ({_OUTPATIENT_SQL}) THEN 1 END) AS prior_outpatient_count,
                   COALESCE(date_diff('day', MAX(pv.visit_start_date), CAST(c.cohort_start_date AS DATE)), {lb}) AS days_since_prior_encounter,
                   COALESCE(date_diff('day', MAX(CASE WHEN pv.visit_concept_id IN (9201, 9203, 262, 581385, 2000000048) THEN pv.visit_start_date END),
                                      CAST(c.cohort_start_date AS DATE)), {lb}) AS days_since_prior_ip_ed,
                   COUNT(CASE WHEN pv.visit_concept_id IN (9201, 262)
                               AND pv.visit_start_date >= CAST(c.cohort_start_date AS DATE) - 365 THEN 1 END) AS readmission_count_twelve_months
            FROM _bf_ph c
            LEFT JOIN visit_occurrence pv ON pv.person_id = c.subject_id
                 AND pv.visit_start_date < CAST(c.cohort_start_date AS DATE)
                 AND pv.visit_start_date >= CAST(c.cohort_start_date AS DATE) - {lb}
            GROUP BY c.row_id, c.cohort_start_date ORDER BY c.row_id""").df()
        principal = _principal_sql(con)
        dx = con.execute(f"""
            SELECT c.row_id,
                   COUNT(DISTINCT CASE WHEN co.condition_start_date >= CAST(c.cohort_start_date AS DATE) THEN {_DX_CODE_SQL} END)
                   - COUNT(DISTINCT CASE WHEN co.condition_start_date >= CAST(c.cohort_start_date AS DATE) AND {principal}
                                         THEN {_DX_CODE_SQL} END) AS n_secondary_dx,
                   COUNT(DISTINCT CASE WHEN co.condition_start_date < CAST(c.cohort_start_date AS DATE) THEN co.condition_concept_id END)
                       AS elix_pre_index_conditions_count,
                   MAX(CASE WHEN {_TOB_DX} LIKE 'F17%' OR {_TOB_DX} LIKE 'Z720%' OR {_TOB_DX} LIKE '3051%' THEN 1 ELSE 0 END) AS tob_cur_dx,
                   MAX(CASE WHEN {_TOB_DX} LIKE 'Z87891%' OR {_TOB_DX} LIKE 'V1582%' THEN 1 ELSE 0 END) AS tob_former_dx
            FROM _bf_ph c
            LEFT JOIN condition_occurrence co ON co.person_id = c.subject_id
                 AND co.condition_start_date <= CAST(c.cohort_end_date AS DATE)
            GROUP BY c.row_id ORDER BY c.row_id""").df()
        ocols = {r[0].lower() for r in con.execute("DESCRIBE observation").fetchall()}
        obs = con.execute(f"""
            SELECT c.row_id,
                   MAX(CASE WHEN o.observation_concept_id IN (4005823, 4276526) OR {_smoking_is(SMOKING_CURRENT, ocols)} THEN 1 ELSE 0 END) AS tob_cur_obs,
                   MAX(CASE WHEN o.observation_concept_id = 4310280 OR {_smoking_is(SMOKING_FORMER, ocols)} THEN 1 ELSE 0 END) AS tob_former_obs,
                   MAX(CASE WHEN o.observation_concept_id IN ({", ".join(map(str, NEVER_SMOKER_CONCEPTS))})
                             OR {_smoking_is(SMOKING_NEVER, ocols)} THEN 1 ELSE 0 END) AS tob_never_obs
            FROM _bf_ph c
            LEFT JOIN observation o ON o.person_id = c.subject_id AND o.observation_date <= CAST(c.cohort_end_date AS DATE)
            GROUP BY c.row_id ORDER BY c.row_id""").df()
    finally:
        con.unregister("_bf_ph")
    cur = (dx["tob_cur_dx"].to_numpy() == 1) | (obs["tob_cur_obs"].to_numpy() == 1)
    former = (dx["tob_former_dx"].to_numpy() == 1) | (obs["tob_former_obs"].to_numpy() == 1)
    out = util.drop(columns=["row_id"]).copy()
    out["n_secondary_dx"] = dx["n_secondary_dx"].to_numpy()
    out["elix_pre_index_conditions_count"] = dx["elix_pre_index_conditions_count"].to_numpy()
    never = obs["tob_never_obs"].to_numpy() == 1
    out["tobacco"] = np.where(cur, "Current user", np.where(former, "Former user", np.where(never, "Never user", TOBACCO_UNKNOWN)))
    return out


def _align(cohort: pd.DataFrame, df: pd.DataFrame, prefix: str = "", only: Sequence[str] | None = None,
           drop: Sequence[str] = ()) -> pd.DataFrame:
    """Reorder an extractor result (one row per distinct person x index date) to the cohort's row order."""
    date_col = next((c for c in ("cohort_start_date", "cohort_end_date", "index_date") if c in df.columns), None)
    person = "subject_id" if "subject_id" in df.columns else "person_id"
    if date_col is None:
        raise RuntimeError(f"Cannot align extractor output without a date column: {list(df.columns)}")
    key_date = "cohort_end_date" if date_col == "cohort_end_date" else "cohort_start_date"
    d = df.copy()
    d[date_col] = pd.to_datetime(d[date_col])
    d = d.drop_duplicates([person, date_col])
    merged = cohort[["row_id", "subject_id", key_date]].merge(
        d, left_on=["subject_id", key_date], right_on=[person, date_col], how="left", suffixes=("", "_x")).sort_values("row_id")
    value_cols = [c for c in d.columns if c not in {person, date_col, "cohort_definition_id", "site", "site_anon", *drop}]
    if only is not None:
        value_cols = [c for c in value_cols if c in set(only)]
    res = merged[value_cols].reset_index(drop=True)
    return res.add_prefix(prefix) if prefix else res
