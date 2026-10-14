"""Staged, sequential ingestion of wide measurement tables (PCORnet ``vital``, flowsheets, device streams).

Unpivoting a wide table with one giant uncommitted ``UNION ALL`` over every measure column materializes the
whole result in DuckDB's append buffer and can exhaust RAM. :func:`load_wide_measurement` instead

1. stages the source once into a keyed intermediate table (integer primary key, values already cast to DOUBLE,
   dates already normalized), then
2. inserts into ``measurement`` one concept at a time, each pass in its own committed transaction, applying
   per-measurement plausibility bounds.

Peak memory is one column's worth of rows, a failed pass loses only that pass, and the returned report says
how many rows each concept read, kept, and rejected.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from omop_etl.datetime_utils import vectorized_strptime
from omop_etl.paths import sql_path_literal

__all__ = [
    "DEFAULT_VITAL_CONCEPT_MAPPING",
    "DEFAULT_VITAL_PLAUSIBILITY_FILTERS",
    "load_wide_measurement",
]

# PCORnet VITAL columns -> OMOP standard concepts (same LOINC/unit choices as ``load_vital``).
DEFAULT_VITAL_CONCEPT_MAPPING: dict[str, dict[str, Any]] = {
    "HT": {"concept_id": 3036277, "loinc": "8302-2", "unit_concept_id": 9326, "unit": "[in_us]"},
    "WT": {"concept_id": 3025315, "loinc": "29463-7", "unit_concept_id": 8739, "unit": "[lb_av]"},
    "BMI": {"concept_id": 3038553, "loinc": "39156-5", "unit_concept_id": 9531, "unit": "kg/m2"},
    "ORIGINAL_BMI": {"concept_id": 3038553, "loinc": "39156-5", "unit_concept_id": 9531, "unit": "kg/m2"},
    "SYSTOLIC": {"concept_id": 3004249, "loinc": "8480-6", "unit_concept_id": 8876, "unit": "mm[Hg]"},
    "DIASTOLIC": {"concept_id": 3012888, "loinc": "8462-4", "unit_concept_id": 8876, "unit": "mm[Hg]"},
}

DEFAULT_VITAL_PLAUSIBILITY_FILTERS: dict[str, tuple[float, float]] = {
    "SYSTOLIC": (30, 300),
    "DIASTOLIC": (10, 200),
    "BMI": (5, 120),
    "ORIGINAL_BMI": (5, 120),
}

_MEASUREMENT_COLUMNS = (
    "measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, "
    "measurement_time, measurement_type_concept_id, operator_concept_id, value_as_number, "
    "value_as_concept_id, unit_concept_id, range_low, range_high, provider_id, visit_occurrence_id, "
    "visit_detail_id, measurement_source_value, measurement_source_concept_id, unit_source_value, "
    "unit_source_concept_id, value_source_value, measurement_event_id, meas_event_field_concept_id"
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_CONCEPT_EHR = 32817  # "EHR"


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_ident(name: str) -> str:
    if not all(_IDENT_RE.match(p) for p in name.split(".")):
        raise ValueError(f"Invalid table name {name!r}.")
    return name


def _source_relation(source: str | os.PathLike) -> str:
    """SQL relation for a table/view name, a file path (csv/tsv/parquet), or a parenthesized query."""
    s = str(source)
    if s.lstrip().startswith("("):
        return s
    suffixes = "".join(Path(s).suffixes).lower()
    if Path(s).exists() or re.search(r"\.(csv|tsv|txt|parquet)(\.gz)?$", suffixes):
        if ".parquet" in suffixes:
            return f"read_parquet({sql_path_literal(s)})"
        return f"read_csv_auto({sql_path_literal(s)}, all_varchar = true, union_by_name = true)"
    return _table_ident(s)


def _resolve_col(columns: Mapping[str, str], wanted: str, role: str) -> str:
    try:
        return columns[wanted.lower()]
    except KeyError:
        raise ValueError(
            f"{role} column {wanted!r} not found in source (columns: {', '.join(columns.values())})."
        ) from None


def _normalize_mapping(con, concept_mapping: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for col, spec in concept_mapping.items():
        entry = {"concept_id": spec} if isinstance(spec, int) else dict(spec)
        if entry.get("concept_id") is None:
            loinc = entry.get("loinc")
            found = None
            if loinc:
                try:
                    row = con.execute(
                        "SELECT COALESCE(MIN(cr.concept_id_2), MIN(CASE WHEN c.standard_concept = 'S' "
                        "THEN c.concept_id END)) FROM concept c LEFT JOIN concept_relationship cr "
                        "ON cr.concept_id_1 = c.concept_id AND cr.relationship_id = 'Maps to' "
                        "WHERE c.vocabulary_id = 'LOINC' AND c.concept_code = ?", [loinc]
                    ).fetchone()
                    found = row[0] if row else None
                except Exception:
                    found = None
            if found is None:
                raise ValueError(
                    f"concept_mapping[{col!r}] has no concept_id and the LOINC code {loinc!r} "
                    "could not be resolved through the concept table."
                )
            entry["concept_id"] = int(found)
        entry["concept_id"] = int(entry["concept_id"])
        out[col] = entry
    return out


def _resolve_coalesce_groups(
    mapping: Mapping[str, Mapping[str, Any]],
    coalesce_columns: str | Sequence[Sequence[str]] | None,
) -> list[tuple[str, ...]]:
    """Validated precedence groups (each of length >= 2) over the mapped source columns."""
    if coalesce_columns is None:
        return []
    if isinstance(coalesce_columns, str):
        if coalesce_columns != "auto":
            raise ValueError("coalesce_columns must be 'auto', None, or a sequence of column-name groups.")
        by_concept: dict[int, list[str]] = {}
        for col, entry in mapping.items():
            by_concept.setdefault(entry["concept_id"], []).append(col)
        return [tuple(cols) for cols in by_concept.values() if len(cols) > 1]
    groups, seen = [], set()
    for group in coalesce_columns:
        group = tuple(group)
        if len(group) < 2:
            raise ValueError(f"coalesce group {group!r} needs at least two columns.")
        for col in group:
            if col not in mapping:
                raise ValueError(f"coalesce column {col!r} is not in concept_mapping.")
            if col in seen:
                raise ValueError(f"coalesce column {col!r} appears in more than one group.")
            seen.add(col)
        if len({mapping[c]["concept_id"] for c in group}) != 1:
            raise ValueError(f"coalesce group {group!r} maps to more than one concept_id.")
        groups.append(group)
    return groups


def load_wide_measurement(
    con,
    source: str | os.PathLike,
    concept_mapping: Mapping[str, Any],
    *,
    person_col: str = "PATID",
    date_col: str = "MEASURE_DATE",
    time_col: str | None = "MEASURE_TIME",
    visit_col: str | None = "ENCOUNTERID",
    staged_table_name: str = "_staged_wide_measurement",
    batch_execution_mode: str = "sequential",
    value_plausibility_filters: Mapping[str, tuple[float | None, float | None]] | None = None,
    person_id_is_cdm_id: bool = False,
    measurement_type_concept_id: int = _TYPE_CONCEPT_EHR,
    target_table: str = "measurement",
    datetime_formats: list[str] | None = None,
    datetime_on_error: str = "null",
    keep_staged: bool = False,
    missing_columns: str = "raise",
    coalesce_columns: str | Sequence[Sequence[str]] | None = "auto",
) -> pd.DataFrame:
    """Load a wide measurement table (one column per measure) into ``measurement`` in staged passes.

    Args:
        con: DuckDB connection holding the OMOP CDM (``person``, ``visit_occurrence``, ``measurement``).
        source: Table/view name, CSV/TSV/Parquet path, or a parenthesized query.
        concept_mapping: ``{source_column: {"concept_id": int, "loinc": str, "unit_concept_id": int,
            "unit": str}}``. A bare int is shorthand for ``{"concept_id": int}``. If ``concept_id`` is
            absent the ``loinc`` code is resolved through ``concept`` / ``concept_relationship``.
            See :data:`DEFAULT_VITAL_CONCEPT_MAPPING`.
        person_col, date_col, time_col, visit_col: Source column names (case-insensitive). ``time_col`` and
            ``visit_col`` may be ``None``.
        staged_table_name: Name of the intermediate table (integer primary key ``stage_id``).
        batch_execution_mode: ``'sequential'`` (default; one committed single-concept insert per pass) or
            ``'single'`` (all passes in one transaction, committed at the end; only for small inputs).
        value_plausibility_filters: ``{source_column: (min, max)}`` inclusive clinical bounds; either bound
            may be ``None``. Out-of-range values are dropped and counted, not inserted. A ``"min"``/``"max"``
            inside a mapping entry is used when the column has no filter here.
            See :data:`DEFAULT_VITAL_PLAUSIBILITY_FILTERS`.
        person_id_is_cdm_id: Treat ``person_col`` as ``person.person_id`` instead of the source patient id
            (matched to ``person.person_source_value``).
        measurement_type_concept_id: Type concept stamped on every row.
        target_table: Destination (a ``measurement``-shaped table).
        datetime_formats, datetime_on_error: Passed to :func:`omop_etl.datetime_utils.vectorized_strptime`.
        keep_staged: Keep the staging table after loading (default: drop it).
        missing_columns: ``'raise'`` (default) if a mapped column is absent from the source, or ``'skip'`` to
            ignore it (useful with :data:`DEFAULT_VITAL_CONCEPT_MAPPING` on partial extracts).
        coalesce_columns: Source columns that carry the same measure (e.g. PCORnet ``BMI`` and
            ``ORIGINAL_BMI``) and must yield ONE measurement row per source row instead of one each.
            ``[("BMI", "ORIGINAL_BMI")]`` lists a precedence group: the first column with a value inside its
            plausibility bounds wins, and later columns only fill gaps. ``"auto"`` (default) builds a group,
            in ``concept_mapping`` order, from every set of columns that share a ``concept_id``; ``None``
            disables coalescing (one row per column, as in v0.5.7). Every column in a group must map to the
            same ``concept_id``.

    Returns:
        DataFrame with one row per source column: ``source_column, concept_id, rows_non_null,
        rows_out_of_range, rows_unmatched_person, rows_inserted``. A column folded into an earlier one
        reports ``rows_inserted = 0`` and the primary column's name in ``coalesced_into``; the primary's
        counts cover the whole group.
    """
    if batch_execution_mode not in ("sequential", "single"):
        raise ValueError("batch_execution_mode must be 'sequential' or 'single'.")
    if not concept_mapping:
        raise ValueError("concept_mapping must contain at least one source column.")
    staged = _table_ident(staged_table_name)
    target = _table_ident(target_table)
    filters = dict(value_plausibility_filters or {})
    relation = _source_relation(source)

    described = con.execute(f"DESCRIBE SELECT * FROM {relation} LIMIT 0").fetchall()
    columns = {row[0].lower(): row[0] for row in described}
    person_src = _resolve_col(columns, person_col, "person")
    date_src = _resolve_col(columns, date_col, "date")
    time_src = _resolve_col(columns, time_col, "time") if time_col and time_col.lower() in columns else None
    visit_src = _resolve_col(columns, visit_col, "visit") if visit_col and visit_col.lower() in columns else None
    if missing_columns not in ("raise", "skip"):
        raise ValueError("missing_columns must be 'raise' or 'skip'.")
    if missing_columns == "skip":
        concept_mapping = {c: v for c, v in concept_mapping.items() if c.lower() in columns}
        if not concept_mapping:
            raise ValueError("None of the mapped columns exist in the source.")
    mapping = _normalize_mapping(con, concept_mapping)
    value_src = {col: _resolve_col(columns, col, "measure") for col in mapping}

    groups = _resolve_coalesce_groups(mapping, coalesce_columns)
    primary_of = {c: g[0] for g in groups for c in g[1:]}
    group_of = {g[0]: g for g in groups}

    # ---- 1. stage ------------------------------------------------------------------------------
    value_cols = {col: f"v{i}" for i, col in enumerate(mapping)}
    value_ddl = ", ".join(f"{v} DOUBLE" for v in value_cols.values())
    con.execute(f"DROP TABLE IF EXISTS {staged}")
    con.execute(
        f"CREATE TABLE {staged} (stage_id BIGINT PRIMARY KEY, person_key VARCHAR, visit_key VARCHAR, "
        f"measure_datetime TIMESTAMP, measure_time VARCHAR, {value_ddl})"
    )
    dt_expr = vectorized_strptime(_ident(date_src), _ident(time_src) if time_src else None,
                                  datetime_formats, datetime_on_error)
    value_select = ", ".join(f"TRY_CAST({_ident(value_src[c])} AS DOUBLE)" for c in mapping)
    visit_select = f"CAST({_ident(visit_src)} AS VARCHAR)" if visit_src else "NULL"
    time_select = f"CAST({_ident(time_src)} AS VARCHAR)" if time_src else "NULL"
    con.execute(
        f"INSERT INTO {staged} SELECT ROW_NUMBER() OVER ()::BIGINT, CAST({_ident(person_src)} AS VARCHAR), "
        f"{visit_select}, {dt_expr}, {time_select}, {value_select} FROM {relation}"
    )

    # ---- 2. insert, one concept per committed pass ----------------------------------------------
    if person_id_is_cdm_id:
        person_join = "p.person_id = TRY_CAST(s.person_key AS BIGINT)"
    else:
        person_join = "p.person_source_value = s.person_key"

    def bounds(col: str) -> tuple[Any, Any]:
        lo, hi = filters.get(col, (mapping[col].get("min"), mapping[col].get("max")))
        return lo, hi

    def guarded(col: str, alias: str = "s") -> str:
        """Column value, NULL when outside its plausibility bounds."""
        v = f"{alias}.{value_cols[col]}"
        lo, hi = bounds(col)
        conds = []
        if lo is not None:
            conds.append(f"{v} >= {float(lo)!r}")
        if hi is not None:
            conds.append(f"{v} <= {float(hi)!r}")
        return f"CASE WHEN {' AND '.join(conds)} THEN {v} END" if conds else v

    def value_expr(col: str, alias: str = "s") -> str:
        members = group_of.get(col, (col,))
        if len(members) == 1:
            return guarded(col, alias)
        return "COALESCE(" + ", ".join(guarded(m, alias) for m in members) + ")"

    def raw_non_null(col: str) -> str:
        return " OR ".join(f"{value_cols[m]} IS NOT NULL" for m in group_of.get(col, (col,)))

    def pass_sql(col: str, offset_expr: str) -> str:
        entry = mapping[col]
        expr = value_expr(col)
        where = [f"({expr}) IS NOT NULL"]
        unit = entry.get("unit")
        unit_sql = "'" + str(unit).replace("'", "''") + "'" if unit else "NULL"
        loinc = entry.get("loinc")
        src_val = "'" + str(loinc if loinc else col).replace("'", "''") + "'"
        return (
            f"INSERT INTO {target} ({_MEASUREMENT_COLUMNS}) "
            f"SELECT {offset_expr} + s.stage_id, p.person_id, {entry['concept_id']}, "
            f"CAST(s.measure_datetime AS DATE), s.measure_datetime, s.measure_time, "
            f"{int(measurement_type_concept_id)}, 0, {expr}, 0, {entry.get('unit_concept_id') or 0}, "
            f"NULL, NULL, NULL, vo.visit_occurrence_id, NULL, {src_val}, 0, {unit_sql}, "
            f"{entry.get('unit_concept_id') or 0}, CAST({expr} AS VARCHAR), NULL, 0 "
            f"FROM {staged} s JOIN person p ON {person_join} "
            f"LEFT JOIN visit_occurrence vo ON vo.visit_source_value = s.visit_key "
            f"WHERE {' AND '.join(where)}"
        )

    def count_sql(col: str) -> tuple[int, int, int]:
        non_null, kept = con.execute(
            f"SELECT COUNT(*) FILTER (WHERE {raw_non_null(col)}), "
            f"COUNT(*) FILTER (WHERE ({value_expr(col, 's')}) IS NOT NULL) FROM {staged} s"
        ).fetchone()
        matched = con.execute(
            f"SELECT COUNT(*) FROM {staged} s JOIN person p ON {person_join} "
            f"WHERE ({value_expr(col, 's')}) IS NOT NULL"
        ).fetchone()[0]
        return int(non_null), int(kept), int(matched)

    report = []
    single = batch_execution_mode == "single"
    try:
        if single:
            con.execute("BEGIN TRANSACTION")
        for col in mapping:
            if col in primary_of:
                nn = int(con.execute(
                    f"SELECT COUNT({value_cols[col]}) FROM {staged}").fetchone()[0])
                report.append({
                    "source_column": col, "concept_id": mapping[col]["concept_id"], "rows_non_null": nn,
                    "rows_out_of_range": 0, "rows_unmatched_person": 0, "rows_inserted": 0,
                    "coalesced_into": primary_of[col],
                })
                continue
            non_null, kept, matched = count_sql(col)
            before = int(con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0])
            max_id = int(con.execute(f"SELECT COALESCE(MAX(measurement_id), 0) FROM {target}").fetchone()[0])
            if not single:
                con.execute("BEGIN TRANSACTION")
            con.execute(pass_sql(col, str(max_id)))
            if not single:
                con.execute("COMMIT")
            after = int(con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0])
            report.append({
                "source_column": col,
                "concept_id": mapping[col]["concept_id"],
                "rows_non_null": non_null,
                "rows_out_of_range": non_null - kept,
                "rows_unmatched_person": kept - matched,
                "rows_inserted": after - before,
                "coalesced_into": None,
            })
        if single:
            con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if not keep_staged:
            con.execute(f"DROP TABLE IF EXISTS {staged}")
    return pd.DataFrame(report)
