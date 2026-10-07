"""omop_connect() (RFC 1.1), the concept-ancestry macros (RFC 1.2) and clamp_physiologic (RFC 6.1).

Everything runs on tiny synthetic fixtures (a site CDM plus a stand-in central vocabulary database);
the optional smoke test at the bottom uses a real Athena vocabulary only when OMOP_VOCAB_DB points at one.
"""

import hashlib
import os
import re
import shutil
import warnings
from pathlib import Path

import duckdb
import pytest

from omop_etl import build_omop_cdm, vocabulary
from omop_etl.build_omop_cdm import (
    MACRO_FILES,
    _resource_path,
    _split_sql_statements,
    attach_central_vocabulary,
    build_schema,
    load_macros,
)
from omop_etl.vocabulary import omop_connect

T2DM = 1001  # synthetic stand-ins, not real Athena ids
CONCEPT_ROWS = [
    # concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code
    (1000, "Diabetes mellitus", "Condition", "SNOMED", "Disorder", "S", "DM"),
    (1001, "Type 2 diabetes mellitus", "Condition", "SNOMED", "Disorder", "S", "T2DM"),
    (1002, "Type 2 diabetes mellitus with complication", "Condition", "SNOMED", "Disorder", "S", "T2DMC"),
    (1003, "Type 1 diabetes mellitus", "Condition", "SNOMED", "Disorder", "S", "T1DM"),
    (2000, "Hypertensive disorder", "Condition", "SNOMED", "Disorder", "S", "HTN"),
    (9001, "Type 2 diabetes mellitus without complications", "Condition", "ICD10CM", "5-char code", None, "E11.9"),
]
ANCESTOR_ROWS = [
    # ancestor, descendant, min_levels, max_levels -- Athena includes the self row (0, 0) for every concept
    (1000, 1000, 0, 0), (1000, 1001, 1, 1), (1000, 1002, 2, 2), (1000, 1003, 1, 1),
    (1001, 1001, 0, 0), (1001, 1002, 1, 1),
    (1002, 1002, 0, 0), (1003, 1003, 0, 0), (2000, 2000, 0, 0),
]
RELATIONSHIP_ROWS = [(9001, 1001, "Maps to"), (1001, 9001, "Mapped from")]
VOCAB_TABLES = {"concept", "concept_ancestor", "concept_relationship"}


# --------------------------------------------------------------------------------------------- fixtures
def _make_vocab(path, tables=VOCAB_TABLES):
    con = duckdb.connect(str(path))
    if "concept" in tables:
        con.execute(
            "CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR, domain_id VARCHAR, "
            "vocabulary_id VARCHAR, concept_class_id VARCHAR, standard_concept VARCHAR, concept_code VARCHAR, "
            "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)"
        )
        con.executemany(
            "INSERT INTO concept VALUES (?, ?, ?, ?, ?, ?, ?, DATE '1970-01-01', DATE '2099-12-31', NULL)",
            CONCEPT_ROWS,
        )
    if "concept_ancestor" in tables:
        con.execute(
            "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER, "
            "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)"
        )
        con.executemany("INSERT INTO concept_ancestor VALUES (?, ?, ?, ?)", ANCESTOR_ROWS)
    if "concept_relationship" in tables:
        con.execute(
            "CREATE TABLE concept_relationship (concept_id_1 INTEGER, concept_id_2 INTEGER, relationship_id VARCHAR, "
            "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)"
        )
        con.executemany(
            "INSERT INTO concept_relationship VALUES (?, ?, ?, DATE '1970-01-01', DATE '2099-12-31', NULL)",
            RELATIONSHIP_ROWS,
        )
    con.close()


def _fill_cdm(con):
    for pid in (1, 2, 3, 4):
        con.execute(
            "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
            f"VALUES ({pid}, 8507, 1970, 8527, 38003564)"
        )
    for oid, (pid, cid) in enumerate([(1, 1001), (2, 1002), (3, 2000), (4, 1003)], start=1):
        con.execute(
            "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
            f"condition_start_date, condition_type_concept_id) VALUES ({oid}, {pid}, {cid}, DATE '2020-01-01', 32020)"
        )


@pytest.fixture(scope="session")
def cdm_template(tmp_path_factory):
    """A CDM built by build_schema() (so every vocabulary table exists, empty) with a few patients."""
    path = tmp_path_factory.mktemp("template") / "template.duckdb"
    con = duckdb.connect(str(path))
    build_schema(con)
    _fill_cdm(con)
    con.close()
    return path


@pytest.fixture()
def site(tmp_path, cdm_template):
    """A site directory holding cdm.duckdb (empty vocabulary tables) and a sibling central_vocabulary.duckdb."""
    directory = tmp_path / "site"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    _make_vocab(directory / "central_vocabulary.duckdb")
    return directory


@pytest.fixture()
def cdm(site):
    return site / "cdm.duckdb"


@pytest.fixture()
def vocab(site):
    return site / "central_vocabulary.duckdb"


@pytest.fixture()
def connected(cdm):
    """Read-only connection with the sibling vocabulary auto-discovered."""
    con = omop_connect(cdm, read_only=True)
    yield con
    con.close()


