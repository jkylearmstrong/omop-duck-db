"""Pluggable dual-strategy medication classification with a cross-strategy discrepancy auditor.

Multi-site studies see sites whose drug data are coded very differently: some carry legacy source codes
(CUI / NDC strings in ``drug_source_value``), others carry fully standardized RxNorm ingredients. This module
classifies each cohort member's medication exposure two independent ways and then audits how well the two
agree, per site, so a site whose mapping is broken stands out.

* **Strategy A - source / crosswalk** (``'source'``): a user-supplied crosswalk (CSV path, DuckDB table, or
  DataFrame) maps source strings to drug classes. Matched against ``drug_source_value`` and the
  ``concept_code`` of ``drug_source_concept_id``.
* **Strategy B - OMOP standard hierarchy** (``'standard'``): ``concept_ancestor`` traversal from ATC class
  concepts (levels 3/4/5, by concept id or ATC code) or RxNorm ingredients down to ``drug_concept_id``.
* ``strategy='both'`` runs both; the combined flag is their OR.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.paths import read_csv_sql

__all__ = [
    "extract_medication_classes",
    "audit_medication_strategies",
    "MedicationExtractor",
    "extract_medication_transitions",
    "MedicationTransitions",
]

_STRATEGIES = ("source", "standard", "both")
_SOURCE_COLS = ("source_value", "source_code", "source", "code", "cui", "ndc")
_CLASS_COLS = ("drug_class", "medication_class", "class", "class_name")
_PERSON_COLS = ("subject_id", "person_id")
_INDEX_COLS = ("cohort_start_date", "index_date")
_END_COLS = ("cohort_end_date", "visit_end_date", "discharge_date", "end_date")
_WINDOWS = ("pre_admission", "inpatient", "discharge")
_SITE_COLS = ("site_anon", "site_id", "site")


def _q(s: Any) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _first_present(columns: Sequence[str], candidates: Sequence[str], what: str, given: str | None) -> str:
    by_lower = {c.lower(): c for c in columns}
    if given:
        if given.lower() not in by_lower:
            raise ValueError(f"{what} column {given!r} not in {list(columns)}.")
        return by_lower[given.lower()]
    for cand in candidates:
        if cand in by_lower:
            return by_lower[cand]
    raise ValueError(f"Could not find a {what} column among {list(columns)}; pass it explicitly.")


def _resolve_table(con, name: str) -> str:
    """First reachable, non-empty copy of a vocabulary table (main, then the attached central vocabulary)."""
    for ref in (name, f"central_vocab.main.{name}", f"central_vocab.{name}"):
        try:
            if con.execute(f"SELECT 1 FROM {ref} LIMIT 1").fetchone() is not None:
                return ref
        except (duckdb.CatalogException, duckdb.BinderException):
            continue
    raise ValueError(f"Vocabulary table {name!r} is not reachable (or empty) on this connection.")


def _normalize_specs(medication_specs: Mapping[str, Any]) -> dict[str, dict[str, list]]:
    if not medication_specs:
        raise ValueError("medication_specs must define at least one drug class.")
    out: dict[str, dict[str, list]] = {}
    for cls, spec in medication_specs.items():
        if isinstance(spec, Mapping):
            known = {"ancestor_concept_ids", "atc_codes", "rxnorm_ingredient_ids", "source_values"}
            extra = set(spec) - known
            if extra:
                raise ValueError(f"Unknown keys {sorted(extra)} in spec for {cls!r}; expected {sorted(known)}.")
            out[cls] = {k: list(spec.get(k) or []) for k in known}
        else:  # bare list -> ancestor concept ids
            out[cls] = {"ancestor_concept_ids": list(spec), "atc_codes": [], "rxnorm_ingredient_ids": [],
                        "source_values": []}
    return out


def _load_crosswalk(con, crosswalk) -> pd.DataFrame:
    if crosswalk is None:
        return pd.DataFrame(columns=["source_value", "drug_class"])
    if isinstance(crosswalk, pd.DataFrame):
        df = crosswalk.copy()
    else:
        text = str(os.fspath(crosswalk)) if isinstance(crosswalk, os.PathLike) else str(crosswalk)
        if text.lower().endswith((".csv", ".tsv", ".txt", ".csv.gz")) or os.path.exists(text):
            df = con.execute(f"SELECT * FROM {read_csv_sql(text, all_varchar=True)}").df()
        else:
            df = con.execute(f"SELECT * FROM {text}").df()
    src = _first_present(list(df.columns), _SOURCE_COLS, "crosswalk source-value", None)
    cls = _first_present(list(df.columns), _CLASS_COLS, "crosswalk drug-class", None)
    out = pd.DataFrame({"source_value": df[src].astype(str).str.strip().str.upper(),
                        "drug_class": df[cls].astype(str).str.strip()})
    return out[out["source_value"].ne("") & out["source_value"].ne("NAN")].drop_duplicates()


def extract_medication_classes(
    db_con,
    cohort_table: str,
    medication_specs: Mapping[str, Any],
    strategy: str = "both",
    *,
    crosswalk=None,
    cohort_definition_id: int | None = None,
    person_col: str | None = None,
    index_date_col: str | None = None,
    site_col: str | None = None,
    lookback_days: int | None = None,
    lookahead_days: int = 0,
    window: str | None = None,
    end_date_col: str | None = None,
    return_audit: bool = False,
    **audit_kwargs,
):
    """Flag medication-class exposure for each cohort row by source crosswalk, OMOP hierarchy, or both.

    Args:
        db_con: DuckDB connection with ``drug_exposure`` (and ``concept_ancestor`` / ``concept`` for the
            standard strategy).
        cohort_table: Table/view with a person column (``subject_id``/``person_id``) and an index date
            (``cohort_start_date``/``index_date``).
        medication_specs: ``{class_name: spec}``. ``spec`` is a dict with any of ``ancestor_concept_ids``
            (ATC class or RxNorm ingredient concept ids), ``atc_codes`` (ATC codes, resolved via ``concept``),
            ``rxnorm_ingredient_ids``, ``source_values`` (inline source strings for strategy A); or a bare
            list, taken as ``ancestor_concept_ids``.
        strategy: ``'source'``, ``'standard'`` or ``'both'``.
        crosswalk: Strategy-A mapping of source strings to classes: DataFrame, CSV path, or table name with
            columns like ``source_value`` / ``drug_class``. Combined with inline ``source_values``.
        cohort_definition_id: Restrict a stacked OMOP cohort table to one definition.
        person_col, index_date_col, site_col: Override column auto-detection. ``site_col`` (or an
            auto-detected ``site_anon``/``site_id``/``site`` column) carries through for per-site auditing.
        lookback_days, lookahead_days: Exposure window around the index date. ``None`` lookback = unbounded.
        window: Optional named temporal frame that overrides ``lookback_days``/``lookahead_days``:
            ``'pre_admission'`` = [index - ``lookback_days`` (default 365), index - 1]; ``'inpatient'`` =
            [index, stay end]; ``'discharge'`` = [stay end, stay end] (orders written at discharge).
            The stay end is ``end_date_col`` (default ``cohort_end_date``/``visit_end_date``).
        return_audit: Also return :func:`audit_medication_strategies` output (needs ``strategy='both'``).
        **audit_kwargs: Forwarded to :func:`audit_medication_strategies`.

    Returns:
        DataFrame with ``person_id``, ``index_date``, optional ``site``, and per class ``<class>_source``,
        ``<class>_standard`` (0/1, per the chosen strategy) and ``<class>`` (OR of the available flags).
        With ``return_audit=True``, a ``(flags, audit)`` tuple.
    """
    if strategy not in _STRATEGIES:
        raise ValueError(f"strategy must be one of {_STRATEGIES}, got {strategy!r}.")
    if return_audit and strategy != "both":
        raise ValueError("return_audit=True requires strategy='both'.")
    specs = _normalize_specs(medication_specs)
    use_source = strategy in ("source", "both")
    use_standard = strategy in ("standard", "both")

    cols = [r[0] for r in db_con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()]
    pcol = _first_present(cols, _PERSON_COLS, "person", person_col)
    icol = _first_present(cols, _INDEX_COLS, "index date", index_date_col)
    lower_cols = {c.lower() for c in cols}
    scol = None
    if site_col or any(c in lower_cols for c in _SITE_COLS):
        scol = _first_present(cols, _SITE_COLS, "site", site_col)
    where = ""
    if cohort_definition_id is not None:
        _first_present(cols, ("cohort_definition_id",), "cohort_definition_id", None)
        where = f"WHERE cohort_definition_id = {int(cohort_definition_id)}"

    if window is not None and window not in _WINDOWS:
        raise ValueError(f"window must be one of {_WINDOWS}, got {window!r}.")
    ecol = None
    if window in ("inpatient", "discharge"):
        ecol = _first_present(cols, _END_COLS, "stay end date", end_date_col)
    end_sel = f', CAST("{ecol}" AS DATE) AS end_date' if ecol else ""
    site_sel = f', "{scol}" AS site' if scol else ""
    base = db_con.execute(
        f'SELECT ROW_NUMBER() OVER () AS _row, "{pcol}" AS person_id, CAST("{icol}" AS DATE) AS index_date'
        f"{end_sel}{site_sel} FROM {cohort_table} {where}"
    ).df()
    result = base.drop(columns=["_row"] + (["end_date"] if ecol else [])).copy()

    d = "de.drug_exposure_start_date"
    if window == "pre_admission":
        lb = 365 if lookback_days is None else int(lookback_days)
        lo, hi = f"AND {d} >= v.index_date - {lb}", f"AND {d} <= v.index_date - 1"
    elif window == "inpatient":
        lo, hi = f"AND {d} >= v.index_date", f"AND {d} <= v.end_date"
    elif window == "discharge":
        lo, hi = f"AND {d} >= v.end_date", f"AND {d} <= v.end_date"
    else:
        lo = "" if lookback_days is None else f"AND {d} >= v.index_date - {int(lookback_days)}"
        hi = f"AND {d} <= v.index_date + {int(lookahead_days)}"

    db_con.register("_med_cohort_df", base[["_row", "person_id", "index_date"] + (["end_date"] if ecol else [])])
    flags: dict[str, dict[str, set]] = {"source": {}, "standard": {}}
    tmp_tables = []
    try:
        db_con.execute("CREATE OR REPLACE TEMPORARY VIEW _med_cohort AS "
            "SELECT _row, person_id, CAST(index_date AS DATE) AS index_date"
            + (", CAST(end_date AS DATE) AS end_date" if ecol else "") + " FROM _med_cohort_df")
        if use_standard:
            anc = _resolve_table(db_con, "concept_ancestor")
            concept = None
            rows = []
            for cls, spec in specs.items():
                ids = [int(x) for x in spec["ancestor_concept_ids"] + spec["rxnorm_ingredient_ids"]]
                if spec["atc_codes"]:
                    concept = concept or _resolve_table(db_con, "concept")
                    codes = ", ".join(_q(c) for c in spec["atc_codes"])
                    ids += [int(r[0]) for r in db_con.execute(
                        f"SELECT concept_id FROM {concept} WHERE vocabulary_id LIKE 'ATC%' "
                        f"AND concept_code IN ({codes})").fetchall()]
                if ids:
                    rows += [(cls, i) for i in sorted(set(ids))]
            anc_df = pd.DataFrame(rows, columns=["drug_class", "ancestor_concept_id"])
            db_con.register("_med_anc_df", anc_df)
            db_con.execute(
                "CREATE OR REPLACE TEMPORARY TABLE _med_class_concepts AS "
                f"SELECT DISTINCT a.drug_class, ca.descendant_concept_id AS concept_id FROM _med_anc_df a "
                f"JOIN {anc} ca ON ca.ancestor_concept_id = a.ancestor_concept_id")
            tmp_tables.append("_med_class_concepts")
            hits = db_con.execute(
                "SELECT DISTINCT v._row, m.drug_class FROM _med_cohort v "
                "JOIN drug_exposure de ON de.person_id = v.person_id "
                "JOIN _med_class_concepts m ON m.concept_id = de.drug_concept_id "
                f"WHERE TRUE {lo} {hi}").fetchall()
            for row, cls in hits:
                flags["standard"].setdefault(cls, set()).add(row)

        if use_source:
            xw = _load_crosswalk(db_con, crosswalk)
            inline = pd.DataFrame(
                [(str(v).strip().upper(), cls) for cls, spec in specs.items() for v in spec["source_values"]],
                columns=["source_value", "drug_class"])
            xw = pd.concat([xw, inline], ignore_index=True).drop_duplicates()
            unknown = sorted(set(xw["drug_class"]) - set(specs))
            if unknown:
                xw = xw[xw["drug_class"].isin(specs)]
            if xw.empty:
                raise ValueError("Strategy 'source' needs a crosswalk or inline source_values for the "
                                 "classes in medication_specs.")
            db_con.register("_med_xw_df", xw)
            try:
                concept = _resolve_table(db_con, "concept")
                code_join = f"LEFT JOIN {concept} sc ON sc.concept_id = de.drug_source_concept_id"
                code_expr = "UPPER(TRIM(sc.concept_code))"
            except ValueError:
                code_join, code_expr = "", "NULL"
            hits = db_con.execute(
                "SELECT DISTINCT v._row, x.drug_class FROM _med_cohort v "
                "JOIN drug_exposure de ON de.person_id = v.person_id "
                f"{code_join} "
                "JOIN _med_xw_df x ON x.source_value = UPPER(TRIM(de.drug_source_value)) "
                f"OR x.source_value = {code_expr} "
                f"WHERE TRUE {lo} {hi}").fetchall()
            for row, cls in hits:
                flags["source"].setdefault(cls, set()).add(row)
    finally:
        db_con.execute("DROP VIEW IF EXISTS _med_cohort")
        for t in tmp_tables:
            db_con.execute(f"DROP TABLE IF EXISTS {t}")
        for reg in ("_med_cohort_df", "_med_anc_df", "_med_xw_df"):
            try:
                db_con.unregister(reg)
            except Exception:
                pass

    rows_index = base["_row"].to_numpy()
    for cls in specs:
        present = []
        if use_source:
            result[f"{cls}_source"] = np.isin(rows_index, list(flags["source"].get(cls, ()))).astype(int)
            present.append(f"{cls}_source")
        if use_standard:
            result[f"{cls}_standard"] = np.isin(rows_index, list(flags["standard"].get(cls, ()))).astype(int)
            present.append(f"{cls}_standard")
        result[cls] = result[present].max(axis=1)

    if return_audit:
        return result, audit_medication_strategies(result, classes=list(specs), **audit_kwargs)
    return result


def _kappa(a: int, b: int, c: int, d: int) -> float:
    """Cohen's kappa for a 2x2 table: a = both, b = source only, c = standard only, d = neither."""
    n = a + b + c + d
    if n == 0:
        return float("nan")
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else float("nan")
    return (po - pe) / (1 - pe)


