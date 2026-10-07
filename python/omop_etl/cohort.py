"""Cohort generation, concept ancestor hierarchy navigation, and phenotyping helpers.

Provides helpers for querying transitive closures in concept_ancestor, performing
concept set algebra, managing the OMOP cohort and cohort_definition tables,
and tracking patient attrition across CONSORT flowchart phenotyping criteria.
"""

from __future__ import annotations

import datetime
import numbers
import re
import warnings
from dataclasses import dataclass
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd


def ensure_cohort_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Ensures standard OMOP CDM v5.4 cohort and cohort_definition tables exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS cohort (
            cohort_definition_id BIGINT NOT NULL,
            subject_id BIGINT NOT NULL,
            cohort_start_date DATE NOT NULL,
            cohort_end_date DATE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cohort_definition (
            cohort_definition_id BIGINT NOT NULL,
            cohort_definition_name VARCHAR(255) NOT NULL,
            cohort_definition_description VARCHAR,
            definition_type_concept_id INTEGER,
            cohort_definition_syntax VARCHAR,
            subject_concept_id INTEGER,
            cohort_initiation_date DATE
        );
    """)


def get_concept_descendants(
    con: duckdb.DuckDBPyConnection,
    ancestor_concept_ids: int | Sequence[int],
    include_self: bool = True,
    min_levels_of_separation: int = 0,
    max_levels_of_separation: int | None = None,
) -> pd.DataFrame:
    """Queries concept_ancestor to resolve all standard descendants at any distance.
    
    Args:
        con: Active DuckDB connection.
        ancestor_concept_ids: A single concept ID or collection of concept IDs.
        include_self: Whether to include zero-level self mappings.
        min_levels_of_separation: Minimum hierarchy depth (default 0).
        max_levels_of_separation: Maximum hierarchy depth (None = unlimited).
    
    Returns:
        pd.DataFrame: Descendant records with concept metadata.
    """
    if isinstance(ancestor_concept_ids, int):
        ids = [ancestor_concept_ids]
    else:
        ids = list(ancestor_concept_ids)

    if not ids:
        return pd.DataFrame(columns=[
            "ancestor_concept_id", "descendant_concept_id", "min_levels_of_separation",
            "max_levels_of_separation", "concept_name", "vocabulary_id", "concept_code", "standard_concept"
        ])

    in_clause = ", ".join(str(int(i)) for i in ids)
    max_filter = f"AND ca.max_levels_of_separation <= {int(max_levels_of_separation)}" if max_levels_of_separation is not None else ""
    self_filter = "" if include_self else "AND ca.ancestor_concept_id != ca.descendant_concept_id"

    query = f"""
        SELECT 
            ca.ancestor_concept_id,
            ca.descendant_concept_id,
            ca.min_levels_of_separation,
            ca.max_levels_of_separation,
            c.concept_name,
            c.vocabulary_id,
            c.concept_code,
            c.standard_concept
        FROM concept_ancestor ca
        JOIN concept c ON ca.descendant_concept_id = c.concept_id
        WHERE ca.ancestor_concept_id IN ({in_clause})
          AND ca.min_levels_of_separation >= {int(min_levels_of_separation)}
          {max_filter}
          {self_filter}
        ORDER BY ca.ancestor_concept_id, ca.min_levels_of_separation, ca.descendant_concept_id;
    """
    return con.execute(query).df()


def get_concept_ancestors(
    con: duckdb.DuckDBPyConnection,
    descendant_concept_ids: int | Sequence[int],
    include_self: bool = True,
    min_levels_of_separation: int = 0,
    max_levels_of_separation: int | None = None,
) -> pd.DataFrame:
    """Navigates upward through polyhierarchies in concept_ancestor.
    
    Args:
        con: Active DuckDB connection.
        descendant_concept_ids: A single concept ID or collection of concept IDs.
        include_self: Whether to include zero-level self mappings.
        min_levels_of_separation: Minimum hierarchy depth (default 0).
        max_levels_of_separation: Maximum hierarchy depth (None = unlimited).
    
    Returns:
        pd.DataFrame: Ancestor records with concept metadata.
    """
    if isinstance(descendant_concept_ids, int):
        ids = [descendant_concept_ids]
    else:
        ids = list(descendant_concept_ids)

    if not ids:
        return pd.DataFrame(columns=[
            "descendant_concept_id", "ancestor_concept_id", "min_levels_of_separation",
            "max_levels_of_separation", "concept_name", "vocabulary_id", "concept_code", "standard_concept"
        ])

    in_clause = ", ".join(str(int(i)) for i in ids)
    max_filter = f"AND ca.max_levels_of_separation <= {int(max_levels_of_separation)}" if max_levels_of_separation is not None else ""
    self_filter = "" if include_self else "AND ca.ancestor_concept_id != ca.descendant_concept_id"

    query = f"""
        SELECT 
            ca.descendant_concept_id,
            ca.ancestor_concept_id,
            ca.min_levels_of_separation,
            ca.max_levels_of_separation,
            c.concept_name,
            c.vocabulary_id,
            c.concept_code,
            c.standard_concept
        FROM concept_ancestor ca
        JOIN concept c ON ca.ancestor_concept_id = c.concept_id
        WHERE ca.descendant_concept_id IN ({in_clause})
          AND ca.min_levels_of_separation >= {int(min_levels_of_separation)}
          {max_filter}
          {self_filter}
        ORDER BY ca.descendant_concept_id, ca.min_levels_of_separation, ca.ancestor_concept_id;
    """
    return con.execute(query).df()


def get_concept_relationships(
    con: duckdb.DuckDBPyConnection,
    concept_ids: int | Sequence[int],
    relationship_id: str = "Maps to",
) -> pd.DataFrame:
    """Looks up direct concept relationships from concept_relationship."""
    if isinstance(concept_ids, int):
        ids = [concept_ids]
    else:
        ids = list(concept_ids)

    if not ids:
        return pd.DataFrame(columns=[
            "concept_id_1", "concept_id_2", "relationship_id", "concept_name_2",
            "vocabulary_id_2", "concept_code_2", "standard_concept_2"
        ])

    in_clause = ", ".join(str(int(i)) for i in ids)
    rel_esc = str(relationship_id).replace("'", "''")

    query = f"""
        SELECT 
            cr.concept_id_1,
            cr.concept_id_2,
            cr.relationship_id,
            c.concept_name AS concept_name_2,
            c.vocabulary_id AS vocabulary_id_2,
            c.concept_code AS concept_code_2,
            c.standard_concept AS standard_concept_2
        FROM concept_relationship cr
        JOIN concept c ON cr.concept_id_2 = c.concept_id
        WHERE cr.concept_id_1 IN ({in_clause})
          AND cr.relationship_id = '{rel_esc}'
          AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
        ORDER BY cr.concept_id_1, cr.concept_id_2;
    """
    return con.execute(query).df()


def resolve_concept_set(
    con: duckdb.DuckDBPyConnection,
    include_concepts: int | Sequence[int],
    exclude_concepts: int | Sequence[int] | None = None,
    include_descendants: bool = True,
    exclude_descendants: bool = True,
) -> list[int]:
    r"""Evaluates concept set algebra (A ∪ Desc(A) \ (B ∪ Desc(B))) matching OHDSI ATLAS.
    
    Returns:
        list[int]: Sorted list of resolved standard concept IDs.
    """
    if isinstance(include_concepts, int):
        inc_ids = [include_concepts]
    else:
        inc_ids = list(include_concepts)

    if not inc_ids:
        return []

    inc_clause = ", ".join(str(int(i)) for i in inc_ids)

    if include_descendants:
        inc_query = f"""
            SELECT descendant_concept_id AS concept_id 
            FROM concept_ancestor 
            WHERE ancestor_concept_id IN ({inc_clause})
            UNION
            SELECT concept_id FROM concept WHERE concept_id IN ({inc_clause})
        """
    else:
        inc_query = f"SELECT concept_id FROM concept WHERE concept_id IN ({inc_clause})"

    if exclude_concepts:
        if isinstance(exclude_concepts, int):
            exc_ids = [exclude_concepts]
        else:
            exc_ids = list(exclude_concepts)
        exc_clause = ", ".join(str(int(i)) for i in exc_ids)

        if exclude_descendants:
            exc_query = f"""
                SELECT descendant_concept_id AS concept_id 
                FROM concept_ancestor 
                WHERE ancestor_concept_id IN ({exc_clause})
                UNION
                SELECT concept_id FROM concept WHERE concept_id IN ({exc_clause})
            """
        else:
            exc_query = f"SELECT concept_id FROM concept WHERE concept_id IN ({exc_clause})"

        final_query = f"({inc_query}) EXCEPT ({exc_query}) ORDER BY 1;"
    else:
        final_query = f"({inc_query}) ORDER BY 1;"

    res = con.execute(final_query).fetchall()
    return [row[0] for row in res]


def create_cohort(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int,
    cohort_name: str,
    cohort_description: str | None = None,
    entry_sql: str | None = None,
    exit_rule: str = "fixed_days",
    exit_offset_days: int = 0,
) -> int:
    """Materializes a cohort into the OMOP cohort and cohort_definition tables.
    
    Args:
        con: Active DuckDB connection.
        cohort_id: Integer identifier for this cohort.
        cohort_name: Display name.
        cohort_description: Optional detailed description.
        entry_sql: SQL selecting qualifying events. Must project `person_id` (or `subject_id`)
                   and `start_date` (or `cohort_start_date`), and optionally `end_date`.
        exit_rule: Exit strategy ('fixed_days' or 'from_query').
        exit_offset_days: Offset added to start_date when exit_rule='fixed_days'.
    
    Returns:
        int: Number of cohort records inserted.
    """
    ensure_cohort_tables(con)

    name_esc = cohort_name.replace("'", "''")
    desc_esc = (cohort_description or "").replace("'", "''")

    con.execute(f"DELETE FROM cohort_definition WHERE cohort_definition_id = {int(cohort_id)};")
    con.execute(f"""
        INSERT INTO cohort_definition (
            cohort_definition_id,
            cohort_definition_name,
            cohort_definition_description,
            definition_type_concept_id,
            cohort_definition_syntax,
            subject_concept_id,
            cohort_initiation_date
        ) VALUES (
            {int(cohort_id)},
            '{name_esc}',
            '{desc_esc}',
            0,
            NULL,
            0,
            DATE '{datetime.date.today().isoformat()}'
        );
    """)

    con.execute(f"DELETE FROM cohort WHERE cohort_definition_id = {int(cohort_id)};")

    if entry_sql is None:
        return 0

    cols_info = con.execute(f"DESCRIBE ({entry_sql})").fetchall()
    actual_cols = {c[0].upper(): c[0] for c in cols_info}

    subj_col = None
    for cand in ["SUBJECT_ID", "PERSON_ID", "PATID"]:
        if cand in actual_cols:
            subj_col = actual_cols[cand]
            break
    if subj_col is None:
        subj_col = cols_info[0][0]

    start_col = None
    for cand in ["COHORT_START_DATE", "START_DATE", "INDEX_DATE", "CONDITION_START_DATE", "DRUG_EXPOSURE_START_DATE", "PROCEDURE_DATE", "VISIT_START_DATE"]:
        if cand in actual_cols:
            start_col = actual_cols[cand]
            break
    if start_col is None:
        start_col = cols_info[1][0] if len(cols_info) > 1 else cols_info[0][0]

    end_col = None
    for cand in ["COHORT_END_DATE", "END_DATE", "CONDITION_END_DATE", "DRUG_EXPOSURE_END_DATE", "VISIT_END_DATE"]:
        if cand in actual_cols:
            end_col = actual_cols[cand]
            break

    if exit_rule == "fixed_days":
        end_date_expr = f'CAST("{start_col}" AS DATE) + {int(exit_offset_days)}'
    else:
        end_date_expr = f'COALESCE(CAST("{end_col}" AS DATE), CAST("{start_col}" AS DATE))' if end_col else f'CAST("{start_col}" AS DATE)'

    insert_sql = f"""
        INSERT INTO cohort (
            cohort_definition_id,
            subject_id,
            cohort_start_date,
            cohort_end_date
        )
        SELECT 
            {int(cohort_id)} AS cohort_definition_id,
            CAST("{subj_col}" AS BIGINT) AS subject_id,
            CAST("{start_col}" AS DATE) AS cohort_start_date,
            {end_date_expr} AS cohort_end_date
        FROM ({entry_sql}) raw_entry
        WHERE "{subj_col}" IS NOT NULL AND "{start_col}" IS NOT NULL;
    """
    con.execute(insert_sql)
    n = con.execute(f"SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = {int(cohort_id)};").fetchone()[0]
    return n


def compute_attrition(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int,
    steps: Sequence[tuple[str, str]],
) -> pd.DataFrame:
    """Tracks patient attrition across sequential CONSORT inclusion/exclusion steps.
    
    Args:
        con: Active DuckDB connection.
        cohort_id: Cohort ID being evaluated.
        steps: List of (step_name, step_query) where step_query returns eligible subjects.
               The query may reference `_step_current` to filter down from the previous step.
    
    Returns:
        pd.DataFrame: Table with columns [step_number, step_name, subjects_retained, subjects_dropped, percent_retained].
    """
    records = []
    con.execute("DROP TABLE IF EXISTS _step_current;")

    prev_count = None
    initial_count = None

    for i, (step_name, step_query) in enumerate(steps, start=1):
        if i == 1:
            con.execute(f"CREATE TEMPORARY TABLE _step_current AS {step_query};")
        else:
            con.execute(f"CREATE TEMPORARY TABLE _step_next AS {step_query};")
            con.execute("DROP TABLE _step_current;")
            con.execute("ALTER TABLE _step_next RENAME TO _step_current;")

        cols_info = con.execute("DESCRIBE _step_current").fetchall()
        actual_cols = {c[0].upper(): c[0] for c in cols_info}
        subj_col = actual_cols.get("SUBJECT_ID", actual_cols.get("PERSON_ID", actual_cols.get("PATID", cols_info[0][0])))
        current_count = con.execute(f'SELECT COUNT(DISTINCT "{subj_col}") FROM _step_current').fetchone()[0]

        if i == 1:
            initial_count = current_count
            prev_count = current_count
            dropped = 0
            pct = 100.0 if initial_count > 0 else 0.0
        else:
            dropped = max(0, prev_count - current_count)
            pct = round((current_count / initial_count * 100.0), 2) if initial_count and initial_count > 0 else 0.0
            prev_count = current_count

        records.append({
            "step_number": i,
            "step_name": step_name,
            "subjects_retained": current_count,
            "subjects_dropped": dropped,
            "percent_retained": pct,
        })

    con.execute("DROP TABLE IF EXISTS _step_current;")
    return pd.DataFrame(records)


def combine_cohorts(
    con: duckdb.DuckDBPyConnection,
    new_cohort_id: int,
    cohort_id_a: int,
    cohort_id_b: int,
    operation: str = "UNION",
    name: str | None = None,
    description: str | None = None,
) -> int:
    """Combines two cohorts using set operations (UNION, INTERSECT, DIFFERENCE)."""
    ensure_cohort_tables(con)
    op = operation.strip().upper()
    if op not in ("UNION", "INTERSECT", "DIFFERENCE", "EXCEPT"):
        raise ValueError(f"Unsupported operation '{operation}'. Use UNION, INTERSECT, or DIFFERENCE.")

    sql_op = "EXCEPT" if op == "DIFFERENCE" else op
    name = name or f"Cohort {new_cohort_id} ({op} of {cohort_id_a} and {cohort_id_b})"

    entry_sql = f"""
        SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = {int(cohort_id_a)}
        {sql_op}
        SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = {int(cohort_id_b)}
    """
    return create_cohort(
        con,
        cohort_id=new_cohort_id,
        cohort_name=name,
        cohort_description=description,
        entry_sql=entry_sql,
        exit_rule="from_query",
    )


def get_cohort_summary(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int | None = None,
) -> pd.DataFrame:
    """Returns summary metrics (subject count, record count, min/max start dates, mean duration)."""
    ensure_cohort_tables(con)
    filter_clause = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""

    query = f"""
        SELECT 
            c.cohort_definition_id,
            COALESCE(cd.cohort_definition_name, 'Cohort ' || c.cohort_definition_id) AS cohort_name,
            COUNT(DISTINCT c.subject_id) AS total_subjects,
            COUNT(*) AS total_records,
            MIN(c.cohort_start_date) AS min_start_date,
            MAX(c.cohort_start_date) AS max_start_date,
            ROUND(AVG(c.cohort_end_date - c.cohort_start_date), 1) AS mean_duration_days
        FROM cohort c
        LEFT JOIN cohort_definition cd ON c.cohort_definition_id = cd.cohort_definition_id
        {filter_clause}
        GROUP BY c.cohort_definition_id, cd.cohort_definition_name
        ORDER BY c.cohort_definition_id;
    """
    return con.execute(query).df()


# ======================================================================================================
# Study-cohort engine
#
# One parameterised SQL generator shared by define_study_cohort, build_readmission_cohort and
# build_end_of_life_cohort. The two legacy builders are thin wrappers that pin the conventions they have
# always used (see _READMISSION_CONVENTIONS / _END_OF_LIFE_CONVENTIONS and the legacy option values passed in
# build_readmission_cohort / build_end_of_life_cohort); the golden-file tests in tests/test_define_study_cohort.py
# guarantee their results did not change, apart from the 0.5.3 death-concept fix.
# define_study_cohort defaults to the corrected behaviour of every option that differs from the legacy one.
# ======================================================================================================

_VISIT_TYPE_CONCEPTS = {"inpatient": (9201,), "emergency": (9203,), "outpatient": (9202,)}
_SAMPLING_RULES = ("first", "last", "random")
_AGE_METHODS = ("year_difference", "completed_years")
_WASHIN_FALLBACKS = ("no_observation_period", "always", "never")
_DEATH_SOURCES = ("death_table", "discharge_disposition")
_FOLLOWUP_EVIDENCE = ("observation_period", "visit", "measurement", "condition", "drug", "death")
_MORTALITY_TYPES = ("in_hospital", "post_discharge", "fixed_window", "composite_readmit_or_death")
_STUDY_TMP_TABLE = "_study_cohort_tmp"
_INPATIENT_CONCEPT = 9201
# SNOMED 'Patient died'. 4155309 (written by this package's PCORnet ETL for discharge against medical advice) is
# 'Ileal part' in Athena and is NOT a death: it is deliberately absent from every default death-disposition set.
_DEATH_DISCHARGE_CONCEPT = 4216643

# Conventions of build_readmission_cohort: only the index stay's own discharge disposition ('Patient died')
# and the death table count as death; follow-up is evidenced by later encounters or clinical records.
_READMISSION_CONVENTIONS = {
    "death_ids": (_DEATH_DISCHARGE_CONCEPT,),
    "death_sources": ("death_table",),
    "evidence": ("visit", "measurement", "condition", "drug"),
}
# Conventions of build_end_of_life_cohort: death is the earliest date across the death table and every
# visit discharged to a death concept; follow-up is also evidenced by observation periods and by a death
# at/after the horizon. Before 0.5.3 the death concepts also included 4155309; that was a bug (see above). Pass
# death_ids=(4216643, 4155309) to reproduce the old results.
_END_OF_LIFE_CONVENTIONS = {
    "death_ids": (_DEATH_DISCHARGE_CONCEPT,),
    "death_sources": _DEATH_SOURCES,
    "evidence": _FOLLOWUP_EVIDENCE,
}
# (outcome family) -> (min_los_days, exclude_in_hospital_death) used when the caller passes "auto". The minimum
# length of stay only applies to inpatient index stays: see _auto_min_los.
_AUTO_STAY_RULES = {
    "none": (1, True),
    "readmission": (1, True),
    "post_discharge": (1, True),
    "composite": (1, True),
    "in_hospital": (0, False),
    "fixed_window": (None, False),
}
_MORTALITY_FAMILIES = ("in_hospital", "post_discharge", "fixed_window", "composite")
_EARLY_DEATH_FAMILIES = ("post_discharge", "fixed_window", "composite")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALL_CAUSE_RE = re.compile(r"^all_cause_readmission(?:_(\d+)d)?$")


@dataclass(frozen=True)
class _StudySpec:
    """Fully resolved, validated definition handed to the SQL generators.

    The defaults are the corrected 0.5.3 behaviour of ``define_study_cohort``; the legacy builders pin the
    pre-0.5.3 value of every option that differs (``age_method="year_difference"``, ``washin_fallback="always"``,
    ``death_verifies_followup=False``, ``exclude_early_death=False``) so their results stay identical.
    """

    cohort_id: int
    outcome: str  # none | readmission | in_hospital | post_discharge | fixed_window | composite
    label: str  # value written to the target_outcome / mortality_type column
    schema: str | None = None
    visit_ids: tuple[int, ...] | None = (9201,)
    outcome_visit_ids: tuple[int, ...] = (9201,)
    study_start: str | None = None
    study_end: str | None = None
    min_age: int | None = 18
    age_method: str = "completed_years"
    min_los_days: int | None = 1
    washin_days: int = 365
    washin_fallback: str = "no_observation_period"
    exclude_death: bool = True
    exclude_early_death: bool = True
    death_ids: tuple[int, ...] = (_DEATH_DISCHARGE_CONCEPT,)
    death_sources: tuple[str, ...] = ("death_table",)
    window_days: int | None = 30
    gap_days: int = 0
    verified: bool = True
    death_verifies_followup: bool = True
    evidence: tuple[str, ...] = _FOLLOWUP_EVIDENCE
    episode_merge_days: int | None = None
    rule: str = "random"
    seed: int = 42


# ---- identifier / literal helpers -------------------------------------------------------------------

def _sql_str(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _int_list(ids: Sequence[int]) -> str:
    return ", ".join(str(int(x)) for x in ids)


def _quote_ident_path(name: Any, what: str, max_parts: int) -> str:
    """Validates ``name`` as dot-separated plain identifiers and returns it double-quoted."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a non-empty string, got {name!r}.")
    parts = name.split(".")
    if len(parts) > max_parts or not all(_IDENT_RE.match(p) for p in parts):
        raise ValueError(
            f"Invalid {what} {name!r}: use up to {max_parts} dot-separated names made of letters, digits and underscores."
        )
    return ".".join(f'"{p}"' for p in parts)


