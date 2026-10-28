"""Regression tests for the v0.8.7 review findings (B1-B5, B7-B9, FR-C11, G1)."""
import datetime
import warnings

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from omop_etl.achilles import run_achilles
from omop_etl.bigquery_sync import read_bigquery_table, sync_vocabulary_from_bigquery
from omop_etl.build_omop_cdm import build_schema
from omop_etl.dqd_catalog import run_dqd_catalog
from omop_etl.federated_learning import federated_cox, federated_logistic_regression


def _batch(ids):
    return pa.record_batch([pa.array(ids, pa.int64())], names=["concept_id"])


def test_overwrite_failure_keeps_existing_table_b1():
    con = duckdb.connect()
    read_bigquery_table(con, "p.d", "concept", reader=lambda r, c: [_batch([1, 2, 3, 4, 5])])

    def failing(ref, cols):
        yield _batch([9, 9, 9])
        raise RuntimeError("stream broke")

    with pytest.raises(RuntimeError):
        read_bigquery_table(con, "p.d", "concept", reader=failing)
    assert con.execute("SELECT count(*) FROM concept").fetchone()[0] == 5
    assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '_bq_%'").fetchone()[0] == 0


def test_skip_missing_does_not_swallow_validation_errors_b2():
    con = duckdb.connect()
    with pytest.raises(ValueError, match="project.dataset"):
        sync_vocabulary_from_bigquery(con, "not-a-source", ["concept"], reader=lambda r, c: [], skip_missing=True)
    assert sync_vocabulary_from_bigquery(con, "p.d", ["concept"], reader=lambda r, c: iter(()), skip_missing=True) == {}


def test_view_target_is_refused_b7():
    con = duckdb.connect()
    con.execute("CREATE TABLE base AS SELECT 1 AS concept_id")
    con.execute("CREATE VIEW concept AS SELECT * FROM base")
    with pytest.raises(ValueError, match="view"):
        read_bigquery_table(con, "p.d", "concept", reader=lambda r, c: [_batch([1])])


def test_achilles_rerun_does_not_duplicate_b3():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "VALUES (1, 8507, 1980, 8527, 38003563)")
    run_achilles(con, [1, 2])
    first = con.execute("SELECT count(*) FROM achilles_results").fetchone()[0]
    run_achilles(con, [1, 2], drop_existing=False)
    assert con.execute("SELECT count(*) FROM achilles_results").fetchone()[0] == first


def test_achilles_ignores_same_named_table_in_other_schema_b8():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA other")
    con.execute("CREATE TABLE other.person (person_id INTEGER)")
    res = run_achilles(con, [1])
    assert 1 in res["skipped"]


def _frames(n=200, sep=False, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n)
    y = (x > 0).astype(int) if sep else rng.binomial(1, 1 / (1 + np.exp(-x)))
    return pd.DataFrame({"x": x, "y": y, "time": rng.exponential(1, n), "event": rng.binomial(1, 0.7, n)})


def test_separable_logistic_warns_b5():
    sites = {"A": _frames(sep=True, seed=1), "B": _frames(sep=True, seed=2)}
    with pytest.warns(UserWarning, match="separation"):
        res = federated_logistic_regression(sites, ["x"], "y")
    assert not res.converged


def test_regular_logistic_has_no_warning_b5():
    sites = {"A": _frames(seed=1), "B": _frames(seed=2)}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert federated_logistic_regression(sites, ["x"], "y").converged


def test_cox_noise_raises_on_nonpositive_risk_set_and_blurs_counts_b4():
    sites = {"A": _frames(seed=1), "B": _frames(seed=2)}
    with pytest.raises(ValueError, match="risk-set"):
        federated_cox(sites, ["x"], "time", "event", noise_scale=1e6, seed=1)
    from omop_etl.federated_learning import FederatedNode
    node = FederatedNode("A", sites["A"], noise_scale=5.0, seed=3)
    grid = node.cox_grid("time", "event", None)
    d = node.cox_stats(np.zeros(1), ["x"], "time", "event", grid, None)["d"]
    assert not np.allclose(d, np.round(d))     # counts are no longer exact integers


def test_cox_min_site_events_b12():
    sites = {"A": _frames(seed=1), "B": _frames(seed=2)}
    with pytest.raises(ValueError, match="min_site_events"):
        federated_cox(sites, ["x"], "time", "event", min_site_events=10_000)


def test_dqd_death_primary_key_and_visit_person_match():
    con = duckdb.connect()
    build_schema(con)
    for pid in (1, 2):
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                    f"VALUES ({pid}, 8507, 1980, 8527, 38003563)")
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                "visit_type_concept_id) VALUES (1, 1, 9201, DATE '2020-01-05', DATE '2020-01-09', 0)")
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                "measurement_type_concept_id, visit_occurrence_id) VALUES (1, 2, 3004249, DATE '2020-01-06', 0, 1)")
    for d in ("2020-02-01", "2020-03-01"):
        con.execute(f"INSERT INTO death (person_id, death_date) VALUES (1, DATE '{d}')")
    res = run_dqd_catalog(con, today=datetime.date(2024, 1, 1))
    got = {(r["check_name"], r["cdm_table_name"]): r for r in res["CheckResults"]}
    assert got[("visitPersonMatch", "measurement")]["failed"]
    assert got[("isPrimaryKey", "death")]["failed"]


def test_oracle_unsupported_construct_raises_g1():
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import translate_cohort_sql
    from sqlglot.errors import UnsupportedError
    with pytest.raises((UnsupportedError, ValueError)):
        translate_cohort_sql("SELECT DATEADD(hour, 1, d) FROM t", "oracle")


def test_observation_period_data_cutoff_n2():
    from omop_etl.build_omop_cdm import build_observation_period, resolve_max_date
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "VALUES (1, 8507, 1980, 8527, 38003563)")
    today = datetime.date.today().isoformat()
    for vid, d in ((1, "2020-01-05"), (2, "2023-04-01"), (3, today)):
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                    f"visit_end_date, visit_type_concept_id) VALUES ({vid}, 1, 9201, DATE '{d}', DATE '{d}', 0)")
    assert resolve_max_date(con, "auto") == "2023-04-01"
    assert resolve_max_date(con, None) is None
    build_observation_period(con)
    assert con.execute("SELECT max(observation_period_end_date) FROM observation_period").fetchone()[0].isoformat() == today
    build_observation_period(con, max_date="auto")
    assert con.execute("SELECT max(observation_period_end_date) FROM observation_period").fetchone()[0].isoformat() == "2023-04-01"
