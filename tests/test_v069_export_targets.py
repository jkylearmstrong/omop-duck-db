"""FR-B4: DDL, data files and loader scripts for Oracle / SQL Server / Snowflake / BigQuery from the single DDL source."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema, export_cdm  # noqa: E402
from omop_etl.export_targets import (  # noqa: E402
    Column, EXTRA_TARGETS, IdentifierError, generate_ddl, lint_ddl, load_cdm_schema, oracle_ctl,
)


@pytest.fixture(scope="module")
def schema():
    return load_cdm_schema()


def test_schema_model_matches_duckdb_tables(schema):
    con = duckdb.connect()
    build_schema(con)
    for t, cols in schema.items():
        live = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {t}").fetchall()]
        assert [c.name for c in cols] == live, t


@pytest.mark.parametrize("dialect", EXTRA_TARGETS)
def test_generated_ddl_lints_clean_for_every_table(dialect):
    sql = generate_ddl(dialect)
    assert sql.count("CREATE TABLE") == 39
    assert lint_ddl(sql, dialect) == []  # includes sqlglot parsing when it is installed


def test_lint_catches_problems():
    assert any("unquoted reserved" in p for p in lint_ddl("CREATE TABLE T (\n    DATE DATE\n);", "oracle"))
    assert any("unexpected type" in p for p in lint_ddl("CREATE TABLE T (\n    A TEXT\n);", "sqlserver"))
    assert any("longer than" in p for p in lint_ddl("CREATE TABLE T (\n    " + "A" * 40 + " DATE\n);", "oracle"))


def test_oracle_identifier_limit_and_reserved_words():
    long_schema = {"t": [Column("a_very_long_column_name_that_exceeds_thirty", "integer", None, False)]}
    with pytest.raises(IdentifierError, match="30"):
        generate_ddl("oracle", schema=long_schema)
    assert "NUMBER(10)" in generate_ddl("oracle", schema=long_schema, oracle_max_identifier=128)
    odd = {"t": [Column("date", "date", None, True), Column("comment", "varchar", 10, True), Column("level", "integer", None, True)]}
    ddl = generate_ddl("oracle", schema=odd)
    assert '"DATE" DATE' in ddl and '"COMMENT" VARCHAR2(10 CHAR)' in ddl and '"LEVEL" NUMBER(10)' in ddl
    assert "[date]" in generate_ddl("sqlserver", schema={"t": [Column("end", "date", None, True)]})
    assert "`end`" in generate_ddl("bigquery", schema={"t": [Column("end", "date", None, True)]})


def test_oracle_ctl_skips_header_and_has_explicit_masks(schema):
    ctl = oracle_ctl("person", schema["person"])
    assert "SKIP=1" in ctl and "INFILE 'person.csv'" in ctl
    assert 'BIRTH_DATETIME TIMESTAMP "YYYY-MM-DD HH24:MI:SS"' in ctl
    ctl2 = oracle_ctl("observation_period", schema["observation_period"])
    assert 'OBSERVATION_PERIOD_START_DATE DATE "YYYY-MM-DD"' in ctl2


@pytest.fixture()
def populated():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, birth_datetime, race_concept_id, "
                "ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1970, TIMESTAMP '1970-03-04 05:06:07.891', "
                "8527, 38003564, 'a,b \"quoted\"')")
    con.execute("INSERT INTO observation_period VALUES (1, 1, DATE '2020-01-01', DATE '2020-12-31', 32817)")
    return con


@pytest.mark.parametrize("target,files", [
    ("oracle", ["person.csv", "person.ctl", "omop_oracle_ddl.sql", "load_oracle.sh"]),
    ("sqlserver", ["person.csv", "omop_sqlserver_ddl.sql", "load_sqlserver.sql"]),
    ("snowflake", ["person.parquet", "omop_snowflake_ddl.sql", "load_snowflake.sql"]),
    ("bigquery", ["person.parquet", "omop_bigquery_ddl.sql", "load_bigquery.sh"]),
])
def test_export_cdm_writes_files_for_each_target(populated, tmp_path, target, files):
    res = export_cdm(populated, target, tmp_path, tables=["person", "observation_period"])
    for f in files:
        assert (tmp_path / f).exists(), f
    assert set(res["exported_tables"]) == {"person", "observation_period"}
    ddl = (tmp_path / f"omop_{target}_ddl.sql").read_text()
    assert ddl.count("CREATE TABLE") == 2 and lint_ddl(ddl, target) == []


def test_csv_round_trip_is_loadable_with_the_ctl_masks(populated, tmp_path):
    export_cdm(populated, "oracle", tmp_path, tables=["person", "observation_period"])
    row = duckdb.connect().execute(f"SELECT * FROM read_csv_auto('{(tmp_path / 'person.csv').as_posix()}', all_varchar=true)").fetchone()
    cols = [c.name for c in load_cdm_schema()["person"]]
    r = dict(zip(cols, row))
    assert r["birth_datetime"] == "1970-03-04 05:06:07"          # second precision, matches the TIMESTAMP mask
    assert r["person_source_value"] == 'a,b "quoted"'            # OPTIONALLY ENCLOSED BY '"' round-trips
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", duckdb.connect().execute(
        f"SELECT * FROM read_csv_auto('{(tmp_path / 'observation_period.csv').as_posix()}', all_varchar=true)").fetchone()[2])
    assert "BULK INSERT" not in (tmp_path / "load_oracle.sh").read_text()


def test_loader_scripts_content(populated, tmp_path):
    for t in ("sqlserver", "snowflake", "bigquery"):
        export_cdm(populated, t, tmp_path / t, tables=["person"])
    assert "FORMAT = 'CSV'" in (tmp_path / "sqlserver" / "load_sqlserver.sql").read_text()
    sf = (tmp_path / "snowflake" / "load_snowflake.sql").read_text()
    assert "PUT file://" in sf and "COPY INTO PERSON" in sf and "MATCH_BY_COLUMN_NAME" in sf
    assert "bq load --source_format=PARQUET" in (tmp_path / "bigquery" / "load_bigquery.sh").read_text()
    with pytest.raises(ValueError, match="Unsupported"):
        export_cdm(populated, "mysql", tmp_path / "x")