def audit_medication_strategies(
    flags: pd.DataFrame,
    classes: Sequence[str] | None = None,
    *,
    site_col: str = "site",
    kappa_threshold: float = 0.6,
    concordance_threshold: float = 0.9,
    prevalence_gap_threshold: float = 0.10,
    peer_kappa_gap: float = 0.25,
) -> pd.DataFrame:
    """Per-site agreement between the source-crosswalk and OMOP-hierarchy strategies.

    Args:
        flags: Output of :func:`extract_medication_classes` with ``strategy='both'``.
        classes: Classes to audit (default: every class that has both ``_source`` and ``_standard`` columns).
        site_col: Site column in ``flags``; if absent all rows are audited as one site, ``'all'``.
        kappa_threshold: Flag Cohen's kappa below this.
        concordance_threshold: Flag raw agreement below this.
        prevalence_gap_threshold: Flag ``|prevalence_source - prevalence_standard|`` above this.
        peer_kappa_gap: With >= 3 sites, flag a site whose kappa trails the median of the *other* sites by
            more than this.

    Returns:
        One row per (site, class) plus an ``ALL`` pooled row per class: counts (``both``, ``source_only``,
        ``standard_only``, ``neither``), ``source_prevalence``, ``standard_prevalence``, ``concordance``,
        ``kappa``, ``flagged`` and a ``reason`` string (``source_blind``, ``standard_blind``, ``low_kappa``,
        ``low_concordance``, ``prevalence_gap``, ``peer_outlier``).
    """
    if classes is None:
        classes = [c[: -len("_source")] for c in flags.columns
                   if c.endswith("_source") and f"{c[: -len('_source')]}_standard" in flags.columns]
    if not classes:
        raise ValueError("No class has both '<class>_source' and '<class>_standard' columns; "
                         "run extract_medication_classes(strategy='both').")
    frame = flags.copy()
    if site_col not in frame.columns:
        frame[site_col] = "all"
    records = []
    for cls in classes:
        s, t = frame[f"{cls}_source"].astype(bool), frame[f"{cls}_standard"].astype(bool)
        groups = [(site, idx) for site, idx in frame.groupby(site_col).groups.items()]
        for site, idx in groups + [("ALL", frame.index)]:
            ss, tt = s.loc[idx], t.loc[idx]
            a, b = int((ss & tt).sum()), int((ss & ~tt).sum())
            c, d = int((~ss & tt).sum()), int((~ss & ~tt).sum())
            n = a + b + c + d
            records.append({
                "site": site, "medication_class": cls, "n": n,
                "both": a, "source_only": b, "standard_only": c, "neither": d,
                "source_prevalence": (a + b) / n if n else float("nan"),
                "standard_prevalence": (a + c) / n if n else float("nan"),
                "concordance": (a + d) / n if n else float("nan"),
                "kappa": _kappa(a, b, c, d),
            })
    audit = pd.DataFrame(records)

    reasons = []
    for i, r in audit.iterrows():
        why = []
        if r["n"]:
            if r["source_prevalence"] == 0 and r["standard_prevalence"] > 0:
                why.append("source_blind")
            if r["standard_prevalence"] == 0 and r["source_prevalence"] > 0:
                why.append("standard_blind")
            if r["kappa"] == r["kappa"] and r["kappa"] < kappa_threshold and not why:
                why.append("low_kappa")
            if r["concordance"] < concordance_threshold:
                why.append("low_concordance")
            if abs(r["source_prevalence"] - r["standard_prevalence"]) > prevalence_gap_threshold:
                why.append("prevalence_gap")
        reasons.append(why)

    per_site = audit["site"] != "ALL"
    for cls in classes:
        mask = per_site & (audit["medication_class"] == cls)
        if mask.sum() >= 3:
            for i in audit.index[mask]:
                k = audit.at[i, "kappa"]
                peers = audit.loc[mask & (audit.index != i), "kappa"].dropna()
                if k == k and len(peers) and k < peers.median() - peer_kappa_gap:
                    reasons[i].append("peer_outlier")
    audit["reason"] = [",".join(dict.fromkeys(r)) for r in reasons]
    audit["flagged"] = audit["reason"].ne("")
    return audit


