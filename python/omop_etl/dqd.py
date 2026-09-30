"""Native DuckDB DataQualityDashboard (DQD) Engine.

Executes pure-SQL, high-throughput OHDSI DQD checks (conformance, completeness,
plausibility, temporal logic) directly in DuckDB and serializes to standard
OHDSI DQD JSON format.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Sequence
import duckdb


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