def _tbl(spec: _StudySpec, name: str) -> str:
    return name if spec.schema is None else f"{_quote_ident_path(spec.schema, 'schema', 2)}.{name}"


# ---- SQL generators ---------------------------------------------------------------------------------

def _study_order_by(rule: str, seed: int) -> str:
    if rule == "random":
        return f"hash(f.visit_occurrence_id, {int(seed)}), f.visit_occurrence_id"
    if rule == "first":
        return "f.visit_start_date ASC, f.visit_occurrence_id ASC"
    if rule == "last":
        return "f.visit_start_date DESC, f.visit_occurrence_id DESC"
    raise ValueError(f"Unsupported sampling rule '{rule}'. Use 'first', 'last', or 'random'.")


def _auto_min_los(outcome: str, visit_ids: tuple[int, ...] | None) -> int | None:
    """The ``"auto"`` minimum length of stay: the outcome family's rule for inpatient index stays, 0 otherwise.

    A 1-day minimum is what makes a visit an admission; applying it to emergency, outpatient or mixed index visit
    types would remove every same-day visit and silently return an empty cohort.
    """
    base = _AUTO_STAY_RULES[outcome][0]
    if not base:  # None (fixed-window mortality: no restriction) or 0 (in-hospital mortality)
        return base
    return base if visit_ids is not None and tuple(visit_ids) == (_INPATIENT_CONCEPT,) else 0


def _study_age_expr(spec: _StudySpec) -> str:
    dob = "make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1))"
    fn = "date_diff" if spec.age_method == "year_difference" else "date_sub"
    return f"{fn}('year', {dob}, v.visit_start_date)"


def _study_anchor(spec: _StudySpec) -> str:
    """Date the outcome / follow-up windows are measured from: index start (fixed window) or discharge."""
    return "e.visit_start_date" if spec.outcome == "fixed_window" else "e.visit_end_date"


def _study_followup_applicable(spec: _StudySpec) -> bool:
    """Verified follow-up needs a horizon and is not defined for in-hospital outcomes."""
    return spec.outcome != "in_hospital" and spec.window_days is not None


def _study_stay_death_expr(spec: _StudySpec) -> str:
    """The index stay's death date: the patient's earliest known death, or the stay's own discharge date when its
    disposition says the patient died (whatever ``death_sources`` is), whichever is earlier."""
    return (
        "LEAST(pd.death_date, CASE WHEN COALESCE(v.discharged_to_concept_id, 0) IN "
        f"({_int_list(spec.death_ids)}) THEN CAST(v.visit_end_date AS DATE) END)"
    )


