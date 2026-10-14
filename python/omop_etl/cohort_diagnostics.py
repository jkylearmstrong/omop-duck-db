"""OHDSI Cohort Diagnostics in Pure DuckDB.

Characterizes study cohorts across federated network sites directly in DuckDB
without R, Java, or DatabaseConnector dependencies. Generates calendar-year
incidence rates, time-to-event distributions, age/sex population pyramids,
and baseline concept prevalence with optional secondary cell suppression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence
import duckdb
import numpy as np
import pandas as pd

from .federation import audit_cell_suppression


class ConsortiumCohortDiagnostics:
    """Container for pure DuckDB OHDSI Cohort Diagnostics results."""

    def __init__(
        self,
        incidence: pd.DataFrame,
        time_to_event: pd.DataFrame,
        population_pyramid: pd.DataFrame,
        concept_prevalence: pd.DataFrame,
        metadata: dict[str, Any],
    ):
        self.incidence = incidence
        self.time_to_event = time_to_event
        self.population_pyramid = population_pyramid
        self.age_sex_pyramid = population_pyramid
        self.concept_prevalence = concept_prevalence
        self.metadata = metadata

    def plot_pyramid(
        self,
        output_path: str | Path | None = None,
        save_path: str | Path | None = None,
        title: str = "Age-Sex Population Pyramid",
        figsize: tuple[float, float] = (7.0, 5.0),
        show: bool = False,
    ) -> Any:
        """Plots a demographic age-sex population pyramid."""
        if save_path is not None:
            output_path = save_path
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        df = self.population_pyramid.copy()
        if df.empty or "age_group" not in df.columns:
            return None

        gender_col = "gender" if "gender" in df.columns else ("sex" if "sex" in df.columns else None)
        cnt_col = "count" if "count" in df.columns else ("patient_count" if "patient_count" in df.columns else None)
        if gender_col is None or cnt_col is None:
            return None

        def to_num(val):
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val).strip()
            if s.startswith("<"):
                return 5.0
            try:
                return float(s.split()[0])
            except Exception:
                return 0.0

        df["_cnt_num"] = df[cnt_col].apply(to_num)
        age_groups = sorted(df["age_group"].unique())
        male_counts = []
        female_counts = []

        for ag in age_groups:
            sub = df[df["age_group"] == ag]
            m_row = sub[sub[gender_col].astype(str).str.upper().str.startswith("M")]
            f_row = sub[sub[gender_col].astype(str).str.upper().str.startswith("F")]
            male_counts.append(float(m_row["_cnt_num"].sum()) if not m_row.empty else 0.0)
            female_counts.append(float(f_row["_cnt_num"].sum()) if not f_row.empty else 0.0)

        y_pos = np.arange(len(age_groups))
        fig, ax = plt.subplots(figsize=figsize, dpi=300)

        ax.barh(y_pos, [-m for m in male_counts], color="#2b5c8f", label="Male", edgecolor="none", height=0.7)
        ax.barh(y_pos, female_counts, color="#d95f02", label="Female", edgecolor="none", height=0.7)

        ax.set_yticks(y_pos)
        ax.set_yticklabels(age_groups, fontsize=9)
        ax.set_ylabel("Age Group (Years)", fontsize=10, fontweight="bold")
        ax.set_xlabel("Number of Patients", fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.legend(loc="upper right", frameon=True)

        ticks = ax.get_xticks()
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{abs(int(t)):,}" for t in ticks])
        ax.axvline(0, color="gray", linewidth=0.8)
        ax.grid(axis="x", linestyle=":", alpha=0.5)

        plt.tight_layout()
        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(p), dpi=300, bbox_inches="tight")
        if show:
            plt.show()
        return fig

    def summary(self) -> pd.DataFrame:
        """Returns a concise summary of diagnostic metrics."""
        n_pts = self.metadata.get("n_subjects", 0)
        n_inc_years = len(self.incidence)
        n_concepts = len(self.concept_prevalence)
        med_dur = (
            float(self.time_to_event["median_days"].iloc[0])
            if not self.time_to_event.empty and "median_days" in self.time_to_event.columns
            else 0.0
        )
        return pd.DataFrame([{
            "cohort_id": self.metadata.get("cohort_id", 1),
            "total_subjects": n_pts,
            "median_followup_days": med_dur,
            "calendar_years_observed": n_inc_years,
            "top_concepts_profiled": n_concepts,
            "secondary_suppression_applied": self.metadata.get("secondary_suppression", False),
        }])

    def incidence_table(self) -> pd.DataFrame:
        """Returns calendar-year incidence rates table."""
        return self.incidence.copy()

    def time_to_event_table(self) -> pd.DataFrame:
        """Returns time-to-event / duration summary table."""
        return self.time_to_event.copy()

    def pyramid_table(self) -> pd.DataFrame:
        """Returns age-sex population pyramid table."""
        return self.population_pyramid.copy()

    def prevalence_table(self) -> pd.DataFrame:
        """Returns top concept prevalence table."""
        return self.concept_prevalence.copy()

    def to_quarto(
        self,
        caption: str = "Cohort Diagnostics Summary",
        label: str = "tbl-diagnostics",
    ) -> str:
        """Renders cohort diagnostics tables into Quarto Markdown."""
        lines = [
            f"### {caption} {{#{label}}}",
            "",
            "#### Calendar-Year Incidence Rates",
            "",
            "| Calendar Year | New Cases | Person-Years | Incidence Rate (per 1k PY) |",
            "|:---:|:---:|:---:|:---:|",
        ]
        for _, row in self.incidence.iterrows():
            yr = row.get("calendar_year", "")
            nc = row.get("new_cases", "")
            py = row.get("person_years", "")
            ir = row.get("incidence_rate_per_1k_py", "")
            lines.append(f"| {yr} | {nc} | {py} | {ir} |")

        lines.extend([
            "",
            "#### Time-to-Event / Observation Duration Distribution",
            "",
            "| Metric | Days |",
            "|:---|:---:|",
        ])
        if not self.time_to_event.empty:
            r = self.time_to_event.iloc[0]
            for col in ["min_days", "p25_days", "median_days", "p75_days", "max_days", "mean_days", "sd_days"]:
                if col in r:
                    name = col.replace("_days", "").upper()
                    val = f"{float(r[col]):.1f}"
                    lines.append(f"| {name} | {val} |")

        lines.extend([
            "",
            "#### Age / Sex Population Distribution",
            "",
            "| Age Group | Gender | Count | Percent (%) |",
            "|:---|:---|:---:|:---:|",
        ])
        for _, row in self.population_pyramid.iterrows():
            ag = row.get("age_group", "")
            g = row.get("gender", "")
            cnt = row.get("count", "")
            pct = row.get("percent", "")
            lines.append(f"| {ag} | {g} | {cnt} | {pct} |")

        return "\n".join(lines) + "\n"

    def to_typst(self, caption: str = "Cohort Diagnostics Summary") -> str:
        """Renders cohort diagnostics into Typst table syntax."""
        lines = [
            f"== {caption}",
            "",
            "#figure(",
            "  table(",
            "    columns: (1fr, 1fr, 1.2fr, 1.5fr),",
            "    align: (center, center, center, center),",
            "    table.header([*Calendar Year*], [*New Cases*], [*Person-Years*], [*Incidence Rate (/1k PY)*]),",
        ]
        for _, row in self.incidence.iterrows():
            lines.append(
                f"    [{row.get('calendar_year', '')}], [{row.get('new_cases', '')}], "
                f"[{row.get('person_years', '')}], [{row.get('incidence_rate_per_1k_py', '')}],"
            )
        lines.append("  ),")
        lines.append(f"  caption: [{caption} - Incidence Rates],")
        lines.append(")")
        return "\n".join(lines) + "\n"


    def __repr__(self) -> str:
        return (
            f"<ConsortiumCohortDiagnostics: Cohort ID {self.metadata.get('cohort_id', 1)}, "
            f"{self.metadata.get('n_subjects', 0)} subjects across {len(self.incidence)} calendar year(s)>\n"
            f"{self.summary().to_string(index=False)}"
        )


def consortium_cohort_diagnostics(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int = 1,
    cohort_definition_id: int | None = None,
    baseline_window_days: int = 365,
    top_n_concepts: int = 10,
    secondary_suppression: bool = True,
    min_cell_count: int = 11,
) -> ConsortiumCohortDiagnostics:
    """Generates pure DuckDB OHDSI Cohort Diagnostics without R or Java dependencies.

    Directly executes vectorized DuckDB SQL queries against OMOP CDM tables to produce:
    1. Calendar-year incidence rates (new cases, person-years, rate per 1,000 PY).
    2. Time-to-event and follow-up duration distributions (p25, median, p75, IQR).
    3. Age and sex population pyramids (stratified by 10-year age bins and gender).
    4. Top baseline concept prevalence for conditions and medications.
    Automatically applies complementary secondary cell suppression (HIPAA/GDPR de-identification).

    Args:
        con: DuckDB connection with OMOP CDM and cohort tables.
        cohort_table: Name of the cohort table (default 'cohort').
        cohort_id: Target cohort definition ID (default 1).
        cohort_definition_id: Optional alias for cohort_id.
        baseline_window_days: Lookback window in days for concept prevalence (default 365).
        top_n_concepts: Number of top prevalent concepts to profile (default 10).
        secondary_suppression: Whether to apply secondary cell suppression to small counts (default True).
        min_cell_count: Cell suppression threshold (default 11).

    Returns:
        ConsortiumCohortDiagnostics: Object containing diagnostic tables and publication renderers.
    """
    if cohort_definition_id is not None:
        cohort_id = cohort_definition_id

    # 1. Check if cohort table exists
    tables_in_db = [t[0].lower() for t in con.execute("SHOW TABLES").fetchall()]
    if cohort_table.lower() not in tables_in_db:
        raise ValueError(f"Cohort table {cohort_table!r} not found in database.")

    # Inspect columns in cohort table
    cols_info = con.execute(f"PRAGMA table_info('{cohort_table}')").fetchall()
    cols = {c[1].lower(): c[1] for c in cols_info}

    subj_col = cols.get("subject_id", cols.get("person_id", "subject_id"))
    start_col = cols.get("cohort_start_date", "cohort_start_date")
    end_col = cols.get("cohort_end_date", "cohort_end_date")

    filter_cid = ""
    if "cohort_definition_id" in cols:
        filter_cid = f"WHERE cohort_definition_id = {int(cohort_id)}"

    # Check total subjects in cohort
    count_sql = f"SELECT COUNT(DISTINCT {subj_col}) FROM {cohort_table} {filter_cid}"
    n_subjects = int(con.execute(count_sql).fetchone()[0])
    if n_subjects == 0:
        raise ValueError(f"No patients found in {cohort_table!r} with cohort_id={cohort_id}.")

    # 2. Calendar-Year Incidence Rates
    # Incidence rate = new_cases / person_years * 1000
    inc_where = f"WHERE cohort_definition_id = {int(cohort_id)}" if "cohort_definition_id" in cols else ""
    inc_sql = f"""
    SELECT 
        EXTRACT(year FROM {start_col}) AS calendar_year,
        COUNT(DISTINCT {subj_col}) AS new_cases,
        ROUND(SUM(DATEDIFF('day', {start_col}, COALESCE({end_col}, {start_col} + 1))) / 365.25, 2) AS person_years
    FROM {cohort_table}
    {inc_where}
    GROUP BY EXTRACT(year FROM {start_col})
    ORDER BY calendar_year
    """
    df_inc = con.execute(inc_sql).df()
    if not df_inc.empty:
        df_inc["person_years"] = df_inc["person_years"].apply(lambda py: max(0.01, float(py)))
        df_inc["incidence_rate_per_1k_py"] = np.round(
            (df_inc["new_cases"].astype(float) / df_inc["person_years"]) * 1000.0, 2
        )
    else:
        df_inc = pd.DataFrame(columns=["calendar_year", "new_cases", "person_years", "incidence_rate_per_1k_py"])

    # 3. Time-to-Event / Duration Distributions
    dur_sql = f"""
    WITH durations AS (
        SELECT 
            DATEDIFF('day', {start_col}, COALESCE({end_col}, {start_col})) AS dur
        FROM {cohort_table}
        {inc_where}
    )
    SELECT 
        COUNT(*) AS n,
        ROUND(MIN(dur), 1) AS min_days,
        ROUND(quantile_cont(dur, 0.25), 1) AS p25_days,
        ROUND(quantile_cont(dur, 0.50), 1) AS median_days,
        ROUND(quantile_cont(dur, 0.75), 1) AS p75_days,
        ROUND(MAX(dur), 1) AS max_days,
        ROUND(AVG(dur), 1) AS mean_days,
        ROUND(COALESCE(STDDEV(dur), 0.0), 1) AS sd_days
    FROM durations
    """
    df_dur = con.execute(dur_sql).df()

    # 4. Age / Sex Population Pyramid
    has_person = "person" in tables_in_db
    if has_person:
        p_info = con.execute("PRAGMA table_info('person')").fetchall()
        p_cols = {c[1].lower(): c[1] for c in p_info}
        p_id = p_cols.get("person_id", "person_id")
        yob_col = p_cols.get("year_of_birth", "year_of_birth")
        gender_col = p_cols.get("gender_concept_id", "gender_concept_id")

        c_where = f"AND c.cohort_definition_id = {int(cohort_id)}" if "cohort_definition_id" in cols else ""
        pyramid_sql = f"""
        WITH cohort_pts AS (
            SELECT 
                c.{subj_col} AS person_id,
                EXTRACT(year FROM c.{start_col}) - p.{yob_col} AS age,
                CASE 
                    WHEN p.{gender_col} = 8507 THEN 'Male'
                    WHEN p.{gender_col} = 8532 THEN 'Female'
                    ELSE 'Other/Unknown'
                END AS gender
            FROM {cohort_table} c
            JOIN person p ON c.{subj_col} = p.{p_id}
            WHERE 1=1 {c_where}
        ),
        binned AS (
            SELECT 
                gender,
                CASE 
                    WHEN age < 10 THEN '00-09'
                    WHEN age < 20 THEN '10-19'
                    WHEN age < 30 THEN '20-29'
                    WHEN age < 40 THEN '30-39'
                    WHEN age < 50 THEN '40-49'
                    WHEN age < 60 THEN '50-59'
                    WHEN age < 70 THEN '60-69'
                    WHEN age < 80 THEN '70-79'
                    ELSE '80+'
                END AS age_group
            FROM cohort_pts
        )
        SELECT 
            age_group,
            gender,
            COUNT(*) AS count,
            ROUND(COUNT(*) * 100.0 / {n_subjects}, 1) AS percent
        FROM binned
        GROUP BY age_group, gender
        ORDER BY age_group, gender
        """
        df_pyramid = con.execute(pyramid_sql).df()
    else:
        df_pyramid = pd.DataFrame(columns=["age_group", "gender", "count", "percent"])

    # 5. Top Concept Prevalence Deltas (Conditions & Medications in Baseline Window)
    has_condition = "condition_occurrence" in tables_in_db
    has_concept = "concept" in tables_in_db

    if has_condition:
        cond_where = f"AND c.cohort_definition_id = {int(cohort_id)}" if "cohort_definition_id" in cols else ""
        concept_join = "LEFT JOIN concept cpt ON co.condition_concept_id = cpt.concept_id" if has_concept else ""
        concept_name_sel = "COALESCE(cpt.concept_name, 'Concept ' || CAST(co.condition_concept_id AS VARCHAR))" if has_concept else "'Concept ' || CAST(co.condition_concept_id AS VARCHAR)"

        cond_sql = f"""
        SELECT 
            co.condition_concept_id AS concept_id,
            {concept_name_sel} AS concept_name,
            'Condition' AS domain,
            COUNT(DISTINCT c.{subj_col}) AS count,
            ROUND(COUNT(DISTINCT c.{subj_col}) * 100.0 / {n_subjects}, 1) AS percent
        FROM {cohort_table} c
        JOIN condition_occurrence co ON c.{subj_col} = co.person_id
            AND co.condition_start_date >= c.{start_col} - INTERVAL '{int(baseline_window_days)}' DAY
            AND co.condition_start_date <= c.{start_col}
        {concept_join}
        WHERE 1=1 {cond_where}
        GROUP BY co.condition_concept_id, {concept_name_sel}
        ORDER BY count DESC
        LIMIT {int(top_n_concepts)}
        """
        try:
            df_prev = con.execute(cond_sql).df()
        except Exception:
            df_prev = pd.DataFrame(columns=["concept_id", "concept_name", "domain", "count", "percent"])
    else:
        df_prev = pd.DataFrame(columns=["concept_id", "concept_name", "domain", "count", "percent"])

    # 6. Apply secondary / complementary cell suppression if requested
    tables_to_audit = {
        "incidence": df_inc,
        "population_pyramid": df_pyramid,
        "concept_prevalence": df_prev,
    }

    if secondary_suppression:
        audit_res = audit_cell_suppression(
            tables=tables_to_audit,
            min_cell_count=min_cell_count,
            secondary_suppression=True,
            return_audit=True,
        )
        df_inc = audit_res.tables["incidence"]
        df_pyramid = audit_res.tables["population_pyramid"]
        df_prev = audit_res.tables["concept_prevalence"]

    meta = {
        "cohort_id": cohort_id,
        "cohort_table": cohort_table,
        "n_subjects": n_subjects,
        "secondary_suppression": secondary_suppression,
        "min_cell_count": min_cell_count,
        "baseline_window_days": baseline_window_days,
    }

    return ConsortiumCohortDiagnostics(
        incidence=df_inc,
        time_to_event=df_dur,
        population_pyramid=df_pyramid,
        concept_prevalence=df_prev,
        metadata=meta,
    )
