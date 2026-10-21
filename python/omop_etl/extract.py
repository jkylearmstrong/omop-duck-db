"""Source-database neutrality: stream tables from any SQLAlchemy-supported server to Parquet or DuckDB.

* :func:`from_sql` - chunked, streaming export of source tables to Parquet (``omop-duckdb extract``).
* :func:`import_omop` - copy an OMOP CDM that already lives in a server database into a local DuckDB file
  (``omop-duckdb import-omop``), so the PCORnet ETL is skipped and DQD / feature code runs unchanged.

Optional engines are installed with extras: ``omop-duck-db[postgres|mssql|oracle|snowflake]``. SQLAlchemy itself
is imported lazily; the base install does not need it.

Credentials: pass the URL as ``env:VARNAME`` (read from the environment) or as a SQLAlchemy URL. The URL is never
logged or returned; messages show it with the password masked.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb

__all__ = ["from_sql", "import_omop", "resolve_url", "CDM_STUDY_TABLES", "CDM_VOCAB_TABLES"]

log = logging.getLogger(__name__)

CDM_STUDY_TABLES = [
    "person", "observation_period", "visit_occurrence", "visit_detail", "condition_occurrence",
    "procedure_occurrence", "drug_exposure", "measurement", "observation", "device_exposure", "death",
    "condition_era", "drug_era", "provider", "care_site", "location", "cdm_source",
]
CDM_VOCAB_TABLES = [
    "concept", "concept_ancestor", "concept_relationship", "concept_synonym", "vocabulary", "domain",
    "concept_class", "relationship", "drug_strength",
]


def _sa():
    try:
        import sqlalchemy as sa
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "SQLAlchemy is required for server extraction: pip install sqlalchemy plus a driver extra, e.g. "
            "pip install 'omop-duck-db[postgres]' (also [mssql], [oracle], [snowflake])."
        ) from exc
    return sa


def resolve_url(url: str) -> str:
    """Returns the real URL: ``env:NAME`` is read from the environment variable NAME."""
    if url.startswith("env:"):
        name = url[4:]
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"Environment variable {name!r} (given as --url env:{name}) is not set.")
        return value
    return url


def _masked(url: str) -> str:
    sa = _sa()
    try:
        return sa.engine.make_url(url).render_as_string(hide_password=True)
    except Exception:  # unparsable: never echo it
        return "<url>"


def _make_engine(url: str):
    sa = _sa()
    real = resolve_url(url)
    try:
        return sa.create_engine(real)
    except Exception as exc:
        # SQLAlchemy messages can echo the URL; replace it
        raise RuntimeError(f"Could not create engine for {_masked(real)}: {type(exc).__name__}") from None


def _arrow_type(col_type: Any):
    """Map a reflected SQLAlchemy column type to an Arrow type (None -> infer / stringify)."""
    import pyarrow as pa

    sa = _sa()
    t = col_type
    if isinstance(t, sa.Boolean):
        return pa.bool_()
    if isinstance(t, sa.Integer):
        return pa.int64()
    if isinstance(t, sa.Numeric) and not isinstance(t, sa.Float):
        prec, scale = getattr(t, "precision", None), getattr(t, "scale", None)
        if scale == 0 and prec is not None and prec <= 18:
            return pa.int64()  # Oracle NUMBER(n) -> integer
        if prec is not None and scale is not None and prec <= 38:
            return pa.decimal128(prec, scale)
        return pa.float64()  # Oracle NUMBER without precision
    if isinstance(t, sa.Float):
        return pa.float64()
    if isinstance(t, sa.DateTime):  # includes Oracle DATE (carries time), SQL Server datetime2
        return pa.timestamp("us")
    if isinstance(t, sa.Date):
        return pa.date32()
    if isinstance(t, sa.LargeBinary):
        return pa.binary()
    if isinstance(t, (sa.String, sa.Text)):
        return pa.string()
    return None


def _convert(values: list, arrow_type):
    import pyarrow as pa

    if arrow_type is None:
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())
    if pa.types.is_integer(arrow_type):
        values = [None if v is None else int(v) for v in values]
    elif pa.types.is_floating(arrow_type):
        values = [None if v is None else float(v) for v in values]
    elif pa.types.is_decimal(arrow_type):
        values = [None if v is None else decimal.Decimal(str(v)) for v in values]
    elif pa.types.is_date(arrow_type):
        values = [v.date() if isinstance(v, _dt.datetime) else v for v in values]
    elif pa.types.is_string(arrow_type):
        values = [None if v is None else str(v) for v in values]
    return pa.array(values, type=arrow_type)


def _resolve_table_name(inspector, requested: str, schema: str | None) -> str:
    """Case-insensitive lookup (Oracle / Snowflake fold case differently from the names a site types)."""
    names = inspector.get_table_names(schema=schema)
    try:
        names = names + inspector.get_view_names(schema=schema)
    except Exception:  # some dialects lack views listing
        pass
    for n in names:
        if n == requested:
            return n
    matches = [n for n in names if n.lower() == requested.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"Table {requested!r} not found" + (f" in schema {schema!r}" if schema else "") + ".")
    raise LookupError(f"Table name {requested!r} is ambiguous ({matches}); use the exact name via a mapping file.")


def _load_mapping(mapping: Mapping[str, str] | str | os.PathLike | None) -> dict[str, str]:
    if mapping is None:
        return {}
    if isinstance(mapping, Mapping):
        return {str(k).lower(): str(v) for k, v in mapping.items()}
    p = Path(mapping)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return {str(k).lower(): str(v) for k, v in json.loads(text).items()}
    out: dict[str, str] = {}
    for line in text.splitlines():  # csv / simple "expected,source_table" lines
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("expected"):
            continue
        a, _, b = line.partition(",")
        out[a.strip().lower()] = b.strip()
    return out


def from_sql(
    url: str,
    tables: Sequence[str],
    out_dir: str | os.PathLike,
    *,
    chunksize: int = 500_000,
    schema: str | None = None,
    table_map: Mapping[str, str] | str | os.PathLike | None = None,
    compression: str = "zstd",
) -> list[dict[str, Any]]:
    """Streams ``tables`` from a SQLAlchemy URL into ``out_dir/<table>.parquet`` (one file each).

    Args:
        url: SQLAlchemy URL or ``env:VARNAME``. Never logged.
        tables: Expected (PCORnet / OMOP) table names; each ``tables`` entry is also accepted as ``name=SOURCE``.
        out_dir: Output directory (created).
        chunksize: Rows per fetch / Parquet row group.
        schema: Source schema.
        table_map: ``{expected_name: actual_source_table}`` (dict, JSON object, or ``expected,source`` CSV) for
            sites whose tables are not named like PCORnet.
        compression: Parquet codec.

    Returns:
        One dict per table: ``table``, ``source_table``, ``rows``, ``columns`` ({name: arrow dtype}), ``file``.
        Output columns are lower-cased (Oracle / Snowflake upper-case identifiers); table names in the output
        files are the expected (mapped) names, lower-cased.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    sa = _sa()
    if int(chunksize) < 1:
        raise ValueError("chunksize must be >= 1.")
    mapping = _load_mapping(table_map)
    requests: list[tuple[str, str | None]] = []
    for t in tables:
        t = str(t).strip()
        if not t:
            continue
        exp, eq, src = t.partition("=")
        requests.append((exp.strip(), src.strip() if eq else None))
    if not requests:
        raise ValueError("No tables requested.")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    engine = _make_engine(url)
    report: list[dict[str, Any]] = []
    try:
        inspector = sa.inspect(engine)
        for expected, explicit in requests:
            wanted = explicit or mapping.get(expected.lower(), expected)
            actual = _resolve_table_name(inspector, wanted, schema)
            md = sa.MetaData()
            tbl = sa.Table(actual, md, schema=schema, autoload_with=engine)
            names = [c.name.lower() for c in tbl.columns]
            if len(set(names)) != len(names):
                raise ValueError(f"Table {actual!r} has columns that collide once lower-cased: {names}")
            types = [_arrow_type(c.type) for c in tbl.columns]
            fields = []
            for n, ty in zip(names, types):
                fields.append(pa.field(n, ty if ty is not None else pa.string()))
            schema_pa = pa.schema(fields)
            target = out / f"{expected.lower()}.parquet"
            tmp = target.with_suffix(".parquet.tmp")
            rows = 0
            with engine.connect() as conn:
                result = conn.execution_options(stream_results=True).execute(sa.select(tbl))
                with pq.ParquetWriter(str(tmp), schema_pa, compression=compression) as writer:
                    while True:
                        batch = result.fetchmany(int(chunksize))
                        if not batch:
                            break
                        cols = list(zip(*batch))
                        arrays = [_convert(list(cols[i]), types[i]) for i in range(len(names))]
                        writer.write_table(pa.Table.from_arrays(arrays, schema=schema_pa))
                        rows += len(batch)
                        log.info("extract %s: %d rows", expected, rows)
            os.replace(tmp, target)
            report.append({"table": expected.lower(), "source_table": actual, "rows": rows,
                           "columns": {f.name: str(f.type) for f in schema_pa}, "file": str(target)})
    except Exception as exc:
        raise _scrub(exc, url) from None
    finally:
        engine.dispose()
    return report


