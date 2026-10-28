"""Zero-copy BigQuery -> DuckDB sync for Athena vocabulary tables (and any OMOP-shaped table).

Rows stream from BigQuery as Arrow record batches (the BigQuery Storage Read API when
``google-cloud-bigquery-storage`` is installed) and are appended to DuckDB straight from memory: no CSV/TSV export, no
Athena zip, no intermediate file on disk. Install the client with ``pip install omop-duck-db[bigquery]``.

``read_bigquery_table`` / ``sync_vocabulary_from_bigquery`` accept ``reader=`` (any callable
``(table_ref, columns) -> iterable of pyarrow.RecordBatch``) so tests and other Arrow sources (for example Arrow
Flight) can plug in without BigQuery credentials.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Iterator, Sequence

import duckdb

__all__ = ["VOCABULARY_TABLES", "read_bigquery_table", "sync_vocabulary_from_bigquery"]

VOCABULARY_TABLES = (
    "concept", "vocabulary", "domain", "concept_class", "relationship", "concept_relationship",
    "concept_ancestor", "concept_synonym", "drug_strength",
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT = re.compile(r"^[A-Za-z0-9._:-]+$")

Reader = Callable[[str, "Sequence[str] | None"], Iterable[Any]]


class _EmptyTable(ValueError):
    """The source returned no batches, so the table is missing or has no schema."""


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"{what} must be a plain identifier, got {name!r}")
    return name


def _bq_reader(billing_project: str | None, client: Any = None) -> Reader:
    def read(table_ref: str, columns: Sequence[str] | None) -> Iterator[Any]:
        nonlocal client
        if client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:
                raise ImportError("BigQuery sync needs google-cloud-bigquery: pip install omop-duck-db[bigquery]") from exc
            client = bigquery.Client(project=billing_project)
        storage = None
        try:
            from google.cloud import bigquery_storage
            storage = bigquery_storage.BigQueryReadClient()
        except Exception:  # noqa: BLE001 - optional; falls back to the REST path (slow for large tables)
            pass
        rows = client.list_rows(table_ref)
        yield from (rows.to_arrow_iterable(bqstorage_client=storage) if storage is not None else rows.to_arrow_iterable())
    return read


def read_bigquery_table(con: duckdb.DuckDBPyConnection, source: str, table: str, *, dataset: str | None = None,
                        billing_project: str | None = None, mode: str = "overwrite", reader: Reader | None = None,
                        client: Any = None, transaction: bool = True) -> int:
    """Streams one BigQuery table into DuckDB ``table``; returns the number of rows loaded.

    ``source`` is ``"project.dataset"`` (``dataset`` is accepted instead for symmetry with the R helper). ``mode`` is
    ``"overwrite"`` (replace the table) or ``"append"`` (add rows; creates the table when missing). The target is
    created from the first Arrow batch's schema, so column types follow BigQuery's. The load runs in its own transaction and
    swaps the staged table in only on success; pass ``transaction=False`` when the caller already holds a transaction
    (DuckDB cannot nest them) and will commit or roll back itself.
    """
    import pyarrow as pa

    if mode not in ("overwrite", "append"):
        raise ValueError("mode must be 'overwrite' or 'append'")
    src = dataset or source
    parts = src.split(".")
    if len(parts) != 2 or not all(_PROJECT.match(p) for p in parts):
        raise ValueError("source must be 'project.dataset', e.g. 'my-project.athena_vocab'")
    tbl = _ident(table, "table")
    read = reader or _bq_reader(billing_project, client)
    ref = f"{src}.{tbl}"

    schema = con.execute("SELECT current_schema()").fetchone()[0]
    kind = con.execute("SELECT table_type FROM information_schema.tables WHERE table_schema = ? AND lower(table_name) = lower(?)",
                       [schema, tbl]).fetchone()
    if kind is not None and "VIEW" in kind[0].upper():
        raise ValueError(f"target {schema}.{tbl} is a view; refusing to replace or append to it")
    exists = kind is not None

    # Stream into a staging table inside one transaction, then swap: a failure mid-stream leaves the target untouched.
    stage, batch_view = f"_bq_new_{tbl}", f"_bq_batch_{tbl}"
    own_txn = bool(transaction)
    if own_txn:
        con.execute("BEGIN TRANSACTION")
    n, started = 0, False
    try:
        con.execute(f'DROP TABLE IF EXISTS "{stage}"')
        for batch in read(ref, None):
            if batch.num_rows == 0 and started:
                continue
            con.register(batch_view, pa.Table.from_batches([batch]))
            try:
                if not started:
                    con.execute(f'CREATE TABLE "{stage}" AS SELECT * FROM {batch_view} WHERE false')
                    started = True
                con.execute(f'INSERT INTO "{stage}" BY NAME SELECT * FROM {batch_view}')
            finally:
                con.unregister(batch_view)
            n += batch.num_rows
        if not started:
            raise _EmptyTable(f"BigQuery table {ref} returned no batches (not found or no schema)")
        if mode == "overwrite" or not exists:
            con.execute(f'DROP TABLE IF EXISTS "{tbl}"')
            con.execute(f'ALTER TABLE "{stage}" RENAME TO "{tbl}"')
        else:
            con.execute(f'INSERT INTO "{tbl}" BY NAME SELECT * FROM "{stage}"')
            con.execute(f'DROP TABLE "{stage}"')
    except BaseException:
        if own_txn:
            con.execute("ROLLBACK")
        raise
    if own_txn:
        con.execute("COMMIT")
    return n


def sync_vocabulary_from_bigquery(con: duckdb.DuckDBPyConnection, source: str,
                                  tables: Sequence[str] = VOCABULARY_TABLES, *, billing_project: str | None = None,
                                  mode: str = "overwrite", reader: Reader | None = None, client: Any = None,
                                  skip_missing: bool = False) -> dict[str, int]:
    """Loads the Athena vocabulary tables from a BigQuery dataset into DuckDB; returns ``{table: rows}``.

    Use after copying an Athena download into BigQuery once (shared by the consortium), so every site attaches the
    vocabulary without handling the CSV zip. ``skip_missing=True`` skips tables the dataset lacks (for example
    ``drug_strength`` when not licensed) instead of raising.
    """
    out: dict[str, int] = {}
    for t in tables:
        try:
            out[t] = read_bigquery_table(con, source, t, billing_project=billing_project, mode=mode, reader=reader, client=client)
        except Exception as exc:  # noqa: BLE001 - BigQuery NotFound is not importable without the client
            # Skip only "table not found" (BigQuery NotFound, or our empty-result error); validation errors still raise.
            if skip_missing and (type(exc).__name__ == "NotFound" or isinstance(exc, _EmptyTable)):
                continue
            raise
    return out
