"""v0.8.7: BigQuery -> DuckDB streaming sync, exercised with a fake Arrow reader / client (no GCP credentials)."""
import duckdb
import pyarrow as pa
import pytest

from omop_etl.bigquery_sync import read_bigquery_table, sync_vocabulary_from_bigquery


def _batches(n, size=4):
    ids = list(range(n))
    for i in range(0, n, size):
        yield pa.record_batch([pa.array(ids[i:i + size], pa.int64()), pa.array([f"c{j}" for j in ids[i:i + size]])],
                              names=["concept_id", "concept_name"])


def reader(ref, cols):
    assert ref.startswith("proj.vocab.")
    return _batches(10)


def test_streams_batches_and_overwrites_or_appends():
    con = duckdb.connect()
    assert read_bigquery_table(con, "proj.vocab", "concept", reader=reader) == 10
    assert read_bigquery_table(con, "proj.vocab", "concept", reader=reader, mode="append") == 10
    assert con.execute("SELECT count(*), count(DISTINCT concept_id) FROM concept").fetchone() == (20, 10)
    read_bigquery_table(con, "proj.vocab", "concept", reader=reader)
    assert con.execute("SELECT count(*) FROM concept").fetchone()[0] == 10
    assert con.execute("SELECT typeof(concept_id) FROM concept LIMIT 1").fetchone()[0] == "BIGINT"


def test_sync_vocabulary_and_skip_missing():
    con = duckdb.connect()

    def rd(ref, cols):
        if ref.endswith("drug_strength"):
            return iter(())
        return _batches(3)

    with pytest.raises(ValueError, match="no batches"):
        sync_vocabulary_from_bigquery(con, "proj.vocab", ["concept", "drug_strength"], reader=rd)
    out = sync_vocabulary_from_bigquery(con, "proj.vocab", ["concept", "vocabulary", "drug_strength"], reader=rd, skip_missing=True)
    assert out == {"concept": 3, "vocabulary": 3}


def test_validation_and_client_path():
    con = duckdb.connect()
    for bad in ("concept; DROP TABLE x", "1abc", ""):
        with pytest.raises(ValueError):
            read_bigquery_table(con, "proj.vocab", bad, reader=reader)
    with pytest.raises(ValueError, match="project.dataset"):
        read_bigquery_table(con, "justone", "concept", reader=reader)
    with pytest.raises(ValueError, match="mode"):
        read_bigquery_table(con, "proj.vocab", "concept", reader=reader, mode="x")

    class Rows:
        def to_arrow_iterable(self):
            return _batches(5)

    class Client:
        def list_rows(self, ref):
            assert ref == "proj.vocab.concept"
            return Rows()

    assert read_bigquery_table(con, "proj.vocab", "concept", client=Client()) == 5
