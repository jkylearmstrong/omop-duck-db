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
    "DEFAULT_UNIT_RULES",
    "resolve_bounds",
    "filter_bounds",
    "harmonize_measurement_units",
]

DEFAULT_PHYSIOLOGIC_BOUNDS: dict[str, list[float]] = {
    "wbc": [0.1, 200.0],           # 10^3/uL
    "creatinine": [0.1, 30.0],     # mg/dL
    "sodium": [100.0, 180.0],      # mEq/L
    "potassium": [1.0, 10.0],      # mEq/L
    "hemoglobin": [2.0, 25.0],     # g/dL
    "glucose": [10.0, 2000.0],     # mg/dL
    "systolic_bp": [40.0, 300.0],  # mmHg
    "diastolic_bp": [20.0, 200.0],  # mmHg
    "bmi": [10.0, 120.0],          # kg/m^2
}

# feature -> canonical unit label and [(unit regex (matched on normalized text), multiplier)] to canonical.
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
    "albumin": {"canonical": "g/dL", "rules": [(r"^(g/l)$", 0.1), (r"^(g/dl)$", 1.0)]},
    "bun": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 2.801), (r"^(mg/dl)$", 1.0)]},
    "calcium": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 4.008), (r"^(mg/dl)$", 1.0)]},
    "ldl": {"canonical": "mg/dL", "rules": [(r"^(mmol/l)$", 38.67), (r"^(mg/dl)$", 1.0)]},
    "bilirubin_total": {"canonical": "mg/dL", "rules": [
        (r"^(umol/l|µmol/l)$", 1 / 17.1), (r"^(mg/dl)$", 1.0)]},
    "lactate": {"canonical": "mmol/L", "rules": [(r"^(mg/dl)$", 1 / 9.008), (r"^(mmol/l|meq/l)$", 1.0)]},
}


def resolve_bounds(bounds: Mapping[str, Sequence[float | None]] | bool | None = None) -> dict[str, list]:
    """Defaults merged with user overrides. ``False`` -> ``{}`` (disabled); ``None``/``True`` -> defaults."""
    if bounds is False:
        return {}
    merged = {k: list(v) for k, v in DEFAULT_PHYSIOLOGIC_BOUNDS.items()}
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
    action: str = "nullify",
    feature_col: str | None = None,
    value_col: str = "value_as_number",
) -> pd.DataFrame:
    """Apply physiologic bounds to a DataFrame; returns a new frame (input untouched).

    Args:
        data: Wide frame (one column per feature, e.g. from ``extract_measurements``) when ``feature_col``
            is None; otherwise a long frame with ``feature_col`` naming the feature and ``value_col`` the value.
        bounds: ``None`` = :data:`DEFAULT_PHYSIOLOGIC_BOUNDS`; a dict overrides/extends per feature
            (``{"wbc": [0.5, 150.0]}``); ``False`` disables filtering. Inclusive; ``None`` end = open.
        action: ``'nullify'`` (out-of-range -> NaN; default), ``'clip'`` (winsorize to the bound) or
            ``'drop'`` (long frames: drop the row; wide frames: drop rows where any filtered feature is out of range).

    The result's ``.attrs['bounds_report']`` is a DataFrame of ``feature, low, high, n_checked, n_out_of_range``.
    """
    if action not in ("nullify", "clip", "drop"):
        raise ValueError("action must be 'nullify', 'clip' or 'drop'.")
    cfg = resolve_bounds(bounds)
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
        return pd.DataFrame(columns=["feature", "unit", "canonical_unit", "factor", "status", "n"])

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
        factor, status = None, "unrecognized"
        if nu == "":
            factor, status = 1.0, "no_unit"
        else:
            for pat, mult in spec["rules"]:
                if re.match(pat, nu):
                    factor, status = float(mult), ("canonical" if mult == 1.0 else "scaled")
                    break
        rows.append((r.feature, r.unit, spec["canonical"], 1.0 if factor is None else factor, status, int(r.n)))
    report = pd.DataFrame(rows, columns=["feature", "unit", "canonical_unit", "factor", "status", "n"])

    if create_view:
        fac = report[report.status == "scaled"][["feature", "unit", "factor"]].copy()
        fac["unit"] = fac["unit"].astype(str)
        con.register("_hu_fac_df", fac)
        con.execute("CREATE OR REPLACE TEMPORARY TABLE _hu_fac AS SELECT CAST(feature AS VARCHAR) feature, "
                    "CAST(unit AS VARCHAR) unit, CAST(factor AS DOUBLE) factor FROM _hu_fac_df")
        con.unregister("_hu_fac_df")
        canon = pd.DataFrame([(f, rules[f]["canonical"]) for f in feats], columns=["feature", "canonical_unit"])
        con.register("_hu_canon_df", canon)
        con.execute("CREATE OR REPLACE TEMPORARY TABLE _hu_canon AS SELECT CAST(feature AS VARCHAR) feature, "
                    "CAST(canonical_unit AS VARCHAR) canonical_unit FROM _hu_canon_df")
        con.unregister("_hu_canon_df")
        scaled = {"value_as_number", "range_low", "range_high"}
        order = [r[0] for r in con.execute(f"DESCRIBE {measurement_table}").fetchall()]
        sel = ", ".join(
            f"m.{c} * COALESCE(f.factor, 1.0) AS {c}" if c.lower() in scaled else f"m.{c}" for c in order)
        # explicit column list (not `* REPLACE`): robust to optimizer projection pruning in downstream joins
        con.execute(
            f"CREATE OR REPLACE TEMPORARY VIEW {view_name} AS "
            f"SELECT {sel}, COALESCE(f.factor, 1.0) AS harmonization_factor, ca.canonical_unit AS unit_harmonized "
            f"FROM {measurement_table} m "
            "LEFT JOIN _hu_map mp ON mp.cid = m.measurement_concept_id "
            f"{uj} "
            f"LEFT JOIN _hu_fac f ON f.feature = mp.feature AND f.unit = {unit_sql} "
            "LEFT JOIN _hu_canon ca ON ca.feature = f.feature")
    return report
