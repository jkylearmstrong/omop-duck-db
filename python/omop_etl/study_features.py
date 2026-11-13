"""Study-design features: a small framework for adding your own person-level features to an OMOP study.

A *study design feature* is anything a protocol needs that the OMOP CDM does not define itself: an area-level index of the
residence ZIP (RUCA, SDI, census income), a risk score, a site-specific definition of an exposure. This module keeps the
package small by shipping the *framework* and a few worked examples, not the data:

* :class:`StudyFeature` is one feature: a name, the columns it produces, the OMOP tables it needs, a compute function and any
  *fitted state* (SDI cut points, a training mean) that must travel with a model.
* :class:`FeatureSet` collects features (``add`` / ``extend``), computes them for a set of index stays in one pass (the address
  in effect at admission is resolved once and shared), and then
  - returns a wide, leakage-safe table (:meth:`FeatureSet.compute`),
  - writes the values as ``measurement`` rows under site-defined concepts so OHDSI FeatureExtraction, PatientLevelPrediction
    and DeepPatientLevelPrediction read them like any other covariate (:meth:`FeatureSet.write`,
    :meth:`FeatureSet.feature_extraction_r`), and
  - exports a manifest with the concept ids, parameters and fitted state, so a replicating site reproduces the features
    exactly (:meth:`FeatureSet.manifest`, :meth:`FeatureSet.load_state`).
* Built-ins: :func:`ruca_feature`, :func:`sdi_feature` (these reproduce :mod:`omop_etl.geography`), :func:`area_feature` (any
  ZIP-keyed numeric table such as ACS median household income, with log / percentile / z-score transforms fitted on the
  training sites only and a missing indicator), :func:`sql_feature` and :func:`custom_feature`.

Transforms that learn something from data (percentile grid, mean and sd, SDI cut points) refuse to run on a new site unless
the training state is supplied, exactly like ``sdi_cut_points`` in :func:`omop_etl.geography.derive_geography_features`; pass
``fit=True`` only when building the training reference. Nothing here reads outcomes or events after the index date.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.custom_covariates import DERIVED_VALUE_TYPE_CONCEPT_ID, register_custom_concepts, write_custom_covariates
from omop_etl.geography import (
    GEOGRAPHY_CONCEPT_IDS, SDI_LABELS, _ADDRESS_TABLE, _has_table, _index_addresses, assign_sdi_quintile, fit_sdi_cut_points,
    read_ruca_crosswalk, read_sdi_table, ruca_category, validate_sdi_cut_points,
)

__all__ = ["StudyFeature", "FeatureSet", "StudyContext", "stays_from_cohort", "ruca_feature", "sdi_feature", "area_feature",
           "sql_feature", "custom_feature", "concept_feature", "register_feature_factory", "available_features", "make_feature", "feature_list",
           "FEATURE_LISTS", "MANIFEST_FORMAT"]

MANIFEST_FORMAT = "omop-duck-db/study-feature-set"
_KEY = ("person_id", "stay_id", "admit_date")
_TRANSFORMS = ("none", "log1p", "percentile", "zscore")


# ---------------------------------------------------------------------------------------------------------------------
# Context: the index stays and anything shared between features
# ---------------------------------------------------------------------------------------------------------------------
def stays_from_cohort(con: duckdb.DuckDBPyConnection, cohort_table: str = "cohort", *, cohort_id: int | None = None) -> pd.DataFrame:
    """Index stays of a cohort table as ``person_id, stay_id, admit_date`` (cohort order).

    ``stay_id`` is ``visit_occurrence_id`` when the cohort has it, else the row number. ``cohort_table`` must have
    ``subject_id`` (or ``person_id``) and ``cohort_start_date`` (the admission).
    """
    if not cohort_table.replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"invalid cohort table name {cohort_table!r}.")
    cols = {r[0].lower() for r in con.execute(f"DESCRIBE {cohort_table}").fetchall()}
    pid = "subject_id" if "subject_id" in cols else "person_id" if "person_id" in cols else None
    if pid is None or "cohort_start_date" not in cols:
        raise ValueError(f"{cohort_table} needs subject_id (or person_id) and cohort_start_date.")
    sid = "visit_occurrence_id" if "visit_occurrence_id" in cols else "row_number() OVER ()"
    where = f"WHERE cohort_definition_id = {int(cohort_id)}" if cohort_id is not None and "cohort_definition_id" in cols else ""
    df = con.execute(f"SELECT {pid} AS person_id, {sid} AS stay_id, CAST(cohort_start_date AS DATE) AS admit_date "
                     f"FROM {cohort_table} {where}").df()
    df["admit_date"] = pd.to_datetime(df["admit_date"])
    return df


class StudyContext:
    """What a feature may use: the connection, the index ``stays`` and cached shared lookups.

    ``zip5()`` returns one row per stay (``person_id, stay_id, zip5, rule``) using the address in effect at admission (the
    rule of :mod:`omop_etl.geography`); it is computed once however many area features ask for it.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, stays: pd.DataFrame, *, address_table: str | None = "auto") -> None:
        self.con, self.stays, self.address_table = con, stays, address_table
        self._zip: pd.DataFrame | None = None
        self._zip3: pd.DataFrame | None = None

    def zip3(self) -> pd.DataFrame:
        """One row per stay (``person_id, stay_id, zip3``): the first three digits of the residence ZIP at admission.

        De-identified OMOP extracts often hold only 3-digit ZIPs in ``location.zip``; those are used when no 5-digit ZIP
        resolves for a stay (the address-history rule is applied first).
        """
        if self._zip3 is None:
            z = self.zip5()[["person_id", "stay_id", "zip5"]].copy()
            z["zip3"] = z["zip5"].str[:3]
            if z["zip3"].isna().any() and _has_table(self.con, "location") and _has_table(self.con, "person"):
                raw = self.con.execute("""SELECT p.person_id, nullif(regexp_extract(CAST(l.zip AS VARCHAR), '(\\d{3})', 1), '') AS z3
                                          FROM person p JOIN location l ON l.location_id = p.location_id""").df()
                z = z.merge(raw.drop_duplicates("person_id"), on="person_id", how="left")
                z["zip3"] = z["zip3"].fillna(z["z3"])
            self._zip3 = z[["person_id", "stay_id", "zip3"]]
        return self._zip3

    def zip5(self) -> pd.DataFrame:
        if self._zip is None:
            table = self.address_table
            if table == "auto":
                table = _ADDRESS_TABLE if (_has_table(self.con, _ADDRESS_TABLE)
                                           and self.con.execute(f"SELECT count(*) FROM {_ADDRESS_TABLE}").fetchall()[0][0]) else None
            if table is not None and not table.replace("_", "").isalnum():
                raise ValueError(f"invalid address table name {table!r}.")
            self.con.register("_sf_stays", self.stays[list(_KEY)])
            try:
                self._zip = _index_addresses(self.con, "SELECT person_id, stay_id, CAST(admit_date AS DATE) AS admit_date FROM _sf_stays", table)
            finally:
                self.con.unregister("_sf_stays")
        return self._zip