def _snapshot(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns


def _databases(con):
    return {name: path for name, path in con.execute(
        "SELECT database_name, path FROM duckdb_databases() WHERE NOT internal").fetchall()}


def _setting(con, name):
    return con.execute("SELECT value FROM duckdb_settings() WHERE name = ?", [name]).fetchone()[0]


def _macros(con, database):
    return {r[0] for r in con.execute(
        "SELECT function_name FROM duckdb_functions() WHERE function_type IN ('macro', 'table_macro') "
        "AND NOT internal AND database_name = ?", [database]).fetchall()}


def _omop_warnings(record):
    return [str(w.message) for w in record if str(w.message).startswith("omop_connect")]


def _shipped_macro_names():
    names = set()
    for macro_file in MACRO_FILES:
        sql = Path(_resource_path("sql", macro_file)).read_text(encoding="utf-8")
        names |= set(re.findall(r"CREATE OR REPLACE MACRO (\w+)", re.sub(r"--[^\n]*", "", sql)))
    return names


# ------------------------------------------------------------------------------- path / connection input
@pytest.mark.parametrize("as_path", [False, True], ids=["str", "pathlib"])
def test_path_input_attaches_vocab_and_queries_without_prefix(cdm, vocab, as_path):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        con = omop_connect(cdm if as_path else str(cdm))
    try:
        assert isinstance(con, duckdb.DuckDBPyConnection)
        assert _omop_warnings(record) == []
        assert os.path.samefile(_databases(con)["central_vocab"], vocab)
        assert _setting(con, "search_path") == "main,central_vocab.main"
        # no schema prefix, and the RFC's headline query shape works
        rows = con.execute(
            "SELECT c.concept_name, COUNT(*) FROM condition_occurrence co "
            "JOIN concept c ON co.condition_concept_id = c.concept_id GROUP BY 1 ORDER BY 1"
        ).fetchall()
        assert rows == [("Hypertensive disorder", 1), ("Type 1 diabetes mellitus", 1),
                        ("Type 2 diabetes mellitus", 1), ("Type 2 diabetes mellitus with complication", 1)]
    finally:
        con.close()


def test_existing_connection_is_reused_and_never_closed(cdm, vocab):
    con0 = duckdb.connect(str(cdm))
    try:
        assert omop_connect(con0, vocab_db_path=vocab) is con0
        assert con0.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        # a failing call must not close a connection it does not own either
        with pytest.raises(FileNotFoundError):
            omop_connect(con0, vocab_db_path=vocab.with_name("nope.duckdb"))
        assert con0.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(ValueError):
            omop_connect(con0, vocab_db_path=cdm)
        assert con0.execute("SELECT 1").fetchone()[0] == 1
    finally:
        con0.close()
    # connecting never wrote anything into the (writable) database: macros live in the session only
    with duckdb.connect(str(cdm), read_only=True) as check:
        assert _macros(check, "cdm") == set()


def test_existing_read_only_connection_and_read_only_flag(cdm, vocab):
    con0 = duckdb.connect(str(cdm), read_only=True)
    try:
        assert omop_connect(con0, vocab_db_path=vocab) is con0
        assert con0.execute("SELECT clamp_physiologic(500, 0, 300)").fetchone()[0] == 300
    finally:
        con0.close()

    con1 = duckdb.connect(str(cdm))
    try:
        with pytest.warns(UserWarning, match="read_only=True has no effect"):
            omop_connect(con1, vocab_db_path=vocab, read_only=True)
    finally:
        con1.close()


def test_read_only_connect_to_missing_database_is_an_error(tmp_path):
    missing = tmp_path / "does_not_exist.duckdb"
    with pytest.raises(FileNotFoundError, match="Database file not found"):
        omop_connect(missing, read_only=True)
    assert not missing.exists()


@pytest.mark.parametrize("target", [":memory:", "", ":memory:named"])
def test_read_only_in_memory_database_is_a_clear_error(target):
    """An in-memory database has no file to open read-only; R and Python both refuse it up front."""
    with pytest.raises(ValueError, match="in-memory database"):
        omop_connect(target, read_only=True)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """A scratch directory that '~' expands to (POSIX reads HOME, Windows USERPROFILE)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert os.path.expanduser("~") == str(home)
    return home


def test_a_leading_tilde_is_expanded_in_the_database_and_vocabulary_paths(fake_home, cdm, vocab):
    shutil.copy(cdm, fake_home / "site.duckdb")
    shutil.copy(vocab, fake_home / "v.duckdb")
    con = omop_connect("~/site.duckdb", vocab_db_path="~/v.duckdb", read_only=True)
    try:
        assert os.path.samefile(_databases(con)["central_vocab"], fake_home / "v.duckdb")
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
    finally:
        con.close()


def test_a_tilde_database_path_still_auto_discovers_a_sibling_vocabulary(fake_home, cdm, vocab):
    shutil.copy(cdm, fake_home / "site.duckdb")
    shutil.copy(vocab, fake_home / "vocab.duckdb")
    con = omop_connect(Path("~") / "site.duckdb", read_only=True)  # a pathlib spelling too
    try:
        assert os.path.samefile(_databases(con)["central_vocab"], fake_home / "vocab.duckdb")
    finally:
        con.close()


def test_missing_tilde_paths_report_the_expanded_path(fake_home):
    def reported(error):  # the path after "...not found: ", compared independent of separators and case
        return os.path.normcase(os.path.normpath(str(error.value).split("not found: ", 1)[1]))

    with pytest.raises(FileNotFoundError, match="Database file not found") as db_error:
        omop_connect("~/nope.duckdb", read_only=True)
    assert reported(db_error) == os.path.normcase(str(fake_home / "nope.duckdb"))
    with pytest.raises(FileNotFoundError, match="Vocabulary database not found") as vocab_error:
        omop_connect(":memory:", vocab_db_path="~/nope_vocab.duckdb")
    assert reported(vocab_error) == os.path.normcase(str(fake_home / "nope_vocab.duckdb"))
    assert not (fake_home / "nope.duckdb").exists()


# ------------------------------------------------------------------------------------------- discovery
@pytest.mark.parametrize("name", ["central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb"])
def test_auto_discovery_finds_each_sibling_name(tmp_path, cdm_template, name):
    directory = tmp_path / "d"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    _make_vocab(directory / name)
    con = omop_connect(directory / "cdm.duckdb", read_only=True)
    try:
        assert os.path.samefile(_databases(con)["central_vocab"], directory / name)
    finally:
        con.close()


def test_auto_discovery_priority_order(tmp_path, cdm_template):
    directory = tmp_path / "d"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    for name in ("vocab.duckdb", "vocabulary.duckdb"):
        _make_vocab(directory / name)
    con = omop_connect(directory / "cdm.duckdb", read_only=True)
    try:
        assert os.path.samefile(_databases(con)["central_vocab"], directory / "vocabulary.duckdb")
    finally:
        con.close()


def test_auto_discovery_only_looks_next_to_the_database(tmp_path, cdm_template, monkeypatch):
    """The cwd (and a private project layout under it) is never searched."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "derived" / "omop_duckdb").mkdir(parents=True)
    _make_vocab(elsewhere / "central_vocabulary.duckdb")
    _make_vocab(elsewhere / "derived" / "omop_duckdb" / "central_vocabulary.duckdb")
    directory = tmp_path / "site"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    monkeypatch.chdir(elsewhere)
    with pytest.warns(UserWarning, match="No vocabulary available"):
        con = omop_connect(directory / "cdm.duckdb", read_only=True)
    try:
        assert "central_vocab" not in _databases(con)
    finally:
        con.close()


@pytest.mark.parametrize("name", ["central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb"])
def test_discovery_never_attaches_the_primary_to_itself(tmp_path, cdm_template, name):
    cdm_file = tmp_path / name  # the primary database itself carries a discoverable name
    shutil.copy(cdm_template, cdm_file)
    with pytest.warns(UserWarning, match="No vocabulary available"):
        con = omop_connect(cdm_file, read_only=True)
    try:
        assert "central_vocab" not in _databases(con)
        assert con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
    finally:
        con.close()


def test_auto_attach_false_skips_discovery_but_explicit_path_still_attaches(cdm, vocab):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        con = omop_connect(cdm, read_only=True, auto_attach_vocab=False)
    try:
        assert "central_vocab" not in _databases(con)
        assert _omop_warnings(record) == []  # the caller opted out, so no "no vocabulary" nag
    finally:
        con.close()
    con = omop_connect(cdm, vocab_db_path=vocab, read_only=True, auto_attach_vocab=False)
    try:
        assert "central_vocab" in _databases(con)
    finally:
        con.close()


def test_explicit_vocab_wins_over_a_sibling(site, cdm, tmp_path):
    other = tmp_path / "other_vocab.duckdb"
    _make_vocab(other, tables={"concept"})
    con = omop_connect(cdm, vocab_db_path=other, read_only=True)
    try:
        assert os.path.samefile(_databases(con)["central_vocab"], other)
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------- errors
def test_explicit_missing_vocab_is_an_error_and_opens_nothing(cdm, site):
    with pytest.raises(FileNotFoundError, match="Vocabulary database not found"):
        omop_connect(cdm, vocab_db_path=site / "typo.duckdb")
    with pytest.raises(FileNotFoundError, match="Vocabulary database not found"):
        omop_connect(cdm, vocab_db_path=site)  # a directory is not a vocabulary database
    # the primary database was never opened, so it can still be opened in any mode
    with duckdb.connect(str(cdm)) as check:
        assert check.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4


def test_explicit_vocab_missing_is_an_error_even_with_a_sibling(site, cdm):
    """No silent fall-back to auto-discovery when the caller named a vocabulary explicitly."""
    with pytest.raises(FileNotFoundError):
        omop_connect(cdm, vocab_db_path=site / "typo.duckdb", auto_attach_vocab=True)


def test_vocab_equal_to_the_primary_is_an_error_and_releases_the_database(cdm):
    with pytest.raises(ValueError, match="primary database itself") as excinfo:
        omop_connect(cdm, vocab_db_path=cdm)
    # The connection omop_connect opened before failing was closed again. excinfo keeps its frames (and so
    # any leaked connection) alive, and DuckDB refuses a read-only open of a file that still has a
    # read-write instance in this process.
    with duckdb.connect(str(cdm), read_only=True) as check:
        assert check.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
    del excinfo


def test_a_failure_during_setup_closes_only_a_connection_it_opened(cdm, monkeypatch):
    seen = []

    def boom(con, *args):
        seen.append(con)
        raise RuntimeError("boom")

    monkeypatch.setattr(vocabulary, "_configure_omop_connection", boom)
    with pytest.raises(RuntimeError, match="boom"):
        omop_connect(cdm)
    with pytest.raises(duckdb.ConnectionException):
        seen[0].execute("SELECT 1")  # the connection omop_connect opened was closed again
    con0 = duckdb.connect(str(cdm))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            omop_connect(con0)
        assert con0.execute("SELECT 1").fetchone()[0] == 1  # one it was given stays open
    finally:
        con0.close()


def test_different_database_already_attached_as_central_vocab_is_an_error(cdm, vocab, tmp_path):
    other = tmp_path / "other.duckdb"
    _make_vocab(other)
    con0 = duckdb.connect(str(cdm), read_only=True)
    try:
        con0.execute(f"ATTACH '{other.as_posix()}' AS central_vocab (READ_ONLY)")
        with pytest.raises(ValueError, match="different database is already attached"):
            omop_connect(con0, vocab_db_path=vocab)
    finally:
        con0.close()


def test_paths_with_quotes_and_spaces_are_escaped(tmp_path, cdm_template):
    directory = tmp_path / "O'Brien's site"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    _make_vocab(directory / "central_vocabulary.duckdb")
    for kwargs in ({}, {"vocab_db_path": directory / "central_vocabulary.duckdb"}):
        con = omop_connect(directory / "cdm.duckdb", read_only=True, **kwargs)
        try:
            assert os.path.samefile(_databases(con)["central_vocab"], directory / "central_vocabulary.duckdb")
            assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        finally:
            con.close()


# ------------------------------------------------------------------------------------------ idempotency
def test_attach_is_idempotent(cdm, vocab, monkeypatch):
    con = duckdb.connect(str(cdm), read_only=True)
    try:
        for _ in range(3):
            omop_connect(con, vocab_db_path=vocab)
        assert [d for d in _databases(con) if d == "central_vocab"] == ["central_vocab"]
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        # the same file spelled differently (relative / backslashes on Windows) is the same attachment
        monkeypatch.chdir(vocab.parent)
        omop_connect(con, vocab_db_path="central_vocabulary.duckdb")
        omop_connect(con, vocab_db_path=str(vocab).replace("/", os.sep))
        omop_connect(con)  # auto-discovery on an already-attached connection is a no-op too
    finally:
        con.close()


def test_pre_attached_vocabulary_is_adopted(cdm, vocab):
    con = duckdb.connect(str(cdm), read_only=True)
    try:
        con.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY)")
        omop_connect(con)
        assert _setting(con, "search_path") == "main,central_vocab.main"
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
    finally:
        con.close()


