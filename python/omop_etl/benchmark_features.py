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
import warnings
from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

__all__ = ["extract_benchmark_features", "DEFAULT_DISPOSITION_MAP"]

#: discharge disposition concept -> benchmark category. 8536 Home, 581476 Home Health, 8863 SNF, 32216 AMA (as written by
#: the PCORnet ETL). Everything else is "other"; a missing disposition is its own indicator.
DEFAULT_DISPOSITION_MAP: dict[int, str] = {8536: "home", 581476: "home_health", 8863: "snf", 32216: "ama"}
_DISPOSITION_LEVELS = ("home", "home_health", "snf", "ama", "other", "missing")

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
        warnings.warn(f"Could not attach the central vocabulary {path!r}: {exc}", UserWarning, stacklevel=3)


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


def _concept_map(con, loincs: Mapping[str, Sequence[str]], overrides: Mapping[str, Sequence[int]] | None) -> pd.DataFrame:
    """name -> concept_id rows: explicit ids first, plus LOINC codes resolved through ``concept``."""
    rows: list[tuple[str, int]] = []
    for name, ids in (overrides or {}).items():
        rows += [(name, int(i)) for i in ids]
    has_concept = bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'concept'").fetchone()[0])
    if has_concept:
        codes = [(n, c) for n, cs in loincs.items() if n not in (overrides or {}) for c in cs]
        if codes:
            con.register("_bf_codes", pd.DataFrame(codes, columns=["name", "code"]))
            try:
                got = con.execute("SELECT DISTINCT k.name, c.concept_id FROM _bf_codes k JOIN concept c "
                                  "ON c.concept_code = k.code AND c.vocabulary_id = 'LOINC'").fetchall()
            finally:
                con.unregister("_bf_codes")
            rows += [(n, int(i)) for n, i in got]
    return pd.DataFrame(rows, columns=["name", "concept_id"]).drop_duplicates()


def _measurement_stats(con, cohort: pd.DataFrame, cmap: pd.DataFrame, pre_days: int) -> pd.DataFrame:
    """Per (row_id, name): mean, latest value and count of measurements dated in [admit - pre_days, discharge]."""
    if cmap.empty:
        return pd.DataFrame(columns=["row_id", "name", "mean", "last", "n"])
    con.register("_bf_c", cohort[["row_id", "subject_id", "cohort_start_date", "cohort_end_date"]])
    con.register("_bf_map", cmap)
    try:
        return con.execute(f"""
            SELECT c.row_id, f.name, avg(m.value_as_number) AS mean,
                   arg_max(m.value_as_number, struct_pack(t := COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)),
                                                          id := m.measurement_id)) AS last,
                   count(*) AS n
            FROM _bf_c c
            JOIN measurement m ON m.person_id = c.subject_id
                 AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - {int(pre_days)}
                 AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)
            JOIN _bf_map f ON f.concept_id = m.measurement_concept_id
            WHERE m.value_as_number IS NOT NULL
            GROUP BY c.row_id, f.name
        """).df()
    finally:
        con.unregister("_bf_c")
        con.unregister("_bf_map")


def _wide(stats: pd.DataFrame, prefix: str, names: Sequence[str], n_rows: int, with_last: bool) -> pd.DataFrame:
    out = pd.DataFrame({"row_id": np.arange(n_rows, dtype="int64")}).set_index("row_id")
    for name in names:
        s = stats[stats["name"] == name].set_index("row_id")
        out[f"{prefix}{name}"] = s["mean"].reindex(out.index)
        if with_last:
            out[f"{prefix}{name}_last"] = s["last"].reindex(out.index)
    return out.reset_index()