def _scrub(exc: Exception, url: str) -> Exception:
    """Same exception class for our own LookupError / ValueError, RuntimeError otherwise, with secrets masked."""
    msg = str(exc)
    try:
        real = resolve_url(url)
        secrets = {real, getattr(_sa().engine.make_url(real), "password", None) or ""}
    except Exception:
        secrets = set()
    for secret in secrets:
        if secret:
            msg = msg.replace(secret, "***")
    if isinstance(exc, (LookupError, ValueError)):
        return type(exc)(msg)
    return RuntimeError(f"{type(exc).__name__}: {msg}")


def import_omop(
    url: str,
    out: str | os.PathLike,
    *,
    schema: str | None = None,
    tables: Sequence[str] | None = None,
    include_vocab: bool = False,
    chunksize: int = 500_000,
    table_map: Mapping[str, str] | str | os.PathLike | None = None,
) -> list[dict[str, Any]]:
    """Copies an OMOP CDM from a server database into a new local DuckDB file with the package's CDM v5.4 schema.

    Args:
        url: SQLAlchemy URL or ``env:VARNAME``.
        out: DuckDB file to create/extend.
        schema: Server schema holding the CDM tables (schema-qualified names handled).
        tables: Subset of CDM tables (default: :data:`CDM_STUDY_TABLES`; ``person`` is mandatory).
        include_vocab: Also copy :data:`CDM_VOCAB_TABLES` (large).
        table_map: Overrides for non-standard server table names.

    Tables absent on the server are skipped and reported with ``rows=None`` (``person`` absent raises).
    Columns are matched by name, case-insensitively; server columns unknown to the v5.4 DDL are dropped.
    """
    from omop_etl.build_omop_cdm import build_schema

    wanted = [t.lower() for t in (tables or CDM_STUDY_TABLES)]
    if include_vocab:
        wanted += [t for t in CDM_VOCAB_TABLES if t not in wanted]
    if "person" not in wanted:
        raise ValueError("'person' must be among the imported tables.")

    import tempfile

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="omop_import_") as tmp:
        present: list[str] = []
        sa = _sa()
        mapping = _load_mapping(table_map)
        engine = _make_engine(url)
        try:
            insp = sa.inspect(engine)
            have = {n.lower() for n in insp.get_table_names(schema=schema)}
            try:
                have |= {n.lower() for n in insp.get_view_names(schema=schema)}
            except Exception:
                pass
        except Exception as exc:
            raise _scrub(exc, url) from None
        finally:
            engine.dispose()
        for t in wanted:
            if mapping.get(t, t).lower() in have:
                present.append(t)
            else:
                report.append({"table": t, "rows": None, "skipped": "not found on server"})
        if "person" not in present:
            raise LookupError(f"No 'person' table found{' in schema ' + repr(schema) if schema else ''}; is this an OMOP CDM?")
        extracted = from_sql(url, present, tmp, chunksize=chunksize, schema=schema, table_map=table_map)
        con = duckdb.connect(str(out_path))
        try:
            build_schema(con)
            for item in extracted:
                t = item["table"]
                target_cols = {r[1].lower(): r[1] for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()} \
                    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [t]).fetchone()[0] else {}
                if not target_cols:
                    report.append({"table": t, "rows": None, "skipped": "no such table in CDM v5.4 DDL"})
                    continue
                cols = [c for c in item["columns"] if c in target_cols]
                col_sql = ", ".join(f'"{target_cols[c]}"' for c in cols)
                con.execute(f"DELETE FROM {t}")
                con.execute(
                    f'INSERT INTO {t} ({col_sql}) SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} '
                    f"FROM read_parquet('{Path(item['file']).as_posix()}')"
                )
                n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                dropped = [c for c in item["columns"] if c not in target_cols]
                report.append({"table": t, "rows": n, "source_rows": item["rows"], "dropped_columns": dropped})
        finally:
            con.close()
    return report