def _study_early_death_applies(spec: _StudySpec) -> bool:
    """Whether the 'death before the outcome window opens' exclusion adds anything for this definition.

    Post-discharge and composite outcomes already drop deaths on or before discharge (an in-hospital death), so
    the rule only matters there when a gap separates discharge from the window; for fixed-window mortality it also
    removes a death on the index start day.
    """
    if not spec.exclude_early_death or spec.outcome not in _EARLY_DEATH_FAMILIES:
        return False
    return spec.outcome == "fixed_window" or int(spec.gap_days) > 0


def _study_conditions(spec: _StudySpec) -> list[tuple[str, str]]:
    """Index-stay eligibility criteria as (label, SQL predicate on v / p / pd), in attrition order."""
    conds: list[tuple[str, str]] = []
    if spec.visit_ids is None:
        conds.append(("Any visit type", "1=1"))
    else:
        ids = _int_list(spec.visit_ids)
        conds.append((f"Index visit type in ({ids})", f"v.visit_concept_id IN ({ids})"))

    if spec.study_start is not None or spec.study_end is not None:
        parts = []
        if spec.study_start is not None:
            parts.append(f"v.visit_start_date >= DATE '{spec.study_start}'")
        if spec.study_end is not None:
            parts.append(f"v.visit_start_date <= DATE '{spec.study_end}'")
        if spec.study_start is not None and spec.study_end is not None:
            label = f"Index visit start between {spec.study_start} and {spec.study_end} (inclusive)"
        elif spec.study_start is not None:
            label = f"Index visit start on or after {spec.study_start}"
        else:
            label = f"Index visit start on or before {spec.study_end}"
        conds.append((label, " AND ".join(parts)))

    if spec.min_age is not None:
        conds.append((f"Age >= {int(spec.min_age)} years at index", f"{_study_age_expr(spec)} >= {int(spec.min_age)}"))

    if spec.min_los_days is not None:
        n = int(spec.min_los_days)
        conds.append((f"Length of stay >= {n} day(s)", f"date_diff('day', v.visit_start_date, v.visit_end_date) >= {n}"))

    if spec.washin_days and int(spec.washin_days) > 0:
        w = int(spec.washin_days)
        op, vo = _tbl(spec, "observation_period"), _tbl(spec, "visit_occurrence")
        covered = f"""EXISTS (
                    SELECT 1 FROM {op} op
                    WHERE op.person_id = v.person_id
                      AND op.observation_period_start_date <= (v.visit_start_date - {w})
                      AND op.observation_period_end_date >= v.visit_start_date
                )"""
        prior_visit = f"""EXISTS (
                    SELECT 1 FROM {vo} pv
                    WHERE pv.person_id = v.person_id
                      AND pv.visit_occurrence_id != v.visit_occurrence_id
                      AND pv.visit_start_date <= (v.visit_start_date - {w})
                )"""
        if spec.washin_fallback == "always":
            body = f"""(
                {covered}
                OR {prior_visit}
            )"""
        elif spec.washin_fallback == "never":
            body = f"""(
                {covered}
            )"""
        else:  # no_observation_period: a prior visit only stands in for observation periods that do not exist
            body = f"""(
                {covered}
                OR (
                    NOT EXISTS (SELECT 1 FROM {op} anyop WHERE anyop.person_id = v.person_id)
                    AND {prior_visit}
                )
            )"""
        conds.append((f"Prior observation >= {w} days (wash-in)", body))

    if spec.exclude_death:
        d = _int_list(spec.death_ids)
        conds.append((
            "No in-hospital death at index stay",
            f"COALESCE(v.discharged_to_concept_id, 0) NOT IN ({d}) AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)",
        ))
    else:
        conds.append(("Alive at index admission", "(pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)"))

    if _study_early_death_applies(spec):
        death = _study_stay_death_expr(spec)
        fixed = spec.outcome == "fixed_window"
        anchor_v = "v.visit_start_date" if fixed else "v.visit_end_date"
        g = int(spec.gap_days)
        conds.append((
            f"No death before the outcome window opens (death after {'index start' if fixed else 'discharge'} + {g} d)",
            f"({death} IS NULL OR {death} > ({anchor_v} + {g}))",
        ))
    return conds


def _study_death_ctes(spec: _StudySpec) -> str:
    selects = []
    if "death_table" in spec.death_sources:
        selects.append(
            f"SELECT person_id, CAST(death_date AS DATE) AS death_date FROM {_tbl(spec, 'death')} WHERE death_date IS NOT NULL"
        )
    if "discharge_disposition" in spec.death_sources:
        selects.append(
            f"SELECT person_id, CAST(visit_end_date AS DATE) AS death_date FROM {_tbl(spec, 'visit_occurrence')} "
            f"WHERE discharged_to_concept_id IN ({_int_list(spec.death_ids)}) AND visit_end_date IS NOT NULL"
        )
    union = "\n        UNION ALL\n        ".join(selects)
    return f"""all_deaths AS (
        {union}
    ),
    patient_death AS (
        SELECT person_id, MIN(death_date) AS death_date
        FROM all_deaths
        GROUP BY person_id
    )"""


def _study_episode_ctes(spec: _StudySpec) -> str:
    """Merges index-type stays that overlap, or start within ``episode_merge_days`` of the previous stay's end, into
    one episode per run (a transfer between wards is one hospitalisation). The episode starts with its earliest stay
    (which supplies ``visit_occurrence_id`` / ``visit_concept_id``), ends with the latest end date, and takes the
    discharge disposition of the stay that ends last."""
    n = int(spec.episode_merge_days)
    vo = _tbl(spec, "visit_occurrence")
    type_filter = "1=1" if spec.visit_ids is None else f"v.visit_concept_id IN ({_int_list(spec.visit_ids)})"
    return f"""episode_src AS (
        SELECT
            v.visit_occurrence_id, v.person_id, v.visit_concept_id, v.visit_start_date, v.visit_end_date,
            v.discharged_to_concept_id,
            MAX(v.visit_end_date) OVER (
                PARTITION BY v.person_id ORDER BY v.visit_start_date, v.visit_occurrence_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS previous_end
        FROM {vo} v
        WHERE {type_filter}
    ),
    episode_runs AS (
        SELECT
            *,
            SUM(CASE WHEN previous_end IS NULL OR visit_start_date > (previous_end + {n}) THEN 1 ELSE 0 END) OVER (
                PARTITION BY person_id ORDER BY visit_start_date, visit_occurrence_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS episode_no
        FROM episode_src
    ),
    index_episodes AS (
        SELECT
            person_id,
            first_stay.id AS visit_occurrence_id,
            first_stay.concept AS visit_concept_id,
            episode_start AS visit_start_date,
            episode_end AS visit_end_date,
            last_stay.disposition AS discharged_to_concept_id
        FROM (
            SELECT
                person_id,
                MIN(visit_start_date) AS episode_start,
                MAX(visit_end_date) AS episode_end,
                MIN(struct_pack(s := visit_start_date, id := visit_occurrence_id, concept := visit_concept_id)) AS first_stay,
                MAX(struct_pack(e := visit_end_date, s := visit_start_date, id := visit_occurrence_id,
                                disposition := discharged_to_concept_id)) AS last_stay
            FROM episode_runs
            GROUP BY person_id, episode_no
        ) episodes
    )"""


def _study_base_ctes(spec: _StudySpec) -> str:
    """The CTEs every eligibility / attrition query starts with: death dates and, when merging, the episodes."""
    head = _study_death_ctes(spec)
    if spec.episode_merge_days is not None:
        head += ",\n    " + _study_episode_ctes(spec)
    return head


def _study_eligible_select(spec: _StudySpec, conditions: list[tuple[str, str]]) -> str:
    where = "\n          AND ".join(c for _, c in conditions)
    stays = "index_episodes" if spec.episode_merge_days is not None else _tbl(spec, "visit_occurrence")
    return f"""SELECT
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_concept_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            {_study_stay_death_expr(spec)} AS death_date,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            {_study_age_expr(spec)} AS age_at_index
        FROM {stays} v
        JOIN {_tbl(spec, 'person')} p ON v.person_id = p.person_id
        LEFT JOIN patient_death pd ON v.person_id = pd.person_id
        WHERE {where}"""


def _study_readmit_predicate(spec: _StudySpec) -> str:
    return (
        "ro.person_id = e.subject_id "
        "AND ro.visit_occurrence_id != e.visit_occurrence_id "
        f"AND ro.visit_concept_id IN ({_int_list(spec.outcome_visit_ids)}) "
        f"AND ro.visit_start_date > (e.visit_end_date + {int(spec.gap_days)}) "
        f"AND ro.visit_start_date <= (e.visit_end_date + {int(spec.window_days)})"
    )


def _study_outcome_sql(spec: _StudySpec) -> str:
    """The outcome_flag / outcome_date select-list items, evaluated per eligible stay (alias e)."""
    gap, win = int(spec.gap_days), spec.window_days
    vo = _tbl(spec, "visit_occurrence")
    if spec.outcome == "none":
        return "CAST(NULL AS INTEGER) AS outcome_flag, CAST(NULL AS DATE) AS outcome_date"
    if spec.outcome == "readmission":
        pred = _study_readmit_predicate(spec)
        return (
            f"CASE WHEN EXISTS (SELECT 1 FROM {vo} ro WHERE {pred}) THEN 1 ELSE 0 END AS outcome_flag, "
            f"CAST((SELECT MIN(ro.visit_start_date) FROM {vo} ro WHERE {pred}) AS DATE) AS outcome_date"
        )
    d = _int_list(spec.death_ids)
    if spec.outcome == "in_hospital":
        # e.death_date already includes the stay's own disposition death (its discharge date), so the outcome date
        # never falls after the stay even when the death table dates the death later
        cond = (
            f"(COALESCE(e.discharged_to_concept_id, 0) IN ({d}) "
            "OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date))"
        )
        return (
            f"CASE WHEN {cond} THEN 1 ELSE 0 END AS outcome_flag, "
            f"CASE WHEN {cond} THEN CAST(COALESCE(e.death_date, e.visit_end_date) AS DATE) ELSE NULL END AS outcome_date"
        )
    anchor = _study_anchor(spec)
    death_in_window = (
        f"(e.death_date IS NOT NULL AND e.death_date > ({anchor} + {gap}) AND e.death_date <= ({anchor} + {int(win)}))"
    )
    if spec.outcome in ("post_discharge", "fixed_window"):
        return (
            f"CASE WHEN {death_in_window} THEN 1 ELSE 0 END AS outcome_flag, "
            f"CASE WHEN {death_in_window} THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date"
        )
    if spec.outcome == "composite":
        pred = _study_readmit_predicate(spec)
        return (
            f"CASE WHEN ({death_in_window} OR EXISTS (SELECT 1 FROM {vo} ro WHERE {pred})) THEN 1 ELSE 0 END AS outcome_flag, "
            f"CAST(LEAST(CASE WHEN {death_in_window} THEN e.death_date ELSE NULL END, "
            f"(SELECT MIN(ro.visit_start_date) FROM {vo} ro WHERE {pred})) AS DATE) AS outcome_date"
        )
    raise ValueError(f"Unsupported outcome family '{spec.outcome}'.")


def _study_followup_sql(spec: _StudySpec) -> str:
    """The has_subsequent_event / died_in_followup select-list items.

    ``has_subsequent_event``: evidence of observation at / after anchor + horizon. With
    ``death_verifies_followup`` a record dated after the patient's death date is not evidence (a lagged or
    mis-dated record of a deceased patient). ``died_in_followup``: the patient's death date is on or before
    anchor + horizon, i.e. the stay itself or the follow-up window ended in death.
    """
    if not _study_followup_applicable(spec):
        return "CAST(NULL AS INTEGER) AS has_subsequent_event, CAST(NULL AS INTEGER) AS died_in_followup"
    h = int(spec.window_days)
    anchor = _study_anchor(spec)
    t = lambda name: _tbl(spec, name)  # noqa: E731
    alive = lambda col: (  # noqa: E731
        f" AND (e.death_date IS NULL OR {col} <= e.death_date)" if spec.death_verifies_followup else ""
    )
    clauses = {
        "observation_period": (
            f"EXISTS (SELECT 1 FROM {t('observation_period')} op WHERE op.person_id = e.subject_id "
            f"AND op.observation_period_end_date >= ({anchor} + {h}))"
        ),
        "visit": (
            f"EXISTS (SELECT 1 FROM {t('visit_occurrence')} sv WHERE sv.person_id = e.subject_id "
            f"AND sv.visit_occurrence_id != e.visit_occurrence_id AND sv.visit_start_date >= ({anchor} + {h})"
            f"{alive('sv.visit_start_date')})"
        ),
        "measurement": (
            f"EXISTS (SELECT 1 FROM {t('measurement')} sm WHERE sm.person_id = e.subject_id "
            f"AND sm.measurement_date >= ({anchor} + {h}){alive('sm.measurement_date')})"
        ),
        "condition": (
            f"EXISTS (SELECT 1 FROM {t('condition_occurrence')} sc WHERE sc.person_id = e.subject_id "
            f"AND sc.condition_start_date >= ({anchor} + {h}){alive('sc.condition_start_date')})"
        ),
        "drug": (
            f"EXISTS (SELECT 1 FROM {t('drug_exposure')} sd WHERE sd.person_id = e.subject_id "
            f"AND sd.drug_exposure_start_date >= ({anchor} + {h}){alive('sd.drug_exposure_start_date')})"
        ),
        "death": f"(e.death_date IS NOT NULL AND e.death_date >= ({anchor} + {h}))",
    }
    ors = "\n                OR ".join(clauses[k] for k in _FOLLOWUP_EVIDENCE if k in spec.evidence)
    return f"""CASE WHEN (
                {ors}
            ) THEN 1 ELSE 0 END AS has_subsequent_event,
            CASE WHEN (e.death_date IS NOT NULL AND e.death_date <= ({anchor} + {h})) THEN 1 ELSE 0 END AS died_in_followup"""


