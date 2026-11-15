"""Oracle -> DuckDB -> PostgreSQL -> Oracle round trip (omop_etl.oracle_roundtrip, scripts/oracle_roundtrip_check.py).

The offline tests pin the type mapping, the DuckDB fingerprint (order-free, and sensitive to any one changed value) and the saved
fingerprint file. The live tests need ``OMOP_TEST_ORACLE_URL`` (the live-db workflow starts Oracle Free 23 in a container) and load a
SYNTHETIC edge-case table: unconstrained NUMBER holding whole numbers, decimals and repeating fractions, 24-digit numbers, accented and
Japanese text in VARCHAR2 / NVARCHAR2, CLOB, DATE with times before 1970, TIMESTAMP(6) and TIMESTAMP(9), FLOAT, RAW and NULLs. The
PostgreSQL leg also needs ``pg0-embedded`` and a non-root user (PostgreSQL refuses to run as root).
"""

from __future__ import annotations

import decimal
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import oracle_roundtrip as rt  # noqa: E402
from omop_etl.oracle_roundtrip import OracleColumn, OracleTable  # noqa: E402


# ---------------------------------------------------------------- offline
@pytest.mark.parametrize("otype, p, s, kind, duck", [
    ("NUMBER", 10, 0, "int", "BIGINT"),
    ("NUMBER", 30, 0, "int", "DECIMAL(38,0)"),
    ("NUMBER", 12, 3, "decimal", "DECIMAL(12,3)"),
    ("BINARY_DOUBLE", None, None, "float", "DOUBLE"),
    ("DATE", None, None, "datetime", "TIMESTAMP"),
    ("TIMESTAMP(9)", None, 9, "datetime", "TIMESTAMP"),
    ("VARCHAR2", None, None, "text", "VARCHAR"),
    ("NVARCHAR2", None, None, "text", "VARCHAR"),
    ("CLOB", None, None, "lob", "VARCHAR"),
    ("RAW", None, None, "other", "BLOB"),
    ("INTERVAL DAY(2) TO SECOND(6)", None, 6, "other", "VARCHAR"),
])
def test_type_mapping(otype, p, s, kind, duck):
    c = rt._classify(OracleColumn(source="X", name="x", oracle_type=otype, precision=p, scale=s))
    assert (c.kind, c.duck_type) == (kind, duck)
    assert not c.probed


@pytest.mark.parametrize("probe, kind, duck, scale", [
    ((0, 9, None, 0), "int", "BIGINT", 0),               # whole numbers, 9 digits
    ((0, 24, None, 0), "int", "DECIMAL(38,0)", 0),       # whole numbers beyond BIGINT
    ((3, 5, 3, 0), "decimal", "DECIMAL(18,3)", 3),       # 12345.678
    ((3, 10, 25, 0), "decimal", "DECIMAL(38,25)", 25),
    ((3, 1, 40, 0), "float", "DOUBLE", None),            # 1/7: no exact DECIMAL(38, s)
    ((0, 0, None, 2), "float", "DOUBLE", None),          # scientific notation (1E-50)
    ((None, None, None, None), "int", "BIGINT", 0),      # empty table
])
def test_unconstrained_number_probe(probe, kind, duck, scale):
    t = OracleTable("O", "T", "t", [rt._classify(OracleColumn(source="U", name="u", oracle_type="NUMBER"))])
    assert t.columns[0].probed
    rt._apply_probe(t, probe)
    c = t.columns[0]
    assert (c.kind, c.duck_type) == (kind, duck)
    if scale is not None:
        assert c.scale == scale


def _plan():
    cols = [OracleColumn("ID", "id", "NUMBER", 10, 0), OracleColumn("D", "d", "NUMBER", 12, 3), OracleColumn("F", "f", "BINARY_DOUBLE"),
            OracleColumn("S", "s", "VARCHAR2"), OracleColumn("C", "c", "CLOB"), OracleColumn("T", "t", "DATE"), OracleColumn("R", "r", "RAW")]
    return OracleTable("O", "T", "t", [rt._classify(c) for c in cols])


def _duck(order="id"):
    con = duckdb.connect()
    con.execute("CREATE TABLE src AS SELECT i::BIGINT AS id, (i * 1.125)::DECIMAL(12,3) AS d, i / 3.0 AS f, "
                "CASE WHEN i % 5 = 0 THEN NULL ELSE 'café ' || i || ' 日本' END AS s, repeat('x', 1500) || i AS c, "
                "TIMESTAMP '1960-01-02 03:04:05' + to_seconds(i * 3700) AS t, '\\xAB'::BLOB AS r FROM range(500) r(i)")
    con.execute(f"CREATE TABLE t AS SELECT * FROM src ORDER BY {order}")
    return con


