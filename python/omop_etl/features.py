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
