"""DuckDB <-> embedded PostgreSQL (omop_etl.embedded_postgres): the server starts with no install, a site's CDM goes in and comes back
identical, the study cohort is the same from either copy, and raw text files read by PostgreSQL's COPY match DuckDB's read_csv.

Synthetic data only. The server tests run when ``pg0-embedded`` (or ``pgembed``) is installed and the user is not root, which
PostgreSQL refuses; CI runs them in the ``embedded-postgres`` job.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.embedded_postgres import (  # noqa: E402
    EmbeddedPostgres,
    RoundTripError,
    _libpq_kv,
    _with_database,
    compare_duckdb,
    list_site_schemas,
    load_site,
    load_text_files,
    site_schema,
    store_site,
)

BACKENDS = [b for b, m in (("pg0", "pg0"), ("pgembed", "pgembed")) if importlib.util.find_spec(m) is not None]
IS_ROOT = os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0
needs_pg = pytest.mark.skipif(not BACKENDS or IS_ROOT or importlib.util.find_spec("psycopg2") is None,
                              reason="needs pg0-embedded or pgembed, psycopg2 and a non-root user")
EDGE_SQL = """
CREATE TABLE study_extra AS
SELECT i::BIGINT AS id, (i % 2 = 0) AS flag,
       CASE i % 5 WHEN 0 THEN 'nan'::DOUBLE WHEN 1 THEN 'inf'::DOUBLE WHEN 2 THEN '-inf'::DOUBLE WHEN 3 THEN NULL ELSE i * 0.25 END AS x,
       (i * 1.2345)::DECIMAL(18, 4) AS dec,
       CASE i % 4 WHEN 0 THEN '' WHEN 1 THEN NULL WHEN 2 THEN 'café, "quoted"' || chr(10) || 'line 2' ELSE '日本語 ' || i END AS txt,
       DATE '1955-03-01' + i::INTEGER AS d,
       TIMESTAMP '1999-12-31 23:59:59.123456' + to_seconds(i) AS ts