def test_duckdb_fingerprint_is_order_free_and_catches_one_change():
    plan = _plan()
    a = rt.fingerprint_duckdb(_duck("id"), plan)
    b = rt.fingerprint_duckdb(_duck("id DESC"), plan)
    assert rt.compare_fingerprints(plan, a, b)["match"].all()
    for change in ("UPDATE t SET s = 'cafe 7 日本' WHERE id = 7",            # one accent dropped
                   "UPDATE t SET d = d + 0.001 WHERE id = 9",
                   "UPDATE t SET t = t + INTERVAL 1 SECOND WHERE id = 11",
                   "UPDATE t SET id = 1000 WHERE id = 3",
                   "UPDATE t SET c = c || 'z' WHERE id = 4",                # beyond the hashed prefix: caught by the length
                   "DELETE FROM t WHERE id = 13"):
        con = _duck()
        con.execute(change)
        rep = rt.compare_fingerprints(plan, a, rt.fingerprint_duckdb(con, plan))
        assert not rep["match"].all(), change


def test_float_tolerance_and_nan():
    plan = OracleTable("O", "T", "t", [rt._classify(OracleColumn("F", "f", "BINARY_DOUBLE"))])
    base = {"(rows)": 3, "f:n": 3, "f:sum": 0.30000000000000004, "f:min": 0.1, "f:max": float("nan")}
    other = dict(base, **{"f:sum": 0.3})
    assert rt.compare_fingerprints(plan, base, other)["match"].all()
    assert not rt.compare_fingerprints(plan, base, dict(base, **{"f:sum": 0.31}))["match"].all()


def test_fingerprint_file_round_trip(tmp_path):
    plan = _plan()
    fp = rt.fingerprint_duckdb(_duck(), plan)
    fp["big"] = 2 ** 100                                 # exact integers beyond 64 bits survive
    fp["dec"] = decimal.Decimal("12345678901234567890.125")
    path = tmp_path / ("m.duckdb" + rt.FINGERPRINT_SUFFIX)
    rt._save_fingerprint(path, [plan], {"t": fp}, {"read_only_snapshot": True})
    plan2, fps, info = rt.load_fingerprint(tmp_path / "m.duckdb")
    assert plan2[0].columns == plan.columns and plan2[0].owner == "" and info["read_only_snapshot"] is True
    assert fps["t"]["big"] == 2 ** 100
    assert decimal.Decimal(str(fps["t"]["dec"])) == fp["dec"]
    assert rt.compare_fingerprints(plan, fp, fps["t"])["match"].all()
    assert '"owner": ""' in path.read_text()            # the Oracle owner (schema) is not saved


def test_script_refuses_without_url(monkeypatch, tmp_path):
    monkeypatch.delenv("OMOP_ORACLE_URL", raising=False)
    r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "oracle_roundtrip_check.py"), "--out-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "OMOP_ORACLE_URL is not set" in r.stderr


# ---------------------------------------------------------------- live Oracle
URL = os.environ.get("OMOP_TEST_ORACLE_URL")
live = pytest.mark.skipif(not URL, reason="OMOP_TEST_ORACLE_URL not set")

EDGE_DDL = """CREATE TABLE {name} (id NUMBER(10), u NUMBER, uint NUMBER, ubig NUMBER, d NUMBER(12,3), f BINARY_DOUBLE,
 s VARCHAR2(400), ch CHAR(5), ns NVARCHAR2(40), c CLOB, dt DATE, ts TIMESTAMP(6), ts9 TIMESTAMP(9), fl FLOAT, big NUMBER(30), r RAW(16))"""
EDGE_ROWS = """INSERT INTO {name} SELECT level, level / 7, level * 3, 123456789012345678901234 + level, level * 1.125, level / 3,
 'café ' || level || ' ñ', 'ab', N'日本' || level, TO_CLOB(RPAD('x', 2000, 'y') || level), DATE '1960-01-02' + level + level / 86400,
 TIMESTAMP '1999-12-31 23:59:59.123456' + NUMTODSINTERVAL(level, 'SECOND'), TIMESTAMP '2000-01-01 00:00:00.123456', level * 0.5, 10 * level,
 HEXTORAW('ABCD') FROM dual CONNECT BY level <= 1500"""


def _ora():
    import oracledb  # noqa: F401
    from omop_etl.oracle_roundtrip import _driver_connection

    last = None
    for _ in range(60):
        try:
            return _driver_connection(URL)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    pytest.fail(f"Oracle did not become ready: {type(last).__name__}")


