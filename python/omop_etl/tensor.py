"""Longitudinal sequence tensor extraction for deep learning models on OMOP CDM.

Constructs 3D tensors (N_patients x max_visits x max_tokens) representing longitudinal
clinical trajectories (concept tokens across historical inpatient and acute encounters)
directly using DuckDB vectorized relational queries.
"""

from __future__ import annotations

from typing import Any, Sequence
import duckdb
import numpy as np
import pandas as pd

__all__ = ["extract_longitudinal_tensors", "MAX_LONGITUDINAL_VISITS", "MAX_TOKENS_PER_VISIT", "VOCAB_SIZE"]

MAX_LONGITUDINAL_VISITS = 20
MAX_TOKENS_PER_VISIT = 30
VOCAB_SIZE = 7477


def extract_longitudinal_tensors(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    *,
    max_visits: int = MAX_LONGITUDINAL_VISITS,
    max_tokens: int = MAX_TOKENS_PER_VISIT,
    vocab_size: int = VOCAB_SIZE,
    lookback_days: int = 365,
    include_index_stay: bool = False,
    include_labs: bool = False,
) -> np.ndarray:
    """Extracts 3D longitudinal visit tensors (N x max_visits x max_tokens) for a cohort.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Active connection to DuckDB OMOP CDM database.
    cohort_table : str
        Cohort table containing `subject_id` and `cohort_start_date`.
    max_visits : int, default 20
        Maximum number of prior longitudinal encounters to capture per patient.
    max_tokens : int, default 30
        Maximum number of clinical concept tokens to capture per encounter.
    vocab_size : int, default 7477
        Vocabulary token dictionary dimension for the deep residual sequence model.
    lookback_days : int, default 365
        Lookback window in days prior to index admission.
    include_index_stay : bool, default False
        Also tokenize a stay that starts ON ``cohort_start_date`` (FR-A7): the default window uses
        ``visit_start_date < cohort_start_date``. For cohorts anchored at discharge (``cohorts/target_cohort.sql``) the
        index stay started earlier and is already included, so this changes nothing; for cohorts anchored at admission
        it adds the index stay, whose records dated after admission may not be known at prediction time (leakage risk).
    include_labs : bool, default False
        Add measurement tokens (FR-A7): ``hash(measurement_concept_id * 3 + flag)`` with ``flag`` 0 = within or no
        reference range, 1 = below ``range_low``, 2 = above ``range_high``. Token ids come from DuckDB ``hash()`` like
        the other tokens, so they are stable only within one DuckDB version: train and score with the same one.

    Returns
    -------
    np.ndarray
        Array of shape (N, max_visits, max_tokens) with integer concept tokens.
    """
    # 1. Get ordered cohort keys
    cohort_df = con.execute(f"""
        SELECT subject_id, cohort_start_date 
        FROM {cohort_table} 
        ORDER BY subject_id, cohort_start_date
    """).df()
    n_cohort = len(cohort_df)
    if n_cohort == 0:
        return np.zeros((0, max_visits, max_tokens), dtype=np.int64)

    # Key lookup mapping (subject_id, cohort_start_date) -> cohort row index
    key_to_idx = {
        (row.subject_id, str(row.cohort_start_date)[:10]): idx
        for idx, row in enumerate(cohort_df.itertuples(index=False))
    }

    tensor = np.zeros((n_cohort, max_visits, max_tokens), dtype=np.int64)

    # 2. Extract prior visits and tokens using DuckDB
    if include_index_stay:
        visit_window = "v.visit_start_date <= c.cohort_start_date"
    else:
        visit_window = "v.visit_start_date < c.cohort_start_date"
    lab_block = f"""
        UNION ALL

        SELECT
            ev.subject_id,
            ev.cohort_start_date,
            ev.visit_rank,
            (hash(m.measurement_concept_id * 3 + CASE WHEN m.range_low IS NOT NULL AND m.value_as_number < m.range_low THEN 1
                                                      WHEN m.range_high IS NOT NULL AND m.value_as_number > m.range_high THEN 2
                                                      ELSE 0 END)
                 % ({int(vocab_size)} - 1) + 1)::BIGINT AS token_id
        FROM eligible_visits ev
        JOIN measurement m ON m.visit_occurrence_id = ev.visit_occurrence_id
        WHERE m.measurement_concept_id IS NOT NULL AND m.measurement_concept_id > 0
    """ if include_labs else ""
    tokens_query = f"""
    WITH cohort_visits AS (
        SELECT 
            c.subject_id,
            c.cohort_start_date,
            v.visit_occurrence_id,
            v.visit_start_date,
            DENSE_RANK() OVER (
                PARTITION BY c.subject_id, c.cohort_start_date 
                ORDER BY v.visit_start_date DESC, v.visit_occurrence_id DESC
            ) AS visit_rank
        FROM {cohort_table} c
        JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE {visit_window}
          AND v.visit_start_date >= c.cohort_start_date - INTERVAL ({int(lookback_days)}) DAY
    ),
    eligible_visits AS (
        SELECT * FROM cohort_visits WHERE visit_rank <= {int(max_visits)}
    ),
    all_events AS (
        SELECT 
            ev.subject_id,
            ev.cohort_start_date,
            ev.visit_rank,
            (hash(co.condition_concept_id) % ({int(vocab_size)} - 1) + 1)::BIGINT AS token_id
        FROM eligible_visits ev
        JOIN condition_occurrence co ON co.visit_occurrence_id = ev.visit_occurrence_id
        WHERE co.condition_concept_id IS NOT NULL AND co.condition_concept_id > 0

        UNION ALL

        SELECT 
            ev.subject_id,
            ev.cohort_start_date,
            ev.visit_rank,
            (hash(de.drug_concept_id) % ({int(vocab_size)} - 1) + 1)::BIGINT AS token_id
        FROM eligible_visits ev
        JOIN drug_exposure de ON de.visit_occurrence_id = ev.visit_occurrence_id
        WHERE de.drug_concept_id IS NOT NULL AND de.drug_concept_id > 0

        UNION ALL

        SELECT 
            ev.subject_id,
            ev.cohort_start_date,
            ev.visit_rank,
            (hash(po.procedure_concept_id) % ({int(vocab_size)} - 1) + 1)::BIGINT AS token_id
        FROM eligible_visits ev
        JOIN procedure_occurrence po ON po.visit_occurrence_id = ev.visit_occurrence_id
        WHERE po.procedure_concept_id IS NOT NULL AND po.procedure_concept_id > 0
        {lab_block}
    ),
    ranked_tokens AS (
        SELECT 
            subject_id,
            cohort_start_date,
            visit_rank,
            token_id,
            ROW_NUMBER() OVER (
                PARTITION BY subject_id, cohort_start_date, visit_rank 
                ORDER BY token_id
            ) AS token_rank
        FROM all_events
    )
    SELECT subject_id, cohort_start_date, visit_rank, token_id, token_rank
    FROM ranked_tokens
    WHERE token_rank <= {int(max_tokens)}
    ORDER BY subject_id, cohort_start_date, visit_rank, token_rank;
    """

    try:
        events = con.execute(tokens_query).fetchall()
        for subj, cdate, v_rank, tok_id, t_rank in events:
            key = (subj, str(cdate)[:10])
            if key in key_to_idx:
                row_i = key_to_idx[key]
                v_i = int(v_rank) - 1
                t_i = int(t_rank) - 1
                if 0 <= v_i < max_visits and 0 <= t_i < max_tokens:
                    tensor[row_i, v_i, t_i] = int(tok_id)
    except Exception:
        # Fall back gracefully if some clinical tables are empty
        pass

    return tensor