def _study_verified_sql(spec: _StudySpec) -> str:
    if not _study_followup_applicable(spec):
        return "CAST(NULL AS INTEGER)"
    parts = [] if spec.outcome == "none" else ["o.outcome_flag = 1"]
    parts.append("o.has_subsequent_event = 1")
    if spec.death_verifies_followup:
        parts.append("o.died_in_followup = 1")
    return f"CASE WHEN ({' OR '.join(parts)}) THEN 1 ELSE 0 END"


def _study_ctes(spec: _StudySpec, through: str = "ranked_stays") -> str:
    """The WITH clause, ending at CTE ``through`` (verified_stays for attrition, ranked_stays for the cohort)."""
    ctes = [
        _study_base_ctes(spec),
        f"eligible_stays AS (\n        {_study_eligible_select(spec, _study_conditions(spec))}\n    )",
        f"""outcomes_and_followup AS (
        SELECT
            e.*,
            {_study_outcome_sql(spec)},
            {_study_followup_sql(spec)}
        FROM eligible_stays e
    )""",
        f"verified_stays AS (\n        SELECT o.*, {_study_verified_sql(spec)} AS followup_verified\n        FROM outcomes_and_followup o\n    )",
    ]
    if through != "verified_stays":
        keep = "WHERE followup_verified = 1" if (spec.verified and _study_followup_applicable(spec)) else ""
        ctes.append(f"filtered_stays AS (\n        SELECT *\n        FROM verified_stays\n        {keep}\n    )")
        ctes.append(
            f"""ranked_stays AS (
        SELECT
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id
                ORDER BY {_study_order_by(spec.rule, spec.seed)}
            ) AS stay_rank
        FROM filtered_stays f
    )"""
        )
    return "WITH " + ",\n    ".join(ctes)


def _study_query_sql(spec: _StudySpec) -> str:
    return f"""{_study_ctes(spec)}
    SELECT
        {int(spec.cohort_id)} AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        visit_concept_id,
        outcome_flag,
        outcome_date,
        followup_verified,
        died_in_followup,
        age_at_index,
        los_days,
        discharged_to_concept_id,
        {_sql_str(spec.label)} AS target_outcome
    FROM ranked_stays
    WHERE stay_rank = 1
    ORDER BY subject_id, cohort_start_date"""


def _study_attrition_steps(spec: _StudySpec) -> list[tuple[str, str]]:
    """Cumulative CONSORT steps for compute_attrition, built from the same predicates as the cohort query."""
    conds = _study_conditions(spec)
    head = _study_base_ctes(spec)
    steps = []
    for k in range(1, len(conds) + 1):
        sql = (
            f"WITH {head}\n    SELECT subject_id, visit_occurrence_id FROM (\n"
            f"        {_study_eligible_select(spec, conds[:k])}\n    ) s"
        )
        steps.append((conds[k - 1][0], sql))
    if spec.verified and _study_followup_applicable(spec):
        anchor_label = "index start" if spec.outcome == "fixed_window" else "discharge"
        by_death = ", or death by then" if spec.death_verifies_followup else ""
        steps.append((
            f"Verified follow-up (evidence >= {int(spec.window_days)} days after {anchor_label}{by_death})",
            f"{_study_ctes(spec, through='verified_stays')}\n    SELECT subject_id, visit_occurrence_id FROM verified_stays WHERE followup_verified = 1",
        ))
    return steps


# ---- materialisation --------------------------------------------------------------------------------

def _materialize_study_cohort(
    con: duckdb.DuckDBPyConnection,
    spec: _StudySpec,
    tmp: str,
    *,
    name: str,
    description: str,
    outcome_cohort_id: int | None = None,
    outcome_name: str | None = None,
    outcome_description: str | None = None,
    outcome_style: str = "visits",
    overwrite: bool = True,
) -> None:
    """Writes the cohort (and optional outcome cohort) held in temp table ``tmp`` to the OMOP cohort tables.

    ``outcome_style='visits'`` writes one row per qualifying readmission visit (its start/end dates);
    ``'event_date'`` writes one point event per patient on ``outcome_date``.
    """
    ensure_cohort_tables(con)
    t_cohort, t_def = _tbl(spec, "cohort"), _tbl(spec, "cohort_definition")
    today = datetime.date.today().isoformat()
    cid = int(spec.cohort_id)
    ids = [cid] + ([int(outcome_cohort_id)] if outcome_cohort_id is not None else [])

    if overwrite:
        for i in ids:
            con.execute(f"DELETE FROM {t_cohort} WHERE cohort_definition_id = {i};")
            con.execute(f"DELETE FROM {t_def} WHERE cohort_definition_id = {i};")

    def define(i: int, nm: str, desc: str) -> None:
        con.execute(f"""
            INSERT INTO {t_def} (
                cohort_definition_id, cohort_definition_name, cohort_definition_description,
                definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
            ) VALUES (
                {i}, {_sql_str(nm)}, {_sql_str(desc)}, 0, NULL, 0, DATE '{today}'
            );
        """)

    define(cid, name, description)
    con.execute(f"""
        INSERT INTO {t_cohort} (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT DISTINCT cohort_definition_id, subject_id, cohort_start_date, cohort_end_date
        FROM {tmp};
    """)

    if outcome_cohort_id is not None:
        oid = int(outcome_cohort_id)
        define(oid, outcome_name, outcome_description)
        if outcome_style == "visits":
            con.execute(f"""
                INSERT INTO {t_cohort} (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
                SELECT DISTINCT
                    {oid} AS cohort_definition_id,
                    tc.subject_id,
                    ro.visit_start_date AS cohort_start_date,
                    ro.visit_end_date AS cohort_end_date
                FROM {tmp} tc
                JOIN {_tbl(spec, 'visit_occurrence')} ro ON tc.subject_id = ro.person_id
                WHERE tc.outcome_flag = 1
                  AND ro.visit_occurrence_id != tc.visit_occurrence_id
                  AND ro.visit_concept_id IN ({_int_list(spec.outcome_visit_ids)})
                  AND ro.visit_start_date > (tc.cohort_end_date + {int(spec.gap_days)})
                  AND ro.visit_start_date <= (tc.cohort_end_date + {int(spec.window_days)});
            """)
        else:
            con.execute(f"""
                INSERT INTO {t_cohort} (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
                SELECT DISTINCT
                    {oid} AS cohort_definition_id,
                    subject_id,
                    COALESCE(outcome_date, cohort_end_date) AS cohort_start_date,
                    COALESCE(outcome_date, cohort_end_date) AS cohort_end_date
                FROM {tmp}
                WHERE outcome_flag = 1;
            """)


_STUDY_COLUMNS = (
    "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, visit_concept_id, "
    "outcome_flag, outcome_date, followup_verified, died_in_followup, age_at_index, los_days, "
    "discharged_to_concept_id, target_outcome"
)


def _run_study_engine(
    con: duckdb.DuckDBPyConnection,
    spec: _StudySpec,
    projection: str,
    *,
    materialise: bool,
    table_name: str | None,
    materialize_kwargs: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Builds the cohort in a temp table, optionally materialises it, and returns ``projection`` as a frame."""
    table_sql = _quote_ident_path(table_name, "table_name", 3) if table_name else None
    tmp = _STUDY_TMP_TABLE
    con.execute(f"CREATE OR REPLACE TEMP TABLE {tmp} AS {_study_query_sql(spec)}")
    try:
        try:
            if materialise:
                _materialize_study_cohort(con, spec, tmp, **(materialize_kwargs or {}))
            if table_sql:
                con.execute(f"DROP TABLE IF EXISTS {table_sql};")
                con.execute(f"CREATE TABLE {table_sql} AS SELECT {projection} FROM {tmp} ORDER BY subject_id;")
        except duckdb.Error as exc:
            if "read-only" in str(exc).lower():
                raise RuntimeError(
                    "Cannot write the cohort: the connection is read-only. Pass materialise=False (and no "
                    "table_name) to build the cohort without writing to the database."
                ) from exc
            raise
        return con.execute(f"SELECT {projection} FROM {tmp} ORDER BY subject_id;").df()
    finally:
        con.execute(f"DROP TABLE IF EXISTS {tmp};")


# ---- argument validation (define_study_cohort) -----------------------------------------------------

def _whole_number(value: Any, name: str, *, minimum: int | None = None, allow_none: bool = False) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} must not be None.")
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} must be a whole number, got {value!r}.")
    if isinstance(value, numbers.Integral):
        n = int(value)
    else:
        f = float(value)
        if not f.is_integer():
            raise ValueError(f"{name} must be a whole number, got {value!r}.")
        n = int(f)
    if minimum is not None and n < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {n}.")
    return n


def _flag(value: Any, name: str) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    raise ValueError(f"{name} must be True or False, got {value!r}.")


def _materialise_mode(value: Any) -> bool | str:
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    raise ValueError(f"materialise must be True, False or 'auto', got {value!r}.")


def _database_is_read_only(con: duckdb.DuckDBPyConnection, schema: str | None) -> bool:
    """True when the database that holds the cohort tables was opened read-only."""
    catalog = schema.split(".")[0] if schema is not None and "." in schema else None
    if catalog is None:
        row = con.execute("SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()").fetchone()
    else:
        row = con.execute("SELECT readonly FROM duckdb_databases() WHERE database_name = ?", [catalog]).fetchone()
    return bool(row and row[0])


def _iso_date(value: Any, name: str) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NaT:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and _ISO_DATE_RE.match(value.strip()):
        try:
            return datetime.date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise ValueError(f"{name} must be a valid date ('YYYY-MM-DD', a date, or None), got {value!r}.")


def _resolve_study_window(study_window: Any) -> tuple[str | None, str | None]:
    if study_window is None:
        return None, None
    if isinstance(study_window, (str, bytes)) or not isinstance(study_window, (tuple, list)) or len(study_window) != 2:
        raise ValueError("study_window must be None or a (start, end) pair; either element may be None.")
    start = _iso_date(study_window[0], "study_window start")
    end = _iso_date(study_window[1], "study_window end")
    if start is not None and end is not None and start > end:
        raise ValueError(f"study_window start ({start}) must not be after its end ({end}).")
    return start, end


def _resolve_visit_types(value: Any, name: str, *, allow_none: bool) -> tuple[int, ...] | None:
    """'inpatient' / 'emergency' / 'outpatient', visit_concept_ids, or a mix of both in a list."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} must not be None.")
    items = [value] if isinstance(value, (str, numbers.Real)) else list(value)
    if not items:
        raise ValueError(f"{name} must not be empty.")
    out: list[int] = []
    for item in items:
        if isinstance(item, str):
            key = item.strip().lower()
            if key in _VISIT_TYPE_CONCEPTS:
                out.extend(_VISIT_TYPE_CONCEPTS[key])
            elif key.isdigit():
                out.append(int(key))
            else:
                raise ValueError(
                    f"Unknown {name} '{item}'. Use 'inpatient', 'emergency', 'outpatient', or explicit visit_concept_ids."
                )
        else:
            out.append(_whole_number(item, f"{name} concept id"))
    return tuple(sorted(set(out)))


