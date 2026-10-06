"""Machine Learning Feature Matrix Extractor (Bridge to omop-learn and OHDSI PLP).

Extracts vectorized patient feature matrices directly from DuckDB OMOP CDM tables,
anchored to index date T0 from standard cohort definitions. Supports lookback
windows (30d/180d/365d), demographics, and exports to PyArrow, Polars, or Pandas.
"""

from __future__ import annotations

from typing import Any, Sequence
import duckdb
import pandas as pd


def extract_patient_features(
    con: duckdb.DuckDBPyConnection,
    cohort_id: int,
    outcome_cohort_id: int | None = None,
    lookback_days: Sequence[int] = (30, 180, 365),
    include_demographics: bool = True,
    include_conditions: bool = True,
    include_drugs: bool = True,
    include_procedures: bool = True,
    include_measurements: bool = True,
    format: str = "df",
) -> Any:
    """Extracts ML-ready patient features anchored to cohort_start_date (T0).
    
    Args:
        con: Active DuckDB connection.
        cohort_id: Cohort definition ID in the cohort table defining the target population.
        outcome_cohort_id: Optional cohort definition ID defining binary outcome Y.
        lookback_days: Sequence of days for prior observation windows.
        include_demographics: Include age, gender, race, ethnicity, index year.
        include_conditions: Include condition occurrence counts across lookback windows.
        include_drugs: Include drug exposure counts across lookback windows.
        include_procedures: Include procedure occurrence counts across lookback windows.
        include_measurements: Include measurement counts across lookback windows.
        format: Output data format: 'df' (Pandas), 'arrow' (PyArrow), 'polars', or 'sparse'.
        
    Returns:
        pd.DataFrame, pyarrow.Table, polars.DataFrame, or sparse DataFrame.
    """
    fmt = format.lower().strip()
    windows = sorted([int(w) for w in lookback_days])

    outcome_join = ""
    outcome_select = ""
    if outcome_cohort_id is not None:
        outcome_select = "COALESCE(oc.outcome_flag, 0) AS y,"
        outcome_join = f"""
            LEFT JOIN (
                SELECT DISTINCT subject_id, 1 AS outcome_flag
                FROM cohort
                WHERE cohort_definition_id = {int(outcome_cohort_id)}
            ) oc ON c.subject_id = oc.subject_id
        """

    demo_select = ""
    demo_join = ""
    if include_demographics:
        demo_select = """
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age_at_index,
            p.gender_concept_id,
            p.race_concept_id,
            p.ethnicity_concept_id,
            YEAR(c.cohort_start_date) AS index_year,
        """
        demo_join = "LEFT JOIN person p ON c.subject_id = p.person_id"

    # Aggregated lookback windows
    lookback_selects = []
    lookback_joins = []

    if include_conditions:
        cond_cols = ", ".join(
            f"COUNT(DISTINCT CASE WHEN co.condition_start_date BETWEEN c.cohort_start_date - {w} AND c.cohort_start_date - 1 THEN co.condition_occurrence_id END) AS condition_count_{w}d"
            for w in windows
        )
        lookback_selects.append(cond_cols)
        lookback_joins.append("LEFT JOIN condition_occurrence co ON c.subject_id = co.person_id")

    if include_drugs:
        drug_cols = ", ".join(
            f"COUNT(DISTINCT CASE WHEN de.drug_exposure_start_date BETWEEN c.cohort_start_date - {w} AND c.cohort_start_date - 1 THEN de.drug_exposure_id END) AS drug_count_{w}d"
            for w in windows
        )
        lookback_selects.append(drug_cols)
        lookback_joins.append("LEFT JOIN drug_exposure de ON c.subject_id = de.person_id")

    if include_procedures:
        proc_cols = ", ".join(
            f"COUNT(DISTINCT CASE WHEN po.procedure_date BETWEEN c.cohort_start_date - {w} AND c.cohort_start_date - 1 THEN po.procedure_occurrence_id END) AS procedure_count_{w}d"
            for w in windows
        )
        lookback_selects.append(proc_cols)
        lookback_joins.append("LEFT JOIN procedure_occurrence po ON c.subject_id = po.person_id")

    if include_measurements:
        meas_cols = ", ".join(
            f"COUNT(DISTINCT CASE WHEN m.measurement_date BETWEEN c.cohort_start_date - {w} AND c.cohort_start_date - 1 THEN m.measurement_id END) AS measurement_count_{w}d"
            for w in windows
        )
        lookback_selects.append(meas_cols)
        lookback_joins.append("LEFT JOIN measurement m ON c.subject_id = m.person_id")

    select_items = [
        "c.subject_id",
        "c.cohort_start_date",
        "c.cohort_end_date",
    ]
    if outcome_cohort_id is not None:
        select_items.append("COALESCE(oc.outcome_flag, 0) AS y")
    if include_demographics:
        select_items.extend([
            "date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age_at_index",
            "p.gender_concept_id",
            "p.race_concept_id",
            "p.ethnicity_concept_id",
            "YEAR(c.cohort_start_date) AS index_year",
        ])
    select_items.extend(lookback_selects)

    select_clause = ",\n            ".join(select_items)
    all_joins = "\n        ".join([j for j in [demo_join, outcome_join] + lookback_joins if j])

    dense_sql = f"""
        SELECT 
            {select_clause}
        FROM cohort c
        {all_joins}
        WHERE c.cohort_definition_id = {int(cohort_id)}
        GROUP BY 
            c.subject_id, c.cohort_start_date, c.cohort_end_date
            {', oc.outcome_flag' if outcome_cohort_id is not None else ''}
            {', p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id' if include_demographics else ''}
        ORDER BY c.subject_id, c.cohort_start_date;
    """

    if fmt == "sparse":
        # Returns standard OHDSI (row_id, covariate_id, covariate_value)
        df_dense = con.execute(dense_sql).df()
        sparse_rows = []
        feature_cols = [col for col in df_dense.columns if col not in ("subject_id", "cohort_start_date", "cohort_end_date", "y")]

        for row_id, (_, row) in enumerate(df_dense.iterrows(), start=1):
            for col_idx, col_name in enumerate(feature_cols, start=101):
                val = row[col_name]
                if pd.notna(val) and val != 0:
                    sparse_rows.append({
                        "row_id": row_id,
                        "subject_id": row["subject_id"],
                        "covariate_id": col_idx,
                        "covariate_name": col_name,
                        "covariate_value": float(val),
                    })
        return pd.DataFrame(sparse_rows)

    if fmt in ("arrow", "pyarrow"):
        reader = con.execute(dense_sql).arrow()
        return reader.read_all() if hasattr(reader, "read_all") else reader

    if fmt == "polars":
        try:
            import polars as pl
            reader = con.execute(dense_sql).arrow()
            return pl.from_arrow(reader.read_all() if hasattr(reader, "read_all") else reader)
        except ImportError:
            # Fall back to PyArrow table if polars is not installed
            reader = con.execute(dense_sql).arrow()
            return reader.read_all() if hasattr(reader, "read_all") else reader

    return con.execute(dense_sql).df()


