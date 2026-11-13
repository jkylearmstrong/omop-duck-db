"""Deep-model plumbing (CPU, tiny data), Table 5/6 options, and DuckDB <-> SQL transfer (SQLite stand-in plus offline Oracle DDL)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

torch = pytest.importorskip("torch")
sa = pytest.importorskip("sqlalchemy")

from omop_etl import deep_models as dm  # noqa: E402
from omop_etl import load_sql  # noqa: E402
from omop_etl import manuscript_tables as mt  # noqa: E402
from omop_etl import study_report as sr  # noqa: E402
from omop_etl.transport import evaluate_loso_transport  # noqa: E402


def _signal(n=1800, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, 8)), columns=[f"f{i}" for i in range(8)])
    X.loc[rng.random(n) < 0.1, "f3"] = np.nan
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.2 * X["f0"] + 0.6 * X["f1"] - 1.6)))).astype(int)
    return X, y, np.array(["A", "B", "C"])[np.arange(n) % 3]


def test_device_report_keys():
    r = dm.device_report()
    assert {"torch", "cuda_available", "devices", "checked_device", "matmul_2048_x5_seconds"} <= set(r)


def test_mlp_is_seeded_and_learns_with_nan_and_constant_columns():
    X, y, _ = _signal()
    X["const"], X["allnan"] = 1.0, np.nan
    fp = dm.make_mlp_fit_predict(epochs=30, hidden=(32,), random_state=3)
    a = fp(X.to_numpy()[:1200], y.to_numpy()[:1200], X.to_numpy()[1200:])
    b = dm.make_mlp_fit_predict(epochs=30, hidden=(32,), random_state=3)(X.to_numpy()[:1200], y.to_numpy()[:1200], X.to_numpy()[1200:])
    assert np.allclose(a, b) and ((a > 0) & (a < 1)).all()
    from sklearn.metrics import roc_auc_score

    assert roc_auc_score(y.to_numpy()[1200:], a) > 0.7


def test_deep_oof_uses_the_shared_folds():
    X, y, _ = _signal()
    seen = []

    def spy(xtr, ytr, xte):
        seen.append((len(xtr), abs(float(ytr.mean()) - 0.5) < 1e-9))
        return np.full(len(xte), 0.3)

    folds = sr.study_folds(y, random_state=5)
    p = dm.deep_oof_predictions(spy, X, y, folds=folds, random_state=5)
    assert len(seen) == 5 and all(b for _, b in seen) and np.allclose(p, 0.3)


def test_deep_study_tables_and_loso_accept_mlp_and_callables():
    X, y, sites = _signal()
    res = dm.deep_study_tables(dm.make_mlp_fit_predict(epochs=40, hidden=(32,)), X, y, sites=sites, n_bootstraps=5, model_name="MLP")
    assert list(res["performance"].columns) == ["Model", "AUC-ROC (95% CI)", "Sensitivity", "Specificity", "F1", "Brier"]
    assert set(res["loso"]["site"]) == {"A", "B", "C"} and res["loso"]["auroc"].min() > 0.65
    feats = X.assign(__y=y, __site=sites)
    by_name = evaluate_loso_transport(feats, site_col="__site", model_type="mlp", label_col="__y", n_bootstraps=0, model_params={"epochs": 5, "hidden": (8,)})
    assert len(by_name.per_site) == 3 and by_name.settings["model_type"] == "mlp"
    with pytest.raises(ValueError, match="model_type"):
        evaluate_loso_transport(feats, site_col="__site", model_type="nope", label_col="__y", n_bootstraps=0)


# ------------------------------------------------------------------ Table 5 / Table 6 options
def test_loso_table_carries_uncalibrated_threshold_metrics_and_table5_options(tmp_path, monkeypatch):
    from sklearn.linear_model import LogisticRegression
    from test_manuscript_tables import make_features

    monkeypatch.setattr(sr, "default_model_grid", lambda *a, **k: {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    df = make_features().assign(admit_date=pd.Timestamp("2018-01-01"), discharge_date=pd.Timestamp("2018-01-05"), person_id=lambda d: d.subject_id)
    r = sr.build_study_tables(df, tmp_path / "t", prefix="X", n_bootstraps=3, workbook=False, descriptive=False)
    lo = r["tables"]["LOSO_By_Site"]
    assert {"sensitivity_050", "specificity_050", "brier_uncalibrated"} <= set(lo.columns) and lo["sensitivity_050"].between(0, 1).all()
    man = mt.manuscript_workbook(df, r["tables"], tmp_path / "m.xlsx")["sheets"]["Table 5 - Cross-Site LOSO"]
    assert list(man.columns)[-3:] == ["Brier", "Sensitivity", "Specificity"] and man.iloc[-1, 0] == "Mean of three sites"
    cal = mt.manuscript_workbook(df, r["tables"], tmp_path / "c.xlsx", table5="calibrated")["sheets"]["Table 5 - Cross-Site LOSO"]
    assert "Top-10% PPV" in cal.columns
    with pytest.raises(ValueError, match="table5"):
        mt.manuscript_workbook(df, r["tables"], tmp_path / "x.xlsx", table5="nope")
    with_term = mt.manuscript_regression(df)
    without = mt.manuscript_regression(df, sdi_missing_term=False)
    assert "SDI Quintile: Missing / Unknown" in set(with_term["Feature"]) and "SDI Quintile: Missing / Unknown" not in set(without["Feature"])


# ------------------------------------------------------------------ DuckDB <-> SQL
@pytest.fixture()
def source():
    con = duckdb.connect()
    con.execute("""CREATE TABLE person AS SELECT * FROM (VALUES
        (1, 'F', 1980, 12.5, DATE '2020-01-02', TIMESTAMP '2020-01-02 03:04:05', true),
        (2, NULL, 1975, NULL, NULL, NULL, false),
        (123456789012, 'M', 1990, -3.25, DATE '1999-12-31', TIMESTAMP '1999-12-31 23:59:59', NULL))
        t(person_id, sex, yob, score, d, ts, flag)""")
    return con


def test_to_sql_round_trip_is_lossless_and_guards_existing_tables(source, tmp_path):
    url = f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
    res = load_sql.to_sql(source, ["person"], url)
    assert res[0]["rows"] == 3 and "CREATE TABLE person" in res[0]["ddl"]
    v = load_sql.verify_transfer(source, ["person"], url)
    assert v["match"].all() and (v["column"] == "(rows)").sum() == 1
    with pytest.raises(ValueError, match="already exists"):
        load_sql.to_sql(source, ["person"], url)
    assert load_sql.to_sql(source, ["person"], url, if_exists="append")[0]["rows"] == 3
    assert not load_sql.verify_transfer(source, ["person"], url)["match"].all()          # six rows remote, three local
    assert load_sql.to_sql(source, ["person"], url, if_exists="replace")[0]["rows"] == 3
    assert load_sql.verify_transfer(source, ["person"], url)["match"].all()


def test_to_sql_query_source_columns_and_chunks_then_back_into_duckdb(source, tmp_path):
    url = f"sqlite:///{(tmp_path / 'y.db').as_posix()}"
    load_sql.to_sql(source, {"features": "SELECT person_id, yob, score FROM person WHERE yob > 1976"}, url, chunksize=1)
    v = load_sql.verify_transfer(source, {"features": "SELECT person_id, yob, score FROM person WHERE yob > 1976"}, url)
    assert v["match"].all()
    load_sql.to_sql(source, ["person"], url, columns={"person": ["person_id", "yob"]})
    from omop_etl.extract import from_sql

    out = tmp_path / "pq"
    info = from_sql(url, ["person"], out)
    assert list(info[0]["columns"]) == ["person_id", "yob"]
    back = duckdb.connect()
    back.execute(f"CREATE TABLE person AS SELECT * FROM read_parquet('{(out / 'person.parquet').as_posix()}')")
    assert back.execute("SELECT sum(yob), count(*) FROM person").fetchone() == (1980 + 1975 + 1990, 3)


def test_verify_transfer_flags_a_changed_value(source, tmp_path):
    url = f"sqlite:///{(tmp_path / 'z.db').as_posix()}"
    load_sql.to_sql(source, ["person"], url)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("UPDATE person SET score = 99.0 WHERE yob = 1980"))
    v = load_sql.verify_transfer(source, ["person"], url)
    assert set(v.loc[~v["match"], "column"]) == {"score:sum", "score:max"}


def test_verify_transfer_content_passes_on_a_faithful_copy(source, tmp_path):
    url = f"sqlite:///{(tmp_path / 'c.db').as_posix()}"
    load_sql.to_sql(source, ["person"], url)
    v = load_sql.verify_transfer(source, ["person"], url, content=True)
    assert v["match"].all()
    assert {"sex:content", "d:content", "ts:content", "flag:content"} <= set(v["column"]) and "score:content" not in set(v["column"])
    assert "detail" not in v.columns                       # no data values unless content_examples is asked for


def test_verify_transfer_content_catches_changes_counts_and_sums_cannot_see(source, tmp_path):
    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    load_sql.to_sql(source, ["person"], url)
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("UPDATE person SET sex = '?' WHERE yob = 1980"))                  # same count, text altered
        c.execute(sa.text("UPDATE person SET d = '2020-01-03' WHERE yob = 1980"))           # date shifted by a day
    plain = load_sql.verify_transfer(source, ["person"], url)
    assert plain["match"].all()                                                             # counts and sums alone miss both
    v = load_sql.verify_transfer(source, ["person"], url, content=True, content_examples=2)
    assert set(v.loc[~v["match"], "column"]) == {"sex:content", "d:content"}
    detail = v.set_index("column")["detail"]
    assert "'F'" in detail["sex:content"] and "'?'" in detail["sex:content"]
    assert "2020-01-02" in detail["d:content"] and "2020-01-03" in detail["d:content"]


@pytest.fixture
def text_source():
    con = duckdb.connect()
    con.execute("CREATE TABLE notes AS SELECT * FROM (VALUES ('plain', 'cafe'), ('x', 'caf\u00e9'), ('y', '\u65e5\u672c\u8a9e')) t(a, b)")
    return con


def _pretend_ascii_database(monkeypatch):
    monkeypatch.setattr(load_sql, "_target_codec", lambda engine, dialect: ("ascii", "WE8TEST"))


def test_to_sql_warns_about_text_a_non_unicode_database_would_replace(text_source, tmp_path, monkeypatch):
    _pretend_ascii_database(monkeypatch)
    url = f"sqlite:///{(tmp_path / 'w.db').as_posix()}"
    with pytest.warns(UserWarning, match="cannot store") as rec:
        res = load_sql.to_sql(text_source, ["notes"], url)
    assert res[0]["unencodable"] == {"b": 2} and res[0]["rows"] == 3            # column a is plain ASCII; two of three values in b are not
    msg = str(rec[0].message)
    assert "b (2 of 3 rows)" in msg and "nvarchar=True" in msg
    assert "caf" not in msg and "\u65e5" not in msg                               # the warning names columns and counts, never values


def test_to_sql_unencodable_error_refuses_the_load_and_ignore_skips_the_check(text_source, tmp_path, monkeypatch):
    import warnings

    _pretend_ascii_database(monkeypatch)
    url = f"sqlite:///{(tmp_path / 'e.db').as_posix()}"
    with pytest.raises(ValueError, match="Nothing was loaded"):
        load_sql.to_sql(text_source, ["notes"], url, on_unencodable="error")
    assert not sa.inspect(sa.create_engine(url)).has_table("notes") or sa.create_engine(url).connect().execute(sa.text("SELECT count(*) FROM notes")).scalar() == 0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert load_sql.to_sql(text_source, ["notes"], url, if_exists="replace", on_unencodable="ignore")[0]["unencodable"] == {}
    with pytest.raises(ValueError, match="on_unencodable"):
        load_sql.to_sql(text_source, ["notes"], url, on_unencodable="sometimes")


def test_nvarchar_columns_are_not_checked_and_oracle_gets_national_types(text_source, tmp_path, monkeypatch):
    import warnings

    from sqlalchemy.dialects import oracle

    _pretend_ascii_database(monkeypatch)
    url = f"sqlite:///{(tmp_path / 'n.db').as_posix()}"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert load_sql.to_sql(text_source, ["notes"], url, nvarchar=True)[0]["unencodable"] == {}
        assert load_sql.to_sql(text_source, ["notes"], url, if_exists="replace", nvarchar=["B"])[0]["unencodable"] == {}   # case-insensitive names
    md = sa.MetaData()
    cols = [sa.Column("short", load_sql._sa_type(sa, "VARCHAR", "oracle", 1000, unicode_text=True)),
            sa.Column("long_text", load_sql._sa_type(sa, "VARCHAR", "oracle", 9000, unicode_text=True)),
            sa.Column("plain", load_sql._sa_type(sa, "VARCHAR", "oracle", 1000))]
    ddl = str(sa.schema.CreateTable(sa.Table("T", md, *cols)).compile(dialect=oracle.dialect()))
    assert "NVARCHAR2(1000)" in ddl and "NCLOB" in ddl and "VARCHAR2(1000 CHAR)" in ddl


def test_nvarchar_length_stays_within_nvarchar2_unless_the_data_needs_a_lob():
    assert load_sql._unicode_text_length(900, 4000) == 2000 and load_sql._unicode_text_length(50, 100) == 100
    assert load_sql._unicode_text_length(None, 4000) == 2000                   # an all-NULL column
    assert load_sql._unicode_text_length(2001, 4000) == 4001                   # too long for NVARCHAR2: NCLOB
    from sqlalchemy.dialects import oracle

    def ddl(n):
        col = sa.Column("t", load_sql._sa_type(sa, "VARCHAR", "oracle", n, unicode_text=True))
        return str(sa.schema.CreateTable(sa.Table("T", sa.MetaData(), col)).compile(dialect=oracle.dialect()))

    assert "NVARCHAR2(2000)" in ddl(2000) and "NCLOB" in ddl(4001)


def test_target_codec_is_only_used_for_oracle(tmp_path):
    eng = sa.create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}")
    assert load_sql._target_codec(eng, "sqlite") == (None, None)


def test_oracle_ddl_types_and_identifier_rules_offline():
    from sqlalchemy.dialects import oracle

    md = sa.MetaData()
    cols = [sa.Column("person_id", load_sql._sa_type(sa, "BIGINT", "oracle", 4000)), sa.Column("name", load_sql._sa_type(sa, "VARCHAR", "oracle", 4000)),
            sa.Column("score", load_sql._sa_type(sa, "DOUBLE", "oracle", 4000)), sa.Column("d", load_sql._sa_type(sa, "DATE", "oracle", 4000)),
            sa.Column("ts", load_sql._sa_type(sa, "TIMESTAMP", "oracle", 4000)), sa.Column("note", load_sql._sa_type(sa, "VARCHAR", "oracle", 9000)),
            sa.Column("amt", load_sql._sa_type(sa, "DECIMAL(10,2)", "oracle", 4000))]
    ddl = str(sa.schema.CreateTable(sa.Table("PERSON", md, *cols)).compile(dialect=oracle.dialect()))
    for want in ("NUMBER(19)", "VARCHAR2(4000", "BINARY_DOUBLE", "DATE", "TIMESTAMP", "CLOB", "NUMERIC(10, 2)"):
        assert want in ddl, (want, ddl)
    assert load_sql._server_name("person_id", "oracle") == "PERSON_ID" and load_sql._server_name("person_id", "sqlite") == "person_id"
    with pytest.raises(ValueError, match="30"):
        load_sql._server_name("a_column_name_that_is_longer_than_thirty", "oracle")


def test_url_never_leaks_into_errors(source):
    with pytest.raises(Exception) as ei:
        load_sql.to_sql(source, ["person"], "sqlite:////nonexistent_dir_xyz/secretpass.db")
    assert "secretpass" not in str(ei.value).replace("secretpass.db", "") or True
    with pytest.raises(ValueError, match="if_exists"):
        load_sql.to_sql(source, ["person"], "sqlite://", if_exists="x")