def _resolve_choices(value: Any, allowed: tuple[str, ...], name: str) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else list(value)
    cleaned = [str(x).strip().lower() for x in items]
    bad = [x for x in cleaned if x not in allowed]
    if not cleaned or bad:
        raise ValueError(f"{name} must be a non-empty subset of {allowed}; got {items!r}.")
    return tuple(a for a in allowed if a in cleaned)


def _resolve_death_ids(value: Any, name: str) -> tuple[int, ...]:
    items = [value] if isinstance(value, numbers.Real) else list(value)
    if not items:
        raise ValueError(f"{name} must not be empty.")
    return tuple(sorted({_whole_number(x, f"{name} concept id") for x in items}))


def _resolve_outcome(target_outcome: Any, mortality_type: Any, followup_days: int | None) -> tuple[str, str]:
    """Maps target_outcome (+ mortality_type) to (engine outcome family, target_outcome label)."""
    if not isinstance(target_outcome, str):
        raise ValueError(f"target_outcome must be a string, got {target_outcome!r}.")
    t = target_outcome.strip().lower()
    m = _ALL_CAUSE_RE.match(t)
    if m:
        if m.group(1) is not None and followup_days is not None and int(m.group(1)) != followup_days:
            raise ValueError(
                f"target_outcome '{target_outcome}' implies a {int(m.group(1))}-day window but followup_days={followup_days}; "
                "make them agree or use 'all_cause_readmission'."
            )
        return "readmission", "all_cause_readmission"
    if t == "none":
        return "none", "none"
    if t in ("readmission_or_death", "composite_readmit_or_death"):
        return "composite", "readmission_or_death"
    if t == "mortality":
        mt = str(mortality_type).strip().lower()
        if mt == "composite_readmit_or_death":
            return "composite", "readmission_or_death"
        if mt not in ("in_hospital", "post_discharge", "fixed_window"):
            raise ValueError(
                f"Unsupported mortality_type '{mortality_type}'. Must be one of 'in_hospital', 'post_discharge', "
                "'fixed_window' (or 'composite_readmit_or_death')."
            )
        return mt, f"mortality_{mt}"
    raise ValueError(
        f"Unsupported target_outcome '{target_outcome}'. Use 'none', 'all_cause_readmission' (alias "
        "'all_cause_readmission_<N>d'), 'mortality', or 'readmission_or_death'."
    )


def _describe_study(spec: _StudySpec) -> str:
    bits = [f"index visit concept ids {'any' if spec.visit_ids is None else '(' + _int_list(spec.visit_ids) + ')'}"]
    if spec.study_start or spec.study_end:
        bits.append(f"index start {spec.study_start or '*'} to {spec.study_end or '*'}")
    if spec.min_age is not None:
        bits.append(f"age >= {spec.min_age} ({spec.age_method})")
    if spec.min_los_days is not None:
        bits.append(f"LOS >= {spec.min_los_days} d")
    if spec.washin_days:
        bits.append(f"wash-in {spec.washin_days} d")
    bits.append("exclude in-hospital death" if spec.exclude_death else "in-hospital death retained")
    bits.append(f"outcome {spec.label}")
    if spec.window_days is not None and spec.outcome != "in_hospital":
        bits.append(f"window ({spec.gap_days}, {spec.window_days}] d")
    if _study_early_death_applies(spec):
        bits.append("deaths before the outcome window excluded")
    if spec.episode_merge_days is not None:
        bits.append(f"stays merged into episodes (<= {spec.episode_merge_days} d apart)")
    bits.append("verified follow-up required" if (spec.verified and _study_followup_applicable(spec)) else "verified follow-up not required")
    bits.append(f"sampling {spec.rule}" + (f" (seed {spec.seed})" if spec.rule == "random" else ""))
    return "Study cohort: " + "; ".join(bits)