def extract_temporal_features(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    lookback_days: int = 365,
    encounter_categories: dict[str, Sequence[int]] | None = None,
    include_demographics: bool = True,
    include_stay_characteristics: bool = True,
    format: str = "df",
) -> Any:
    r"""Extracts prior utilization, recency, stay characteristics, and baseline demographics.
    
    Computes zero-copy windowed historical encounter counts in [t_admit - lookback_days, t_admit),
    days since prior encounters, prior 30-day readmissions in lookback, index LOS, and discharge categories.
    
    Args:
        con: Active DuckDB connection.
        cohort_table: Cohort table or view name (default 'cohort').
        cohort_id: Optional cohort definition ID filter if referencing standard cohort table.
        lookback_days: Lookback window in days prior to admission (default 365).
        encounter_categories: Optional dict mapping category name to sequence of visit_concept_ids.
        include_demographics: Whether to include age, age groups, sex, race, ethnicity.
        include_stay_characteristics: Whether to include index LOS and discharge disposition.
        format: Output format ('df', 'arrow', 'polars').
        
    Returns:
        pd.DataFrame, pyarrow.Table, or polars.DataFrame.
    """
    fmt = format.lower().strip()
    filter_cohort = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""

    cols_info = con.execute(f"DESCRIBE SELECT * FROM {cohort_table} LIMIT 0").fetchall()
    actual_cols = {c[0].upper(): c[0] for c in cols_info}
    has_outcome = "OUTCOME_FLAG" in actual_cols or "Y" in actual_cols
    outcome_col = actual_cols.get("OUTCOME_FLAG", actual_cols.get("Y", ""))

    has_visit_id = "VISIT_OCCURRENCE_ID" in actual_cols
    visit_id_col = actual_cols.get("VISIT_OCCURRENCE_ID", "")

    if has_visit_id:
        visit_join = f'LEFT JOIN visit_occurrence v ON c."{visit_id_col}" = v.visit_occurrence_id'
        visit_id_select = f'c."{visit_id_col}" AS visit_occurrence_id,'
    else:
        visit_join = "LEFT JOIN visit_occurrence v ON c.subject_id = v.person_id AND c.cohort_start_date = v.visit_start_date"
        visit_id_select = "v.visit_occurrence_id,"

    stay_select = ""
    if include_stay_characteristics:
        stay_select = """
            date_diff('day', c.cohort_start_date, c.cohort_end_date) AS index_los,
            v.discharged_to_concept_id,
            CASE 
                WHEN COALESCE(v.discharged_to_concept_id, 0) IN (8536, 4132319) THEN 'Home'
                WHEN COALESCE(v.discharged_to_concept_id, 0) IN (581476) THEN 'Home Health'
                WHEN COALESCE(v.discharged_to_concept_id, 0) IN (38004284, 8676, 8863, 8920, 38004285) THEN 'SNF/Rehab'
                WHEN COALESCE(v.discharged_to_concept_id, 0) = 44814650 THEN 'AMA'
                WHEN COALESCE(v.discharged_to_concept_id, 0) = 4216643 THEN 'Expired'
                ELSE 'Other'
            END AS discharge_category,
        """

    demo_select = ""
    demo_join = ""
    if include_demographics:
        demo_select = """
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age_at_admission,
            CASE 
                WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) < 18 THEN '<18'
                WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 18 AND 44 THEN '18-44'
                WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 45 AND 64 THEN '45-64'
                WHEN date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) BETWEEN 65 AND 74 THEN '65-74'
                ELSE '75+'
            END AS age_group,
            p.gender_concept_id,
            CASE WHEN p.gender_concept_id = 8532 THEN 'Female' WHEN p.gender_concept_id = 8507 THEN 'Male' ELSE 'Other' END AS gender_name,
            p.race_concept_id,
            CASE WHEN p.race_concept_id = 8527 THEN 'White' WHEN p.race_concept_id = 8516 THEN 'Black' ELSE 'Other' END AS race_name,
            p.ethnicity_concept_id,
            CASE WHEN p.ethnicity_concept_id = 38003563 THEN 'Hispanic' WHEN p.ethnicity_concept_id = 38003564 THEN 'Non-Hispanic' ELSE 'Other' END AS ethnicity_name,
        """
        demo_join = "LEFT JOIN person p ON c.subject_id = p.person_id"

    if encounter_categories is None:
        enc_selects = """
            COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9201) THEN pv.visit_occurrence_id END) AS prior_ip_count,
            COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9203) THEN pv.visit_occurrence_id END) AS prior_ed_count,
            COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9201, 9203) THEN pv.visit_occurrence_id END) AS prior_ip_ed_obs_count,
            COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN (9202) THEN pv.visit_occurrence_id END) AS prior_op_count,
            COUNT(DISTINCT pv.visit_occurrence_id) AS prior_visit_count,
        """
    else:
        parts = []
        for cat_name, cat_ids in encounter_categories.items():
            ids_str = ", ".join(str(int(i)) for i in cat_ids)
            parts.append(f"COUNT(DISTINCT CASE WHEN pv.visit_concept_id IN ({ids_str}) THEN pv.visit_occurrence_id END) AS prior_{cat_name}_count")
        parts.append("COUNT(DISTINCT pv.visit_occurrence_id) AS prior_visit_count")
        enc_selects = ",\n            ".join(parts) + ",\n"

    outcome_select = f'c."{outcome_col}" AS outcome_flag,' if has_outcome else ""

    sql = f"""
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        c.cohort_end_date,
        {visit_id_select}
        {outcome_select}
        {stay_select}
        {demo_select}
        {enc_selects}
        MIN(date_diff('day', pv.visit_start_date, c.cohort_start_date)) AS days_since_prior_encounter,
        MIN(CASE WHEN pv.visit_concept_id IN (9201, 9203) THEN date_diff('day', pv.visit_start_date, c.cohort_start_date) END) AS days_since_prior_ip_ed,
        COUNT(DISTINCT CASE 
            WHEN pv.visit_concept_id = 9201 
             AND EXISTS (
                 SELECT 1 FROM visit_occurrence prev_ip
                 WHERE prev_ip.person_id = c.subject_id
                   AND prev_ip.visit_occurrence_id != pv.visit_occurrence_id
                   AND prev_ip.visit_concept_id = 9201
                   AND pv.visit_start_date > prev_ip.visit_end_date
                   AND pv.visit_start_date <= prev_ip.visit_end_date + 30
             )
            THEN pv.visit_occurrence_id END) AS prior_readmissions_30d
    FROM {cohort_table} c
    {visit_join}
    {demo_join}
    LEFT JOIN visit_occurrence pv 
      ON c.subject_id = pv.person_id 
     AND pv.visit_start_date >= (c.cohort_start_date - {int(lookback_days)})
     AND pv.visit_start_date < c.cohort_start_date
    {filter_cohort}
    GROUP BY 
        c.subject_id, c.cohort_start_date, c.cohort_end_date
        {', ' + (f'c."{visit_id_col}"' if has_visit_id else 'v.visit_occurrence_id')}
        {', c."' + outcome_col + '"' if has_outcome else ''}
        {', v.discharged_to_concept_id' if include_stay_characteristics else ''}
        {', p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id' if include_demographics else ''}
    ORDER BY c.subject_id, c.cohort_start_date;
    """

    if fmt in ("arrow", "pyarrow"):
        reader = con.execute(sql).arrow()
        return reader.read_all() if hasattr(reader, "read_all") else reader
    if fmt == "polars":
        try:
            import polars as pl
            reader = con.execute(sql).arrow()
            return pl.from_arrow(reader.read_all() if hasattr(reader, "read_all") else reader)
        except ImportError:
            reader = con.execute(sql).arrow()
            return reader.read_all() if hasattr(reader, "read_all") else reader
    return con.execute(sql).df()


