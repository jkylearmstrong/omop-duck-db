"""FR-B1/B2/B3: SQL extraction to Parquet, OMOP CDM import, source contract, Parquet ETL input."""

from __future__ import annotations

import datetime as dt
import decimal
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

sa = pytest.importorskip("sqlalchemy")

from omop_etl import etl_pcornet  # noqa: E402
from omop_etl.extract import from_sql, import_omop, resolve_url  # noqa: E402
from omop_etl.source_check import SourceContractError, check_source  # noqa: E402

SAMPLE = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"


@pytest.fixture()
def sqlite_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'src.db'}"
    eng = sa.create_engine(url)
    md = sa.MetaData()
    t = sa.Table(
        "ENCOUNTER", md,
        sa.Column("ENCOUNTERID", sa.Integer, primary_key=True),
        sa.Column("PATID", sa.String(10)),
        sa.Column("ADMIT_DATE", sa.DateTime),
        sa.Column("COST", sa.Numeric(10, 2)),
        sa.Column("FLAG", sa.Boolean),
    )
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(t.insert(), [
            {"ENCOUNTERID": i, "PATID": f"P{i}", "ADMIT_DATE": dt.datetime(2020, 1, 1, 13, 30) + dt.timedelta(days=i),
             "COST": decimal.Decimal("10.50") * i if i % 4 else None, "FLAG": bool(i % 2)} for i in range(1, 11)])
    eng.dispose()
    return url


def test_from_sql_roundtrip_chunked_lowercase(sqlite_url, tmp_path):
    rep = from_sql(sqlite_url, ["encounter"], tmp_path / "out", chunksize=3)
    assert rep[0]["rows"] == 10 and rep[0]["source_table"] == "ENCOUNTER"
    con = duckdb.connect()
    f = (tmp_path / "out" / "encounter.parquet").as_posix()
    cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM '{f}'").fetchall()}
    assert set(cols) == {"encounterid", "patid", "admit_date", "cost", "flag"}
    assert cols["encounterid"] == "BIGINT" and cols["admit_date"].startswith("TIMESTAMP") and cols["flag"] == "BOOLEAN"
    assert cols["cost"].startswith("DECIMAL(10,2)")
    assert con.execute(f"SELECT count(*), count(cost), max(admit_date) FROM '{f}'").fetchone() == (
        10, 8, dt.datetime(2020, 1, 11, 13, 30))  # time component survives


def test_from_sql_table_map_and_missing(sqlite_url, tmp_path):
    rep = from_sql(sqlite_url, ["enc"], tmp_path / "o", table_map={"enc": "encounter"})
    assert rep[0]["table"] == "enc" and (tmp_path / "o" / "enc.parquet").exists()
    with pytest.raises(LookupError, match="nope"):
        from_sql(sqlite_url, ["nope"], tmp_path / "o2")


def test_url_from_env_and_no_secret_leak(monkeypatch, tmp_path):
    monkeypatch.setenv("OMOP_SRC", "sqlite:///x.db")
    assert resolve_url("env:OMOP_SRC") == "sqlite:///x.db"
    with pytest.raises(ValueError, match="NOT_SET"):
        resolve_url("env:NOT_SET")
    with pytest.raises(Exception) as ei:
        from_sql("postgresql://user:hunter2@127.0.0.1:1/db", ["person"], tmp_path / "o")
    assert "hunter2" not in str(ei.value)


def _cdm_server(tmp_path):
    """Build a CDM in memory, then mirror two tables (upper-case, as Oracle would) into a SQLite 'server'."""
    from omop_etl import build_schema

    src = duckdb.connect()
    build_schema(src)
    src.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "VALUES (1, 8507, 1970, 8527, 38003564), (2, 8532, 1980, 8527, 38003564)")
    src.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                "visit_end_date, visit_type_concept_id) VALUES (10, 1, 9201, DATE '2021-01-01', DATE '2021-01-05', 32827)")
    url = f"sqlite:///{tmp_path / 'cdm.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        for t in ("person", "visit_occurrence"):
            df = src.execute(f"SELECT * FROM {t}").df()
            md = sa.MetaData()
            tbl = sa.Table(t.upper(), md, *[sa.Column(x.upper(), sa.String) for x in df.columns])
            md.create_all(c)
            rows = [{k.upper(): (None if pd.isna(v) else str(v))
                     for k, v in r.items()} for r in df.to_dict("records")]
            c.execute(tbl.insert(), rows)
    eng.dispose()
    return url


def test_import_omop_copies_tables(tmp_path):
    url = _cdm_server(tmp_path)
    out = tmp_path / "site.duckdb"
    rep = {r["table"]: r for r in import_omop(url, out, tables=["person", "visit_occurrence", "measurement"])}
    assert rep["person"]["rows"] == 2 and rep["visit_occurrence"]["rows"] == 1
    assert rep["measurement"]["rows"] is None  # absent on the server: skipped, not fatal
    con = duckdb.connect(str(out))
    assert con.execute("SELECT person_id, year_of_birth FROM person ORDER BY 1").fetchall() == [(1, 1970), (2, 1980)]
    assert con.execute("SELECT visit_start_date FROM visit_occurrence").fetchone()[0] == dt.date(2021, 1, 1)


def test_import_omop_requires_person(sqlite_url, tmp_path):
    with pytest.raises(LookupError, match="person"):
        import_omop(sqlite_url, tmp_path / "x.duckdb", tables=["person"])


def test_check_source_ok_and_missing_column(tmp_path):
    ok = check_source(SAMPLE)
    assert ok["ok"] and ok["tables"]["encounter"]["present"] and "death" in ok["skipped"]
    bad = tmp_path / "bad"
    shutil.copytree(SAMPLE, bad)
    enc = bad / "encounter.csv"
    pd.read_csv(enc, dtype=str).drop(columns=["ENC_TYPE"]).to_csv(enc, index=False)
    rep = check_source(bad)
    assert not rep["ok"] and rep["tables"]["encounter"]["missing_required"] == ["ENC_TYPE"]
    with pytest.raises(SourceContractError, match="encounter.*ENC_TYPE"):
        check_source(bad, raise_on_error=True)
    with pytest.raises(SourceContractError):
        etl_pcornet(str(bad), str(tmp_path / "o.duckdb"), build_schema_flag=True, strict_source=True)


def test_parquet_source_matches_csv(tmp_path):
    pq_dir = tmp_path / "pq"
    pq_dir.mkdir()
    con = duckdb.connect()
    for f in SAMPLE.glob("*.csv"):
        con.execute(f"COPY (SELECT * FROM read_csv_auto('{f.as_posix()}', all_varchar=true)) TO "
                    f"'{(pq_dir / (f.stem + '.parquet')).as_posix()}' (FORMAT PARQUET)")
    assert check_source(pq_dir, "parquet")["ok"]
    etl_pcornet(str(SAMPLE), str(tmp_path / "csv.duckdb"), build_schema_flag=True)
    etl_pcornet(str(pq_dir), str(tmp_path / "pq.duckdb"), build_schema_flag=True)
    a, b = duckdb.connect(str(tmp_path / "csv.duckdb")), duckdb.connect(str(tmp_path / "pq.duckdb"))
    for t in ("person", "visit_occurrence", "condition_occurrence", "measurement", "drug_exposure"):
        q = f"SELECT * FROM {t} ORDER BY ALL"
        assert a.execute(q).fetchall() == b.execute(q).fetchall(), t