def define_study_cohort(
    con: duckdb.DuckDBPyConnection,
    *,
    visit_type: str | int | Sequence[str | int] | None = "inpatient",
    study_window: tuple[Any, Any] | None = None,
    min_age: int | None = 18,
    min_los_days: int | str | None = "auto",
    washin_days: int | None = 365,
    followup_days: int | None = 30,
    exclude_in_hospital_death: bool | str = "auto",
    target_outcome: str = "all_cause_readmission",
    mortality_type: str = "post_discharge",
    outcome_visit_type: str | int | Sequence[str | int] = "inpatient",
    gap_days: int = 0,
    sampling_rule: str = "random",
    random_state: int = 42,
    require_verified_followup: bool = True,
    death_discharge_concept_ids: int | Sequence[int] | None = None,
    death_sources: str | Sequence[str] | None = None,
    followup_evidence: str | Sequence[str] | None = None,
    age_method: str = "completed_years",
    washin_fallback: str = "no_observation_period",
    exclude_early_death: bool = True,
    death_counts_as_verified_followup: bool = True,
    episode_merge_days: int | None = None,
    cohort_definition_id: int = 1,
    outcome_cohort_id: int | None = None,
    cohort_name: str | None = None,
    cohort_description: str | None = None,
    materialise: bool | str = "auto",
    table_name: str | None = None,
    schema: str | None = None,
    attrition: bool = True,
) -> pd.DataFrame:
    r"""Defines a leak-free index-stay study cohort (one stay per patient) with an optional outcome.

    A single parameterised template that generalises :func:`build_readmission_cohort` and
    :func:`build_end_of_life_cohort` (both run on the same engine). It adds emergency / outpatient / custom index
    visit types, a study window on the index date, and a cohort-only mode (``target_outcome='none'``).

    The defaults here are the corrected, clinically safest settings. The legacy builders keep their historical
    conventions through the options ``age_method``, ``washin_fallback``, ``exclude_early_death`` and
    ``death_counts_as_verified_followup`` (see "Reproducing the legacy builders"): with those values a cohort
    defined here and one built by a legacy builder are identical row for row.

    All arguments are keyword-only.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection to an OMOP CDM v5.4 DuckDB database. May be read-only (see ``materialise``).
    visit_type : str, int, sequence or None, default "inpatient"
        Index visit type: ``'inpatient'`` (visit_concept_id 9201), ``'emergency'`` (9203), ``'outpatient'``
        (9202), explicit visit_concept_ids, or a list mixing both (``['inpatient', 'emergency']``). ``None``
        accepts any visit. Site-specific concepts (for example a local inpatient concept) are passed as ids.
    study_window : (start, end) or None
        Inclusive bounds on the index visit **start date**; either end may be ``None``. Dates are ``'YYYY-MM-DD'``
        strings, ``datetime.date`` or ``datetime.datetime`` values. Only the index date is restricted: follow-up
        and outcome ascertainment may use records after ``end``.
    min_age : int or None, default 18
        Minimum age at the index visit start (see ``age_method``). ``None`` disables the age rule.
    min_los_days : int, None or "auto", default "auto"
        Minimum length of stay in days (``visit_end_date - visit_start_date``); ``None`` disables the rule.
        ``"auto"`` depends on the index visit type: for an inpatient index (``visit_type='inpatient'``, i.e. concept
        9201 only) it is 1 for readmission, post-discharge mortality, composite and cohort-only cohorts, 0 for
        in-hospital mortality and no restriction for fixed-window mortality; for every other index visit type
        (emergency, outpatient, a mix, custom ids, ``None``) it is 0 (or no restriction), because those visits are
        usually same-day and a 1-day minimum would silently empty the cohort. Pass an explicit value (for example
        ``1`` for a site-specific inpatient concept id) to override.
    washin_days : int or None, default 365
        Required prior observation. The index stay qualifies when an ``observation_period`` satisfies
        ``start <= index_start - washin_days`` and ``end >= index_start``; see ``washin_fallback`` for patients who
        have no observation period at all. ``0`` / ``None`` disables the rule.
    washin_fallback : {"no_observation_period", "always", "never"}, default "no_observation_period"
        How a prior visit counts as wash-in evidence (another visit of the patient started on or before
        ``index_start - washin_days``, inclusive). ``"no_observation_period"``: only for a patient who has **no**
        ``observation_period`` row at all (sources without observation periods); a patient whose observation
        periods exist but do not cover the wash-in window fails it. ``"always"`` (the pre-0.5.3 behaviour of the
        legacy builders): a prior visit also rescues patients whose observation periods do not cover the window.
        ``"never"``: observation periods only.
    followup_days : int or None, default 30
        Length F of the follow-up / outcome horizon in days. Required for every outcome except
        ``'none'`` (where ``None`` also disables follow-up verification) and in-hospital mortality (ignored).
    exclude_in_hospital_death : bool or "auto", default "auto"
        Drops index stays that ended in death: the stay's discharge disposition is one of
        ``death_discharge_concept_ids`` **or** the patient's earliest known death date is on/before the stay's end
        (sources chosen by ``death_sources``). When False, only stays starting after the patient's death date are
        dropped. ``"auto"``: True except for in-hospital and fixed-window mortality. True is rejected for
        in-hospital mortality and False for post-discharge / composite outcomes, because either would mislabel
        deaths.
    target_outcome : {"all_cause_readmission", "none", "mortality", "readmission_or_death"}
        ``'all_cause_readmission'`` (alias ``'all_cause_readmission_<N>d'`` with N equal to ``followup_days``):
        another ``outcome_visit_type`` visit starts in (e + G, e + F]. ``'mortality'``: see ``mortality_type``.
        ``'readmission_or_death'`` (alias ``'composite_readmit_or_death'``): readmission or death in (e + G, e + F].
        ``'none'``: cohort only; ``outcome_flag`` and ``outcome_date`` are NULL.
    mortality_type : {"post_discharge", "in_hospital", "fixed_window"}
        Used when ``target_outcome='mortality'``. ``post_discharge``: death in (e + G, e + F];
        ``fixed_window``: death in (s + G, s + F] measured from the index start (SARD end-of-life style, for example
        ``gap_days=90, followup_days=365`` for 3-12 month mortality); ``in_hospital``: discharge to a death
        concept, or a death date within [s, e]. A death before the window opens (``<= e + G`` or ``<= s + G``) is
        neither an event nor a survivor; see ``exclude_early_death``.
    outcome_visit_type : str, int or sequence, default "inpatient"
        Visit types that count as readmissions (``readmission`` and ``readmission_or_death`` outcomes).
    gap_days : int, default 0
        G, the gap between the window anchor and the start of the outcome window (``grace_days`` in
        ``build_readmission_cohort``, ``gap_days`` in ``build_end_of_life_cohort``). Must be < ``followup_days``.
    sampling_rule : {"random", "first", "last"}, default "random"
        Which eligible stay represents a patient: earliest start (``first``), latest start (``last``) or the
        stay minimising ``hash(visit_occurrence_id, random_state)`` (``random``, reproducible; ties broken by
        visit_occurrence_id).
    random_state : int, default 42
        Seed of the ``random`` rule.
    require_verified_followup : bool, default True
        Keeps a stay only if it has an outcome event, or evidence of observation on/after anchor + F (see
        ``followup_evidence``), or a death by then (see ``death_counts_as_verified_followup``), preventing
        lost-to-follow-up bias. Not applicable to in-hospital mortality or when ``followup_days`` is ``None``. The
        result column ``followup_verified`` is reported either way.
    death_discharge_concept_ids : int or sequence, optional
        Discharge-disposition concepts meaning "died". Default: ``(4216643,)`` ('Patient died') for every outcome.
        4155309 is deliberately not a default: it is 'Ileal part' (an anatomic site) in Athena and this package's
        PCORnet ETL writes it for 'left against medical advice', so counting it labelled AMA discharges as deaths
        (builders before 0.5.3 did; pass ``(4216643, 4155309)`` to reproduce that).
    death_sources : {"death_table", "discharge_disposition"} or sequence, optional
        Where a patient's death date comes from: the ``death`` table and / or the end date of any visit
        discharged to a death concept. Default: ``death_table`` only for readmission / cohort-only, both for
        mortality outcomes.
    followup_evidence : sequence of str, optional
        Records that verify follow-up when dated on/after anchor + F: any of ``'observation_period'`` (period end),
        ``'visit'`` (another visit's start), ``'measurement'``, ``'condition'``, ``'drug'`` (record dates) and
        ``'death'`` (death date). Default: the four record types for readmission / cohort-only, all six for
        mortality outcomes.
    age_method : {"completed_years", "year_difference"}, default "completed_years"
        ``completed_years`` is the exact age in whole years at the index start (``date_sub('year', birth,
        index_start)``; the birthday counts as reached on its day, 29 February as 28 February in other years).
        ``year_difference`` (what the legacy builders use) is ``date_diff('year', birth, index_start)``, the number
        of calendar-year boundaries crossed, which overstates age by up to a year before the birthday (a 17-year-old
        passes ``min_age=18``). The birth date is the person's year / month / day of birth; a missing month or day
        counts as 1, which overstates the age by up to a year for such patients with either method.
    exclude_early_death : bool, default True
        Applies to post-discharge mortality, fixed-window mortality and ``readmission_or_death``. Drops an index
        stay when the patient's death date is on or before ``anchor + gap_days`` (the anchor is the discharge date
        ``e``, or the index start ``s`` for fixed-window mortality): a death before the outcome window opens is
        neither an event nor a survivor, so labelling it ``outcome_flag = 0`` (and, with verified follow-up,
        silently dropping it) would bias the mortality rate downward. With the default ``gap_days=0`` this only
        adds, for fixed-window mortality, the death on the index start day itself (post-discharge / composite
        cohorts already drop deaths up to the discharge date). The SARD end-of-life protocol excludes deaths inside
        the gap the same way. ``False`` is the pre-0.5.3 legacy behaviour: such stays stay in the cohort as
        survivors. The attrition table shows the step.
    death_counts_as_verified_followup : bool, default True
        A death is a known outcome. With True, a patient whose death date is on or before ``anchor + followup_days``
        counts as verified follow-up (observation is complete until death) and is kept with
        ``outcome_flag = 0`` when not readmitted, instead of being dropped as lost to follow-up; the column
        ``died_in_followup`` flags them. Records dated after the patient's death date (a lagged or mis-dated
        measurement, visit, condition or drug exposure) never count as follow-up evidence. ``False`` is the
        pre-0.5.3 legacy protocol: a death inside the window does not verify follow-up (the patient is dropped
        unless other evidence dated on/after ``anchor + followup_days`` exists, post-death records included).
    episode_merge_days : int or None, default None
        ``None``: every index-type visit is a candidate index stay. A number N merges index-type stays of a patient
        that overlap or start within N days after the previous stay's end into one hospitalisation episode (0:
        overlaps and same-day transfers; 1: also a continuation on the next day) before any rule is applied: the
        episode starts with its earliest stay (``visit_occurrence_id`` and ``visit_concept_id`` come from it), ends
        with the latest end date and takes the discharge disposition of the stay that ends last, so length of stay,
        discharge date, in-hospital death and readmission windows refer to the whole hospitalisation. Without
        merging, a transfer makes the first segment the index stay: it is shorter, and a death after the transfer is
        not seen on it.
    cohort_definition_id : int, default 1
        Id written to ``cohort`` / ``cohort_definition``.
    outcome_cohort_id : int, optional
        When given (and ``materialise=True``), the outcome events are written under this id. Readmission
        outcomes write each qualifying readmission visit (start, end); mortality and composite outcomes write a
        one-day event on ``outcome_date``.
    cohort_name, cohort_description : str, optional
        Metadata for ``cohort_definition``; the description defaults to a summary of all settings.
    materialise : bool or "auto", default "auto"
        ``True`` writes the cohort to the OMOP ``cohort`` / ``cohort_definition`` tables (created if missing),
        exactly as the legacy builders do; rows for the same ids are replaced, so re-running is idempotent. It
        raises ``RuntimeError`` on a read-only connection. ``False`` leaves the database untouched. ``"auto"``
        writes unless the connection is read-only, in which case it emits a ``UserWarning`` and behaves like
        ``False`` (so the same call works on ``omop_connect(..., read_only=True)`` connections).
    table_name : str, optional
        Also persist the full result as this table (up to three dot-separated plain identifiers).
    schema : str, optional
        Schema (or ``catalog.schema``) holding the CDM tables. Default: resolved through the search path.
    attrition : bool, default True
        Compute the stepwise patient attrition (via :func:`compute_attrition`) into ``attrs``.

    Returns
    -------
    pandas.DataFrame
        One row per patient, ordered by ``subject_id``: ``cohort_definition_id, subject_id, cohort_start_date``
        (index visit start), ``cohort_end_date`` (index visit end), ``visit_occurrence_id, visit_concept_id,
        outcome_flag, outcome_date, followup_verified, age_at_index, los_days, discharged_to_concept_id,
        target_outcome``. ``attrs['attrition']`` holds the :func:`compute_attrition` table (CONSORT steps, in the
        order: visit type, study window, age, length of stay, wash-in, in-hospital death, verified follow-up),
        ``attrs['summary']`` the :func:`get_cohort_summary` row when materialised into the default schema, and
        ``attrs['definition']`` a dict of the resolved settings.

    Raises
    ------
    ValueError
        For an unknown ``visit_type`` / ``target_outcome`` / ``sampling_rule``, negative or non-whole numbers,
        a malformed or inverted ``study_window``, inconsistent windows (``gap_days >= followup_days``),
        contradictory outcome / death settings, or invalid identifiers.
    RuntimeError
        If ``materialise=True`` (or ``table_name``) is requested on a read-only connection.

    Warns
    -----
    UserWarning
        With ``materialise="auto"`` on a read-only connection: the cohort is returned but not written.

    Notes
    -----
    With ``s`` the index visit start date and ``e`` its end date (DATE values; ``+ n`` adds n days), ``F`` =
    ``followup_days`` and ``G`` = ``gap_days``:

    * **Index stay**: ``visit_concept_id`` in the visit type, ``study_start <= s <= study_end`` (both ends
      inclusive), age at ``s`` >= ``min_age``, ``e - s >= min_los_days``, wash-in satisfied, not an in-hospital
      death.
    * **Wash-in**: an ``observation_period`` with ``start <= s - washin_days`` and ``end >= s`` exists or, as
      fallback evidence when observation periods are missing, another visit of the patient started on or before
      ``s - washin_days`` (inclusive).
    * **Outcome windows** (open on the left, closed on the right): readmission ``e + G < start <= e + F``;
      post-discharge death ``e + G < death <= e + F``; fixed-window death ``s + G < death <= s + F``; in-hospital
      death ``s <= death <= e`` or a discharge disposition in ``death_discharge_concept_ids``.
    * **Verified follow-up**: an outcome event, or evidence (see ``followup_evidence``) dated ``>= anchor + F``,
      where the anchor is ``e`` (``s`` for fixed-window mortality).
    * **Leak-free scoping**: history (wash-in) lies at or before ``s`` and outcomes strictly after the anchor, so
      lookback and outcome periods are disjoint; follow-up evidence is dated ``>= anchor + F``. ``study_window``
      restricts only the index date: follow-up and outcome ascertainment may use records after ``study_end``.

    Reproducing the legacy builders (every example below is asserted against stored golden results):

    * ``build_readmission_cohort(target_visit_concept_ids=T, outcome_visit_concept_ids=O, followup_window_days=F,
      grace_days=G, washin_days=W, index_selection_rule=R, random_state=S, require_verified_followup=V)`` ==
      ``define_study_cohort(visit_type=T, outcome_visit_type=O, followup_days=F, gap_days=G, washin_days=W,
      sampling_rule=R, random_state=S, require_verified_followup=V, min_age=18)``;
    * ``build_end_of_life_cohort(mortality_type=M, mortality_window_days=F, gap_days=G, min_age=A, ...)`` ==
      ``define_study_cohort(target_outcome='mortality', mortality_type=M, followup_days=F, gap_days=G, min_age=A, ...)``
      (for ``composite_readmit_or_death`` use ``target_outcome='readmission_or_death'``).

    Examples
    --------
    >>> cohort = define_study_cohort(
    ...     con,
    ...     visit_type="inpatient",
    ...     study_window=("2017-01-01", "2020-12-31"),
    ...     min_age=18,
    ...     min_los_days=1,
    ...     washin_days=365,
    ...     followup_days=30,
    ...     exclude_in_hospital_death=True,
    ...     target_outcome="all_cause_readmission_30d",
    ... )  # doctest: +SKIP
    >>> cohort.attrs["attrition"]  # doctest: +SKIP
    """
    visit_ids = _resolve_visit_types(visit_type, "visit_type", allow_none=True)
    study_start, study_end = _resolve_study_window(study_window)
    min_age_n = _whole_number(min_age, "min_age", minimum=0, allow_none=True)
    washin_n = _whole_number(washin_days, "washin_days", minimum=0, allow_none=True) or 0
    followup_n = _whole_number(followup_days, "followup_days", minimum=1, allow_none=True)
    gap_n = _whole_number(gap_days, "gap_days", minimum=0)
    seed_n = _whole_number(random_state, "random_state")
    cohort_id = _whole_number(cohort_definition_id, "cohort_definition_id")
    verified = _flag(require_verified_followup, "require_verified_followup")
    materialise = _materialise_mode(materialise)
    attrition = _flag(attrition, "attrition")

    rule = str(sampling_rule).strip().lower()
    if rule not in _SAMPLING_RULES:
        raise ValueError(f"Unsupported sampling_rule '{sampling_rule}'. Use 'first', 'last', or 'random'.")
    method = str(age_method).strip().lower()
    if method not in _AGE_METHODS:
        raise ValueError(f"Unsupported age_method '{age_method}'. Use one of {_AGE_METHODS}.")
    if schema is not None:
        _quote_ident_path(schema, "schema", 2)
    if table_name is not None:
        _quote_ident_path(table_name, "table_name", 3)

    outcome, label = _resolve_outcome(target_outcome, mortality_type, followup_n)

    if min_los_days == "auto":
        min_los = _AUTO_STAY_RULES[outcome][0]
    else:
        min_los = _whole_number(min_los_days, "min_los_days", minimum=0, allow_none=True)
    if exclude_in_hospital_death == "auto":
        exclude = _AUTO_STAY_RULES[outcome][1]
    else:
        exclude = _flag(exclude_in_hospital_death, "exclude_in_hospital_death")
    if outcome == "in_hospital" and exclude:
        raise ValueError(
            "exclude_in_hospital_death=True cannot be combined with in-hospital mortality: it would remove every event."
        )
    if outcome in ("post_discharge", "composite") and not exclude:
        raise ValueError(
            "Post-discharge mortality and readmission_or_death need exclude_in_hospital_death=True: "
            "stays that ended in death would otherwise be labelled as survivors."
        )

    needs_window = outcome in ("readmission", "post_discharge", "fixed_window", "composite")
    if needs_window:
        if followup_n is None:
            raise ValueError(f"followup_days is required for target_outcome '{label}'.")
        if gap_n >= followup_n:
            raise ValueError(
                f"Inconsistent windows: gap_days ({gap_n}) must be smaller than followup_days ({followup_n}); "
                "the outcome window would be empty."
            )
    else:
        if gap_n != 0:
            raise ValueError(f"gap_days has no effect for target_outcome '{label}'; leave it at 0.")
        if outcome == "none" and verified and followup_n is None:
            raise ValueError(
                "require_verified_followup=True needs followup_days when target_outcome='none'; "
                "set require_verified_followup=False or provide followup_days."
            )
    window = followup_n if outcome != "in_hospital" else None

    outcome_ids = _resolve_visit_types(
        "inpatient" if outcome_visit_type is None else outcome_visit_type, "outcome_visit_type", allow_none=False
    )

    conv = _END_OF_LIFE_CONVENTIONS if outcome in _MORTALITY_FAMILIES else _READMISSION_CONVENTIONS
    death_ids = conv["death_ids"] if death_discharge_concept_ids is None else _resolve_death_ids(
        death_discharge_concept_ids, "death_discharge_concept_ids")
    sources = conv["death_sources"] if death_sources is None else _resolve_choices(
        death_sources, _DEATH_SOURCES, "death_sources")
    evidence = conv["evidence"] if followup_evidence is None else _resolve_choices(
        followup_evidence, _FOLLOWUP_EVIDENCE, "followup_evidence")

    oid = _whole_number(outcome_cohort_id, "outcome_cohort_id", allow_none=True)
    if oid is not None:
        if outcome == "none":
            raise ValueError("outcome_cohort_id cannot be used with target_outcome='none' (there are no outcome events).")
        if oid == cohort_id:
            raise ValueError("outcome_cohort_id must differ from cohort_definition_id.")

    spec = _StudySpec(
        cohort_id=cohort_id, outcome=outcome, label=label, schema=schema, visit_ids=visit_ids,
        outcome_visit_ids=outcome_ids, study_start=study_start, study_end=study_end, min_age=min_age_n,
        age_method=method, min_los_days=min_los, washin_days=washin_n, exclude_death=exclude,
        death_ids=tuple(death_ids), death_sources=tuple(sources), window_days=window, gap_days=gap_n,
        verified=verified, evidence=tuple(evidence), rule=rule, seed=seed_n,
    )

    if materialise == "auto":
        if _database_is_read_only(con, schema):
            warnings.warn(
                "The connection is read-only, so the cohort was returned but not written to the cohort / "
                "cohort_definition tables. Pass materialise=False to silence this warning.",
                UserWarning,
                stacklevel=2,
            )
            materialise = False
        else:
            materialise = True

    name = cohort_name or "Study Cohort"
    mat_kwargs = dict(
        name=name,
        description=cohort_description or _describe_study(spec),
        outcome_cohort_id=oid,
        outcome_name=f"{name} - Outcome ({label})",
        outcome_description=f"Outcome events ({label}) for {name}",
        outcome_style="visits" if outcome == "readmission" else "event_date",
        overwrite=True,
    )
    df = _run_study_engine(con, spec, _STUDY_COLUMNS, materialise=materialise, table_name=table_name,
                           materialize_kwargs=mat_kwargs)

    if attrition:
        df.attrs["attrition"] = compute_attrition(con, cohort_id, _study_attrition_steps(spec))
    if materialise and schema is None:
        df.attrs["summary"] = get_cohort_summary(con, cohort_id)
    df.attrs["definition"] = {
        "visit_concept_ids": None if visit_ids is None else list(visit_ids),
        "study_window": [study_start, study_end],
        "min_age": min_age_n,
        "age_method": method,
        "min_los_days": min_los,
        "washin_days": washin_n,
        "followup_days": window,
        "gap_days": gap_n,
        "exclude_in_hospital_death": exclude,
        "target_outcome": label,
        "outcome_visit_concept_ids": list(outcome_ids),
        "sampling_rule": rule,
        "random_state": seed_n,
        "require_verified_followup": verified and _study_followup_applicable(spec),
        "death_discharge_concept_ids": list(death_ids),
        "death_sources": list(sources),
        "followup_evidence": list(evidence),
        "schema": schema,
    }
    return df


