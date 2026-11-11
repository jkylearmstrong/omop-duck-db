"""Offline Oracle tests: no Oracle server or driver needed, so they run in every default ``pytest tests`` job.

They complement ``test_live_databases.py`` (which needs ``OMOP_TEST_ORACLE_URL``) by pinning the Oracle-specific
behaviour that can be checked without a server:

* reflected Oracle column types (``NUMBER(p,0)``, ``NUMBER(p,s)``, bare ``NUMBER``, ``DATE``, ``VARCHAR2``, ``CLOB``)
  map to the right Arrow types;
* Oracle folds unquoted identifiers to UPPER CASE: ``from_sql`` finds the table case-insensitively and writes
  lower-case columns/files (emulated here with a SQLite table created with upper-case quoted names);
* credentials never appear in error messages for an ``oracle+oracledb://`` URL, and ``env:NAME`` resolves;
* the generated Oracle DDL passes ``lint_ddl`` and respects the 30-character identifier limit;
* cohort SQL translation to Oracle rewrites ``DATEADD``.

Passing here does not prove behaviour on a real Oracle server; that is the live-db workflow's job.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

sa = pytest.importorskip("sqlalchemy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from sqlalchemy.dialects import oracle as ora  # noqa: E402

from omop_etl import extract  # noqa: E402
from omop_etl.extract import from_sql  # noqa: E402


# ---------------------------------------------------------------- type mapping
@pytest.mark.parametrize(
    "col_type, expected",
    [
        (ora.NUMBER(10, 0), pa.int64()),
        (ora.NUMBER(18, 0), pa.int64()),
        (ora.NUMBER(10, 2), pa.decimal128(10, 2)),
        (ora.NUMBER(), pa.float64()),          # NUMBER without precision
        (ora.DATE(), pa.timestamp("us")),      # Oracle DATE carries a time part
        (ora.TIMESTAMP(), pa.timestamp("us")),
        (ora.VARCHAR2(40), pa.string()),
        (ora.CLOB(), pa.string()),
        (ora.FLOAT(), pa.float64()),
    ],
)
def test_oracle_reflected_types_map_to_arrow(col_type, expected):
    assert extract._arrow_type(col_type) == expected


def test_unknown_oracle_type_falls_back_to_string_conversion():
    assert extract._arrow_type(ora.INTERVAL()) is None
    arr = extract._convert([dt.timedelta(days=1), None], None)
    assert arr.type == pa.string() and arr[1].as_py() is None


# ------------------------------------------- upper-case identifiers (SQLite stand-in)
@pytest.fixture
def upper_case_db(tmp_path):
    """SQLite file with an Oracle-style upper-case table and columns (created with quoted identifiers)."""
    path = tmp_path / "src.db"
    url = f"sqlite:///{path.as_posix()}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text(
            'CREATE TABLE "PRESCRIBING" ("PRESCRIBINGID" INTEGER, "RXNORM_CUI" VARCHAR(8), '
            '"RX_ORDER_DATE" DATE, "RX_DOSE_ORDERED" NUMERIC(10, 2))'))
        c.execute(sa.text('INSERT INTO "PRESCRIBING" VALUES (:i, :c, :d, :q)'), [
            {"i": i, "c": None if i % 3 == 0 else f"C{i}", "d": dt.date(2020, 1, 1) + dt.timedelta(days=i),
             "q": None if i % 4 == 0 else i / 4} for i in range(1, 26)])
    eng.dispose()
    return url


def test_case_insensitive_table_lookup_and_lowercase_columns(upper_case_db, tmp_path):
    rep = from_sql(upper_case_db, ["prescribing"], tmp_path / "out", chunksize=7)  # 25 rows, 4 chunks
    assert rep[0]["source_table"] == "PRESCRIBING"
    assert rep[0]["table"] == "prescribing" and rep[0]["rows"] == 25
    assert list(rep[0]["columns"]) == ["prescribingid", "rxnorm_cui", "rx_order_date", "rx_dose_ordered"]
    t = pq.read_table(rep[0]["file"])
    assert t.num_rows == 25
    assert t.column("rxnorm_cui").null_count == 8
    assert t.column("rx_dose_ordered").null_count == 6


def test_table_map_selects_site_specific_name(upper_case_db, tmp_path):
    rep = from_sql(upper_case_db, ["prescribing"], tmp_path / "out", table_map={"prescribing": "PRESCRIBING"})
    assert rep[0]["rows"] == 25


def test_missing_table_is_a_lookup_error(upper_case_db, tmp_path):
    with pytest.raises(LookupError, match="not found"):
        from_sql(upper_case_db, ["dispensing"], tmp_path / "out")


# ----------------------------------------------------------------- credentials
SECRET = "S3cr3t-Pw!"
ORACLE_URL = f"oracle+oracledb://kyle:S3cr3t-Pw%21@127.0.0.1:1/?service_name=NOPE"  # secret-scan:allow (fake)


def test_oracle_url_password_never_leaks_in_errors(tmp_path):
    with pytest.raises(Exception) as ei:  # driver missing or connection refused: either way, no secret
        from_sql(ORACLE_URL, ["person"], tmp_path / "out")
    assert SECRET not in str(ei.value) and "S3cr3t" not in str(ei.value)


def test_env_url_is_resolved_and_missing_env_is_reported(monkeypatch):
    monkeypatch.setenv("ORA_URL_TEST", ORACLE_URL)
    assert extract.resolve_url("env:ORA_URL_TEST") == ORACLE_URL
    monkeypatch.delenv("ORA_URL_TEST")
    with pytest.raises(ValueError, match="ORA_URL_TEST"):
        extract.resolve_url("env:ORA_URL_TEST")


def test_masked_url_hides_password():
    assert SECRET not in extract._masked(ORACLE_URL) and "***" in extract._masked(ORACLE_URL)


# ------------------------------------------------------------- DDL and cohort SQL
def test_generated_oracle_ddl_lints_clean_and_respects_identifier_limit():
    from omop_etl.export_targets import generate_ddl, lint_ddl, load_cdm_schema

    model = load_cdm_schema()
    ddl = generate_ddl("oracle", list(model), schema=model)
    assert lint_ddl(ddl, "oracle") == []
    longest = max(len(c.name) for cols in model.values() for c in cols)
    assert longest <= 30, "a CDM column exceeds Oracle's pre-12.2 identifier limit"


def test_cohort_sql_oracle_rewrites_dateadd():
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import translate_cohort_sql

    out = translate_cohort_sql("SELECT DATEADD(day, 30, start_date) AS d FROM t", "oracle")
    out = " ".join(out) if isinstance(out, (list, tuple)) else out  # returns one statement per input statement
    assert "DATEADD" not in out.upper() and "30" in out
