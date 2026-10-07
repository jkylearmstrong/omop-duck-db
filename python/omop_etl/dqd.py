"""Native DuckDB DataQualityDashboard (DQD) Engine.

Executes pure-SQL, high-throughput OHDSI DQD checks (conformance, completeness,
plausibility, temporal logic) directly in DuckDB and serializes to standard
OHDSI DQD JSON format. Also provides ``sanitize_measurements()``, which screens
measurement values against physiologic limits or statistical outliers.
"""

from __future__ import annotations

import csv
import datetime
import json
import math
import numbers
import os
import warnings
from pathlib import Path
from typing import Any, Sequence
import duckdb
import pandas as pd

from omop_etl.build_omop_cdm import _resource_path


def run_dqd(
    con: duckdb.DuckDBPyConnection,
    output_json: str | Path | None = None,
    check_levels: Sequence[str] = ("TABLE", "FIELD", "CONCEPT", "TEMPORAL"),
    cdm_version: str = "5.4",
) -> dict[str, Any]:
    """Runs native vectorized OHDSI DataQualityDashboard checks in DuckDB.
    
    Args:
        con: Active DuckDB connection to the OMOP CDM database.
        output_json: Optional file path to write standard OHDSI DQD JSON.
        check_levels: Tuple/list of check levels to execute ('TABLE', 'FIELD', 'CONCEPT', 'TEMPORAL').
        cdm_version: CDM version identifier (default '5.4').
        
    Returns:
        dict: Standard OHDSI DQD result dictionary with CheckResults, Metadata, and Overview.
    """
    check_levels_set = {lvl.upper() for lvl in check_levels}
    results: list[dict[str, Any]] = []

    def _has_table(tbl: str) -> bool:
        cnt = con.execute(f"SELECT COUNT(*) FROM information_schema.tables WHERE lower(table_name) = '{tbl.lower()}';").fetchone()[0]
        return cnt > 0

    def _get_row_count(tbl: str) -> int:
        if not _has_table(tbl):
            return 0
        return con.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0]

    # Core CDM tables
    tables = [
        "person", "observation_period", "visit_occurrence", "condition_occurrence",
        "procedure_occurrence", "drug_exposure", "measurement", "death", "care_site",
        "provider", "location", "cdm_source", "concept"
    ]

    # 1. TABLE CHECKS
    if "TABLE" in check_levels_set:
        for tbl in tables:
            exists = _has_table(tbl)
            row_cnt = _get_row_count(tbl) if exists else 0
            is_required = tbl in ("person", "observation_period", "visit_occurrence", "cdm_source")
            failed = (not exists) if is_required else False

            results.append({
                "check_id": f"TABLE_EXISTS_{tbl.upper()}",
                "check_name": "cdmTable",
                "check_level": "TABLE",
                "cdm_table_name": tbl,
                "cdm_field_name": None,
                "check_description": f"Verifies that required table {tbl} is present in the CDM.",
                "num_violated_rows": 0 if exists else 1,
                "pct_violated_rows": 0.0 if exists else 100.0,
                "num_denominator_rows": 1,
                "execution_status": "PASSED" if not failed else "FAILED",
                "failed": failed,
            })

            if exists and is_required:
                empty_failed = (row_cnt == 0)
                results.append({
                    "check_id": f"TABLE_NOT_EMPTY_{tbl.upper()}",
                    "check_name": "measurePersonCompleteness",
                    "check_level": "TABLE",
                    "cdm_table_name": tbl,
                    "cdm_field_name": None,
                    "check_description": f"Verifies that required table {tbl} contains clinical rows.",
                    "num_violated_rows": 1 if empty_failed else 0,
                    "pct_violated_rows": 100.0 if empty_failed else 0.0,
                    "num_denominator_rows": 1,
                    "execution_status": "PASSED" if not empty_failed else "FAILED",
                    "failed": empty_failed,
                })

    # 2. FIELD CHECKS (Nullability, PK uniqueness, FK integrity)
    if "FIELD" in check_levels_set:
        required_fields = [
            ("person", "person_id"),
            ("person", "gender_concept_id"),
            ("person", "year_of_birth"),
            ("observation_period", "observation_period_id"),
            ("observation_period", "person_id"),
            ("observation_period", "observation_period_start_date"),
            ("observation_period", "observation_period_end_date"),
            ("visit_occurrence", "visit_occurrence_id"),
            ("visit_occurrence", "person_id"),
            ("visit_occurrence", "visit_concept_id"),
            ("visit_occurrence", "visit_start_date"),
            ("condition_occurrence", "condition_occurrence_id"),
            ("condition_occurrence", "person_id"),
            ("condition_occurrence", "condition_concept_id"),
            ("condition_occurrence", "condition_start_date"),
            ("drug_exposure", "drug_exposure_id"),
            ("drug_exposure", "person_id"),
            ("drug_exposure", "drug_concept_id"),
            ("drug_exposure", "drug_exposure_start_date"),
            ("procedure_occurrence", "procedure_occurrence_id"),
            ("procedure_occurrence", "person_id"),
            ("procedure_occurrence", "procedure_concept_id"),
            ("procedure_occurrence", "procedure_date"),
            ("measurement", "measurement_id"),
            ("measurement", "person_id"),
            ("measurement", "measurement_concept_id"),
            ("measurement", "measurement_date"),
        ]

        for tbl, fld in required_fields:
            if not _has_table(tbl):
                continue
            total = _get_row_count(tbl)
            if total == 0:
                continue

            null_count = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {fld} IS NULL;").fetchone()[0]
            pct = round((null_count / total * 100.0), 2)
            results.append({
                "check_id": f"FIELD_NOT_NULL_{tbl.upper()}_{fld.upper()}",
                "check_name": "cdmField",
                "check_level": "FIELD",
                "cdm_table_name": tbl,
                "cdm_field_name": fld,
                "check_description": f"The number and percent of records with a NULL value in the {fld} field of the {tbl} table.",
                "num_violated_rows": null_count,
                "pct_violated_rows": pct,
                "num_denominator_rows": total,
                "execution_status": "PASSED" if null_count == 0 else "FAILED",
                "failed": null_count > 0,
            })

        # Primary Key Uniqueness
        pk_fields = [
            ("person", "person_id"),
            ("observation_period", "observation_period_id"),
            ("visit_occurrence", "visit_occurrence_id"),
            ("condition_occurrence", "condition_occurrence_id"),
            ("drug_exposure", "drug_exposure_id"),
            ("procedure_occurrence", "procedure_occurrence_id"),
            ("measurement", "measurement_id"),
            ("care_site", "care_site_id"),
            ("provider", "provider_id"),
            ("location", "location_id"),
        ]
        for tbl, pk in pk_fields:
            if not _has_table(tbl):
                continue
            total = _get_row_count(tbl)
            if total == 0:
                continue
            dup_count = con.execute(f"SELECT COUNT(*) - COUNT(DISTINCT {pk}) FROM {tbl};").fetchone()[0]
            pct = round((dup_count / total * 100.0), 2)
            results.append({
                "check_id": f"FIELD_PK_UNIQUE_{tbl.upper()}_{pk.upper()}",
                "check_name": "isPrimaryKey",
                "check_level": "FIELD",
                "cdm_table_name": tbl,
                "cdm_field_name": pk,
                "check_description": f"Verifies that primary key {pk} in table {tbl} is unique.",
                "num_violated_rows": dup_count,
                "pct_violated_rows": pct,
                "num_denominator_rows": total,
                "execution_status": "PASSED" if dup_count == 0 else "FAILED",
                "failed": dup_count > 0,
            })

        # Foreign Key Integrity: person_id in clinical occurrence tables exists in person
        fk_tables = [
            "observation_period", "visit_occurrence", "condition_occurrence",
            "drug_exposure", "procedure_occurrence", "measurement"
        ]
        if _has_table("person"):
            for tbl in fk_tables:
                if not _has_table(tbl):
                    continue
                total = _get_row_count(tbl)
                if total == 0:
                    continue
                orphan_count = con.execute(f"""
                    SELECT COUNT(*) FROM {tbl} t
                    LEFT JOIN person p ON t.person_id = p.person_id
                    WHERE p.person_id IS NULL;
                """).fetchone()[0]
                pct = round((orphan_count / total * 100.0), 2)
                results.append({
                    "check_id": f"FIELD_FK_PERSON_{tbl.upper()}",
                    "check_name": "fkPerson",
                    "check_level": "FIELD",
                    "cdm_table_name": tbl,
                    "cdm_field_name": "person_id",
                    "check_description": f"The number and percent of records in {tbl} with a person_id that does not exist in person.",
                    "num_violated_rows": orphan_count,
                    "pct_violated_rows": pct,
                    "num_denominator_rows": total,
                    "execution_status": "PASSED" if orphan_count == 0 else "FAILED",
                    "failed": orphan_count > 0,
                })

    # 3. CONCEPT CHECKS (Standard concepts conformance & Unmapped audit)
    if "CONCEPT" in check_levels_set and _has_table("concept") and _get_row_count("concept") > 0:
        concept_fields = [
            ("condition_occurrence", "condition_concept_id"),
            ("drug_exposure", "drug_concept_id"),
            ("procedure_occurrence", "procedure_concept_id"),
            ("measurement", "measurement_concept_id"),
        ]
        for tbl, fld in concept_fields:
            if not _has_table(tbl):
                continue
            total = _get_row_count(tbl)
            if total == 0:
                continue

            # Standard concept conformance (concepts != 0 should have standard_concept = 'S')
            non_std_count = con.execute(f"""
                SELECT COUNT(*) FROM {tbl} t
                JOIN concept c ON t.{fld} = c.concept_id
                WHERE t.{fld} != 0 AND (c.standard_concept != 'S' OR c.standard_concept IS NULL);
            """).fetchone()[0]
            pct_non_std = round((non_std_count / total * 100.0), 2)
            results.append({
                "check_id": f"CONCEPT_STANDARD_{tbl.upper()}_{fld.upper()}",
                "check_name": "standardConceptRecordCompleteness",
                "check_level": "CONCEPT",
                "cdm_table_name": tbl,
                "cdm_field_name": fld,
                "check_description": f"The number and percent of non-zero concept records in {tbl}.{fld} that map to a non-standard concept.",
                "num_violated_rows": non_std_count,
                "pct_violated_rows": pct_non_std,
                "num_denominator_rows": total,
                "execution_status": "PASSED" if non_std_count == 0 else "FAILED",
                "failed": non_std_count > 0,
            })

    # 4. TEMPORAL CHECKS
    if "TEMPORAL" in check_levels_set:
        span_tables = [
            ("observation_period", "observation_period_start_date", "observation_period_end_date"),
            ("visit_occurrence", "visit_start_date", "visit_end_date"),
            ("condition_occurrence", "condition_start_date", "condition_end_date"),
            ("drug_exposure", "drug_exposure_start_date", "drug_exposure_end_date"),
        ]
        for tbl, start_f, end_f in span_tables:
            if not _has_table(tbl):
                continue
            total = _get_row_count(tbl)
            if total == 0:
                continue

            viol_cnt = con.execute(f"""
                SELECT COUNT(*) FROM {tbl}
                WHERE {start_f} IS NOT NULL 
                  AND {end_f} IS NOT NULL 
                  AND {start_f} > {end_f};
            """).fetchone()[0]
            pct = round((viol_cnt / total * 100.0), 2)
            results.append({
                "check_id": f"TEMPORAL_START_BEFORE_END_{tbl.upper()}",
                "check_name": "plausibleTemporalAfter",
                "check_level": "TEMPORAL",
                "cdm_table_name": tbl,
                "cdm_field_name": f"{start_f}, {end_f}",
                "check_description": f"The number and percent of records in {tbl} where {start_f} occurs after {end_f}.",
                "num_violated_rows": viol_cnt,
                "pct_violated_rows": pct,
                "num_denominator_rows": total,
                "execution_status": "PASSED" if viol_cnt == 0 else "FAILED",
                "failed": viol_cnt > 0,
            })

        # Birth before event checks
        if _has_table("person"):
            event_tables = [
                ("condition_occurrence", "condition_start_date"),
                ("drug_exposure", "drug_exposure_start_date"),
                ("visit_occurrence", "visit_start_date"),
                ("procedure_occurrence", "procedure_date"),
                ("measurement", "measurement_date"),
            ]
            for tbl, dt_f in event_tables:
                if not _has_table(tbl):
                    continue
                total = _get_row_count(tbl)
                if total == 0:
                    continue

                pre_birth_cnt = con.execute(f"""
                    SELECT COUNT(*) FROM {tbl} t
                    JOIN person p ON t.person_id = p.person_id
                    WHERE t.{dt_f} < make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1));
                """).fetchone()[0]
                pct = round((pre_birth_cnt / total * 100.0), 2)
                results.append({
                    "check_id": f"TEMPORAL_BIRTH_BEFORE_EVENT_{tbl.upper()}",
                    "check_name": "plausibleTemporalBirth",
                    "check_level": "TEMPORAL",
                    "cdm_table_name": tbl,
                    "cdm_field_name": dt_f,
                    "check_description": f"The number and percent of records in {tbl} with an event date preceding patient birth date.",
                    "num_violated_rows": pre_birth_cnt,
                    "pct_violated_rows": pct,
                    "num_denominator_rows": total,
                    "execution_status": "PASSED" if pre_birth_cnt == 0 else "FAILED",
                    "failed": pre_birth_cnt > 0,
                })

    # Overview aggregation
    total_checks = len(results)
    failed_checks = sum(1 for r in results if r["failed"])
    passed_checks = total_checks - failed_checks
    pct_passed = round((passed_checks / total_checks * 100.0), 2) if total_checks > 0 else 100.0

    dqd_dict = {
        "Metadata": {
            "cdm_version": cdm_version,
            "dqa_tool": "omop-duck-db native DQD engine",
            "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "Overview": {
            "count_total": total_checks,
            "count_passed": passed_checks,
            "count_failed": failed_checks,
            "percent_passed": pct_passed,
        },
        "CheckResults": results,
    }

    if output_json is not None:
        p = Path(output_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(dqd_dict, f, indent=2)

    return dqd_dict

# ----------------------------------------------------------------------------------------
# sanitize_measurements: physiologic-range and outlier sanitization (RFC 6.1)
# ----------------------------------------------------------------------------------------
_SANITIZE_METHODS = ("dqd_biologic_limits", "winsorize_iqr", "z_score_cutoff")
_SANITIZE_ACTIONS = ("nullify", "clamp", "drop_row")
_SANITIZE_FORMATS = ("df", "arrow", "pyarrow", "polars")
# What dqd_biologic_limits does with a measurement whose unit_concept_id is NULL or 0 (see sanitize_measurements).
_SANITIZE_UNIT_UNKNOWN = ("envelope", "skip")
# Statistical methods need at least this many finite values in a (concept, unit) group.
_SANITIZE_MIN_GROUP_ROWS = 3
_SANITIZE_LIMITS_FILE = "physiologic_limits.csv"
# z_score_cutoff computes its statistics on values divided by the group's largest magnitude when that
# magnitude exceeds this, so that squaring a ~1e154+ artifact cannot overflow the variance. Below it the
# divisor is exactly 1.0 and the arithmetic is unchanged.
_SANITIZE_Z_RESCALE_ABOVE = "1e100"
_SANITIZE_REQUIRED_MEASUREMENT_COLS = (
    "measurement_id", "person_id", "measurement_concept_id", "measurement_date",
    "unit_concept_id", "value_as_number",
)
# Passed through when the measurement table has them (needed to aggregate per visit downstream).
_SANITIZE_OPTIONAL_MEASUREMENT_COLS = ("measurement_datetime", "visit_occurrence_id")
_SANITIZE_REPORT_COLUMNS = (
    "measurement_concept_id", "unit_concept_id", "n", "n_ok", "n_below", "n_above", "n_invalid",
    "n_changed", "n_dropped", "n_no_limit", "n_unit_skipped", "n_insufficient_data", "n_envelope",
)


def _sanitize_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sanitize_table_ref(name: Any, arg: str) -> str:
    """Quote a (possibly ``schema.table`` / ``catalog.schema.table``) table name."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{arg} must be a non-empty table or view name; got {name!r}")
    parts = name.strip().split(".")
    if len(parts) > 3 or any(not p for p in parts):
        raise ValueError(f"{arg} must look like 'table', 'schema.table' or 'catalog.schema.table'; got {name!r}")
    return ".".join(_sanitize_ident(p) for p in parts)


def _sanitize_num(x: float) -> str:
    """Round-trip-exact decimal text for a finite float (identical to the R implementation)."""
    return format(float(x), ".17g")


def _sanitize_positive(value: Any, arg: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{arg} must be a single positive, finite number; got {value!r}")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{arg} must be a single positive, finite number; got {value!r}")
    return v


def _sanitize_int(value: Any, what: str) -> int:
    """Strict integer: ints and integral floats are accepted; bool, text, NaN and fractions are not."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{what} must be a whole number; got {value!r}")
    f = float(value)
    if not math.isfinite(f) or f != math.floor(f):
        raise ValueError(f"{what} must be a whole number; got {value!r}")
    return int(f)