@pytest.fixture(scope="module")
def edge_table():
    pytest.importorskip("oracledb")
    engine, raw, con = _ora()
    name = f"OMOP_EDGE_{os.getpid() % 100000}"
    cur = con.cursor()
    try:
        cur.execute(f"DROP TABLE {name} PURGE")
    except Exception:  # noqa: BLE001
        pass
    cur.execute(EDGE_DDL.format(name=name))
    cur.execute(EDGE_ROWS.format(name=name))
    cur.execute(f"INSERT INTO {name} (id) VALUES (0)")
    con.commit()
    time.sleep(3)   # a read-only transaction started within a second or so of the CREATE gets ORA-01466
    yield name
    cur.execute(f"DROP TABLE {name} PURGE")
    raw.close()
    engine.dispose()


@live
def test_mirror_matches_oracle_row_level_and_types(edge_table, tmp_path):
    out = tmp_path / "site_a.duckdb"
    rep = rt.mirror_oracle(URL, out, tables=[f"edge={edge_table}"], row_level=True)
    assert rep["match"].all(), rep.loc[~rep["match"]].to_dict("records")
    assert (rep["stat"] == "row_level").sum() == 1
    types = dict(duckdb.connect(str(out), read_only=True).execute("SELECT column_name, data_type FROM information_schema.columns "
                                                                   "WHERE table_name = 'edge'").fetchall())
    assert types["id"] == "BIGINT" and types["uint"] == "BIGINT" and types["ubig"] == "DECIMAL(38,0)"
    assert types["d"] == "DECIMAL(12,3)" and types["u"] == "DOUBLE" and types["fl"] == "DECIMAL(18,1)" and types["dt"] == "TIMESTAMP"
    con = duckdb.connect(str(out), read_only=True)
    assert con.execute("SELECT s, ns FROM edge WHERE id = 1").fetchone() == ("café 1 ñ", "日本1")
    assert con.execute("SELECT ubig FROM edge WHERE id = 1").fetchone()[0] == decimal.Decimal("123456789012345678901235")
    con.close()
    assert rt.verify_duckdb_copy(out)["match"].all()
    with pytest.raises(FileExistsError):
        rt.mirror_oracle(URL, out, tables=[f"edge={edge_table}"])


@live
def test_tampered_copy_fails(edge_table, tmp_path):
    out = tmp_path / "a.duckdb"
    rt.mirror_oracle(URL, out, tables=[f"edge={edge_table}"])
    con = duckdb.connect(str(out))
    con.execute("UPDATE edge SET s = 'cafe 5 ñ' WHERE id = 5")
    con.close()
    rep = rt.verify_duckdb_copy(out)
    assert set(rep.loc[~rep["match"], "column"]) == {"s"}


@live
def test_read_only_snapshot_ignores_rows_loaded_meanwhile(edge_table, tmp_path):
    from omop_etl.oracle_roundtrip import _driver_connection

    def insert_now(msg):
        if msg.endswith(": copy"):
            e, r, c = _driver_connection(URL)
            c.cursor().execute(f"INSERT INTO {edge_table} (id) VALUES (99999)")
            c.commit()
            r.close()
            e.dispose()

    try:
        rep = rt.mirror_oracle(URL, tmp_path / "a.duckdb", tables=[f"edge={edge_table}"], progress=insert_now)
        assert rep["match"].all()
        rep = rt.mirror_oracle(URL, tmp_path / "b.duckdb", tables=[f"edge={edge_table}"], progress=insert_now, snapshot=False)
        assert not rep["match"].all()               # without the snapshot the copy sees the new row, Oracle's fingerprint did not
    finally:
        e, r, c = _driver_connection(URL)
        c.cursor().execute(f"DELETE FROM {edge_table} WHERE id = 99999")
        c.commit()
        r.close()
        e.dispose()


@live
def test_nanoseconds_are_reported_not_hidden(tmp_path):
    from omop_etl.oracle_roundtrip import _driver_connection

    e, r, c = _driver_connection(URL)
    name = f"OMOP_NS_{os.getpid() % 100000}"
    cur = c.cursor()
    cur.execute(f"CREATE TABLE {name} (id NUMBER(5), t TIMESTAMP(9))")
    cur.execute(f"INSERT INTO {name} VALUES (1, TIMESTAMP '2000-01-01 00:00:00.123456789')")
    c.commit()
    time.sleep(3)
    try:
        rep = rt.mirror_oracle(URL, tmp_path / "n.duckdb", tables=[name])
        assert rep.loc[~rep["match"], ["column", "stat"]].values.tolist() == [["t", "sub_us"]]
    finally:
        cur.execute(f"DROP TABLE {name} PURGE")
        r.close()
        e.dispose()