def aggregate_concept_sets(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    concept_sets: dict[str, Sequence[int]] | None = None,
    domain: str = "drug",
    lookback_days: int = 365,
    window: str = "lookback",
    as_flag: bool = True,
    format: str = "df",
) -> Any:
    r"""Maps high-level concept sets to standard descendants and aggregates across cohort observation windows.
    
    Utilizes Athena hierarchy traversal (concept_ancestor) to resolve all standard descendant
    concept IDs, linking to domain tables (drug_exposure, condition_occurrence, etc.) to produce
    indicator flags or event counts.
    
    Args:
        con: Active DuckDB connection.
        cohort_table: Cohort table or view name (default 'cohort').
        cohort_id: Optional cohort definition ID filter.
        concept_sets: Dictionary mapping set name to ancestor concept IDs. If None and domain='drug',
                      defaults to the 8 core chronic medication classes.
        domain: Domain table to aggregate ('drug', 'condition', 'procedure', 'measurement').
        lookback_days: Lookback window in days (default 365).
        window: Windowing strategy: 'lookback' ([t0-W, t0)), 'stay' ([t0, t_end]), or 'all' (<= t_end).
        as_flag: If True, returns 0/1 binary indicator; if False, returns event count.
        format: Output format ('df', 'arrow', 'polars').
        
    Returns:
        pd.DataFrame, pyarrow.Table, or polars.DataFrame.
    """
    from omop_etl.table1 import DEFAULT_MEDICATION_CONCEPTS

    if concept_sets is None and domain.lower() == "drug":
        sets = DEFAULT_MEDICATION_CONCEPTS
    elif concept_sets is None:
        raise ValueError("concept_sets dictionary must be provided when domain is not 'drug'.")
    else:
        sets = concept_sets

    filter_cohort = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""
    domain_lower = domain.lower().strip()

    if domain_lower == "drug":
        event_table = "drug_exposure"
        concept_col = "drug_concept_id"
        date_col = "drug_exposure_start_date"
        id_col = "drug_exposure_id"
    elif domain_lower == "condition":
        event_table = "condition_occurrence"
        concept_col = "condition_concept_id"
        date_col = "condition_start_date"
        id_col = "condition_occurrence_id"
    elif domain_lower == "procedure":
        event_table = "procedure_occurrence"
        concept_col = "procedure_concept_id"
        date_col = "procedure_date"
        id_col = "procedure_occurrence_id"
    elif domain_lower == "measurement":
        event_table = "measurement"
        concept_col = "measurement_concept_id"
        date_col = "measurement_date"
        id_col = "measurement_id"
    else:
        raise ValueError(f"Unsupported domain '{domain}'. Use 'drug', 'condition', 'procedure', or 'measurement'.")

    con.execute("DROP TABLE IF EXISTS _temp_seed_concepts;")
    con.execute("CREATE TEMPORARY TABLE _temp_seed_concepts (set_name VARCHAR, ancestor_concept_id BIGINT);")
    for s_name, anc_ids in sets.items():
        if isinstance(anc_ids, int):
            anc_ids = [anc_ids]
        for aid in anc_ids:
            con.execute(f"INSERT INTO _temp_seed_concepts VALUES ('{s_name}', {int(aid)});")

    con.execute("DROP TABLE IF EXISTS _concept_set_resolved;")
    con.execute("""
        CREATE TEMPORARY TABLE _concept_set_resolved AS
        SELECT s.set_name, ca.descendant_concept_id AS concept_id
        FROM _temp_seed_concepts s
        JOIN concept_ancestor ca ON s.ancestor_concept_id = ca.ancestor_concept_id
        UNION
        SELECT s.set_name, s.ancestor_concept_id AS concept_id
        FROM _temp_seed_concepts s;
    """)

    if window == "lookback":
        date_cond = f"e.{date_col} >= (c.cohort_start_date - {int(lookback_days)}) AND e.{date_col} < c.cohort_start_date"
    elif window == "stay":
        date_cond = f"e.{date_col} >= c.cohort_start_date AND e.{date_col} <= c.cohort_end_date"
    elif window == "all":
        date_cond = f"e.{date_col} <= c.cohort_end_date"
    else:
        raise ValueError(f"Unsupported window '{window}'. Use 'lookback', 'stay', or 'all'.")

    set_names = list(sets.keys())
    if as_flag:
        agg_exprs = ",\n            ".join(
            f"MAX(CASE WHEN e.set_name = '{name}' THEN 1 ELSE 0 END) AS {name}"
            for name in set_names
        )
    else:
        agg_exprs = ",\n            ".join(
            f"COUNT(DISTINCT CASE WHEN e.set_name = '{name}' THEN e.{id_col} END) AS {name}"
            for name in set_names
        )


    sql = f"""
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        {agg_exprs}
    FROM {cohort_table} c
    LEFT JOIN (
        SELECT e.person_id, e.{date_col}, e.{id_col}, r.set_name
        FROM {event_table} e
        JOIN _concept_set_resolved r ON e.{concept_col} = r.concept_id
    ) e ON c.subject_id = e.person_id AND {date_cond}
    {filter_cohort}
    GROUP BY c.subject_id, c.cohort_start_date
    ORDER BY c.subject_id, c.cohort_start_date;
    """

    res_df = con.execute(sql).df()
    con.execute("DROP TABLE IF EXISTS _temp_seed_concepts; DROP TABLE IF EXISTS _concept_set_resolved;")

    fmt = format.lower().strip()
    if fmt in ("arrow", "pyarrow"):
        import pyarrow as pa
        return pa.Table.from_pandas(res_df)
    if fmt == "polars":
        try:
            import polars as pl
            return pl.from_pandas(res_df)
        except ImportError:
            return res_df
    return res_df


