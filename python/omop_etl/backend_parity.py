"""Checks that the BigQuery and DuckDB backends, and the R and Python packages, give the same answers.

The BigQuery demo (:mod:`omop_etl.demo_bigquery`, ``omopduckdb::run_bigquery_demo()``) builds a cohort and features in
GoogleSQL. This module runs the same steps on DuckDB and compares the results:

- :func:`run_duckdb_demo` builds the cohort with the native Circe compiler and the features with
  ``inst/sql/duckdb_demo_features.sql`` (the DuckDB twin of ``bigquery_demo_features.sql``).
- :func:`emulate_bigquery_demo` runs the *GoogleSQL* the demo sends to BigQuery on a DuckDB database instead, by
  transpiling it back with sqlglot. It needs no credentials, so the offline tests can check that the dialect translation
  keeps the cohort and the features the same.
- :func:`compare_cohorts`, :func:`compare_features`, :func:`compare_column_types`, :func:`check_id_precision` and
  :func:`compare_demo_outputs` do the comparisons. The R package has the same functions with the same rules.

Ids are compared as exact integers (as text), never as floats: a 64-bit id read into a double is only exact up to 2^53,
and an id read into a 32-bit integer is only exact up to 2^31 - 1.

    python -m omop_etl.backend_parity compare-outputs r_out/ py_out/   # exits 1 if the two demo runs disagree
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = ["FEATURE_COLUMNS", "BIGQUERY_TO_DUCKDB", "googlesql_to_duckdb", "run_duckdb_demo", "emulate_bigquery_demo",
           "compare_cohorts", "compare_features", "compare_column_types", "check_id_precision",
           "compare_demo_summaries", "compare_demo_outputs", "main"]

#: the feature columns both backends write (``subject_id`` is the key)
FEATURE_COLUMNS = ("index_date", "age_at_index", "sex", "race", "prior_ip_count", "prior_ed_count", "prior_os_count",
                   "prior_ip_os_ed_count", "readmission_count_twelve_months")

#: DuckDB types that hold a BigQuery type without loss. INT64 into INTEGER is allowed except for key ids (see
#: :func:`_is_key_id`), whose values can pass 2^31 - 1.
BIGQUERY_TO_DUCKDB = {
    "INT64": ("BIGINT", "HUGEINT", "INTEGER", "SMALLINT", "TINYINT"),
    "FLOAT64": ("DOUBLE", "FLOAT", "DECIMAL"),
    "NUMERIC": ("DECIMAL", "DOUBLE"),
    "BIGNUMERIC": ("DECIMAL", "DOUBLE"),
    "STRING": ("VARCHAR",),
    "BOOL": ("BOOLEAN",),
    "DATE": ("DATE",),
    "DATETIME": ("TIMESTAMP",),
    "TIMESTAMP": ("TIMESTAMP", "TIMESTAMP WITH TIME ZONE"),
    "BYTES": ("BLOB",),
}
_EXACT_64 = ("BIGINT", "HUGEINT")
_MAX_EXACT_DOUBLE = 2 ** 53
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_DIR = Path(__file__).resolve().parent


def _resource(*parts: str) -> Path:
    packaged = _PACKAGE_DIR / "resources" / Path(*parts)
    return packaged if packaged.exists() else _REPO_ROOT / "inst" / Path(*parts)


def _ident(x: str, what: str) -> str:
    if not _IDENT.match(x or ""):
        raise ValueError(f"{what} must be a plain identifier, got {x!r}")
    return x


def _is_key_id(column: str) -> bool:
    """A column that holds a row id (person_id, visit_occurrence_id, event_id, ...) and must stay 64-bit. Concept ids and
    cohort definition ids are small by construction and may be 32-bit."""
    c = column.lower()
    return c.endswith("_id") and not c.endswith("concept_id") and not c.endswith("definition_id")


# ---------------------------------------------------------------------------------------------------------------------
# running the demo on DuckDB


def googlesql_to_duckdb(sql: str, schemas: Mapping[str, str]) -> str:
    """Transpile GoogleSQL to DuckDB and point ``project.dataset`` references at local schemas.

    ``schemas`` maps each ``"project.dataset"`` the SQL reads or writes to a DuckDB schema name. A reference to a
    dataset that is not in the map is an error, so nothing silently reads the wrong table.
    """
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="bigquery")
    for table in tree.find_all(exp.Table):
        if not table.args.get("db"):
            continue  # a CTE or an unqualified name
        dataset = f"{table.catalog}.{table.db}" if table.catalog else table.db
        if dataset not in schemas:
            raise KeyError(f"no DuckDB schema given for BigQuery dataset {dataset!r}")
        table.set("catalog", None)
        table.set("db", exp.to_identifier(schemas[dataset]))
    return tree.sql(dialect="duckdb")


def _render(sql: str, **values: Any) -> str:
    from omop_etl.demo_bigquery import render_template
    return render_template(sql, **values)


def _default_cohort_json() -> Path:
    return _resource("cohorts", "diabetes_phenotype.circe.json")


def run_duckdb_demo(con: Any, cdm_schema: str = "main", cohort_json: str | Path | dict | None = None,
                    cohort_id: int = 1, cohort_table: str = "demo_cohort",
                    features_table: str = "demo_features") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the demo cohort and features on DuckDB; returns ``(cohort, features)``.

    The cohort comes from the native Circe compiler, the features from ``inst/sql/duckdb_demo_features.sql``. Both
    tables are written to the connection's current schema.
    """
    from omop_etl.circe import compile_circe_to_duckdb

    _ident(cdm_schema, "cdm_schema"), _ident(cohort_table, "cohort_table"), _ident(features_table, "features_table")
    sql = compile_circe_to_duckdb(cohort_json or _default_cohort_json(), target_cohort_id=int(cohort_id), cdm_schema=cdm_schema)
    con.execute(f"CREATE OR REPLACE TABLE {cohort_table} (cohort_definition_id BIGINT, subject_id BIGINT, "
                "cohort_start_date DATE, cohort_end_date DATE)")
    con.execute(f"INSERT INTO {cohort_table} {sql}")
    template = _resource("sql", "duckdb_demo_features.sql").read_text(encoding="utf-8")
    con.execute(_render(template, cdm=cdm_schema, cohort=cohort_table, features_table=features_table, cohort_id=int(cohort_id)))
    cohort = con.execute(f"SELECT * FROM {cohort_table} ORDER BY subject_id, cohort_start_date").df()
    features = con.execute(f"SELECT * FROM {features_table} ORDER BY subject_id").df()
    return cohort, features