class MedicationExtractor:
    """Medication exposure engine: RxNorm/ATC ``concept_ancestor`` roll-up, local crosswalk, temporal frames.

    A thin, stateful front-end over :func:`extract_medication_classes` (dual track: ``'standard'`` =
    ``concept_ancestor`` join, ``'source'`` = custom NDC/CUI crosswalk, ``'both'``).

    >>> me = MedicationExtractor(con, specs, crosswalk="ndc_map.csv")
    >>> me.extract("cohort", window="pre_admission")
    """

    def __init__(self, con, medication_specs: Mapping[str, Any], *, strategy: str = "standard", crosswalk=None):
        self.con, self.specs, self.strategy, self.crosswalk = con, dict(medication_specs), strategy, crosswalk

    def extract(self, cohort_table: str, window: str | None = None, **kwargs):
        return extract_medication_classes(
            self.con, cohort_table, self.specs, self.strategy, crosswalk=self.crosswalk, window=window, **kwargs)

    def extract_all_windows(self, cohort_table: str, **kwargs) -> pd.DataFrame:
        """One frame with ``<class>__<window>`` flag columns for every temporal frame."""
        out = None
        for w in _WINDOWS:
            df = self.extract(cohort_table, window=w, **kwargs)
            flag = df[[c for c in self.specs]].add_suffix(f"__{w}")
            out = df[[c for c in df.columns if c not in self.specs and not c.endswith(("_source", "_standard"))]]                 .join(flag) if out is None else out.join(flag)
        return out