def extract_measurements(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    loinc_map: dict[str, Sequence[str | int]] | None = None,
    strategy: str = "last_before_discharge",
    window: str = "stay",
    lookback_days: int | None = None,
    format: str = "df",
) -> Any:
    r"""Extracts consolidated laboratory and vital sign measurements with selectable windowing strategies.
    
    Supports LOINC code consolidation groups (e.g. BUN, WBC, Creatinine) and aggregates numeric
    measurement values (`value_as_number`) during the index stay or lookback window.
    
    Args:
        con: Active DuckDB connection.
        cohort_table: Cohort table or view name (default 'cohort').
        cohort_id: Optional cohort definition ID filter.
        loinc_map: Dictionary mapping measurement feature name to LOINC codes or concept IDs.
                   If None, defaults to the 14 consolidated acute labs and vitals.
        strategy: Aggregation strategy: 'last_before_discharge', 'first_on_admission',
                  'mean', 'median', 'min', or 'max'.
        window: Windowing scope: 'stay' ([t0, t_end]), 'lookback' ([t0-W, t0)), or 'all' (<= t_end).
        lookback_days: Lookback window in days when window='lookback' (default 365).
        format: Output format ('df', 'arrow', 'polars').
        
    Returns:
        pd.DataFrame, pyarrow.Table, or polars.DataFrame.
    """
    from omop_etl.table1 import DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS

    if loinc_map is None:
        active_map = {**DEFAULT_LAB_LOINCS, **DEFAULT_VITAL_LOINCS}
    else:
        active_map = loinc_map

    strat = strategy.lower().strip()
    strat_allowed = ("last_before_discharge", "first_on_admission", "mean", "median", "min", "max")
    if strat not in strat_allowed:
        raise ValueError(f"Unsupported strategy '{strategy}'. Use one of {strat_allowed}.")

    filter_cohort = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""

    con.execute("DROP TABLE IF EXISTS _temp_loinc_seeds;")
    con.execute("CREATE TEMPORARY TABLE _temp_loinc_seeds (lab_name VARCHAR, code_or_id VARCHAR, is_concept_id BOOLEAN);")
    for lab_name, codes in active_map.items():
        if isinstance(codes, (str, int)):
            codes = [codes]
        for cd in codes:
            is_id = isinstance(cd, int) or (isinstance(cd, str) and cd.isdigit())
            con.execute(f"INSERT INTO _temp_loinc_seeds VALUES ('{lab_name}', '{cd}', {is_id});")

    con.execute("DROP TABLE IF EXISTS _temp_loinc_resolved;")
    con.execute("""
        CREATE TEMPORARY TABLE _temp_loinc_resolved AS
        SELECT s.lab_name, CAST(s.code_or_id AS BIGINT) AS concept_id, s.code_or_id AS loinc_code
        FROM _temp_loinc_seeds s
        WHERE s.is_concept_id = TRUE
        UNION
        SELECT s.lab_name, c.concept_id, s.code_or_id AS loinc_code
        FROM _temp_loinc_seeds s
        JOIN concept c ON (c.vocabulary_id = 'LOINC' AND c.concept_code = s.code_or_id)
        WHERE s.is_concept_id = FALSE;
    """)

    if window == "stay":
        date_cond = "m.measurement_date >= c.cohort_start_date AND m.measurement_date <= c.cohort_end_date"
    elif window == "lookback":
        lb = int(lookback_days or 365)
        date_cond = f"m.measurement_date >= (c.cohort_start_date - {lb}) AND m.measurement_date < c.cohort_start_date"
    elif window == "all":
        date_cond = "m.measurement_date <= c.cohort_end_date"
    else:
        raise ValueError(f"Unsupported window '{window}'. Use 'stay', 'lookback', or 'all'.")

    cohort_filter_and = f"AND c.cohort_definition_id = {int(cohort_id)}" if cohort_id is not None else ""

    con.execute("DROP TABLE IF EXISTS _temp_matching_meas;")
    con.execute(f"""
        CREATE TEMPORARY TABLE _temp_matching_meas AS
        SELECT 
            c.subject_id,
            c.cohort_start_date,
            r.lab_name,
            m.measurement_id,
            m.measurement_date,
            m.measurement_datetime,
            m.value_as_number
        FROM {cohort_table} c
        JOIN measurement m ON c.subject_id = m.person_id AND {date_cond}
        JOIN (
            SELECT lab_name, concept_id FROM _temp_loinc_resolved
            UNION
            SELECT s.lab_name, m2.measurement_concept_id AS concept_id
            FROM _temp_loinc_seeds s
            JOIN measurement m2 ON m2.measurement_source_value = s.code_or_id
        ) r ON m.measurement_concept_id = r.concept_id
        WHERE m.value_as_number IS NOT NULL
          {cohort_filter_and};
    """)

    lab_names = sorted(list(active_map.keys()))


    if strat == "last_before_discharge":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, value_as_number
            FROM (
                SELECT 
                    subject_id, cohort_start_date, lab_name, value_as_number,
                    ROW_NUMBER() OVER (
                        PARTITION BY subject_id, cohort_start_date, lab_name 
                        ORDER BY COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP)) DESC, measurement_id DESC
                    ) AS rn
                FROM _temp_matching_meas
            ) WHERE rn = 1
        """
    elif strat == "first_on_admission":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, value_as_number
            FROM (
                SELECT 
                    subject_id, cohort_start_date, lab_name, value_as_number,
                    ROW_NUMBER() OVER (
                        PARTITION BY subject_id, cohort_start_date, lab_name 
                        ORDER BY COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP)) ASC, measurement_id ASC
                    ) AS rn
                FROM _temp_matching_meas
            ) WHERE rn = 1
        """
    elif strat == "mean":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, AVG(value_as_number) AS value_as_number
            FROM _temp_matching_meas
            GROUP BY subject_id, cohort_start_date, lab_name
        """
    elif strat == "median":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, MEDIAN(value_as_number) AS value_as_number
            FROM _temp_matching_meas
            GROUP BY subject_id, cohort_start_date, lab_name
        """
    elif strat == "min":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, MIN(value_as_number) AS value_as_number
            FROM _temp_matching_meas
            GROUP BY subject_id, cohort_start_date, lab_name
        """
    elif strat == "max":
        agg_sql = """
            SELECT subject_id, cohort_start_date, lab_name, MAX(value_as_number) AS value_as_number
            FROM _temp_matching_meas
            GROUP BY subject_id, cohort_start_date, lab_name
        """

    con.execute("DROP TABLE IF EXISTS _temp_meas_aggregated;")
    con.execute(f"CREATE TEMPORARY TABLE _temp_meas_aggregated AS {agg_sql};")

    pivots = ",\n            ".join(
        f"MAX(CASE WHEN m.lab_name = '{name}' THEN m.value_as_number END) AS {name}"
        for name in lab_names
    )

    pivot_sql = f"""
    SELECT 
        c.subject_id,
        c.cohort_start_date,
        {pivots}
    FROM {cohort_table} c
    LEFT JOIN _temp_meas_aggregated m ON c.subject_id = m.subject_id AND c.cohort_start_date = m.cohort_start_date
    {filter_cohort}
    GROUP BY c.subject_id, c.cohort_start_date
    ORDER BY c.subject_id, c.cohort_start_date;
    """

    res_df = con.execute(pivot_sql).df()
    con.execute("DROP TABLE IF EXISTS _temp_loinc_seeds; DROP TABLE IF EXISTS _temp_loinc_resolved; DROP TABLE IF EXISTS _temp_matching_meas; DROP TABLE IF EXISTS _temp_meas_aggregated;")

    fmt = format.lower().strip()
    if fmt in ("arrow", "pyarrow"):
        import pyarrow as pa
        return pa.Table.from_pandas(res_df)
    if fmt == "polars":
        try:
            import polars as pl
            return pl.from_pandas(res_df)
        except ImportError:
            return res_df
    return res_df


# ======================================================================================
# RFC 3.1 & ML Modality 3: 31 Elixhauser Comorbidities & Charlson Comorbidity Index
# ======================================================================================

ELIXHAUSER_DOMAINS = {
    "chf": {
        "col": "elix_chf", "name": "Congestive Heart Failure", "weight": 7,
        "icd10": ["I099", "I110", "I130", "I132", "I255", "I420", "I425", "I426", "I427", "I428", "I429", "I43", "I50", "P290"],
        "icd9": ["39891", "40201", "40211", "40291", "40401", "40403", "40411", "40413", "40491", "40493", "4254", "4255", "4256", "4257", "4258", "4259", "428"],
        "snomed": [84114007, 316139, 314378, 422944002],
    },
    "arrhythmia": {
        "col": "elix_arrhythmia", "name": "Cardiac Arrhythmias", "weight": 5,
        "icd10": ["I441", "I442", "I443", "I456", "I459", "I47", "I48", "I49", "R000", "R001", "R008", "T821", "Z450", "Z950"],
        "icd9": ["4260", "42613", "4267", "4269", "4270", "4271", "4272", "4273", "4274", "4276", "4277", "4278", "4279", "7850", "V450", "V533"],
        "snomed": [17366009, 313217, 49436004, 315286],
    },
    "valvular": {
        "col": "elix_valvular", "name": "Valvular Disease", "weight": -1,
        "icd10": ["A520", "I05", "I06", "I07", "I08", "I091", "I098", "I34", "I35", "I36", "I37", "I38", "I39", "Q230", "Q231", "Q232", "Q233", "Z952", "Z953", "Z954"],
        "icd9": ["0932", "394", "395", "396", "397", "424", "7463", "7464", "7465", "7466", "V422", "V433"],
        "snomed": [368009, 4014295, 315295],
    },
    "pulm_circ": {
        "col": "elix_pulm_circ", "name": "Pulmonary Circulation Disorders", "weight": 4,
        "icd10": ["I26", "I27", "I280", "I288", "I289"],
        "icd9": ["4150", "4151", "416", "4170", "4178", "4179"],
        "snomed": [70687000, 312327],
    },
    "pvd": {
        "col": "elix_pvd", "name": "Peripheral Vascular Disorders", "weight": 2,
        "icd10": ["I70", "I71", "I731", "I738", "I739", "I771", "I790", "I792", "K551", "K558", "K559", "Z958", "Z959"],
        "icd9": ["0930", "4373", "440", "441", "4431", "4432", "4438", "4439", "4471", "5571", "5579", "V434"],
        "snomed": [399957001, 321052, 318798],
    },
    "htn_uncomp": {
        "col": "elix_htn_uncomp", "name": "Hypertension, Uncomplicated", "weight": 0,
        "icd10": ["I10"],
        "icd9": ["401"],
        "snomed": [38341003, 316866, 320128],
    },
    "htn_comp": {
        "col": "elix_htn_comp", "name": "Hypertension, Complicated", "weight": 0,
        "icd10": ["I11", "I12", "I13", "I15"],
        "icd9": ["402", "403", "404", "405"],
        "snomed": [319844, 317576],
    },
    "paralysis": {
        "col": "elix_paralysis", "name": "Paralysis", "weight": 7,
        "icd10": ["G041", "G114", "G801", "G802", "G81", "G82", "G830", "G831", "G832", "G833", "G834", "G839"],
        "icd9": ["3341", "342", "343", "3440", "3441", "3442", "3443", "3444", "3445", "3446", "3449"],
        "snomed": [381316, 437750],
    },
    "neuro_other": {
        "col": "elix_neuro_other", "name": "Other Neurological Disorders", "weight": 6,
        "icd10": ["G10", "G11", "G12", "G13", "G20", "G21", "G22", "G254", "G255", "G312", "G318", "G319", "G32", "G35", "G36", "G37", "G40", "G41", "G931", "G934", "R470", "R56"],
        "icd9": ["3319", "3320", "3321", "3334", "3335", "33392", "334", "335", "340", "341", "345", "3481", "3483", "7803", "7843"],
        "snomed": [374341, 381537, 439847],
    },
    "copd": {
        "col": "elix_copd", "name": "Chronic Pulmonary Disease", "weight": 3,
        "icd10": ["I278", "I279", "J40", "J41", "J42", "J43", "J44", "J45", "J46", "J47", "J60", "J61", "J62", "J63", "J64", "J65", "J66", "J67", "J684", "J701", "J703"],
        "icd9": ["4168", "4169", "490", "491", "492", "493", "494", "495", "496", "497", "498", "499", "500", "501", "502", "503", "504", "505", "5064", "5081", "5088"],
        "snomed": [13645005, 317009, 313296],
    },
    "dm_uncomp": {
        "col": "elix_dm_uncomp", "name": "Diabetes, Uncomplicated", "weight": 0,
        "icd10": ["E100", "E101", "E109", "E110", "E111", "E119", "E120", "E121", "E129", "E130", "E131", "E139", "E140", "E141", "E149"],
        "icd9": ["2500", "2501", "2502", "2503"],
        "snomed": [201820, 443767],
    },
    "dm_comp": {
        "col": "elix_dm_comp", "name": "Diabetes, Complicated", "weight": 0,
        "icd10": ["E102", "E103", "E104", "E105", "E106", "E107", "E108", "E112", "E113", "E114", "E115", "E116", "E117", "E118", "E122", "E123", "E124", "E125", "E126", "E127", "E128", "E132", "E133", "E134", "E135", "E136", "E137", "E138", "E142", "E143", "E144", "E145", "E146", "E147", "E148"],
        "icd9": ["2504", "2505", "2506", "2507", "2508", "2509"],
        "snomed": [443734, 443729, 443735],
    },
    "hypothyroid": {
        "col": "elix_hypothyroid", "name": "Hypothyroidism", "weight": 0,
        "icd10": ["E00", "E01", "E02", "E03", "E890"],
        "icd9": ["2409", "243", "244", "2461", "2468"],
        "snomed": [140214, 4032338],
    },
    "renal_failure": {
        "col": "elix_renal_failure", "name": "Renal Failure", "weight": 5,
        "icd10": ["I120", "I131", "N18", "N19", "N250", "Z490", "Z491", "Z492", "Z940", "Z992"],
        "icd9": ["40301", "40311", "40391", "40402", "40403", "40412", "40413", "40492", "40493", "582", "5830", "5831", "5832", "5834", "5836", "5837", "585", "586", "5880", "V420", "V451", "V56"],
        "snomed": [443614, 193782, 197320],
    },
    "liver_disease": {
        "col": "elix_liver_disease", "name": "Liver Disease", "weight": 11,
        "icd10": ["B18", "I85", "I864", "I982", "K70", "K711", "K713", "K714", "K715", "K717", "K72", "K73", "K74", "K760", "K762", "K763", "K764", "K765", "K766", "K767", "K768", "K769", "Z944"],
        "icd9": ["07022", "07023", "07032", "07033", "07044", "07054", "0706", "0709", "4560", "4561", "4562", "570", "571", "5722", "5723", "5724", "5728", "5733", "5734", "5738", "5739", "V427"],
        "snomed": [4245975, 4028300, 4212540],
    },
    "pud": {
        "col": "elix_pud", "name": "Peptic Ulcer Disease", "weight": 0,
        "icd10": ["K25", "K26", "K27", "K28"],
        "icd9": ["531", "532", "533", "534"],
        "snomed": [4027663, 192359],
    },
    "hiv": {
        "col": "elix_hiv", "name": "AIDS/HIV", "weight": 0,
        "icd10": ["B20", "B21", "B22", "B24"],
        "icd9": ["042", "043", "044"],
        "snomed": [439727, 4299839],
    },
    "lymphoma": {
        "col": "elix_lymphoma", "name": "Lymphoma", "weight": 9,
        "icd10": ["C81", "C82", "C83", "C84", "C85", "C88", "C900", "C902", "C96"],
        "icd9": ["200", "201", "202", "2030", "2386"],
        "snomed": [432571, 4178431],
    },
    "mets": {
        "col": "elix_mets", "name": "Metastatic Cancer", "weight": 12,
        "icd10": ["C77", "C78", "C79", "C80"],
        "icd9": ["196", "197", "198", "199"],
        "snomed": [432851, 443384],
    },
    "solid_tumor": {
        "col": "elix_solid_tumor", "name": "Solid Tumor without Metastasis", "weight": 4,
        "icd10": ["C00", "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15", "C16", "C17", "C18", "C19", "C20", "C21", "C22", "C23", "C24", "C25", "C26", "C30", "C31", "C32", "C33", "C34", "C37", "C38", "C39", "C40", "C41", "C43", "C45", "C46", "C47", "C48", "C49", "C50", "C51", "C52", "C53", "C54", "C55", "C56", "C57", "C58", "C60", "C61", "C62", "C63", "C64", "C65", "C66", "C67", "C68", "C69", "C70", "C71", "C72", "C73", "C74", "C75", "C76", "C97"],
        "icd9": ["140", "141", "142", "143", "144", "145", "146", "147", "148", "149", "150", "151", "152", "153", "154", "155", "156", "157", "158", "159", "160", "161", "162", "163", "164", "165", "170", "171", "172", "174", "175", "176", "177", "178", "179", "180", "181", "182", "183", "184", "185", "186", "187", "188", "189", "190", "191", "192", "193", "194", "195"],
        "snomed": [435754, 443388],
    },
    "rheumatic": {
        "col": "elix_rheumatic", "name": "Rheumatoid Arthritis / Collagen", "weight": 0,
        "icd10": ["L940", "L941", "L943", "M05", "M06", "M08", "M120", "M123", "M30", "M310", "M311", "M312", "M313", "M32", "M33", "M34", "M35", "M45", "M461", "M468", "M469"],
        "icd9": ["446", "7010", "7100", "7101", "7102", "7103", "7104", "7108", "7109", "7112", "714", "7193", "720", "725", "7285", "72889"],
        "snomed": [80809, 4173504],
    },
    "coagulopathy": {
        "col": "elix_coagulopathy", "name": "Coagulopathy", "weight": 3,
        "icd10": ["D65", "D66", "D67", "D68", "D691", "D693", "D694", "D695", "D696"],
        "icd9": ["286", "2871", "2873", "2874", "2875"],
        "snomed": [440374, 439857],
    },
    "obesity": {
        "col": "elix_obesity", "name": "Obesity", "weight": -4,
        "icd10": ["E66"],
        "icd9": ["2780"],
        "snomed": [433736, 40480436],
    },
    "weight_loss": {
        "col": "elix_weight_loss", "name": "Weight Loss", "weight": 6,
        "icd10": ["E40", "E41", "E42", "E43", "E44", "E45", "E46", "R634", "R64"],
        "icd9": ["260", "261", "262", "263", "7832", "7994"],
        "snomed": [437525, 4141639],
    },
    "fluid_electrolyte": {
        "col": "elix_fluid_electrolyte", "name": "Fluid & Electrolyte Disorders", "weight": 5,
        "icd10": ["E222", "E86", "E87"],
        "icd9": ["2536", "276"],
        "snomed": [433753, 443428],
    },
    "blood_loss_anemia": {
        "col": "elix_blood_loss_anemia", "name": "Blood Loss Anemia", "weight": -2,
        "icd10": ["D500"],
        "icd9": ["2800"],
        "snomed": [437894],
    },
    "deficiency_anemia": {
        "col": "elix_deficiency_anemia", "name": "Deficiency Anemia", "weight": -2,
        "icd10": ["D508", "D509", "D51", "D52", "D53"],
        "icd9": ["2801", "2802", "2803", "2804", "2805", "2806", "2807", "2808", "2809", "281"],
        "snomed": [439777, 432585],
    },
    "alcohol_abuse": {
        "col": "elix_alcohol_abuse", "name": "Alcohol Abuse", "weight": 0,
        "icd10": ["F10", "E244", "G312", "I426", "K292", "K700", "K703", "K709", "T510", "T511", "T519", "Z502", "Z714", "Z721"],
        "icd9": ["2652", "2911", "2912", "2913", "2915", "2918", "2919", "3030", "3039", "3050", "3575", "4255", "5353", "5710", "5711", "5712", "5713", "9800", "V113"],
        "snomed": [433753, 374375],
    },
    "drug_abuse": {
        "col": "elix_drug_abuse", "name": "Drug Abuse", "weight": -7,
        "icd10": ["F11", "F12", "F13", "F14", "F15", "F16", "F18", "F19", "Z715", "Z722"],
        "icd9": ["292", "304", "3052", "3053", "3054", "3055", "3056", "3057", "3058", "3059", "V6542"],
        "snomed": [436665, 437249],
    },
    "psychoses": {
        "col": "elix_psychoses", "name": "Psychoses", "weight": 0,
        "icd10": ["F20", "F22", "F23", "F24", "F25", "F28", "F29", "F302", "F312", "F315"],
        "icd9": ["2938", "295", "29604", "29614", "29644", "29654", "297", "298"],
        "snomed": [435783, 444101],
    },
    "depression": {
        "col": "elix_depression", "name": "Depression", "weight": -3,
        "icd10": ["F313", "F314", "F315", "F32", "F33", "F341", "F412", "F432"],
        "icd9": ["2962", "2963", "2965", "3004", "3090", "3091", "311"],
        "snomed": [440383, 4152280],
    },
}

CHARLSON_CATEGORIES = {
    "mi": {
        "col": "cci_mi", "name": "Myocardial Infarction", "weight": 1,
        "icd10": ["I21", "I22", "I252"], "icd9": ["410", "412"], "snomed": [4329847, 312327],
    },
    "chf": {
        "col": "cci_chf", "name": "Congestive Heart Failure", "weight": 1,
        "icd10": ["I099", "I110", "I130", "I132", "I255", "I420", "I425", "I426", "I427", "I428", "I429", "I43", "I50", "P290"],
        "icd9": ["39891", "40201", "40211", "40291", "40401", "40403", "40411", "40413", "40491", "40493", "4254", "4255", "4256", "4257", "4258", "4259", "428"],
        "snomed": [84114007, 316139, 314378],
    },
    "pvd": {
        "col": "cci_pvd", "name": "Peripheral Vascular Disease", "weight": 1,
        "icd10": ["I70", "I71", "I731", "I738", "I739", "I771", "I790", "I792", "K551", "K558", "K559", "Z958", "Z959"],
        "icd9": ["0930", "4373", "440", "441", "4431", "4432", "4438", "4439", "4471", "5571", "5579", "V434"],
        "snomed": [321052, 318798],
    },
    "cevd": {
        "col": "cci_cevd", "name": "Cerebrovascular Disease", "weight": 1,
        "icd10": ["G45", "G46", "H340", "I60", "I61", "I62", "I63", "I64", "I65", "I66", "I67", "I68", "I69"],
        "icd9": ["36234", "430", "431", "432", "433", "434", "435", "436", "437", "438"],
        "snomed": [37311061, 381537],
    },
    "dementia": {
        "col": "cci_dementia", "name": "Dementia", "weight": 1,
        "icd10": ["F00", "F01", "F02", "F03", "F051", "G30", "G311"],
        "icd9": ["290", "2941", "3312"],
        "snomed": [4182210, 374341],
    },
    "copd": {
        "col": "cci_copd", "name": "Chronic Pulmonary Disease", "weight": 1,
        "icd10": ["I278", "I279", "J40", "J41", "J42", "J43", "J44", "J45", "J46", "J47", "J60", "J61", "J62", "J63", "J64", "J65", "J66", "J67", "J684", "J701", "J703"],
        "icd9": ["4168", "4169", "490", "491", "492", "493", "494", "495", "496", "497", "498", "499", "500", "501", "502", "503", "504", "505", "5064", "5081", "5088"],
        "snomed": [13645005, 317009],
    },
    "rheum": {
        "col": "cci_rheum", "name": "Rheumatic Disease", "weight": 1,
        "icd10": ["M05", "M06", "M315", "M32", "M33", "M34", "M351", "M353", "M360"],
        "icd9": ["4465", "7100", "7101", "7104", "7140", "7141", "7142", "7148", "725"],
        "snomed": [80809, 4173504],
    },
    "pud": {
        "col": "cci_pud", "name": "Peptic Ulcer Disease", "weight": 1,
        "icd10": ["K25", "K26", "K27", "K28"],
        "icd9": ["531", "532", "533", "534"],
        "snomed": [4027663, 192359],
    },
    "mild_liver": {
        "col": "cci_mild_liver", "name": "Mild Liver Disease", "weight": 1,
        "icd10": ["B18", "K700", "K701", "K702", "K703", "K709", "K713", "K714", "K715", "K717", "K73", "K74", "K760", "K762", "K763", "K764", "K768", "K769", "Z944"],
        "icd9": ["07022", "07023", "07032", "07033", "07044", "07054", "0706", "0709", "570", "571", "5733", "5734", "5738", "5739", "V427"],
        "snomed": [4245975, 4028300],
    },
    "dm_uncomp": {
        "col": "cci_dm_uncomp", "name": "Diabetes without Complication", "weight": 1,
        "icd10": ["E100", "E101", "E106", "E108", "E109", "E110", "E111", "E116", "E118", "E119", "E120", "E121", "E126", "E128", "E129", "E130", "E131", "E136", "E138", "E139", "E140", "E141", "E146", "E148", "E149"],
        "icd9": ["2500", "2501", "2502", "2503", "2508", "2509"],
        "snomed": [201820, 443767],
    },
    "dm_comp": {
        "col": "cci_dm_comp", "name": "Diabetes with Complication", "weight": 2,
        "icd10": ["E102", "E103", "E104", "E105", "E107", "E112", "E113", "E114", "E115", "E117", "E122", "E123", "E124", "E125", "E127", "E132", "E133", "E134", "E135", "E137", "E142", "E143", "E144", "E145", "E147"],
        "icd9": ["2504", "2505", "2506", "2507"],
        "snomed": [443734, 443729],
    },
    "plegia": {
        "col": "cci_plegia", "name": "Hemiplegia or Paraplegia", "weight": 2,
        "icd10": ["G041", "G114", "G801", "G802", "G81", "G82", "G830", "G831", "G832", "G833", "G834", "G839"],
        "icd9": ["3341", "342", "343", "3440", "3441", "3442", "3443", "3444", "3445", "3446", "3449"],
        "snomed": [381316, 437750],
    },
    "renal": {
        "col": "cci_renal", "name": "Renal Disease", "weight": 2,
        "icd10": ["I120", "I131", "N18", "N19", "N250", "Z490", "Z491", "Z492", "Z940", "Z992"],
        "icd9": ["40301", "40311", "40391", "40402", "40403", "40412", "40413", "40492", "40493", "582", "5830", "5831", "5832", "5834", "5836", "5837", "585", "586", "5880", "V420", "V451", "V56"],
        "snomed": [443614, 193782],
    },
    "malignancy": {
        "col": "cci_malignancy", "name": "Any Malignancy", "weight": 2,
        "icd10": ["C00", "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15", "C16", "C17", "C18", "C19", "C20", "C21", "C22", "C23", "C24", "C25", "C26", "C30", "C31", "C32", "C33", "C34", "C37", "C38", "C39", "C40", "C41", "C43", "C45", "C46", "C47", "C48", "C49", "C50", "C51", "C52", "C53", "C54", "C55", "C56", "C57", "C58", "C60", "C61", "C62", "C63", "C64", "C65", "C66", "C67", "C68", "C69", "C70", "C71", "C72", "C73", "C74", "C75", "C76", "C81", "C82", "C83", "C84", "C85", "C88", "C90", "C91", "C92", "C93", "C94", "C95", "C96", "C97"],
        "icd9": ["140", "141", "142", "143", "144", "145", "146", "147", "148", "149", "150", "151", "152", "153", "154", "155", "156", "157", "158", "159", "160", "161", "162", "163", "164", "165", "170", "171", "172", "174", "175", "176", "177", "178", "179", "180", "181", "182", "183", "184", "185", "186", "187", "188", "189", "190", "191", "192", "193", "194", "195", "200", "201", "202", "203", "204", "205", "206", "207", "208", "2386"],
        "snomed": [435754, 432571],
    },
    "mod_severe_liver": {
        "col": "cci_mod_severe_liver", "name": "Moderate or Severe Liver Disease", "weight": 3,
        "icd10": ["I850", "I859", "I864", "I982", "K704", "K711", "K721", "K729", "K765", "K766", "K767"],
        "icd9": ["4560", "4561", "4562", "5722", "5723", "5724", "5728"],
        "snomed": [4212540],
    },
    "mets": {
        "col": "cci_mets", "name": "Metastatic Solid Tumor", "weight": 6,
        "icd10": ["C77", "C78", "C79", "C80"],
        "icd9": ["196", "197", "198", "199"],
        "snomed": [432851, 443384],
    },
    "hiv": {
        "col": "cci_hiv", "name": "AIDS/HIV", "weight": 6,
        "icd10": ["B20", "B21", "B22", "B24"],
        "icd9": ["042", "043", "044"],
        "snomed": [439727, 4299839],
    },
}


def _build_comorbidity_condition(domain_info, source_and_standard=True):
    """Builds a vectorized SQL boolean condition for a comorbidity domain."""
    clauses = []
    # Source codes (ICD-10 and ICD-9 prefixes)
    icd_prefixes = domain_info.get("icd10", []) + domain_info.get("icd9", [])
    if icd_prefixes:
        like_parts = [f"_src_code LIKE '{p}%'" for p in icd_prefixes]
        clauses.append(f"({' OR '.join(like_parts)})")
    # Standard concept IDs (SNOMED root/ancestor concepts)
    if source_and_standard and domain_info.get("snomed"):
        snomed_ids = ", ".join(str(int(c)) for c in domain_info["snomed"])
        clauses.append(f"co.condition_concept_id IN ({snomed_ids})")
    return " OR ".join(clauses) if clauses else "FALSE"


def extract_elixhauser_comorbidities(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    person_col: str = "subject_id",
    index_date_col: str = "cohort_start_date",
    lookback_days: int | None = 365,
    source_and_standard: bool = True,
    format: str = "df",
) -> Any:
    """Extracts standard 31 Elixhauser Comorbidity Domains (AHRQ / Quan et al. 2005) and van Walraven score.

    RFC 3.1: Computes all 31 binary comorbidity indicators within a baseline lookback window
    [index - lookback_days, index), checking both source ICD-9/10 values and standard SNOMED concepts.
    Also computes the composite van Walraven index score and comorbidity count.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Active DuckDB connection.
    cohort_table : str, default 'cohort'
        Table containing the index cohort population.
    cohort_id : int, optional
        Optional cohort definition ID to filter rows if cohort_definition_id exists.
    person_col : str, default 'subject_id'
        Column name identifying the patient/person.
    index_date_col : str, default 'cohort_start_date'
        Column name identifying the index stay admission/event date.
    lookback_days : int or None, default 365
        Lookback window in days strictly prior to index date. If None, uses all prior history.
    source_and_standard : bool, default True
        Whether to evaluate both condition_source_value and standard condition_concept_id.
    format : str, default 'df'
        Output format: 'df' (Pandas), 'arrow', or 'polars'.

    Returns
    -------
    pd.DataFrame, pyarrow.Table, or polars.DataFrame
        Table with person_col, index_date_col, all 31 elix_* binary columns,
        elix_van_walraven_score, and elix_total_conditions.
    """
    tbl_cols = [r[0].lower() for r in con.execute(f"DESCRIBE {cohort_table};").fetchall()]
    pcol = person_col if person_col in tbl_cols else ("person_id" if "person_id" in tbl_cols else tbl_cols[0])
    dcol = index_date_col if index_date_col in tbl_cols else ("visit_start_date" if "visit_start_date" in tbl_cols else tbl_cols[1])

    filter_cohort = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if (cohort_id is not None and "cohort_definition_id" in tbl_cols) else ""
    window_clause = f"AND co.condition_start_date BETWEEN c.{dcol} - {int(lookback_days)} AND c.{dcol} - 1" if lookback_days is not None else f"AND co.condition_start_date < c.{dcol}"

    # Build domain select expressions
    domain_selects = []
    vw_terms = []
    sum_terms = []
    for d_key, d_info in ELIXHAUSER_DOMAINS.items():
        cond = _build_comorbidity_condition(d_info, source_and_standard=source_and_standard)
        col_name = d_info["col"]
        domain_selects.append(f"MAX(CASE WHEN ({cond}) THEN 1 ELSE 0 END) AS {col_name}")
        w = d_info["weight"]
        vw_terms.append(f"({col_name} * {w})")
        sum_terms.append(col_name)

    domain_select_sql = ",\n        ".join(domain_selects)
    vw_sql = " + ".join(vw_terms)
    sum_sql = " + ".join(sum_terms)

    query = f"""
    WITH cohort_base AS (
        SELECT DISTINCT {pcol} AS person_id_key, {dcol} AS index_date_key
        FROM {cohort_table} c
        {filter_cohort}
    ),
    raw_flags AS (
        SELECT
            cb.person_id_key AS {pcol},
            cb.index_date_key AS {dcol},
            {domain_select_sql}
        FROM cohort_base cb
        LEFT JOIN (
            SELECT
                person_id,
                condition_concept_id,
                condition_start_date,
                UPPER(REPLACE(REPLACE(COALESCE(condition_source_value, ''), '.', ''), ' ', '')) AS _src_code
            FROM condition_occurrence
        ) co ON cb.person_id_key = co.person_id
        JOIN cohort_base c ON cb.person_id_key = c.person_id_key AND cb.index_date_key = c.index_date_key
        {window_clause}
        GROUP BY cb.person_id_key, cb.index_date_key
    )
    SELECT
        *,
        ({vw_sql}) AS elix_van_walraven_score,
        ({sum_sql}) AS elix_total_conditions
    FROM raw_flags
    ORDER BY {pcol}, {dcol};
    """
    res_df = con.execute(query).df()

    fmt = format.lower().strip()
    if fmt in ("arrow", "pyarrow"):
        import pyarrow as pa
        return pa.Table.from_pandas(res_df)
    if fmt == "polars":
        try:
            import polars as pl
            return pl.from_pandas(res_df)
        except ImportError:
            return res_df
    return res_df


def extract_charlson_index(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    person_col: str = "subject_id",
    index_date_col: str = "cohort_start_date",
    lookback_days: int | None = 365,
    source_and_standard: bool = True,
    hierarchy_adjusted: bool = True,
    format: str = "df",
) -> Any:
    """Extracts standard 17 Charlson Comorbidity Index categories (Quan et al. 2005) and weighted score.

    RFC 3.1: Calculates Quan-Charlson 17 categories within a baseline lookback window,
    applies hierarchical severity adjustments (e.g. metastatic cancer supersedes localized malignancy,
    severe liver disease supersedes mild, complicated diabetes supersedes uncomplicated),
    and computes the composite Charlson Comorbidity Index.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Active DuckDB connection.
    cohort_table : str, default 'cohort'
        Table containing the index cohort population.
    cohort_id : int, optional
        Optional cohort definition ID to filter rows if cohort_definition_id exists.
    person_col : str, default 'subject_id'
        Column name identifying the patient/person.
    index_date_col : str, default 'cohort_start_date'
        Column name identifying the index stay admission/event date.
    lookback_days : int or None, default 365
        Lookback window in days strictly prior to index date. If None, uses all prior history.
    source_and_standard : bool, default True
        Whether to evaluate both condition_source_value and standard condition_concept_id.
    hierarchy_adjusted : bool, default True
        Whether to adjust for disease severity hierarchy in Charlson weighting.
    format : str, default 'df'
        Output format: 'df' (Pandas), 'arrow', or 'polars'.

    Returns
    -------
    pd.DataFrame, pyarrow.Table, or polars.DataFrame
        Table with person_col, index_date_col, 17 cci_* binary flags,
        charlson_index, and cci_total_conditions.
    """
    tbl_cols = [r[0].lower() for r in con.execute(f"DESCRIBE {cohort_table};").fetchall()]
    pcol = person_col if person_col in tbl_cols else ("person_id" if "person_id" in tbl_cols else tbl_cols[0])
    dcol = index_date_col if index_date_col in tbl_cols else ("visit_start_date" if "visit_start_date" in tbl_cols else tbl_cols[1])

    filter_cohort = f"WHERE c.cohort_definition_id = {int(cohort_id)}" if (cohort_id is not None and "cohort_definition_id" in tbl_cols) else ""
    window_clause = f"AND co.condition_start_date BETWEEN c.{dcol} - {int(lookback_days)} AND c.{dcol} - 1" if lookback_days is not None else f"AND co.condition_start_date < c.{dcol}"

    cat_selects = []
    for c_key, c_info in CHARLSON_CATEGORIES.items():
        cond = _build_comorbidity_condition(c_info, source_and_standard=source_and_standard)
        col_name = c_info["col"]
        cat_selects.append(f"MAX(CASE WHEN ({cond}) THEN 1 ELSE 0 END) AS {col_name}")

    cat_select_sql = ",\n        ".join(cat_selects)

    # Hierarchical adjustment expressions for Charlson scoring
    if hierarchy_adjusted:
        score_expr = """
            (cci_mi * 1) +
            (cci_chf * 1) +
            (cci_pvd * 1) +
            (cci_cevd * 1) +
            (cci_dementia * 1) +
            (cci_copd * 1) +
            (cci_rheum * 1) +
            (cci_pud * 1) +
            (CASE WHEN cci_mod_severe_liver = 1 THEN 0 ELSE (cci_mild_liver * 1) END) +
            (CASE WHEN cci_dm_comp = 1 THEN 0 ELSE (cci_dm_uncomp * 1) END) +
            (cci_dm_comp * 2) +
            (cci_plegia * 2) +
            (cci_renal * 2) +
            (CASE WHEN cci_mets = 1 THEN 0 ELSE (cci_malignancy * 2) END) +
            (cci_mod_severe_liver * 3) +
            (cci_mets * 6) +
            (cci_hiv * 6)
        """
    else:
        score_expr = " + ".join(f"({c_info['col']} * {c_info['weight']})" for c_info in CHARLSON_CATEGORIES.values())

    sum_expr = " + ".join(c_info["col"] for c_info in CHARLSON_CATEGORIES.values())

    query = f"""
    WITH cohort_base AS (
        SELECT DISTINCT {pcol} AS person_id_key, {dcol} AS index_date_key
        FROM {cohort_table} c
        {filter_cohort}
    ),
    raw_flags AS (
        SELECT
            cb.person_id_key AS {pcol},
            cb.index_date_key AS {dcol},
            {cat_select_sql}
        FROM cohort_base cb
        LEFT JOIN (
            SELECT
                person_id,
                condition_concept_id,
                condition_start_date,
                UPPER(REPLACE(REPLACE(COALESCE(condition_source_value, ''), '.', ''), ' ', '')) AS _src_code
            FROM condition_occurrence
        ) co ON cb.person_id_key = co.person_id
        JOIN cohort_base c ON cb.person_id_key = c.person_id_key AND cb.index_date_key = c.index_date_key
        {window_clause}
        GROUP BY cb.person_id_key, cb.index_date_key
    )
    SELECT
        *,
        ({score_expr}) AS charlson_index,
        ({sum_expr}) AS cci_total_conditions
    FROM raw_flags
    ORDER BY {pcol}, {dcol};
    """
    res_df = con.execute(query).df()

    fmt = format.lower().strip()
    if fmt in ("arrow", "pyarrow"):
        import pyarrow as pa
        return pa.Table.from_pandas(res_df)
    if fmt == "polars":
        try:
            import polars as pl
            return pl.from_pandas(res_df)
        except ImportError:
            return res_df
    return res_df

