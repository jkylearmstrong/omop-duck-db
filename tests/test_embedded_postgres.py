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


def test_generated_password_is_safe_on_the_command_line(tmp_path):
    """pg0 passes the password as a command-line argument; one starting with '-' was read as an option (seen in CI)."""
    if not BACKENDS:
        pytest.skip("no embedded backend installed")
    import re

    for i in range(64):
        pw = EmbeddedPostgres(tmp_path / str(i))._password()
        assert re.fullmatch(r"[0-9a-f]{48}", pw)
        assert EmbeddedPostgres(tmp_path / str(i))._password() == pw     # stable across restarts


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


# ---------------------------------------------------------------------------------------------------------------------- backups
from omop_etl.embedded_postgres import (  # noqa: E402
    backup_raw_files,
    backup_site,
    backup_status,
    drop_backup,
    register_backup,
    restore_raw_files,
    restore_site,
    verify_backup,
)


def test_backup_schema_names_are_checked_before_anything_runs():
    for bad in ("omop_backup", "site_a__new", "site_a__old", "x" * 60, "Site A"):
        with pytest.raises(ValueError):
            backup_site("postgresql+psycopg2://x@localhost/x", "missing.duckdb", bad)


@needs_pg
def test_backup_is_a_full_copy_that_verifies_and_restores_identically(pg, tmp_path):
    src = _site_db(tmp_path / "omop_bk.duckdb")
    con = duckdb.connect(str(src))
    con.execute("CREATE VIEW adults AS SELECT person_id FROM person WHERE year_of_birth < 2000")
    con.execute("CREATE VIEW adult_count AS SELECT count(*) AS n FROM adults")       # a view on a view
    con.close()
    rep = backup_site(pg, src, "bk_a", label="Site A")
    assert rep.attrs["backup"]["status"] == "created" and bool(rep["match"].all())
    src_tables = set(duckdb.connect(str(src), read_only=True).execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'").df()["table_name"])
    st = backup_status(pg).set_index("schema")
    assert st.loc["bk_a", "kind"] == "duckdb" and st.loc["bk_a", "label"] == "Site A"
    assert st.loc["bk_a", "tables"] == len(src_tables) and st.loc["bk_a", "views"] == 2            # every table: a full copy
    assert "bk_a__new" not in set(pg.query("SELECT schema_name FROM information_schema.schemata")["schema_name"])
    v = verify_backup(pg, "bk_a")
    assert bool(v["match"].all()) and set(v["table"]) == src_tables
    v = verify_backup(pg, "bk_a", against=src)
    assert bool(v["match"].all())
    out = tmp_path / "restored" / "omop_bk.duckdb"
    res = restore_site(pg, "bk_a", out)
    assert bool(res["match"].all())
    cmp = compare_duckdb(src, out)
    assert bool(cmp["match"].all()), cmp[~cmp["match"]]
    r = duckdb.connect(str(out), read_only=True)
    assert r.execute("SELECT n FROM adult_count").fetchone() == duckdb.connect(str(src), read_only=True).execute(
        "SELECT n FROM adult_count").fetchone()
    r.close()
    with pytest.raises(FileExistsError):
        restore_site(pg, "bk_a", out)


@needs_pg
def test_verify_catches_a_changed_dropped_or_added_table(pg, tmp_path):
    src = _site_db(tmp_path / "omop_bv.duckdb")
    backup_site(pg, src, "bk_v", tables=["person", "study_extra", "measurement"])
    pg.query("UPDATE bk_v.study_extra SET txt = 'changed' WHERE id = 3")
    pg.query("DELETE FROM bk_v.measurement WHERE measurement_id IN (SELECT min(measurement_id) FROM bk_v.measurement)")
    pg.query("CREATE TABLE bk_v.stray AS SELECT 1 AS x")
    v = verify_backup(pg, "bk_v").set_index("table")
    assert bool(v.loc["person", "match"])
    assert v.loc["study_extra", "note"] == "values changed"
    assert v.loc["measurement", "note"] == "row count changed"
    assert v.loc["stray", "note"] == "table not in the backup record"
    before = backup_status(pg).set_index("schema").loc["bk_v", "verified_at"]
    verify_backup(pg, "bk_v")
    assert backup_status(pg).set_index("schema").loc["bk_v", "verified_at"] == before            # not marked verified
    with pytest.raises(RoundTripError):
        restore_site(pg, "bk_v", tmp_path / "bad.duckdb")


@needs_pg
def test_refresh_replaces_the_copy_and_skips_when_unchanged(pg, tmp_path):
    src = _site_db(tmp_path / "omop_br.duckdb")
    assert backup_site(pg, src, "bk_r", tables="cdm").attrs["backup"]["status"] == "created"
    assert backup_site(pg, src, "bk_r", tables="cdm", if_changed=True).attrs["backup"]["status"] == "unchanged"
    con = duckdb.connect(str(src))
    con.execute("DELETE FROM person WHERE person_id IN (SELECT max(person_id) FROM person)")
    con.close()
    assert backup_site(pg, src, "bk_r", tables="cdm", if_changed=True).attrs["backup"]["status"] == "refreshed"
    assert bool(verify_backup(pg, "bk_r", against=src)["match"].all())
    n_src = duckdb.connect(str(src), read_only=True).execute("SELECT count(*) FROM person").fetchone()[0]
    assert int(pg.query("SELECT count(*) FROM bk_r.person").iloc[0, 0]) == n_src


@needs_pg
def test_a_failed_refresh_keeps_the_previous_backup(pg, tmp_path, monkeypatch):
    import omop_etl.embedded_postgres as ep

    src = _site_db(tmp_path / "omop_bf.duckdb")
    backup_site(pg, src, "bk_f", tables=["person", "study_extra"])
    real = ep.store_site

    def broken(pg_, con, schema, **kw):
        real(pg_, con, schema, **kw)
        raise RoundTripError("simulated mismatch")

    monkeypatch.setattr(ep, "store_site", broken)
    with pytest.raises(RoundTripError):
        backup_site(pg, src, "bk_f", tables=["person", "study_extra"])
    schemas = set(pg.query("SELECT schema_name FROM information_schema.schemata")["schema_name"])
    assert "bk_f" in schemas and "bk_f__new" not in schemas
    assert bool(verify_backup(pg, "bk_f", against=src)["match"].all())


@needs_pg
def test_register_adopts_a_round_trip_copy_and_refuses_a_different_one(pg, tmp_path):
    src = _site_db(tmp_path / "omop_bg.duckdb")
    store_site(pg, src, "bk_g", tables=["person", "study_extra"])
    rep = register_backup(pg, "bk_g", src, label="Site G")
    assert rep.attrs["backup"]["status"] == "registered" and "measurement" in rep.attrs["backup"]["missing_tables"]
    assert bool(verify_backup(pg, "bk_g")["match"].all())
    store_site(pg, src, "bk_h", tables=["person", "study_extra"])
    pg.query("UPDATE bk_h.study_extra SET txt = 'changed' WHERE id = 3")
    with pytest.raises(RoundTripError):
        register_backup(pg, "bk_h", src)
    assert "bk_h" not in set(backup_status(pg)["schema"])
    drop_backup(pg, "bk_g")
    assert "bk_g" not in set(backup_status(pg)["schema"])
    assert "bk_g" not in list_site_schemas(pg)


@needs_pg
def test_raw_files_backup_verifies_against_the_files_and_restores(pg, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for f in (ROOT / "tests" / "fixtures" / "pcornet_sample").glob("*.csv"):
        (raw / f.name).write_bytes(f.read_bytes())
    (raw / "note.csv").write_bytes('ID|TXT\n1|"a | b"\n2|"two\nlines"\n3|日本語\n4|\n'.encode("utf-8"))
    rep = backup_raw_files(pg, raw, "raw_bk_a", label="Site A")
    assert rep.attrs["backup"]["status"] == "created" and bool(rep["match"].all())
    assert backup_raw_files(pg, raw, "raw_bk_a", if_changed=True).attrs["backup"]["status"] == "unchanged"
    assert backup_status(pg).set_index("schema").loc["raw_bk_a", "kind"] == "raw"
    assert bool(verify_backup(pg, "raw_bk_a", against=raw)["match"].all())
    (raw / "note.csv").write_bytes('ID|TXT\n1|"a | b"\n2|changed\n3|日本語\n4|\n'.encode("utf-8"))
    v = verify_backup(pg, "raw_bk_a", against=raw)
    bad = v[~v["match"]]
    assert bad[["table", "note"]].values.tolist() == [["note", "differs from the source"]]       # the stored copy itself is intact
    out = tmp_path / "raw_back"
    res = restore_raw_files(pg, "raw_bk_a", out)
    assert bool(res["match"].all()) and (out / "note.csv").exists()
    back = duckdb.connect().execute(f"SELECT * FROM read_csv('{(out / 'note.csv').as_posix()}', all_varchar = true) ORDER BY id").df()
    assert back["txt"].tolist()[:3] == ["a | b", "two\nlines", "日本語"]
    with pytest.raises(ValueError):
        restore_site(pg, "raw_bk_a", tmp_path / "x.duckdb")


@needs_pg
def test_backup_script_any_number_of_sites_anonymous(tmp_path, capsys):
    import postgres_backup as script
    import postgres_roundtrip_check as rt

    db = tmp_path / "db"
    db.mkdir()
    sites = ["northfield", "southport", "eastvale", "westbrook"]        # made-up keys; output must not name them
    persons = duckdb.connect().execute(f"SELECT person_id FROM read_csv('{(ROOT / 'tests/fixtures/study_cohort_cdm/person.csv').as_posix()}')").df()
    ids = persons.person_id.tolist()
    raws = []
    for k, s in enumerate(sites):
        _site_db(db / f"omop_{s}.duckdb", set(ids[k::4]))
        r = tmp_path / f"raw_{s}"
        r.mkdir()
        (r / "demographic.csv").write_bytes((ROOT / "tests/fixtures/pcornet_sample/demographic.csv").read_bytes())
        raws.append(str(r))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"northfield": "Site D", "southport": "Site A", "eastvale": "Site C", "westbrook": "Site B"}))
    common = ["--pg-dir", str(tmp_path / "pg"), "--backend", BACKENDS[0]]
    site_args = ["--sites", *sites, "--labels-file", str(labels)]
    try:
        # the round trip with --backup leaves managed backups
        rec = tmp_path / "record.md"
        assert rt.main(["--db-dir", str(db), *site_args, *common, "--out-db-dir", str(tmp_path / "back"), "--raw-dirs", *raws,
                        "--backup", "--out", str(rec)]) == 0
        assert "kept as a managed backup" in rec.read_text(encoding="utf-8")
        capsys.readouterr()
        assert script.main(["backup", "--db-dir", str(db), *site_args, "--raw-dirs", *raws, "--if-changed", *common]) == 0
        assert capsys.readouterr().out.count(": unchanged") == 8
        assert script.main(["status", *common]) == 0
        status = capsys.readouterr().out
        assert all(f"site_{c}" in status and f"raw_site_{c}" in status for c in "abcd")
        assert script.main(["verify", *common]) == 0
        assert script.main(["verify", "--db-dir", str(db), "--raw-dirs", *raws, *site_args, *common]) == 0
        assert script.main(["restore", *site_args, "--out-db-dir", str(tmp_path / "restored"), *common]) == 0
        for s in sites:
            assert bool(compare_duckdb(db / f"omop_{s}.duckdb", tmp_path / "restored" / f"omop_{s}.duckdb")["match"].all())
        with pytest.raises(SystemExit):                                     # never overwrites restored files
            script.main(["restore", *site_args, "--out-db-dir", str(tmp_path / "restored"), *common])
        # a changed site is refreshed, the others are skipped; verify against the files then passes again
        con = duckdb.connect(str(db / f"omop_{sites[0]}.duckdb"))
        con.execute("DELETE FROM study_extra WHERE id = 7")
        con.close()
        assert script.main(["verify", "--db-dir", str(db), *site_args, *common]) == 1
        capsys.readouterr()
        assert script.main(["backup", "--db-dir", str(db), *site_args, "--if-changed", *common]) == 0
        out = capsys.readouterr().out
        assert "[Site D] site_d: refreshed" in out and out.count(": unchanged") == 3
        assert script.main(["verify", "--db-dir", str(db), *site_args, *common]) == 0
        assert script.main(["drop", "--sites", sites[1], "--labels-file", str(labels), *common]) == 0
        capsys.readouterr()
        script.main(["status", *common])
        status = capsys.readouterr().out
        assert "site_a" not in status and "site_b" in status
        printed = status + out + rec.read_text(encoding="utf-8")
        assert not any(s in printed.lower() for s in sites)
    finally:
        EmbeddedPostgres(tmp_path / "pg", backend=BACKENDS[0]).drop()


@needs_pg
def test_roundtrip_drop_after_leaves_nothing_in_postgres(tmp_path):
    import postgres_roundtrip_check as rt

    db = tmp_path / "db"
    db.mkdir()
    _site_db(db / "omop_one.duckdb")
    common = ["--pg-dir", str(tmp_path / "pg"), "--backend", BACKENDS[0]]
    try:
        assert rt.main(["--db-dir", str(db), "--sites", "one", "--tables", "person,study_extra", "--out-db-dir", str(tmp_path / "b"),
                        "--drop-after", *common]) == 0
        with EmbeddedPostgres(tmp_path / "pg", backend=BACKENDS[0]) as s:
            assert list_site_schemas(s) == []
        with pytest.raises(SystemExit):
            rt.main(["--db-dir", str(db), "--sites", "one", "--out-db-dir", str(tmp_path / "c"), "--backup", "--drop-after", *common])
    finally:
        EmbeddedPostgres(tmp_path / "pg", backend=BACKENDS[0]).drop()
