"""Coverage for the paths added by the v0.20.0 review fixes (transactions, validation, compat shims)."""
from pathlib import Path

import duckdb
import pytest

from omop_etl import etl_pcornet

SAMPLE_DIR = Path(__file__).resolve().parent / "fixtures" / "pcornet_sample"


def _count(db, table):
    c = duckdb.connect(str(db))
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_full_mode_rerun_does_not_double_tables(tmp_path):
    db = tmp_path / "cdm.duckdb"
    etl_pcornet(str(SAMPLE_DIR), str(db), build_schema_flag=True, key_check="ignore")
    first = {t: _count(db, t) for t in ("person", "visit_occurrence", "condition_occurrence", "measurement", "care_site")}
    etl_pcornet(str(SAMPLE_DIR), str(db), build_schema_flag=True, key_check="ignore")
    assert {t: _count(db, t) for t in first} == first
    assert first["person"] > 0


def test_etl_rejects_bad_arguments(tmp_path):
    with pytest.raises(ValueError, match="use_enrollment"):
        etl_pcornet(str(SAMPLE_DIR), str(tmp_path / "a.duckdb"), use_enrollment="yes")
    with pytest.raises(ValueError, match="memory_limit"):
        etl_pcornet(str(SAMPLE_DIR), str(tmp_path / "b.duckdb"), memory_limit="8GB'; DROP TABLE x; --")


def test_atomic_commits_rolls_back_and_nests():
    from omop_etl.build_omop_cdm import _atomic

    c = duckdb.connect()
    c.execute("CREATE TABLE t (i INTEGER)")
    with _atomic(c):
        c.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError):
        with _atomic(c):
            c.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    assert c.execute("SELECT i FROM t").fetchall() == [(1,)]
    with _atomic(c):  # inside an open transaction the block joins it instead of failing
        with _atomic(c):
            c.execute("INSERT INTO t VALUES (3)")
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_arrow_helper_covers_every_duckdb_api_shape():
    from omop_etl._arrow import to_arrow_table

    class New:
        def to_arrow_table(self):
            return "new"

    class Mid:
        def fetch_arrow_table(self):
            return "mid"

    class Reader:
        def read_all(self):
            return "reader"

    class Old:
        def arrow(self):
            return Reader()

    class Bare:
        def arrow(self):
            return "bare"

    assert [to_arrow_table(x) for x in (New(), Mid(), Old(), Bare())] == ["new", "mid", "reader", "bare"]
    assert to_arrow_table(duckdb.connect().execute("SELECT 1 AS a")).num_rows == 1


def test_path_helpers_validate_and_escape(tmp_path):
    from omop_etl.paths import attach_database, copy_to

    c = duckdb.connect()
    db = tmp_path / "x.duckdb"
    duckdb.connect(str(db)).close()
    with pytest.raises(ValueError):
        attach_database(c, db, "a; DROP TABLE t")
    with pytest.raises(ValueError):
        attach_database(c, db, "ok", db_type="x y")
    attach_database(c, db, "ok", read_only=True)
    c.execute("CREATE TABLE t AS SELECT 1 AS a")
    with pytest.raises(ValueError):
        copy_to(c, "t", tmp_path / "o.csv", format="CSV; DROP TABLE t")
    with pytest.raises(ValueError):
        copy_to(c, "t", tmp_path / "o.csv", format="CSV", **{"bad key": 1})
    copy_to(c, "t", tmp_path / "it's.csv", format="CSV", header=True, delimiter="'")
    assert (tmp_path / "it's.csv").exists()


def test_export_validates_tables_and_compression(tmp_path):
    from omop_etl.export import export_cdm

    c = duckdb.connect()
    with pytest.raises(ValueError):
        export_cdm(c, "parquet", tmp_path, tables=["person; DROP TABLE person"])
    with pytest.raises(ValueError):
        export_cdm(c, "parquet", tmp_path, tables=["person"], compression="zstd'); --")


def test_load_vocabulary_missing_requested_file_and_cpt4_rollback(tmp_path):
    from omop_etl.build_omop_cdm import build_schema
    from omop_etl.vocabulary import load_vocabulary

    db = tmp_path / "v.duckdb"
    con = duckdb.connect(str(db))
    build_schema(con)
    con.close()
    vocab = tmp_path / "vocab"
    vocab.mkdir()
    (vocab / "DOMAIN.csv").write_text("domain_id\tdomain_name\tdomain_concept_id\nCondition\tCondition\t19\n", encoding="utf-8")
    assert load_vocabulary(str(vocab), str(db), tables=["domain", "concept"]) is False  # concept.csv absent
    assert _count(db, "domain") == 1
    (vocab / "CONCEPT_CPT4.csv").write_text("not\ta\tconcept\nfile\n", encoding="utf-8")
    (vocab / "CONCEPT.csv").write_text(
        "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\tstandard_concept\tconcept_code\t"
        "valid_start_date\tvalid_end_date\tinvalid_reason\n"
        "1\tx\tCondition\tSNOMED\tClinical Finding\tS\tc1\t19700101\t20991231\t\n", encoding="utf-8")
    assert load_vocabulary(str(vocab), str(db), tables=["concept"]) is False  # malformed CPT4 file rolls back, flagged
    assert _count(db, "concept") == 1


def test_use_enrollment_and_macro_loader_guard():
    from omop_etl.build_omop_cdm import load_macros

    c = duckdb.connect()
    assert isinstance(load_macros(c, skip_unresolved=True), list)  # tolerates statements that need missing tables


def _typed_tables(c):
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id INT, visit_type_concept_id INT)")
    c.execute("INSERT INTO visit_occurrence VALUES (1, 32817), (2, 44818518)")
    c.execute("CREATE TABLE condition_occurrence (condition_occurrence_id INT, condition_type_concept_id INT)")
    c.execute("INSERT INTO condition_occurrence VALUES (1, 32840), (2, 32901)")
    c.execute("CREATE TABLE measurement (measurement_id INT, measurement_type_concept_id INT, measurement_concept_id INT, value_as_number DOUBLE)")
    c.execute("INSERT INTO measurement VALUES (1, 32817, 3004410, 5.0), (2, 44818702, 3004410, 6.0)")


def test_filter_helpers_write_output_tables_and_reject_unsafe_names():
    from omop_etl import filter_conditions, filter_measurements, filter_visits

    c = duckdb.connect()
    _typed_tables(c)
    for fn, table, kw in ((filter_visits, "visit_occurrence", {"type_concept_ids": [32817]}),
                          (filter_conditions, "condition_occurrence", {"type_concept_ids": [32840]}),
                          (filter_measurements, "measurement", {"type_concept_ids": [32817]})):
        out = fn(c, output_table=f"out_{table}", **kw)
        assert len(out) == 1
        assert c.execute(f"SELECT count(*) FROM out_{table}").fetchone()[0] == 1
        with pytest.raises(ValueError, match="output_table"):
            fn(c, output_table="x; DROP TABLE person", **kw)


def test_normalize_units_validates_output_table():
    from omop_etl import normalize_units

    c = duckdb.connect()
    _typed_tables(c)
    with pytest.raises(ValueError, match="output_table"):
        normalize_units(c, concept_id=3004410, output_table="bad name")
    out = normalize_units(c, concept_id=3004410, output_table="norm_out")
    assert len(out) == 2 and c.execute("SELECT count(*) FROM norm_out").fetchone()[0] == 2
