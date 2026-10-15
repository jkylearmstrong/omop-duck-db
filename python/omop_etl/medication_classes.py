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
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.paths import read_csv_sql

__all__ = [
    "extract_medication_classes",
    "audit_medication_strategies",
    "audit_combination_products",
    "assert_exposure_coverage",
    "explain_medication_disagreement",
    "export_medication_crosswalk",
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


def _resolve_table(con, name: str, vocab_schema: str | None = None, vocab_db: str | None = None) -> str:
    """First reachable, non-empty copy of a vocabulary table (main, then the attached central vocabulary)."""
    schema = vocab_schema or vocab_db
    candidates = []
    if schema:
        candidates.extend([f"{schema}.{name}", f"{schema}.main.{name}"])
    candidates.extend([name, f"central_vocab.main.{name}", f"central_vocab.{name}"])
    for ref in candidates:
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


def audit_combination_products(
    con: duckdb.DuckDBPyConnection,
    class_concept_id: int | Sequence[int],
    cohort_table: str | None = None,
    vocab_schema: str | None = None,
    vocab_db: str | None = None,
) -> dict[str, Any]:
    """Audits exposures to an OMOP class/ingredient, disaggregating single-ingredient
    (plain) vs fixed-dose combination products.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Active DuckDB database connection.
    class_concept_id : int or Sequence[int]
        Concept ID(s) of interest (e.g. RxNorm ingredient ID or ATC class concept ID).
    cohort_table : Optional[str]
        Optional cohort table or view. If provided, restricts audit to patients in this cohort.
    vocab_schema : Optional[str]
        Schema or attached database containing Athena vocabulary tables.
    vocab_db : Optional[str]
        Alias for vocab_schema.

    Returns
    -------
    dict[str, Any]
        Dictionary of counts: plain_only_patients, combination_only_patients,
        overlapping_patients, total_patients, plain_only_exposures,
        combination_only_exposures, total_exposures, combination_share_patients.
    """
    v_schema = vocab_schema or vocab_db
    anc_tbl = _resolve_table(con, "concept_ancestor", v_schema)
    concept_tbl = _resolve_table(con, "concept", v_schema)

    ids = [class_concept_id] if isinstance(class_concept_id, (int, np.integer)) else list(class_concept_id)
    ids_str = ", ".join(str(int(x)) for x in ids)

    cohort_join = f"JOIN {cohort_table} coh ON coh.person_id = de.person_id" if cohort_table else ""

    summary_sql = f"""
    WITH target_descendants AS (
        SELECT DISTINCT ca.descendant_concept_id AS drug_concept_id
        FROM {anc_tbl} ca
        WHERE ca.ancestor_concept_id IN ({ids_str})
    ),
    ingredient_counts AS (
        SELECT td.drug_concept_id, count(DISTINCT ca2.ancestor_concept_id) AS n_ing
        FROM target_descendants td
        JOIN {anc_tbl} ca2 ON ca2.descendant_concept_id = td.drug_concept_id
        JOIN {concept_tbl} c ON c.concept_id = ca2.ancestor_concept_id
        WHERE c.concept_class_id = 'Ingredient'
        GROUP BY td.drug_concept_id
    ),
    classified_drugs AS (
        SELECT
            td.drug_concept_id,
            CASE WHEN COALESCE(ic.n_ing, 1) > 1 THEN 1 ELSE 0 END AS is_combination
        FROM target_descendants td
        LEFT JOIN ingredient_counts ic ON ic.drug_concept_id = td.drug_concept_id
    ),
    patient_flags AS (
        SELECT
            de.person_id,
            MAX(cd.is_combination) AS has_combo,
            MAX(CASE WHEN cd.is_combination = 0 THEN 1 ELSE 0 END) AS has_plain,
            SUM(cd.is_combination) AS combo_exp,
            SUM(CASE WHEN cd.is_combination = 0 THEN 1 ELSE 0 END) AS plain_exp
        FROM drug_exposure de
        JOIN classified_drugs cd ON cd.drug_concept_id = de.drug_concept_id
        {cohort_join}
        GROUP BY de.person_id
    )
    SELECT
        COUNT(DISTINCT person_id) AS total_patients,
        COUNT(CASE WHEN has_plain = 1 AND has_combo = 0 THEN 1 END) AS plain_only_patients,
        COUNT(CASE WHEN has_combo = 1 AND has_plain = 0 THEN 1 END) AS combination_only_patients,
        COUNT(CASE WHEN has_plain = 1 AND has_combo = 1 THEN 1 END) AS overlapping_patients,
        COALESCE(SUM(plain_exp), 0) AS plain_only_exposures,
        COALESCE(SUM(combo_exp), 0) AS combination_only_exposures,
        COALESCE(SUM(plain_exp + combo_exp), 0) AS total_exposures
    FROM patient_flags;
    """

    res = con.execute(summary_sql).fetchone()
    tot_pts = int(res[0] or 0)
    plain_pts = int(res[1] or 0)
    combo_pts = int(res[2] or 0)
    overlap_pts = int(res[3] or 0)
    plain_exp = int(res[4] or 0)
    combo_exp = int(res[5] or 0)
    tot_exp = int(res[6] or 0)

    combo_share = round((combo_pts + overlap_pts) / tot_pts, 4) if tot_pts > 0 else 0.0

    return {
        "class_concept_ids": ids,
        "total_patients": tot_pts,
        "plain_only_patients": plain_pts,
        "combination_only_patients": combo_pts,
        "overlapping_patients": overlap_pts,
        "plain_only_exposures": plain_exp,
        "combination_only_exposures": combo_exp,
        "total_exposures": tot_exp,
        "combination_share_patients": combo_share,
        "plain_only_share": round(plain_pts / tot_pts, 4) if tot_pts > 0 else 0.0,
        "overlapping_share": round(overlap_pts / tot_pts, 4) if tot_pts > 0 else 0.0,
    }


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
    on_unresolved: str = "ignore",
    exclude_forms: Sequence[str | int] | None = None,
    exclude_dose_form_concept_ids: Sequence[int] | None = None,
    apply_exclude_to_source: bool = False,
    exclude_forms_source: bool = False,
    disaggregate_combinations: bool = False,
    is_combination: bool = False,
    vocab_schema: str | None = None,
    vocab_db: str | None = None,
    route_concept_ids: Sequence[int] | None = None,
    by_type: bool = False,
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
        on_unresolved: How to handle unresolvable ATC codes / concepts with no descendants (``'ignore'``,
            ``'warn'``, or ``'raise'``).
        exclude_forms: Dosage form strings or concept IDs to exclude (e.g. ``['Ophthalmic', 'Topical']``).
        exclude_dose_form_concept_ids: Standard dose form concept IDs to exclude via 'RxNorm has dose form'.
        apply_exclude_to_source: If True, also apply exclude_forms filters to source/crosswalk matches.
        exclude_forms_source: Alias for apply_exclude_to_source.
        disaggregate_combinations: If True, outputs ``<class>_plain`` and ``<class>_combination`` flags.
        is_combination: Alias for disaggregate_combinations.
        vocab_schema: Optional schema or attached database name containing Athena vocabulary tables.
        vocab_db: Alias for vocab_schema.
        route_concept_ids: Restrict drug exposure to specific route concept IDs (e.g. systemic/oral administration).
        by_type: If True, also extract per-source-type flags (``<class>_type_<id>``).
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
    if on_unresolved not in ("ignore", "warn", "raise"):
        raise ValueError(f"on_unresolved must be one of ('ignore', 'warn', 'raise'), got {on_unresolved!r}.")
    specs = _normalize_specs(medication_specs)
    use_source = strategy in ("source", "both")
    use_standard = strategy in ("standard", "both")
    v_schema = vocab_schema or vocab_db
    filter_combo = disaggregate_combinations or is_combination
    apply_source_filter = apply_exclude_to_source or exclude_forms_source

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

    route_clause = ""
    if route_concept_ids:
        r_ids = ", ".join(str(int(x)) for x in route_concept_ids)
        route_clause = f"AND de.route_concept_id IN ({r_ids})"

    db_con.register("_med_cohort_df", base[["_row", "person_id", "index_date"] + (["end_date"] if ecol else [])])
    flags: dict[str, dict[str, set]] = {"source": {}, "standard": {}}
    flags_plain: dict[str, dict[str, set]] = {"source": {}, "standard": {}}
    flags_combo: dict[str, dict[str, set]] = {"source": {}, "standard": {}}
    flags_by_type: dict[str, dict[tuple[str, int], set]] = {"source": {}, "standard": {}}
    tmp_tables = []
    try:
        db_con.execute("CREATE OR REPLACE TEMPORARY VIEW _med_cohort AS "
            "SELECT _row, person_id, CAST(index_date AS DATE) AS index_date"
            + (", CAST(end_date AS DATE) AS end_date" if ecol else "") + " FROM _med_cohort_df")
        if use_standard:
            anc = _resolve_table(db_con, "concept_ancestor", v_schema)
            concept = None
            rows = []
            for cls, spec in specs.items():
                ids = [int(x) for x in spec["ancestor_concept_ids"] + spec["rxnorm_ingredient_ids"]]
                if spec["atc_codes"]:
                    concept = concept or _resolve_table(db_con, "concept", v_schema)
                    codes = ", ".join(_q(c) for c in spec["atc_codes"])
                    found_atc = [int(r[0]) for r in db_con.execute(
                        f"SELECT concept_id FROM {concept} WHERE vocabulary_id LIKE 'ATC%' "
                        f"AND concept_code IN ({codes})").fetchall()]
                    ids += found_atc
                    if not found_atc and on_unresolved in ("raise", "warn"):
                        msg = f"Medication class '{cls}' ATC codes {spec['atc_codes']} resolved to no concept in vocabulary."
                        if on_unresolved == "raise":
                            raise ValueError(msg)
                        import warnings
                        warnings.warn(msg, UserWarning)
                if ids:
                    rows += [(cls, i) for i in sorted(set(ids))]
                elif not spec["atc_codes"] and on_unresolved in ("raise", "warn"):
                    msg = f"Medication class '{cls}' has no ancestor concepts or ATC codes."
                    if on_unresolved == "raise":
                        raise ValueError(msg)
                    import warnings
                    warnings.warn(msg, UserWarning)

            anc_df = pd.DataFrame(rows, columns=["drug_class", "ancestor_concept_id"])
            db_con.register("_med_anc_df", anc_df)

            exclude_clause = ""
            concept = concept or _resolve_table(db_con, "concept", v_schema)
            ex_cond_parts = []

            if exclude_dose_form_concept_ids:
                cr_tbl = _resolve_table(db_con, "concept_relationship", v_schema)
                df_ids = ", ".join(str(int(x)) for x in exclude_dose_form_concept_ids)
                ex_cond_parts.append(f"""(
                    ca.descendant_concept_id IN (
                        SELECT concept_id_1 FROM {cr_tbl}
                        WHERE relationship_id = 'RxNorm has dose form'
                          AND concept_id_2 IN ({df_ids})
                    )
                    OR ca.descendant_concept_id IN ({df_ids})
                )""")

            if exclude_forms:
                cr_tbl = _resolve_table(db_con, "concept_relationship", v_schema)
                ex_form_strings = []
                ex_form_ids = []
                for f in exclude_forms:
                    if isinstance(f, (int, np.integer)) or (isinstance(f, str) and f.strip().isdigit()):
                        ex_form_ids.append(int(f))
                    else:
                        ex_form_strings.append(str(f).lower())

                if ex_form_ids:
                    df_ids = ", ".join(str(x) for x in ex_form_ids)
                    ex_cond_parts.append(f"""(
                        ca.descendant_concept_id IN (
                            SELECT concept_id_1 FROM {cr_tbl}
                            WHERE relationship_id = 'RxNorm has dose form'
                              AND concept_id_2 IN ({df_ids})
                        )
                        OR ca.descendant_concept_id IN ({df_ids})
                    )""")

                if ex_form_strings:
                    c_matches = " OR ".join(f"LOWER(c.concept_name) LIKE '%{s}%'" for s in ex_form_strings)
                    df_matches = " OR ".join(f"LOWER(df_c.concept_name) LIKE '%{s}%'" for s in ex_form_strings)
                    ex_cond_parts.append(f"""(
                        ({c_matches})
                        OR ca.descendant_concept_id IN (
                            SELECT cr_sub.concept_id_1
                            FROM {cr_tbl} cr_sub
                            JOIN {concept} df_c ON df_c.concept_id = cr_sub.concept_id_2
                            WHERE cr_sub.relationship_id = 'RxNorm has dose form'
                              AND ({df_matches})
                        )
                    )""")

            if ex_cond_parts:
                combined_ex = " OR ".join(ex_cond_parts)
                exclude_clause = f"JOIN {concept} c ON c.concept_id = ca.descendant_concept_id WHERE NOT ({combined_ex})"

            combo_sel = ""
            combo_join = ""
            if filter_combo:
                combo_join = f"""
                LEFT JOIN (
                    SELECT ca_ing.descendant_concept_id, count(DISTINCT ca_ing.ancestor_concept_id) AS n_ing
                    FROM {anc} ca_ing
                    JOIN {concept} c_ing ON c_ing.concept_id = ca_ing.ancestor_concept_id
                    WHERE c_ing.concept_class_id = 'Ingredient'
                    GROUP BY ca_ing.descendant_concept_id
                ) ic ON ic.descendant_concept_id = ca.descendant_concept_id
                """
                combo_sel = ", CASE WHEN COALESCE(ic.n_ing, 1) > 1 THEN 1 ELSE 0 END AS is_combination"

            db_con.execute(
                "CREATE OR REPLACE TEMPORARY TABLE _med_class_concepts AS "
                f"SELECT DISTINCT a.drug_class, ca.descendant_concept_id AS concept_id{combo_sel} FROM _med_anc_df a "
                f"JOIN {anc} ca ON ca.ancestor_concept_id = a.ancestor_concept_id "
                f"{combo_join} "
                f"{exclude_clause}")
            tmp_tables.append("_med_class_concepts")

            if on_unresolved in ("raise", "warn"):
                desc_counts = dict(db_con.execute("SELECT drug_class, count(*) FROM _med_class_concepts GROUP BY drug_class").fetchall())
                for cls in specs:
                    if cls not in anc_df["drug_class"].values:
                        continue
                    if desc_counts.get(cls, 0) == 0:
                        msg = f"Medication class '{cls}' resolved to no descendant concepts in concept_ancestor."
                        if on_unresolved == "raise":
                            raise ValueError(msg)
                        import warnings
                        warnings.warn(msg, UserWarning)

            type_sel = ", de.drug_type_concept_id" if by_type else ""
            combo_hit_sel = ", m.is_combination" if filter_combo else ""
            hits = db_con.execute(
                f"SELECT DISTINCT v._row, m.drug_class{combo_hit_sel}{type_sel} FROM _med_cohort v "
                "JOIN drug_exposure de ON de.person_id = v.person_id "
                "JOIN _med_class_concepts m ON m.concept_id = de.drug_concept_id "
                f"WHERE TRUE {lo} {hi} {route_clause}").fetchall()
            for hit in hits:
                row, cls = hit[0], hit[1]
                flags["standard"].setdefault(cls, set()).add(row)
                idx = 2
                if filter_combo:
                    is_c = hit[idx]
                    idx += 1
                    if is_c:
                        flags_combo["standard"].setdefault(cls, set()).add(row)
                    else:
                        flags_plain["standard"].setdefault(cls, set()).add(row)
                if by_type and len(hit) > idx and hit[idx] is not None:
                    flags_by_type["standard"].setdefault((cls, int(hit[idx])), set()).add(row)

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
                concept = _resolve_table(db_con, "concept", v_schema)
                de_cols = [c[0].lower() for c in db_con.execute("DESCRIBE SELECT * FROM drug_exposure LIMIT 0").fetchall()]
                if "drug_source_concept_id" in de_cols:
                    code_join = f"LEFT JOIN {concept} sc ON sc.concept_id = de.drug_source_concept_id"
                    code_expr = "UPPER(TRIM(sc.concept_code))"
                else:
                    code_join, code_expr = "", "NULL"
            except (ValueError, Exception):
                code_join, code_expr = "", "NULL"

            src_exclude_clause = ""
            if apply_source_filter and (exclude_forms or exclude_dose_form_concept_ids):
                cr_tbl = _resolve_table(db_con, "concept_relationship", v_schema)
                concept = _resolve_table(db_con, "concept", v_schema)
                src_ex_conds = []
                if exclude_dose_form_concept_ids:
                    df_ids = ", ".join(str(int(x)) for x in exclude_dose_form_concept_ids)
                    src_ex_conds.append(f"""(
                        de.drug_concept_id IN (
                            SELECT concept_id_1 FROM {cr_tbl}
                            WHERE relationship_id = 'RxNorm has dose form'
                              AND concept_id_2 IN ({df_ids})
                        )
                        OR de.drug_concept_id IN ({df_ids})
                    )""")
                if exclude_forms:
                    s_ex_strings = [str(f).lower() for f in exclude_forms if not str(f).isdigit()]
                    s_ex_ids = [int(f) for f in exclude_forms if str(f).isdigit()]
                    if s_ex_ids:
                        df_ids = ", ".join(str(x) for x in s_ex_ids)
                        src_ex_conds.append(f"""(
                            de.drug_concept_id IN (
                                SELECT concept_id_1 FROM {cr_tbl}
                                WHERE relationship_id = 'RxNorm has dose form'
                                  AND concept_id_2 IN ({df_ids})
                            )
                            OR de.drug_concept_id IN ({df_ids})
                        )""")
                    if s_ex_strings:
                        val_conds = " OR ".join(f"LOWER(de.drug_source_value) LIKE '%{s}%'" for s in s_ex_strings)
                        df_conds = " OR ".join(f"LOWER(df_c.concept_name) LIKE '%{s}%'" for s in s_ex_strings)
                        src_ex_conds.append(f"""(
                            ({val_conds})
                            OR de.drug_concept_id IN (
                                SELECT cr_sub.concept_id_1
                                FROM {cr_tbl} cr_sub
                                JOIN {concept} df_c ON df_c.concept_id = cr_sub.concept_id_2
                                WHERE cr_sub.relationship_id = 'RxNorm has dose form'
                                  AND ({df_conds})
                            )
                        )""")
                if src_ex_conds:
                    src_exclude_clause = f"AND NOT ({' OR '.join(src_ex_conds)})"

            type_sel = ", de.drug_type_concept_id" if by_type else ""
            hits = db_con.execute(
                f"SELECT DISTINCT v._row, x.drug_class{type_sel} FROM _med_cohort v "
                "JOIN drug_exposure de ON de.person_id = v.person_id "
                f"{code_join} "
                "JOIN _med_xw_df x ON x.source_value = UPPER(TRIM(de.drug_source_value)) "
                f"OR x.source_value = {code_expr} "
                f"WHERE TRUE {lo} {hi} {route_clause} {src_exclude_clause}").fetchall()
            for hit in hits:
                row, cls = hit[0], hit[1]
                flags["source"].setdefault(cls, set()).add(row)
                if by_type and len(hit) > 2 and hit[2] is not None:
                    flags_by_type["source"].setdefault((cls, int(hit[2])), set()).add(row)
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
            if filter_combo:
                result[f"{cls}_plain"] = np.isin(rows_index, list(flags_plain["standard"].get(cls, ()))).astype(int)
                result[f"{cls}_combination"] = np.isin(rows_index, list(flags_combo["standard"].get(cls, ()))).astype(int)
        result[cls] = result[present].max(axis=1)

    if by_type:
        all_type_keys = set(flags_by_type["source"].keys()) | set(flags_by_type["standard"].keys())
        for (cls, type_id) in sorted(all_type_keys):
            t_rows = set(flags_by_type["source"].get((cls, type_id), ())) | set(flags_by_type["standard"].get((cls, type_id), ()))
            result[f"{cls}_type_{type_id}"] = np.isin(rows_index, list(t_rows)).astype(int)

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
    min_n: int = 10,
    min_prevalence: float | Mapping[str, float] | None = None,
    explain: bool = False,
    con=None,
    medication_specs: Mapping[str, Any] | None = None,
    cohort_table: str = "cohort",
    vocab_schema: str | None = None,
    vocab_db: str | None = None,
    **explain_kwargs,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
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
        min_n: Minimum cohort size in a site to evaluate blind/prevalence reasons (default 10).
        min_prevalence: Minimum expected prevalence floor; flags if both strategies are below this.
        explain: If True, also returns disagreement explanation details (needs ``con`` and ``medication_specs``).
        con: Active DuckDB connection (required if ``explain=True``).
        medication_specs: Dict of medication specifications (required if ``explain=True``).
        cohort_table: Cohort table name (default 'cohort').
        **explain_kwargs: Forwarded to :func:`explain_medication_disagreement`.

    Returns:
        One row per (site, class) plus an ``ALL`` pooled row per class: counts (``both``, ``source_only``,
        ``standard_only``, ``neither``), ``source_prevalence``, ``standard_prevalence``, ``concordance``,
        ``kappa``, ``flagged`` and a ``reason`` string (``both_blind``, ``source_blind``, ``standard_blind``,
        ``low_kappa``, ``low_concordance``, ``prevalence_gap``, ``peer_outlier``, ``below_min_prevalence``).
        With ``explain=True``, returns a ``(audit, explanation)`` tuple.
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
        cls = r["medication_class"]
        why = []
        if r["n"] > 0:
            if r["source_prevalence"] == 0 and r["standard_prevalence"] > 0:
                why.append("source_blind")
            elif r["standard_prevalence"] == 0 and r["source_prevalence"] > 0:
                why.append("standard_blind")
            elif r["source_prevalence"] == 0 and r["standard_prevalence"] == 0:
                if min_n > 0 and r["n"] >= min_n:
                    why.append("both_blind")

            if min_prevalence is not None and (min_n == 0 or r["n"] >= min_n):
                floor = min_prevalence.get(cls, 0.0) if isinstance(min_prevalence, Mapping) else float(min_prevalence)
                if floor > 0 and (r["source_prevalence"] < floor or r["standard_prevalence"] < floor):
                    why.append("below_min_prevalence")

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

    if explain:
        if con is None or medication_specs is None:
            raise ValueError("explain=True requires con and medication_specs to be provided.")
        expl = explain_medication_disagreement(
            con, flags, medication_specs, cohort_table=cohort_table,
            classes=classes, site_col=site_col,
            vocab_schema=vocab_schema, vocab_db=vocab_db,
            **explain_kwargs
        )
        return audit, expl

    return audit


def assert_exposure_coverage(
    flags: pd.DataFrame,
    classes: Sequence[str] | None = None,
    *,
    min_n: int = 1000,
    site_col: str = "site",
    min_prevalence: float = 0.0,
) -> None:
    """Verifies that medication classes have non-zero (or expected) exposure coverage across sites.

    Raises AssertionError if any class has zero prevalence (or below min_prevalence) in a hospital
    site with at least min_n participants, preventing silent-zero data drops.

    Args:
        flags: DataFrame with exposure flags (from :func:`extract_medication_classes`).
        classes: Classes to check (default: all base class columns).
        min_n: Minimum site cohort size required to trigger assertion (default 1000).
        site_col: Site column name (default 'site').
        min_prevalence: Minimum expected prevalence threshold (default 0.0).

    Raises:
        AssertionError: If coverage is zero or below min_prevalence.
    """
    df = flags.copy()
    actual_site = site_col if site_col in df.columns else None
    if actual_site is None:
        df["_site"] = "ALL"
        actual_site = "_site"

    if classes is None:
        classes = [c for c in df.columns if not c.endswith(("_source", "_standard")) and not c.startswith("_") and c not in ("person_id", "subject_id", "index_date", "site", actual_site)]

    for site, grp in df.groupby(actual_site):
        n = len(grp)
        if n < min_n:
            continue
        for cls in classes:
            if cls in grp.columns:
                prev = float(grp[cls].mean())
            elif f"{cls}_standard" in grp.columns:
                prev = float(grp[f"{cls}_standard"].mean())
            elif f"{cls}_source" in grp.columns:
                prev = float(grp[f"{cls}_source"].mean())
            else:
                continue

            if prev <= min_prevalence or prev == 0.0:
                raise AssertionError(
                    f"assert_exposure_coverage failed for medication class '{cls}' at site '{site}': "
                    f"observed prevalence {prev:.4f} <= threshold {min_prevalence} (N={n}). "
                    f"Check vocabulary mapping and input drug_exposure completeness."
                )


def explain_medication_disagreement(
    con: duckdb.DuckDBPyConnection,
    flags: pd.DataFrame,
    medication_specs: Mapping[str, Any],
    cohort_table: str = "cohort",
    *,
    classes: Sequence[str] | None = None,
    site_col: str = "site",
    person_col: str | None = None,
    top_n: int = 10,
    min_cell_count: int = 11,
    mask_value: str = "<11",
    vocab_schema: str | None = None,
    vocab_db: str | None = None,
) -> pd.DataFrame:
    """Explains why source-crosswalk and standard OMOP strategies disagree on medication classes.

    Identifies top drug concepts and source values present in one strategy but missing in the other,
    with small-cell count suppression (<11) so output aggregates can safely leave the institution.

    Args:
        con: Active DuckDB connection.
        flags: Output of :func:`extract_medication_classes` with ``strategy='both'``.
        medication_specs: Medication class specifications dict.
        cohort_table: Cohort table name.
        classes: Classes to audit (default: classes present in flags).
        site_col: Site column name (default 'site').
        person_col: Person identifier column name (default 'person_id' or auto-detected).
        top_n: Maximum top disagreement patterns to return per site and class (default 10).
        min_cell_count: Cell count threshold for privacy suppression (default 11).
        mask_value: Suppression mask string (default '<11').
        vocab_schema: Optional schema name containing vocabulary tables.
        vocab_db: Optional attached database containing vocabulary tables.

    Returns:
        pd.DataFrame: Table with columns [site, medication_class, disagreement_type,
            drug_source_value, drug_concept_id, concept_name, concept_class_id,
            patient_count, row_count, is_suppressed].
    """
    if classes is None:
        classes = [c[: -len("_source")] for c in flags.columns
                   if c.endswith("_source") and f"{c[: -len('_source')]}_standard" in flags.columns]

    pcol = person_col or ("person_id" if "person_id" in flags.columns else "subject_id")
    actual_site = site_col if site_col in flags.columns else None

    df = flags.copy()
    if actual_site is None:
        df["_site"] = "ALL"
        actual_site = "_site"

    concept_table = _resolve_table(con, "concept", vocab_schema=vocab_schema, vocab_db=vocab_db)
    records = []

    for cls in classes:
        src_col = f"{cls}_source"
        std_col = f"{cls}_standard"
        if src_col not in df.columns or std_col not in df.columns:
            continue

        for site, grp in df.groupby(actual_site):
            # Source only
            src_only_pids = grp.loc[(grp[src_col] == 1) & (grp[std_col] == 0), pcol].unique()
            if len(src_only_pids) > 0:
                p_df = pd.DataFrame({"pid": src_only_pids})
                con.register("_disagree_pids", p_df)
                try:
                    res = con.execute(f"""
                        SELECT 
                            COALESCE(de.drug_source_value, 'None') AS drug_source_value,
                            COALESCE(de.drug_concept_id, 0) AS drug_concept_id,
                            COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
                            COALESCE(c.concept_class_id, 'None') AS concept_class_id,
                            COUNT(DISTINCT de.person_id) AS patient_count,
                            COUNT(*) AS row_count
                        FROM drug_exposure de
                        JOIN _disagree_pids dp ON dp.pid = de.person_id
                        LEFT JOIN {concept_table} c ON c.concept_id = de.drug_concept_id
                        GROUP BY 1, 2, 3, 4
                        ORDER BY patient_count DESC, row_count DESC
                        LIMIT {int(top_n)}
                    """).df()
                    for _, row in res.iterrows():
                        p_cnt = int(row["patient_count"])
                        r_cnt = int(row["row_count"])
                        suppressed = p_cnt < min_cell_count
                        records.append({
                            "site": site,
                            "medication_class": cls,
                            "disagreement_type": "source_only",
                            "drug_source_value": row["drug_source_value"],
                            "drug_concept_id": int(row["drug_concept_id"]),
                            "concept_name": row["concept_name"],
                            "concept_class_id": row["concept_class_id"],
                            "patient_count": mask_value if suppressed else p_cnt,
                            "row_count": mask_value if r_cnt < min_cell_count else r_cnt,
                            "is_suppressed": suppressed,
                        })
                finally:
                    con.unregister("_disagree_pids")

            # Standard only
            std_only_pids = grp.loc[(grp[src_col] == 0) & (grp[std_col] == 1), pcol].unique()
            if len(std_only_pids) > 0:
                p_df = pd.DataFrame({"pid": std_only_pids})
                con.register("_disagree_pids", p_df)
                try:
                    res = con.execute(f"""
                        SELECT 
                            COALESCE(de.drug_source_value, 'None') AS drug_source_value,
                            COALESCE(de.drug_concept_id, 0) AS drug_concept_id,
                            COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
                            COALESCE(c.concept_class_id, 'None') AS concept_class_id,
                            COUNT(DISTINCT de.person_id) AS patient_count,
                            COUNT(*) AS row_count
                        FROM drug_exposure de
                        JOIN _disagree_pids dp ON dp.pid = de.person_id
                        LEFT JOIN {concept_table} c ON c.concept_id = de.drug_concept_id
                        GROUP BY 1, 2, 3, 4
                        ORDER BY patient_count DESC, row_count DESC
                        LIMIT {int(top_n)}
                    """).df()
                    for _, row in res.iterrows():
                        p_cnt = int(row["patient_count"])
                        r_cnt = int(row["row_count"])
                        suppressed = p_cnt < min_cell_count
                        records.append({
                            "site": site,
                            "medication_class": cls,
                            "disagreement_type": "standard_only",
                            "drug_source_value": row["drug_source_value"],
                            "drug_concept_id": int(row["drug_concept_id"]),
                            "concept_name": row["concept_name"],
                            "concept_class_id": row["concept_class_id"],
                            "patient_count": mask_value if suppressed else p_cnt,
                            "row_count": mask_value if r_cnt < min_cell_count else r_cnt,
                            "is_suppressed": suppressed,
                        })
                finally:
                    con.unregister("_disagree_pids")

    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=[
            "site", "medication_class", "disagreement_type",
            "drug_source_value", "drug_concept_id", "concept_name",
            "concept_class_id", "patient_count", "row_count", "is_suppressed"
        ])
    return out


