"""Tests for the omop-learn DuckDB backend.

Layered so each piece is verified on its own:

1. pure units (SQL rewrite, cohort helper)           - no omop-learn needed
2. ``DuckDBBackend`` with duck-typed Cohort/Feature  - no omop-learn needed; checks exact data.json content
3. lateral vs per_person strategies                   - the set-based rewrite must equal the reference loop
4. backend vs the set-based extractors                - two independent implementations must agree
5. real omop-learn classes (ABC, Feature, Cohort, OMOPDataset, torch collate) - skipped if not importable
"""

import importlib
import json
import sys
import types
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from omop_etl.omop_learn_backend import (
    DuckDBBackend,
    cohort_frame_from_parquet,
    default_features,
    feature_sql_path,
    lateral_feature_sql,
)


def _have(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


needs_omop_learn = pytest.mark.skipif(not _have("omop_learn.backends.backend"),
                                      reason="omop-learn not importable (set OMOP_LEARN_SRC or pip install it)")


def _feature(name, temporal):
    """Duck-typed stand-in for omop_learn.data.feature.Feature (same attributes the backend reads)."""
    return types.SimpleNamespace(name=name, temporal=temporal,
                                 raw_sql=Path(feature_sql_path(name)).read_text())


def _features(*names):
    return [_feature(n, n in ("conditions", "drugs", "procedures")) for n in names]


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _build(con, parquet, out_dir, features=("conditions", "drugs", "age", "gender"), strategy="lateral",
           is_visit_dataset=False, tokenizer=None, keep_end_date=False):
    backend = DuckDBBackend(con=con, strategy=strategy, keep_end_date=keep_end_date)
    cohort = types.SimpleNamespace(cohort=cohort_frame_from_parquet(con, parquet))
    out_dir.mkdir(parents=True, exist_ok=True)
    tok = backend.build_features(types.SimpleNamespace(cdm_schema="main"), cohort, _features(*features),
                                 tokenizer, out_dir, is_visit_dataset)
    return tok, out_dir / "data.json"


# ------------------------------------------------------------------ 1. pure units
def test_lateral_rewrite_placeholders():
    sql = lateral_feature_sql(
        "select x from {cdm_schema}.t a join {vocab_schema}.concept c on 1=1 where a.person_id = {person_id};",
        cdm_schema="main", vocab_schema="central_vocab.main")
    assert sql == ("select x from main.t a join central_vocab.main.concept c on 1=1 "
                   "where a.person_id = _olc.person_id")
    # vocab_schema defaults to cdm_schema
    assert "main.concept" in lateral_feature_sql("select 1 from {vocab_schema}.concept", cdm_schema="main")


def test_lateral_rewrite_end_date_quoted_and_bare():
    sql = lateral_feature_sql("select cast('{end_date}' as date), {end_date} from {cdm_schema}.t", "main")
    assert "CAST(_olc.end_date AS VARCHAR)" in sql and "'{end_date}'" not in sql
    assert ", _olc.end_date from" in sql


def test_lateral_rewrite_rejects_unknown_placeholder():
    with pytest.raises(ValueError, match=r"\{study_id\}"):
        lateral_feature_sql("select {study_id} from {cdm_schema}.t", "main")


def test_bundled_feature_sql_is_found_and_unknown_is_rejected():
    for name in ("conditions", "drugs", "procedures", "age", "gender"):
        assert Path(feature_sql_path(name)).exists()
    with pytest.raises(FileNotFoundError):
        feature_sql_path("nope")


def test_cohort_frame_from_parquet_shape_and_order(cdm_and_cohort):
    con, parquet = cdm_and_cohort
    frame = cohort_frame_from_parquet(con, parquet)
    assert list(frame.columns) == ["person_id", "end_date", "y"]
    assert list(frame["person_id"]) == [2, 1, 3, 1]          # parquet order, not sorted
    assert list(frame["y"]) == [1, 0, 0, 1]
    assert str(frame["end_date"].iloc[3].date()) == "2021-09-01"


# ------------------------------------------------------------------ 2. backend SQL surface
def test_backend_sql_surface_and_identifier_guards(tmp_path):
    con = duckdb.connect(":memory:")
    backend = DuckDBBackend(con=con)
    backend.create_schema("scratch")
    backend.build_table("scratch", "t", "CREATE TABLE scratch.t AS SELECT 1 AS a")                    # (schema, table, sql)
    backend.build_table("scratch.t", "CREATE TABLE scratch.t AS SELECT 2 AS a")                      # ("schema.table", sql), replaces
    assert backend.execute_query("SELECT a FROM scratch.t")["a"].tolist() == [2]
    assert "t" in backend.get_all_tables("scratch").tolist()
    assert "t" not in backend.get_all_tables("main").tolist()
    backend.execute_queries("CREATE TABLE scratch.u AS SELECT 1 a", "DROP TABLE scratch.u")
    backend.reset_schema("scratch")
    assert "t" not in backend.get_all_tables().tolist()
    with pytest.raises(ValueError, match="Invalid"):
        backend.build_table("scratch", "t; DROP TABLE x", "select 1")
    with pytest.raises(ValueError, match="Invalid"):
        backend.reset_schema("a b")
    with pytest.raises(ValueError, match="strategy"):
        DuckDBBackend(con=con, strategy="magic")


def test_backend_opens_file_read_only_and_requires_a_source(tmp_path):
    db = tmp_path / "x.duckdb"
    w = duckdb.connect(str(db)); w.execute("CREATE TABLE t AS SELECT 1 a"); w.close()
    backend = DuckDBBackend(config=types.SimpleNamespace(path=str(db)))
    assert backend.execute_query("SELECT a FROM t")["a"].tolist() == [1]
    with pytest.raises(duckdb.Error):
        backend.execute_query("CREATE TABLE y AS SELECT 1")   # default is read-only
    backend.close()
    with pytest.raises(ValueError, match="con=|path"):
        DuckDBBackend()


# ------------------------------------------------------------------ 2b. exact data.json content
def test_build_features_data_json_contents(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    tok, data = _build(con, parquet, tmp_path / "ds")
    lines = _read_jsonl(data)
    assert len(lines) == 3, "person 3 has no events and must be omitted (omop-learn drops empty patients)"

    t2d, met = "201 - condition - Type 2 diabetes", "301 - drug - Metformin"
    p2, p1_first, p1_second = lines                   # parquet order with person 3 removed

    assert "end_date" not in p2, "end_date is used to bound features but not written (breaks torch collate)"
    assert (p2["person_id"], p2["y"]) == (2, 1)
    assert p2["dates"] == ["2021-05-10", "2021-05-30"]
    assert p2["visits"] == [["205 - condition"], [t2d, met]]
    assert p2["Gender M(1)/F(0)"] == 0 and p2["Age at end_date"] == 51

    assert (p1_first["person_id"], p1_first["y"]) == (1, 0)
    # omop-learn semantics: no lookback bound (204 from 2020 is kept), event ON end_date kept (202),
    # event AFTER end_date dropped (203), concept 0 excluded by the SQL.
    assert p1_first["dates"] == ["2020-01-01", "2021-05-20", "2021-05-25", "2021-06-01"]
    assert p1_first["visits"] == [["204 - condition"], [t2d], [t2d, met], ["202 - condition"]]
    assert p1_first["Gender M(1)/F(0)"] == 1

    assert p1_second["person_id"] == 1 and p1_second["dates"][-1] == "2021-06-10"   # 203 now in the past
    assert p1_second["visits"][-1] == ["203 - condition"]

    expected_tokens = {"205 - condition", t2d, met, "204 - condition", "202 - condition", "203 - condition"}
    assert set(tok.concept_list[5:]) == expected_tokens and tok.pad_token_idx == 3
    assert not any(t.startswith("0 - ") for t in tok.concept_list)


def test_keep_end_date_option_writes_it(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    _, data = _build(con, parquet, tmp_path / "k", features=("conditions",), keep_end_date=True)
    assert [r["end_date"][:10] for r in _read_jsonl(data)] == ["2021-06-01", "2021-06-01", "2021-09-01"]


def test_build_features_visit_dataset_mode(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    _, data = _build(con, parquet, tmp_path / "v", features=("conditions", "drugs"), is_visit_dataset=True)
    lines = _read_jsonl(data)
    assert len(lines) == 2 + 4 + 5                      # visits (dates) per kept patient row
    assert set(lines[0]) == {"concepts", "date"}
    assert lines[0] == {"concepts": ["205 - condition"], "date": "2021-05-10"}


def test_build_features_reuses_given_tokenizer(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    tok, _ = _build(con, parquet, tmp_path / "a")
    tok2, _ = _build(con, parquet, tmp_path / "b", tokenizer=tok)
    assert tok2 is tok


def test_build_features_rejects_feature_with_too_few_columns(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    backend = DuckDBBackend(con=con)
    bad = types.SimpleNamespace(name="bad", temporal=True, raw_sql="select 1 as only_one where {person_id} > 0")
    cohort = types.SimpleNamespace(cohort=cohort_frame_from_parquet(con, parquet))
    (tmp_path / "bad").mkdir()
    with pytest.raises(ValueError, match="at least two columns"):
        backend.build_features(types.SimpleNamespace(cdm_schema="main"), cohort, [bad], None, tmp_path / "bad", False)


# ------------------------------------------------------------------ 3. lateral == per_person
def test_lateral_strategy_equals_per_person_reference(cdm_and_cohort, tmp_path):
    con, parquet = cdm_and_cohort
    _, fast = _build(con, parquet, tmp_path / "fast", strategy="lateral")
    _, ref = _build(con, parquet, tmp_path / "ref", strategy="per_person")
    assert fast.read_text() == ref.read_text()


# ------------------------------------------------------------------ 4. backend vs set-based extractors
def test_backend_and_set_based_extractors_agree_on_tokens(cdm_and_cohort, tmp_path):
    """Two independent code paths (omop-learn Feature SQL via LATERAL; ml_features core) must select the same
    concepts per row when configured alike: unbounded lookback, index date inclusive (omop-learn keeps
    events dated <= end_date), same domains."""
    pytest.importorskip("scipy")
    from omop_etl import extract_sard_visit_tensors

    con, parquet = cdm_and_cohort
    _, data = _build(con, parquet, tmp_path / "x", features=("conditions", "drugs"), keep_end_date=True)
    from_backend = {(r["person_id"], r["end_date"][:10]): {c for v in r["visits"] for c in v}
                    for r in _read_jsonl(data)}

    res = extract_sard_visit_tensors(con, parquet, lookback_days=None, include_index_date=True,
                                     domains=("condition", "drug"))
    tok, T = res["tokenizer"], res["concept_tensor"]
    from_core = {}
    for i, (pid, idx) in enumerate(zip(res["cohort"]["person_id"], res["cohort"]["index_date"])):
        tokens = {tok.concept_list[t] for t in T[i].ravel() if t != tok.pad_token_idx}
        if tokens:
            from_core[(int(pid), str(idx.date()))] = tokens
    assert from_core == from_backend


# ------------------------------------------------------------------ 5. real omop-learn
@needs_omop_learn
def test_backend_satisfies_real_omop_learn_interface(cdm_and_cohort):
    from omop_learn.backends.backend import OMOPDatasetBackend
    con, _ = cdm_and_cohort
    backend = DuckDBBackend(con=con)          # would raise TypeError if an abstract method were missing
    assert isinstance(backend, OMOPDatasetBackend)


@needs_omop_learn
def test_default_features_and_cohort_use_real_classes(cdm_and_cohort):
    from omop_learn.data.cohort import Cohort
    from omop_learn.data.feature import Feature
    from omop_etl.omop_learn_backend import cohort_from_parquet
    con, parquet = cdm_and_cohort
    feats = default_features()
    assert all(isinstance(f, Feature) for f in feats)
    assert {f.name: f.temporal for f in feats} == {"conditions": True, "drugs": True, "age": False, "gender": False}
    with pytest.raises(ValueError, match="Unknown"):
        default_features(["nope"])
    cohort = cohort_from_parquet(con, parquet, params={"training_end_date": "2021-12-31"})
    assert isinstance(cohort, Cohort) and len(cohort) == 4 and cohort.params["training_end_date"] == "2021-12-31"


@pytest.fixture
def omop_dataset_module(monkeypatch):
    """``omop_learn.omop`` imports `datasets` and `sparse` at module level (HF / numba stack). Neither is
    needed to build a dataset or call ``to_torch``, so stub them only if they are not installed."""
    pytest.importorskip("torch")
    for name in ("datasets", "sparse"):
        if not _have(name):
            monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(load_dataset=None, COO=None))
    try:
        return importlib.import_module("omop_learn.omop")
    except ImportError as exc:
        pytest.skip(f"omop_learn.omop not importable: {exc}")


@needs_omop_learn
def test_end_to_end_omop_dataset_to_torch(cdm_and_cohort, tmp_path, omop_dataset_module):
    """omop-learn's own OMOPDataset, driven by the DuckDB backend, through to padded torch batches."""
    import torch
    from omop_learn.utils.config import Config
    from omop_etl.omop_learn_backend import cohort_from_parquet

    con, parquet = cdm_and_cohort
    config = Config({"cdm_schema": "main"})
    dataset = omop_dataset_module.OMOPDataset(
        name="duck", config=config, cohort=cohort_from_parquet(con, parquet), features=default_features(),
        backend=DuckDBBackend(config, con=con), data_dir=tmp_path,
    )
    assert (tmp_path / "duck" / "data.json").exists() and (tmp_path / "duck" / "tokens.json").exists()

    ds = dataset.to_torch()
    assert len(ds) == 3 and list(ds.outcomes) == [1, 0, 1]            # patients with visits, parquet order
    batch = ds.collate([ds[i] for i in range(len(ds))])      # works as-is: no string columns in the records
    visits, times, lengths = batch["visits"], batch["times"], batch["lengths"]
    assert visits.dtype == torch.long and visits.shape[0] == 3
    assert list(lengths.long()) == [2, 4, 5]
    pad = ds.tokenizer.pad_token_idx
    assert (visits[0, 2:] == pad).all() and (times[0, 2:] == -1).all()
    assert (visits[1, :4] != pad).any(dim=1).all(), "every real visit has at least one concept"
