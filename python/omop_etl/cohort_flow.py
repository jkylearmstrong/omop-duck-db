"""``CohortFlow`` (computable phenotype + CONSORT), ``Table2Generator`` (completeness profiler) and
``as_plp_arrow`` (in-memory Arrow hand-off to OHDSI PatientLevelPrediction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.cohort import ConsortAttrition

__all__ = ["CohortFlow", "Table2Generator", "as_plp_arrow", "PLPArrowData"]

_ANCHORS = {"admission": "visit_start_date", "discharge": "visit_end_date"}


class CohortFlow:
    """Sequential cohort filters -> one consistent set of counts, CONSORT outputs and pre-flight checks.

    The base population is ``visit_occurrence`` restricted to ``visit_concept_ids`` (default inpatient 9201
    and emergency-to-inpatient 262). Every step is a SQL predicate over alias ``v`` (a ``visit_occurrence``
    row) and ``p`` (``person``); steps are applied cumulatively, so the attrition table, the final cohort
    and the diagram can never disagree.

    >>> flow = CohortFlow(con, anchor="admission", follow_up_days=30)
    >>> flow.add_step("Adults", "EXTRACT(year FROM v.visit_start_date) - p.year_of_birth >= 18")
    >>> flow.add_step("Alive at discharge", "v.visit_end_date IS NOT NULL")
    >>> res = flow.run()
    >>> res.attrition, res.checks, flow.to_mermaid(), flow.to_graphviz()
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, *, visit_concept_ids: Sequence[int] = (9201, 262),
                 anchor: str = "admission", follow_up_days: int | None = None, one_visit_per_person: bool = False):
        if anchor not in _ANCHORS:
            raise ValueError(f"anchor must be one of {sorted(_ANCHORS)}.")
        self.con, self.anchor, self.follow_up_days = con, anchor, follow_up_days
        self.visit_concept_ids = tuple(int(x) for x in visit_concept_ids)
        self.one_visit_per_person = one_visit_per_person
        self.steps: list[tuple[str, str]] = []
        self.cohort: pd.DataFrame | None = None
        self.attrition: pd.DataFrame | None = None
        self.checks: pd.DataFrame | None = None

    def add_step(self, name: str, predicate: str) -> "CohortFlow":
        self.steps.append((name, predicate))
        return self

    def _base_sql(self) -> str:
        ids = ", ".join(str(i) for i in self.visit_concept_ids)
        return (f"FROM visit_occurrence v JOIN person p ON p.person_id = v.person_id "
                f"WHERE v.visit_concept_id IN ({ids})")

    def run(self) -> "CohortFlow":
        con = self.con
        anchor_col = _ANCHORS[self.anchor]
        base = self._base_sql()
        counts = [("All visits", *con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT v.person_id) FROM visit_occurrence v").fetchone())]
        counts.append((f"Index visit types {self.visit_concept_ids}",
                       *con.execute(f"SELECT COUNT(*), COUNT(DISTINCT v.person_id) {base}").fetchone()))
        preds: list[str] = []
        for name, pred in self.steps:
            preds.append(f"({pred})")
            counts.append((name, *con.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT v.person_id) {base} AND " + " AND ".join(preds)).fetchone()))
        where = base + ("".join(f" AND {p}" for p in preds))
        sel = (f"SELECT v.person_id AS subject_id, v.visit_occurrence_id, v.visit_concept_id, "
               f"v.{anchor_col} AS cohort_start_date, v.visit_end_date AS cohort_end_date, "
               f"v.visit_start_date, v.visit_end_date {where}")
        if self.one_visit_per_person:
            sel = (f"SELECT * EXCLUDE rn FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY subject_id "
                   f"ORDER BY cohort_start_date, visit_occurrence_id) rn FROM ({sel})) WHERE rn = 1")
            counts.append(("One index visit per person",
                           con.execute(f"SELECT COUNT(*), COUNT(DISTINCT subject_id) FROM ({sel})").fetchone()[0],
                           con.execute(f"SELECT COUNT(DISTINCT subject_id) FROM ({sel})").fetchone()[0]))
        self.cohort = con.execute(sel + " ORDER BY subject_id, visit_occurrence_id" if not self.one_visit_per_person
                                  else sel).df()
        n0 = max(int(counts[0][1]), 1)
        recs, prev = [], None
        for i, (name, n, persons) in enumerate(counts, start=1):
            recs.append({"step_number": i, "step_name": name, "subjects_retained": int(n), "persons": int(persons),
                         "subjects_dropped": 0 if prev is None else int(prev - n),
                         "percent_retained": round(100.0 * n / n0, 1)})
            prev = n
        self.attrition = pd.DataFrame(recs)  # unit of analysis = index visits ("subjects_retained" = visits)
        self.checks = self._preflight()
        return self

    # ------------------------------------------------------------------ pre-flight
    def _preflight(self) -> pd.DataFrame:
        con, c = self.con, self.cohort
        out: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            out.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

        bad_type = int((~c["visit_concept_id"].isin(self.visit_concept_ids)).sum())
        add("visit_concept_id in " + str(self.visit_concept_ids), bad_type == 0, f"{bad_type} cohort rows outside the set")
        inpatient_only = set(self.visit_concept_ids) == {9201}
        add("visit types are inpatient (9201) or ER-to-inpatient (262)",
            set(self.visit_concept_ids) <= {9201, 262}, f"visit_concept_ids={self.visit_concept_ids}"
            + ("" if not inpatient_only else " (262 transfers excluded - intentional?)"))
        missing_end = int(c["cohort_end_date"].isna().sum())
        add("visit_end_date present", missing_end == 0, f"{missing_end} cohort rows lack visit_end_date")
        neg = int((pd.to_datetime(c["visit_end_date"]) < pd.to_datetime(c["visit_start_date"])).sum())
        add("visit_end_date >= visit_start_date", neg == 0, f"{neg} rows end before they start")
        add(f"anchor = {self.anchor} ({_ANCHORS[self.anchor]})", True,
            "readmission/follow-up windows should start at discharge" if self.anchor == "admission"
            else "labs/meds 'at admission' must not use post-discharge data")
        if self.follow_up_days is not None:
            n = len(c)
            try:
                short = con.execute(
                    "WITH c AS (SELECT * FROM cohort_df), obs AS (SELECT person_id, MAX(observation_period_end_date) e "
                    "FROM observation_period GROUP BY 1), d AS (SELECT person_id, MIN(death_date) dd FROM death GROUP BY 1) "
                    f"SELECT COUNT(*) FROM c LEFT JOIN obs ON obs.person_id=c.subject_id LEFT JOIN d ON d.person_id=c.subject_id "
                    f"WHERE obs.e IS NULL OR (obs.e < CAST(c.visit_end_date AS DATE) + {int(self.follow_up_days)} "
                    "AND (d.dd IS NULL OR d.dd > obs.e))" if self._reg() else "SELECT 0").fetchone()[0]
                add(f"{self.follow_up_days}-day follow-up verifiable", short == 0,
                    f"{short}/{n} rows censored by end of observation before day {self.follow_up_days} and not dead "
                    "(handle as censored or add an exclusion step)")
            except duckdb.Error as exc:  # observation_period / death missing
                add(f"{self.follow_up_days}-day follow-up verifiable", False, f"cannot verify: {exc}")
            finally:
                self._unreg()
        dup = int(c["subject_id"].duplicated().sum())
        add("one row per person", dup == 0, f"{dup} repeat index visits (set one_visit_per_person=True to dedupe)")
        return pd.DataFrame(out)

    def _reg(self) -> bool:
        self.con.register("cohort_df", self.cohort)
        return True

    def _unreg(self) -> None:
        try:
            self.con.unregister("cohort_df")
        except Exception:
            pass

    # ------------------------------------------------------------------ outputs
    def _need(self) -> ConsortAttrition:
        if self.attrition is None:
            raise RuntimeError("call run() first.")
        return ConsortAttrition(self.attrition)

    def to_markdown(self) -> str:
        return self._need().to_markdown()

    def to_mermaid(self) -> str:
        return self._need().to_mermaid()

    def to_latex(self) -> str:
        return self._need().to_tikz()

    def to_graphviz(self) -> str:
        """DOT source for ``dot -Tpdf``."""
        df = self._need().df
        lines = ["digraph CONSORT {", "  node [shape=box, style=rounded, fontname=Helvetica];"]
        for _, r in df.iterrows():
            i = int(r["step_number"])
            lines.append(f'  s{i} [label="{str(r["step_name"]).replace(chr(34), chr(39))}\\nN = {int(r["subjects_retained"]):,}"];')
            if i > 1:
                lines.append(f"  s{i-1} -> s{i};")
                if int(r["subjects_dropped"]) > 0:
                    lines.append(f'  x{i} [label="Excluded\\nN = {int(r["subjects_dropped"]):,}", style="dashed,rounded"];')
                    lines.append(f"  s{i-1} -> x{i} [style=dashed];")
        lines.append("}")
        return "\n".join(lines)


class Table2Generator:
    """Completeness / sentinel profiler: N, observed N, % missing, median, IQR, min, max, SD, flags."""

    def __init__(self, missing_threshold: float = 50.0, sentinel_values: Sequence[float] = (-9999, -999, 9999, 999999)):
        self.missing_threshold = missing_threshold
        self.sentinel_values = tuple(sentinel_values)

    def generate(self, data: pd.DataFrame | duckdb.DuckDBPyConnection, table_name: str | None = None,
                 columns: Sequence[str] | None = None, strata_col: str | None = None) -> pd.DataFrame:
        if isinstance(data, duckdb.DuckDBPyConnection):
            if not table_name:
                raise ValueError("table_name required with a connection.")
            data = data.execute(f"SELECT * FROM {table_name}").df()
        cols = [c for c in (columns or data.columns) if c != strata_col]
        groups = [("Overall", data)] if strata_col is None else [(str(k), g) for k, g in data.groupby(strata_col)]
        recs = []
        for gname, g in groups:
            n = len(g)
            for col in cols:
                s = g[col]
                obs = int(s.notna().sum())
                rec = {"stratum": gname, "variable": col, "total_n": n, "observed_n": obs,
                       "pct_missing": round(100.0 * (n - obs) / n, 1) if n else 0.0,
                       "median": np.nan, "q1": np.nan, "q3": np.nan, "min": np.nan, "max": np.nan, "sd": np.nan}
                flags = []
                if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s) and obs:
                    v = s.dropna().astype(float)
                    rec.update(median=v.median(), q1=v.quantile(.25), q3=v.quantile(.75), min=v.min(), max=v.max(),
                               sd=v.std() if obs > 1 else np.nan)
                    if obs > 1 and v.nunique() == 1:
                        flags.append("ZERO_SD_SENTINEL")
                    if v.isin(self.sentinel_values).mean() > 0.05:
                        flags.append("SENTINEL_VALUES")
                if rec["pct_missing"] > self.missing_threshold:
                    flags.append("HIGH_MISSINGNESS")
                if obs == 0:
                    flags.append("ALL_MISSING")
                rec["flags"] = ";".join(flags)
                recs.append(rec)
        return pd.DataFrame(recs)


@dataclass
class PLPArrowData:
    """PatientLevelPrediction-shaped Arrow tables (``rowId``, ``covariateId``, ``covariateValue`` ...)."""
    cohorts: Any
    covariates: Any
    covariate_ref: Any
    outcomes: Any | None = None


def as_plp_arrow(con: duckdb.DuckDBPyConnection, features: pd.DataFrame | str, *, id_col: str = "subject_id",
                 outcome_col: str | None = None, date_col: str | None = "cohort_start_date",
                 feature_cols: Sequence[str] | None = None, stream: bool = False) -> PLPArrowData:
    """Build PLP covariate structures in memory (no files, no SQLite) from a wide feature table.

    ``features`` is a DataFrame or DuckDB table name. Non-null, non-zero numeric features become sparse
    ``(rowId, covariateId, covariateValue)`` rows computed by DuckDB and returned as ``pyarrow`` tables (or
    record-batch readers with ``stream=True``). Hand to R with ``reticulate``/``arrow`` (shared memory) or
    ``Arrow C Data`` interface; no intermediate Parquet/SQLite is written.
    """
    import pyarrow as pa  # noqa: F401  (declared dependency of the arrow path)

    src = "_plp_src"
    if isinstance(features, pd.DataFrame):
        con.register(src, features)
        rel = src
    else:
        rel = features
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
        types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
        skip = {id_col, outcome_col, date_col, "cohort_end_date", "cohort_definition_id", "visit_occurrence_id"}
        numeric = ("INT", "DOUBLE", "FLOAT", "DECIMAL", "BOOL")
        fcols = list(feature_cols) if feature_cols else [
            c for c in cols if c not in skip and any(t in types[c].upper() for t in numeric)]
        if not fcols:
            raise ValueError("No numeric feature columns found.")
        ref_df = pd.DataFrame({"covariateId": range(1, len(fcols) + 1), "covariateName": fcols,
                               "analysisId": 1, "conceptId": 0})
        con.register("_plp_ref", ref_df)
        q = lambda c: '"' + c.replace('"', '""') + '"'
        con.execute(f"CREATE OR REPLACE TEMPORARY TABLE _plp_base AS SELECT ROW_NUMBER() OVER () AS rowId, * FROM {rel}")
        base = "_plp_base"  # in-memory temp table: one stable rowId shared by every output
        unions = " UNION ALL ".join(
            f"SELECT rowId, {i} AS covariateId, CAST({q(c)} AS DOUBLE) AS covariateValue FROM {base} "
            f"WHERE {q(c)} IS NOT NULL AND CAST({q(c)} AS DOUBLE) <> 0" for i, c in enumerate(fcols, start=1))
        sel_cohort = (f"SELECT rowId, {q(id_col)} AS subjectId"
                      + (f", {q(date_col)} AS cohortStartDate" if date_col and date_col in cols else "")
                      + (f", CAST({q(outcome_col)} AS INTEGER) AS outcomeCount" if outcome_col else "")
                      + f" FROM {base}")

        def get(sql: str):
            cur = con.execute(sql)
            return cur.fetch_record_batch() if stream else (cur.to_arrow_table() if hasattr(cur, "to_arrow_table") else cur.fetch_arrow_table())

        res = PLPArrowData(cohorts=get(sel_cohort), covariates=get(unions),
                           covariate_ref=get("SELECT * FROM _plp_ref"))
        if outcome_col:
            res.outcomes = get(f"SELECT rowId, CAST({q(outcome_col)} AS INTEGER) AS outcomeCount FROM {base}")
        return res
    finally:
        if not stream:  # streamed readers still read these lazily; the caller's connection owns them
            for r in (src, "_plp_ref"):
                try:
                    con.unregister(r)
                except Exception:
                    pass
            con.execute("DROP TABLE IF EXISTS _plp_base")
