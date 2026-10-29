"""Native CIRCE / OHDSI Atlas JSON-to-DuckDB Vectorized SQL Compiler.

Parses OHDSI CIRCE cohort definition JSON (PrimaryCriteria, ConceptSets, InclusionRules,
ObservationWindow) and compiles directly into vectorized DuckDB SQL CTEs without Java,
rJava, or SqlRender dependencies.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any
import duckdb
import pandas as pd


DOMAIN_TABLE_MAP = {
    "ConditionOccurrence": {
        "table": "condition_occurrence",
        "concept_col": "condition_concept_id",
        "date_col": "condition_start_date",
        "end_date_col": "COALESCE(condition_end_date, condition_start_date + INTERVAL '1' DAY)",
    },
    "DrugExposure": {
        "table": "drug_exposure",
        "concept_col": "drug_concept_id",
        "date_col": "drug_exposure_start_date",
        "end_date_col": "COALESCE(drug_exposure_end_date, drug_exposure_start_date + INTERVAL '1' DAY)",
    },
    "ProcedureOccurrence": {
        "table": "procedure_occurrence",
        "concept_col": "procedure_concept_id",
        "date_col": "procedure_date",
        "end_date_col": "procedure_date + INTERVAL '1' DAY",
    },
    "VisitOccurrence": {
        "table": "visit_occurrence",
        "concept_col": "visit_concept_id",
        "date_col": "visit_start_date",
        "end_date_col": "visit_end_date",
    },
    "Measurement": {
        "table": "measurement",
        "concept_col": "measurement_concept_id",
        "date_col": "measurement_date",
        "end_date_col": "measurement_date + INTERVAL '1' DAY",
    },
    # an event ends the day after it starts when the table has no end date (the ATLAS convention)
    "Observation": {
        "table": "observation",
        "concept_col": "observation_concept_id",
        "date_col": "observation_date",
        "end_date_col": "observation_date + INTERVAL '1' DAY",
    },
    "DeviceExposure": {
        "table": "device_exposure",
        "concept_col": "device_concept_id",
        "date_col": "device_exposure_start_date",
        "end_date_col": "COALESCE(device_exposure_end_date, device_exposure_start_date + INTERVAL '1' DAY)",
    },
    "Death": {
        "table": "death",
        "concept_col": "cause_concept_id",
        "date_col": "death_date",
        "end_date_col": "death_date + INTERVAL '1' DAY",
    },
}


def _parse_op(op_str: str) -> str:
    op_clean = str(op_str).strip().lower()
    mapping = {
        "gte": ">=",
        ">=": ">=",
        "gt": ">",
        ">": ">",
        "lte": "<=",
        "<=": "<=",
        "lt": "<",
        "<": "<",
        "eq": "=",
        "=": "=",
        "==": "=",
        "neq": "!=",
        "!=": "!=",
        "!eq": "!=",
        "bt": "bt",
        "!bt": "!bt",
    }
    if op_clean not in mapping:
        raise NotImplementedError(f"Circe numeric operator {op_str!r} is not supported by the native compiler.")
    return mapping[op_clean]


def _compile_numeric_filter(col_expr: str, filter_dict: dict | Any) -> str:
    if not isinstance(filter_dict, dict):
        try:
            return f"{col_expr} = {float(filter_dict)}"
        except (ValueError, TypeError):
            return f"{col_expr} = '{str(filter_dict)}'"
    op = _parse_op(filter_dict.get("Op", "eq"))
    if op in ("bt", "!bt"):
        val = filter_dict.get("Value", 0)
        ext = filter_dict.get("Extent", val)
        return f"{col_expr} {'NOT ' if op == '!bt' else ''}BETWEEN {float(val)} AND {float(ext)}"
    val = filter_dict.get("Value")
    if val is not None:
        return f"{col_expr} {op} {float(val)}"
    return "1=1"


def _compile_window_bound(bound_dict: dict | None, index_date: str = "p.event_start_date") -> str | None:
    if not bound_dict or not isinstance(bound_dict, dict):
        return None
    days = bound_dict.get("Days")
    if days is None:
        return None
    coeff = int(bound_dict.get("Coeff", 1))
    offset = int(days) * coeff
    if offset == 0:
        return index_date
    elif offset > 0:
        return f"{index_date} + INTERVAL '{offset}' DAY"
    else:
        return f"{index_date} - INTERVAL '{abs(offset)}' DAY"


# attributes of a domain criterion the compiler evaluates; any other attribute that is set would narrow the official cohort
_SUPPORTED_ATTRS = {"CodesetId", "ValueAsNumber", "First", "ConditionTypeExclude", "DrugTypeExclude", "MeasurementTypeExclude",
                    "ProcedureTypeExclude", "ObservationTypeExclude", "VisitTypeExclude", "DeviceTypeExclude"}


# attributes the compiler evaluates on an entry event (primary criterion) but not on an inclusion-rule criterion
_PRIMARY_ONLY_ATTRS = {"Age", "DateAdjustment", "CorrelatedCriteria"}
_DATE_FIELDS = ("START_DATE", "END_DATE")


def _empty(v: Any) -> bool:
    return v is None or v is False or v == [] or v == {} or v == ""


def _check_criteria_supported(item: dict, where: str, primary: bool = False) -> None:
    allowed = _SUPPORTED_ATTRS | _PRIMARY_ONLY_ATTRS if primary else _SUPPORTED_ATTRS
    extra = [k for k, v in item.items() if k not in allowed and not _empty(v)]
    if extra:
        raise NotImplementedError(f"{where}: criterion attribute(s) {sorted(extra)} are not supported by the native compiler; "
                                  "run this definition with CirceR + CohortGenerator.")
    if item.get("First"):
        raise NotImplementedError(f"{where}: 'First' (earliest event only) is not supported by the native compiler.")
    if primary:
        adj = item.get("DateAdjustment") or {}
        for key in ("StartWith", "EndWith"):
            if adj.get(key) is not None and str(adj[key]).upper() not in _DATE_FIELDS:
                raise NotImplementedError(f"{where}: DateAdjustment {key} {adj[key]!r} is not supported by the native compiler.")
        if not _empty(item.get("CorrelatedCriteria")):
            _check_group_supported(item["CorrelatedCriteria"], f"{where} CorrelatedCriteria")


def _check_group_supported(expr: dict, where: str) -> None:
    """A criteria group (an inclusion rule's expression, or the CorrelatedCriteria of an entry event)."""
    if expr.get("Groups"):
        raise NotImplementedError(f"{where}: nested criteria groups are not supported by the native compiler.")
    if str(expr.get("Type", "ALL")).upper() not in ("ALL", "ANY"):
        raise NotImplementedError(f"{where}: group type {expr.get('Type')!r} is not supported.")


def _check_wrapper_supported(w: dict, where: str) -> None:
    if not isinstance(w, dict):
        return
    if w.get("RestrictVisit"):
        raise NotImplementedError(f"{where}: RestrictVisit is not supported by the native compiler.")
    if (w.get("Occurrence") or {}).get("IsDistinct"):
        raise NotImplementedError(f"{where}: distinct-count occurrences are not supported by the native compiler.")
    if not _empty(w.get("EndWindow")):
        raise NotImplementedError(f"{where}: EndWindow is not supported by the native compiler.")
    sw = w.get("StartWindow") or {}
    if sw.get("UseIndexEnd") or sw.get("UseEventEnd"):
        raise NotImplementedError(f"{where}: windows anchored on the index or event end are not supported by the native compiler.")
    crit = w.get("Criteria", w)
    for key, item in crit.items():
        if key in DOMAIN_TABLE_MAP and isinstance(item, dict):
            _check_criteria_supported(item, f"{where} {key}")


def _compile_criteria(crit_wrapper: dict, cdm_schema: str = "main") -> str | None:
    _check_wrapper_supported(crit_wrapper, "inclusion criterion")
    crit = crit_wrapper.get("Criteria", crit_wrapper)
    start_win = crit_wrapper.get("StartWindow", {})
    occ = crit_wrapper.get("Occurrence", {})
    occ_type = int(occ.get("Type", 2))
    occ_count = int(occ.get("Count", 1))

    # 1. DemographicCriteria
    if "DemographicCriteria" in crit:
        demo = crit["DemographicCriteria"]
        demo_conds = ["per.person_id = p.subject_id"]
        if "Age" in demo:
            age_expr = (
                f"date_diff('year', make_date(per.year_of_birth, "
                f"COALESCE(per.month_of_birth, 1), COALESCE(per.day_of_birth, 1)), p.event_start_date)"
            )
            demo_conds.append(_compile_numeric_filter(age_expr, demo["Age"]))
        if "Gender" in demo:
            g_items = demo["Gender"]
            g_cids = []
            if isinstance(g_items, list):
                for g in g_items:
                    if isinstance(g, dict):
                        cid = g.get("CONCEPT_ID", g.get("concept_id"))
                        if cid is not None:
                            g_cids.append(int(cid))
                    elif isinstance(g, (int, str)):
                        g_cids.append(int(g))
            elif isinstance(g_items, (int, str)):
                g_cids.append(int(g_items))
            if g_cids:
                cids_str = ", ".join(str(c) for c in g_cids)
                demo_conds.append(f"per.gender_concept_id IN ({cids_str})")

        subquery = f"SELECT 1 FROM {cdm_schema}.person per WHERE {' AND '.join(demo_conds)}"
        if (occ_type == 0 and occ_count == 0) or (occ_type == 1 and occ_count == 0):
            return f"NOT EXISTS ({subquery})"
        return f"EXISTS ({subquery})"

    # 2. Clinical Domain Criteria
    for domain_key, domain_cfg in DOMAIN_TABLE_MAP.items():
        if domain_key in crit:
            c_item = crit[domain_key]
            tbl = domain_cfg["table"]
            c_col = domain_cfg["concept_col"]
            d_col = domain_cfg["date_col"]

            conds = [f"e.person_id = p.subject_id"]
            codeset_id = c_item.get("CodesetId")
            if codeset_id is not None:
                conds.append(f"e.{c_col} IN (SELECT concept_id FROM _cs_resolved WHERE codeset_id = {int(codeset_id)})")

            if not crit_wrapper.get("IgnoreObservationPeriod", False):
                conds.append(f"EXISTS (SELECT 1 FROM {cdm_schema}.observation_period o WHERE o.person_id = p.subject_id "
                             f"AND o.observation_period_start_date <= p.event_start_date "
                             f"AND o.observation_period_end_date >= p.event_start_date "
                             f"AND CAST(e.{d_col} AS DATE) BETWEEN o.observation_period_start_date AND o.observation_period_end_date)")
            start_bound = _compile_window_bound(start_win.get("Start"))
            end_bound = _compile_window_bound(start_win.get("End"))
            if start_bound:
                conds.append(f"CAST(e.{d_col} AS DATE) >= {start_bound}")
            if end_bound:
                conds.append(f"CAST(e.{d_col} AS DATE) <= {end_bound}")

            if "ValueAsNumber" in c_item:
                conds.append(_compile_numeric_filter("e.value_as_number", c_item["ValueAsNumber"]))

            where_clause = " AND ".join(conds)
            subquery = f"SELECT 1 FROM {cdm_schema}.{tbl} e WHERE {where_clause}"

            if (occ_type == 0 and occ_count == 0) or (occ_type == 1 and occ_count == 0):
                return f"NOT EXISTS ({subquery})"
            elif occ_type == 2 and occ_count <= 1:
                return f"EXISTS ({subquery})"
            elif occ_type == 2:
                return f"(SELECT COUNT(*) FROM {cdm_schema}.{tbl} e WHERE {where_clause}) >= {occ_count}"
            elif occ_type == 0:
                return f"(SELECT COUNT(*) FROM {cdm_schema}.{tbl} e WHERE {where_clause}) = {occ_count}"
            elif occ_type == 1:
                return f"(SELECT COUNT(*) FROM {cdm_schema}.{tbl} e WHERE {where_clause}) <= {occ_count}"

    raise NotImplementedError(f"criterion type {sorted(crit)} is not supported by the native compiler; "
                              f"supported: {sorted(DOMAIN_TABLE_MAP)} and DemographicCriteria.")


def _compile_group(expr: dict, cdm_schema: str) -> str | None:
    """SQL condition (on the alias ``p`` with ``subject_id`` and ``event_start_date``) of a criteria group, or None when it has none."""
    group_type = str(expr.get("Type", "ALL")).upper()
    clauses = []
    for crit_wrapper in expr.get("CriteriaList", []):
        c_sql = _compile_criteria(crit_wrapper, cdm_schema=cdm_schema)
        if c_sql:
            clauses.append(c_sql)
    # ATLAS puts age / gender conditions of a rule in DemographicCriteriaList (evaluated on the entry date)
    for demo in expr.get("DemographicCriteriaList", []) or []:
        c_sql = _compile_criteria({"Criteria": {"DemographicCriteria": demo}}, cdm_schema=cdm_schema)
        if c_sql:
            clauses.append(c_sql)
    if not clauses:
        return None
    join_op = " AND\n                " if group_type == "ALL" else " OR\n                "
    return f"(\n                {join_op.join(clauses)}\n            )"


def _check_definition_supported(data: dict) -> None:
    """Raises for constructs that change WHO is in the cohort and that this compiler does not evaluate (silently dropping
    them gave larger cohorts than ATLAS); warns for constructs that only change the end dates."""
    if not _empty(data.get("AdditionalCriteria")):
        raise NotImplementedError("AdditionalCriteria is not supported by the native compiler; run this definition with CirceR + CohortGenerator.")
    for i, cs in enumerate(data.get("ConceptSets", [])):
        for item in cs.get("expression", {}).get("items", []):
            if item.get("includeMapped"):
                raise NotImplementedError(f"concept set {cs.get('name', i)!r}: includeMapped is not supported by the native compiler.")
    for rule in data.get("InclusionRules", []):
        _check_group_supported(rule.get("expression", {}), f"inclusion rule {rule.get('name')!r}")
    for crit in data.get("PrimaryCriteria", {}).get("CriteriaList", []):
        if not set(crit) & set(DOMAIN_TABLE_MAP):
            raise NotImplementedError(f"primary criterion type {sorted(crit)} is not supported by the native compiler; "
                                      f"supported: {sorted(DOMAIN_TABLE_MAP)}.")
        for key, item in crit.items():
            if key in DOMAIN_TABLE_MAP and isinstance(item, dict):
                _check_criteria_supported(item, f"primary criterion {key}", primary=True)
    notes = []
    strat = data.get("EndStrategy") or {}
    if strat and "DateOffset" not in strat:
        notes.append("EndStrategy " + "/".join(strat))
    if not _empty(data.get("CensoringCriteria")):
        notes.append("CensoringCriteria")
    if (data.get("ExpressionLimit") or {}).get("Type", "All") not in ("All", "First"):
        notes.append("ExpressionLimit Last")
    if notes:
        warnings.warn("native Circe compiler: " + ", ".join(notes) + " not applied; subject_id follows the definition but "
                      "cohort_end_date (and so the era collapse and cohort_start_date of repeated entries) is the entry event's end date.",
                      stacklevel=3)


def _is_file(x: str | Path) -> bool:
    """True for an existing file. JSON text is never a path: a long string raises OSError (file name too long) on Linux."""
    s = str(x)
    if s.lstrip().startswith(("{", "[")):
        return False
    try:
        return Path(s).exists()
    except OSError:
        return False


def compile_circe_to_duckdb(
    circe_json: str | dict | Path,
    target_cohort_id: int = 1,
    cdm_schema: str = "main",
    stage: str | None = None,
) -> str:
    """Compiles an OHDSI Atlas CIRCE JSON cohort definition into vectorized DuckDB SQL.

    A SUBSET of Circe is evaluated, in the ATLAS order: entry events of the Condition, Drug, Procedure, Visit, Measurement,
    Observation and Device domains (concept sets with descendants and exclusions, ``ValueAsNumber``), the observation
    window, the primary limit, inclusion rules (windows, occurrence counts, age and gender), ``DateOffset`` or default end,
    era collapse and ``ExpressionLimit``. Constructs that would change who is in the cohort (``AdditionalCriteria``,
    ``includeMapped``, nested groups, unevaluated criterion attributes, unknown operators) raise ``NotImplementedError``;
    ``CustomEra`` and censoring criteria only change end dates and warn. For other definitions use CirceR + CohortGenerator.

    Args:
        circe_json: CIRCE JSON string, dictionary, or Path to JSON file.
        target_cohort_id: Integer ID to populate in cohort_definition_id (default 1).
        cdm_schema: Schema name containing OMOP CDM tables (default 'main').
        stage: Stop after an intermediate step and return ``subject_id, event_start_date`` of its events instead of the cohort:
            ``'_primary_events_raw'`` (entry events), ``'_primary_limited'`` (inside the observation window, after the primary
            limit) or ``'_primary_qualified'`` (after the inclusion rules). :func:`omop_etl.cohort_reconcile.explain_cohort_gaps`
            uses it to find the step that dropped a patient.

    Returns:
        str: Fully executable, parameterized DuckDB SQL CTE query.
    """
    if stage is not None and stage not in ("_primary_events_raw", "_primary_limited", "_primary_qualified"):
        raise ValueError(f"unknown stage {stage!r}")
    if isinstance(circe_json, (str, Path)) and _is_file(circe_json):
        with open(str(circe_json), "r", encoding="utf-8") as f:
            data = json.load(f)
    elif isinstance(circe_json, str):
        data = json.loads(circe_json)
    elif isinstance(circe_json, dict):
        data = circe_json
    else:
        raise TypeError(f"Expected str, dict, or Path, got {type(circe_json)}")

    _check_definition_supported(data)

    # 1. Parse ConceptSets
    concept_sets = data.get("ConceptSets", [])
    cs_unions = []
    for cs in concept_sets:
        cs_id = int(cs.get("id", 0))
        items = cs.get("expression", {}).get("items", [])
        included: list[str] = []
        excluded: list[str] = []
        for item in items:
            c = item.get("concept", {})
            cid = int(c.get("CONCEPT_ID", c.get("concept_id", 0)))
            # the concept itself, plus its descendants when includeDescendants (includeMapped is not supported)
            sel = f"SELECT {cid} AS concept_id"
            if item.get("includeDescendants", False):
                sel += (f" UNION SELECT descendant_concept_id FROM {cdm_schema}.concept_ancestor "
                        f"WHERE ancestor_concept_id = {cid}")
            (excluded if item.get("isExcluded", False) else included).append(sel)
        if not included:
            continue
        body = " UNION ".join(included)
        if excluded:
            # ATLAS semantics: excluded items (and their descendants) are removed from the set
            body = f"SELECT concept_id FROM ({body}) WHERE concept_id NOT IN ({' UNION '.join(excluded)})"
        cs_unions.append(f"SELECT {cs_id} AS codeset_id, concept_id FROM ({body})")

    cs_sql = " UNION ALL\n    ".join(cs_unions) if cs_unions else "SELECT 0 AS codeset_id, 0 AS concept_id WHERE 1=0"

    # 2. Parse PrimaryCriteria
    primary_criteria = data.get("PrimaryCriteria", {})
    criteria_list = primary_criteria.get("CriteriaList", [])
    obs_window = primary_criteria.get("ObservationWindow", {})
    prior_days = int(obs_window.get("PriorDays", 0))
    post_days = int(obs_window.get("PostDays", 0))

    primary_selects = []
    for idx, crit in enumerate(criteria_list):
        for domain_key, domain_cfg in DOMAIN_TABLE_MAP.items():
            if domain_key in crit:
                c_item = crit[domain_key]
                codeset_id = c_item.get("CodesetId", None)
                tbl = domain_cfg["table"]
                c_col = domain_cfg["concept_col"]
                d_col = domain_cfg["date_col"]
                end_col = domain_cfg["end_date_col"]

                cs_join = (
                    f"JOIN _cs_resolved cs ON e.{c_col} = cs.concept_id AND cs.codeset_id = {int(codeset_id)}"
                    if codeset_id is not None
                    else ""
                )

                # DateAdjustment moves the event's start / end onto its start or end date plus an offset (ATLAS: an index on discharge)
                adj = c_item.get("DateAdjustment") or {}
                src = {"START_DATE": f"CAST(e.{d_col} AS DATE)", "END_DATE": f"CAST({end_col} AS DATE)"}
                start_expr, end_expr_ev = src["START_DATE"], src["END_DATE"]
                if not _empty(adj):
                    start_expr = (f"CAST({src[str(adj.get('StartWith', 'START_DATE')).upper()]} + "
                                  f"INTERVAL '{int(adj.get('StartOffset', 0))}' DAY AS DATE)")
                    end_expr_ev = (f"CAST({src[str(adj.get('EndWith', 'END_DATE')).upper()]} + "
                                   f"INTERVAL '{int(adj.get('EndOffset', 0))}' DAY AS DATE)")
                # a measurement entry event can carry a value filter (e.g. HbA1c >= 6.5); Age is the year of the (adjusted)
                # start date minus the year of birth, as in ATLAS
                filters = []
                person_join = ""
                if "ValueAsNumber" in c_item:
                    filters.append(_compile_numeric_filter("e.value_as_number", c_item["ValueAsNumber"]))
                if not _empty(c_item.get("Age")):
                    person_join = f"JOIN {cdm_schema}.person per ON per.person_id = e.person_id"
                    filters.append(_compile_numeric_filter(f"(year({start_expr}) - per.year_of_birth)", c_item["Age"]))
                value_where = f"WHERE {' AND '.join(filters)}" if filters else ""
                # CorrelatedCriteria qualify the entry event itself, before the primary limit, so the limit picks the earliest event that qualifies
                corr_sql = _compile_group(c_item["CorrelatedCriteria"], cdm_schema) if not _empty(c_item.get("CorrelatedCriteria")) else None
                corr_where = f"WHERE {corr_sql}" if corr_sql else ""
                primary_selects.append(f"""
                SELECT p.subject_id, p.event_start_date, p.event_end_date, p.criteria_type
                FROM (
                    SELECT
                        e.person_id AS subject_id,
                        {start_expr} AS event_start_date,
                        {end_expr_ev} AS event_end_date,
                        '{domain_key}' AS criteria_type
                    FROM {cdm_schema}.{tbl} e
                    {cs_join}
                    {person_join}
                    {value_where}
                ) p
                {corr_where}
                """)

    if not primary_selects:
        # Default fallback to all observation periods or visits if no specific domain matched
        primary_selects.append(f"""
        SELECT 
            person_id AS subject_id,
            visit_start_date AS event_start_date,
            visit_end_date AS event_end_date,
            'VisitOccurrence' AS criteria_type
        FROM {cdm_schema}.visit_occurrence
        """)

    primary_union = " UNION ALL\n".join(primary_selects)

    # 3. Observation window filter
    # an entry event must fall in an observation period (with the required days before and after), as in ATLAS
    obs_filter = f"""
        JOIN {cdm_schema}.observation_period op
          ON op.person_id = p.subject_id
         AND op.observation_period_start_date <= p.event_start_date - INTERVAL '{prior_days}' DAY
         AND op.observation_period_end_date >= p.event_start_date + INTERVAL '{post_days}' DAY
        """

    # 4. Limit rule (First or All)
    limit_rule = primary_criteria.get("PrimaryCriteriaLimit", {}).get("Type", "All")
    order_clause = ""
    if limit_rule.lower() == "first":
        order_clause = "QUALIFY ROW_NUMBER() OVER (PARTITION BY p.subject_id ORDER BY p.event_start_date ASC) = 1"

    # 5. Parse InclusionRules
    inclusion_rules = data.get("InclusionRules", [])
    rule_clauses = []
    for rule in inclusion_rules:
        rule_sql = _compile_group(rule.get("expression", {}), cdm_schema)
        if rule_sql:
            rule_clauses.append(rule_sql)

    inc_filter = ""
    if rule_clauses:
        inc_filter = "WHERE " + " AND\n          ".join(rule_clauses)
    # ATLAS order: observation window, then the primary-criteria limit, then the inclusion rules, then the end strategy, the
    # era collapse and the expression limit (QualifiedLimit is a legacy field that CirceR does not apply)
    expression_first = (data.get("ExpressionLimit") or {}).get("Type", "All") == "First"
    era_pad = int((data.get("CollapseSettings") or {}).get("EraPad", 0) or 0)

    # EndStrategy DateOffset: the end is the start (or end) date of the entry event plus the offset
    offset = (data.get("EndStrategy") or {}).get("DateOffset")
    end_expr = "event_end_date"                                      # CustomEra and other strategies: not applied (warned)
    if offset:
        base = "event_start_date" if str(offset.get("DateField", "StartDate")).lower() == "startdate" else "event_end_date"
        end_expr = f"LEAST(CAST({base} + INTERVAL '{int(offset.get('Offset', 0))}' DAY AS DATE), op_end)"
    elif not (data.get("EndStrategy") or {}):
        end_expr = "op_end"                                          # ATLAS default: the end of the observation period

    # Assemble complete SQL
    sql = f"""
    WITH _cs_resolved AS (
        {cs_sql}
    ),
    _primary_events_raw AS (
        {primary_union}
    ),
    _primary_limited AS (
        SELECT 
            p.subject_id,
            p.event_start_date,
            p.event_end_date,
            op.observation_period_end_date AS op_end
        FROM _primary_events_raw p
        {obs_filter}
        {order_clause}
    ),
    _primary_qualified AS (
        SELECT 
            p.subject_id,
            p.event_start_date,
            p.event_end_date,
            p.op_end
        FROM _primary_limited p
        {inc_filter}
    ),
    _events_ended AS (
        SELECT subject_id, event_start_date AS s, {end_expr} AS e FROM _primary_qualified
    ),
    _era_flagged AS (
        SELECT subject_id, s, e,
               CASE WHEN MAX(e) OVER (PARTITION BY subject_id ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) IS NULL
                      OR s > MAX(e) OVER (PARTITION BY subject_id ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + INTERVAL '{era_pad}' DAY
                    THEN 1 ELSE 0 END AS new_era
        FROM _events_ended
    ),
    _eras AS (
        SELECT subject_id, MIN(s) AS cohort_start_date, MAX(e) AS cohort_end_date
        FROM (SELECT *, SUM(new_era) OVER (PARTITION BY subject_id ORDER BY s, e) AS era FROM _era_flagged)
        GROUP BY subject_id, era
    )
    SELECT
        {int(target_cohort_id)} AS cohort_definition_id,
        subject_id,
        cohort_start_date,
        cohort_end_date
    FROM _eras
    {"QUALIFY ROW_NUMBER() OVER (PARTITION BY subject_id ORDER BY cohort_start_date) = 1" if expression_first else ""}
    ORDER BY subject_id, cohort_start_date;
    """
    if stage is not None:
        cut = sql.rindex("SELECT", 0, sql.rindex("AS cohort_definition_id"))
        sql = sql[:cut] + f"SELECT DISTINCT subject_id, event_start_date FROM {stage}"
    return sql.strip()


def execute_circe_cohort(
    con: duckdb.DuckDBPyConnection,
    circe_json: str | dict | Path,
    target_cohort_id: int = 1,
    cohort_table: str = "cohort",
    cdm_schema: str = "main",
) -> pd.DataFrame:
    """Compiles and executes CIRCE cohort JSON directly in DuckDB into a cohort table.

    Args:
        con: Active DuckDB connection.
        circe_json: CIRCE JSON string, dict, or Path.
        target_cohort_id: Target cohort_definition_id (default 1).
        cohort_table: Destination cohort table name (default 'cohort').
        cdm_schema: OMOP CDM schema name (default 'main').

    Returns:
        pd.DataFrame: Resulting cohort records.
    """
    sql = compile_circe_to_duckdb(circe_json, target_cohort_id=target_cohort_id, cdm_schema=cdm_schema)
    con.execute(f"CREATE TABLE IF NOT EXISTS {cohort_table} (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE);")
    con.execute(f"DELETE FROM {cohort_table} WHERE cohort_definition_id = {int(target_cohort_id)};")
    con.execute(f"INSERT INTO {cohort_table} {sql}")
    return con.execute(f"SELECT * FROM {cohort_table} WHERE cohort_definition_id = {int(target_cohort_id)}").df()


def export_to_circe_json(
    cohort_flow: Any,
    cohort_name: str = "Consortium Cohort",
    cohort_description: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Exports a CohortFlow or computable phenotype definition into compliant OHDSI Circe JSON.

    Translates base index visit concepts, lookback/follow-up observation windows,
    sequential inclusion steps, and single-visit-per-person deduplication into
    the standard OHDSI CIRCE specification for seamless round-trip integration with
    OHDSI Atlas, ATLAS Network Studies, and the OHDSI Phenotype Library.

    Args:
        cohort_flow: CohortFlow instance or object with visit_concept_ids and steps.
        cohort_name: Display name of the exported cohort (default 'Consortium Cohort').
        cohort_description: Optional narrative description of the phenotype.
        output_path: Optional file path to write the JSON specification to.

    Returns:
        dict: Compliant OHDSI Circe JSON specification dictionary.
    """
    # 1. ConceptSets
    concept_sets: list[dict[str, Any]] = []

    # Visit Concept Set (CodesetId: 0)
    visit_ids = list(getattr(cohort_flow, "visit_concept_ids", (9201, 262)))
    visit_items = [
        {
            "concept": {
                "CONCEPT_ID": int(cid),
                "CONCEPT_NAME": f"Encounter Visit Concept {cid}",
                "STANDARD_CONCEPT": "S",
                "STANDARD_CONCEPT_CAPTION": "Standard",
                "INVALID_REASON": "V",
                "INVALID_REASON_CAPTION": "Valid",
                "CONCEPT_CODE": str(cid),
                "DOMAIN_ID": "Visit",
                "VOCABULARY_ID": "Visit",
                "CONCEPT_CLASS_ID": "Visit",
            },
            "isExcluded": False,
            "includeDescendants": True,
            "includeMapped": False,
        }
        for cid in visit_ids
    ]
    concept_sets.append({
        "id": 0,
        "name": f"{cohort_name} - Index Visit Types",
        "expression": {"items": visit_items},
    })

    # 2. Primary Criteria
    follow_up = getattr(cohort_flow, "follow_up_days", None)
    post_days = int(follow_up) if follow_up is not None else 0
    one_visit = getattr(cohort_flow, "one_visit_per_person", False)

    primary_criteria = {
        "CriteriaList": [
            {
                "VisitOccurrence": {
                    "CodesetId": 0,
                }
            }
        ],
        "ObservationWindow": {
            "PriorDays": 0,
            "PostDays": post_days,
        },
        "PrimaryCriteriaLimit": {
            "Type": "First" if one_visit else "All",
        },
    }

    # 3. Inclusion Rules from CohortFlow steps
    steps = getattr(cohort_flow, "steps", [])
    inclusion_rules: list[dict[str, Any]] = []

    for idx, (name, predicate) in enumerate(steps, start=1):
        rule_crit: list[dict[str, Any]] = []

        # Check if predicate defines conditions, drugs, or visit constraints
        pred_lower = str(predicate).lower()

        # Parse potential concept IDs in predicate e.g. "IN (201820, 201826)"
        import re
        c_in_match = re.search(r"in\s*\(\s*([\d\s,]+)\s*\)", pred_lower)
        if c_in_match:
            c_ids = [int(x.strip()) for x in c_in_match.group(1).split(",") if x.strip().isdigit()]
            cs_id = len(concept_sets)
            concept_sets.append({
                "id": cs_id,
                "name": f"Rule {idx}: {name} Concepts",
                "expression": {
                    "items": [
                        {
                            "concept": {
                                "CONCEPT_ID": cid,
                                "CONCEPT_NAME": f"Concept {cid}",
                                "STANDARD_CONCEPT": "S",
                                "DOMAIN_ID": "Condition" if "condition" in pred_lower else "Drug",
                            },
                            "isExcluded": False,
                            "includeDescendants": True,
                        }
                        for cid in c_ids
                    ]
                },
            })
            domain_crit_key = "ConditionOccurrence" if "condition" in pred_lower else ("DrugExposure" if "drug" in pred_lower else "VisitOccurrence")
            rule_crit.append({
                domain_crit_key: {
                    "CodesetId": cs_id,
                },
                "Occurrence": {"Type": 2, "Count": 1},
                "StartWindow": {
                    "Start": {"Days": 0, "Coeff": -1},
                    "End": {"Days": 0, "Coeff": 1},
                },
            })
        else:
            # Default to index visit occurrence with SQL rule specification
            rule_crit.append({
                "VisitOccurrence": {
                    "CodesetId": 0,
                },
                "Occurrence": {"Type": 2, "Count": 1},
                "StartWindow": {
                    "Start": {"Days": 0, "Coeff": -1},
                    "End": {"Days": 0, "Coeff": 1},
                },
            })

        inclusion_rules.append({
            "name": name,
            "description": f"SQL Predicate: {predicate}",
            "expression": {
                "Type": "ALL",
                "CriteriaList": rule_crit,
            },
        })

    circe_data = {
        "ConceptSets": concept_sets,
        "PrimaryCriteria": primary_criteria,
        "InclusionRules": inclusion_rules,
        "QualifiedLimit": {
            "Type": "First" if one_visit else "All",
        },
        "ExpressionType": "SIMPLE_EXPRESSION",
        "Title": cohort_name,
        "Description": cohort_description or f"Exported from omop-duck-db CohortFlow ({len(steps)} inclusion rule(s)).",
    }

    if output_path is not None:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(circe_data, f, indent=2)

    return circe_data

