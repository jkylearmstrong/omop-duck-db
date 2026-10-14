"""Pluggable physiologic bounding (``filter_bounds``) and unit harmonization for measurements.

* :data:`DEFAULT_PHYSIOLOGIC_BOUNDS` - overrideable plausibility bounds per feature.
* :func:`filter_bounds` - applies them to a wide (one column per feature) or long (feature + value column)
  DataFrame; ``bounds=False`` disables, ``bounds={"wbc": [0.5, 150]}`` overrides single features.
* :func:`harmonize_measurement_units` - a preflight step that inspects ``unit_concept_id`` /
  ``unit_source_value`` and builds a view whose ``value_as_number`` is scaled to the canonical unit
  (e.g. WBC cells/uL -> 10^3/uL, creatinine umol/L -> mg/dL).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

__all__ = [
    "DEFAULT_PHYSIOLOGIC_BOUNDS",
    "POPULATION_PHYSIOLOGIC_BOUNDS",
    "DEFAULT_UNIT_RULES",
    "resolve_bounds",
    "filter_bounds",
    "harmonize_measurement_units",
    "harmonize_anthropometrics",
]

POPULATION_PHYSIOLOGIC_BOUNDS: dict[str, dict[str, list[float]]] = {
    "adult": {
        "wbc": [0.1, 200.0],           # 10^3/uL
        "creatinine": [0.1, 30.0],     # mg/dL
        "sodium": [100.0, 180.0],      # mEq/L
        "potassium": [1.0, 10.0],      # mEq/L
        "hemoglobin": [2.0, 25.0],     # g/dL
        "glucose": [10.0, 2000.0],     # mg/dL
        "systolic_bp": [40.0, 300.0],  # mmHg
        "diastolic_bp": [20.0, 200.0], # mmHg
        "bmi": [10.0, 120.0],          # kg/m^2
        "heart_rate": [20.0, 250.0],   # bpm
        "respiratory_rate": [4.0, 60.0]# breaths/min
    },
    "pediatric": {
        "wbc": [1.0, 150.0],           # 10^3/uL
        "creatinine": [0.1, 15.0],     # mg/dL
        "sodium": [110.0, 175.0],      # mEq/L
        "potassium": [1.5, 9.0],       # mEq/L
        "hemoglobin": [3.0, 24.0],     # g/dL
        "glucose": [15.0, 1500.0],     # mg/dL
        "systolic_bp": [50.0, 220.0],  # mmHg
        "diastolic_bp": [25.0, 140.0], # mmHg
        "bmi": [8.0, 80.0],            # kg/m^2
        "heart_rate": [40.0, 240.0],   # bpm
        "respiratory_rate": [10.0, 80.0]# breaths/min
    },
    "neonatal": {
        "wbc": [2.0, 100.0],           # 10^3/uL
        "creatinine": [0.1, 5.0],      # mg/dL
        "sodium": [115.0, 170.0],      # mEq/L
        "potassium": [2.0, 10.0],      # mEq/L
        "hemoglobin": [5.0, 26.0],     # g/dL
        "glucose": [15.0, 800.0],      # mg/dL
        "systolic_bp": [35.0, 130.0],  # mmHg
        "diastolic_bp": [15.0, 90.0],  # mmHg
        "bmi": [5.0, 40.0],            # kg/m^2
        "heart_rate": [60.0, 260.0],   # bpm
        "respiratory_rate": [15.0, 120.0]# breaths/min
    },
}

DEFAULT_PHYSIOLOGIC_BOUNDS: dict[str, list[float]] = POPULATION_PHYSIOLOGIC_BOUNDS["adult"]

# feature -> canonical unit label and [(unit regex (matched on normalized text), multiplier, [intercept])] to canonical.
# Unit text is lower-cased with whitespace removed and 'µ'/'μ' read as 'u'.
DEFAULT_UNIT_RULES: dict[str, dict[str, Any]] = {
    "wbc": {"canonical": "10*3/uL", "rules": [
        (r"^(/ul|/mcl|/mm3|/mm\^3|cells/ul|cells/mm3|#/ul|10\*0/ul)$", 0.001),
        (r"^(10\*6/l)$", 0.001), (r"^(/l|cells/l)$", 1e-6),
        (r"^(10\*3/ul|10\^3/ul|k/ul|x10\*3/ul|x10\^3/ul|thou/ul|10\*9/l|10e9/l|g/l|10\*3/mm3)$", 1.0)]},
    "platelets": {"canonical": "10*3/uL", "rules": [
        (r"^(/ul|/mcl|/mm3|cells/ul)$", 0.001), (r"^(10\*3/ul|10\^3/ul|k/ul|thou/ul|10\*9/l|g/l)$", 1.0)]},
    "creatinine": {"canonical": "mg/dL", "rules": [
        (r"^(umol/l|µmol/l|micromol/l)$", 1 / 88.42), (r"^(mmol/l)$", 1000 / 88.42), (r"^(mg/dl)$", 1.0)]},
    "glucose": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 18.016), (r"^(mg/dl)$", 1.0), (r"^(g/l)$", 100.0)]},
    "hemoglobin": {"canonical": "g/dL", "rules": [(r"^(g/l)$", 0.1), (r"^(mmol/l)$", 1.611), (r"^(g/dl)$", 1.0)]},
    "hba1c": {"canonical": "%", "rules": [
        (r"^(mmol/mol)$", 0.09148, 2.152), (r"^(%|percent)$", 1.0, 0.0)]},
    "albumin": {"canonical": "g/dL", "rules": [(r"^(g/l)$", 0.1), (r"^(g/dl)$", 1.0)]},
    "bun": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 2.801), (r"^(mg/dl)$", 1.0)]},
    "calcium": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 4.008), (r"^(mg/dl)$", 1.0)]},
    "ldl": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 38.67), (r"^(mg/dl)$", 1.0)]},
    "bilirubin_total": {"canonical": "mg/dL", "rules": [
        (r"^(umol/l|µmol/l)$", 1 / 17.1), (r"^(mg/dl)$", 1.0)]},
    "lactate": {"canonical": "mmol/L", "rules": [(r"^(mg/dl)$", 1 / 9.008), (r"^(mmol/l|meq/l)$", 1.0)]},
    "height": {"canonical": "cm", "rules": [
        (r"^(in|\[in_us\]|\[in_i\]|inches|inch)$", 2.54),
        (r"^(m|meters|meter)$", 100.0),
        (r"^(cm|centimeters|centimeter)$", 1.0)]},
    "weight": {"canonical": "kg", "rules": [
        (r"^(lb|lbs|\[lb_av\]|pounds|pound)$", 0.45359237),
        (r"^(g|grams|gram)$", 0.001),
        (r"^(kg|kilograms|kilogram)$", 1.0),
        (r"^(oz|\[oz_av\]|ounces)$", 0.0283495)]},
}


def resolve_bounds(
    bounds: Mapping[str, Sequence[float | None]] | bool | None = None,
    population: str = "adult",
) -> dict[str, list]:
    """Defaults merged with user overrides. ``False`` -> ``{}`` (disabled); ``None``/``True`` -> population defaults.

    Args:
        bounds: Mapping of feature names to ``[low, high]`` bounds, or bool.
        population: Target patient population (``'adult'``, ``'pediatric'``, or ``'neonatal'``).
    """
    if bounds is False:
        return {}
    pop_key = str(population).lower()
    if pop_key not in POPULATION_PHYSIOLOGIC_BOUNDS:
        raise ValueError(f"population must be one of {list(POPULATION_PHYSIOLOGIC_BOUNDS)}, got {population!r}.")
    merged = {k: list(v) for k, v in POPULATION_PHYSIOLOGIC_BOUNDS[pop_key].items()}
    if bounds is None or bounds is True:
        return merged
    for k, v in bounds.items():
        if v is None:
            merged.pop(k, None)  # explicit None removes the bound for that feature
            continue
        if len(v) != 2:
            raise ValueError(f"bounds[{k!r}] must be [low, high] (either may be None).")
        merged[k] = list(v)
    return merged


def filter_bounds(
    data: pd.DataFrame,
    bounds: Mapping[str, Sequence[float | None]] | bool | None = None,
    *,
    population: str = "adult",
    action: str = "nullify",
    feature_col: str | None = None,
    value_col: str = "value_as_number",
) -> pd.DataFrame:
    """Apply physiologic bounds to a DataFrame; returns a new frame (input untouched).

    Args:
        data: Wide frame (one column per feature, e.g. from ``extract_measurements``) when ``feature_col``
            is None; otherwise a long frame with ``feature_col`` naming the feature and ``value_col`` the value.
        bounds: ``None`` = defaults for ``population``; a dict overrides/extends per feature
            (``{"wbc": [0.5, 150.0]}``); ``False`` disables filtering. Inclusive; ``None`` end = open.
        population: ``'adult'`` (default), ``'pediatric'``, or ``'neonatal'``.
        action: ``'nullify'`` (out-of-range -> NaN; default), ``'clip'`` (winsorize to the bound) or
            ``'drop'`` (long frames: drop the row; wide frames: drop rows where any filtered feature is out of range).

    The result's ``.attrs['bounds_report']`` is a DataFrame of ``feature, low, high, n_checked, n_out_of_range``.
    """
    if action not in ("nullify", "clip", "drop"):
        raise ValueError("action must be 'nullify', 'clip' or 'drop'.")
    cfg = resolve_bounds(bounds, population=population)
    df = data.copy()
    report = []
    drop_mask = pd.Series(False, index=df.index)

    def _oor(s: pd.Series, lo, hi) -> pd.Series:
        m = pd.Series(False, index=s.index)
        if lo is not None:
            m |= s < lo
        if hi is not None:
            m |= s > hi
        return m

    if feature_col is None:
        for feat, (lo, hi) in cfg.items():
            if feat not in df.columns:
                continue
            s = pd.to_numeric(df[feat], errors="coerce")
            bad = _oor(s, lo, hi)
            report.append((feat, lo, hi, int(s.notna().sum()), int(bad.sum())))
            if action == "nullify":
                df[feat] = s.mask(bad)
            elif action == "clip":
                df[feat] = s.clip(lower=lo, upper=hi)
            else:
                drop_mask |= bad
    else:
        if feature_col not in df.columns or value_col not in df.columns:
            raise ValueError(f"Long frame needs columns {feature_col!r} and {value_col!r}.")
        vals = pd.to_numeric(df[value_col], errors="coerce")
        for feat, (lo, hi) in cfg.items():
            sel = df[feature_col] == feat
            if not sel.any():
                continue
            bad = sel & _oor(vals, lo, hi)
            report.append((feat, lo, hi, int((sel & vals.notna()).sum()), int(bad.sum())))
            if action == "nullify":
                vals = vals.mask(bad)
            elif action == "clip":
                vals = vals.mask(sel, vals.clip(lower=lo, upper=hi))
            else:
                drop_mask |= bad
        df[value_col] = vals
    if action == "drop":
        df = df[~drop_mask]
    df.attrs["bounds_report"] = pd.DataFrame(
        report, columns=["feature", "low", "high", "n_checked", "n_out_of_range"])
    return df


def _norm_unit(u: Any) -> str:
    if u is None or (isinstance(u, float) and pd.isna(u)):
        return ""
    return re.sub(r"\s+", "", str(u)).lower().replace("µ", "u").replace("μ", "u")


def harmonize_measurement_units(
    con: duckdb.DuckDBPyConnection,
    features: Mapping[str, Sequence[str | int]] | None = None,
    *,
    unit_rules: Mapping[str, Mapping[str, Any]] | None = None,
    measurement_table: str = "measurement",
    view_name: str = "measurement_harmonized",
    create_view: bool = True,
) -> pd.DataFrame:
    """Unit-normalization preflight. Reports (and optionally applies, via a view) order-of-magnitude fixes.

    For every feature in ``features`` (``{name: LOINC codes or concept ids}``; default
    ``DEFAULT_LAB_LOINCS`` + ``DEFAULT_VITAL_LOINCS``) that has a rule in ``unit_rules``
    (default :data:`DEFAULT_UNIT_RULES`), the distinct units seen (``unit_source_value``, else the
    ``concept_name`` of ``unit_concept_id``) are classified as scaled / already canonical / unrecognized.

    The source table is never modified. With ``create_view`` a view ``view_name`` is created with the same
    columns as ``measurement_table`` except that ``value_as_number`` (and ``range_low``/``range_high`` when
    present) are multiplied by the factor, plus ``unit_harmonized`` and ``harmonization_factor``.
    Unrecognized units are left unscaled (factor 1) and flagged in the report - never silently guessed.
    Feed the view to ``extract_measurements(measurement_table=view_name)``.

    Returns:
        DataFrame ``feature, unit, canonical_unit, factor, status, n`` (status: ``scaled`` / ``canonical`` /
        ``no_unit`` / ``unrecognized``).
    """
    from omop_etl.table1 import DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS

    feats = dict(features) if features is not None else {**DEFAULT_LAB_LOINCS, **DEFAULT_VITAL_LOINCS}
    rules = {**DEFAULT_UNIT_RULES, **(unit_rules or {})}
    feats = {k: ([v] if isinstance(v, (str, int)) else list(v)) for k, v in feats.items() if k in rules}

    if not feats:
        if create_view:
            con.execute(f"CREATE OR REPLACE TEMPORARY VIEW {view_name} AS SELECT *, 1.0 AS harmonization_factor, "
                        f"CAST(NULL AS VARCHAR) AS unit_harmonized FROM {measurement_table}")
        return pd.DataFrame(columns=["feature", "unit", "canonical_unit", "factor", "status", "n", "intercept"])

    mcols = {r[0].lower() for r in con.execute(f"DESCRIBE {measurement_table}").fetchall()}
    has_concept = bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='concept'").fetchone()[0])

    # measurement_id -> feature, via LOINC concept or measurement_source_value or direct concept id
    seeds = pd.DataFrame([(f, str(c)) for f, codes in feats.items() for c in codes], columns=["feature", "code"])
    con.register("_hu_seeds", seeds)
    try:
        parts = ["SELECT s.feature, m.measurement_concept_id AS cid FROM _hu_seeds s "
                 f"JOIN {measurement_table} m ON m.measurement_source_value = s.code"]
        if has_concept:
            parts.append("SELECT s.feature, c.concept_id FROM _hu_seeds s JOIN concept c "
                         "ON c.vocabulary_id='LOINC' AND c.concept_code = s.code")
        parts.append("SELECT s.feature, TRY_CAST(s.code AS BIGINT) FROM _hu_seeds s WHERE regexp_matches(s.code, '^[0-9]+$')")
        con.execute("CREATE OR REPLACE TEMPORARY TABLE _hu_map AS SELECT CAST(MIN(feature) AS VARCHAR) AS feature, CAST(cid AS BIGINT) AS cid FROM ("
                    + " UNION ".join(parts) + ") WHERE cid IS NOT NULL GROUP BY cid")
        unit_sql = "COALESCE(NULLIF(TRIM(m.unit_source_value), ''), " + (
            "uc.concept_name" if has_concept and "unit_concept_id" in mcols else "NULL") + ")"
        uj = "LEFT JOIN concept uc ON uc.concept_id = m.unit_concept_id" if has_concept and "unit_concept_id" in mcols else ""
        seen = con.execute(
            f"SELECT mp.feature, {unit_sql} AS unit, COUNT(*) AS n FROM {measurement_table} m "
            f"JOIN _hu_map mp ON mp.cid = m.measurement_concept_id {uj} "
            "WHERE m.value_as_number IS NOT NULL GROUP BY 1, 2").df()
    finally:
        con.unregister("_hu_seeds")

        rows = []
        for r in seen.itertuples(index=False):
            spec = rules[r.feature]
            nu = _norm_unit(r.unit)
            factor, intercept, status = None, 0.0, "unrecognized"
            if nu == "":
                factor, intercept, status = 1.0, 0.0, "no_unit"
            else:
                for item in spec["rules"]:
                    pat = item[0]
                    mult = item[1]
                    icpt = item[2] if len(item) > 2 else 0.0
                    if re.match(pat, nu):
                        factor, intercept = float(mult), float(icpt)
                        status = "canonical" if (factor == 1.0 and intercept == 0.0) else "scaled"
                        break
            rows.append((r.feature, r.unit, spec["canonical"], 1.0 if factor is None else factor, status, int(r.n), intercept))
        report = pd.DataFrame(rows, columns=["feature", "unit", "canonical_unit", "factor", "status", "n", "intercept"])

        if create_view:
            fac = report[report.status == "scaled"][["feature", "unit", "factor", "intercept"]].copy()
            fac["unit"] = fac["unit"].astype(str)
            con.register("_hu_fac_df", fac)
            con.execute("CREATE OR REPLACE TEMPORARY TABLE _hu_fac AS SELECT CAST(feature AS VARCHAR) feature, "
                        "CAST(unit AS VARCHAR) unit, CAST(factor AS DOUBLE) factor, "
                        "CAST(intercept AS DOUBLE) intercept FROM _hu_fac_df")
            con.unregister("_hu_fac_df")
            canon = pd.DataFrame([(f, rules[f]["canonical"]) for f in feats], columns=["feature", "canonical_unit"])
            con.register("_hu_canon_df", canon)
            con.execute("CREATE OR REPLACE TEMPORARY TABLE _hu_canon AS SELECT CAST(feature AS VARCHAR) feature, "
                        "CAST(canonical_unit AS VARCHAR) canonical_unit FROM _hu_canon_df")
            con.unregister("_hu_canon_df")
            scaled = {"value_as_number", "range_low", "range_high"}
            order = [r[0] for r in con.execute(f"DESCRIBE {measurement_table}").fetchall()]
            sel = ", ".join(
                f"(m.{c} * COALESCE(f.factor, 1.0) + COALESCE(f.intercept, 0.0)) AS {c}" if c.lower() in scaled else f"m.{c}" for c in order)
            # explicit column list (not `* REPLACE`): robust to optimizer projection pruning in downstream joins
            con.execute(
                f"CREATE OR REPLACE TEMPORARY VIEW {view_name} AS "
                f"SELECT {sel}, COALESCE(f.factor, 1.0) AS harmonization_factor, ca.canonical_unit AS unit_harmonized "
                f"FROM {measurement_table} m "
                "LEFT JOIN _hu_map mp ON mp.cid = m.measurement_concept_id "
                f"{uj} "
                f"LEFT JOIN _hu_fac f ON f.feature = mp.feature AND f.unit = {unit_sql} "
                "LEFT JOIN _hu_canon ca ON ca.feature = f.feature")
        return report.drop(columns=["intercept"])


def harmonize_anthropometrics(
    con: duckdb.DuckDBPyConnection,
    measurement_table: str = "measurement",
    view_name: str = "measurement_harmonized_anthropometrics",
    *,
    calculate_bmi: bool = True,
    replace_implausible_bmi: bool = True,
    create_view: bool = True,
) -> pd.DataFrame:
    """Standardizes height to cm and weight to kg, calculating and validating BMI.

    Prevents raw inch/cm or lb/kg unit mismatches from inflating calculated BMI to > 300.
    Identifies height (LOINC 8302-2 / 'HT') and weight (LOINC 29463-7 / 'WT') measurements
    for each patient and calculates BMI = weight (kg) / (height (m))^2. When ``replace_implausible_bmi``
    is True, existing BMI measurements outside [10.0, 120.0] are recalculated from concordant
    height/weight where available, or nullified.

    Args:
        con: Active DuckDB connection.
        measurement_table: Source measurement table or view (e.g. from ``harmonize_measurement_units``).
        view_name: Target view name to register.
        calculate_bmi: Whether to compute BMI where height and weight are present.
        replace_implausible_bmi: Whether to overwrite implausible BMI (<10 or >120) with valid calculated BMI.
        create_view: Whether to create the temporary view ``view_name``.

    Returns:
        pd.DataFrame: Audit summary with counts of standardized heights, weights, and calculated/corrected BMIs.
    """
    mcols = {r[0].lower() for r in con.execute(f"DESCRIBE {measurement_table}").fetchall()}
    has_concept = bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='concept'").fetchone()[0])
    if "measurement_datetime" in mcols and "measurement_date" in mcols:
        date_expr = "COALESCE(CAST(m.measurement_datetime AS DATE), CAST(m.measurement_date AS DATE))"
    elif "measurement_datetime" in mcols:
        date_expr = "CAST(m.measurement_datetime AS DATE)"
    else:
        date_expr = "CAST(m.measurement_date AS DATE)"

    # Identify heights, weights, and bmis in the source table
    ht_sql = f"""
        SELECT
            m.person_id,
            {date_expr} AS meas_date,
            CASE
                WHEN LOWER(COALESCE(m.unit_source_value, '')) LIKE '%in%' OR m.unit_concept_id IN (9326, 9327) THEN m.value_as_number * 2.54
                WHEN LOWER(COALESCE(m.unit_source_value, '')) LIKE '%m%' AND LOWER(COALESCE(m.unit_source_value, '')) NOT LIKE '%cm%' AND m.value_as_number < 3.0 THEN m.value_as_number * 100.0
                WHEN m.value_as_number < 3.0 THEN m.value_as_number * 100.0  -- Likely recorded in meters
                ELSE m.value_as_number
            END AS height_cm
        FROM {measurement_table} m
        WHERE (m.measurement_concept_id IN (3036277, 3023540, 3019171)
               OR m.measurement_source_value ILIKE '%height%'
               OR m.measurement_source_value = 'HT'
               OR m.measurement_source_value = '8302-2')
          AND m.value_as_number IS NOT NULL AND m.value_as_number > 0
    """

    wt_sql = f"""
        SELECT
            m.person_id,
            {date_expr} AS meas_date,
            CASE
                WHEN LOWER(COALESCE(m.unit_source_value, '')) LIKE '%lb%' OR m.unit_concept_id = 8739 THEN m.value_as_number * 0.45359237
                WHEN LOWER(COALESCE(m.unit_source_value, '')) LIKE '%oz%' THEN m.value_as_number * 0.0283495
                WHEN LOWER(COALESCE(m.unit_source_value, '')) LIKE '%g%' AND LOWER(COALESCE(m.unit_source_value, '')) NOT LIKE '%kg%' THEN m.value_as_number * 0.001
                ELSE m.value_as_number
            END AS weight_kg
        FROM {measurement_table} m
        WHERE (m.measurement_concept_id IN (3025315, 3013762)
               OR m.measurement_source_value ILIKE '%weight%'
               OR m.measurement_source_value = 'WT'
               OR m.measurement_source_value = '29463-7')
          AND m.value_as_number IS NOT NULL AND m.value_as_number > 0
    """

    n_ht = con.execute(f"SELECT COUNT(*) FROM ({ht_sql})").fetchone()[0]
    n_wt = con.execute(f"SELECT COUNT(*) FROM ({wt_sql})").fetchone()[0]

    calc_bmi_sql = f"""
        WITH hts AS (
            SELECT person_id, meas_date, AVG(height_cm) AS height_cm
            FROM ({ht_sql})
            WHERE height_cm BETWEEN 30.0 AND 280.0
            GROUP BY person_id, meas_date
        ),
        wts AS (
            SELECT person_id, meas_date, AVG(weight_kg) AS weight_kg
            FROM ({wt_sql})
            WHERE weight_kg BETWEEN 1.0 AND 500.0
            GROUP BY person_id, meas_date
        )
        SELECT
            h.person_id,
            h.meas_date,
            ROUND(w.weight_kg / POWER(h.height_cm / 100.0, 2), 2) AS calculated_bmi
        FROM hts h
        JOIN wts w ON w.person_id = h.person_id AND w.meas_date = h.meas_date
        WHERE (w.weight_kg / POWER(h.height_cm / 100.0, 2)) BETWEEN 5.0 AND 120.0
    """
    n_calc = con.execute(f"SELECT COUNT(*) FROM ({calc_bmi_sql})").fetchone()[0]

    if create_view:
        con.execute(f"CREATE OR REPLACE TEMPORARY VIEW _calc_bmis AS {calc_bmi_sql}")
        order = [r[0] for r in con.execute(f"DESCRIBE {measurement_table}").fetchall()]
        sel_cols = []
        for c in order:
            if c.lower() == "value_as_number":
                if replace_implausible_bmi:
                    expr = (
                        f"CASE WHEN (m.measurement_concept_id = 3038553 OR m.measurement_source_value IN ('BMI', 'ORIGINAL_BMI', '39156-5')) "
                        f"AND (m.value_as_number < 10.0 OR m.value_as_number > 120.0) THEN cb.calculated_bmi "
                        f"ELSE m.value_as_number END AS value_as_number"
                    )
                else:
                    expr = "m.value_as_number"
                sel_cols.append(expr)
            else:
                sel_cols.append(f"m.{c}")

        con.execute(f"""
            CREATE OR REPLACE TEMPORARY VIEW {view_name} AS
            SELECT {", ".join(sel_cols)}
            FROM {measurement_table} m
            LEFT JOIN _calc_bmis cb
              ON cb.person_id = m.person_id
             AND cb.meas_date = {date_expr}
        """)

    report = pd.DataFrame([{
        "height_records_checked": int(n_ht),
        "weight_records_checked": int(n_wt),
        "calculated_bmis_derived": int(n_calc),
    }])
    return report