FROM range(120) t(i)
"""


# ---------------------------------------------------------------------------------------------------------------------- no server
def test_required_backend_is_installed_in_ci():
    """CI sets OMOP_REQUIRE_EMBEDDED_PG so a broken install fails the job instead of skipping every server test."""
    want = os.environ.get("OMOP_REQUIRE_EMBEDDED_PG")
    if not want:
        pytest.skip("OMOP_REQUIRE_EMBEDDED_PG not set")
    assert want in BACKENDS and not IS_ROOT


def test_site_schema_names():
    assert site_schema("Site A") == "site_a"
    assert site_schema("Site AB") == "site_ab"
    assert site_schema(" 3rd site ") == "s_3rd_site"
    with pytest.raises(ValueError):
        site_schema("!!!")


def test_uri_helpers_keep_a_socket_host_and_change_only_the_database():
    assert _with_database("postgresql://u:p@127.0.0.1:5432/postgres", "omop") == "postgresql://u:p@127.0.0.1:5432/omop"  # secret-scan:allow (dummy URL in a unit test)
    assert _with_database("postgresql://postgres:@/postgres?host=/tmp/x", "omop") == "postgresql://postgres:@/omop?host=/tmp/x"
    kv = _libpq_kv("postgresql://postgres:@/omop?host=/tmp/x")
    assert kv == "host=/tmp/x dbname=omop user=postgres"
    assert _libpq_kv("postgresql://u:p%40w@127.0.0.1:5999/omop") == "host=127.0.0.1 port=5999 dbname=omop user=u password=p@w"  # secret-scan:allow (dummy URL in a unit test)


def test_bad_names_are_refused_before_anything_runs(tmp_path):
    with pytest.raises(ValueError):
        EmbeddedPostgres(tmp_path, backend="nope")
    with pytest.raises(ValueError):
        store_site("postgresql+psycopg2://x@localhost/x", tmp_path / "a.duckdb", "Site A")  # not a schema name


def test_repr_never_shows_a_password(tmp_path):
    if not BACKENDS:
        pytest.skip("no embedded backend installed")
    assert "password" not in repr(EmbeddedPostgres(tmp_path))


# ---------------------------------------------------------------------------------------------------------------------- fixtures
def _site_db(path: Path, persons: set[int] | None = None) -> Path:
    from test_define_study_cohort import load_fixture_cdm

    con = duckdb.connect(str(path))
    load_fixture_cdm(con)
    if persons is not None:
        for t in ("person", "visit_occurrence", "death", "observation_period", "measurement", "condition_occurrence", "drug_exposure"):
            con.execute(f"DELETE FROM {t} WHERE person_id NOT IN (SELECT unnest(?::BIGINT[]))", [sorted(persons)])
    con.execute(EDGE_SQL)
    con.close()
    return path


@pytest.fixture(scope="module", params=BACKENDS or ["none"])
def pg(request, tmp_path_factory):
    if request.param == "none" or IS_ROOT:
        pytest.skip("needs pg0-embedded or pgembed and a non-root user")
    server = EmbeddedPostgres(tmp_path_factory.mktemp(f"pg_{request.param}"), backend=request.param)
    server.start()
    yield server
    server.drop()


# ---------------------------------------------------------------------------------------------------------------------- server
@needs_pg
def test_site_goes_to_postgres_and_comes_back_identical(pg, tmp_path):
    src = _site_db(tmp_path / "omop_x.duckdb")
    rep = store_site(pg, src, "site_a")
    assert bool(rep["match"].all())
    assert any(c.endswith(":content") for c in rep["column"])          # value-level comparison ran
    assert "site_a" in list_site_schemas(pg)
    back = tmp_path / "back" / "omop_x.duckdb"
    load_site(pg, "site_a", back)
    cmp = compare_duckdb(src, back)
    assert bool(cmp["match"].all()), cmp[~cmp["match"]]
    assert set(cmp.loc[cmp.rows_a > 0, "table"]) >= {"person", "visit_occurrence", "measurement", "study_extra"}
    with pytest.raises(FileExistsError):                                  # never overwrites
        load_site(pg, "site_a", back)


@needs_pg
def test_insert_method_gives_the_same_copy(pg, tmp_path):
    src = _site_db(tmp_path / "omop_y.duckdb")
    rep = store_site(pg, src, "site_b", tables=["person", "study_extra"], method="insert")
    assert bool(rep["match"].all())


@needs_pg
def test_study_cohort_is_the_same_from_the_postgres_copy(pg, tmp_path):
    from omop_etl.cohort import define_study_cohort
    from test_define_study_cohort import RFC_KWARGS

    src = _site_db(tmp_path / "omop_z.duckdb")
    store_site(pg, src, "site_c", tables="cdm")
    back = tmp_path / "omop_z_back.duckdb"
    load_site(pg, "site_c", back, tables="cdm")
    kw = {**RFC_KWARGS, "materialise": False}
    a = define_study_cohort(duckdb.connect(str(src), read_only=True), **kw)
    b = define_study_cohort(duckdb.connect(str(back), read_only=True), **kw)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


@needs_pg
def test_a_changed_value_is_caught(pg, tmp_path):
    src = _site_db(tmp_path / "omop_w.duckdb")
    store_site(pg, src, "site_d", tables=["person", "study_extra"])
    pg.query("UPDATE site_d.study_extra SET txt = 'changed' WHERE id = 3")
    back = tmp_path / "omop_w_back.duckdb"
    load_site(pg, "site_d", back)
    cmp = compare_duckdb(src, back, tables=["person", "study_extra"]).set_index("table")
    assert bool(cmp.loc["person", "match"]) and not bool(cmp.loc["study_extra", "match"])
    assert cmp.loc["study_extra", "only_in_a"] == 1 and cmp.loc["study_extra", "only_in_b"] == 1


@needs_pg
def test_data_and_password_survive_a_restart(tmp_path):
    root = tmp_path / "pgdir"
    with EmbeddedPostgres(root, backend=BACKENDS[0]) as s:
        s.query("CREATE TABLE keep_me AS SELECT 42 AS v")
    with EmbeddedPostgres(root, backend=BACKENDS[0]) as s:
        assert int(s.query("SELECT v FROM keep_me").iloc[0, 0]) == 42
        if s.backend == "pg0":
            assert "127.0.0.1" in s.dsn
    EmbeddedPostgres(root, backend=BACKENDS[0]).drop()
    assert not (root / "pgdata").exists()


@needs_pg
def test_raw_text_files_copy_matches_duckdb_read_csv(pg, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for f in (ROOT / "tests" / "fixtures" / "pcornet_sample").glob("*.csv"):
        (raw / f.name).write_bytes(f.read_bytes())
    # a pipe-delimited extract with quotes, an embedded delimiter and newline, non-Latin text, empty fields and a byte-order mark
    (raw / "encounter_extra.csv").write_bytes(   # bytes: text mode on Windows would turn \n into \r\n
        '﻿ENCOUNTERID|PATID|NOTE|ADMIT_DATE\n'
        'E1|P1|"has | pipe"|2020-01-02\n'
        'E2|P2|"two\nlines"|\n'
        'E3|P3|日本語|2021-05-06\n'
        'E4||"say ""hi"""|2019-12-31\n'.encode("utf-8"))
    rep = load_text_files(pg, raw, "raw_site_a")
    assert bool(rep["match"].all())
    assert rep.attrs["files"]["encounter_extra.csv"] == "encounter_extra"
    got = pg.query("SELECT note FROM raw_site_a.encounter_extra ORDER BY encounterid")["note"].tolist()
    assert got == ["has | pipe", "two\nlines", "日本語", 'say "hi"']


