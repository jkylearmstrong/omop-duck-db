# PostgreSQL inside Python: keep site databases there and check the DuckDB <-> PostgreSQL round trip

PostgreSQL normally needs an installer, a service and admin rights. With the optional extra, Python starts a private PostgreSQL server
from files in your environment, as an ordinary user, and stops it when you are done:

```bash
pip install "omop-duck-db[postgres-embedded]"          # add ,delta for the Delta Lake check below
```

The server is a child process. It listens on `127.0.0.1` only, on a free port, with a password generated on first use and stored in
`<folder>/credentials.json`. Its data stays in the folder you give, so a site database stored there is still there next time.
Run it as an ordinary user: PostgreSQL refuses to start as root.

**Docker images.** Both images include the extra. The GHCR image (`Dockerfile`, amd64 and arm64) already runs as the non-root user
`ruser`, so it works as is; `python scripts/container_smoke_test.py` includes a PostgreSQL round trip and CI runs it in the amd64 and
arm64 images. `Dockerfile.dgx` runs as root, so start the container with `--user omop` (or `runuser -u omop -- <command>` inside it)
for PostgreSQL work; `study-smoke` switches to `omop` by itself. Mounted folders must be writable by that user.

## Which package, and why

| | `pg0-embedded` (chosen) | `pgembed` (supported alternative) | `pgserver` |
|---|---|---|---|
| Licence | MIT | Apache-2.0 | Apache-2.0 |
| Last release (checked 2026-11-14) | 0.16.1, November 2026 | 0.2.0, March 2026 | 0.1.4, June 2024 (no release since) |
| Python | 3.8 and newer (one wheel for all versions) | **3.12 and newer only** | 3.9 and newer |
| Windows x64 | yes | yes | yes |
| Linux x64 | yes (glibc 2.35+, e.g. Ubuntu 22.04+, RHEL 10) | yes (glibc 2.28+, so also RHEL 8 / 9) | yes |
| Linux arm64 (DGX Spark, Grace) | yes (glibc 2.35+; DGX OS is Ubuntu 24.04) | yes | **no** |
| macOS arm64 | yes | yes | yes |
| PostgreSQL version | 18.1 | 17.9 | not checked |
| How clients connect | TCP on 127.0.0.1 | Unix socket on Linux / macOS, TCP on Windows | Unix socket |

`pgserver` is out: no release for more than two years and no Linux arm64 wheel, so it cannot run on a DGX Spark. `pgembed` started
as its maintained fork and works well, but it installs only on Python 3.12+, while this package supports 3.10+. `pg0-embedded` covers
every Python version and platform the package supports, is MIT like this package, and is the most active of the three, so it is the
default. On Python 3.12+ on an older Linux (glibc below 2.35, for example RHEL 8 or 9) use `pgembed` instead:

```bash
pip install "omop-duck-db[postgres]" pgembed
```

and pass `backend="pgembed"` (or `--backend pgembed` to the script). Both backends run the same tests in CI.

The first start of `pg0-embedded` unpacks its PostgreSQL build (about 75 MB) into `~/.pg0`; nothing is downloaded at that point.

## From Python

```python
from omop_etl.embedded_postgres import EmbeddedPostgres, compare_duckdb, load_site, load_text_files, site_schema, store_site

with EmbeddedPostgres("private/postgres") as pg:                   # start (or restart) the server kept in that folder
    schema = site_schema("Site A")                                   # 'site_a'
    store_site(pg, "derived/omop_duckdb/omop_site1.duckdb", schema)  # DuckDB -> PostgreSQL, checked value by value
    load_site(pg, schema, "private/from_postgres/omop_site1.duckdb") # PostgreSQL -> DuckDB
    load_text_files(pg, "raw/site1", "raw_" + schema)               # raw text files: PostgreSQL COPY vs DuckDB read_csv
    print(pg.query("SELECT count(*) FROM site_a.person"))
compare_duckdb("derived/omop_duckdb/omop_site1.duckdb", "private/from_postgres/omop_site1.duckdb")  # row for row
```