def export_medication_crosswalk(
    con: duckdb.DuckDBPyConnection,
    medication_specs: Mapping[str, Any],
    output_path: str | Path | None = None,
    *,
    include_retired: bool = True,
    exclude_forms: Sequence[str] | None = None,
    must_contain: Sequence[str] | None = None,
    vocab_schema: str | None = None,
    vocab_db: str | None = None,
) -> pd.DataFrame:
    """Exports OMOP medication class definitions into a standardized crosswalk file.

    Traverses ATC classes and RxNorm ingredient hierarchies to extract every descendant RxNorm concept
    code (CUI), plus historical/retired concepts that map to them via OMOP 'Maps to' relationships.

    Args:
        con: DuckDB connection with concept and concept_ancestor tables.
        medication_specs: Medication class specification dict.
        output_path: Optional file path to write exported CSV.
        include_retired: Whether to include older retired RxNorm codes mapped via 'Maps to' (default True).
        exclude_forms: Optional list of dosage form strings to exclude (e.g. 'Ophthalmic', 'Topical').
        must_contain: Optional list of required RxNorm codes or source strings; fails fast if missing.
        vocab_schema: Optional schema name containing vocabulary tables.
        vocab_db: Optional attached database containing vocabulary tables.

    Returns:
        pd.DataFrame: Crosswalk table with columns [source_value, drug_class, concept_id,
            concept_name, concept_class_id, vocabulary_id, source].
    """
    specs = _normalize_specs(medication_specs)
    anc = _resolve_table(con, "concept_ancestor", vocab_schema=vocab_schema, vocab_db=vocab_db)
    concept = _resolve_table(con, "concept", vocab_schema=vocab_schema, vocab_db=vocab_db)
    rel = _resolve_table(con, "concept_relationship", vocab_schema=vocab_schema, vocab_db=vocab_db)

    rows = []
    for cls, spec in specs.items():
        ids = [int(x) for x in spec["ancestor_concept_ids"] + spec["rxnorm_ingredient_ids"]]
        if spec["atc_codes"]:
            codes = ", ".join(_q(c) for c in spec["atc_codes"])
            ids += [int(r[0]) for r in con.execute(
                f"SELECT concept_id FROM {concept} WHERE vocabulary_id LIKE 'ATC%' "
                f"AND concept_code IN ({codes})").fetchall()]
        if ids:
            rows += [(cls, i) for i in sorted(set(ids))]

    if not rows:
        raise ValueError("No ancestor concept IDs or ATC codes could be resolved from medication_specs.")

    anc_df = pd.DataFrame(rows, columns=["drug_class", "ancestor_concept_id"])
    con.register("_exp_anc_df", anc_df)

    try:
        curr_sql = f"""
            SELECT DISTINCT 
                c.concept_code AS source_value,
                a.drug_class,
                c.concept_id,
                c.concept_name,
                c.concept_class_id,
                c.vocabulary_id,
                'current' AS source
            FROM _exp_anc_df a
            JOIN {anc} ca ON ca.ancestor_concept_id = a.ancestor_concept_id
            JOIN {concept} c ON c.concept_id = ca.descendant_concept_id
        """
        curr_df = con.execute(curr_sql).df()
        all_dfs = [curr_df]

        if include_retired and not curr_df.empty:
            desc_ids = ", ".join(str(int(i)) for i in curr_df["concept_id"].unique())
            ret_sql = f"""
                SELECT DISTINCT 
                    c_old.concept_code AS source_value,
                    a.drug_class,
                    c_old.concept_id,
                    c_old.concept_name,
                    c_old.concept_class_id,
                    c_old.vocabulary_id,
                    'maps_to' AS source
                FROM {rel} cr
                JOIN {concept} c_old ON c_old.concept_id = cr.concept_id_1
                JOIN _exp_anc_df a ON 1=1
                JOIN {anc} ca ON ca.ancestor_concept_id = a.ancestor_concept_id AND ca.descendant_concept_id = cr.concept_id_2
                WHERE cr.relationship_id = 'Maps to'
                  AND cr.concept_id_2 IN ({desc_ids})
                  AND c_old.concept_id NOT IN ({desc_ids})
            """
            try:
                ret_df = con.execute(ret_sql).df()
                if not ret_df.empty:
                    all_dfs.append(ret_df)
            except Exception:
                pass

        out = pd.concat(all_dfs, ignore_index=True).drop_duplicates()

        if exclude_forms and not out.empty:
            for form in exclude_forms:
                out = out[~out["concept_name"].str.lower().str.contains(str(form).lower(), na=False)]

        if must_contain:
            present_codes = set(out["source_value"].astype(str).str.strip().str.upper())
            missing = [c for c in must_contain if str(c).strip().upper() not in present_codes]
            if missing:
                raise ValueError(f"Export crosswalk validation failed; missing required codes: {missing}")

        if output_path:
            out_p = Path(output_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out.to_csv(out_p, index=False)

        return out
    finally:
        try:
            con.unregister("_exp_anc_df")
        except Exception:
            pass


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