def _default_stays(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("SELECT person_id, visit_occurrence_id AS stay_id, CAST(visit_start_date AS DATE) AS admit_date "
                     "FROM visit_occurrence WHERE visit_concept_id IN (9201, 262)").df()
    df["admit_date"] = pd.to_datetime(df["admit_date"])
    return df


def _clean_stays(stays: pd.DataFrame) -> pd.DataFrame:
    df = stays.copy()
    if "stay_id" not in df:
        df["stay_id"] = np.arange(len(df))
    missing = {"person_id", "admit_date"} - set(df.columns)
    if missing:
        raise ValueError(f"stays is missing column(s) {sorted(missing)}.")
    if df["stay_id"].duplicated().any():
        raise ValueError("stays.stay_id must be unique.")
    df = df[list(_KEY)].reset_index(drop=True)
    df["admit_date"] = pd.to_datetime(df["admit_date"])
    return df


# ---------------------------------------------------------------------------------------------------------------------
# Feature and feature set
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class StudyFeature:
    """One study-design feature.

    ``compute(ctx, state, fit)`` returns a DataFrame with ``stay_id`` and exactly the numeric ``columns`` (any order, any
    subset of stays; missing stays become NaN) and may update ``state`` in place when ``fit`` is true (it must raise when
    it needs state that is absent and ``fit`` is false). ``concept_ids`` pins columns to site-defined concept ids; columns
    without one get the next free id from :func:`register_custom_concepts`. ``labels`` name the columns in the concept table.
    ``params`` and ``state`` must be JSON-serialisable: they are what the manifest ships with the model.
    """
    name: str
    columns: tuple[str, ...]
    compute: Callable[[StudyContext, dict, bool], pd.DataFrame]
    description: str = ""
    requires: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    concept_ids: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"feature name {self.name!r} must be letters, digits and underscores.")
        self.columns = tuple(self.columns)
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError(f"feature {self.name!r} needs at least one distinct column.")
        stray = set(self.concept_ids) - set(self.columns)
        if stray:
            raise ValueError(f"feature {self.name!r} pins concept ids for unknown columns {sorted(stray)}.")