- `store_site` copies every table of the DuckDB file (`tables="cdm"` for the CDM tables only) into one schema with PostgreSQL `COPY`
  (about 700,000 rows a second on a laptop-class machine; `method="insert"` uses the same batched inserts as the Oracle path), then runs
  `verify_transfer(content=True)`: row counts, non-null counts, sum / min / max of numbers, and a value fingerprint of every text, date,
  timestamp and boolean column. Any difference raises `RoundTripError`.
- `load_site` reads a schema back into a new DuckDB file. CDM tables get the package's CDM v5.4 types, so `reproduce_study.py`, cohort
  and feature code run on it unchanged. It refuses to overwrite a file.
- `compare_duckdb` compares two DuckDB files table by table with `EXCEPT ALL` in both directions, so duplicates count too.
- `load_text_files` loads raw delimited files (PCORnet extracts: comma, pipe or tab; quoted fields; UTF-8) into text columns with
  PostgreSQL's own reader and compares every value with DuckDB reading the same files.
- `pg.url` is a SQLAlchemy URL for `pandas`, `to_sql`, `import_omop` or any other tool; `pg.dsn` is the libpq URI for `psql` and
  `psycopg2`; `pg.attach_to(duckdb_connection)` attaches it to DuckDB (DuckDB downloads its `postgres` extension the first time).
  Both contain the password; do not print or log them.

Booleans are stored as `smallint` (as on Oracle), and come back as `BOOLEAN` in the CDM tables; doubles keep NaN and ±Infinity; empty
strings stay empty strings (unlike Oracle, which stores them as NULL), and non-Latin text is kept (the database is UTF-8).

## On the site machine: all sites, raw files and Delta Lake in one run

```bash
python scripts/postgres_roundtrip_check.py --db-dir derived/omop_duckdb --sites site1 site2 site3 \
    --labels-file ../private/labels.json --pg-dir ../private/postgres --out-db-dir ../private/from_postgres \
    --raw-dirs ../raw/site1 ../raw/site2 ../raw/site3 --delta-dir ../private/delta --out pg_roundtrip_record.md
```

For each site this stores the database in schema `site_<letter>`, reads it back into `--out-db-dir` under the same file name and compares
it row for row, loads that site's raw text files into `raw_site_<letter>` and compares them with DuckDB, and writes every table as Delta
Lake version 0 (original) and version 1 (after the round trip) and checks that the two versions have no delta. `--delta-tables` limits the
Delta check (each table is read into memory). Any number of sites works; labels come from the private JSON written by
`reproduce_study.py --labels-out`, or Site A, B, ... in the order given.

The record names sites only by label and gives PASS / FAIL per check with no row counts, so it can be shared. `--pg-dir`, `--out-db-dir`
and `--delta-dir` hold patient-level data: keep them with the site's private files.

Then check the study results from the PostgreSQL copies against a baseline run with the existing tools:

```bash
python scripts/reproduce_study.py --db-dir ../private/from_postgres --out run_pg ...   # same arguments as the baseline run
python scripts/compare_study_deltas.py --new run_pg/<prefix>_Manuscript_Tables.xlsx --baseline run_base/<prefix>_Manuscript_Tables.xlsx --out-dir run_pg
python scripts/check_run_privacy.py --dir run_pg --names-file ../private/site_names.txt
```

A lossless round trip gives 0 differing cells.

## What has been tested, and what has not

- CI (`embedded-postgres` job): Linux x64, Linux arm64, Windows x64 and macOS arm64 with `pg0-embedded`, and Linux x64 with `pgembed`
  on Python 3.12, on synthetic data: the study-cohort fixture CDM plus an edge-case table (NULLs, empty strings, NaN / ±Infinity, DECIMAL,
  quoted text with commas and newlines, Japanese text, dates before 1970, fractional-second timestamps) goes to PostgreSQL and back
  identical; `define_study_cohort` gives the same cohort from both copies; a changed value is caught; data and password survive a restart;
  raw files (comma and pipe delimited, quotes, embedded newlines, a byte-order mark) agree between both readers; the script runs three
  sites with raw files and Delta Lake versions.
- Not yet run on real site data. Run the script above on the site machine and add the record to `testing/tested-environments.md`.
- Not tested: tables over a few million rows, a `--pg-dir` on a network drive, and running more than one server from the same folder.