def test_two_connections_to_the_same_files(cdm):
    con_a = omop_connect(cdm, read_only=True)
    con_b = omop_connect(cdm, read_only=True)
    try:
        for con in (con_a, con_b):
            assert con.execute("SELECT COUNT(*) FROM descendants_of(1000)").fetchone()[0] == 4
    finally:
        con_a.close()
        con_b.close()


# ---------------------------------------------------------------------------------- search_path precedence
def test_a_cursor_is_a_new_connection_that_needs_omop_connect_itself(cdm, vocab):
    """search_path, the TEMP views and the TEMP macros are per connection (documented in the docstring)."""
    con = omop_connect(cdm, vocab_db_path=vocab, read_only=True)
    try:
        bare = con.cursor()
        # the pitfall: the empty local table shadows the vocabulary, silently, and the macros are gone
        assert bare.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == 0
        assert _setting(bare, "search_path") != _setting(con, "search_path")
        with pytest.raises(duckdb.CatalogException):
            bare.execute("SELECT * FROM descendants_of(1000)")
        # the documented remedy: pass the cursor through omop_connect itself (same attachment, no conflict)
        fixed = omop_connect(con.cursor())
        assert fixed.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert _ids(fixed, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
    finally:
        con.close()


def test_the_docstring_warns_about_cursors():
    doc = " ".join((omop_connect.__doc__ or "").split())
    assert "con.cursor()" in doc and "omop_connect(cursor)" in doc


def test_empty_local_vocabulary_tables_fall_through_to_the_attached_vocabulary(connected):
    # build_schema() leaves concept / concept_ancestor / concept_relationship empty in the site database;
    # they must not shadow the attached vocabulary
    for table, expected in (("concept", len(CONCEPT_ROWS)), ("concept_ancestor", len(ANCESTOR_ROWS)),
                            ("concept_relationship", len(RELATIONSHIP_ROWS))):
        assert connected.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
    # tables that are not part of the vocabulary are untouched
    assert connected.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4


def test_a_populated_local_table_wins_over_the_attached_vocabulary(cdm, vocab):
    with duckdb.connect(str(cdm)) as w:
        w.execute("INSERT INTO concept VALUES (1001, 'LOCAL type 2 diabetes', 'Condition', 'SNOMED', 'Disorder', "
                  "'S', 'T2DM', DATE '1970-01-01', DATE '2099-12-31', NULL)")
    con = omop_connect(cdm, read_only=True)
    try:
        assert con.execute("SELECT concept_name FROM concept WHERE concept_id = 1001").fetchall() == [
            ("LOCAL type 2 diabetes",)]
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == 1
        # precedence is per table: concept_ancestor is still empty locally, so it resolves to the vocabulary
        assert con.execute("SELECT COUNT(*) FROM concept_ancestor").fetchone()[0] == len(ANCESTOR_ROWS)
        # and the attached vocabulary is still reachable explicitly
        assert con.execute("SELECT concept_name FROM central_vocab.concept WHERE concept_id = 1001").fetchall() == [
            ("Type 2 diabetes mellitus",)]
    finally:
        con.close()


def test_tables_without_a_local_copy_resolve_through_the_search_path(tmp_path, vocab):
    bare = tmp_path / "bare.duckdb"  # no CDM schema at all: only a person table
    with duckdb.connect(str(bare)) as w:
        w.execute("CREATE TABLE person (person_id INTEGER)")
    con = omop_connect(bare, vocab_db_path=vocab, read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM concept_ancestor").fetchone()[0] == len(ANCESTOR_ROWS)
        # resolved by search_path alone: no shadow views were needed
        assert con.execute("SELECT COUNT(*) FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal"
                           ).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM descendants_of(1000)").fetchone()[0] == 4
    finally:
        con.close()


def test_new_objects_are_created_in_the_primary_database_not_the_vocabulary(cdm, vocab):
    con = omop_connect(cdm, vocab_db_path=vocab)
    try:
        con.execute("CREATE TABLE my_cohort AS SELECT person_id FROM person")
        where = con.execute("SELECT database_name FROM duckdb_tables() WHERE table_name = 'my_cohort'").fetchall()
        assert where == [("cdm",)]
    finally:
        con.close()


def test_precedence_is_documented_main_first_then_central_vocab(connected):
    assert _setting(connected, "search_path") == "main,central_vocab.main"


def test_a_search_path_set_on_a_passed_in_connection_is_kept_with_central_vocab_appended(cdm, vocab):
    con = duckdb.connect(str(cdm))
    try:
        con.execute("CREATE SCHEMA analysis")
        con.execute("CREATE TABLE analysis.cohort_tbl AS SELECT 7 AS person_id")
        con.execute("SET search_path = 'analysis,main'")
        omop_connect(con, vocab_db_path=vocab)
        assert _setting(con, "search_path") == "analysis,main,central_vocab.main"
        assert con.execute("SELECT person_id FROM cohort_tbl").fetchone()[0] == 7  # caller's schema still resolves
        assert con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert _ids(con, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
        omop_connect(con, vocab_db_path=vocab)  # connecting again does not stack another entry
        assert _setting(con, "search_path") == "analysis,main,central_vocab.main"
    finally:
        con.close()


def test_a_search_path_without_main_still_ranks_the_primary_database_before_the_central_vocabulary(cdm, vocab):
    """DuckDB consults `main` only AFTER the search_path entries; appending central_vocab.main alone to a path
    that omits `main` would let the central vocabulary beat a populated local vocabulary table."""
    with duckdb.connect(str(cdm)) as w:
        w.execute("INSERT INTO concept VALUES (1001, 'LOCAL type 2 diabetes', 'Condition', 'SNOMED', 'Disorder', "
                  "'S', 'T2DM', DATE '1970-01-01', DATE '2099-12-31', NULL)")
    con = duckdb.connect(str(cdm))
    try:
        con.execute("CREATE SCHEMA analysis")
        con.execute("CREATE TABLE analysis.cohort_tbl AS SELECT 7 AS person_id")
        con.execute("SET search_path = 'analysis'")  # no `main` in it
        # what the plain append would have produced: the vocabulary wins over the local table (the hazard)
        con.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY)")
        con.execute("SET search_path = 'analysis,central_vocab.main'")
        assert con.execute("SELECT concept_name FROM concept WHERE concept_id = 1001").fetchone()[0] == (
            "Type 2 diabetes mellitus")
        con.execute("SET search_path = 'analysis'")
        omop_connect(con)
        assert _setting(con, "search_path") == "analysis,main,central_vocab.main"
        assert con.execute("SELECT concept_name FROM concept WHERE concept_id = 1001").fetchall() == [
            ("LOCAL type 2 diabetes",)]
        assert con.execute("SELECT person_id FROM cohort_tbl").fetchone()[0] == 7
        assert con.execute("SELECT COUNT(*) FROM concept_ancestor").fetchone()[0] == len(ANCESTOR_ROWS)  # empty locally
        assert con.execute("SELECT current_schema()").fetchone()[0] == "analysis"  # new objects keep landing there
        omop_connect(con)  # idempotent
        assert _setting(con, "search_path") == "analysis,main,central_vocab.main"
    finally:
        con.close()


def test_a_search_path_naming_the_primary_database_main_schema_is_not_given_a_second_main(cdm, vocab):
    con = duckdb.connect(str(cdm))
    try:
        con.execute("CREATE SCHEMA analysis")
        con.execute("SET search_path = 'analysis,cdm.main'")
        omop_connect(con, vocab_db_path=vocab)
        assert _setting(con, "search_path") == "analysis,cdm.main,central_vocab.main"
    finally:
        con.close()


def test_a_search_path_that_already_lists_central_vocab_is_left_exactly_as_the_caller_wrote_it(cdm, vocab):
    con = duckdb.connect(str(cdm))
    try:
        con.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY)")
        con.execute("SET search_path = 'main,central_vocab.main'")
        omop_connect(con)
        assert _setting(con, "search_path") == "main,central_vocab.main"
    finally:
        con.close()


@pytest.mark.parametrize("current, primary, expected", [
    ("", None, "main,central_vocab.main"),
    (None, None, "main,central_vocab.main"),
    ("main", None, "main,central_vocab.main"),
    ("analysis,main", None, "analysis,main,central_vocab.main"),
    ("MAIN,analysis", None, "MAIN,analysis,central_vocab.main"),
    ("analysis", None, "analysis,main,central_vocab.main"),  # main resolution is made explicit, before the vocabulary
    ("a,b", "cdm", "a,b,main,central_vocab.main"),
    ("analysis,cdm.main", "cdm", "analysis,cdm.main,central_vocab.main"),
    ("analysis,CDM.MAIN", "cdm", "analysis,CDM.MAIN,central_vocab.main"),
    ("analysis,cdm.main", None, "analysis,cdm.main,main,central_vocab.main"),  # primary unknown: only `main` counts
    ("analysis,other.main", "cdm", "analysis,other.main,main,central_vocab.main"),  # another catalog's main is not ours
    ("main,central_vocab.main", None, "main,central_vocab.main"),
    ("central_vocab.main,main", None, "central_vocab.main,main"),
    ("CENTRAL_VOCAB.main,main", None, "CENTRAL_VOCAB.main,main"),
    ('"central_vocab".main', None, '"central_vocab".main'),
    ("central_vocab", None, "central_vocab"),
    ("cdm.central_vocab", None, "cdm.central_vocab,main,central_vocab.main"),  # a schema that merely has that name
])
def test_search_path_is_extended_not_replaced(current, primary, expected):
    assert vocabulary._search_path_with_central_vocab(current, primary) == expected


# ----------------------------------------------------------------------- read-only + macros + untouched
def test_read_only_connect_loads_macros_and_leaves_both_files_untouched(cdm, vocab):
    before = {p: _snapshot(p) for p in (cdm, vocab)}
    con = omop_connect(cdm, read_only=True)
    try:
        assert _shipped_macro_names() <= _macros(con, "temp")  # every shipped macro, as TEMP
        assert _macros(con, "cdm") == set()
        assert _macros(con, "central_vocab") == set()
        tables_in_vocab = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'central_vocab'").fetchall()}
        assert tables_in_vocab == VOCAB_TABLES
        # exercise macros and vocabulary reads
        assert con.execute("SELECT COUNT(*) FROM descendants_of(1000)").fetchone()[0] == 4
        assert con.execute("SELECT map_to_standard_concept_id('ICD10CM', 'E11.9')").fetchone()[0] == 1001
        assert con.execute("SELECT clamp_physiologic(500, 0, 300)").fetchone()[0] == 300
    finally:
        con.close()
    assert {p: _snapshot(p) for p in (cdm, vocab)} == before
    assert not list(cdm.parent.glob("*.wal"))


def test_load_macros_default_is_unchanged_for_writable_databases(tmp_path):
    """ETL path: persistent macros on a writable database, TEMP only on a read-only one."""
    path = tmp_path / "etl.duckdb"
    with duckdb.connect(str(path)) as con:
        build_schema(con)
        load_macros(con)
    with duckdb.connect(str(path), read_only=True) as ro:
        assert _shipped_macro_names() <= _macros(ro, "etl")  # persisted in the file
        assert str(ro.execute("SELECT parse_omop_date('2020-01-02')").fetchone()[0])[:10] == "2020-01-02"
        assert load_macros(ro) == []  # read-only: falls back to TEMP instead of failing
        assert _shipped_macro_names() <= _macros(ro, "temp")

    path2 = tmp_path / "etl2.duckdb"
    with duckdb.connect(str(path2)) as con:
        build_schema(con)
        load_macros(con, temporary=True)
    with duckdb.connect(str(path2), read_only=True) as ro:
        assert _macros(ro, "etl2") == set()


def test_shipped_macro_files_split_and_rewrite_cleanly():
    """The statement splitter and the CREATE-to-TEMP rewrite assume this file layout."""
    for macro_file in MACRO_FILES:
        sql = Path(_resource_path("sql", macro_file)).read_text(encoding="utf-8")
        code = re.sub(r"--[^\n]*", "", sql)
        statements = _split_sql_statements(sql)
        assert statements, macro_file
        assert all(s.startswith("CREATE OR REPLACE MACRO ") for s in statements), macro_file
        assert len(statements) == len(re.findall(r"\bMACRO\b", code)), macro_file
        assert not [lit for lit in re.findall(r"'((?:[^']|'')*)'", code) if ";" in lit or "--" in lit], macro_file


def test_missing_macro_file_is_reported_not_skipped_silently(monkeypatch):
    monkeypatch.setattr(build_omop_cdm, "MACRO_FILES", ["mapping_macros.sql", "no_such_macros.sql"])
    con = duckdb.connect()
    build_schema(con)
    with pytest.warns(RuntimeWarning, match="no_such_macros.sql"):
        load_macros(con)
    con.close()


# ------------------------------------------------------------------------------------- macro semantics
def _ids(con, sql):
    return sorted(r[0] for r in con.execute(sql).fetchall())


def test_descendants_of_includes_self_and_all_levels(connected):
    assert [d[0] for d in connected.execute("DESCRIBE SELECT * FROM descendants_of(1000)").fetchall()] == ["concept_id"]
    assert _ids(connected, "SELECT concept_id FROM descendants_of(1000)") == [1000, 1001, 1002, 1003]
    assert _ids(connected, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
    assert _ids(connected, "SELECT concept_id FROM descendants_of(1002)") == [1002]  # a leaf: just itself
    # Athena's concept_ancestor carries the self row; the macro relies on it rather than adding one
    assert connected.execute(
        "SELECT COUNT(*) FROM concept_ancestor WHERE ancestor_concept_id = 1000 AND descendant_concept_id = 1000"
    ).fetchone()[0] == 1


def test_ancestors_of_includes_self_and_all_levels(connected):
    assert [d[0] for d in connected.execute("DESCRIBE SELECT * FROM ancestors_of(1002)").fetchall()] == ["concept_id"]
    assert _ids(connected, "SELECT concept_id FROM ancestors_of(1002)") == [1000, 1001, 1002]
    assert _ids(connected, "SELECT concept_id FROM ancestors_of(1000)") == [1000]
    assert _ids(connected, "SELECT concept_id FROM ancestors_of(1003)") == [1000, 1003]


def test_ancestry_macros_return_nothing_for_unknown_or_null_ids(connected):
    for macro in ("descendants_of", "ancestors_of"):
        assert connected.execute(f"SELECT * FROM {macro}(999999)").fetchall() == []
        assert connected.execute(f"SELECT * FROM {macro}(NULL)").fetchall() == []


def test_ancestry_macros_in_subqueries_joins_and_correlated_use(connected):
    persons = connected.execute(
        "SELECT person_id FROM condition_occurrence "
        "WHERE condition_concept_id IN (SELECT concept_id FROM descendants_of(1001)) ORDER BY 1").fetchall()
    assert persons == [(1,), (2,)]  # type 2 diabetes and its descendant, not type 1 and not hypertension
    joined = connected.execute(
        "SELECT co.person_id, c.concept_name FROM condition_occurrence co "
        "JOIN descendants_of(1000) d ON d.concept_id = co.condition_concept_id "
        "JOIN concept c ON c.concept_id = d.concept_id ORDER BY 1").fetchall()
    assert [r[0] for r in joined] == [1, 2, 4]
    assert connected.execute(
        "SELECT COUNT(*) FROM concept c WHERE c.concept_id IN (SELECT concept_id FROM ancestors_of(1002)) "
        "AND c.standard_concept = 'S'").fetchone()[0] == 3
    # an expression argument, and a correlated column argument
    assert _ids(connected, "SELECT concept_id FROM descendants_of(1000 + 1)") == [1001, 1002]
    connected.execute("CREATE TEMP TABLE probe (person_id INTEGER, ancestor_id INTEGER, descendant_id INTEGER)")
    connected.execute("INSERT INTO probe VALUES (1, 1000, 1002), (2, 1001, 1003), (3, 1003, 1003)")
    # the caller's columns are literally called ancestor_id / descendant_id, like the macro parameters
    assert connected.execute(
        "SELECT person_id FROM probe WHERE descendant_id IN (SELECT concept_id FROM descendants_of(ancestor_id)) "
        "ORDER BY 1").fetchall() == [(1,), (3,)]
    assert connected.execute(
        "SELECT person_id FROM probe WHERE ancestor_id IN (SELECT concept_id FROM ancestors_of(descendant_id)) "
        "ORDER BY 1").fetchall() == [(1,), (3,)]


def test_macro_parameters_cannot_collide_with_the_columns_they_filter(connected):
    columns = {r[0] for r in connected.execute("DESCRIBE concept_ancestor").fetchall()}
    assert columns == {"ancestor_concept_id", "descendant_concept_id",
                       "min_levels_of_separation", "max_levels_of_separation"}
    for macro in ("descendants_of", "ancestors_of"):
        (parameters,) = connected.execute(
            "SELECT parameters FROM duckdb_functions() WHERE function_name = ?", [macro]).fetchone()
        assert parameters and not set(parameters) & columns, (macro, parameters)


# What the callers' columns are called must not matter: these are the names a caller's table can plausibly
# hold, including the macros' own output column (concept_id) and the concept_ancestor columns.
CALLER_COLUMN_NAMES = ["concept_id", "ancestor_concept_id", "descendant_concept_id", "ancestor_id", "descendant_id"]
# value -> (size of descendants_of, size of ancestors_of); unknown and NULL ids give nothing
ANCESTRY_COUNTS = {1000: (4, 1), 1001: (2, 2), 1002: (1, 3), 1003: (1, 2), 2000: (1, 1), 999999: (0, 0), None: (0, 0)}


@pytest.mark.parametrize("column", CALLER_COLUMN_NAMES)
@pytest.mark.parametrize("qualified", [False, True], ids=["unqualified", "qualified"])
def test_ancestry_macros_bind_the_callers_column_whatever_it_is_called(connected, column, qualified):
    connected.execute(f'CREATE TEMP TABLE probe ("{column}" INTEGER)')
    connected.executemany("INSERT INTO probe VALUES (?)", [(v,) for v in ANCESTRY_COUNTS])
    arg = f'probe."{column}"' if qualified else f'"{column}"'
    for position, macro in enumerate(("descendants_of", "ancestors_of")):
        # correlated scalar subquery: the argument is the caller's column, row by row
        rows = connected.execute(
            f'SELECT "{column}", (SELECT COUNT(*) FROM {macro}({arg})) FROM probe '
            f'ORDER BY "{column}" NULLS LAST').fetchall()
        expected = sorted(((v, c[position]) for v, c in ANCESTRY_COUNTS.items()),
                          key=lambda r: (r[0] is None, r[0]))
        assert rows == expected, (macro, column, qualified)
        # a lateral join over the same column returns the very same concepts
        assert connected.execute(
            f'SELECT COUNT(*) FROM probe, {macro}(probe."{column}")').fetchone()[0] == sum(
                c[position] for c in ANCESTRY_COUNTS.values()), (macro, column)
        # and so does an IN subquery that is correlated on it: which rows reach concept 1001?
        reaching = sum(1 for v in ANCESTRY_COUNTS if v is not None and 1001 in _ids(
            connected, f"SELECT concept_id FROM {macro}({v})"))
        assert connected.execute(
            f"SELECT COUNT(*) FROM probe WHERE 1001 IN (SELECT concept_id FROM {macro}({arg}))"
        ).fetchone()[0] == reaching, (macro, column)


# Join and semi-join shapes: {m} is the macro, {c} the caller's column. Every one is checked against the size of
# the macro's result for each literal id, so a caller column that is captured by the macro's own scope (its output
# column concept_id, or a concept_ancestor column) changes the count.
JOIN_SHAPES = {
    "comma lateral, unqualified": ('SELECT COUNT(*) FROM probe, {m}("{c}")', "inner"),
    "comma lateral, qualified": ('SELECT COUNT(*) FROM probe, {m}(probe."{c}")', "inner"),
    "JOIN LATERAL subquery": ('SELECT COUNT(*) FROM probe p JOIN LATERAL (SELECT concept_id FROM {m}(p."{c}")) d ON TRUE', "inner"),
    "LEFT JOIN LATERAL subquery": ('SELECT COUNT(*) FROM probe p LEFT JOIN LATERAL (SELECT concept_id FROM {m}(p."{c}")) d ON TRUE', "left"),
    "JOIN ... ON TRUE, qualified": ('SELECT COUNT(*) FROM probe p JOIN {m}(p."{c}") d ON TRUE', "inner"),
    "JOIN ... ON predicate, unqualified": ('SELECT COUNT(*) FROM probe JOIN {m}("{c}") d ON d.concept_id IS NOT NULL', "inner"),
    "EXISTS, qualified": ('SELECT COUNT(*) FROM probe p WHERE EXISTS (SELECT 1 FROM {m}(p."{c}"))', "exists"),
    "EXISTS, unqualified": ('SELECT COUNT(*) FROM probe WHERE EXISTS (SELECT 1 FROM {m}("{c}"))', "exists"),
    "NOT IN, unqualified": ('SELECT COUNT(*) FROM probe WHERE 1001 NOT IN (SELECT concept_id FROM {m}("{c}"))', "not in 1001"),
}


@pytest.mark.parametrize("column", CALLER_COLUMN_NAMES)
@pytest.mark.parametrize("macro", ["descendants_of", "ancestors_of"])
def test_ancestry_macros_bind_the_callers_column_in_join_and_semi_join_contexts(connected, macro, column):
    connected.execute(f'CREATE TEMP TABLE probe ("{column}" INTEGER)')
    connected.executemany("INSERT INTO probe VALUES (?)", [(v,) for v in ANCESTRY_COUNTS])
    # what the macro returns for each id, written as a literal (no correlation involved)
    members = {v: _ids(connected, f"SELECT concept_id FROM {macro}({'NULL' if v is None else v})")
               for v in ANCESTRY_COUNTS}
    expected = {
        "inner": sum(len(m) for m in members.values()),
        "left": sum(max(1, len(m)) for m in members.values()),
        "exists": sum(1 for m in members.values() if m),
        "not in 1001": sum(1 for m in members.values() if 1001 not in m),
    }
    assert expected["inner"] > 0 and expected["exists"] < len(members)  # a vacuous fixture would prove nothing
    for label, (template, kind) in JOIN_SHAPES.items():
        got = connected.execute(template.format(m=macro, c=column)).fetchone()[0]
        assert got == expected[kind], (label, macro, column)


def test_existing_mapping_macros_resolve_through_the_attached_vocabulary(connected):
    assert connected.execute("SELECT map_to_standard_concept_id('ICD10CM', 'E11.9')").fetchone()[0] == 1001
    assert connected.execute("SELECT map_to_standard_concept_id('ICD10CM', 'nope')").fetchone()[0] == 0
    assert connected.execute("SELECT source_concept_id('ICD10CM', 'E11.9')").fetchone()[0] == 9001


def test_clamp_physiologic_semantics(connected):
    q = lambda sql: connected.execute(sql).fetchall()  # noqa: E731
    assert q("SELECT clamp_physiologic(x, 0, 100) FROM (VALUES (-5), (0), (50), (100), (150)) t(x)") == [
        (0,), (0,), (50,), (100,), (100,)]  # bounds are inclusive
    assert q("SELECT clamp_physiologic(NULL, 0, 100)") == [(None,)]  # NULL stays NULL
    assert q("SELECT clamp_physiologic(x, 0, 100) FROM (VALUES (-5), (NULL), (150)) t(x)") == [(0,), (None,), (100,)]
    assert q("SELECT clamp_physiologic(36.6::DOUBLE, 30, 45), clamp_physiologic(99.5::DOUBLE, 30, 45)") == [(36.6, 45.0)]
    # a NULL bound leaves that side open
    assert q("SELECT clamp_physiologic(-5, 0, NULL), clamp_physiologic(500, 0, NULL), "
             "clamp_physiologic(-5, NULL, 10), clamp_physiologic(50, NULL, 10), "
             "clamp_physiologic(7, NULL, NULL)") == [(0, 500, -5, 10, 7)]
    # bounds may come from columns, row by row
    assert q("SELECT clamp_physiologic(v, lo, hi) FROM (VALUES (5, 0, 10), (5, 6, 10), (5, 0, 4)) t(v, lo, hi)") == [
        (5,), (6,), (4,)]


def test_clamp_physiologic_rejects_an_inverted_range(connected):
    with pytest.raises(duckdb.InvalidInputException, match=r"min_val \(10\) is greater than max_val \(0\)"):
        connected.execute("SELECT clamp_physiologic(5, 10, 0)")
    with pytest.raises(duckdb.InvalidInputException, match="greater than max_val"):
        connected.execute("SELECT clamp_physiologic(NULL, 10, 0)")  # NULL value does not hide a bad range
    with pytest.raises(duckdb.InvalidInputException, match="greater than max_val"):
        connected.execute("SELECT clamp_physiologic(v, lo, hi) FROM (VALUES (5, 0, 10), (5, 10, 0)) t(v, lo, hi)")
    # no rows, no evaluation, no error
    assert connected.execute("SELECT clamp_physiologic(v, 10, 0) FROM range(0) t(v)").fetchall() == []


# ----------------------------------------------------------------------- relation to attach_central_vocabulary
def test_omop_connect_after_attach_central_vocabulary_does_not_conflict(cdm, vocab):
    con = duckdb.connect(str(cdm), read_only=True)
    try:
        attach_central_vocabulary(con, str(vocab), temporary=True)  # ATTACH + temporary views
        views_before = con.execute(
            "SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal ORDER BY 1").fetchall()
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            assert omop_connect(con) is con  # same attachment recognised: no second ATTACH, no error
        assert _omop_warnings(record) == []
        assert [d for d in _databases(con) if d == "central_vocab"] == ["central_vocab"]
        assert con.execute(
            "SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal ORDER BY 1"
        ).fetchall() == views_before  # its views were reused, none duplicated or replaced
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert _ids(con, "SELECT concept_id FROM descendants_of(1000)") == [1000, 1001, 1002, 1003]
    finally:
        con.close()


def _table_types(con, catalog):
    return {name: kind for name, kind in con.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_catalog = ? AND table_schema = 'main'", [catalog]).fetchall()}


def test_attach_central_vocabulary_after_omop_connect_reuses_the_attachment(cdm, vocab, capsys):
    """The reverse order of the test above: used to fail with 'database with name "central_vocab" already exists'."""
    con = omop_connect(cdm, vocab_db_path=vocab, read_only=True)
    try:
        attach_central_vocabulary(con, str(vocab), temporary=True)
        assert "Attached central vocabulary" in capsys.readouterr().out
        assert [d for d in _databases(con) if d == "central_vocab"] == ["central_vocab"]
        assert os.path.samefile(_databases(con)["central_vocab"], vocab)
        # the views were (re)created and everything still resolves
        views = {r[0] for r in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal").fetchall()}
        assert VOCAB_TABLES <= views
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert _ids(con, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
        assert _setting(con, "search_path") == "main,central_vocab.main"
    finally:
        con.close()


def test_attach_central_vocabulary_after_omop_connect_can_persist_the_views(cdm, vocab):
    con = omop_connect(cdm, vocab_db_path=vocab)  # TEMP views over the empty local tables are in front
    try:
        attach_central_vocabulary(con, str(vocab), temporary=False)
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        # the persistent views were written to the primary database in full, not hidden by or mixed up with the
        # TEMP views: the local tables are gone, replaced by views
        persisted = _table_types(con, "cdm")
        assert all(persisted[t] == "VIEW" for t in VOCAB_TABLES), persisted
        assert persisted["vocabulary"] == "BASE TABLE"  # tables the vocabulary does not hold are left alone
    finally:
        con.close()
    with duckdb.connect(str(cdm), read_only=True) as check:
        assert {t: _table_types(check, "cdm")[t] for t in VOCAB_TABLES} == dict.fromkeys(VOCAB_TABLES, "VIEW")
    again = omop_connect(cdm, read_only=True)
    try:
        assert again.execute("SELECT COUNT(*) FROM concept_ancestor").fetchone()[0] == len(ANCESTOR_ROWS)
    finally:
        again.close()


@pytest.mark.parametrize("temporary", [True, False], ids=["temporary", "persistent"])
def test_attach_central_vocabulary_is_idempotent(cdm, vocab, temporary, monkeypatch):
    con = duckdb.connect(str(cdm))
    try:
        for _ in range(3):
            attach_central_vocabulary(con, str(vocab), temporary=temporary)
        # the same file spelled differently (relative, backslashes, pathlib) is still the same attachment
        monkeypatch.chdir(vocab.parent)
        attach_central_vocabulary(con, "central_vocabulary.duckdb", temporary=temporary)
        attach_central_vocabulary(con, str(vocab).replace("/", os.sep), temporary=temporary)
        attach_central_vocabulary(con, vocab, temporary=temporary)
        assert [d for d in _databases(con) if d == "central_vocab"] == ["central_vocab"]
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert con.execute("SELECT COUNT(*) FROM concept_relationship").fetchone()[0] == len(RELATIONSHIP_ROWS)
        assert con.execute("SELECT COUNT(*) FROM duckdb_views() WHERE NOT internal AND view_name = 'concept' "
                           "AND database_name = ?", ["temp" if temporary else "cdm"]).fetchone()[0] == 1
    finally:
        con.close()


def test_attach_central_vocabulary_persistent_views_can_be_recreated_in_a_later_session(cdm, vocab):
    """Used to fail: DROP TABLE refuses the persistent views a previous session left behind."""
    with duckdb.connect(str(cdm)) as first:
        attach_central_vocabulary(first, str(vocab), temporary=False)
    with duckdb.connect(str(cdm)) as second:
        attach_central_vocabulary(second, str(vocab), temporary=False)
        assert second.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert {t: _table_types(second, "cdm")[t] for t in VOCAB_TABLES} == dict.fromkeys(VOCAB_TABLES, "VIEW")


@pytest.mark.parametrize("via", ["omop_connect", "attach_central_vocabulary"])
def test_attach_central_vocabulary_rejects_a_different_database_under_the_same_name(cdm, vocab, tmp_path, via):
    other = tmp_path / "other_vocabulary.duckdb"
    _make_vocab(other, tables={"concept"})
    con = omop_connect(cdm, vocab_db_path=vocab, read_only=True) if via == "omop_connect" else duckdb.connect(
        str(cdm), read_only=True)
    try:
        if via == "attach_central_vocabulary":
            attach_central_vocabulary(con, str(vocab), temporary=True)
        with pytest.raises(ValueError, match="different database is already attached"):
            attach_central_vocabulary(con, str(other), temporary=True)
        # nothing changed: the first vocabulary is still the one attached and in use
        assert os.path.samefile(_databases(con)["central_vocab"], vocab)
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
    finally:
        con.close()


def test_attach_central_vocabulary_missing_file_is_still_an_error(cdm, tmp_path):
    con = duckdb.connect(str(cdm), read_only=True)
    try:
        with pytest.raises(FileNotFoundError, match="Central vocabulary database not found"):
            attach_central_vocabulary(con, str(tmp_path / "nope.duckdb"))
        assert "central_vocab" not in _databases(con)
    finally:
        con.close()


def test_attach_central_vocabulary_escapes_quotes_and_expands_a_leading_tilde(tmp_path, cdm_template, fake_home):
    directory = tmp_path / "O'Brien's site"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    _make_vocab(directory / "central_vocabulary.duckdb")
    con = duckdb.connect(str(directory / "cdm.duckdb"), read_only=True)
    try:
        attach_central_vocabulary(con, str(directory / "central_vocabulary.duckdb"))
        assert os.path.samefile(_databases(con)["central_vocab"], directory / "central_vocabulary.duckdb")
    finally:
        con.close()
    shutil.copy(directory / "central_vocabulary.duckdb", fake_home / "v.duckdb")
    con = duckdb.connect(str(directory / "cdm.duckdb"), read_only=True)
    try:
        attach_central_vocabulary(con, "~/v.duckdb")
        assert os.path.samefile(_databases(con)["central_vocab"], fake_home / "v.duckdb")
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
    finally:
        con.close()


def test_omop_connect_reads_a_database_with_persistent_central_vocabulary_views(cdm, vocab):
    """attach_central_vocabulary(temporary=False) leaves persistent views over central_vocab in the file."""
    with duckdb.connect(str(cdm)) as w:
        attach_central_vocabulary(w, str(vocab), temporary=False)
    con = omop_connect(cdm, read_only=True)  # the views only resolve once the vocabulary is attached again
    try:
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)
        assert _ids(con, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
        assert con.execute("SELECT COUNT(*) FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal"
                           ).fetchone()[0] == 0  # the persistent views already point at the vocabulary
    finally:
        con.close()


# --------------------------------------------------------------------------------- no vocabulary found
def test_no_vocabulary_found_still_returns_a_connection_with_macros_and_a_clear_message(tmp_path, cdm_template):
    cdm_file = tmp_path / "lonely" / "cdm.duckdb"
    cdm_file.parent.mkdir()
    shutil.copy(cdm_template, cdm_file)
    with pytest.warns(UserWarning) as record:
        con = omop_connect(cdm_file, read_only=True)
    try:
        message = str(record[0].message)
        assert "No vocabulary available" in message
        assert "central_vocabulary.duckdb" in message and "vocab_db_path" in message
        assert record[0].filename == __file__  # points at the caller, not at the library
        assert "central_vocab" not in _databases(con)
        assert _shipped_macro_names() <= _macros(con, "temp")  # build_schema tables exist, so every macro loads
        assert con.execute("SELECT clamp_physiologic(500, 0, 300)").fetchone()[0] == 300
        assert con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
    finally:
        con.close()


def test_no_warning_when_the_database_carries_its_own_vocabulary(cdm, tmp_path):
    local = tmp_path / "local" / "cdm.duckdb"
    local.parent.mkdir()
    shutil.copy(cdm, local)
    with duckdb.connect(str(local)) as w:
        w.execute("INSERT INTO concept VALUES (1001, 'T2DM', 'Condition', 'SNOMED', 'Disorder', 'S', 'T2DM', "
                  "DATE '1970-01-01', DATE '2099-12-31', NULL)")
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        con = omop_connect(local, read_only=True)
    try:
        assert _omop_warnings(record) == []
        assert "central_vocab" not in _databases(con)
        assert con.execute("SELECT concept_name FROM concept").fetchall() == [("T2DM",)]
    finally:
        con.close()


def test_database_without_any_vocabulary_tables_skips_only_the_macros_that_need_them(tmp_path):
    bare = tmp_path / "bare.duckdb"
    with duckdb.connect(str(bare)) as w:
        w.execute("CREATE TABLE person (person_id INTEGER)")
    with pytest.warns(UserWarning) as record:
        con = omop_connect(bare, read_only=True)
    try:
        message = str(record[0].message)
        skipped = {"map_to_standard_concept_id", "source_concept_id"}
        assert all(name in message for name in skipped)
        assert "descendants_of" not in message and "ancestors_of" not in message
        assert (_shipped_macro_names() - skipped) <= _macros(con, "temp")
        assert not skipped & _macros(con, "temp")
        assert con.execute("SELECT clamp_physiologic(-4, 0, 10)").fetchone()[0] == 0
        # the ancestry macros are created anyway and report the missing table when they are used
        for macro in ("descendants_of", "ancestors_of"):
            with pytest.raises(duckdb.CatalogException, match="concept_ancestor"):
                con.execute(f"SELECT * FROM {macro}(1)")
    finally:
        con.close()


def test_opting_out_of_a_vocabulary_on_a_database_without_one_does_not_warn(tmp_path):
    bare = tmp_path / "bare.duckdb"
    with duckdb.connect(str(bare)) as w:
        w.execute("CREATE TABLE person (person_id INTEGER)")
    for target in (":memory:", bare):
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            con = omop_connect(target, auto_attach_vocab=False)
        try:
            assert _omop_warnings(record) == []
            assert "clamp_physiologic" in _macros(con, "temp")  # everything that needs no vocabulary still loads
            assert not {"map_to_standard_concept_id", "source_concept_id"} & _macros(con, "temp")
        finally:
            con.close()


def test_opting_out_still_warns_about_a_vocabulary_that_is_present_but_incomplete(tmp_path, cdm, vocab):
    partial = tmp_path / "partial.duckdb"
    with duckdb.connect(str(partial)) as w:
        w.execute("CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR)")  # no concept_ancestor
    with pytest.warns(UserWarning, match="map_to_standard_concept_id") as record:
        con = omop_connect(partial, read_only=True, auto_attach_vocab=False)
    con.close()
    assert "No vocabulary available" not in str(record[0].message)  # the user opted out of the search

    # persistent views over a vocabulary this connection cannot find: present but dangling, so still named
    with duckdb.connect(str(cdm)) as w:
        attach_central_vocabulary(w, str(vocab), temporary=False)
    lonely = tmp_path / "lonely" / "cdm.duckdb"
    lonely.parent.mkdir()
    shutil.copy(cdm, lonely)
    with pytest.warns(UserWarning, match="map_to_standard_concept_id"):
        con = omop_connect(lonely, read_only=True, auto_attach_vocab=False)
    con.close()


def test_persistent_vocabulary_views_that_dangle_give_the_no_vocabulary_message(cdm, vocab, tmp_path):
    """attach_central_vocabulary(temporary=False) views point at a vocabulary this connection cannot find."""
    with duckdb.connect(str(cdm)) as w:
        attach_central_vocabulary(w, str(vocab), temporary=False)
    lonely = tmp_path / "lonely" / "cdm.duckdb"
    lonely.parent.mkdir()
    shutil.copy(cdm, lonely)  # no sibling vocabulary here, so the views cannot resolve
    with pytest.warns(UserWarning) as record:
        con = omop_connect(lonely, read_only=True)
    try:
        message = str(record[0].message)
        assert "No vocabulary available" in message
        assert "map_to_standard_concept_id" in message  # its table is a dangling view: the macro is skipped and named
        assert con.execute("SELECT clamp_physiologic(500, 0, 300)").fetchone()[0] == 300
        # naming the vocabulary afterwards repairs the connection
        assert omop_connect(con, vocab_db_path=vocab) is con
        assert _ids(con, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
    finally:
        con.close()


def test_load_sql_macros_false_creates_no_macros(cdm):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        con = omop_connect(cdm, read_only=True, load_sql_macros=False)
    try:
        assert _omop_warnings(record) == []
        assert _macros(con, "temp") == set()
        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM descendants_of(1000)")
        assert con.execute("SELECT COUNT(*) FROM concept").fetchone()[0] == len(CONCEPT_ROWS)  # vocabulary still attached
    finally:
        con.close()


def test_a_new_path_is_created_as_an_empty_database(tmp_path):
    path = tmp_path / "fresh.duckdb"
    with pytest.warns(UserWarning, match="No vocabulary available"):
        con = omop_connect(path)
    try:
        assert path.exists()
        assert con.execute("SELECT clamp_physiologic(-4, 0, 10)").fetchone()[0] == 0
    finally:
        con.close()


def test_in_memory_database(vocab):
    with pytest.warns(UserWarning, match="in-memory database has no directory"):
        con = omop_connect(":memory:")
    con.close()
    # an explicit vocabulary works for an in-memory database (macros need no local tables then)
    con = omop_connect(":memory:", vocab_db_path=vocab)
    try:
        assert _ids(con, "SELECT concept_id FROM descendants_of(1001)") == [1001, 1002]
        assert con.execute("SELECT concept_name FROM concept WHERE concept_id = 2000").fetchone()[0] == (
            "Hypertensive disorder")
    finally:
        con.close()


def test_vocabulary_without_ancestor_table_reports_it_when_the_ancestry_macros_are_used(tmp_path, cdm):
    thin = tmp_path / "thin_vocab.duckdb"
    _make_vocab(thin, tables={"concept", "concept_relationship"})
    bare = tmp_path / "bare.duckdb"
    with duckdb.connect(str(bare)) as w:
        w.execute("CREATE TABLE person (person_id INTEGER)")
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        con = omop_connect(bare, vocab_db_path=thin, read_only=True)
    try:
        assert _omop_warnings(record) == []  # nothing the macros need at creation time is missing
        assert {"map_to_standard_concept_id", "descendants_of", "ancestors_of"} <= _macros(con, "temp")
        assert con.execute("SELECT map_to_standard_concept_id('ICD10CM', 'E11.9')").fetchone()[0] == 1001
        for macro in ("descendants_of", "ancestors_of"):
            with pytest.raises(duckdb.CatalogException, match="concept_ancestor"):
                con.execute(f"SELECT * FROM {macro}(1000)")
    finally:
        con.close()


def _empty_database(path):
    with duckdb.connect(str(path)) as w:
        w.execute("CREATE TABLE unrelated (x INTEGER)")
    return path


def test_an_explicit_vocabulary_without_vocabulary_tables_warns(tmp_path, cdm):
    empty = _empty_database(tmp_path / "not_a_vocabulary.duckdb")
    with pytest.warns(UserWarning, match="contains none of the vocabulary tables") as record:
        con = omop_connect(cdm, vocab_db_path=empty, read_only=True)
    try:
        message = str(record[0].message)
        assert "not_a_vocabulary.duckdb" in message and "vocab_db_path" in message
        assert "No vocabulary available" not in message  # something was attached, it just holds nothing
        assert "central_vocab" in _databases(con)  # the connection is still returned and usable
        assert con.execute("SELECT clamp_physiologic(500, 0, 300)").fetchone()[0] == 300
        assert con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 4
    finally:
        con.close()


def test_a_discovered_sibling_without_vocabulary_tables_warns_too(tmp_path, cdm_template):
    directory = tmp_path / "site"
    directory.mkdir()
    shutil.copy(cdm_template, directory / "cdm.duckdb")
    _empty_database(directory / "vocab.duckdb")
    with pytest.warns(UserWarning, match="contains none of the vocabulary tables"):
        con = omop_connect(directory / "cdm.duckdb", read_only=True)
    con.close()


def test_a_real_vocabulary_gives_no_empty_vocabulary_warning(cdm, vocab, tmp_path):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        omop_connect(cdm, vocab_db_path=vocab, read_only=True).close()
        # a single vocabulary table is enough for it not to be an empty vocabulary
        thin = tmp_path / "thin.duckdb"
        _make_vocab(thin, tables={"concept"})
        omop_connect(cdm, vocab_db_path=thin, read_only=True).close()
    assert not any("none of the vocabulary tables" in m for m in _omop_warnings(record))


def test_stub_vocabulary_missing_columns_skips_only_the_macros_that_read_them(tmp_path):
    """A hand-made vocabulary without the usual columns still connects; the gap is named, not raised."""
    stub = tmp_path / "stub_vocab.duckdb"
    with duckdb.connect(str(stub)) as w:
        w.execute("CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR)")
        w.execute("INSERT INTO concept VALUES (1, 'stub')")
        w.execute("CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER, "
                  "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)")
        w.execute("INSERT INTO concept_ancestor VALUES (1, 1, 0, 0)")
    bare = tmp_path / "bare.duckdb"
    with duckdb.connect(str(bare)) as w:
        w.execute("CREATE TABLE person (person_id INTEGER)")
    with pytest.warns(UserWarning, match="tables or columns") as record:
        con = omop_connect(bare, vocab_db_path=stub, read_only=True)
    try:
        message = str(record[0].message)
        assert "source_concept_id" in message and "map_to_standard_concept_id" in message
        assert "descendants_of" not in message
        assert _ids(con, "SELECT concept_id FROM descendants_of(1)") == [1]
        assert con.execute("SELECT clamp_physiologic(-4, 0, 10)").fetchone()[0] == 0
    finally:
        con.close()


# --------------------------------------------------------------------------- optional real-vocabulary check
@pytest.mark.skipif(
    not os.environ.get("OMOP_VOCAB_DB") or not os.path.isfile(os.environ.get("OMOP_VOCAB_DB", "")),
    reason="set OMOP_VOCAB_DB to an Athena vocabulary DuckDB file to run this smoke test",
)
def test_descendants_of_on_a_real_vocabulary():
    vocab_file = os.environ["OMOP_VOCAB_DB"]
    stat = lambda: (os.stat(vocab_file).st_size, os.stat(vocab_file).st_mtime_ns)  # noqa: E731 (not a hash: may be GBs)
    before = stat()
    con = omop_connect(":memory:", vocab_db_path=vocab_file)  # attached READ_ONLY; nothing is written to it
    try:
        # 201826 = SNOMED 'Type 2 diabetes mellitus' (the RFC text's 316866 is 'Hypertensive disorder')
        assert con.execute("SELECT concept_name FROM concept WHERE concept_id = 201826").fetchone()[0] == (
            "Type 2 diabetes mellitus")
        descendants = _ids(con, "SELECT concept_id FROM descendants_of(201826)")
        assert 201826 in descendants and len(descendants) > 1  # self row plus more specific concepts
        assert con.execute(
            "SELECT COUNT(*) FROM concept c JOIN descendants_of(201826) d USING (concept_id) "
            "WHERE c.standard_concept = 'S'").fetchone()[0] >= 1
        ancestors = _ids(con, "SELECT concept_id FROM ancestors_of(201826)")
        assert 201826 in ancestors and 201820 in ancestors  # 201820 = 'Diabetes mellitus'
    finally:
        con.close()
    assert stat() == before
