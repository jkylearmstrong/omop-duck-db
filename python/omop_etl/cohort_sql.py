"""Render and run the portable cohort SQL in ``cohorts/`` without SqlRender (FR-D7, FR-D8).

``render_cohort_sql`` implements the small SqlRender subset the shipped files use: ``{DEFAULT @p = v}`` lines,
``{@p == 'v'}?{...}:{...}`` / ``{@p}?{...}`` conditionals and ``@p`` substitution. ``translate_to_duckdb`` turns the
result (OHDSI SQL, T-SQL-flavoured) into DuckDB with ``sqlglot`` (optional: ``pip install omop-duck-db[sql]``).
SqlRender itself stays the reference implementation; use it on every other engine.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

_DEFAULT = re.compile(r"\{DEFAULT\s+@(\w+)\s*=\s*([^}]*)\}\s*", re.IGNORECASE)
_COND = re.compile(r"\{@(\w+)(?:\s*(==|!=)\s*([^}?]+?))?\}\s*\?\s*\{([^{}]*)\}(?:\s*:\s*\{([^{}]*)\})?", re.DOTALL)


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v


def render_cohort_sql(sql_or_path: str | Path, **params: Any) -> str:
    """Returns the SQL with ``{DEFAULT}`` values, conditionals and ``@name`` parameters resolved."""
    text = Path(sql_or_path).read_text(encoding="utf-8") if _looks_like_path(sql_or_path) else str(sql_or_path)
    values = {k: str(v) for k, v in params.items()}
    for name, default in _DEFAULT.findall(text):
        values.setdefault(name, _unquote(default))
    text = _DEFAULT.sub("", text)

    def cond(m: re.Match) -> str:
        name, op, rhs, yes, no = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""
        if name not in values:
            raise ValueError(f"Parameter @{name} is used in a conditional but has no value or default.")
        if op is None:
            truthy = values[name].strip().lower() not in ("", "0", "false", "null", "no")
        else:
            truthy = (values[name] == _unquote(rhs)) == (op == "==")
        return yes if truthy else no

    text = _COND.sub(cond, text)
    # longest names first so @target_cohort_id is not clobbered by a shorter parameter
    for name in sorted(values, key=len, reverse=True):
        text = text.replace(f"@{name}", values[name])
    return text


def _looks_like_path(x: str | Path) -> bool:
    return isinstance(x, Path) or (isinstance(x, str) and "\n" not in x and len(x) < 400 and x.lower().endswith(".sql"))


def translate_to_duckdb(sql: str) -> list[str]:
    """Translates OHDSI/T-SQL text to a list of DuckDB statements using ``sqlglot``."""
    try:
        import sqlglot
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError("translate_to_duckdb needs sqlglot: pip install omop-duck-db[sql]") from exc
    return [s for s in sqlglot.transpile(sql, read="tsql", write="duckdb") if s.strip()]


def run_cohort_sql(con: duckdb.DuckDBPyConnection, sql_or_path: str | Path, **params: Any) -> None:
    """Renders, translates and executes one cohort SQL file on a DuckDB connection."""
    for stmt in translate_to_duckdb(render_cohort_sql(sql_or_path, **params)):
        con.execute(stmt)


def compare_cohort_definitions(
    con: duckdb.DuckDBPyConnection,
    target_a: str | Path,
    target_b: str | Path,
    *,
    outcome_sql: str | Path | None = None,
    params_a: dict[str, Any] | None = None,
    params_b: dict[str, Any] | None = None,
    common_params: dict[str, Any] | None = None,
    followup_days: int = 30,
    cdm_database_schema: str = "main",
) -> dict[str, Any]:
    """Runs two target-cohort SQL files on one CDM and compares them side by side (FR-D8).

    Both cohorts are written to a TEMP table, so a read-only CDM works. ``outcome_sql`` (e.g. ``outcome_cohort.sql``)
    adds a binary ``outcome``: an outcome event starting in ``(start, start + followup_days]``. ``params_a`` /
    ``params_b`` override parameters per definition (e.g. ``{"followup_evidence": "observation_period_or_event"}``);
    ``common_params`` apply to both.

    Returns ``{"summary": one row per definition (n, events, outcome_rate), "overlap": cohort_overlap() table}``.
    """
    import pandas as pd

    from omop_etl.reconciliation import cohort_overlap

    followup_days = int(followup_days)
    base = {"cdm_database_schema": cdm_database_schema, "target_database_schema": "temp.main",
            "target_cohort_table": "_cmp_cohort", "followup_days": followup_days, "min_age": 18, **(common_params or {})}
    con.execute("DROP TABLE IF EXISTS temp.main._cmp_cohort")
    con.execute("CREATE TEMP TABLE _cmp_cohort (cohort_definition_id INTEGER, subject_id BIGINT, "
                "cohort_start_date DATE, cohort_end_date DATE)")
    run_cohort_sql(con, target_a, **{**base, **(params_a or {}), "target_cohort_id": 1})
    run_cohort_sql(con, target_b, **{**base, **(params_b or {}), "target_cohort_id": 2})
    has_outcome = outcome_sql is not None
    if has_outcome:
        run_cohort_sql(con, outcome_sql, **{**base, "target_cohort_id": 3})

    outcome_expr = ("CASE WHEN EXISTS (SELECT 1 FROM _cmp_cohort o WHERE o.cohort_definition_id = 3 "
                    "AND o.subject_id = t.subject_id AND o.cohort_start_date > t.cohort_start_date "
                    f"AND o.cohort_start_date <= t.cohort_start_date + INTERVAL '{followup_days}' DAY) THEN 1 ELSE 0 END"
                    if has_outcome else "NULL")
    frames = {i: con.execute(f"SELECT t.subject_id, t.cohort_start_date, {outcome_expr} AS outcome FROM _cmp_cohort t "
                             f"WHERE t.cohort_definition_id = {i}").df() for i in (1, 2)}
    summary = pd.DataFrame([
        {"definition": name, "n": len(frames[i]),
         "events": int(frames[i]["outcome"].sum()) if has_outcome else None,
         "outcome_rate": float(frames[i]["outcome"].mean()) if has_outcome and len(frames[i]) else None}
        for i, name in ((1, "A"), (2, "B"))])
    overlap = cohort_overlap(None, frames[1], frames[2], by=None, outcome_col="outcome" if has_outcome else None)
    return {"summary": summary, "overlap": overlap}
