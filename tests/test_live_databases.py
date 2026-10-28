"""Live-database integration tests (FR-B1/B2/B4), run in CI against service containers.

Each backend is enabled by an environment variable holding a SQLAlchemy URL; backends without one are skipped, so the
default ``pytest tests`` run is unaffected. ``.github/workflows/live-db.yml`` starts PostgreSQL 16, SQL Server 2022 and
Oracle Free and sets:

* ``OMOP_TEST_POSTGRES_URL``  e.g. ``postgresql+psycopg2://omop:omop@localhost:5432/omop``  # secret-scan:allow
* ``OMOP_TEST_MSSQL_URL``     e.g. ``mssql+pymssql://sa:<pw>@localhost:1433/master``
* ``OMOP_TEST_ORACLE_URL``    e.g. ``oracle+oracledb://omop:<pw>@localhost:1521/?service_name=FREEPDB1``
* ``OMOP_TEST_SQLITE_URL``    optional, lets the generic tests run without any server

What is checked on a real server: chunked streaming with exact row counts / dtypes / values (including NULLs, dates and
timestamps), case-insensitive table lookup (Oracle folds to upper case), importing an OMOP CDM into DuckDB, and - for SQL
Server and Oracle - that the generated DDL for all 39 CDM tables executes and accepts the exported CSV values.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import sys
import time
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

sa = pytest.importorskip("sqlalchemy")

from omop_etl import build_schema, export_cdm  # noqa: E402
from omop_etl.export_targets import generate_ddl, load_cdm_schema  # noqa: E402
from omop_etl.extract import from_sql, import_omop  # noqa: E402

BACKENDS = {
    "postgres": "OMOP_TEST_POSTGRES_URL",
    "mssql": "OMOP_TEST_MSSQL_URL",
    "oracle": "OMOP_TEST_ORACLE_URL",
    "sqlite": "OMOP_TEST_SQLITE_URL",
}


def _engine(name: str):
    url = os.environ.get(BACKENDS[name])
    if not url:
        pytest.skip(f"{BACKENDS[name]} not set")
    last = None
    for _ in range(60):  # service containers can take a minute to accept connections
        try:
            eng = sa.create_engine(url)
            with eng.connect() as c:
                c.execute(sa.text("SELECT 1" if name != "oracle" else "SELECT 1 FROM dual"))
            return eng
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    pytest.fail(f"{name} did not become ready: {type(last).__name__}")


@pytest.fixture(params=list(BACKENDS))
def engine(request):
    eng = _engine(request.param)
    eng.dialect_name = request.param
    yield eng
    eng.dispose()


def _drop(eng, *tables: str) -> None:
    md = sa.MetaData()
    existing = {t.lower(): t for t in sa.inspect(eng).get_table_names()}
    # reflect everything BEFORE opening the DROP transaction: on SQL Server a second connection reflecting while the
    # first holds uncommitted DDL locks blocks forever
    todo = [sa.Table(existing[t.lower()], md, autoload_with=eng) for t in tables if t.lower() in existing]
    with eng.begin() as c:
        for tbl in todo:
            tbl.drop(c)


N = 2503  # not a multiple of the chunk size


def test_chunked_streaming_roundtrip(engine, tmp_path):
    name = "omop_live_events"
    _drop(engine, name)
    md = sa.MetaData()
    t = sa.Table(name, md,
                 sa.Column("event_id", sa.Integer, primary_key=True, autoincrement=False),
                 sa.Column("patid", sa.String(20)),
                 sa.Column("amount", sa.Numeric(10, 2)),
                 sa.Column("event_date", sa.Date),
                 sa.Column("event_ts", sa.DateTime),
                 sa.Column("note", sa.String(200)))
    md.create_all(engine)
    rows = [{"event_id": i, "patid": f"P{i % 97}", "amount": decimal.Decimal(i) / 4 if i % 5 else None,
             "event_date": dt.date(2020, 1, 1) + dt.timedelta(days=i % 400),
             "event_ts": dt.datetime(2020, 1, 1, 13, 30, 15) + dt.timedelta(minutes=i),
             "note": None if i % 7 == 0 else f"a,b \"q\" {i}"} for i in range(N)]
    with engine.begin() as c:
        c.execute(t.insert(), rows)
    try:
        rep = from_sql(os.environ[BACKENDS[engine.dialect_name]], [name.upper()], tmp_path / "out", chunksize=500)
        assert rep[0]["rows"] == N
        con = duckdb.connect()
        f = (tmp_path / "out" / f"{name}.parquet").as_posix()
        cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{f}'").fetchall()}
        assert set(cols) == {"event_id", "patid", "amount", "event_date", "event_ts", "note"}  # lower-cased even on Oracle
        oracle = engine.dialect_name == "oracle"
        assert cols["event_id"] in ("BIGINT", "INTEGER") or (oracle and cols["event_id"].startswith("DECIMAL"))
        assert cols["event_date"] == "DATE" or (oracle and cols["event_date"].startswith("TIMESTAMP"))  # Oracle DATE has a time
        assert cols["event_ts"].startswith("TIMESTAMP") and cols["amount"].startswith("DECIMAL")
        n, n_amt, n_note, total, d0, ts_max = con.execute(
            f"SELECT count(*), count(amount), count(note), sum(amount), min(CAST(event_date AS DATE)), max(event_ts) FROM '{f}'").fetchone()
        assert (n, n_amt, n_note) == (N, sum(1 for r in rows if r["amount"] is not None), sum(1 for r in rows if r["note"]))
        assert total == sum(r["amount"] for r in rows if r["amount"] is not None)
        assert n == N
        assert d0 == dt.date(2020, 1, 1) and ts_max == max(r["event_ts"] for r in rows)
        assert con.execute(f"SELECT note FROM '{f}' WHERE event_id = 1").fetchone()[0] == 'a,b "q" 1'
    finally:
        _drop(engine, name)


def _mirror_cdm(engine) -> None:
    src = duckdb.connect()
    build_schema(src)
    src.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "VALUES (1, 8507, 1970, 8527, 38003564), (2, 8532, 1980, 8527, 38003564)")
    src.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                "visit_end_date, visit_type_concept_id) VALUES (10, 1, 9201, DATE '2021-01-01', DATE '2021-01-05', 32827)")
    model = load_cdm_schema()
    md = sa.MetaData()
    for t in ("person", "visit_occurrence"):
        cols = []
        for c in model[t]:
            typ = {"integer": sa.Integer, "date": sa.Date, "timestamp": sa.DateTime}.get(c.base) or (
                sa.Float if c.base == "numeric" else sa.String(c.length or 255))
            cols.append(sa.Column(c.name, typ))
        sa.Table(t, md, *cols)
    md.create_all(engine)
    with engine.begin() as conn:
        for t in ("person", "visit_occurrence"):
            df = src.execute(f"SELECT * FROM {t}").df()
            tbl = md.tables[t]
            conn.execute(tbl.insert(), [{k: (None if v is None or v != v else (v.date() if k.endswith("_date") and hasattr(v, "date") else
                                                (v.to_pydatetime() if hasattr(v, "to_pydatetime") else
                                                 (int(v) if k.endswith("_id") or k.endswith("_of_birth") else v))))
                                         for k, v in r.items()} for r in df.to_dict("records")])


def test_import_omop_from_live_server(engine, tmp_path):
    _drop(engine, "person", "visit_occurrence")
    _mirror_cdm(engine)
    try:
        out = tmp_path / "site.duckdb"
        rep = {r["table"]: r for r in import_omop(os.environ[BACKENDS[engine.dialect_name]], out,
                                                  tables=["person", "visit_occurrence", "measurement"])}
        assert rep["person"]["rows"] == 2 and rep["visit_occurrence"]["rows"] == 1 and rep["measurement"]["rows"] is None
        con = duckdb.connect(str(out), read_only=True)
        assert con.execute("SELECT person_id, year_of_birth FROM person ORDER BY 1").fetchall() == [(1, 1970), (2, 1980)]
        assert con.execute("SELECT visit_start_date FROM visit_occurrence").fetchone()[0] == dt.date(2021, 1, 1)
    finally:
        _drop(engine, "person", "visit_occurrence")


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_generated_ddl_executes_and_accepts_exported_values(dialect, tmp_path):
    eng = _engine(dialect)
    target = "sqlserver" if dialect == "mssql" else "oracle"
    model = load_cdm_schema()
    _drop(eng, *model)
    stmts = [s.strip() for s in generate_ddl(target).split(";") if s.strip()]
    assert len(stmts) == 39
    try:
        with eng.begin() as c:
            for s in stmts:
                c.execute(sa.text(s))
        assert {t.lower() for t in sa.inspect(eng).get_table_names()} >= set(model)

        src = duckdb.connect()
        build_schema(src)
        src.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, birth_datetime, race_concept_id, "
                    "ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1970, TIMESTAMP '1970-03-04 05:06:07', "
                    "8527, 38003564, 'a,b \"q\"')")
        src.execute("INSERT INTO observation_period VALUES (1, 1, DATE '2020-01-01', DATE '2020-12-31', 32817)")
        export_cdm(src, target, tmp_path, tables=["person", "observation_period"])
        md = sa.MetaData()
        con = duckdb.connect()
        with eng.begin() as c:
            for t in ("person", "observation_period"):
                tbl = sa.Table(t, md, autoload_with=eng)
                raw = con.execute(f"SELECT * FROM read_csv_auto('{(tmp_path / f'{t}.csv').as_posix()}', all_varchar=true)").fetchall()
                for r in raw:
                    vals = {}
                    for col, v in zip(model[t], r):
                        if v is None:
                            continue
                        vals[col.name] = (int(v) if col.base == "integer" else float(v) if col.base == "numeric" else
                                          dt.date.fromisoformat(v) if col.base == "date" else
                                          dt.datetime.fromisoformat(v) if col.base == "timestamp" else v)
                    c.execute(tbl.insert(), [vals])
        with eng.connect() as c:
            assert c.execute(sa.text("SELECT count(*) FROM person")).scalar() == 1
            assert c.execute(sa.text("SELECT count(*) FROM observation_period")).scalar() == 1
    finally:
        _drop(eng, *model)
        eng.dispose()


# ---- FR-D6: the portable cohort SQL on real servers -------------------------------------------------------------
_COHORT_TABLES = {
    "person": "person_id BIGINT, year_of_birth INTEGER",
    "visit_occurrence": ("visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INTEGER, visit_start_date DATE, "
                         "visit_end_date DATE, discharged_to_concept_id INTEGER"),
    "observation_period": "person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE",
    "death": "person_id BIGINT, death_date DATE",
    "measurement": "person_id BIGINT, measurement_date DATE",
    "condition_occurrence": "person_id BIGINT, condition_start_date DATE",
    "drug_exposure": "person_id BIGINT, drug_exposure_start_date DATE",
    "cohort": "cohort_definition_id INTEGER, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE",
}
_SQLGLOT_DIALECT = {"postgres": "postgres", "mssql": "tsql", "oracle": "oracle"}


def _statements(sql: str, backend: str) -> list[str]:
    from omop_etl.cohort_sql import translate_cohort_sql

    return translate_cohort_sql(sql, _SQLGLOT_DIALECT[backend])


@pytest.mark.parametrize("backend", ["postgres", "mssql", "oracle"])
def test_portable_cohort_sql_gives_the_same_cohorts_on_every_server(backend):
    """Same CDM rows -> same target / outcome cohorts as on DuckDB, with the SQL translated for each dialect.

    Translation here uses sqlglot, not SqlRender (the reference translator): it checks the SQL's constructs (DATEADD,
    correlated EXISTS, window functions, schema-qualified DELETE/INSERT) on each engine, not SqlRender's output.
    """
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import render_cohort_sql

    eng = _engine(backend)
    schema = {"postgres": "public", "mssql": "dbo", "oracle": None}[backend] or sa.inspect(eng).default_schema_name
    _drop(eng, *_COHORT_TABLES)
    with eng.begin() as c:
        for name, cols in _COHORT_TABLES.items():
            c.execute(sa.text(f"CREATE TABLE {name} ({cols})"))
        d = dt.date.fromisoformat
        for pid, yob in {1: 1980, 2: 1980, 3: 2015, 4: 1980, 5: 1980}.items():
            c.execute(sa.text("INSERT INTO person VALUES (:p, :y)"), {"p": pid, "y": yob})
            end = d("2020-12-31") if pid != 4 else d("2020-02-01")
            c.execute(sa.text("INSERT INTO observation_period VALUES (:p, :s, :e)"), {"p": pid, "s": d("2015-01-01"), "e": end})
        for row in [(1, 1, 9201, "2020-03-01", "2020-03-05", None), (2, 1, 9201, "2020-02-01", "2020-02-04", None),
                    (3, 2, 9201, "2020-03-01", "2020-03-05", 4216643), (4, 3, 9201, "2020-03-01", "2020-03-05", None),
                    (5, 4, 9201, "2020-01-10", "2020-01-20", None), (6, 5, 262, "2020-03-01", "2020-03-05", None),
                    (7, 1, 9203, "2020-01-01", "2020-01-01", None)]:
            c.execute(sa.text("INSERT INTO visit_occurrence VALUES (:v, :p, :c, :s, :e, :dc)"),
                      {"v": row[0], "p": row[1], "c": row[2], "s": d(row[3]), "e": d(row[4]), "dc": row[5]})

    common = dict(cdm_database_schema=schema, target_database_schema=schema, target_cohort_table="cohort",
                  followup_days=30, min_age=18)
    try:
        with eng.begin() as c:
            for stmt in _statements(render_cohort_sql(REPO_ROOT / "cohorts" / "target_cohort.sql", target_cohort_id=1, **common), backend):
                c.execute(sa.text(stmt))
            for stmt in _statements(render_cohort_sql(REPO_ROOT / "cohorts" / "outcome_cohort.sql", target_cohort_id=2, **common), backend):
                c.execute(sa.text(stmt))
        with eng.connect() as c:
            target = c.execute(sa.text(
                "SELECT subject_id, cohort_start_date FROM cohort WHERE cohort_definition_id = 1 ORDER BY subject_id")).fetchall()
            n_outcome = c.execute(sa.text("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 2")).scalar()
    finally:
        _drop(eng, *_COHORT_TABLES)
    assert [(r[0], str(r[1])[:10]) for r in target] == [(1, "2020-02-04"), (5, "2020-03-05")]   # same as DuckDB
    assert n_outcome == 6
