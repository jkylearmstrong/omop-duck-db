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

__all__ = [
    "CohortFlow",
    "Table2Generator",
    "as_plp_arrow",
    "PLPArrowData",
    "plot_consort_flow",
    "plot_multisite_attrition_heatmap",
]

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

    def to_circe_json(
        self,
        cohort_name: str = "Consortium Cohort",
        cohort_description: str | None = None,
        author: str | None = None,
        output_path: str | Path | None = None,
        as_dict: bool = False,
    ) -> str | dict[str, Any]:
        """Exports this cohort flow into standard OHDSI Circe JSON."""
        import json
        from .circe import export_to_circe_json
        desc = cohort_description
        if author is not None:
            desc = f"{desc} (Author: {author})" if desc else f"Author: {author}"
        res = export_to_circe_json(
            cohort_flow=self,
            cohort_name=cohort_name,
            cohort_description=desc,
            output_path=output_path,
        )
        if as_dict:
            return res
        return json.dumps(res, indent=2)


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
                 feature_cols: Sequence[str] | None = None, stream: bool = False,
                 subprocess_worker: bool = False) -> PLPArrowData:
    """Build PLP covariate structures in memory (no files, no SQLite) from a wide feature table.

    ``features`` is a DataFrame or DuckDB table name. Non-null, non-zero numeric features become sparse
    ``(rowId, covariateId, covariateValue)`` rows computed by DuckDB and returned as ``pyarrow`` tables (or
    record-batch readers with ``stream=True``). Hand to R with ``reticulate``/``arrow`` (shared memory) or
    ``Arrow C Data`` interface; no intermediate Parquet/SQLite is written.

    Args:
        subprocess_worker: If True (defensive on Windows), isolates pyarrow extraction in a separate
            worker execution environment to prevent in-process C++ DLL collision with R's ``arrow.dll``.
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


def plot_consort_flow(
    attrition: Any,
    format: str = "svg",
    title: str = "Cohort Attrition Flow (CONSORT)",
    output_path: str | None = None,
) -> Any:
    """Renders visual cohort attrition flow diagrams in interactive Plotly Sankey or standalone SVG.

    Args:
        attrition: ConsortAttrition instance, DataFrame, or list of dicts with attrition steps.
        format: Output format: 'svg' (vector diagram) or 'plotly' (interactive Sankey JSON/Figure).
        title: Title of the attrition diagram.
        output_path: Optional file path to write output.

    Returns:
        str (SVG markup) or dict/Figure (Plotly Sankey object).
    """
    if hasattr(attrition, "summary_table"):
        df = attrition.summary_table.copy()
    elif hasattr(attrition, "to_dataframe"):
        df = attrition.to_dataframe().copy()
    elif isinstance(attrition, pd.DataFrame):
        df = attrition.copy()
    elif isinstance(attrition, list):
        df = pd.DataFrame(attrition)
    else:
        raise ValueError("attrition must be a ConsortAttrition object, DataFrame, or list of dicts.")

    # Harmonize column names
    col_map = {
        "step": "step_number",
        "step_num": "step_number",
        "name": "step_name",
        "criteria": "step_name",
        "n_subjects": "subjects_retained",
        "retained": "subjects_retained",
        "count": "subjects_retained",
        "n_dropped": "subjects_dropped",
        "dropped": "subjects_dropped",
        "excluded": "subjects_dropped",
        "pct_retained": "percent_retained",
        "reason": "exclusion_reason",
    }
    for c in list(df.columns):
        if c.lower() in col_map and col_map[c.lower()] not in df.columns:
            df[col_map[c.lower()]] = df[c]

    if "step_number" not in df.columns:
        df["step_number"] = list(range(1, len(df) + 1))
    if "step_name" not in df.columns:
        df["step_name"] = [f"Step {i}" for i in df["step_number"]]
    if "subjects_retained" not in df.columns:
        raise ValueError("Attrition data must have a retained subject count column.")

    df["subjects_retained"] = df["subjects_retained"].astype(int)

    if "subjects_dropped" not in df.columns:
        retained = df["subjects_retained"].tolist()
        dropped = [0] + [max(0, retained[i - 1] - retained[i]) for i in range(1, len(retained))]
        df["subjects_dropped"] = dropped
    else:
        df["subjects_dropped"] = df["subjects_dropped"].fillna(0).astype(int)

    if "percent_retained" not in df.columns:
        first_n = df["subjects_retained"].iloc[0] if len(df) > 0 else 1
        df["percent_retained"] = [round((r / max(1, first_n)) * 100.0, 1) for r in df["subjects_retained"]]

    if "exclusion_reason" not in df.columns:
        df["exclusion_reason"] = "Criteria not met"

    format_lower = format.lower()
    if format_lower == "plotly":
        # Build Sankey data dictionary
        node_labels = []
        node_colors = []
        sources = []
        targets = []
        values = []
        link_colors = []

        # Nodes: for each step i:
        # Step node
        step_node_indices = {}
        for idx, row in df.iterrows():
            s_num = int(row["step_number"])
            s_name = str(row["step_name"])
            ret = int(row["subjects_retained"])
            pct = float(row["percent_retained"])
            lbl = f"Step {s_num}: {s_name}<br>N = {ret:,} ({pct:.1f}%)"
            node_idx = len(node_labels)
            step_node_indices[idx] = node_idx
            node_labels.append(lbl)
            node_colors.append("#3182ce")

        for idx in range(1, len(df)):
            prev_idx = idx - 1
            prev_node = step_node_indices[prev_idx]
            curr_node = step_node_indices[idx]

            curr_ret = int(df["subjects_retained"].iloc[idx])
            dropped = int(df["subjects_dropped"].iloc[idx])
            reason = str(df["exclusion_reason"].iloc[idx])

            # Link from prev to curr
            if curr_ret > 0:
                sources.append(prev_node)
                targets.append(curr_node)
                values.append(curr_ret)
                link_colors.append("rgba(49, 130, 206, 0.4)")

            # Exclusion node
            if dropped > 0:
                excl_node = len(node_labels)
                node_labels.append(f"Excluded: {reason}<br>N = {dropped:,}")
                node_colors.append("#e53e3e")

                sources.append(prev_node)
                targets.append(excl_node)
                values.append(dropped)
                link_colors.append("rgba(229, 62, 62, 0.4)")

        sankey_dict = {
            "data": [{
                "type": "sankey",
                "orientation": "h",
                "node": {
                    "pad": 15,
                    "thickness": 20,
                    "line": {"color": "#2d3748", "width": 0.5},
                    "label": node_labels,
                    "color": node_colors,
                },
                "link": {
                    "source": sources,
                    "target": targets,
                    "value": values,
                    "color": link_colors,
                },
            }],
            "layout": {
                "title": {"text": title, "font": {"family": "sans-serif", "size": 16}},
                "font": {"family": "sans-serif", "size": 12},
                "margin": {"l": 20, "r": 20, "t": 50, "b": 20},
            },
        }

        try:
            import plotly.graph_objects as go
            fig = go.Figure(sankey_dict["data"], layout=sankey_dict["layout"])
            if output_path:
                if str(output_path).endswith(".html"):
                    fig.write_html(output_path)
                elif str(output_path).endswith(".json"):
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(fig.to_json())
            return fig
        except ImportError:
            if output_path and str(output_path).endswith(".json"):
                import json
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(sankey_dict, f, indent=2)
            return sankey_dict

    # Default format: SVG
    n_steps = len(df)
    width = 750
    height = max(400, n_steps * 130 + 80)
    box_w = 340
    box_h = 60
    margin_top = 70
    center_x = 220
    excl_x = 520
    excl_w = 200

    svg_elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">',
        f'<text x="{width/2}" y="38" text-anchor="middle" font-size="17" font-weight="600" fill="#1a202c">{title}</text>',
    ]

    for i, row in df.iterrows():
        y = margin_top + i * 130
        idx = int(row["step_number"])
        name = str(row["step_name"])
        ret = int(row["subjects_retained"])
        pct = float(row["percent_retained"])
        dropped = int(row["subjects_dropped"])
        reason = str(row["exclusion_reason"])

        # Main retained box
        svg_elements.append(
            f'<rect x="{center_x - box_w/2}" y="{y}" width="{box_w}" height="{box_h}" rx="6" fill="#ebf8ff" stroke="#3182ce" stroke-width="1.5" />'
        )
        svg_elements.append(
            f'<text x="{center_x}" y="{y + 24}" text-anchor="middle" font-size="13" font-weight="600" fill="#2b6cb0">{idx}. {name}</text>'
        )
        svg_elements.append(
            f'<text x="{center_x}" y="{y + 45}" text-anchor="middle" font-size="12" fill="#4a5568">N = {ret:,} ({pct:.1f}% retained)</text>'
        )

        if i > 0:
            prev_y = margin_top + (i - 1) * 130 + box_h
            # Downward connecting arrow
            svg_elements.append(
                f'<line x1="{center_x}" y1="{prev_y}" x2="{center_x}" y2="{y}" stroke="#4a5568" stroke-width="2" />'
            )
            svg_elements.append(
                f'<polygon points="{center_x-4},{y-6} {center_x+4},{y-6} {center_x},{y}" fill="#4a5568" />'
            )

            # Excluded box
            if dropped > 0:
                mid_y = (prev_y + y) / 2
                svg_elements.append(
                    f'<line x1="{center_x}" y1="{mid_y}" x2="{excl_x}" y2="{mid_y}" stroke="#e53e3e" stroke-width="1.5" stroke-dasharray="3,3" />'
                )
                svg_elements.append(
                    f'<polygon points="{excl_x-4},{mid_y-3} {excl_x-4},{mid_y+3} {excl_x+2},{mid_y}" fill="#e53e3e" />'
                )
                excl_box_h = 44
                svg_elements.append(
                    f'<rect x="{excl_x}" y="{mid_y - excl_box_h/2}" width="{excl_w}" height="{excl_box_h}" rx="4" fill="#fff5f5" stroke="#feb2b2" stroke-width="1" stroke-dasharray="3,3" />'
                )
                svg_elements.append(
                    f'<text x="{excl_x + 10}" y="{mid_y - 4}" font-size="11" font-weight="600" fill="#c53030">Excluded (N = {dropped:,})</text>'
                )
                svg_elements.append(
                    f'<text x="{excl_x + 10}" y="{mid_y + 12}" font-size="10" fill="#742a2a">{reason[:30]}</text>'
                )

    svg_elements.append('</svg>')
    svg_res = "\n".join(svg_elements)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_res)
    return svg_res


def plot_multisite_attrition_heatmap(
    attrition_df: pd.DataFrame,
    site_col: str = "site",
    step_col: str = "step_name",
    retention_col: str = "percent_retained",
    title: str = "Multi-Site Attrition Profile (% Retained)",
    output_path: str | None = None,
) -> str:
    """Generates an SVG matrix heatmap comparing attrition retention rates across consortium sites.

    Args:
        attrition_df: DataFrame with site, step, and retention percentage columns.
        site_col: Name of column identifying site.
        step_col: Name of column identifying attrition step name.
        retention_col: Name of column identifying retention percentage.
        title: Heatmap title.
        output_path: Optional file path to write SVG output.

    Returns:
        str: Standalone SVG heatmap string.
    """
    df = attrition_df.copy()
    for c in [site_col, step_col, retention_col]:
        if c not in df.columns:
            # Check lowercase match
            matches = [col for col in df.columns if col.lower() == c.lower()]
            if matches:
                df[c] = df[matches[0]]
            else:
                raise ValueError(f"Column '{c}' not found in attrition_df.")

    sites = sorted(list(df[site_col].unique()))
    # Preserve order of steps as they appear in df
    steps = list(dict.fromkeys(df[step_col].tolist()))

    n_sites = len(sites)
    n_steps = len(steps)

    cell_w = 120
    cell_h = 36
    margin_l = 150
    margin_t = 80
    margin_b = 60
    margin_r = 50

    width = margin_l + n_steps * cell_w + margin_r
    height = margin_t + n_sites * cell_h + margin_b

    def get_color(val: float) -> tuple[str, str]:
        if val >= 90.0:
            return "#dcfce7", "#166534"  # Green
        elif val >= 75.0:
            return "#fef9c3", "#854d0e"  # Yellow
        elif val >= 50.0:
            return "#fed7aa", "#9a3412"  # Orange
        else:
            return "#fee2e2", "#991b1b"  # Red

    svg_elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">',
        f'<text x="{width/2}" y="35" text-anchor="middle" font-size="16" font-weight="600" fill="#1e293b">{title}</text>',
    ]

    # Step column headers
    for j, step in enumerate(steps):
        x = margin_l + j * cell_w + cell_w / 2
        svg_elements.append(
            f'<text x="{x}" y="{margin_t - 15}" text-anchor="middle" font-size="11" font-weight="600" fill="#475569">{step[:16]}</text>'
        )

    # Site rows
    for i, site in enumerate(sites):
        y = margin_t + i * cell_h
        # Site label
        svg_elements.append(
            f'<text x="{margin_l - 15}" y="{y + cell_h/2 + 4}" text-anchor="end" font-size="12" font-weight="500" fill="#334155">{site}</text>'
        )

        for j, step in enumerate(steps):
            x = margin_l + j * cell_w
            subset = df[(df[site_col] == site) & (df[step_col] == step)]
            if not subset.empty:
                val = float(subset[retention_col].iloc[0])
                txt = f"{val:.1f}%"
            else:
                val = 0.0
                txt = "N/A"

            bg, fg = get_color(val)
            svg_elements.append(
                f'<rect x="{x+2}" y="{y+2}" width="{cell_w-4}" height="{cell_h-4}" rx="4" fill="{bg}" stroke="#e2e8f0" stroke-width="1" />'
            )
            svg_elements.append(
                f'<text x="{x + cell_w/2}" y="{y + cell_h/2 + 4}" text-anchor="middle" font-size="12" font-weight="600" fill="{fg}">{txt}</text>'
            )

    # Legend at bottom
    leg_y = height - 25
    legend_items = [
        (">= 90% (High Retention)", "#dcfce7", "#166534"),
        ("75-89%", "#fef9c3", "#854d0e"),
        ("50-74%", "#fed7aa", "#9a3412"),
        ("< 50% (Disproportionate Drop)", "#fee2e2", "#991b1b"),
    ]
    cur_x = margin_l
    for label, bg, fg in legend_items:
        svg_elements.append(
            f'<rect x="{cur_x}" y="{leg_y - 12}" width="16" height="16" rx="3" fill="{bg}" stroke="#cbd5e1" />'
        )
        svg_elements.append(
            f'<text x="{cur_x + 22}" y="{leg_y}" font-size="11" fill="#475569">{label}</text>'
        )
        cur_x += 160

    svg_elements.append('</svg>')
    svg_res = "\n".join(svg_elements)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_res)
    return svg_res