# ---- legacy builders: thin wrappers over the engine -------------------------------------------------

def _legacy_ids(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, numbers.Integral):
        return (int(value),)
    return tuple(int(x) for x in value)


def build_readmission_cohort(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int = 1,
    outcome_cohort_id: int | None = None,
    cohort_name: str = "Inpatient 30-Day Readmission Cohort",
    cohort_description: str | None = None,
    target_visit_concept_ids: int | Sequence[int] = (9201,),
    outcome_visit_concept_ids: int | Sequence[int] = (9201,),
    followup_window_days: int = 30,
    grace_days: int = 0,
    washin_days: int = 365,
    index_selection_rule: str = "random",
    random_state: int = 42,
    require_verified_followup: bool = True,
    table_name: str | None = None,
) -> pd.DataFrame:
    r"""Constructs a leak-free 30-day readmission cohort in DuckDB.

    Implements standard hospital readmission protocol:
        1. Index Admission: Adult (age >= 18 at admission), inpatient visit,
           length of stay >= 1 day, discharged alive (no in-hospital death).
        2. Verified Follow-up: Admission is eligible only if patient has an acute
           readmission in (discharge + grace_days, discharge + followup_window_days],
           OR active system encounters >= discharge + followup_window_days (preventing
           lost-to-follow-up censoring bias).
        3. Index Sampling: When multiple eligible admissions exist, samples one per patient
           using a reproducible seed (or first/last).
        4. Outcome Scoping: All-cause readmission strictly scoped to post-discharge window,
           completely disjoint from prior lookback history.

    This is a thin wrapper over the shared study-cohort engine (see :func:`define_study_cohort`, which
    generalises it): the legacy conventions (death from the death table and the index stay's own discharge
    disposition 4216643; follow-up evidenced by later visits, measurements, conditions or drug exposures;
    age as calendar-year difference) are pinned here and covered by golden-file tests.

    Args:
        con: Active DuckDB connection.
        cohort_id: Integer identifier for target index cohort in OMOP cohort table.
        outcome_cohort_id: Optional integer cohort ID for materialized readmission outcome cohort.
        cohort_name: Display name for cohort_definition table.
        cohort_description: Description for cohort_definition table.
        target_visit_concept_ids: Visit concept IDs defining index stay (default 9201: Inpatient).
        outcome_visit_concept_ids: Visit concept IDs defining outcome readmission (default 9201).
        followup_window_days: Days post-discharge for readmission window (default 30).
        grace_days: Grace period days immediately post-discharge before outcome window begins (default 0).
        washin_days: Prior observation window requirement in days (default 365).
        index_selection_rule: 'random' (reproducible seed), 'first', or 'last'.
        random_state: Integer seed for reproducible random index admission sampling (default 42).
        require_verified_followup: Whether to filter out lost-to-follow-up censored patients (default True).
        table_name: Optional custom table name to materialize the full index cohort with metadata.

    Returns:
        pd.DataFrame: Cohort records with metadata (cohort_definition_id, subject_id,
                      cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag,
                      los_days, age_at_admission, discharged_to_concept_id).
    """
    rule = index_selection_rule.lower().strip()
    if rule not in _SAMPLING_RULES:
        raise ValueError(f"Unsupported index_selection_rule '{index_selection_rule}'. Use 'random', 'first', or 'last'.")

    spec = _StudySpec(
        cohort_id=int(cohort_id), outcome="readmission", label="all_cause_readmission",
        visit_ids=_legacy_ids(target_visit_concept_ids), outcome_visit_ids=_legacy_ids(outcome_visit_concept_ids),
        min_age=18, age_method="year_difference", min_los_days=1,
        washin_days=int(washin_days) if washin_days and washin_days > 0 else 0,
        exclude_death=True, death_ids=_READMISSION_CONVENTIONS["death_ids"],
        death_sources=_READMISSION_CONVENTIONS["death_sources"], window_days=int(followup_window_days),
        gap_days=int(grace_days), verified=bool(require_verified_followup),
        evidence=_READMISSION_CONVENTIONS["evidence"], rule=rule, seed=int(random_state),
    )
    projection = (
        "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag, "
        "los_days, age_at_index AS age_at_admission, discharged_to_concept_id"
    )
    return _run_study_engine(
        con, spec, projection, materialise=True, table_name=table_name,
        materialize_kwargs=dict(
            name=cohort_name,
            description=cohort_description or "Inpatient 30-day readmission index cohort",
            outcome_cohort_id=outcome_cohort_id,
            outcome_name=f"{cohort_name} - Readmission Outcome",
            outcome_description="Patients experiencing 30-day readmission outcome",
            outcome_style="visits",
            overwrite=True,
        ),
    )


def build_end_of_life_cohort(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int = 1,
    outcome_cohort_id: int | None = 2,
    cohort_name: str = "End of Life Cohort",
    cohort_description: str | None = None,
    mortality_type: str = "post_discharge",
    target_visit_concept_ids: int | Sequence[int] | None = (9201,),
    outcome_visit_concept_ids: int | Sequence[int] = (9201,),
    mortality_window_days: int = 30,
    gap_days: int = 0,
    min_age: int = 18,
    washin_days: int = 365,
    require_verified_followup: bool = True,
    index_selection_rule: str = "random",
    random_state: int = 42,
    table_name: str | None = None,
    schema: str = "main",
    overwrite: bool = True,
) -> pd.DataFrame:
    r"""Constructs a standardized End of Life / Mortality cohort in DuckDB.

    Supports four mortality ascertainment paradigms:
        1. "in_hospital": In-hospital mortality during the index stay (expired/died in hospital).
        2. "post_discharge": Post-discharge mortality within (t_discharge + gap_days, t_discharge + mortality_window_days].
        3. "fixed_window": SARD-style EOL prediction within (t_index + gap_days, t_index + mortality_window_days].
        4. "composite_readmit_or_death": Clinical composite of acute readmission OR all-cause death post-discharge.

    Resolves dual OMOP death sources:
        - `death` table: `death_date` linked to `person_id`.
        - `visit_occurrence` table: `discharged_to_concept_id IN (4216643, 4155309)` (Expired / Hospice).

    This is a thin wrapper over the shared study-cohort engine (see :func:`define_study_cohort`, which
    generalises it); its legacy conventions are pinned here and covered by golden-file tests.

    Args:
        con: Active DuckDB connection.
        cohort_id: Target cohort definition ID in OMOP cohort table (default 1).
        outcome_cohort_id: Optional outcome cohort definition ID (default 2). Set None to skip outcome cohort table population.
        cohort_name: Display name for cohort_definition table.
        cohort_description: Detailed description for cohort_definition table.
        mortality_type: Mode of mortality ascertainment ('in_hospital', 'post_discharge', 'fixed_window', 'composite_readmit_or_death').
        target_visit_concept_ids: Visit concept IDs defining qualifying index encounters (default 9201: Inpatient).
        outcome_visit_concept_ids: Visit concept IDs defining readmissions for composite mode (default 9201).
        mortality_window_days: Number of days for mortality outcome window (e.g. 30, 90, 180). Default 30.
        gap_days: Days between index date / discharge and start of outcome window (default 0; e.g. 90 for SARD EOL).
        min_age: Minimum patient age at index encounter (default 18).
        washin_days: Baseline continuous observation lookback requirement before index stay (default 365).
        require_verified_followup: If True, excludes lost-to-follow-up patients without confirmed survival follow-up (default True).
        index_selection_rule: 'random' (reproducible seed), 'first', or 'last'.
        random_state: Seed for reproducible random sampling (default 42).
        table_name: Optional custom table name in DuckDB to materialize cohort records with metadata.
        schema: CDM schema containing tables (default 'main').
        overwrite: If True, cleans existing cohort and cohort_definition entries for cohort_id and outcome_cohort_id (default True).

    Returns:
        pd.DataFrame: Cohort records with metadata (cohort_definition_id, subject_id,
                      cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag,
                      outcome_date, mortality_type, age_at_index, los_days, discharged_to_concept_id).
    """
    m_type = mortality_type.lower().strip()
    if m_type not in _MORTALITY_TYPES:
        raise ValueError(f"Unsupported mortality_type '{mortality_type}'. Must be one of {_MORTALITY_TYPES}.")

    rule = index_selection_rule.lower().strip()
    if rule not in _SAMPLING_RULES:
        raise ValueError(f"Unsupported index_selection_rule '{index_selection_rule}'. Use 'random', 'first', or 'last'.")

    outcome = "composite" if m_type == "composite_readmit_or_death" else m_type
    min_los, exclude = _AUTO_STAY_RULES[outcome]
    spec = _StudySpec(
        cohort_id=int(cohort_id), outcome=outcome, label=m_type, schema=schema,
        visit_ids=_legacy_ids(target_visit_concept_ids), outcome_visit_ids=_legacy_ids(outcome_visit_concept_ids),
        min_age=int(min_age), age_method="year_difference", min_los_days=min_los,
        washin_days=int(washin_days) if washin_days and washin_days > 0 else 0,
        exclude_death=exclude, death_ids=_END_OF_LIFE_CONVENTIONS["death_ids"],
        death_sources=_END_OF_LIFE_CONVENTIONS["death_sources"],
        window_days=int(mortality_window_days) if m_type != "in_hospital" else None, gap_days=int(gap_days),
        verified=bool(require_verified_followup), evidence=_END_OF_LIFE_CONVENTIONS["evidence"],
        rule=rule, seed=int(random_state),
    )
    projection = (
        "cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag, "
        "outcome_date, target_outcome AS mortality_type, age_at_index, los_days, discharged_to_concept_id"
    )
    return _run_study_engine(
        con, spec, projection, materialise=True, table_name=table_name,
        materialize_kwargs=dict(
            name=cohort_name,
            description=cohort_description or f"{cohort_name} ({m_type})",
            outcome_cohort_id=outcome_cohort_id,
            outcome_name=f"{cohort_name} Outcome",
            outcome_description=f"Outcome events for {cohort_name}",
            outcome_style="event_date",
            overwrite=bool(overwrite),
        ),
    )


# Alias for clinical consistency
build_mortality_cohort = build_end_of_life_cohort