class MedicationTransitions:
    """Transitions-of-care medication trajectories and trajectory summary statistics."""

    def __init__(self, data: pd.DataFrame, classes: Sequence[str]):
        self.data = data
        self.classes = list(classes)

    def summary(self) -> pd.DataFrame:
        """Calculates cohort-level transition rates per medication class."""
        n = len(self.data)
        rows = []
        for c in self.classes:
            pre = int(self.data[f"{c}_pre_admission"].sum()) if f"{c}_pre_admission" in self.data else 0
            inp = int(self.data[f"{c}_inpatient"].sum()) if f"{c}_inpatient" in self.data else 0
            dis = int(self.data[f"{c}_discharge"].sum()) if f"{c}_discharge" in self.data else 0
            init = int(self.data[f"{c}_initiated_inpatient"].sum()) if f"{c}_initiated_inpatient" in self.data else 0
            disc = int(self.data[f"{c}_discontinued_at_discharge"].sum()) if f"{c}_discontinued_at_discharge" in self.data else 0
            cont = int(self.data[f"{c}_continued_across_stay"].sum()) if f"{c}_continued_across_stay" in self.data else 0
            rows.append({
                "medication_class": c,
                "total_patients": n,
                "pre_admission_n": pre,
                "inpatient_n": inp,
                "discharge_n": dis,
                "initiated_inpatient_n": init,
                "discontinued_at_discharge_n": disc,
                "continued_across_stay_n": cont,
                "pct_initiated": round(100.0 * init / max(1, n), 1),
                "pct_discontinued": round(100.0 * disc / max(1, pre if pre > 0 else n), 1),
                "pct_continued": round(100.0 * cont / max(1, pre if pre > 0 else n), 1),
            })
        return pd.DataFrame(rows)

    def transition_matrix(self, drug_class: str) -> pd.DataFrame:
        """2x2 contingency matrix (Pre-admission vs Discharge) for a single drug class."""
        if drug_class not in self.classes:
            raise ValueError(f"drug_class {drug_class!r} not in classes {self.classes}.")
        pre_col = f"{drug_class}_pre_admission"
        dis_col = f"{drug_class}_discharge"
        ct = pd.crosstab(self.data[pre_col], self.data[dis_col], rownames=["Pre-Admission"], colnames=["Discharge"])
        return ct

    def to_dataframe(self) -> pd.DataFrame:
        return self.data.copy()


