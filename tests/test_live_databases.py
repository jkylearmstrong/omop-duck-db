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
    insp = sa.inspect(eng)
    existing = {t.lower(): t for t in insp.get_table_names()}
    with eng.begin() as c:
        for t in tables:
            if t.lower() in existing:
                sa.Table(existing[t.lower()], md, autoload_with=eng).drop(c)


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
        assert cols["event_id"] in ("BIGINT", "INTEGER") and cols["event_date"] == "DATE"
        assert cols["event_ts"].startswith("TIMESTAMP") and cols["amount"].startswith("DECIMAL")
        n, n_amt, n_note, total, d0, ts_max = con.execute(
            f"SELECT count(*), count(amount), count(note), sum(amount), min(event_date), max(event_ts) FROM '{f}'").fetchone()
        assert (n, n_amt, n_note) == (N, sum(1 for r in rows if r["amount"] is not None), sum(1 for r in rows if r["note"]))
        assert total == sum(r["amount"] for r in rows if r["amount"] is not None)
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
