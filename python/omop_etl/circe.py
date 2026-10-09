"""Native CIRCE / OHDSI Atlas JSON-to-DuckDB Vectorized SQL Compiler.

Parses OHDSI CIRCE cohort definition JSON (PrimaryCriteria, ConceptSets, InclusionRules,
ObservationWindow) and compiles directly into vectorized DuckDB SQL CTEs without Java,
rJava, or SqlRender dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import duckdb
import pandas as pd


DOMAIN_TABLE_MAP = {
    "ConditionOccurrence": {
        "table": "condition_occurrence",
        "concept_col": "condition_concept_id",
        "date_col": "condition_start_date",
        "end_date_col": "COALESCE(condition_end_date, condition_start_date)",
    },
    "DrugExposure": {
        "table": "drug_exposure",
        "concept_col": "drug_concept_id",
        "date_col": "drug_exposure_start_date",
        "end_date_col": "COALESCE(drug_exposure_end_date, drug_exposure_start_date)",
    },
    "ProcedureOccurrence": {
        "table": "procedure_occurrence",
        "concept_col": "procedure_concept_id",
        "date_col": "procedure_date",
        "end_date_col": "procedure_date",
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
        "end_date_col": "measurement_date",
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
    }
    return mapping.get(op_clean, "=")


def _compile_numeric_filter(col_expr: str, filter_dict: dict | Any) -> str:
    if not isinstance(filter_dict, dict):
        try:
            return f"{col_expr} = {float(filter_dict)}"
        except (ValueError, TypeError):
            return f"{col_expr} = '{str(filter_dict)}'"
    op = _parse_op(filter_dict.get("Op", "eq"))
    if op == "between" or "Extent" in filter_dict:
        val = filter_dict.get("Value", 0)
        ext = filter_dict.get("Extent", val)
        return f"{col_expr} BETWEEN {float(val)} AND {float(ext)}"
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


def _compile_criteria(crit_wrapper: dict, cdm_schema: str = "main") -> str | None:
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

    return None


def compile_circe_to_duckdb(
    circe_json: str | dict | Path,
    target_cohort_id: int = 1,
    cdm_schema: str = "main",
) -> str:
    """Compiles an OHDSI Atlas CIRCE JSON cohort definition into vectorized DuckDB SQL.

    Args:
        circe_json: CIRCE JSON string, dictionary, or Path to JSON file.
        target_cohort_id: Integer ID to populate in cohort_definition_id (default 1).
        cdm_schema: Schema name containing OMOP CDM tables (default 'main').

    Returns:
        str: Fully executable, parameterized DuckDB SQL CTE query.
    """
    if isinstance(circe_json, (str, Path)) and Path(str(circe_json)).exists():
        with open(str(circe_json), "r", encoding="utf-8") as f:
            data = json.load(f)
    elif isinstance(circe_json, str):
        data = json.loads(circe_json)
    elif isinstance(circe_json, dict):
        data = circe_json
    else:
        raise TypeError(f"Expected str, dict, or Path, got {type(circe_json)}")

    # 1. Parse ConceptSets
    concept_sets = data.get("ConceptSets", [])
    cs_unions = []
    for cs in concept_sets:
        cs_id = cs.get("id", 0)
        items = cs.get("expression", {}).get("items", [])
        for item in items:
            c = item.get("concept", {})
            cid = c.get("CONCEPT_ID", c.get("concept_id", 0))
            include_desc = item.get("includeDescendants", False)
            is_excluded = item.get("isExcluded", False)

            if is_excluded:
                continue

            if include_desc:
                cs_unions.append(
                    f"SELECT {cs_id} AS codeset_id, descendant_concept_id AS concept_id "
                    f"FROM {cdm_schema}.concept_ancestor WHERE ancestor_concept_id = {int(cid)}"
                )
            else:
                cs_unions.append(f"SELECT {cs_id} AS codeset_id, {int(cid)} AS concept_id")

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

                primary_selects.append(f"""
                SELECT 
                    e.person_id AS subject_id,
                    CAST(e.{d_col} AS DATE) AS event_start_date,
                    CAST({end_col} AS DATE) AS event_end_date,
                    '{domain_key}' AS criteria_type
                FROM {cdm_schema}.{tbl} e
                {cs_join}
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
    obs_filter = ""
    if prior_days > 0 or post_days > 0:
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
        expr = rule.get("expression", {})
        rule_type = expr.get("Type", "ALL").upper()
        crit_list = expr.get("CriteriaList", [])
        crit_clauses = []
        for crit_wrapper in crit_list:
            c_sql = _compile_criteria(crit_wrapper, cdm_schema=cdm_schema)
            if c_sql:
                crit_clauses.append(c_sql)
        if crit_clauses:
            join_op = " AND\n                " if rule_type == "ALL" else " OR\n                "
            rule_clauses.append(f"(\n                {join_op.join(crit_clauses)}\n            )")

    inc_filter = ""
    if rule_clauses:
        inc_filter = "WHERE " + " AND\n          ".join(rule_clauses)

    # Assemble complete SQL
    sql = f"""
    WITH _cs_resolved AS (
        {cs_sql}
    ),
    _primary_events_raw AS (
        {primary_union}
    ),
    _primary_qualified AS (
        SELECT 
            p.subject_id,
            p.event_start_date,
            p.event_end_date
        FROM _primary_events_raw p
        {obs_filter}
        {inc_filter}
        {order_clause}
    )
    SELECT 
        {int(target_cohort_id)} AS cohort_definition_id,
        subject_id,
        event_start_date AS cohort_start_date,
        event_end_date AS cohort_end_date
    FROM _primary_qualified
    ORDER BY subject_id, cohort_start_date;
    """
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