class FeatureSet:
    """An ordered, iteratively extendable collection of :class:`StudyFeature`.

    >>> fs = FeatureSet().add(ruca_feature(ruca_xwalk)).add(sdi_feature(sdi_tbl, cut_points=CUTS))   # doctest: +SKIP
    >>> wide = fs.compute(con, stays_from_cohort(con, "cohort"))                                    # doctest: +SKIP
    >>> fs.write(con, wide)                                  # measurement rows for FeatureExtraction   # doctest: +SKIP
    """

    def __init__(self, features: Sequence[StudyFeature] = ()) -> None:
        self._features: dict[str, StudyFeature] = {}
        self.extend(features)

    def add(self, feature: StudyFeature) -> "FeatureSet":
        """Adds a feature (chainable). Names and output columns must be unique within the set."""
        if not isinstance(feature, StudyFeature):
            raise TypeError("add() takes a StudyFeature; build one with ruca_feature(), area_feature(), sql_feature(), custom_feature() ...")
        if feature.name in self._features:
            raise ValueError(f"feature {feature.name!r} is already in the set.")
        taken = {c: n for n, f in self._features.items() for c in f.columns}
        clash = {c: taken[c] for c in feature.columns if c in taken or c in _KEY}
        if clash:
            raise ValueError(f"feature {feature.name!r} would duplicate column(s) {clash}.")
        self._features[feature.name] = feature
        return self

    def extend(self, features: Sequence[StudyFeature] | "FeatureSet") -> "FeatureSet":
        for f in (features._features.values() if isinstance(features, FeatureSet) else features):
            self.add(f)
        return self

    def remove(self, name: str) -> "FeatureSet":
        if name not in self._features:
            raise KeyError(f"no feature {name!r}; have {self.names}.")
        del self._features[name]
        return self

    @property
    def names(self) -> list[str]:
        return list(self._features)

    @property
    def columns(self) -> list[str]:
        return [c for f in self._features.values() for c in f.columns]

    def __len__(self) -> int:
        return len(self._features)

    def __contains__(self, name: str) -> bool:
        return name in self._features

    def __getitem__(self, name: str) -> StudyFeature:
        return self._features[name]

    def __iter__(self):
        return iter(self._features.values())

    # -- requirements ---------------------------------------------------------------------------------------------------
    def check(self, con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
        """``{feature: [missing OMOP tables]}`` for features whose required tables are absent (empty when all is in place)."""
        out = {}
        for f in self:
            gone = [t for t in f.requires if not _has_table(con, t)]
            if gone:
                out[f.name] = gone
        return out

    # -- compute --------------------------------------------------------------------------------------------------------
    def compute(self, con: duckdb.DuckDBPyConnection, stays: pd.DataFrame | None = None, *, fit: bool | Sequence[str] = False,
                address_table: str | None = "auto") -> pd.DataFrame:
        """One row per index stay (input order): ``person_id, stay_id, admit_date`` and every feature column.

        ``stays`` has ``person_id`` and ``admit_date`` (optional unique ``stay_id``); default every inpatient visit. Use
        :func:`stays_from_cohort` for a cohort table. ``fit`` is ``True`` (fit every feature that learns from data, the
        training reference), a list of feature names, or ``False`` (use the state already on each feature or loaded with
        :meth:`load_state`; a feature that needs state it does not have raises). Fitted state lands on the features and in
        ``result.attrs["feature_state"]``; ``result.attrs["feature_provenance"]`` holds the manifest.
        """
        missing = self.check(con)
        if missing:
            raise ValueError(f"required OMOP table(s) missing: {missing}.")
        stays_df = _default_stays(con) if stays is None else _clean_stays(stays)
        fit_names = set(self.names) if fit is True else set(fit or ())
        unknown = fit_names - set(self.names)
        if unknown:
            raise ValueError(f"fit names unknown features {sorted(unknown)}.")
        ctx = StudyContext(con, stays_df, address_table=address_table)
        out = stays_df[list(_KEY)].copy()
        for f in self:
            got = f.compute(ctx, f.state, f.name in fit_names)
            absent = set(f.columns) - set(got.columns)
            if "stay_id" not in got or absent:
                raise ValueError(f"feature {f.name!r} returned {list(got.columns)}; needs stay_id and {sorted(absent) or list(f.columns)}.")
            if got["stay_id"].duplicated().any():
                raise ValueError(f"feature {f.name!r} returned more than one row for a stay.")
            out = out.merge(got[["stay_id", *f.columns]], on="stay_id", how="left")
        out.attrs["feature_state"] = self.state()
        out.attrs["feature_provenance"] = self.manifest()
        return out

    # -- state and manifest ---------------------------------------------------------------------------------------------
    def state(self) -> dict[str, dict[str, Any]]:
        """Fitted state of every feature that has any (cut points, percentile grids, means), keyed by feature name."""
        return {f.name: json.loads(json.dumps(f.state)) for f in self if f.state}

    def load_state(self, state: Mapping[str, Any] | str | Path) -> "FeatureSet":
        """Loads fitted state from :meth:`state`, a manifest dict or a manifest JSON file, so a new site reuses the training fit."""
        if isinstance(state, (str, Path)):
            state = json.loads(Path(state).read_text(encoding="utf-8"))
        if state.get("format") == MANIFEST_FORMAT:
            state = {f["name"]: f["state"] for f in state["features"] if f.get("state")}
        for name, s in state.items():
            if name in self._features:
                self._features[name].state = json.loads(json.dumps(s))
        return self

    def manifest(self, *, concepts: Mapping[str, tuple[int, str]] | None = None) -> dict[str, Any]:
        """JSON-able description of the set: per feature its columns, requirements, parameters, fitted state and concept ids."""
        from omop_etl import __version__
        feats = []
        for f in self:
            ids = {c: (concepts[c][0] if concepts and c in concepts else f.concept_ids.get(c)) for c in f.columns}
            feats.append({"name": f.name, "description": f.description, "columns": list(f.columns), "requires": list(f.requires),
                          "params": json.loads(json.dumps(f.params)), "state": json.loads(json.dumps(f.state)),
                          "concept_ids": {c: i for c, i in ids.items() if i is not None}})
        return {"format": MANIFEST_FORMAT, "format_version": 1, "omop_duck_db_version": __version__, "features": feats}

    def export_manifest(self, path: str | Path, *, concepts: Mapping[str, tuple[int, str]] | None = None) -> dict[str, Any]:
        m = self.manifest(concepts=concepts)
        Path(path).write_text(json.dumps(m, indent=1), encoding="utf-8")
        return m

    # -- OMOP / OHDSI bridge --------------------------------------------------------------------------------------------
    def write(self, con: duckdb.DuckDBPyConnection, features: pd.DataFrame, *, registry: Any = None, visit_ids: bool = True,
              register_concepts: bool = True, type_concept_id: int = DERIVED_VALUE_TYPE_CONCEPT_ID) -> dict[str, tuple[int, str]]:
        """Writes the computed values as ``measurement`` rows (one per stay and column, dated the admission date).

        This is what makes the features visible to OHDSI tools. Returns ``{column: (concept_id, label)}``. Pinned ids
        (the geography ids for SDI and RUCA) are kept, everything else is registered through
        :func:`register_custom_concepts` (``registry`` pins consortium-wide ids). Missing values write no row.
        ``visit_ids=True`` links rows to ``visit_occurrence_id = stay_id``, right when stays are ``visit_occurrence`` rows.
        """
        need = set(_KEY) | set(self.columns)
        if need - set(features.columns):
            raise ValueError(f"features is missing column(s) {sorted(need - set(features.columns))}; pass the result of compute().")
        pinned = {c: (int(f.concept_ids[c]), f.labels.get(c, c)) for f in self for c in f.columns if c in f.concept_ids}
        rest = {c: f.labels.get(c, c) for f in self for c in f.columns if c not in f.concept_ids}
        concepts = {**pinned, **(register_custom_concepts(con, rest, registry=registry) if rest else {})}
        long = features.melt(id_vars=list(_KEY), value_vars=self.columns, var_name="feature", value_name="value")
        long = long.rename(columns={"admit_date": "date", "stay_id": "visit_occurrence_id"})
        write_custom_covariates(con, long, concepts, source="omop_etl.study_features", visit_ids=visit_ids,
                                register_concepts=register_concepts, type_concept_id=type_concept_id)
        return concepts

    def feature_extraction_r(self, concepts: Mapping[str, tuple[int, str]] | None = None, *, long_term_start_days: int = -365,
                             end_days: int = 0) -> str:
        """R code that pulls these features through OHDSI FeatureExtraction (``concepts`` is the result of :meth:`write`)."""
        ids = {c: (concepts[c][0] if concepts and c in concepts else None) for c in self.columns}
        for f in self:
            for c, i in f.concept_ids.items():
                ids[c] = ids[c] or i
        unknown = [c for c, i in ids.items() if i is None]
        if unknown:
            raise ValueError(f"no concept id for {unknown}; pass the result of write().")
        return (f"covariateSettings <- FeatureExtraction::createCovariateSettings(\n  useMeasurementValueLongTerm = TRUE,\n"
                f"  longTermStartDays = {int(long_term_start_days)}, endDays = {int(end_days)},\n"
                f"  includedCovariateConceptIds = c({', '.join(str(int(i)) for i in ids.values())}))   "
                f"# {', '.join(ids)}")


# ---------------------------------------------------------------------------------------------------------------------
# Built-in features
# ---------------------------------------------------------------------------------------------------------------------
def ruca_feature(crosswalk: Any, *, name: str = "ruca", code_99_missing: bool = False) -> StudyFeature:
    """USDA RUCA of the residence ZIP at admission: ``ruca_code`` (primary code 1-10) and ``ruca_group`` (1 metropolitan,
    2 micropolitan, 3 small town / rural, as in :func:`omop_etl.geography.ruca_category`). Same ids as
    :data:`omop_etl.geography.GEOGRAPHY_CONCEPT_IDS`, so it interoperates with ``write_geography_measurements``."""
    xwalk = read_ruca_crosswalk(crosswalk)

    def compute(ctx: StudyContext, state: dict, fit: bool) -> pd.DataFrame:
        g = ctx.zip5().merge(xwalk, on="zip5", how="left")
        cat = g["ruca_code"].map(lambda c: ruca_category(c, code_99_missing=code_99_missing))
        group = cat.map({"RUCA: Metropolitan (1.0)": 1.0, "RUCA: Micropolitan (2.0)": 2.0, "RUCA: Small Town / Rural (3+)": 3.0})
        return pd.DataFrame({"stay_id": g["stay_id"], "ruca_code": np.floor(pd.to_numeric(g["ruca_code"], errors="coerce")),
                             "ruca_group": group})
    return StudyFeature(name, ("ruca_code", "ruca_group"), compute, "Rural-Urban Commuting Area of the residence ZIP at admission",
                        requires=("person",), labels={"ruca_code": "RUCA primary code of the residence ZIP at admission",
                                                     "ruca_group": "RUCA group (1 metro, 2 micro, 3 small town/rural) at admission"},
                        concept_ids={"ruca_code": GEOGRAPHY_CONCEPT_IDS["ruca_code"]},
                        params={"code_99_missing": bool(code_99_missing)})


def sdi_feature(table: Any, cut_points: Sequence[float] | None = None, *, name: str = "sdi") -> StudyFeature:
    """Robert Graham Center SDI of the residence ZIP: ``sdi_score`` and ``sdi_quintile`` (1 least to 5 most deprived).

    The quintile uses the training cohort's fixed ``cut_points`` (four ascending scores); without them it raises unless the
    set is computed with ``fit=True`` (training reference), which stores the cuts in the feature state."""
    tbl = read_sdi_table(table)
    state = {"cut_points": validate_sdi_cut_points(cut_points)} if cut_points is not None else {}

    def compute(ctx: StudyContext, st: dict, fit: bool) -> pd.DataFrame:
        g = ctx.zip5().merge(tbl, on="zip5", how="left")
        if fit:
            st["cut_points"] = fit_sdi_cut_points(g["sdi_score"])
        if "cut_points" not in st:
            raise ValueError("sdi_feature needs the training cohort's cut_points (or fit=True when building the training reference); "
                             "local quantiles would change quintiles between sites.")
        q = assign_sdi_quintile(g["sdi_score"], st["cut_points"]).to_numpy()
        n = pd.Series(q).map({label: float(i + 1) for i, label in enumerate(SDI_LABELS)})
        return pd.DataFrame({"stay_id": g["stay_id"], "sdi_score": g["sdi_score"], "sdi_quintile": n.to_numpy()})
    return StudyFeature(name, ("sdi_score", "sdi_quintile"), compute, "Social Deprivation Index of the residence ZIP at admission",
                        requires=("person",), labels={"sdi_score": "SDI score of the residence ZIP at admission",
                                                     "sdi_quintile": "SDI quintile (1 least to 5 most deprived) at admission"},
                        concept_ids={k: GEOGRAPHY_CONCEPT_IDS[k] for k in ("sdi_score", "sdi_quintile")}, state=state)


def area_feature(name: str, table: Any, value_column: str | None = None, *, key_column: str | None = None, transform: str = "none",
                 impute: bool = False, missing_indicator: bool = True, description: str = "", label: str | None = None,
                 state: Mapping[str, Any] | None = None, geography: str = "zip5") -> StudyFeature:
    """Any ZIP-keyed area-level numeric (ACS median household income, % below poverty, ADI ...) as a patient feature.

    ``table`` is a CSV path or DataFrame with a ZIP/ZCTA column (``key_column``, default the first of zip5 / zcta / zip /
    zipcode / zcta5 ...) and the numeric ``value_column`` (default the only other column). Output columns: ``name`` (the
    transformed value; NaN where the ZIP is unmapped unless ``impute``) and, with ``missing_indicator``, ``name_missing``.

    ``transform``: ``none``; ``log1p`` (right-skewed dollars, e.g. income); ``percentile`` (position in the *training* ZIP
    distribution, 0-1, which is comparable across sites); ``zscore`` (training mean and sd). Learned parameters and the median
    used by ``impute`` are stored in the feature state, fitted only with ``fit=True`` and then reused unchanged at other sites.
    """
    if transform not in _TRANSFORMS:
        raise ValueError(f"transform must be one of {_TRANSFORMS}.")
    if geography not in ("zip5", "zip3"):
        raise ValueError("geography must be 'zip5' or 'zip3' (zip3 matches 3-digit ZIPs, as kept by de-identified extracts).")
    nd = 5 if geography == "zip5" else 3
    df = table.copy() if isinstance(table, pd.DataFrame) else pd.read_csv(Path(table), dtype=str)
    lower = {c.lower(): c for c in df.columns}
    key = key_column or next((lower[k] for k in ("zip5", "zcta5_fips", "zcta5", "zcta", "zipcode", "zip_code", "zip") if k in lower), None)
    if key is None or key not in df.columns:
        raise ValueError(f"area table needs a ZIP column (zip5, zcta, zipcode ...); found {list(df.columns)}.")
    val = value_column or (df.columns.drop(key)[0] if len(df.columns) == 2 else None)
    if val is None or val not in df.columns:
        raise ValueError("pass value_column: the table has more than one candidate column.")
    area = pd.DataFrame({"zip5": df[key].astype("string").str.strip().str.zfill(nd).str.extract(rf"(\d{{{nd}}})", expand=False),
                         "raw": pd.to_numeric(df[val], errors="coerce")}).dropna(subset=["zip5"]).drop_duplicates("zip5")
    cols = (name, f"{name}_missing") if missing_indicator else (name,)

    def apply(x: pd.Series, st: dict) -> pd.Series:
        if transform == "log1p":
            return np.log1p(x.where(x > -1))
        if transform == "percentile":
            grid = np.asarray(st["percentile_grid"], dtype=float)
            return pd.Series(np.interp(x, grid, np.linspace(0, 1, len(grid))), index=x.index).where(x.notna())
        if transform == "zscore":
            return (x - st["mean"]) / (st["sd"] or 1.0)
        return x

    def compute(ctx: StudyContext, st: dict, fit: bool) -> pd.DataFrame:
        g = (ctx.zip5() if geography == "zip5" else ctx.zip3()).rename(columns={geography: "zip5"}).merge(area, on="zip5", how="left")
        raw = g["raw"].astype(float)
        if fit:                      # learn from the stays of the training cohort, on the raw scale
            ok = raw.dropna()
            if len(ok) < 5:
                raise ValueError(f"{name}: need at least 5 mapped stays to fit the transform; got {len(ok)}.")
            if transform == "percentile":
                st["percentile_grid"] = [float(v) for v in np.quantile(ok, np.linspace(0, 1, 101))]
            elif transform == "zscore":
                st["mean"], st["sd"] = float(ok.mean()), float(ok.std(ddof=0))
            if impute:
                st["fill_value"] = float(np.nanmedian(apply(raw, st).dropna()))
        need = {"percentile": "percentile_grid", "zscore": "mean"}.get(transform)
        if (need and need not in st) or (impute and "fill_value" not in st):
            raise ValueError(f"{name}: the {transform!r} transform needs the training fit (state); pass state=... / load_state() or "
                             "fit=True when building the training reference.")
        x = apply(raw, st)
        out = pd.DataFrame({"stay_id": g["stay_id"], name: x.to_numpy()})
        if missing_indicator:
            out[f"{name}_missing"] = raw.isna().astype(float).to_numpy()
        if impute:
            out[name] = out[name].fillna(st["fill_value"])
        return out
    return StudyFeature(name, cols, compute, description or f"Area-level {val} of the residence ZIP at admission",
                        requires=("person",), labels={name: label or f"{name} ({transform}) of the residence ZIP at admission",
                                                      f"{name}_missing": f"{name} unavailable (1 = ZIP not in the area table)"},
                        params={"transform": transform, "impute": bool(impute), "missing_indicator": bool(missing_indicator),
                                "value_column": val, "geography": geography},
                        state=dict(state or {}))


def sql_feature(name: str, sql: str, columns: Sequence[str], *, requires: Sequence[str] = (), description: str = "",
                labels: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None) -> StudyFeature:
    """A feature defined by one SQL query over the OMOP tables.

    ``sql`` selects ``stay_id`` and the numeric ``columns`` from a view named ``_stays`` (``person_id, stay_id, admit_date``).
    Use only data dated before ``admit_date`` (or during the stay when the protocol says so); that is the author's contract.

    >>> sql_feature("n_prior_ed", "SELECT s.stay_id, count(*) AS n_prior_ed FROM _stays s JOIN visit_occurrence v "
    ...             "ON v.person_id = s.person_id AND v.visit_concept_id = 9203 AND v.visit_start_date < s.admit_date GROUP BY 1",
    ...             ["n_prior_ed"])  # doctest: +SKIP
    """
    def compute(ctx: StudyContext, state: dict, fit: bool) -> pd.DataFrame:
        ctx.con.register("_stays_df", ctx.stays)
        try:
            ctx.con.execute("CREATE OR REPLACE TEMP VIEW _stays AS SELECT * FROM _stays_df")
            return ctx.con.execute(sql).df()
        finally:
            ctx.con.execute("DROP VIEW IF EXISTS _stays")
            ctx.con.unregister("_stays_df")
    return StudyFeature(name, tuple(columns), compute, description, requires=tuple(requires), labels=dict(labels or {}),
                        params={"sql": sql, **(params or {})})


def custom_feature(name: str, columns: Sequence[str], fn: Callable[[duckdb.DuckDBPyConnection, pd.DataFrame], pd.DataFrame], *,
                   requires: Sequence[str] = (), description: str = "", labels: Mapping[str, str] | None = None,
                   params: Mapping[str, Any] | None = None) -> StudyFeature:
    """A feature from your own Python: ``fn(con, stays)`` returns ``stay_id`` plus the numeric ``columns``."""
    def compute(ctx: StudyContext, state: dict, fit: bool) -> pd.DataFrame:
        return fn(ctx.con, ctx.stays.copy())
    return StudyFeature(name, tuple(columns), compute, description, requires=tuple(requires), labels=dict(labels or {}),
                        params=dict(params or {}))


_CONCEPT_DOMAINS = {   # domain -> (table, concept column, date column, value column or None)
    "measurement": ("measurement", "measurement_concept_id", "measurement_date", "value_as_number"),
    "observation": ("observation", "observation_concept_id", "observation_date", "value_as_number"),
    "condition": ("condition_occurrence", "condition_concept_id", "condition_start_date", None),
    "drug": ("drug_exposure", "drug_concept_id", "drug_exposure_start_date", None),
    "procedure": ("procedure_occurrence", "procedure_concept_id", "procedure_date", None),
}
_VALUE_AGGS = {"last": "arg_max(v.val, v.dt)", "first": "arg_min(v.val, v.dt)", "mean": "avg(v.val)", "min": "min(v.val)",
               "max": "max(v.val)", "count": "count(*)", "any": "1.0"}


def concept_feature(name: str, concept_sets: Mapping[str, Sequence[int]], *, domain: str = "measurement", agg: str = "last",
                    lookback_days: int = 365, include_descendants: bool = False, missing_indicator: bool = False,
                    labels: Mapping[str, str] | None = None) -> StudyFeature:
    """Concept sets mapped to clinical feature columns: ``{"lab_glucose": [3004501, 3000483], "vital_bmi": [3038553]}``.

    Each key becomes one column holding ``agg`` of the records of any concept in its set that are dated in
    ``[admit - lookback_days, admit)`` (strictly before the admission, so nothing leaks from the stay). ``domain`` is
    measurement / observation (numeric ``value_as_number``; ``agg`` last, first, mean, min, max, count, any) or condition /
    drug / procedure (``agg`` count or any). ``count`` / ``any`` are 0 for stays with no record, value aggregates NaN
    (``missing_indicator`` adds ``<column>_missing``). ``include_descendants`` expands each set through ``concept_ancestor``.

    >>> concept_feature("labs", {"lab_glucose": [3004501], "lab_creatinine": [3016723]}, agg="last")   # doctest: +SKIP
    """
    if domain not in _CONCEPT_DOMAINS:
        raise ValueError(f"domain must be one of {sorted(_CONCEPT_DOMAINS)}.")
    table, ccol, dcol, vcol = _CONCEPT_DOMAINS[domain]
    if agg not in _VALUE_AGGS or (vcol is None and agg not in ("count", "any")):
        raise ValueError(f"agg {agg!r} is not available for the {domain!r} domain.")
    if not concept_sets or any(not len(v) for v in concept_sets.values()):
        raise ValueError("concept_sets needs at least one concept id per column.")
    sets = {str(k): [int(c) for c in v] for k, v in concept_sets.items()}
    for k in sets:
        if not k.replace("_", "").isalnum():
            raise ValueError(f"column name {k!r} must be letters, digits and underscores.")
    cols = tuple(sets) + (tuple(f"{k}_missing" for k in sets) if missing_indicator else ())
    count_like = agg in ("count", "any")

    def compute(ctx: StudyContext, state: dict, fit: bool) -> pd.DataFrame:
        con = ctx.con
        cmap = pd.DataFrame([(k, c) for k, v in sets.items() for c in v], columns=["feature", "concept_id"])
        if include_descendants and _has_table(con, "concept_ancestor"):
            con.register("_cf_seed", cmap)
            try:
                cmap = con.execute("""SELECT DISTINCT s.feature, a.descendant_concept_id AS concept_id FROM _cf_seed s
                                      JOIN concept_ancestor a ON a.ancestor_concept_id = s.concept_id""").df()
                cmap = pd.concat([cmap, pd.DataFrame([(k, c) for k, v in sets.items() for c in v], columns=["feature", "concept_id"])]
                                 ).drop_duplicates()
            finally:
                con.unregister("_cf_seed")
        val = f"m.{vcol}" if vcol else "NULL"
        need_val = "" if count_like or not vcol else f"AND m.{vcol} IS NOT NULL AND isfinite(m.{vcol})"
        con.register("_cf_stays", ctx.stays)
        con.register("_cf_map", cmap)
        try:
            long = con.execute(f"""
                SELECT s.stay_id, v.feature, {_VALUE_AGGS[agg]} AS v
                FROM _cf_stays s
                JOIN (SELECT m.person_id, k.feature, CAST(m.{dcol} AS DATE) AS dt0, CAST(m.{dcol} AS TIMESTAMP) AS dt,
                             CAST({val} AS DOUBLE) AS val
                      FROM {table} m JOIN _cf_map k ON k.concept_id = m.{ccol} WHERE TRUE {need_val}) v
                  ON v.person_id = s.person_id AND v.dt0 < CAST(s.admit_date AS DATE)
                 AND v.dt0 >= CAST(s.admit_date AS DATE) - INTERVAL ({int(lookback_days)}) DAY
                GROUP BY s.stay_id, v.feature""").df()
        finally:
            con.unregister("_cf_stays")
            con.unregister("_cf_map")
        wide = long.pivot(index="stay_id", columns="feature", values="v").reindex(index=ctx.stays["stay_id"], columns=list(sets))
        if count_like:
            wide = wide.fillna(0.0)
        out = wide.astype(float)
        if missing_indicator:
            for k in sets:
                out[f"{k}_missing"] = wide[k].isna().astype(float)
        return out.reset_index()
    lab = {c: (labels or {}).get(c, f"{c} ({agg} of concept set, {domain}, {lookback_days} d before admission)") for c in cols}
    return StudyFeature(name, cols, compute, f"Concept-set {domain} features ({agg})", requires=(table, "person"), labels=lab,
                        params={"concept_sets": sets, "domain": domain, "agg": agg, "lookback_days": int(lookback_days),
                                "include_descendants": bool(include_descendants), "missing_indicator": bool(missing_indicator)})


# ---------------------------------------------------------------------------------------------------------------------
# Registry of feature factories and named feature lists
# ---------------------------------------------------------------------------------------------------------------------
_FACTORIES: dict[str, Callable[..., StudyFeature]] = {"ruca": ruca_feature, "sdi": sdi_feature, "area": area_feature,
                                                      "sql": sql_feature, "custom": custom_feature, "concept": concept_feature}
FEATURE_LISTS: dict[str, tuple[str, ...]] = {"geography": ("ruca", "sdi")}


def register_feature_factory(kind: str, factory: Callable[..., StudyFeature], *, overwrite: bool = False) -> None:
    """Makes ``factory(**kwargs) -> StudyFeature`` available as ``make_feature(kind, ...)``; a package or study adds its own kinds here."""
    if kind in _FACTORIES and not overwrite:
        raise ValueError(f"feature kind {kind!r} is already registered; pass overwrite=True to replace it.")
    _FACTORIES[kind] = factory


def available_features() -> list[str]:
    return sorted(_FACTORIES)


def make_feature(kind: str, **kwargs: Any) -> StudyFeature:
    if kind not in _FACTORIES:
        raise KeyError(f"unknown feature kind {kind!r}; available: {available_features()}.")
    return _FACTORIES[kind](**kwargs)


def feature_list(name: str, **kwargs_by_kind: Mapping[str, Any]) -> FeatureSet:
    """A named list of features as a :class:`FeatureSet`, e.g. ``feature_list("geography", ruca={"crosswalk": x}, sdi={"table": t,
    "cut_points": cuts})``. Add more lists to :data:`FEATURE_LISTS` (``{list name: (feature kinds, ...)}``)."""
    if name not in FEATURE_LISTS:
        raise KeyError(f"unknown feature list {name!r}; available: {sorted(FEATURE_LISTS)}.")
    kinds = FEATURE_LISTS[name]
    absent = [k for k in kinds if k not in kwargs_by_kind]
    if absent:
        raise ValueError(f"feature list {name!r} needs arguments for {absent}.")
    return FeatureSet([make_feature(k, **kwargs_by_kind[k]) for k in kinds])