def extract_medication_transitions(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    medication_specs: Mapping[str, Any] | None = None,
    *,
    strategy: str = "standard",
    crosswalk=None,
    lookback_days: int = 365,
    person_id_col: str | None = None,
    index_date_col: str | None = None,
    end_date_col: str | None = None,
) -> MedicationTransitions:
    """Extract medication trajectory flags across care transitions (pre-admission, inpatient, discharge).

    Identifies drug continuation, discontinuation, and new initiation per drug class:
    - ``{cls}_initiated_inpatient``: 1 if unexposed pre-admission and exposed during inpatient or discharge.
    - ``{cls}_discontinued_at_discharge``: 1 if exposed pre-admission/inpatient and not active at discharge.
    - ``{cls}_continued_across_stay``: 1 if exposed both pre-admission and at discharge.

    Also populates consortium-level summary flags:
    - ``med_initiated_inpatient``: 1 if ANY evaluated drug class was newly initiated.
    - ``med_discontinued_at_discharge``: 1 if ANY evaluated drug class was discontinued.
    - ``med_continued_across_stay``: 1 if ANY evaluated drug class was continued across stay.

    Returns:
        MedicationTransitions: Object wrapping the trajectory DataFrame with ``.summary()`` and ``.transition_matrix()``.
    """
    if medication_specs is None:
        from omop_etl.table1 import DEFAULT_MEDICATION_CONCEPTS
        specs = DEFAULT_MEDICATION_CONCEPTS
    else:
        specs = dict(medication_specs)

    extractor = MedicationExtractor(con, specs, strategy=strategy, crosswalk=crosswalk)

    # Extract across three canonical transitions
    df_pre = extractor.extract(
        cohort_table, window="pre_admission", lookback_days=lookback_days,
        person_id_col=person_id_col, index_date_col=index_date_col, end_date_col=end_date_col)
    df_inp = extractor.extract(
        cohort_table, window="inpatient",
        person_id_col=person_id_col, index_date_col=index_date_col, end_date_col=end_date_col)
    df_dis = extractor.extract(
        cohort_table, window="discharge",
        person_id_col=person_id_col, index_date_col=index_date_col, end_date_col=end_date_col)

    base_cols = [c for c in df_pre.columns if c not in specs and not c.endswith(("_source", "_standard"))]
    out = df_pre[base_cols].copy()

    any_init = pd.Series(0, index=out.index)
    any_disc = pd.Series(0, index=out.index)
    any_cont = pd.Series(0, index=out.index)

    for c in specs:
        pre_s = df_pre[c].astype(int)
        inp_s = df_inp[c].astype(int)
        dis_s = df_dis[c].astype(int)

        init_s = ((pre_s == 0) & ((inp_s == 1) | (dis_s == 1))).astype(int)
        disc_s = (((pre_s == 1) | (inp_s == 1)) & (dis_s == 0)).astype(int)
        cont_s = ((pre_s == 1) & (dis_s == 1)).astype(int)

        out[f"{c}_pre_admission"] = pre_s
        out[f"{c}_inpatient"] = inp_s
        out[f"{c}_discharge"] = dis_s
        out[f"{c}_initiated_inpatient"] = init_s
        out[f"{c}_discontinued_at_discharge"] = disc_s
        out[f"{c}_continued_across_stay"] = cont_s

        any_init |= (init_s == 1)
        any_disc |= (disc_s == 1)
        any_cont |= (cont_s == 1)

    out["med_initiated_inpatient"] = any_init.astype(int)
    out["med_discontinued_at_discharge"] = any_disc.astype(int)
    out["med_continued_across_stay"] = any_cont.astype(int)

    return MedicationTransitions(out, list(specs.keys()))