@live
def test_write_back_to_oracle(edge_table, tmp_path):
    out = tmp_path / "w.duckdb"
    rt.mirror_oracle(URL, out, tables=[f"edge={edge_table}"])
    rep = rt.write_back(URL, out, prefix=f"RT{os.getpid() % 10000}_")
    assert rep["match"].all(), rep.loc[~rep["match"]].to_dict("records")


@live
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="PostgreSQL refuses to run as root")
def test_script_with_postgres_backup_and_read_back(edge_table, tmp_path):
    pytest.importorskip("pg0")
    env = dict(os.environ, OMOP_ORACLE_URL=URL)
    rec = tmp_path / "record.md"
    r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "oracle_roundtrip_check.py"), "--tables", f"edge={edge_table}",
                        "--label", "Site C", "--out-dir", str(tmp_path / "m"), "--pg-dir", str(tmp_path / "pg"), "--read-back",
                        "--row-level", "--skip-duckdb-pg-check", "--out", str(rec)], capture_output=True, text=True, env=env, timeout=600)
    text = rec.read_text(encoding="utf-8")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert text.count("Result: **PASS**") == 4 and "FAIL" not in text
    assert "skipped (--skip-duckdb-pg-check)" in text
    assert edge_table not in text and "1501" not in text and "omop_ci_pw" not in text and "localhost" not in text
    assert (tmp_path / "m" / "site_c.duckdb").exists() and (tmp_path / "m" / "site_c_from_postgres.duckdb").exists()


def test_num_parses_oracle_text_exactly():
    assert rt._num("12345678901234567890123456789") == 12345678901234567890123456789
    assert rt._num("-.5") == decimal.Decimal("-0.5")
    assert rt._num("1.5E+000") == 1 or rt._num("1.5E+000") == decimal.Decimal("1.5")
    assert rt._num("Nan") != rt._num("Nan")
    assert rt._num("Inf") == float("inf") and rt._num("-Inf") == float("-inf")
    assert rt._num(decimal.Decimal("3.0")) == 3 and rt._num(True) == 1 and rt._num(None) is None


@live
def test_describe_all_tables_map_errors_and_charset_conversion(edge_table):
    from omop_etl.oracle_roundtrip import _driver_connection

    e, r, c = _driver_connection(URL)
    cur = c.cursor()
    try:
        plan = rt.describe_oracle(cur, "all", table_map={"edge_copy": edge_table}, probe=False)
        t = next(p for p in plan if p.source == edge_table)
        assert t.name == "edge_copy"
        assert {x.name: x.duck_type for x in t.columns}["u"] == "DECIMAL(38,0)"   # not probed: assumed whole numbers
        with pytest.raises(LookupError):
            rt.describe_oracle(cur, ["no_such_table_xyz"])
        with pytest.raises(ValueError, match="same DuckDB table name"):
            rt.describe_oracle(cur, [f"a={edge_table}", f"A={edge_table}"])
        with pytest.raises(ValueError, match="letters, digits"):
            rt.describe_oracle(cur, [f"bad-name={edge_table}"])
        # the CONVERT(..., 'AL32UTF8') path used on a non-Unicode database gives the same fingerprint here (AL32UTF8 database)
        t = rt.describe_oracle(cur, [f"edge={edge_table}"])[0]
        assert rt.fingerprint_oracle(cur, t, unicode_db=False) == rt.fingerprint_oracle(cur, t, unicode_db=True)
        assert rt.fingerprint_oracle(cur, t) == rt.fingerprint_oracle(cur, t, unicode_db=True)
    finally:
        r.close()
        e.dispose()
    with pytest.raises(ValueError, match="reads Oracle"):
        rt._driver_connection("sqlite://")


@live
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="PostgreSQL refuses to run as root")
def test_postgres_copy_matches_oracle_in_process(edge_table, tmp_path):
    pytest.importorskip("pg0")
    from omop_etl.embedded_postgres import EmbeddedPostgres, store_site

    out = tmp_path / "p.duckdb"
    rt.mirror_oracle(URL, out, tables=[f"edge={edge_table}"])
    with EmbeddedPostgres(tmp_path / "pg") as pg:
        store_site(pg, out, "oracle_site_a")
        assert rt.verify_postgres_copy(pg, "oracle_site_a", out)["match"].all()
        conn = pg.connect()
        try:
            conn.cursor().execute('UPDATE oracle_site_a.edge SET s = \'x\' WHERE id = 3')
            conn.commit()
            rep = rt.verify_postgres_copy(conn, "oracle_site_a", out)
        finally:
            conn.close()
        assert set(rep.loc[~rep["match"], "column"]) == {"s"}