def _derive_labs(frame: pd.DataFrame, age: pd.Series, female: pd.Series) -> list[str]:
    """CKD-EPI 2021 eGFR and anion gap for the mean and the latest representation. Returns the new column names."""
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
            frame[f"lab_anion_gap{suffix}"] = frame[parts[0]] - frame[parts[1]] - frame[parts[2]]
            new.append(f"lab_anion_gap{suffix}")
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
    medication_window: str = "pre_admission",
    lab_loincs: Mapping[str, Sequence[str]] | None = None,
    vital_loincs: Mapping[str, Sequence[str]] | None = None,
    lab_concept_ids: Mapping[str, Sequence[int]] | None = None,
    vital_concept_ids: Mapping[str, Sequence[int]] | None = None,
    derive_labs: bool = True,
    ruca_crosswalk: Any = None,
    sdi_table: Any = None,
    sdi_cut_points: Sequence[float] | None = None,
    disposition_map: Mapping[int, str] | None = None,
    sdoh_fallback: str = "indicator",
    impute: str | None = None,
    impute_values: Mapping[str, float] | None = None,
    vocabulary_db: str | os.PathLike | None = "auto",
    feature_set: Any = None,
    feature_fit: bool | Sequence[str] = False,
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
        window ``medication_window`` (default ``"pre_admission"``: the ``lookback_days`` before the admission).
    include_vitals, include_labs : bool
        Index-admission mean ``vital_<name>`` / ``lab_<name>`` **and** the latest in-stay value ``lab_<name>_last`` over
        ``[admission - lab_lookback_days, discharge]``. LOINC codes are resolved through ``concept``; pass
        ``lab_concept_ids`` / ``vital_concept_ids`` ``{name: [concept_id]}`` for databases without the vocabulary.
    derive_labs : bool, default True
        Adds ``lab_egfr`` (CKD-EPI 2021) and ``lab_anion_gap`` (Na - Cl - HCO3), each for the mean and ``_last``.
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
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                el = extract_elixhauser_comorbidities(con, view, person_col="subject_id", index_date_col="cohort_end_date",
                                                      lookback_days=lookback_days, exclude_index_diagnoses=False, end_inclusive=True)
            add("elixhauser", _align(cohort, el))
        if include_utilization:
            from omop_etl.utilization import extract_prior_utilization
            ut = extract_prior_utilization(con, view, lookback_days=lookback_days)
            add("utilization", _align(cohort, ut))
        if include_medications:
            from omop_etl.medication_classes import extract_medication_classes
            specs = dict(medication_specs) if medication_specs else _default_medication_specs()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                med = extract_medication_classes(con, view, specs, strategy="standard", person_col="subject_id",
                                                 index_date_col="cohort_start_date", end_date_col="cohort_end_date",
                                                 lookback_days=lookback_days, window=medication_window)
            add("medications", _align(cohort, med, prefix="med_", only=list(specs)))
    finally:
        con.unregister(view)

    if include_vitals:
        from omop_etl.table1 import DEFAULT_VITAL_LOINCS
        vmap = _concept_map(con, vital_loincs or DEFAULT_VITAL_LOINCS, vital_concept_ids)
        stats = _measurement_stats(con, cohort, vmap, lab_lookback_days)
        names = list(vital_loincs or DEFAULT_VITAL_LOINCS)
        skipped += [f"vital_{x}" for x in names if x not in set(vmap["name"])]
        add("vitals", _wide(stats, "vital_", [x for x in names if x in set(vmap["name"])], n, with_last=False).drop(columns=["row_id"]))
    if include_labs:
        from omop_etl.features import CORE_14_LAB_PANEL
        loincs = dict(lab_loincs or CORE_14_LAB_PANEL)
        lmap = _concept_map(con, loincs, lab_concept_ids)
        stats = _measurement_stats(con, cohort, lmap, lab_lookback_days)
        present = [x for x in loincs if x in set(lmap["name"])]
        skipped += [f"lab_{x}" for x in loincs if x not in present]
        labs = _wide(stats, "lab_", present, n, with_last=True).drop(columns=["row_id"])
        cols = list(labs.columns)
        if derive_labs:
            cols += _derive_labs(labs, age if age is not None else pd.Series(np.nan, index=labs.index),
                                 female if female is not None else pd.Series(np.nan, index=labs.index))
        add("labs", labs[cols])

    if include_disposition:
        disp = cohort["discharged_to_concept_id"] if "discharged_to_concept_id" in cohort else None
        if disp is None and "visit_occurrence_id" in cohort:
            disp = pd.Series(con.execute("SELECT v.discharged_to_concept_id FROM (SELECT unnest(?) AS vid, generate_subscripts(?, 1) AS ord) k "
                                         "LEFT JOIN visit_occurrence v ON v.visit_occurrence_id = k.vid ORDER BY k.ord",
                                         [cohort["visit_occurrence_id"].tolist()] * 2).df().iloc[:, 0].to_numpy())
        mapping = {int(k): v for k, v in (disposition_map or DEFAULT_DISPOSITION_MAP).items()}
        if disp is None:
            skipped.append("disposition")
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
            warnings.warn("include_sdoh=True but ruca_crosswalk, sdi_table or sdi_cut_points is missing: sdi_quintile and "
                          "ruca_category are NaN and sdoh_missing = 1 for every row.", UserWarning, stacklevel=2)
            add("sdoh", pd.DataFrame({"sdi_quintile": np.full(n, np.nan), "ruca_category": np.full(n, np.nan, dtype=object),
                                      "sdoh_missing": np.ones(n, dtype="int8")}))
        else:
            from omop_etl.geography import derive_geography_features
            stays = pd.DataFrame({"person_id": cohort["subject_id"], "stay_id": cohort["row_id"], "admit_date": cohort["cohort_start_date"]})
            geo = derive_geography_features(con, ruca_crosswalk, sdi_table, sdi_cut_points, stays=stays)
            geo = geo.drop_duplicates("stay_id").set_index("stay_id").reindex(cohort["row_id"])
            q = geo["sdi_quintile"].to_numpy()
            add("sdoh", pd.DataFrame({"sdi_quintile": q, "ruca_category": geo["ruca_category"].to_numpy(),
                                      "sdoh_missing": pd.isna(q).astype("int8")}))

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
