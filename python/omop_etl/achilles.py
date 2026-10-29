"""In-database OHDSI Achilles characterization, vectorized in DuckDB SQL (no R, no Java).

``run_achilles`` writes the standard ``achilles_results`` (analysis_id, stratum_1..5, count_value) and
``achilles_results_dist`` (adds min/max/avg/stdev/median/p10/p25/p75/p90) tables plus ``achilles_analysis`` metadata, using
one set-based statement per analysis. Analyses whose CDM tables or columns are missing are skipped and reported.

Coverage (``ACHILLES_ANALYSES``): person demographics (0-5), observation periods (101, 103, 105, 108, 111-113) and,
for the visit / condition / procedure / drug / observation / measurement / device domains, persons-by-concept (x00),
records-by-concept (x01) and persons-by-concept-and-month (x02), plus death by cause (500, 501) and drug / condition
eras (900, 901, 1000, 1001). Result tables use the same shape as the R package, so ``AchillesWeb`` / DQD
tooling can read them, but this is a subset of Achilles' ~170 analyses, not a replacement for the full catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import duckdb
import pandas as pd

__all__ = ["ACHILLES_ANALYSES", "AchillesAnalysis", "run_achilles", "achilles_analysis_catalog"]


@dataclass(frozen=True)
class AchillesAnalysis:
    analysis_id: int
    name: str
    tables: tuple[tuple[str, tuple[str, ...]], ...]   # (table, required columns)
    select: str                                         # FROM ... body yielding stratum_1..5 + count_value (+ dist cols)
    dist: bool = False


def _month(col: str) -> str:
    return f"CAST(year({col}) * 100 + month({col}) AS VARCHAR)"


def _count(aid, name, tables, body):
    return AchillesAnalysis(aid, name, tuple(tables), body)


def _dist(aid, name, tables, body):
    return AchillesAnalysis(aid, name, tuple(tables), body, dist=True)


def _stats(expr: str) -> str:
    """Distribution columns as the official Achilles computes them: sample standard deviation (NULL for one value) and the
    discrete percentile (the smallest value whose cumulative count reaches q * n), not an interpolated one."""
    return (f"count(*) AS count_value, min({expr}) AS min_value, max({expr}) AS max_value, avg({expr}) AS avg_value, "
            f"stddev_samp({expr}) AS stdev_value, quantile_disc({expr}, 0.50) AS median_value, "
            f"quantile_disc({expr}, 0.10) AS p10_value, quantile_disc({expr}, 0.25) AS p25_value, "
            f"quantile_disc({expr}, 0.75) AS p75_value, quantile_disc({expr}, 0.90) AS p90_value")


_FIRST_OP = ("(SELECT person_id, observation_period_start_date AS s, observation_period_end_date AS e, "
             "row_number() OVER (PARTITION BY person_id ORDER BY observation_period_start_date, observation_period_id) AS rn "
             "FROM observation_period)")
_DAYS = "date_diff('day', f.s, f.e)"
_OP_COLS = ("person_id", "observation_period_id", "observation_period_start_date", "observation_period_end_date")


def _build() -> dict[int, AchillesAnalysis]:
    a: list[AchillesAnalysis] = []
    a.append(_count(0, "Source name", [("person", ("person_id",))],
                    "SELECT coalesce((SELECT cdm_source_name FROM cdm_source LIMIT 1), 'NA') AS stratum_1, NULL AS stratum_2, "
                    "NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, count(*) AS count_value FROM person"))
    a.append(_count(1, "Number of persons", [("person", ("person_id",))],
                    "SELECT NULL AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                    "count(DISTINCT person_id) AS count_value FROM person"))
    for aid, nm, col in ((2, "Number of persons by gender", "gender_concept_id"), (3, "Number of persons by year of birth", "year_of_birth"),
                         (4, "Number of persons by race", "race_concept_id"), (5, "Number of persons by ethnicity", "ethnicity_concept_id")):
        a.append(_count(aid, nm, [("person", ("person_id", col))],
                        f"SELECT CAST({col} AS VARCHAR) AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, "
                        f"NULL AS stratum_5, count(DISTINCT person_id) AS count_value FROM person GROUP BY 1"))
    ageq = (f"FROM {_FIRST_OP} f JOIN person p ON p.person_id = f.person_id WHERE f.rn = 1")
    a.append(_count(101, "Number of persons by age at first observation period", [("person", ("person_id", "year_of_birth")), ("observation_period", _OP_COLS)],
                    f"SELECT CAST(year(f.s) - p.year_of_birth AS VARCHAR) AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, "
                    f"NULL AS stratum_5, count(DISTINCT f.person_id) AS count_value {ageq} GROUP BY 1"))
    a.append(_dist(103, "Distribution of age at first observation period", [("person", ("person_id", "year_of_birth")), ("observation_period", _OP_COLS)],
                   f"SELECT NULL AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                   f"{_stats('year(f.s) - p.year_of_birth')} {ageq}"))
    a.append(_dist(105, "Length of observation (days) of first observation period", [("observation_period", _OP_COLS)],
                   f"SELECT NULL AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                   f"{_stats(_DAYS)} FROM {_FIRST_OP} f WHERE f.rn = 1"))
    a.append(_count(108, "Number of persons by length of observation period, in 30d increments", [("observation_period", _OP_COLS)],
                    "SELECT CAST(CAST(floor(date_diff('day', f.s, f.e) / 30) AS BIGINT) AS VARCHAR) AS stratum_1, NULL AS stratum_2, "
                    f"NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, count(DISTINCT f.person_id) AS count_value "
                    f"FROM {_FIRST_OP} f WHERE f.rn = 1 GROUP BY 1"))
    a.append(_count(111, "Number of persons by observation period start month", [("observation_period", _OP_COLS)],
                    f"SELECT {_month('observation_period_start_date')} AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, "
                    "NULL AS stratum_5, count(DISTINCT person_id) AS count_value FROM observation_period GROUP BY 1"))
    a.append(_count(112, "Number of persons by observation period end month", [("observation_period", _OP_COLS)],
                    f"SELECT {_month('observation_period_end_date')} AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, "
                    "NULL AS stratum_5, count(DISTINCT person_id) AS count_value FROM observation_period GROUP BY 1"))
    a.append(_count(113, "Number of persons by number of observation periods", [("observation_period", _OP_COLS)],
                    "SELECT CAST(n AS VARCHAR) AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                    "count(*) AS count_value FROM (SELECT person_id, count(*) AS n FROM observation_period GROUP BY person_id) GROUP BY 1"))

    domains = ((200, "visit_occurrence", "visit_concept_id", "visit_start_date", "visit"),
               (400, "condition_occurrence", "condition_concept_id", "condition_start_date", "condition occurrence"),
               (600, "procedure_occurrence", "procedure_concept_id", "procedure_date", "procedure"),
               (700, "drug_exposure", "drug_concept_id", "drug_exposure_start_date", "drug exposure"),
               (800, "observation", "observation_concept_id", "observation_date", "observation"),
               (1800, "measurement", "measurement_concept_id", "measurement_date", "measurement"),
               (2100, "device_exposure", "device_concept_id", "device_exposure_start_date", "device exposure"))
    for base, tbl, cc, dc, label in domains:
        req = [(tbl, ("person_id", cc))]
        blank = "NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5"
        a.append(_count(base, f"Number of persons with at least one {label}, by concept_id", req,
                        f"SELECT CAST({cc} AS VARCHAR) AS stratum_1, {blank}, count(DISTINCT person_id) AS count_value FROM {tbl} GROUP BY 1"))
        a.append(_count(base + 1, f"Number of {label} records, by concept_id", req,
                        f"SELECT CAST({cc} AS VARCHAR) AS stratum_1, {blank}, count(*) AS count_value FROM {tbl} GROUP BY 1"))
        a.append(_count(base + 2, f"Number of persons with at least one {label}, by concept_id and start month", [(tbl, ("person_id", cc, dc))],
                        f"SELECT CAST({cc} AS VARCHAR) AS stratum_1, {_month(dc)} AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, "
                        f"NULL AS stratum_5, count(DISTINCT person_id) AS count_value FROM {tbl} GROUP BY 1, 2"))
    for aid, tbl, cc, label in ((500, "death", "cause_concept_id", "death, by cause"), (900, "drug_era", "drug_concept_id", "drug era, by concept_id"),
                                (1000, "condition_era", "condition_concept_id", "condition era, by concept_id")):
        a.append(_count(aid, f"Number of persons with {label}", [(tbl, ("person_id", cc))],
                        f"SELECT CAST({cc} AS VARCHAR) AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                        f"count(DISTINCT person_id) AS count_value FROM {tbl} GROUP BY 1"))
        a.append(_count(aid + 1, f"Number of records of {label}", [(tbl, ("person_id", cc))],
                        f"SELECT CAST({cc} AS VARCHAR) AS stratum_1, NULL AS stratum_2, NULL AS stratum_3, NULL AS stratum_4, NULL AS stratum_5, "
                        f"count(*) AS count_value FROM {tbl} GROUP BY 1"))
    return {x.analysis_id: x for x in a}


ACHILLES_ANALYSES: dict[int, AchillesAnalysis] = _build()


def achilles_analysis_catalog() -> pd.DataFrame:
    """The implemented analyses as ``analysis_id, analysis_name, is_distribution`` (the shape of ``achilles_analysis``)."""
    return pd.DataFrame([{"analysis_id": x.analysis_id, "analysis_name": x.name, "is_distribution": int(x.dist)}
                         for x in ACHILLES_ANALYSES.values()])


def _columns(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    # Only the schema unqualified names resolve to: a same-named table in another schema or attached catalog (for
    # example central_vocab) must not hide a missing CDM table or column.
    rows = con.execute("SELECT lower(table_name), lower(column_name) FROM information_schema.columns "
                       "WHERE table_catalog = current_database() AND table_schema = current_schema()").fetchall()
    out: dict[str, set[str]] = {}
    for t, c in rows:
        out.setdefault(t, set()).add(c)
    return out


def run_achilles(con: duckdb.DuckDBPyConnection, analysis_ids: Sequence[int] | None = None, *,
                 results_table: str = "achilles_results", dist_table: str = "achilles_results_dist",
                 analysis_table: str = "achilles_analysis", min_cell_count: int = 0,
                 drop_existing: bool = True) -> dict[str, Any]:
    """Runs the Achilles analyses on a DuckDB OMOP CDM and stores the results in DuckDB tables.

    ``min_cell_count`` > 0 drops result rows with ``count_value`` below it (Achilles' small-cell guard; ``0`` keeps
    all rows). This differs from the R package, which masks small cells instead of removing them; rows for groups
    smaller than the threshold (including their min / max / median in the distribution table) are simply absent here.
    Re-running an analysis replaces its earlier rows. Returns ``{"run": [ids], "skipped": {id: reason}, "n_rows": int, "n_dist_rows": int}``.
    """
    for name in (results_table, dist_table, analysis_table):
        if not name.replace("_", "").isalnum():
            raise ValueError(f"invalid table name {name!r}")
    wanted = list(ACHILLES_ANALYSES) if analysis_ids is None else [int(i) for i in analysis_ids]
    unknown = [i for i in wanted if i not in ACHILLES_ANALYSES]
    if unknown:
        raise ValueError(f"unknown analysis ids {unknown}; see achilles_analysis_catalog()")
    if drop_existing:
        for t in (results_table, dist_table, analysis_table):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.execute(f"CREATE TABLE IF NOT EXISTS {results_table} (analysis_id INTEGER, stratum_1 VARCHAR, stratum_2 VARCHAR, "
                "stratum_3 VARCHAR, stratum_4 VARCHAR, stratum_5 VARCHAR, count_value BIGINT)")
    con.execute(f"CREATE TABLE IF NOT EXISTS {dist_table} (analysis_id INTEGER, stratum_1 VARCHAR, stratum_2 VARCHAR, "
                "stratum_3 VARCHAR, stratum_4 VARCHAR, stratum_5 VARCHAR, count_value BIGINT, min_value DOUBLE, max_value DOUBLE, "
                "avg_value DOUBLE, stdev_value DOUBLE, median_value DOUBLE, p10_value DOUBLE, p25_value DOUBLE, p75_value DOUBLE, "
                "p90_value DOUBLE)")
    con.execute(f"CREATE TABLE IF NOT EXISTS {analysis_table} (analysis_id INTEGER, analysis_name VARCHAR, is_distribution INTEGER)")
    have = _columns(con)
    ran: list[int] = []
    skipped: dict[int, str] = {}
    cut = f" WHERE count_value >= {int(min_cell_count)}" if min_cell_count and min_cell_count > 0 else ""
    for aid in wanted:
        an = ACHILLES_ANALYSES[aid]
        missing = [f"{t}.{c}" if t in have else t for t, cols in an.tables for c in (cols if t in have else ("*",))
                   if t not in have or c not in have[t]]
        if missing:
            skipped[aid] = "missing " + ", ".join(sorted(set(missing)))
            continue
        target = dist_table if an.dist else results_table
        con.execute(f"DELETE FROM {target} WHERE analysis_id = {aid}")   # re-runs replace, never duplicate, an analysis
        con.execute(f"INSERT INTO {target} BY NAME SELECT {aid} AS analysis_id, * FROM ({an.select}){cut}")
        con.execute(f"DELETE FROM {analysis_table} WHERE analysis_id = {aid}")
        con.execute(f"INSERT INTO {analysis_table} VALUES ({aid}, ?, {int(an.dist)})", [an.name])
        ran.append(aid)
    return {"run": ran, "skipped": skipped,
            "n_rows": con.execute(f"SELECT count(*) FROM {results_table}").fetchone()[0],
            "n_dist_rows": con.execute(f"SELECT count(*) FROM {dist_table}").fetchone()[0]}
