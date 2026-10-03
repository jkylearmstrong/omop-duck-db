"""Cohort generation, concept ancestor hierarchy navigation, and phenotyping helpers.

Provides helpers for querying transitive closures in concept_ancestor, performing
concept set algebra, managing the OMOP cohort and cohort_definition tables,
and tracking patient attrition across CONSORT flowchart phenotyping criteria.
"""

from __future__ import annotations

import datetime
from typing import Any, Sequence
import duckdb
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
    ensure_cohort_tables(con)

    if isinstance(target_visit_concept_ids, int):
        tgt_ids = [target_visit_concept_ids]
    else:
        tgt_ids = list(target_visit_concept_ids)
    tgt_in = ", ".join(str(int(x)) for x in tgt_ids)

    if isinstance(outcome_visit_concept_ids, int):
        out_ids = [outcome_visit_concept_ids]
    else:
        out_ids = list(outcome_visit_concept_ids)
    out_in = ", ".join(str(int(x)) for x in out_ids)

    rule = index_selection_rule.lower().strip()
    if rule == "random":
        order_by = f"hash(f.visit_occurrence_id, {int(random_state)}), f.visit_occurrence_id"
    elif rule == "first":
        order_by = "f.visit_start_date ASC, f.visit_occurrence_id ASC"
    elif rule == "last":
        order_by = "f.visit_start_date DESC, f.visit_occurrence_id DESC"
    else:
        raise ValueError(f"Unsupported index_selection_rule '{index_selection_rule}'. Use 'random', 'first', or 'last'.")

    washin_clause = ""
    if washin_days and washin_days > 0:
        washin_clause = f"""
            AND (
                EXISTS (
                    SELECT 1 FROM observation_period op 
                    WHERE op.person_id = v.person_id 
                      AND op.observation_period_start_date <= (v.visit_start_date - {int(washin_days)})
                      AND op.observation_period_end_date >= v.visit_start_date
                )
                OR EXISTS (
                    SELECT 1 FROM visit_occurrence pv 
                    WHERE pv.person_id = v.person_id 
                      AND pv.visit_occurrence_id != v.visit_occurrence_id
                      AND pv.visit_start_date <= (v.visit_start_date - {int(washin_days)})
                )
            )
        """

    followup_filter = "WHERE (outcome_flag = 1 OR has_subsequent_event = 1)" if require_verified_followup else ""

    query = f"""
    WITH eligible_stays AS (
        SELECT 
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) AS age_at_admission
        FROM visit_occurrence v
        JOIN person p ON v.person_id = p.person_id
        WHERE v.visit_concept_id IN ({tgt_in})
          AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
          AND date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) >= 18
          AND COALESCE(v.discharged_to_concept_id, 0) != 4216643
          AND NOT EXISTS (
              SELECT 1 FROM death d 
              WHERE d.person_id = v.person_id 
                AND d.death_date <= v.visit_end_date
          )
          {washin_clause}
    ),
    outcomes_and_followup AS (
        SELECT 
            e.*,
            -- 30-day readmission outcome
            CASE WHEN EXISTS (
                SELECT 1 FROM visit_occurrence ro
                WHERE ro.person_id = e.subject_id
                  AND ro.visit_occurrence_id != e.visit_occurrence_id
                  AND ro.visit_concept_id IN ({out_in})
                  AND ro.visit_start_date > (e.visit_end_date + {int(grace_days)})
                  AND ro.visit_start_date <= (e.visit_end_date + {int(followup_window_days)})
            ) THEN 1 ELSE 0 END AS outcome_flag,
            -- Verified follow-up (subsequent encounter >= followup_window_days)
            CASE WHEN (
                EXISTS (
                    SELECT 1 FROM visit_occurrence sv
                    WHERE sv.person_id = e.subject_id
                      AND sv.visit_occurrence_id != e.visit_occurrence_id
                      AND sv.visit_start_date >= (e.visit_end_date + {int(followup_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM measurement sm
                    WHERE sm.person_id = e.subject_id
                      AND sm.measurement_date >= (e.visit_end_date + {int(followup_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM condition_occurrence sc
                    WHERE sc.person_id = e.subject_id
                      AND sc.condition_start_date >= (e.visit_end_date + {int(followup_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM drug_exposure sd
                    WHERE sd.person_id = e.subject_id
                      AND sd.drug_exposure_start_date >= (e.visit_end_date + {int(followup_window_days)})
                )
            ) THEN 1 ELSE 0 END AS has_subsequent_event
        FROM eligible_stays e
    ),
    filtered_stays AS (
        SELECT *
        FROM outcomes_and_followup
        {followup_filter}
    ),
    ranked_stays AS (
        SELECT 
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id 
                ORDER BY {order_by}
            ) AS stay_rank
        FROM filtered_stays f
    )
    SELECT 
        {int(cohort_id)} AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        outcome_flag,
        los_days,
        age_at_admission,
        discharged_to_concept_id
    FROM ranked_stays
    WHERE stay_rank = 1
    ORDER BY subject_id, cohort_start_date;
    """

    con.execute("DROP TABLE IF EXISTS _temp_readmission_cohort;")
    con.execute(f"CREATE TEMPORARY TABLE _temp_readmission_cohort AS {query};")

    name_esc = cohort_name.replace("'", "''")
    desc_esc = (cohort_description or "Inpatient 30-day readmission index cohort").replace("'", "''")

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
    con.execute(f"""
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT cohort_definition_id, subject_id, cohort_start_date, cohort_end_date
        FROM _temp_readmission_cohort;
    """)

    if outcome_cohort_id is not None:
        out_name_esc = f"{cohort_name} - Readmission Outcome".replace("'", "''")
        con.execute(f"DELETE FROM cohort_definition WHERE cohort_definition_id = {int(outcome_cohort_id)};")
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
                {int(outcome_cohort_id)},
                '{out_name_esc}',
                'Patients experiencing 30-day readmission outcome',
                0,
                NULL,
                0,
                DATE '{datetime.date.today().isoformat()}'
            );
        """)
        con.execute(f"DELETE FROM cohort WHERE cohort_definition_id = {int(outcome_cohort_id)};")
        con.execute(f"""
            INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
            SELECT DISTINCT
                {int(outcome_cohort_id)} AS cohort_definition_id,
                tc.subject_id,
                ro.visit_start_date AS cohort_start_date,
                ro.visit_end_date AS cohort_end_date
            FROM _temp_readmission_cohort tc
            JOIN visit_occurrence ro ON tc.subject_id = ro.person_id
            WHERE tc.outcome_flag = 1
              AND ro.visit_occurrence_id != tc.visit_occurrence_id
              AND ro.visit_concept_id IN ({out_in})
              AND ro.visit_start_date > (tc.cohort_end_date + {int(grace_days)})
              AND ro.visit_start_date <= (tc.cohort_end_date + {int(followup_window_days)});
        """)

    if table_name:
        con.execute(f"DROP TABLE IF EXISTS {table_name};")
        con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM _temp_readmission_cohort;")

    res_df = con.execute("SELECT * FROM _temp_readmission_cohort ORDER BY subject_id;").df()
    con.execute("DROP TABLE IF EXISTS _temp_readmission_cohort;")
    return res_df


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
    ensure_cohort_tables(con)

    valid_types = ("in_hospital", "post_discharge", "fixed_window", "composite_readmit_or_death")
    m_type = mortality_type.lower().strip()
    if m_type not in valid_types:
        raise ValueError(f"Unsupported mortality_type '{mortality_type}'. Must be one of {valid_types}.")

    if target_visit_concept_ids is None:
        tgt_clause = "1=1"
    elif isinstance(target_visit_concept_ids, int):
        tgt_clause = f"v.visit_concept_id = {int(target_visit_concept_ids)}"
    else:
        tgt_in = ", ".join(str(int(x)) for x in target_visit_concept_ids)
        tgt_clause = f"v.visit_concept_id IN ({tgt_in})"

    if isinstance(outcome_visit_concept_ids, int):
        out_ids = [outcome_visit_concept_ids]
    else:
        out_ids = list(outcome_visit_concept_ids)
    out_in = ", ".join(str(int(x)) for x in out_ids)

    rule = index_selection_rule.lower().strip()
    if rule == "random":
        order_by = f"hash(f.visit_occurrence_id, {int(random_state)}), f.visit_occurrence_id"
    elif rule == "first":
        order_by = "f.visit_start_date ASC, f.visit_occurrence_id ASC"
    elif rule == "last":
        order_by = "f.visit_start_date DESC, f.visit_occurrence_id DESC"
    else:
        raise ValueError(f"Unsupported index_selection_rule '{index_selection_rule}'. Use 'random', 'first', or 'last'.")

    washin_clause = ""
    if washin_days and washin_days > 0:
        washin_clause = f"""
            AND (
                EXISTS (
                    SELECT 1 FROM {schema}.observation_period op 
                    WHERE op.person_id = v.person_id 
                      AND op.observation_period_start_date <= (v.visit_start_date - {int(washin_days)})
                      AND op.observation_period_end_date >= v.visit_start_date
                )
                OR EXISTS (
                    SELECT 1 FROM {schema}.visit_occurrence pv 
                    WHERE pv.person_id = v.person_id 
                      AND pv.visit_occurrence_id != v.visit_occurrence_id
                      AND pv.visit_start_date <= (v.visit_start_date - {int(washin_days)})
                )
            )
        """

    # Stay filters and anchor configuration per mortality_type
    if m_type == "in_hospital":
        stay_criteria = "AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 0 AND (pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)"
        outcome_expr = """
            CASE WHEN COALESCE(e.discharged_to_concept_id, 0) IN (4216643, 4155309) 
                   OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date)
                 THEN 1 ELSE 0 END AS outcome_flag,
            CASE WHEN COALESCE(e.discharged_to_concept_id, 0) IN (4216643, 4155309) 
                   OR (e.death_date IS NOT NULL AND e.death_date >= e.visit_start_date AND e.death_date <= e.visit_end_date)
                 THEN CAST(COALESCE(e.death_date, e.visit_end_date) AS DATE) ELSE NULL END AS outcome_date
        """
        anchor_expr = "e.visit_end_date"
    elif m_type == "post_discharge":
        stay_criteria = """
            AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
            AND COALESCE(v.discharged_to_concept_id, 0) NOT IN (4216643, 4155309)
            AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)
        """
        outcome_expr = f"""
            CASE WHEN e.death_date IS NOT NULL 
                  AND e.death_date > (e.visit_end_date + {int(gap_days)}) 
                  AND e.death_date <= (e.visit_end_date + {int(mortality_window_days)})
                 THEN 1 ELSE 0 END AS outcome_flag,
            CASE WHEN e.death_date IS NOT NULL 
                  AND e.death_date > (e.visit_end_date + {int(gap_days)}) 
                  AND e.death_date <= (e.visit_end_date + {int(mortality_window_days)})
                 THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date
        """
        anchor_expr = "e.visit_end_date"
    elif m_type == "fixed_window":
        stay_criteria = "AND (pd.death_date IS NULL OR pd.death_date >= v.visit_start_date)"
        outcome_expr = f"""
            CASE WHEN e.death_date IS NOT NULL 
                  AND e.death_date > (e.visit_start_date + {int(gap_days)}) 
                  AND e.death_date <= (e.visit_start_date + {int(mortality_window_days)})
                 THEN 1 ELSE 0 END AS outcome_flag,
            CASE WHEN e.death_date IS NOT NULL 
                  AND e.death_date > (e.visit_start_date + {int(gap_days)}) 
                  AND e.death_date <= (e.visit_start_date + {int(mortality_window_days)})
                 THEN CAST(e.death_date AS DATE) ELSE NULL END AS outcome_date
        """
        anchor_expr = "e.visit_start_date"
    elif m_type == "composite_readmit_or_death":
        stay_criteria = """
            AND date_diff('day', v.visit_start_date, v.visit_end_date) >= 1
            AND COALESCE(v.discharged_to_concept_id, 0) NOT IN (4216643, 4155309)
            AND (pd.death_date IS NULL OR pd.death_date > v.visit_end_date)
        """
        outcome_expr = f"""
            CASE WHEN (
                (e.death_date IS NOT NULL AND e.death_date > (e.visit_end_date + {int(gap_days)}) AND e.death_date <= (e.visit_end_date + {int(mortality_window_days)}))
                OR EXISTS (
                    SELECT 1 FROM {schema}.visit_occurrence ro
                    WHERE ro.person_id = e.subject_id
                      AND ro.visit_occurrence_id != e.visit_occurrence_id
                      AND ro.visit_concept_id IN ({out_in})
                      AND ro.visit_start_date > (e.visit_end_date + {int(gap_days)})
                      AND ro.visit_start_date <= (e.visit_end_date + {int(mortality_window_days)})
                )
            ) THEN 1 ELSE 0 END AS outcome_flag,
            CAST(LEAST(
                CASE WHEN e.death_date IS NOT NULL AND e.death_date > (e.visit_end_date + {int(gap_days)}) AND e.death_date <= (e.visit_end_date + {int(mortality_window_days)}) THEN e.death_date ELSE NULL END,
                (SELECT MIN(ro.visit_start_date) FROM {schema}.visit_occurrence ro WHERE ro.person_id = e.subject_id AND ro.visit_occurrence_id != e.visit_occurrence_id AND ro.visit_concept_id IN ({out_in}) AND ro.visit_start_date > (e.visit_end_date + {int(gap_days)}) AND ro.visit_start_date <= (e.visit_end_date + {int(mortality_window_days)}))
            ) AS DATE) AS outcome_date
        """
        anchor_expr = "e.visit_end_date"

    # Follow-up verification for censoring
    if require_verified_followup and m_type != "in_hospital":
        followup_check = f"""
            CASE WHEN (
                EXISTS (
                    SELECT 1 FROM {schema}.observation_period op
                    WHERE op.person_id = e.subject_id
                      AND op.observation_period_end_date >= ({anchor_expr} + {int(mortality_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM {schema}.visit_occurrence sv
                    WHERE sv.person_id = e.subject_id
                      AND sv.visit_occurrence_id != e.visit_occurrence_id
                      AND sv.visit_start_date >= ({anchor_expr} + {int(mortality_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM {schema}.measurement sm
                    WHERE sm.person_id = e.subject_id
                      AND sm.measurement_date >= ({anchor_expr} + {int(mortality_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM {schema}.condition_occurrence sc
                    WHERE sc.person_id = e.subject_id
                      AND sc.condition_start_date >= ({anchor_expr} + {int(mortality_window_days)})
                )
                OR EXISTS (
                    SELECT 1 FROM {schema}.drug_exposure sd
                    WHERE sd.person_id = e.subject_id
                      AND sd.drug_exposure_start_date >= ({anchor_expr} + {int(mortality_window_days)})
                )
                OR (e.death_date IS NOT NULL AND e.death_date >= ({anchor_expr} + {int(mortality_window_days)}))
            ) THEN 1 ELSE 0 END AS has_subsequent_event
        """
        filter_clause = "WHERE (outcome_flag = 1 OR has_subsequent_event = 1)"
    else:
        followup_check = "1 AS has_subsequent_event"
        filter_clause = ""

    query = f"""
    CREATE OR REPLACE TEMP TABLE _temp_eol_cohort AS
    WITH all_deaths AS (
        SELECT person_id, CAST(death_date AS DATE) AS death_date
        FROM {schema}.death
        WHERE death_date IS NOT NULL
        UNION ALL
        SELECT person_id, CAST(visit_end_date AS DATE) AS death_date
        FROM {schema}.visit_occurrence
        WHERE discharged_to_concept_id IN (4216643, 4155309)
          AND visit_end_date IS NOT NULL
    ),
    patient_death AS (
        SELECT person_id, MIN(death_date) AS death_date
        FROM all_deaths
        GROUP BY person_id
    ),
    eligible_stays AS (
        SELECT 
            v.visit_occurrence_id,
            v.person_id AS subject_id,
            v.visit_start_date,
            v.visit_end_date,
            v.discharged_to_concept_id,
            pd.death_date,
            date_diff('day', v.visit_start_date, v.visit_end_date) AS los_days,
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) AS age_at_index
        FROM {schema}.visit_occurrence v
        JOIN {schema}.person p ON v.person_id = p.person_id
        LEFT JOIN patient_death pd ON v.person_id = pd.person_id
        WHERE {tgt_clause}
          AND date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) >= {int(min_age)}
          {stay_criteria}
          {washin_clause}
    ),
    outcomes_and_followup AS (
        SELECT 
            e.*,
            '{m_type}' AS mortality_type,
            {outcome_expr},
            {followup_check}
        FROM eligible_stays e
    ),
    filtered_stays AS (
        SELECT *
        FROM outcomes_and_followup
        {filter_clause}
    ),
    ranked_stays AS (
        SELECT 
            f.*,
            ROW_NUMBER() OVER (
                PARTITION BY f.subject_id 
                ORDER BY {order_by}
            ) AS rnk
        FROM filtered_stays f
    )
    SELECT 
        {int(cohort_id)} AS cohort_definition_id,
        subject_id,
        visit_start_date AS cohort_start_date,
        visit_end_date AS cohort_end_date,
        visit_occurrence_id,
        outcome_flag,
        outcome_date,
        mortality_type,
        age_at_index,
        los_days,
        discharged_to_concept_id
    FROM ranked_stays
    WHERE rnk = 1;
    """
    con.execute(query)

    desc_esc = (cohort_description or f"{cohort_name} ({m_type})").replace("'", "''")
    name_esc = cohort_name.replace("'", "''")

    if overwrite:
        con.execute(f"DELETE FROM {schema}.cohort WHERE cohort_definition_id = {int(cohort_id)};")
        con.execute(f"DELETE FROM {schema}.cohort_definition WHERE cohort_definition_id = {int(cohort_id)};")
        if outcome_cohort_id is not None:
            con.execute(f"DELETE FROM {schema}.cohort WHERE cohort_definition_id = {int(outcome_cohort_id)};")
            con.execute(f"DELETE FROM {schema}.cohort_definition WHERE cohort_definition_id = {int(outcome_cohort_id)};")

    # Insert target cohort definition
    con.execute(f"""
        INSERT INTO {schema}.cohort_definition (
            cohort_definition_id, cohort_definition_name, cohort_definition_description,
            definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
        ) VALUES (
            {int(cohort_id)}, '{name_esc}', '{desc_esc}', 0, NULL, 0, DATE '{datetime.date.today().isoformat()}'
        );
    """)

    # Populate target cohort table
    con.execute(f"""
        INSERT INTO {schema}.cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT DISTINCT
            cohort_definition_id,
            subject_id,
            cohort_start_date,
            cohort_end_date
        FROM _temp_eol_cohort;
    """)

    # Populate outcome cohort table if requested
    if outcome_cohort_id is not None:
        out_name_esc = f"{name_esc} Outcome".replace("'", "''")
        con.execute(f"""
            INSERT INTO {schema}.cohort_definition (
                cohort_definition_id, cohort_definition_name, cohort_definition_description,
                definition_type_concept_id, cohort_definition_syntax, subject_concept_id, cohort_initiation_date
            ) VALUES (
                {int(outcome_cohort_id)}, '{out_name_esc}', 'Outcome events for {name_esc}', 0, NULL, 0, DATE '{datetime.date.today().isoformat()}'
            );
        """)
        con.execute(f"""
            INSERT INTO {schema}.cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
            SELECT DISTINCT
                {int(outcome_cohort_id)} AS cohort_definition_id,
                subject_id,
                COALESCE(outcome_date, cohort_end_date) AS cohort_start_date,
                COALESCE(outcome_date, cohort_end_date) AS cohort_end_date
            FROM _temp_eol_cohort
            WHERE outcome_flag = 1;
        """)

    if table_name:
        con.execute(f"DROP TABLE IF EXISTS {table_name};")
        con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM _temp_eol_cohort;")

    res_df = con.execute("SELECT * FROM _temp_eol_cohort ORDER BY subject_id;").df()
    con.execute("DROP TABLE IF EXISTS _temp_eol_cohort;")
    return res_df


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