def _sanitize_concept_ids(values: Any) -> list[int]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"measurement_concept_ids must be a sequence of integer concept ids; got {values!r}")
    if isinstance(values, numbers.Real):
        values = [values]
    try:
        items = list(values)
    except TypeError as e:
        raise ValueError(f"measurement_concept_ids must be a sequence of integer concept ids; got {values!r}") from e
    if not items:
        raise ValueError("measurement_concept_ids is empty; pass None to sanitize every concept")
    return sorted({_sanitize_int(v, "measurement_concept_ids entries") for v in items})


def _sanitize_bound(value: Any, what: str) -> float | None:
    """A limit bound: a finite number, or missing (None/NaN/NA) for 'no bound on this side'."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{what} must be numeric or missing; got {value!r}") from e
    if not math.isfinite(f):
        raise ValueError(f"{what} must be finite (use a missing value for an open bound); got {value!r}")
    return f


def _sanitize_limit_rows(limits: Any) -> list[tuple]:
    """Validate a user ``limits`` frame -> ``[(concept_id, unit_concept_id | None, min | None, max | None)]``."""
    if not isinstance(limits, pd.DataFrame):
        try:
            limits = pd.DataFrame(limits)
        except (TypeError, ValueError) as e:
            raise ValueError("limits must be a DataFrame with columns concept_id, min_value, max_value "
                             "(and optionally unit_concept_id)") from e
    cols = {str(c).lower(): c for c in limits.columns}
    missing = [c for c in ("concept_id", "min_value", "max_value") if c not in cols]
    if missing:
        raise ValueError(f"limits is missing required column(s) {missing}; got columns {list(limits.columns)}")
    units = limits[cols["unit_concept_id"]].tolist() if "unit_concept_id" in cols else [None] * len(limits)
    rows, seen = [], set()
    for i, (cid, uid, lo, hi) in enumerate(zip(
            limits[cols["concept_id"]].tolist(), units,
            limits[cols["min_value"]].tolist(), limits[cols["max_value"]].tolist()), start=1):
        where = f"limits row {i}"
        if cid is None or (not isinstance(cid, str) and pd.isna(cid)):
            raise ValueError(f"{where}: concept_id is missing")
        cid = _sanitize_int(cid, f"{where}: concept_id")
        uid = None if (uid is None or (not isinstance(uid, str) and pd.isna(uid))) \
            else _sanitize_int(uid, f"{where}: unit_concept_id")
        lo, hi = _sanitize_bound(lo, f"{where}: min_value"), _sanitize_bound(hi, f"{where}: max_value")
        if lo is None and hi is None:
            raise ValueError(f"{where}: min_value and max_value are both missing, so it would limit nothing")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{where}: min_value ({lo}) is greater than max_value ({hi})")
        if (cid, uid) in seen:
            raise ValueError(f"{where}: duplicate (concept_id, unit_concept_id) = ({cid}, {uid})")
        seen.add((cid, uid))
        rows.append((cid, uid, lo, hi))
    return rows


def _sanitize_bundled_limits(path: str | os.PathLike | None = None) -> list[tuple]:
    """The bundled table ``inst/extdata/physiologic_limits.csv`` -> ``[(concept_id, unit_concept_id, min, max)]``.

    ``path`` overrides the bundled file location (used by the tests to feed a malformed table).
    """
    if path is None:
        path = _resource_path("extdata", _SANITIZE_LIMITS_FILE)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Bundled physiologic limits table not found at {path}. A source checkout needs "
            f"inst/extdata/{_SANITIZE_LIMITS_FILE}; an installed wheel ships it as "
            f"omop_etl/resources/extdata/{_SANITIZE_LIMITS_FILE}.")
    rows, seen = [], set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        need = ("concept_id", "unit_concept_id", "min_value", "max_value")
        absent = [c for c in need if c not in (reader.fieldnames or [])]
        if absent:
            raise ValueError(f"{path} is missing column(s) {absent}")
        for rec in reader:
            where = f"{path} line {reader.line_num}"
            try:
                row = (int(rec["concept_id"]), int(rec["unit_concept_id"]),
                       float(rec["min_value"]), float(rec["max_value"]))
            except (TypeError, ValueError) as e:
                raise ValueError(f"{where}: non-numeric concept_id/unit_concept_id/min_value/max_value") from e
            if not (math.isfinite(row[2]) and math.isfinite(row[3]) and row[2] < row[3]):
                raise ValueError(f"{where}: min_value must be finite and less than max_value")
            if row[:2] in seen:
                raise ValueError(f"{where}: duplicate (concept_id, unit_concept_id) = {row[:2]}")
            seen.add(row[:2])
            rows.append(row)
    if not rows:
        raise ValueError(f"{path} contains no limits")
    return rows


def _sanitize_effective_limits(user_rows: list[tuple] | None) -> list[tuple]:
    """Bundled table with the user's rows applied on top.

    A user row naming a unit replaces the bundled row for that (concept, unit); a user row without a
    unit is an any-unit limit that replaces every bundled row of its concept.
    """
    bundled = _sanitize_bundled_limits()
    if not user_rows:
        return bundled
    any_unit = {r[0] for r in user_rows if r[1] is None}
    by_unit = {(r[0], r[1]) for r in user_rows if r[1] is not None}
    kept = [r for r in bundled if r[0] not in any_unit and (r[0], r[1]) not in by_unit]
    return kept + list(user_rows)


def _sanitize_limits_cte(rows: list[tuple]) -> str:
    """``lim`` CTE: the limits as an inline literal (no temporary table, nothing registered)."""
    def lit(v, as_int):
        if v is None:
            return "NULL"
        return f"'{int(v)}'" if as_int else f"'{_sanitize_num(v)}'"

    values = ",\n      ".join(
        f"({lit(c, True)}, {lit(u, True)}, {lit(lo, False)}, {lit(hi, False)})" for c, u, lo, hi in rows)
    return (
        "lim AS (\n"
        "  SELECT CAST(concept_id AS BIGINT) AS concept_id, CAST(unit_concept_id AS BIGINT) AS unit_concept_id,\n"
        "         CAST(min_value AS DOUBLE) AS min_value, CAST(max_value AS DOUBLE) AS max_value\n"
        f"  FROM (VALUES\n      {values}\n  ) AS v(concept_id, unit_concept_id, min_value, max_value)\n"
        ")"
    )


def _sanitize_build_sql(*, method, action, extras, cohort_ref, person_ref, concept_ids, limit_rows,
                        iqr_multiplier, z_threshold, unit_unknown="envelope"):
    """Compose ``(rows_sql, report_sql)``; both share one classification CTE chain.

    Every row of the in-scope measurements gets a ``sanitize_status`` and a cleaned value in SQL, so the
    rows and the per-concept report are computed by the same text and always reconcile.
    """
    where = ["m.value_as_number IS NOT NULL"]
    if cohort_ref is not None:
        where.append(f"m.person_id IN (SELECT TRY_CAST(c.{person_ref} AS BIGINT) FROM {cohort_ref} AS c)")
    if concept_ids is not None:
        where.append("m.measurement_concept_id IN (" + ", ".join(str(int(c)) for c in concept_ids) + ")")
    extra_select = "".join(f", m.{_sanitize_ident(c)}" for c in extras)
    ctes = [
        "base AS (\n"
        "  SELECT m.measurement_id, m.person_id, m.measurement_concept_id, m.measurement_date,\n"
        f"         m.unit_concept_id, CAST(m.value_as_number AS DOUBLE) AS value_raw{extra_select}\n"
        "  FROM measurement AS m\n"
        f"  WHERE {' AND '.join(where)}\n"
        ")"
    ]

    if method == "dqd_biologic_limits":
        # unit_unknown='envelope': a measurement with a NULL or 0 unit that no unit row or any-unit row of its
        # concept covers is judged by the widest range over the concept's unit rows (lowest min, highest max;
        # a side stays open when any unit row leaves it open). 'skip' never builds the envelope.
        envelope = unit_unknown == "envelope"
        env_cond = "(le.concept_id IS NOT NULL AND (b.unit_concept_id IS NULL OR b.unit_concept_id = 0))" \
            if envelope else "FALSE"
        ctes += [
            _sanitize_limits_cte(limit_rows),
            "lim_unit AS (SELECT * FROM lim WHERE unit_concept_id IS NOT NULL)",
            "lim_any AS (SELECT * FROM lim WHERE unit_concept_id IS NULL)",
            "lim_concept AS (SELECT DISTINCT concept_id FROM lim)",
        ]
        if envelope:
            ctes.append(
                "lim_env AS (\n"
                "  SELECT concept_id,\n"
                "         CASE WHEN COUNT(*) FILTER (WHERE min_value IS NULL) > 0 THEN NULL ELSE MIN(min_value) END"
                " AS env_lo,\n"
                "         CASE WHEN COUNT(*) FILTER (WHERE max_value IS NULL) > 0 THEN NULL ELSE MAX(max_value) END"
                " AS env_hi\n"
                "  FROM lim_unit\n"
                "  GROUP BY concept_id\n"
                ")")
        env_join = "  LEFT JOIN lim_env AS le ON le.concept_id = b.measurement_concept_id\n" if envelope else ""
        env_lo = "WHEN " + env_cond + " THEN le.env_lo " if envelope else ""
        env_hi = "WHEN " + env_cond + " THEN le.env_hi " if envelope else ""
        ctes += [
            # a unit-specific row wins over an any-unit row, which wins over the envelope; a concept with
            # limits only in other units is 'unit_skipped', a concept with no limit row at all is 'no_limit'
            "joined AS (\n"
            "  SELECT b.*,\n"
            "         CASE WHEN lu.concept_id IS NOT NULL THEN lu.min_value\n"
            "              WHEN la.concept_id IS NOT NULL THEN la.min_value " + env_lo + "END AS lo,\n"
            "         CASE WHEN lu.concept_id IS NOT NULL THEN lu.max_value\n"
            "              WHEN la.concept_id IS NOT NULL THEN la.max_value " + env_hi + "END AS hi,\n"
            f"         (lu.concept_id IS NOT NULL OR la.concept_id IS NOT NULL OR {env_cond}) AS limit_applies,\n"
            f"         (lu.concept_id IS NULL AND la.concept_id IS NULL AND {env_cond}) AS by_envelope,\n"
            "         (lc.concept_id IS NOT NULL) AS concept_has_limit\n"
            "  FROM base AS b\n"
            "  LEFT JOIN lim_unit AS lu ON lu.concept_id = b.measurement_concept_id"
            " AND lu.unit_concept_id = b.unit_concept_id\n"
            "  LEFT JOIN lim_any AS la ON la.concept_id = b.measurement_concept_id\n"
            "  LEFT JOIN lim_concept AS lc ON lc.concept_id = b.measurement_concept_id\n"
            + env_join +
            ")",
            "cls AS (\n"
            "  SELECT joined.*, CASE\n"
            "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n"
            "    WHEN NOT limit_applies THEN CASE WHEN concept_has_limit THEN 'unit_skipped' ELSE 'no_limit' END\n"
            "    WHEN lo IS NOT NULL AND value_raw < lo THEN 'below_min'\n"
            "    WHEN hi IS NOT NULL AND value_raw > hi THEN 'above_max'\n"
            "    ELSE 'ok' END AS sanitize_status\n"
            "  FROM joined\n"
            ")",
        ]
    elif method == "winsorize_iqr":
        k = f"CAST('{_sanitize_num(iqr_multiplier)}' AS DOUBLE)"
        usable = f"s.s_n >= {_SANITIZE_MIN_GROUP_ROWS} AND s.s_q3 > s.s_q1"
        ctes += [
            # statistics come from the finite in-scope values only; concept 0 (unmapped) is never pooled
            "stats AS (\n"
            "  SELECT measurement_concept_id AS s_concept_id, unit_concept_id AS s_unit_concept_id,\n"
            "         COUNT(*) AS s_n, quantile_cont(value_raw, 0.25) AS s_q1, quantile_cont(value_raw, 0.75) AS s_q3\n"
            "  FROM base\n"
            "  WHERE isfinite(value_raw) AND measurement_concept_id <> 0\n"
            "  GROUP BY measurement_concept_id, unit_concept_id\n"
            ")",
            "joined AS (\n"
            f"  SELECT b.*, CASE WHEN {usable} THEN s.s_q1 - {k} * (s.s_q3 - s.s_q1) END AS lo,\n"
            f"         CASE WHEN {usable} THEN s.s_q3 + {k} * (s.s_q3 - s.s_q1) END AS hi\n"
            "  FROM base AS b\n"
            "  LEFT JOIN stats AS s ON s.s_concept_id = b.measurement_concept_id"
            " AND s.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n"
            ")",
            "cls AS (\n"
            "  SELECT joined.*, CASE\n"
            "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n"
            "    WHEN measurement_concept_id = 0 THEN 'no_limit'\n"
            "    WHEN lo IS NULL THEN 'insufficient_data'\n"
            "    WHEN value_raw < lo THEN 'below_min'\n"
            "    WHEN value_raw > hi THEN 'above_max'\n"
            "    ELSE 'ok' END AS sanitize_status\n"
            "  FROM joined\n"
            ")",
        ]
    else:
        k = f"CAST('{_sanitize_num(z_threshold)}' AS DOUBLE)"
        usable = f"s.s_n >= {_SANITIZE_MIN_GROUP_ROWS} AND s.s_sd > 0"
        ctes += [
            # statistics come from the finite in-scope values only; concept 0 (unmapped) is never pooled.
            # Mean and sd are taken on value / s_scale (s_scale is 1.0 unless the group holds a value above
            # 1e100) because DuckDB raises on STDDEV_SAMP overflow, and a single 1e160 artifact would abort
            # the whole call. The z test is made on the scaled value against the scaled fences; lo/hi are the
            # unscaled fences, only used to clamp (a flagged value's fence is always finite).
            "zscale AS (\n"
            "  SELECT measurement_concept_id AS s_concept_id, unit_concept_id AS s_unit_concept_id,\n"
            "         COUNT(*) AS s_n,\n"
            f"         CASE WHEN MAX(abs(value_raw)) > {_SANITIZE_Z_RESCALE_ABOVE} THEN MAX(abs(value_raw)) "
            "ELSE 1.0 END AS s_scale\n"
            "  FROM base\n"
            "  WHERE isfinite(value_raw) AND measurement_concept_id <> 0\n"
            "  GROUP BY measurement_concept_id, unit_concept_id\n"
            ")",
            "stats AS (\n"
            "  SELECT z.s_concept_id, z.s_unit_concept_id, z.s_n, z.s_scale,\n"
            "         avg(b.value_raw / z.s_scale) AS s_mean, stddev_samp(b.value_raw / z.s_scale) AS s_sd\n"
            "  FROM base AS b\n"
            "  JOIN zscale AS z ON z.s_concept_id = b.measurement_concept_id"
            " AND z.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n"
            "  WHERE isfinite(b.value_raw)\n"
            "  GROUP BY z.s_concept_id, z.s_unit_concept_id, z.s_n, z.s_scale\n"
            ")",
            "joined AS (\n"
            "  SELECT b.*, s.s_scale,\n"
            f"         CASE WHEN {usable} THEN s.s_mean - {k} * s.s_sd END AS lo_s,\n"
            f"         CASE WHEN {usable} THEN s.s_mean + {k} * s.s_sd END AS hi_s\n"
            "  FROM base AS b\n"
            "  LEFT JOIN stats AS s ON s.s_concept_id = b.measurement_concept_id"
            " AND s.s_unit_concept_id IS NOT DISTINCT FROM b.unit_concept_id\n"
            ")",
            "cls AS (\n"
            "  SELECT joined.*, lo_s * s_scale AS lo, hi_s * s_scale AS hi, CASE\n"
            "    WHEN NOT isfinite(value_raw) THEN 'invalid_number'\n"
            "    WHEN measurement_concept_id = 0 THEN 'no_limit'\n"
            "    WHEN lo_s IS NULL THEN 'insufficient_data'\n"
            "    WHEN value_raw / s_scale < lo_s THEN 'below_min'\n"
            "    WHEN value_raw / s_scale > hi_s THEN 'above_max'\n"
            "    ELSE 'ok' END AS sanitize_status\n"
            "  FROM joined\n"
            ")",
        ]

    if action == "clamp":
        out_of_range = "CASE WHEN sanitize_status = 'below_min' THEN lo ELSE hi END"
        # +/-Inf go to the nearest finite bound when there is one; NaN (and any value with no finite bound,
        # e.g. an IQR fence that overflowed) is nullified
        invalid = ("CASE WHEN isinf(value_raw) AND value_raw > 0 AND isfinite(hi) THEN hi "
                   "WHEN isinf(value_raw) AND value_raw < 0 AND isfinite(lo) THEN lo ELSE NULL END")
    else:
        out_of_range = invalid = "NULL"
    ctes.append(
        "fin AS (\n"
        "  SELECT cls.*,\n"
        "         (sanitize_status IN ('below_min', 'above_max', 'invalid_number')) AS flagged,\n"
        "         CASE WHEN sanitize_status IN ('below_min', 'above_max') THEN " + out_of_range + "\n"
        "              WHEN sanitize_status = 'invalid_number' THEN " + invalid + "\n"
        "              ELSE value_raw END AS value_clean\n"
        "  FROM cls\n"
        ")"
    )
    with_clause = "WITH " + ",\n".join(ctes)

    out_cols = ("measurement_id, person_id, measurement_concept_id, measurement_date, unit_concept_id, "
                "value_clean AS value_as_number, value_raw AS value_as_number_raw, sanitize_status"
                + "".join(f", {_sanitize_ident(c)}" for c in extras))
    rows_sql = (
        f"{with_clause}\nSELECT {out_cols}\nFROM fin\n"
        + ("WHERE NOT flagged\n" if action == "drop_row" else "")
        + "ORDER BY measurement_id, person_id, measurement_concept_id, measurement_date"
    )

    def n_of(status):
        return f"COUNT(*) FILTER (WHERE sanitize_status = '{status}')"

    changed, dropped = ("CAST(0 AS BIGINT)", "COUNT(*) FILTER (WHERE flagged)") if action == "drop_row" \
        else ("COUNT(*) FILTER (WHERE flagged)", "CAST(0 AS BIGINT)")
    # rows whose ok / below_min / above_max verdict came from the unit envelope (never NaN/Inf rows)
    n_envelope = ("COUNT(*) FILTER (WHERE by_envelope AND isfinite(value_raw))"
                  if method == "dqd_biologic_limits" else "CAST(0 AS BIGINT)")
    report_sql = (
        f"{with_clause}\n"
        "SELECT measurement_concept_id, unit_concept_id, COUNT(*) AS n,\n"
        f"       {n_of('ok')} AS n_ok, {n_of('below_min')} AS n_below, {n_of('above_max')} AS n_above,\n"
        f"       {n_of('invalid_number')} AS n_invalid, {changed} AS n_changed, {dropped} AS n_dropped,\n"
        f"       {n_of('no_limit')} AS n_no_limit, {n_of('unit_skipped')} AS n_unit_skipped,\n"
        f"       {n_of('insufficient_data')} AS n_insufficient_data, {n_envelope} AS n_envelope\n"
        "FROM fin\n"
        "GROUP BY measurement_concept_id, unit_concept_id\n"
        "ORDER BY measurement_concept_id, unit_concept_id NULLS LAST"
    )
    return rows_sql, report_sql


def _sanitize_arrow(result) -> Any:
    reader = result.arrow()
    return reader.read_all() if hasattr(reader, "read_all") else reader


def _sanitize_warn_unscreened(report: pd.DataFrame) -> None:
    """Warn when most of a limit-bearing concept's rows lack a unit and so were not screened at all.

    Only reachable with ``unit_unknown='skip'``: the package's own ETL writes ``unit_concept_id = 0`` for lab
    results, and without the unit envelope such labs come back as ``unit_skipped`` with their artifacts intact.
    """
    if report.empty:
        return
    no_unit = report["unit_concept_id"].isna() | (report["unit_concept_id"] == 0)
    by_concept = report["measurement_concept_id"]
    total = report["n"].groupby(by_concept).sum()
    unknown = report["n_unit_skipped"].where(no_unit, 0).groupby(by_concept).sum()
    bad = sorted(int(c) for c in total.index[2 * unknown.reindex(total.index) > total])
    if not bad:
        return
    shown = ", ".join(str(c) for c in bad[:5]) + (f", ... ({len(bad)} in all)" if len(bad) > 5 else "")
    warnings.warn(
        f"sanitize_measurements: more than half of the measurements of {len(bad)} concept(s) with physiologic "
        f"limits have no unit (unit_concept_id is NULL or 0) and were NOT screened (sanitize_status "
        f"'unit_skipped'): concept_id {shown}. Use unit_unknown='envelope' (the default) to judge them against "
        f"the widest range over the listed units, map the units in the ETL, or pass `limits` rows without "
        f"unit_concept_id to apply a limit whatever the unit.",
        UserWarning, stacklevel=3)


def sanitize_measurements(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str | None = None,
    method: str = "dqd_biologic_limits",
    action: str = "nullify",
    measurement_concept_ids: Sequence[int] | None = None,
    limits: pd.DataFrame | None = None,
    iqr_multiplier: float = 3.0,
    z_threshold: float = 4.0,
    person_col: str = "subject_id",
    format: str = "df",
    unit_unknown: str = "envelope",
) -> Any:
    r"""Sanitize implausible numeric measurements (RFC 6.1): physiologic limits or statistical outliers.

    Screens ``measurement.value_as_number`` for artifacts such as a systolic blood pressure of 0 or 999,
    negative lab values, or NaN/Inf, and returns the numeric measurements with a sanitized value, the
    raw value and a per-row status. Everything is computed in DuckDB SQL (no temporary or persistent
    objects are created and the database is only read), so it works on a read-only connection and
    gives the same result as ``omopduckdb::sanitize_measurements()`` in R.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection to the OMOP CDM database (read-only is fine).
    cohort_table : str, optional
        Table or view (``name`` or ``schema.name``) restricting the scope to the persons listed in its
        ``person_col``. Duplicate cohort rows do not duplicate measurements. ``None`` = everyone.
    method : {'dqd_biologic_limits', 'winsorize_iqr', 'z_score_cutoff'}
        ``'dqd_biologic_limits'`` compares each value with the bundled per-concept, per-unit
        clinical-plausibility limits (``inst/extdata/physiologic_limits.csv``, see Notes) or with
        ``limits``. The name is the RFC's: the bounds are conservative clinical-plausibility limits curated
        for this package, not numeric thresholds copied from the OHDSI Data Quality Dashboard.
        ``'winsorize_iqr'`` flags values outside the Tukey fences ``Q1 - k*IQR .. Q3 + k*IQR`` of their
        (concept, unit) group (``k = iqr_multiplier``). ``'z_score_cutoff'`` flags values with
        ``|z| > z_threshold`` using the group mean and sample standard deviation. The two statistical
        methods assume roughly symmetric data; see Notes before using them on skewed labs.
    action : {'nullify', 'clamp', 'drop_row'}
        What to do with a flagged value: set it to NULL, move it to the violated bound (for
        ``'winsorize_iqr'`` the fence, for ``'z_score_cutoff'`` ``mean +/- z_threshold*sd``), or remove
        the row from the result. ``'nullify'`` is the right choice for artifacts; see Notes on
        ``'clamp'``.
    measurement_concept_ids : sequence of int, optional
        Only sanitize these ``measurement_concept_id`` values. ``None`` = all concepts.
    limits : pandas.DataFrame, optional
        Custom limits for ``'dqd_biologic_limits'`` with columns ``concept_id``, ``min_value``,
        ``max_value`` and optionally ``unit_concept_id`` (names are case-insensitive, extra columns are
        ignored). A row with a ``unit_concept_id`` replaces the bundled row for that concept and unit (or
        adds one). A row without a unit (missing value or no such column) is an any-unit limit: it
        replaces every bundled row of the concept and applies whatever unit the measurement has,
        including NULL/0. A missing ``min_value`` or ``max_value`` leaves that side open. Passing
        ``limits`` with another method warns and is ignored.
    iqr_multiplier : float, default 3.0
        Fence width ``k`` for ``'winsorize_iqr'`` (3.0 = "far out" outliers; 1.5 = classic Tukey). Must be
        positive and finite.
    z_threshold : float, default 4.0
        Cut-off for ``'z_score_cutoff'``. Must be positive and finite.
    person_col : str, default 'subject_id'
        Person id column of ``cohort_table``. It must exist; there is no fallback to another column.
    format : {'df', 'arrow', 'polars'}, default 'df'
        ``'df'`` returns a pandas DataFrame with the report in ``df.attrs['sanitization_report']``.
        ``'arrow'`` (pyarrow Table) and ``'polars'`` return the rows only: the report is **not**
        attached to them. If polars is not installed ``'polars'`` warns and returns the Arrow table.
    unit_unknown : {'envelope', 'skip'}, default 'envelope'
        Only used by ``'dqd_biologic_limits'``: what to do with a measurement whose ``unit_concept_id`` is
        NULL or 0 (unknown), as every lab written by this package's PCORnet ETL is. ``'envelope'`` judges
        it against the widest range over the units the concept has limits in (lowest ``min_value``, highest
        ``max_value``), so a value that is valid in any listed unit passes and only values impossible in
        every unit (negative, 1e6) are flagged. ``'skip'`` leaves such a measurement untouched
        (``'unit_skipped'``). A non-zero unit that the concept has no limit for is always ``'unit_skipped'``.
        Passing ``'skip'`` with another method warns and is ignored.

    Returns
    -------
    pandas.DataFrame or pyarrow.Table or polars.DataFrame
        One row per in-scope measurement (rows whose ``value_as_number`` is NULL are not part of the
        result), ordered by ``measurement_id``, with columns ``measurement_id``, ``person_id``,
        ``measurement_concept_id``, ``measurement_date``, ``unit_concept_id``, ``value_as_number``
        (sanitized), ``value_as_number_raw``, ``sanitize_status`` and, when the measurement table has
        them, ``measurement_datetime`` and ``visit_occurrence_id``.

        ``sanitize_status`` is one of ``'ok'``, ``'below_min'``, ``'above_max'``, ``'invalid_number'``
        (NaN or +/-Inf), ``'no_limit'``, ``'unit_skipped'`` or ``'insufficient_data'``.

        The per-concept report is a DataFrame in ``df.attrs['sanitization_report']``, one row per
        ``measurement_concept_id`` and ``unit_concept_id`` (NULL unit = NaN, listed last within a concept)
        with columns ``measurement_concept_id``, ``unit_concept_id``, ``n``, ``n_ok``, ``n_below``,
        ``n_above``, ``n_invalid``, ``n_changed``, ``n_dropped``, ``n_no_limit``, ``n_unit_skipped``,
        ``n_insufficient_data`` (the status counts the spec's other columns cannot hold: it is 0 for
        ``'dqd_biologic_limits'``) and ``n_envelope``. It counts every in-scope row, including dropped ones,
        and reconciles exactly:
        ``n = n_ok + n_below + n_above + n_invalid + n_no_limit + n_unit_skipped + n_insufficient_data``
        and ``n_changed + n_dropped = n_below + n_above + n_invalid``. ``n_envelope`` is not part of that
        sum: it counts the rows (of the ``'ok'``, ``'below_min'`` and ``'above_max'`` ones) whose verdict came
        from the unit envelope (see ``unit_unknown``). It is 0 for the statistical methods and with
        ``unit_unknown='skip'``.

    Raises
    ------
    ValueError
        For an unknown ``method``/``action``/``format``/``unit_unknown``, a non-positive ``iqr_multiplier``
        or ``z_threshold``, bad ``measurement_concept_ids`` or ``limits``, a missing ``measurement``
        table/column, or a ``cohort_table`` that cannot be read or lacks ``person_col``.

    Notes
    -----
    **Invalid numbers.** NaN and +/-Inf are invalid under every method, whether or not a limit exists
    (``'invalid_number'``). ``'nullify'`` and ``'drop_row'`` behave as usual. ``'clamp'`` moves
    +Inf/-Inf to the group's upper/lower bound when there is one, and falls back to NULL for NaN and
    whenever no finite bound applies.

    **Inclusive bounds.** A value equal to a bound is ``'ok'``.

    **Bundled limits.** ``physiologic_limits.csv`` holds conservative clinical-plausibility limits curated
    for this package: every row has ``source = clinical_plausibility`` (see its ``note`` column). The units
    were cross-checked against the unit lists of the OHDSI Data Quality Dashboard (DQD), but the bounds are
    not DQD numeric thresholds, whatever the method name ``'dqd_biologic_limits'`` (the RFC's) suggests.
    They are deliberately wide: they remove physiologically impossible values and sensor or data-entry
    artifacts (a systolic pressure of 0 or 999, a saturation of 0, a negative lab value) and keep severe but
    survivable ones (a venous or severe-hypoxemia saturation in the 20s or 30s, a serum sodium of 80-84
    mmol/L, a glucose below 5 mg/dL). The table is a curated subset (10 vital signs and about 25 common
    labs), not every measurement concept: a concept without a row is ``'no_limit'``. Pass ``limits`` to add
    or replace rows.

    **Units (dqd_biologic_limits).** A limit row applies when the measurement's ``unit_concept_id`` equals
    the row's, so a creatinine in umol/L is never compared with the mg/dL bounds. A non-zero unit that the
    concept has no limit for is left untouched and reported as ``'unit_skipped'``. A concept with no limit
    row at all is ``'no_limit'``.

    **Unknown units (unit_unknown).** This package's PCORnet ETL writes ``unit_concept_id = 0`` for lab
    results (the text stays in ``unit_source_value``). With the default ``unit_unknown='envelope'`` a
    measurement with a NULL or 0 unit is judged against the widest range over the units its concept has
    limits in. For creatinine (0.1-30 mg/dL, 8-2650 umol/L) that is 0.1-2650: a value of 999 passes because
    it could be umol/L, while -5 and 1e6 are flagged (``'clamp'`` moves them to the envelope bound). The
    envelope only catches values that are impossible in every unit, so it is much weaker than a limit in the
    known unit, most of all for concepts whose units
    differ by a large factor (creatinine, weight, height), and an ``'ok'`` from it does not mean the value is
    plausible in its true unit. ``n_envelope`` in the report counts these rows. A ``limits`` row without
    ``unit_concept_id``, or one for unit 0, takes precedence over the envelope. With
    ``unit_unknown='skip'`` such rows are left untouched (``'unit_skipped'``) and a ``UserWarning`` is
    raised when more than half of the measurements of a concept with limits are skipped for that reason.

    **Choosing an action.** ``'nullify'`` (or ``'drop_row'``) is the right action for the artifacts
    ``'dqd_biologic_limits'`` finds: a blood pressure of 0 or 999 is a sentinel, not an extreme
    measurement, and ``'clamp'`` would turn it into 40 or 300, a plausible-looking value that downstream
    models treat as real. Use ``'clamp'`` (winsorization) only for values that are extreme but real.

    **The report and pandas.** pandas stores ``attrs`` as is, and it cannot serialize or merge a DataFrame
    kept there: ``df.to_parquet()`` raises ``TypeError`` and ``pandas.concat`` of two frames that both carry
    a report raises ``ValueError``. Take the report out first, e.g.
    ``report = df.attrs.pop('sanitization_report')``, before writing or concatenating the frame. Slicing,
    ``copy()`` and ``assign()`` keep it. The ``'arrow'`` and ``'polars'`` formats carry no report at all.

    **Skewed data.** ``'winsorize_iqr'`` and ``'z_score_cutoff'`` assume a roughly symmetric
    distribution. Right-skewed labs (creatinine, ALT, CRP, ...) have a long valid tail: at the defaults
    about 0.5% of a log-normal creatinine (median 1 mg/dL, sigma 0.45) is flagged, and clinically
    important values (an AKI creatinine of 3-6 mg/dL) are nullified or clamped. Prefer
    ``'dqd_biologic_limits'`` for such labs, or transform them first. ``'z_score_cutoff'`` is also not
    robust: the outliers themselves inflate the mean and standard deviation, and with a sample standard
    deviation no value of a group of ``n`` can have ``|z|`` above ``(n - 1) / sqrt(n)``, so the default
    ``z_threshold = 4`` cannot flag anything in a group of fewer than 18 values.

    **Statistical methods.** Statistics are computed per ``(measurement_concept_id, unit_concept_id)``
    (a NULL unit is its own group) over the finite, in-scope values, i.e. after the cohort and concept
    restrictions. A group with fewer than 3 finite values, a zero IQR (``'winsorize_iqr'``) or a zero or
    undefined standard deviation (``'z_score_cutoff'``) has no usable spread: its values are kept and
    reported as ``'insufficient_data'``. ``measurement_concept_id = 0`` (unmapped) is never pooled into
    a group and is reported as ``'no_limit'``. Quartiles use linear interpolation (DuckDB
    ``quantile_cont``, the same as numpy's default and R's ``quantile(type = 7)``). Values of any finite
    magnitude are accepted: ``'z_score_cutoff'`` rescales a group that holds a value above 1e100 so the
    variance cannot overflow.

    Examples
    --------
    >>> clean = sanitize_measurements(con, cohort_table="index_cohort",
    ...                               method="dqd_biologic_limits", action="nullify")
    >>> clean.attrs["sanitization_report"]
    """
    method_k = str(method).lower().strip()
    action_k = str(action).lower().strip()
    fmt = str(format).lower().strip()
    unit_k = str(unit_unknown).lower().strip()
    if method_k not in _SANITIZE_METHODS:
        raise ValueError(f"method must be one of {list(_SANITIZE_METHODS)}; got {method!r}")
    if action_k not in _SANITIZE_ACTIONS:
        raise ValueError(f"action must be one of {list(_SANITIZE_ACTIONS)}; got {action!r}")
    if fmt not in _SANITIZE_FORMATS:
        raise ValueError(f"format must be one of 'df', 'arrow' or 'polars'; got {format!r}")
    if unit_k not in _SANITIZE_UNIT_UNKNOWN:
        raise ValueError(f"unit_unknown must be one of {list(_SANITIZE_UNIT_UNKNOWN)}; got {unit_unknown!r}")
    k_iqr = _sanitize_positive(iqr_multiplier, "iqr_multiplier")
    k_z = _sanitize_positive(z_threshold, "z_threshold")
    concept_ids = None if measurement_concept_ids is None else _sanitize_concept_ids(measurement_concept_ids)

    limit_rows = None
    if method_k == "dqd_biologic_limits":
        user_rows = None if limits is None else _sanitize_limit_rows(limits)
        limit_rows = _sanitize_effective_limits(user_rows)
    elif limits is not None:
        warnings.warn(f"limits is only used by method='dqd_biologic_limits' and is ignored for method={method_k!r}.",
                      stacklevel=2)
    if method_k != "dqd_biologic_limits" and unit_k != "envelope":
        warnings.warn(f"unit_unknown is only used by method='dqd_biologic_limits' and is ignored for "
                      f"method={method_k!r}.", stacklevel=2)

    try:
        meas_cols = {r[0].lower(): r[0] for r in con.execute("DESCRIBE SELECT * FROM measurement LIMIT 0").fetchall()}
    except duckdb.Error as e:
        raise ValueError(f"sanitize_measurements() needs a CDM 'measurement' table on this connection: {e}") from e
    absent = [c for c in _SANITIZE_REQUIRED_MEASUREMENT_COLS if c not in meas_cols]
    if absent:
        raise ValueError(f"The measurement table is missing required column(s) {absent}")
    extras = [meas_cols[c] for c in _SANITIZE_OPTIONAL_MEASUREMENT_COLS if c in meas_cols]

    cohort_ref = person_ref = None
    if cohort_table is not None:
        cohort_ref = _sanitize_table_ref(cohort_table, "cohort_table")
        if not isinstance(person_col, str) or not person_col.strip():
            raise ValueError(f"person_col must be a non-empty column name; got {person_col!r}")
        try:
            cohort_cols = {r[0].lower(): r[0]
                           for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_ref} LIMIT 0").fetchall()}
        except duckdb.Error as e:
            raise ValueError(f"cohort_table {cohort_table!r} could not be read: {e}") from e
        if person_col.strip().lower() not in cohort_cols:
            raise ValueError(f"person_col {person_col!r} not found in cohort_table {cohort_table!r}. "
                             f"Available columns: {list(cohort_cols.values())}. Pass person_col=... explicitly.")
        person_ref = _sanitize_ident(cohort_cols[person_col.strip().lower()])

    rows_sql, report_sql = _sanitize_build_sql(
        method=method_k, action=action_k, extras=extras, cohort_ref=cohort_ref, person_ref=person_ref,
        concept_ids=concept_ids, limit_rows=limit_rows, iqr_multiplier=k_iqr, z_threshold=k_z,
        unit_unknown=unit_k)

    out = None
    if fmt == "df":
        out = con.execute(rows_sql).df()
    else:
        table = _sanitize_arrow(con.execute(rows_sql))
    # the report is also the input of the unscreened-unit warning, which only exists for unit_unknown='skip'
    warn_unscreened = method_k == "dqd_biologic_limits" and unit_k == "skip"
    report = None
    if fmt == "df" or warn_unscreened:
        report = con.execute(report_sql).df()
    if warn_unscreened:
        _sanitize_warn_unscreened(report)
    if out is not None:
        out.attrs["sanitization_report"] = report
        return out
    if fmt == "polars":
        try:
            import polars as pl
        except ImportError:
            warnings.warn("polars is not installed; returning a pyarrow Table instead.", stacklevel=2)
            return table
        return pl.from_arrow(table)
    return table
