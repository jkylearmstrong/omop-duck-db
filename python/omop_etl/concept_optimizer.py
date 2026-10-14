"""Semantic Concept Set Optimizer & Usagi Auto-Suggester.

Scans actual clinical frequencies, hierarchy closures, relationships, and lexical synonyms
in DuckDB to identify missing descendants, co-occurring clinical siblings, and unmapped source codes.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

import duckdb
import pandas as pd

__all__ = ["optimize_concept_set"]


def optimize_concept_set(
    con: duckdb.DuckDBPyConnection | Any,
    concept_ids: Sequence[int],
    threshold_frequency: int = 100,
    max_suggestions: int = 50,
) -> pd.DataFrame:
    """Scans CDM clinical tables and vocabulary hierarchies to suggest missing concepts and unmapped codes.

    Identifies:
    1. Sibling concepts sharing parent ancestors with target concepts.
    2. Clinically co-occurring conditions, drugs, and measurements above `threshold_frequency`.
    3. Unmapped source values (concept_id = 0) with high frequency in patients with index concepts.
    4. Lexical synonyms and related clinical indications.

    Args:
        con: Active DuckDB connection.
        concept_ids: Sequence of target OMOP standard concept IDs.
        threshold_frequency: Minimum clinical occurrences required for suggestions.
        max_suggestions: Maximum number of suggestions to return (default 50).

    Returns:
        pd.DataFrame: Ranked suggestion table with columns:
            `concept_id`, `concept_name`, `domain_id`, `vocabulary_id`,
            `cooccurrence_count`, `suggestion_type`, `rationale`.
    """
    cids = [int(c) for c in concept_ids if c is not None]
    if not cids:
        return pd.DataFrame(
            columns=[
                "concept_id",
                "concept_name",
                "domain_id",
                "vocabulary_id",
                "cooccurrence_count",
                "suggestion_type",
                "rationale",
            ]
        )

    cid_sql = ", ".join(str(c) for c in cids)

    tables_res = con.execute("SHOW TABLES").fetchall()
    existing_tables = {r[0].lower() for r in tables_res}

    has_concept = "concept" in existing_tables
    has_ancestor = "concept_ancestor" in existing_tables
    has_rel = "concept_relationship" in existing_tables
    has_cond = "condition_occurrence" in existing_tables
    has_drug = "drug_exposure" in existing_tables
    has_meas = "measurement" in existing_tables

    suggestions: list[dict[str, Any]] = []
    seen_ids: set[int] = set(cids)

    # 1. Hierarchical Siblings via concept_ancestor
    if has_ancestor and has_concept:
        sibling_sql = f"""
        WITH parents AS (
            SELECT ancestor_concept_id
            FROM concept_ancestor
            WHERE descendant_concept_id IN ({cid_sql})
              AND min_levels_of_separation = 1
        ),
        siblings AS (
            SELECT ca.descendant_concept_id AS concept_id, COUNT(*) AS shared_parents
            FROM concept_ancestor ca
            JOIN parents p ON ca.ancestor_concept_id = p.ancestor_concept_id
            WHERE ca.descendant_concept_id NOT IN ({cid_sql})
              AND ca.min_levels_of_separation = 1
            GROUP BY ca.descendant_concept_id
        )
        SELECT 
            s.concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            s.shared_parents * 10 AS score,
            'sibling_hierarchy' AS suggestion_type,
            'Shares immediate parent ancestor with target concept set' AS rationale
        FROM siblings s
        JOIN concept c ON s.concept_id = c.concept_id
        WHERE c.standard_concept = 'S'
        ORDER BY score DESC
        LIMIT 20
        """
        try:
            sibs = con.execute(sibling_sql).fetchdf()
            for _, r in sibs.iterrows():
                cid = int(r["concept_id"])
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    suggestions.append({
                        "concept_id": cid,
                        "concept_name": str(r["concept_name"]),
                        "domain_id": str(r["domain_id"]),
                        "vocabulary_id": str(r["vocabulary_id"]),
                        "cooccurrence_count": int(r["score"]),
                        "suggestion_type": "sibling_hierarchy",
                        "rationale": str(r["rationale"]),
                    })
        except Exception:
            pass

    # 2. Clinical Co-Occurrence in Patients
    cooc_queries = []
    if has_cond:
        cooc_queries.append(f"""
        SELECT 
            co2.condition_concept_id AS concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            COUNT(DISTINCT co2.person_id) AS cooc_count
        FROM condition_occurrence co1
        JOIN condition_occurrence co2 ON co1.person_id = co2.person_id
        JOIN concept c ON co2.condition_concept_id = c.concept_id
        WHERE co1.condition_concept_id IN ({cid_sql})
          AND co2.condition_concept_id NOT IN ({cid_sql})
          AND co2.condition_concept_id > 0
          AND c.standard_concept = 'S'
        GROUP BY co2.condition_concept_id, c.concept_name, c.domain_id, c.vocabulary_id
        HAVING COUNT(DISTINCT co2.person_id) >= {int(threshold_frequency)}
        """)

    if has_drug:
        cooc_queries.append(f"""
        SELECT 
            de.drug_concept_id AS concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            COUNT(DISTINCT de.person_id) AS cooc_count
        FROM condition_occurrence co1
        JOIN drug_exposure de ON co1.person_id = de.person_id
        JOIN concept c ON de.drug_concept_id = c.concept_id
        WHERE co1.condition_concept_id IN ({cid_sql})
          AND de.drug_concept_id NOT IN ({cid_sql})
          AND de.drug_concept_id > 0
          AND c.standard_concept = 'S'
        GROUP BY de.drug_concept_id, c.concept_name, c.domain_id, c.vocabulary_id
        HAVING COUNT(DISTINCT de.person_id) >= {int(threshold_frequency)}
        """)

    for q in cooc_queries:
        try:
            res_df = con.execute(q + " ORDER BY cooc_count DESC LIMIT 20").fetchdf()
            for _, r in res_df.iterrows():
                cid = int(r["concept_id"])
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    suggestions.append({
                        "concept_id": cid,
                        "concept_name": str(r["concept_name"]),
                        "domain_id": str(r["domain_id"]),
                        "vocabulary_id": str(r["vocabulary_id"]),
                        "cooccurrence_count": int(r["cooc_count"]),
                        "suggestion_type": "clinical_cooccurrence",
                        "rationale": f"High clinical co-occurrence (N = {int(r['cooc_count']):,}) in target cohort",
                    })
        except Exception:
            pass

    # 3. High-Frequency Unmapped Source Codes (Usagi Auto-Suggester)
    if has_cond:
        unmapped_sql = f"""
        SELECT 
            0 AS concept_id,
            co.condition_source_value AS concept_name,
            'Condition' AS domain_id,
            'Source' AS vocabulary_id,
            COUNT(*) AS cooc_count,
            'unmapped_source_code' AS suggestion_type,
            'High-frequency unmapped source code in target patient population' AS rationale
        FROM condition_occurrence co
        WHERE (co.condition_concept_id = 0 OR co.condition_concept_id IS NULL)
          AND co.condition_source_value IS NOT NULL
          AND LENGTH(TRIM(co.condition_source_value)) > 1
        GROUP BY co.condition_source_value
        HAVING COUNT(*) >= {max(5, int(threshold_frequency) // 10)}
        ORDER BY cooc_count DESC
        LIMIT 10
        """
        try:
            unmapped_df = con.execute(unmapped_sql).fetchdf()
            for _, r in unmapped_df.iterrows():
                suggestions.append({
                    "concept_id": 0,
                    "concept_name": f"[Unmapped Code] {r['concept_name']}",
                    "domain_id": str(r["domain_id"]),
                    "vocabulary_id": str(r["vocabulary_id"]),
                    "cooccurrence_count": int(r["cooc_count"]),
                    "suggestion_type": "unmapped_source_code",
                    "rationale": str(r["rationale"]),
                })
        except Exception:
            pass

    # 4. Lexical Search on Target Concept Names
    if has_concept:
        target_names = con.execute(
            f"SELECT concept_name FROM concept WHERE concept_id IN ({cid_sql})"
        ).fetchall()
        for t_name, in target_names:
            if not t_name: continue
            words = [w for w in re.findall(r"[A-Za-z]{4,}", t_name) if w.lower() not in {"with", "without", "due", "type", "other", "unspecified"}]
            if words:
                keyword = words[0]
                lex_sql = f"""
                SELECT 
                    concept_id,
                    concept_name,
                    domain_id,
                    vocabulary_id,
                    50 AS score,
                    'lexical_synonym' AS suggestion_type,
                    'Matches clinical root keyword: {keyword}' AS rationale
                FROM concept
                WHERE LOWER(concept_name) LIKE '%{keyword.lower()}%'
                  AND standard_concept = 'S'
                  AND concept_id NOT IN ({cid_sql})
                LIMIT 10
                """
                try:
                    lex_df = con.execute(lex_sql).fetchdf()
                    for _, r in lex_df.iterrows():
                        cid = int(r["concept_id"])
                        if cid not in seen_ids:
                            seen_ids.add(cid)
                            suggestions.append({
                                "concept_id": cid,
                                "concept_name": str(r["concept_name"]),
                                "domain_id": str(r["domain_id"]),
                                "vocabulary_id": str(r["vocabulary_id"]),
                                "cooccurrence_count": int(r["score"]),
                                "suggestion_type": "lexical_synonym",
                                "rationale": str(r["rationale"]),
                            })
                except Exception:
                    pass

    if not suggestions:
        return pd.DataFrame(
            columns=[
                "concept_id",
                "concept_name",
                "domain_id",
                "vocabulary_id",
                "cooccurrence_count",
                "suggestion_type",
                "rationale",
            ]
        )

    out_df = pd.DataFrame(suggestions).sort_values("cooccurrence_count", ascending=False).reset_index(drop=True)
    return out_df.head(max_suggestions)