@needs_pg
def test_attach_from_duckdb(pg, tmp_path):
    con = duckdb.connect()
    try:
        pg.attach_to(con)
    except duckdb.Error as exc:  # the postgres extension is downloaded by DuckDB; offline machines cannot
        pytest.skip(f"DuckDB postgres extension unavailable: {type(exc).__name__}")
    store_site(pg, _site_db(tmp_path / "omop_v.duckdb"), "site_e", tables=["person"])
    assert con.execute("SELECT count(*) FROM pg.site_e.person").fetchone()[0] == 260


@needs_pg
def test_script_checks_any_number_of_sites_with_raw_files_and_delta(tmp_path, capsys):
    import postgres_roundtrip_check as script

    pytest.importorskip("deltalake")
    db = tmp_path / "db"
    db.mkdir()
    sites = ["northfield", "southport", "eastvale"]                     # made-up keys; the record must not name them
    persons = duckdb.connect().execute(f"SELECT person_id FROM read_csv('{(ROOT / 'tests/fixtures/study_cohort_cdm/person.csv').as_posix()}')").df()
    ids = persons.person_id.tolist()
    raws = []
    for k, s in enumerate(sites):
        _site_db(db / f"omop_{s}.duckdb", set(ids[k::3]))
        r = tmp_path / f"raw_{s}"
        r.mkdir()
        (r / "demographic.csv").write_bytes((ROOT / "tests/fixtures/pcornet_sample/demographic.csv").read_bytes())
        raws.append(str(r))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"northfield": "Site C", "southport": "Site A", "eastvale": "Site B"}))
    out = tmp_path / "record.md"
    rc = script.main(["--db-dir", str(db), "--sites", *sites, "--labels-file", str(labels), "--pg-dir", str(tmp_path / "pg"),
                      "--out-db-dir", str(tmp_path / "back"), "--raw-dirs", *raws, "--delta-dir", str(tmp_path / "delta"),
                      "--delta-tables", "person", "study_extra", "--backend", BACKENDS[0], "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert rc == 0, text
    assert "Result: **PASS**" in text and text.count("**PASS**") == 1 + 4 * 3
    assert all(f"## Site {c}" in text for c in "ABC")
    assert not any(s in text.lower() for s in sites)
    for s in sites:
        assert compare_duckdb(db / f"omop_{s}.duckdb", tmp_path / "back" / f"omop_{s}.duckdb")["match"].all()
    # a second run refuses to overwrite the files read back
    with pytest.raises(SystemExit):
        script.main(["--db-dir", str(db), "--sites", *sites, "--pg-dir", str(tmp_path / "pg"), "--out-db-dir", str(tmp_path / "back")])
    EmbeddedPostgres(tmp_path / "pg", backend=BACKENDS[0]).drop()
