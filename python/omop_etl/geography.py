"""Area-level social/geographic features (RUCA, SDI) for model replication at another OMOP site.

OMOP CDM v5.4 stores only raw address fields (``location``); it defines no RUCA or SDI. A model that uses them as
predictors therefore needs three things at the replicating site, all supplied here or by the model release:

* a dated address history, so the address *in effect at the index admission* can be chosen (:func:`load_location` fills
  ``person_address_history``; without it :func:`derive_geography_features` falls back to ``person.location_id``);
* a ZIP -> RUCA crosswalk (USDA) and a ZCTA -> SDI score table (Robert Graham Center), both supplied by the caller;
* the **fixed SDI quintile cut points** of the training cohort (``sdi_cut_points``, four ascending scores). Cutting at
  quantiles of the local population would change every patient's quintile whenever the local SDI distribution differs
  from the training one ("quintile drift"), so :func:`derive_geography_features` refuses to run without cut points
  unless ``fit_cut_points=True`` is passed explicitly (training-site use only; the fitted cuts are returned in
  ``frame.attrs["sdi_cut_points"]`` so they can be published with the model).

Address rule (reference study): the address whose period contains the admission date; else the most recent address that
started before admission; else the earliest address that started after it. Addresses with a ZIP always beat those
without. Ties: ``preferred = 'Y'``, then the nearest start date.

RUCA labels follow the reference coding: primary code 1 = Metropolitan, 2 = Micropolitan, 3 and above = Small Town / Rural.
USDA code 99 ("not coded") therefore lands in Small Town / Rural, as in the reference; pass ``code_99_missing=True`` to
treat it as unknown instead (this changes the feature, so only do so if the model was trained that way).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.custom_covariates import DERIVED_VALUE_TYPE_CONCEPT_ID, write_custom_covariates

__all__ = [
    "RUCA_MISSING", "SDI_MISSING", "SDI_LABELS", "SDI_PROBS",
    "ruca_category", "fit_sdi_cut_points", "assign_sdi_quintile", "validate_sdi_cut_points",
    "read_ruca_crosswalk", "read_sdi_table", "derive_geography_features", "write_geography_measurements",
    "GEOGRAPHY_CONCEPT_IDS",
]

RUCA_MISSING = "RUCA: Missing / Unknown"
SDI_MISSING = "SDI: Missing / Unknown"
SDI_LABELS = ["SDI Quintile 1 (Least Deprived)", "SDI Quintile 2", "SDI Quintile 3", "SDI Quintile 4",
              "SDI Quintile 5 (Most Deprived)"]
SDI_PROBS = (0.2, 0.4, 0.6, 0.8)
_ADDRESS_TABLE = "person_address_history"


def ruca_category(code: Any, *, code_99_missing: bool = False) -> str:
    """Primary RUCA code -> model category (secondary digits, e.g. 4.2, are ignored)."""
    try:
        if code is None or pd.isna(code):
            return RUCA_MISSING
        p = int(float(code))
    except (TypeError, ValueError):
        return RUCA_MISSING
    if p < 1 or (code_99_missing and p == 99):
        return RUCA_MISSING
    return {1: "RUCA: Metropolitan (1.0)", 2: "RUCA: Micropolitan (2.0)"}.get(p, "RUCA: Small Town / Rural (3+)")


def validate_sdi_cut_points(cut_points: Sequence[float]) -> list[float]:
    cuts = [float(c) for c in cut_points]
    if len(cuts) != 4 or not all(math.isfinite(c) for c in cuts) or any(b <= a for a, b in zip(cuts, cuts[1:])):
        raise ValueError(f"sdi_cut_points must be four finite, strictly ascending SDI scores; got {list(cut_points)!r}.")
    return cuts


def fit_sdi_cut_points(scores: Any) -> list[float]:
    """Quintile boundaries (20/40/60/80th percentiles) of the known SDI scores - run once on the training cohort."""
    s = pd.to_numeric(pd.Series(scores), errors="coerce").dropna()
    if len(s) < 5:
        raise ValueError("Need at least 5 non-missing SDI scores to fit quintile cut points.")
    return validate_sdi_cut_points(np.quantile(s.to_numpy(float), SDI_PROBS).tolist())


def assign_sdi_quintile(scores: Any, cut_points: Sequence[float]) -> pd.Series:
    """Bin SDI scores against fixed cut points; a score equal to a cut point goes to the higher quintile."""
    cuts = validate_sdi_cut_points(cut_points)
    s = pd.to_numeric(pd.Series(scores), errors="coerce")
    out = pd.Series(SDI_MISSING, index=s.index, dtype=object)
    known = s.notna()
    out[known] = np.array(SDI_LABELS, dtype=object)[np.searchsorted(cuts, s[known].to_numpy(float), side="right")]
    return out


def _read_table(source: Any, aliases: dict[str, tuple[str, ...]], what: str) -> pd.DataFrame:
    df = source.copy() if isinstance(source, pd.DataFrame) else pd.read_csv(Path(source), dtype=str)
    lower = {c.lower(): c for c in df.columns}
    out = {}
    for target, names in aliases.items():
        hit = next((lower[n] for n in names if n in lower), None)
        if hit is None:
            raise ValueError(f"{what} needs a column named one of {list(names)}; found {list(df.columns)}.")
        out[target] = df[hit]
    return pd.DataFrame(out)


def _zip5(s: pd.Series) -> pd.Series:
    return s.astype("string").str.extract(r"(\d{5})", expand=False)


def read_ruca_crosswalk(source: Any) -> pd.DataFrame:
    """USDA ZIP -> RUCA crosswalk (CSV path or DataFrame) as ``zip5, ruca_code``; first row wins on duplicate ZIPs."""
    df = _read_table(source, {"zip5": ("zipcode", "zip", "zip5", "zip_code"),
                              "ruca_code": ("ruca_code", "ruca", "ruca1", "primary_ruca")}, "RUCA crosswalk")
    df["zip5"] = _zip5(df["zip5"].astype("string").str.strip().str.zfill(5))
    df["ruca_code"] = pd.to_numeric(df["ruca_code"], errors="coerce")
    return df.dropna(subset=["zip5"]).drop_duplicates("zip5").reset_index(drop=True)


def read_sdi_table(source: Any) -> pd.DataFrame:
    """Robert Graham Center ZCTA SDI table (CSV path or DataFrame) as ``zip5, sdi_score``."""
    df = _read_table(source, {"zip5": ("zcta5_fips", "zcta5", "zcta", "zip", "zip5"),
                              "sdi_score": ("sdi_score", "sdi")}, "SDI table")
    df["zip5"] = _zip5(df["zip5"].astype("string").str.strip().str.zfill(5))
    df["sdi_score"] = pd.to_numeric(df["sdi_score"], errors="coerce")
    return df.dropna(subset=["zip5"]).drop_duplicates("zip5").reset_index(drop=True)


def _has_table(con, name: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema() "
                            "AND table_name = ?", [name]).fetchone()[0])


def _index_addresses(con, stays_sql: str, address_table: str | None) -> pd.DataFrame:
    """One address row per stay, selected by the reference rule. ``stays_sql`` yields (person_id, stay_id, admit_date)."""
    if address_table is None:
        return con.execute(f"""
            SELECT person_id, stay_id, zip5, CASE WHEN zip5 IS NULL THEN 'no_zip' ELSE 'person_location' END AS rule
            FROM (
                SELECT s.person_id, s.stay_id, nullif(regexp_extract(CAST(l.zip AS VARCHAR), '(\\d{{5}})', 1), '') AS zip5
                FROM ({stays_sql}) s
                LEFT JOIN person p ON p.person_id = s.person_id
                LEFT JOIN location l ON l.location_id = p.location_id
            )
        """).df()
    return con.execute(f"""
        WITH m AS (
            SELECT s.person_id, s.stay_id, s.admit_date, a.zip5, a.address_start AS st, a.address_end AS en, a.preferred,
                   (a.zip5 IS NULL) AS nozip,
                   (a.address_start IS NULL OR a.address_start <= s.admit_date) AS started,
                   (a.address_end IS NULL OR a.address_end >= s.admit_date) AS ongoing
            FROM ({stays_sql}) s LEFT JOIN {address_table} a ON a.person_id = s.person_id
        ), r AS (
            SELECT *, CASE WHEN started AND ongoing THEN 'in_effect' WHEN started THEN 'latest_before'
                           ELSE 'earliest_after' END AS rule0 FROM m
        )
        SELECT person_id, stay_id, zip5, CASE WHEN zip5 IS NULL THEN 'no_zip' ELSE rule0 END AS rule
        FROM r
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY stay_id
            ORDER BY nozip,
                     CASE rule0 WHEN 'in_effect' THEN 0 WHEN 'latest_before' THEN 1 ELSE 2 END,
                     CASE WHEN upper(coalesce(preferred, '')) = 'Y' THEN 0 ELSE 1 END,
                     CASE WHEN rule0 = 'earliest_after' THEN date_diff('day', admit_date, st)
                          ELSE coalesce(date_diff('day', st, admit_date), 1000000) END,
                     st DESC NULLS LAST) = 1
    """).df()


def derive_geography_features(
    con: duckdb.DuckDBPyConnection,
    ruca_crosswalk: Any,
    sdi_table: Any,
    sdi_cut_points: Sequence[float] | None = None,
    *,
    stays: pd.DataFrame | None = None,
    address_table: str | None = "auto",
    fit_cut_points: bool = False,
    code_99_missing: bool = False,
) -> pd.DataFrame:
    """RUCA category and SDI quintile for each index stay.

    ``stays`` is a DataFrame with ``person_id`` and ``admit_date`` (plus an optional unique ``stay_id``, else its row
    index is used); the default is every inpatient/ED-inpatient visit (concepts 9201, 262) in ``visit_occurrence``,
    with ``stay_id = visit_occurrence_id``. ``address_table='auto'`` uses ``person_address_history`` when present and
    non-empty, else ``person.location_id``; pass a table name to force one, or ``None`` for the person's location.

    Returns ``person_id, stay_id, zip5, rule, ruca_code, ruca_category, sdi_score, sdi_quintile``. ``rule`` records how the
    address was chosen (``in_effect``, ``latest_before``, ``earliest_after``, ``person_location``, ``no_zip``).
    """
    if sdi_cut_points is None and not fit_cut_points:
        raise ValueError(
            "sdi_cut_points is required: SDI quintiles must use the training cohort's fixed cut points, not quantiles of the "
            "local population. Pass fit_cut_points=True only when building the training reference.")
    cuts = validate_sdi_cut_points(sdi_cut_points) if sdi_cut_points is not None else None
    ruca, sdi = read_ruca_crosswalk(ruca_crosswalk), read_sdi_table(sdi_table)

    if stays is None:
        stays_df = con.execute("SELECT person_id, visit_occurrence_id AS stay_id, visit_start_date AS admit_date "
                               "FROM visit_occurrence WHERE visit_concept_id IN (9201, 262)").df()
    else:
        stays_df = stays.copy()
        if "stay_id" not in stays_df:
            stays_df["stay_id"] = np.arange(len(stays_df))
        missing = {"person_id", "admit_date"} - set(stays_df.columns)
        if missing:
            raise ValueError(f"stays is missing column(s) {sorted(missing)}.")
        if stays_df["stay_id"].duplicated().any():
            raise ValueError("stays.stay_id must be unique.")
        stays_df = stays_df[["person_id", "stay_id", "admit_date"]]
        stays_df["admit_date"] = pd.to_datetime(stays_df["admit_date"])
    con.register("_geo_stays", stays_df)
    try:
        table = address_table
        if table == "auto":
            table = _ADDRESS_TABLE if (_has_table(con, _ADDRESS_TABLE)
                                       and con.execute(f"SELECT count(*) FROM {_ADDRESS_TABLE}").fetchone()[0]) else None
        if table is not None and not table.replace("_", "").isalnum():
            raise ValueError(f"invalid address table name {table!r}.")
        picked = _index_addresses(con, "SELECT person_id, stay_id, CAST(admit_date AS DATE) AS admit_date FROM _geo_stays", table)
    finally:
        con.unregister("_geo_stays")

    g = picked.merge(ruca, on="zip5", how="left").merge(sdi, on="zip5", how="left")
    g = g.merge(stays_df[["stay_id", "admit_date"]], on="stay_id", how="left")
    g["ruca_category"] = g["ruca_code"].map(lambda c: ruca_category(c, code_99_missing=code_99_missing))
    if cuts is None:
        cuts = fit_sdi_cut_points(g["sdi_score"])
    g["sdi_quintile"] = assign_sdi_quintile(g["sdi_score"], cuts).to_numpy()
    g = g[["person_id", "stay_id", "zip5", "rule", "ruca_code", "ruca_category", "sdi_score", "sdi_quintile", "admit_date"]]
    g.attrs["sdi_cut_points"] = cuts
    return g


# Area-level values stored as MEASUREMENT rows so OHDSI tools (FeatureExtraction, PatientLevelPrediction, ATLAS) see them
# as ordinary covariates. Local concept ids (>= 2,000,000,000 is the OMOP convention for site-defined concepts).
GEOGRAPHY_CONCEPT_IDS = {"sdi_score": 2000100001, "sdi_quintile": 2000100002, "ruca_code": 2000100003}
_GEOGRAPHY_CONCEPT_NAMES = {
    "sdi_score": "Social Deprivation Index score of the residence ZIP at admission",
    "sdi_quintile": "Social Deprivation Index quintile (1 least to 5 most deprived) of the residence ZIP at admission",
    "ruca_code": "Rural-Urban Commuting Area primary code of the residence ZIP at admission",
}


def write_geography_measurements(
    con: duckdb.DuckDBPyConnection,
    geography: pd.DataFrame,
    *,
    concept_ids: dict[str, int] | None = None,
    visit_ids: bool = True,
    register_concepts: bool = True,
    type_concept_id: int = DERIVED_VALUE_TYPE_CONCEPT_ID,
) -> int:
    """Writes the derived area-level values as ``measurement`` rows (the result of :func:`derive_geography_features`).

    One row per stay and value, dated the admission date: SDI score and RUCA code as they are, SDI quintile as 1-5 (from
    the fixed cut points). Missing values write no row. Re-running replaces the rows of the same concepts for the same
    persons. ``visit_ids=True`` links each row to ``visit_occurrence_id = stay_id`` (right when the stays are
    ``visit_occurrence`` rows, the default of :func:`derive_geography_features`). ``register_concepts`` adds the three local
    concepts to ``concept`` (Measurement domain) when that table exists, so covariate names resolve. ``unit_concept_id`` is 0
    (the values have no unit): FeatureExtraction joins measurement covariates on the unit and skips rows where it is NULL.
    Returns the rows written.

    FeatureExtraction then reads them with ``createCovariateSettings(useMeasurementValueLongTerm = TRUE,
    includedCovariateConceptIds = c(2000100001, 2000100002, 2000100003))``.
    """
    ids = {**GEOGRAPHY_CONCEPT_IDS, **(concept_ids or {})}
    need = {"person_id", "stay_id", "admit_date", "sdi_score", "sdi_quintile", "ruca_code"}
    if need - set(geography.columns):
        raise ValueError(f"geography is missing column(s) {sorted(need - set(geography.columns))}; pass the result of derive_geography_features().")
    g = geography.copy()
    quint = {label: i + 1 for i, label in enumerate(SDI_LABELS)}
    g["sdi_quintile_n"] = g["sdi_quintile"].map(quint)
    g["ruca_primary"] = np.floor(pd.to_numeric(g["ruca_code"], errors="coerce"))        # secondary digits (4.2) are not part of the primary code
    parts = []
    for col, key in (("sdi_score", "sdi_score"), ("sdi_quintile_n", "sdi_quintile"), ("ruca_primary", "ruca_code")):
        p = g[["person_id", "stay_id", "admit_date", col]].rename(columns={col: "value", "admit_date": "date", "stay_id": "visit_occurrence_id"})
        p["feature"] = key
        parts.append(p)
    return write_custom_covariates(con, pd.concat(parts, ignore_index=True),
                                   {k: (int(ids[k]), _GEOGRAPHY_CONCEPT_NAMES[k]) for k in ids},
                                   source="omop_etl.geography", visit_ids=visit_ids,
                                   register_concepts=register_concepts, type_concept_id=type_concept_id)