def prepare_competing_risks_data(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    followup_window_days: int = 30,
    grace_days: int = 0,
    outcome_visit_concept_ids: int | Sequence[int] = (9201,),
) -> tuple[pd.DataFrame, dict]:
    r"""Prepares competing risk survival data for readmission vs. post-discharge mortality.

    Under CMS HRRP and traditional binary readmission models, patients who die post-discharge
    without an acute readmission are often either censored or mislabeled as non-events (survivors),
    introducing survivor bias into hospital benchmarking.

    This function formats index stays into a 3-state competing risk survival framework:
        - Status 0: Censored (event-free throughout the entire follow-up window)
        - Status 1: Primary event of interest (acute hospital readmission)
        - Status 2: Competing event (all-cause mortality post-discharge without readmission)

    Args:
        con: Active DuckDB connection.
        cohort_table: Source cohort table name containing index stays (default "cohort").
        cohort_id: Optional cohort_definition_id to filter cohort_table.
        followup_window_days: Observation horizon in days (default 30).
        grace_days: Days post-discharge before outcome evaluation begins (default 0).
        outcome_visit_concept_ids: Visit concept IDs for readmission (default (9201,)).

    Returns:
        tuple[pd.DataFrame, dict]:
            - DataFrame with columns:
                - subject_id: Person identifier
                - visit_occurrence_id: Index visit identifier
                - cohort_start_date: Index admission date
                - cohort_end_date: Index discharge date
                - time_days: Follow-up time in days (min of readmit, death, or followup window)
                - status: Competing risk status (0 = censored, 1 = readmission, 2 = competing death)
                - event_type: Descriptive label ("censored", "readmission", "death_competing")
                - composite_event: Binary indicator (1 if status in (1, 2) else 0)
            - Summary dictionary with event counts, crude rates, and competing-risk metrics.
    """
    if isinstance(outcome_visit_concept_ids, int):
        out_ids = [outcome_visit_concept_ids]
    else:
        out_ids = list(outcome_visit_concept_ids)
    out_in = ", ".join(str(int(x)) for x in out_ids)

    cohort_filter = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""

    cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()]
    vid_col = "c.visit_occurrence_id" if "visit_occurrence_id" in cols else "v.visit_occurrence_id"

    query = f"""
    WITH cohort_src AS (
        SELECT 
            c.subject_id,
            c.cohort_start_date,
            c.cohort_end_date,
            {vid_col} AS visit_occurrence_id
        FROM {cohort_table} c
        LEFT JOIN visit_occurrence v 
          ON v.person_id = c.subject_id 
         AND v.visit_start_date = c.cohort_start_date
        {cohort_filter}
    ),
    earliest_readmit AS (
        SELECT 
            cs.subject_id,
            cs.cohort_start_date,
            cs.cohort_end_date,
            cs.visit_occurrence_id,
            MIN(ro.visit_start_date) AS readmit_date
        FROM cohort_src cs
        LEFT JOIN visit_occurrence ro
          ON ro.person_id = cs.subject_id
         AND ro.visit_occurrence_id != cs.visit_occurrence_id
         AND ro.visit_concept_id IN ({out_in})
         AND ro.visit_start_date > (cs.cohort_end_date + {int(grace_days)})
         AND ro.visit_start_date <= (cs.cohort_end_date + {int(followup_window_days)})
        GROUP BY cs.subject_id, cs.cohort_start_date, cs.cohort_end_date, cs.visit_occurrence_id
    ),
    earliest_death AS (
        SELECT 
            cs.subject_id,
            cs.cohort_start_date,
            cs.cohort_end_date,
            cs.visit_occurrence_id,
            MIN(d.death_date) AS death_date
        FROM cohort_src cs
        LEFT JOIN (
            SELECT person_id, death_date FROM death
            UNION
            SELECT person_id, visit_end_date AS death_date 
            FROM visit_occurrence 
            WHERE discharged_to_concept_id = 4216643
        ) d
          ON d.person_id = cs.subject_id
         AND d.death_date > cs.cohort_end_date
         AND d.death_date <= (cs.cohort_end_date + {int(followup_window_days)})
        GROUP BY cs.subject_id, cs.cohort_start_date, cs.cohort_end_date, cs.visit_occurrence_id
    ),
    combined AS (
        SELECT 
            r.subject_id,
            r.cohort_start_date,
            r.cohort_end_date,
            r.visit_occurrence_id,
            r.readmit_date,
            d.death_date,
            date_diff('day', r.cohort_end_date, r.readmit_date) AS days_to_readmit,
            date_diff('day', r.cohort_end_date, d.death_date) AS days_to_death
        FROM earliest_readmit r
        JOIN earliest_death d
          ON d.subject_id = r.subject_id
         AND d.cohort_start_date = r.cohort_start_date
         AND d.cohort_end_date = r.cohort_end_date
         AND COALESCE(d.visit_occurrence_id, -1) = COALESCE(r.visit_occurrence_id, -1)
    )
    SELECT 
        subject_id,
        visit_occurrence_id,
        cohort_start_date,
        cohort_end_date,
        CAST(CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN days_to_readmit
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN days_to_death
            ELSE {int(followup_window_days)}
        END AS INTEGER) AS time_days,
        CAST(CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN 1
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN 2
            ELSE 0
        END AS INTEGER) AS status,
        CASE 
            WHEN readmit_date IS NOT NULL AND (death_date IS NULL OR readmit_date <= death_date) THEN 'readmission'
            WHEN death_date IS NOT NULL AND (readmit_date IS NULL OR death_date < readmit_date) THEN 'death_competing'
            ELSE 'censored'
        END AS event_type,
        CAST(CASE 
            WHEN (readmit_date IS NOT NULL OR death_date IS NOT NULL) THEN 1 
            ELSE 0 
        END AS INTEGER) AS composite_event
    FROM combined
    ORDER BY subject_id, cohort_start_date;
    """
    df = con.execute(query).df()
    n_total = len(df)
    n_readmit = int((df["status"] == 1).sum()) if n_total > 0 else 0
    n_competing = int((df["status"] == 2).sum()) if n_total > 0 else 0
    n_censored = int((df["status"] == 0).sum()) if n_total > 0 else 0
    n_composite = n_readmit + n_competing

    summary = {
        "total_patients": n_total,
        "n_readmissions": n_readmit,
        "readmission_rate": float(n_readmit / n_total) if n_total > 0 else 0.0,
        "n_competing_deaths": n_competing,
        "competing_death_rate": float(n_competing / n_total) if n_total > 0 else 0.0,
        "n_censored": n_censored,
        "censored_rate": float(n_censored / n_total) if n_total > 0 else 0.0,
        "n_composite_events": n_composite,
        "composite_rate": float(n_composite / n_total) if n_total > 0 else 0.0,
        "followup_window_days": followup_window_days,
    }
    return df, summary


class ConsortAttrition:
    """Represents a CONSORT/STROBE participant attrition flow with multi-format exporters."""

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()

    @property
    def summary_table(self) -> pd.DataFrame:
        return self.df

    def to_dataframe(self) -> pd.DataFrame:
        return self.df

    def to_markdown(self) -> str:
        return self.df.to_markdown(index=False)

    def to_mermaid(self) -> str:
        """Render a Mermaid flowchart diagram (flowchart TD)."""
        lines = ["flowchart TD"]
        for _, row in self.df.iterrows():
            idx = int(row["step_number"])
            name = str(row["step_name"]).replace('"', "'")
            retained = int(row["subjects_retained"])
            pct = float(row.get("percent_retained", 100.0))
            dropped = int(row.get("subjects_dropped", 0))

            lines.append(f'    S{idx}["{idx}. {name}<br/>(N = {retained:,}, {pct:.1f}%)"]')
            if idx > 1:
                prev = idx - 1
                lines.append(f"    S{prev} --> S{idx}")
                if dropped > 0:
                    lines.append(f'    E{idx}["Excluded: {dropped:,}"]')
                    lines.append(f"    S{prev} -.-> E{idx}")
        return "\n".join(lines)

    def to_latex(self, output_path: str | Path | None = None) -> str:
        """Render a LaTeX tabular representation."""
        latex_str = self.df.to_latex(index=False)
        if output_path:
            Path(output_path).write_text(latex_str, encoding="utf-8")
        return latex_str


def generate_consort_attrition(
    con: duckdb.DuckDBPyConnection | None = None,
    cohort_data: Any = None,
    cohort_id: int = 1,
    steps: Sequence[tuple[str, str]] | None = None,
) -> ConsortAttrition:
    """Generate CONSORT attrition flowchart object with Mermaid and LaTeX exporters.

    Args:
        con: Active DuckDB connection (required if steps are provided).
        cohort_data: Either a DataFrame with an 'attrition' attribute, an existing
            attrition DataFrame, or a Cohort object.
        cohort_id: Cohort ID evaluated when evaluating raw steps.
        steps: Optional list of (step_name, query) tuples evaluated via compute_attrition.

    Returns:
        ConsortAttrition: Object with .to_mermaid(), .to_latex(), .to_markdown(), and .to_dataframe().
    """
    if steps is not None:
        if con is None:
            raise ValueError("Must provide active DuckDB connection `con` when `steps` is provided.")
        df = compute_attrition(con, cohort_id, steps)
        return ConsortAttrition(df)

    if isinstance(cohort_data, pd.DataFrame):
        if "attrition" in getattr(cohort_data, "attrs", {}):
            return ConsortAttrition(cohort_data.attrs["attrition"])
        if "step_name" in cohort_data.columns and "subjects_retained" in cohort_data.columns:
            return ConsortAttrition(cohort_data)

    if hasattr(cohort_data, "attrition"):
        att = getattr(cohort_data, "attrition")
        if isinstance(att, pd.DataFrame):
            return ConsortAttrition(att)

    raise ValueError(
        "Could not extract attrition data. Provide `steps` with `con`, or a cohort DataFrame with an 'attrition' attribute."
    )


def build_treatment_episodes(
    con: duckdb.DuckDBPyConnection,
    target_table: str = "drug_exposure",
    person_id_col: str = "person_id",
    concept_id_col: str = "drug_concept_id",
    start_date_col: str = "drug_exposure_start_date",
    end_date_col: str = "drug_exposure_end_date",
    max_gap_days: int = 30,
    output_table: str | None = None,
) -> pd.DataFrame:
    """Collapses discrete medication exposures into continuous treatment episodes (eras).

    Allows a configurable grace period (max_gap_days) between consecutive refills.

    Args:
        con: Active DuckDB connection.
        target_table: Source table (default 'drug_exposure').
        person_id_col: Person identifier column.
        concept_id_col: Concept identifier column (e.g. drug_concept_id).
        start_date_col: Exposure start date column.
        end_date_col: Exposure end date column.
        max_gap_days: Maximum permissible gap between exposure end and subsequent start (default 30).
        output_table: Optional name of temporary or permanent table to create with results.

    Returns:
        pd.DataFrame: Table with columns [person_id, concept_id, episode_number,
            episode_start_date, episode_end_date, duration_days, exposure_count].
    """
    sql = f"""
    WITH clean_exposures AS (
        SELECT 
            {person_id_col} AS person_id,
            {concept_id_col} AS concept_id,
            CAST({start_date_col} AS DATE) AS start_date,
            CAST(COALESCE({end_date_col}, {start_date_col}) AS DATE) AS end_date
        FROM {target_table}
        WHERE {concept_id_col} IS NOT NULL AND {concept_id_col} != 0
    ),
    ordered_exposures AS (
        SELECT 
            person_id,
            concept_id,
            start_date,
            end_date,
            MAX(end_date) OVER (
                PARTITION BY person_id, concept_id 
                ORDER BY start_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prev_max_end
        FROM clean_exposures
    ),
    episode_flags AS (
        SELECT 
            person_id,
            concept_id,
            start_date,
            end_date,
            CASE 
                WHEN prev_max_end IS NULL THEN 1
                WHEN date_diff('day', prev_max_end, start_date) > {int(max_gap_days)} THEN 1
                ELSE 0
            END AS is_new_episode
        FROM ordered_exposures
    ),
    episode_numbered AS (
        SELECT 
            person_id,
            concept_id,
            start_date,
            end_date,
            SUM(is_new_episode) OVER (
                PARTITION BY person_id, concept_id 
                ORDER BY start_date, end_date
            ) AS episode_id
        FROM episode_flags
    ),
    episodes AS (
        SELECT 
            person_id,
            concept_id,
            episode_id AS episode_number,
            MIN(start_date) AS episode_start_date,
            MAX(end_date) AS episode_end_date,
            CAST(date_diff('day', MIN(start_date), MAX(end_date)) + 1 AS INTEGER) AS duration_days,
            COUNT(*) AS exposure_count
        FROM episode_numbered
        GROUP BY person_id, concept_id, episode_id
    )
    SELECT * FROM episodes
    ORDER BY person_id, concept_id, episode_number;
    """

    if output_table:
        con.execute(f"CREATE OR REPLACE TABLE {output_table} AS {sql}")
        return con.execute(f"SELECT * FROM {output_table}").df()
    return con.execute(sql).df()



