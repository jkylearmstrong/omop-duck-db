"""DuckDB backend for omop-learn (https://github.com/clinicalml/omop-learn).

omop-learn ships ``PostgresBackend``, ``BigQueryBackend`` and ``SparkBackend`` behind one interface,
``omop_learn.backends.backend.OMOPDatasetBackend``. This module adds the DuckDB implementation, so the
whole omop-learn stack (``Cohort``, ``Feature``, ``OMOPDataset`` -> ``to_torch`` / ``to_sparse`` /
``to_windowed`` / ``to_hf``, and its transformer models) runs unchanged against an omop-duck-db CDM::

    from omop_learn.omop import OMOPDataset
    from omop_learn.utils.config import Config
    from omop_etl.omop_learn_backend import DuckDBBackend, default_features, cohort_from_parquet

    backend = DuckDBBackend(con=con)                       # or DuckDBBackend(Config({"path": "site.duckdb"}))
    cohort = cohort_from_parquet(con, "ederri_features_Temple.parquet")
    dataset = OMOPDataset(name="temple", config=Config({"cdm_schema": "main"}), cohort=cohort,
                          features=default_features(), backend=backend, data_dir=Path("datasets"))

What differs from the Postgres backend:

* **One set-based query per feature instead of one query per patient.** omop-learn's feature SQL is
  written per person (``where person_id = {person_id}``). Here ``{person_id}`` / ``{end_date}`` become
  references to the cohort row and the SQL runs as ``cohort, LATERAL (feature_sql)``, which DuckDB
  decorrelates into a hash join, with identical semantics. ``strategy="per_person"`` runs the original
  per-patient loop (the reference implementation, and a fallback for SQL the rewrite cannot handle).
* **No SQLAlchemy, no multiprocessing.** DuckDB is in-process and parallelises internally, so there is no
  global engine / worker pool (which cannot work with spawn-based multiprocessing, i.e. on Windows).
* **DuckDB-dialect feature SQL** lives in ``inst/sql/omop_learn`` (see ``default_features``), the same way
  omop-learn keeps ``postgres_sql/``, ``bigquery_sql/`` and ``spark_sql/`` side by side.

omop-learn is an optional dependency: this module imports without it (so ``DuckDBBackend`` can be
unit-tested in isolation) but ``default_features`` / ``cohort_from_parquet`` need it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

import duckdb
import pandas as pd

from omop_etl.build_omop_cdm import _resource_path, attach_central_vocabulary
from omop_etl.ml_features import build_concept_tokenizer, _load_cohort

try:  # pragma: no cover - which branch runs depends on the environment
    from omop_learn.backends.backend import OMOPDatasetBackend as _BackendBase
except ImportError:  # omop-learn not installed: stay importable, just without the ABC
    _BackendBase = object

DEFAULT_FEATURE_SQL = {
    "conditions": True,   # name -> temporal?
    "drugs": True,
    "procedures": True,
    "age": False,
    "gender": False,
}
_COHORT_ALIAS = "_olc"
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


def feature_sql_path(name: str) -> str:
    """Path of a bundled DuckDB-dialect omop-learn feature SQL file (``conditions``, ``drugs``, ...)."""
    path = _resource_path("sql", "omop_learn", f"{name}.sql")
    if not Path(path).exists():
        raise FileNotFoundError(f"No bundled omop-learn feature SQL named '{name}' (looked at {path}). "
                                f"Available: {sorted(DEFAULT_FEATURE_SQL)}")
    return path


def default_features(names: Sequence[str] = ("conditions", "drugs", "age", "gender")) -> list:
    """omop-learn ``Feature`` objects for the bundled DuckDB-dialect SQL (needs omop-learn)."""
    try:
        from omop_learn.data.feature import Feature
    except ImportError as exc:
        raise ImportError("default_features needs omop-learn: "
                          "pip install git+https://github.com/clinicalml/omop-learn") from exc
    unknown = [n for n in names if n not in DEFAULT_FEATURE_SQL]
    if unknown:
        raise ValueError(f"Unknown feature(s) {unknown}; choose from {sorted(DEFAULT_FEATURE_SQL)}")
    return [Feature(n, feature_sql_path(n), temporal=DEFAULT_FEATURE_SQL[n]) for n in names]


def cohort_frame_from_parquet(
    con: duckdb.DuckDBPyConnection,
    features_parquet: str | Path,
    person_col: str | None = None,
    index_date_col: str | None = None,
    outcome_col: str | None = None,
) -> pd.DataFrame:
    """Cohort DataFrame in omop-learn's shape (``person_id``, ``end_date``, ``y``) from a cohort parquet.

    ``end_date`` is the prediction/index date: omop-learn keeps events dated on or before it. Row order is
    the parquet's. No omop-learn import is needed.
    """
    _, df = _load_cohort(con, features_parquet, person_col, index_date_col, outcome_col)
    out = pd.DataFrame({"person_id": df["person_id"], "end_date": df["index_date"]})
    if "y" in df.columns:
        out["y"] = df["y"]
    return out


def cohort_from_parquet(con, features_parquet, params: dict | None = None, **column_overrides):
    """omop-learn ``Cohort`` built from a cohort parquet (see ``cohort_frame_from_parquet``)."""
    try:
        from omop_learn.data.cohort import Cohort
    except ImportError as exc:
        raise ImportError("cohort_from_parquet needs omop-learn: "
                          "pip install git+https://github.com/clinicalml/omop-learn") from exc
    frame = cohort_frame_from_parquet(con, features_parquet, **column_overrides)
    return Cohort(None, dict(params or {}), frame)


def lateral_feature_sql(raw_sql: str, cdm_schema: str, vocab_schema: str | None = None,
                        cohort_alias: str = _COHORT_ALIAS) -> str:
    """Rewrite a per-person omop-learn feature SQL template into a correlated subquery.

    ``{person_id}`` -> ``<alias>.person_id``; ``'{end_date}'`` / ``{end_date}`` -> the row's ``end_date``
    (as text / as a value); ``{cdm_schema}`` / ``{vocab_schema}`` -> the schemas. Raises on any other
    placeholder instead of passing a literal ``{...}`` to the database.
    """
    sql = raw_sql.strip().rstrip(";")
    sql = sql.replace("'{end_date}'", f"CAST({cohort_alias}.end_date AS VARCHAR)")
    sql = sql.replace("{end_date}", f"{cohort_alias}.end_date")
    sql = sql.replace("{person_id}", f"{cohort_alias}.person_id")
    sql = sql.replace("{cdm_schema}", cdm_schema)
    sql = sql.replace("{vocab_schema}", vocab_schema or cdm_schema)
    leftover = sorted(set(_PLACEHOLDER_RE.findall(sql)))
    if leftover:
        raise ValueError(f"Unsupported placeholder(s) in feature SQL: {leftover}. "
                         "Supported: {cdm_schema}, {vocab_schema}, {person_id}, {end_date}.")
    return sql


def _config_get(config, name, default=None):
    return getattr(config, name, default) if config is not None else default


class DuckDBBackend(_BackendBase):
    """omop-learn ``OMOPDatasetBackend`` for DuckDB.

    Args:
        config: omop-learn ``Config`` (attribute namespace). Read keys: ``path`` (DuckDB file, when ``con``
            is not given), ``cdm_schema`` (default ``main``), ``vocab_schema`` (where ``concept`` lives;
            defaults to ``cdm_schema``, e.g. ``central_vocab.main``), ``central_vocab`` (path of a central
            vocabulary database to attach read-only).
        connect_args: Extra ``duckdb.connect`` arguments, e.g. ``{"read_only": True}`` (default for files).
        con: An existing DuckDB connection to use instead of opening ``config.path``.
        strategy: ``"lateral"`` (set-based, default) or ``"per_person"`` (omop-learn's original loop).
        keep_end_date: Also write the cohort's ``end_date`` into each ``data.json`` record (default False).
            omop-learn's ``OMOPDatasetTorch.collate`` calls ``torch.tensor`` on every remaining record key and
            fails on strings, so a string ``end_date`` breaks ``to_torch()`` batches; nothing in omop-learn reads
            it back. ``end_date`` is still used to bound the features.
    """

    def __init__(self, config=None, connect_args: dict | None = None, con=None, echo: bool = False,
                 strategy: str = "lateral", keep_end_date: bool = False):
        if strategy not in ("lateral", "per_person"):
            raise ValueError("strategy must be 'lateral' or 'per_person'")
        self.config = config
        self.strategy = strategy
        self.keep_end_date = keep_end_date
        self.echo = echo
        self.owns_connection = con is None
        if con is None:
            path = _config_get(config, "path")
            if not path:
                raise ValueError("Provide con=<duckdb connection> or a config with a DuckDB file `path`.")
            args = {"read_only": True}
            args.update(connect_args or {})
            con = duckdb.connect(str(path), **args)
        self.con = con
        central = _config_get(config, "central_vocab")
        if central:
            attach_central_vocabulary(self.con, central)

    # ----------------------------------------------------------------- generic SQL surface
    def _log(self, sql):
        if self.echo:
            print(sql)

    def execute_query(self, sql):
        """Run ``sql`` and return a pandas DataFrame."""
        self._log(sql)
        return self.con.execute(sql).df()

    def execute_queries(self, *sqls):
        """Run each statement, discarding results."""
        for sql in sqls:
            self._log(sql)
            self.con.execute(sql)
        print(f"Executed {len(sqls)} SQLs")

    def build_table(self, schema_name, table_name=None, sql=None):
        """(Re)build a table: drop ``schema.table`` if present, then run ``sql`` (which creates it).

        Accepts omop-learn's two call shapes: ``(schema, table, sql)`` (``Cohort.from_sql_file``) and
        ``("schema.table", sql)`` (``build_cohort_table_from_sql_file``).
        """
        if sql is None:
            qualified, sql = schema_name, table_name
        else:
            qualified = f"{schema_name}.{table_name}"
        if not _IDENT_RE.match(qualified):
            raise ValueError(f"Invalid table identifier: {qualified!r}")
        self.con.execute(f"DROP TABLE IF EXISTS {qualified}")
        self._log(sql)
        self.con.execute(sql)

    def get_all_tables(self, schema=None):
        """``pandas.Series`` of table names (optionally within ``schema``)."""
        if schema is None:
            rows = self.con.execute("SELECT table_name FROM information_schema.tables").fetchall()
        else:
            rows = self.con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
                                    [schema]).fetchall()
        return pd.Series([r[0] for r in rows], name="table_name", dtype=object)

    def create_schema(self, schema):
        if not _IDENT_RE.match(schema):
            raise ValueError(f"Invalid schema identifier: {schema!r}")
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    def reset_schema(self, schema):
        if not _IDENT_RE.match(schema):
            raise ValueError(f"Invalid schema identifier: {schema!r}")
        self.con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    def close(self):
        if self.owns_connection:
            self.con.close()

    # ----------------------------------------------------------------- feature building
    def _feature_rows(self, cohort_df: pd.DataFrame, feature, cdm_schema, vocab_schema) -> pd.DataFrame:
        """Run one feature for the whole cohort -> DataFrame[ol_row, c1, c2] (first two feature columns)."""
        if self.strategy == "per_person":
            frames = []
            for ol_row, pid, end in zip(cohort_df["ol_row"], cohort_df["person_id"], cohort_df["end_date"]):
                sql = feature.raw_sql.format(cdm_schema=cdm_schema, person_id=pid, end_date=end,
                                             vocab_schema=vocab_schema or cdm_schema)
                part = self.con.execute(sql).df()
                if len(part):
                    part = part.iloc[:, :2].copy()
                    part.columns = ["c1", "c2"]
                    part.insert(0, "ol_row", ol_row)
                    frames.append(part)
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["ol_row", "c1", "c2"])

        sql = lateral_feature_sql(feature.raw_sql, cdm_schema, vocab_schema)
        query = (f"SELECT {_COHORT_ALIAS}.ol_row AS ol_row, f.* FROM _ol_cohort {_COHORT_ALIAS}, "
                 f"LATERAL (\n{sql}\n) f")
        out = self.con.execute(query).df()
        if out.shape[1] < 3:
            raise ValueError(f"Feature '{feature.name}' must return at least two columns "
                             "(temporal: concept name, date; non-temporal: value, name).")
        out = out.iloc[:, :3].copy()
        out.columns = ["ol_row", "c1", "c2"]
        return out

    def build_features(self, config, cohort, features, tokenizer, dataset_dir, is_visit_dataset,
                       num_workers=None):
        """Write ``data.json`` (one JSON line per patient, or per visit) and return a tokenizer.

        Output contract is omop-learn's: per patient, the cohort row's fields plus ``visits`` (list of
        concept lists, oldest first), ``dates``, and each non-temporal feature under its name (``end_date``
        only with ``keep_end_date=True``, see the class docstring). Temporal
        events dated after the cohort ``end_date`` are discarded; patients left with no visits are
        omitted. ``num_workers`` is accepted for interface compatibility and ignored.
        """
        cdm_schema = _config_get(config, "cdm_schema") or _config_get(self.config, "cdm_schema") or "main"
        vocab_schema = _config_get(config, "vocab_schema") or _config_get(self.config, "vocab_schema")
        for name, schema in (("cdm_schema", cdm_schema), ("vocab_schema", vocab_schema)):
            if schema is not None and not _IDENT_RE.match(schema):
                raise ValueError(f"Invalid {name}: {schema!r}")

        person_dicts = cohort.cohort.to_dict("records")
        cohort_df = pd.DataFrame({
            "ol_row": range(len(person_dicts)),
            "person_id": [p["person_id"] for p in person_dicts],
            "end_date": pd.to_datetime([p["end_date"] for p in person_dicts]).date,
        })
        end_dates = pd.to_datetime(cohort_df["end_date"])

        temporal = [f for f in features if f.temporal]
        non_temporal = [f for f in features if not f.temporal]
        visits_by_row: dict[int, dict] = defaultdict(lambda: defaultdict(list))
        ntmp_by_row: dict[int, dict] = defaultdict(dict)
        concept_set = set()

        self.con.register("_ol_cohort", cohort_df)
        try:
            for feat in temporal:
                rows = self._feature_rows(cohort_df, feat, cdm_schema, vocab_schema).dropna(subset=["c1", "c2"])
                if not len(rows):
                    continue
                dates = pd.to_datetime(rows["c2"])
                rows = rows[(dates <= end_dates.to_numpy()[rows["ol_row"].to_numpy()]).to_numpy()]
                for ol_row, concept, date in sorted(zip(rows["ol_row"], rows["c1"],
                                                       pd.to_datetime(rows["c2"]).dt.date)):
                    visits_by_row[int(ol_row)][date].append(concept)
                    concept_set.add(concept)
            for feat in non_temporal:
                for ol_row, value, name in self._feature_rows(cohort_df, feat, cdm_schema, vocab_schema).itertuples(
                        index=False):
                    ntmp_by_row[int(ol_row)][name] = value
        finally:
            self.con.unregister("_ol_cohort")

        json_path = Path(dataset_dir) / "data.json"
        with open(json_path, "w") as fh:
            for ol_row, person in enumerate(person_dicts):
                by_date = visits_by_row.get(ol_row, {})
                dates = sorted(by_date)
                if is_visit_dataset:
                    for d in dates:
                        fh.write(json.dumps({"concepts": by_date[d], "date": d}, default=str) + "\n")
                    continue
                if not dates:
                    continue  # omop-learn drops patients with no visits
                record = dict(person)
                if not self.keep_end_date:
                    record.pop("end_date", None)
                record["visits"] = [by_date[d] for d in dates]
                record["dates"] = dates
                record.update(ntmp_by_row.get(ol_row, {}))
                fh.write(json.dumps(record, default=str) + "\n")

        if tokenizer is None:
            tokenizer = build_concept_tokenizer(concept_set)
        return tokenizer