def emulate_bigquery_demo(con: Any, cdm_schema: str = "main", cohort_json: str | Path | dict | None = None,
                          cohort_id: int = 1, scratch_schema: str = "bq_scratch") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the GoogleSQL the BigQuery demo sends (cohort and features) on DuckDB; returns ``(cohort, features)``.

    The statements are exactly those :func:`omop_etl.demo_bigquery.run_demo` executes, transpiled back with sqlglot, so a
    difference from :func:`run_duckdb_demo` is a dialect-translation bug. Tables are written to ``scratch_schema``. An
    optional table missing from ``cdm_schema`` (``visit_occurrence``, ``measurement``) gets the same empty stand-in the
    demo creates on BigQuery.
    """
    from omop_etl.demo_bigquery import OPTIONAL_TABLES, compile_cohort_bigquery, stub_table_sql, use_stub_tables

    _ident(cdm_schema, "cdm_schema"), _ident(scratch_schema, "scratch_schema")
    cdm, scratch = "emulated-project.cdm", "emulated-project.scratch"
    schemas = {cdm: cdm_schema, scratch: scratch_schema}
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {scratch_schema}")
    present = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
                                         [cdm_schema]).fetchall()}
    stubs = [t for t in OPTIONAL_TABLES if t not in present]  # as run_demo does for a source without them
    for t in stubs:
        con.execute(googlesql_to_duckdb(stub_table_sql(scratch, t), schemas))
    cohort_sql = compile_cohort_bigquery(cohort_json or _default_cohort_json(), cdm, scratch, "demo_cohort", int(cohort_id))
    con.execute(googlesql_to_duckdb(use_stub_tables(cohort_sql, cdm, scratch, stubs), schemas))
    template = _resource("sql", "bigquery_demo_features.sql").read_text(encoding="utf-8")
    features_sql = _render(template, cdm=cdm, scratch=scratch, cohort_table="demo_cohort", features_table="demo_features",
                           cohort_id=int(cohort_id))
    con.execute(googlesql_to_duckdb(use_stub_tables(features_sql, cdm, scratch, stubs), schemas))
    cohort = con.execute(f"SELECT * FROM {scratch_schema}.demo_cohort ORDER BY subject_id, cohort_start_date").df()
    features = con.execute(f"SELECT * FROM {scratch_schema}.demo_features ORDER BY subject_id").df()
    return cohort, features


# ---------------------------------------------------------------------------------------------------------------------
# comparisons


def _id_text(values: Iterable[Any], what: str = "id") -> list[str]:
    """Ids as exact decimal text. A float above 2^53 may already have lost digits, so it is refused, not compared."""
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
            out.append("")
        elif isinstance(v, float):
            if abs(v) >= _MAX_EXACT_DOUBLE:  # 2^53 + 1 rounds to 2^53, so 2^53 itself is already ambiguous
                raise ValueError(f"{what} {v!r} was read as a float at or above 2^53 and may be truncated; read ids as 64-bit "
                                 "integers or text")
            out.append(str(int(v)))
        else:
            out.append(str(int(v)) if not isinstance(v, str) else v.strip())
    return out


def _date_text(values: Iterable[Any]) -> list[str]:
    return ["" if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT or v is pd.NA else str(v)[:10]
            for v in values]


def compare_cohorts(left: pd.DataFrame, right: pd.DataFrame, examples: int = 0) -> dict:
    """Compare two cohort tables (``subject_id``, ``cohort_start_date``, ``cohort_end_date``).

    Returns counts of subjects and of (subject, start, end) rows on each side and in neither's complement; ``match`` is
    true when the row sets are equal. ``examples`` > 0 adds up to that many ids from each side's extras (keep 0 for
    data that are not synthetic).
    """
    def rows(df: pd.DataFrame) -> set[tuple[str, str, str]]:
        return set(zip(_id_text(df["subject_id"], "subject_id"), _date_text(df["cohort_start_date"]),
                       _date_text(df["cohort_end_date"])))

    a, b = rows(left), rows(right)
    sa, sb = {r[0] for r in a}, {r[0] for r in b}
    out = {"n_left_rows": len(a), "n_right_rows": len(b), "n_left_subjects": len(sa), "n_right_subjects": len(sb),
           "n_subjects_both": len(sa & sb), "n_subjects_only_left": len(sa - sb), "n_subjects_only_right": len(sb - sa),
           "n_rows_only_left": len(a - b), "n_rows_only_right": len(b - a),
           "same_subjects": sa == sb, "match": a == b}
    if examples:
        out["examples_only_left"] = sorted(sa - sb)[:examples]
        out["examples_only_right"] = sorted(sb - sa)[:examples]
    return out


def compare_features(left: pd.DataFrame, right: pd.DataFrame, key: str = "subject_id",
                     columns: Sequence[str] | None = None, tolerance: float = 0.0) -> dict:
    """Compare two feature tables row by row on ``key``.

    Numbers agree within ``tolerance``, dates on the day, text exactly; a value missing on both sides agrees. Returns
    the keys found on one side only and, per column, how many matched rows disagree. ``match`` is true when the keys
    are the same and no column disagrees.
    """
    columns = list(columns or [c for c in FEATURE_COLUMNS if c in left.columns or c in right.columns])
    lk, rk = _id_text(left[key], key), _id_text(right[key], key)
    if len(set(lk)) != len(lk) or len(set(rk)) != len(rk):
        raise ValueError(f"{key} is not unique in the feature tables")
    li, ri = dict(zip(lk, range(len(lk)))), dict(zip(rk, range(len(rk))))
    both = sorted(set(li) & set(ri))
    per_column = {}
    for col in columns:
        if col not in left.columns or col not in right.columns:
            per_column[col] = {"n_compared": 0, "n_mismatch": None, "missing_in": "left" if col not in left.columns else "right"}
            continue
        lv = [left[col].iloc[li[k]] for k in both]
        rv = [right[col].iloc[ri[k]] for k in both]
        bad = sum(not _same_value(x, y, tolerance) for x, y in zip(lv, rv))
        per_column[col] = {"n_compared": len(both), "n_mismatch": bad}
    ok = (len(both) == len(li) == len(ri)
          and all(v["n_mismatch"] == 0 for v in per_column.values()))
    return {"n_left": len(li), "n_right": len(ri), "n_both": len(both), "n_only_left": len(li) - len(both),
            "n_only_right": len(ri) - len(both), "columns": per_column, "match": ok}


def _missing(v: Any) -> bool:
    try:
        return v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and math.isnan(v)) or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _same_value(x: Any, y: Any, tolerance: float) -> bool:
    if _missing(x) or _missing(y):
        return _missing(x) and _missing(y)
    if hasattr(x, "year") or hasattr(y, "year"):
        return str(x)[:10] == str(y)[:10]
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return str(x) == str(y)
    if isinstance(x, str) and isinstance(y, str):
        return x == y
    return abs(fx - fy) <= tolerance


def _base_type(t: str) -> str:
    t = str(t).upper().strip()
    t = re.sub(r"\(.*\)$", "", t).strip()
    return {"INT": "INTEGER", "INT4": "INTEGER", "INT8": "BIGINT", "LONG": "BIGINT", "REAL": "FLOAT", "TEXT": "VARCHAR",
            "STRING": "VARCHAR", "BOOL": "BOOLEAN", "NUMERIC": "DECIMAL", "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
            "TIMESTAMP_S": "TIMESTAMP", "TIMESTAMP_MS": "TIMESTAMP", "TIMESTAMP_NS": "TIMESTAMP"}.get(t, t)


def compare_column_types(bigquery: pd.DataFrame, duckdb: pd.DataFrame) -> pd.DataFrame:
    """Compare column types: BigQuery ``INFORMATION_SCHEMA.COLUMNS`` against DuckDB ``information_schema.columns``.

    Both frames need ``table_name``, ``column_name`` and ``data_type``. Each row gets a ``status``: ``ok``;
    ``id_narrowed`` (a key id that is INT64 in BigQuery but not BIGINT in DuckDB, so ids above 2^31 - 1 or 2^53 break);
    ``type_mismatch``; or ``only_bigquery`` / ``only_duckdb`` (the CDM versions differ; informational). Only
    ``id_narrowed`` and ``type_mismatch`` are failures.
    """
    def norm(df: pd.DataFrame, side: str) -> pd.DataFrame:
        d = df[["table_name", "column_name", "data_type"]].copy()
        d["table_name"] = d["table_name"].str.lower()
        d["column_name"] = d["column_name"].str.lower()
        return d.rename(columns={"data_type": f"{side}_type"})

    m = norm(bigquery, "bigquery").merge(norm(duckdb, "duckdb"), on=["table_name", "column_name"], how="outer")

    def status(r) -> str:
        if pd.isna(r.duckdb_type):
            return "only_bigquery"
        if pd.isna(r.bigquery_type):
            return "only_duckdb"
        bq, dk = str(r.bigquery_type).upper().strip(), _base_type(r.duckdb_type)
        if bq == "INT64" and _is_key_id(r.column_name) and dk not in _EXACT_64:
            return "id_narrowed"
        allowed = BIGQUERY_TO_DUCKDB.get(re.sub(r"\(.*\)$", "", bq).strip())
        if allowed is None:
            return "type_mismatch"
        return "ok" if dk in allowed else "type_mismatch"

    m["status"] = m.apply(status, axis=1) if len(m) else pd.Series(dtype=str)
    return m.sort_values(["table_name", "column_name"]).reset_index(drop=True)


def check_id_precision(df: pd.DataFrame, columns: Sequence[str] | None = None) -> list[dict]:
    """Id columns in a downloaded or queried frame that cannot hold every 64-bit id exactly.

    By default every key id column (a name ending in ``_id``, except concept and cohort definition ids). A 64-bit integer column (``int64``, ``Int64``, ``uint64``) or
    text is fine; a float is exact only up to 2^53 and a 32-bit integer only up to 2^31 - 1. Returns one dict per
    problem (empty when all is well).
    """
    cols = list(columns) if columns is not None else [c for c in df.columns if _is_key_id(str(c))]
    problems = []
    for c in cols:
        dt = df[c].dtype
        kind = getattr(dt, "kind", "O")
        name = str(dt)
        if kind in "iu" and getattr(dt, "itemsize", 8) >= 8:
            continue
        if kind == "O" or name in ("string", "str") or name.startswith("string"):
            continue
        reason = ("float: exact only up to 2^53" if kind == "f"
                  else f"{name}: holds ids only up to 2^31 - 1" if kind in "iu" else f"{name}: not an integer type")
        problems.append({"column": c, "dtype": name, "problem": reason})
    return problems


# ---------------------------------------------------------------------------------------------------------------------
# R vs Python demo outputs

_SUMMARY_KEYS = ("n_cohort", "age_at_index", "sex", "race", "utilization", "clinical_telemetry",
                 "numeric_measurement_values_in_cohort")


def _diff(a: Any, b: Any, path: str, tol: float, out: list[str]) -> None:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in {'left' if k in a else 'right'}")
            else:
                _diff(a[k], b[k], f"{path}.{k}", tol, out)
        return
    if a is None or b is None:
        if not (a is None and b is None):
            out.append(f"{path}: {a!r} vs {b!r}")
        return
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
        if a != b:
            out.append(f"{path}: {a!r} vs {b!r}")
        return
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        exact = path.endswith(".n") or path.endswith("n_cohort") or "n_numeric" in path or path.endswith("_in_cohort")
        if (a != b) if exact else abs(float(a) - float(b)) > tol:
            out.append(f"{path}: {a!r} vs {b!r}")
        return
    if a != b:
        out.append(f"{path}: {a!r} vs {b!r}")


def compare_demo_summaries(left: Mapping | str | Path, right: Mapping | str | Path, tolerance: float = 0.051) -> list[str]:
    """Differences between two ``demo_summary.json`` results (R and Python, or two runs); empty when they agree.

    Counts must be equal; means, SDs, medians and percentages may differ by ``tolerance`` (each language rounds to one
    or two decimals). The run block (billing project, timings) and the disclaimer are not compared.
    """
    def load(x):
        return json.loads(Path(x).read_text(encoding="utf-8")) if isinstance(x, (str, Path)) else x

    a, b = load(left), load(right)
    out: list[str] = []
    for k in _SUMMARY_KEYS:
        if k not in a or k not in b:
            out.append(f"{k}: only in {'left' if k in a else 'right'}" if (k in a or k in b) else f"{k}: in neither")
            continue
        _diff(a[k], b[k], k, tolerance, out)
    return out


def compare_demo_outputs(left_dir: str | Path, right_dir: str | Path, tolerance: float = 0.051) -> dict:
    """Compare two demo output folders: ``demo_summary.json`` and, when both have it, ``demo_features.csv``.

    ``match`` is true when the summaries agree and the feature rows are the same person for person.
    """
    left_dir, right_dir = Path(left_dir), Path(right_dir)
    out: dict[str, Any] = {"summary_differences": compare_demo_summaries(left_dir / "demo_summary.json",
                                                                         right_dir / "demo_summary.json", tolerance)}
    lf, rf = left_dir / "demo_features.csv", right_dir / "demo_features.csv"
    if lf.exists() and rf.exists():
        read = lambda p: pd.read_csv(p, dtype={"subject_id": str}, keep_default_na=True)  # noqa: E731 - ids stay text
        out["features"] = compare_features(read(lf), read(rf))
    out["match"] = not out["summary_differences"] and out.get("features", {"match": True})["match"]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare two BigQuery demo output folders (for example R and Python).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare-outputs", help="compare demo_summary.json and demo_features.csv of two runs")
    c.add_argument("left")
    c.add_argument("right")
    c.add_argument("--tolerance", type=float, default=0.051)
    a = ap.parse_args(argv)
    res = compare_demo_outputs(a.left, a.right, a.tolerance)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
