# omop-duck-db


<!-- badges: start -->

[![CI](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/ci.yml/badge.svg)](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/ci.yml)
[![Test
Coverage](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/test-coverage.yml/badge.svg)](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/test-coverage.yml)
[![codecov](https://img.shields.io/codecov/c/github/jkylearmstrong/omop-duck-db/main?logo=codecov.png)](https://codecov.io/gh/jkylearmstrong/omop-duck-db)
<!-- badges: end -->

Tools for building an [OMOP Common Data
Model](https://ohdsi.github.io/CommonDataModel/) (CDM) v5.4 database in
[DuckDB](https://duckdb.org/), loading the OHDSI Athena vocabulary, and
mapping PCORnet-format source data into the CDM. Provided as both an R
package and a Python package, with parallel ETL implementations that
share the same schema and concept-mapping SQL, so a human (or an AI
agent) picking either language gets the same result.

## Scope

This repo provides general-purpose mapping/ETL tooling and a small
fabricated sample fixture for testing – it does not itself ingest,
store, or process any protected or identifiable data. Real-world use
against production source data happens outside this repo, against your
own data infrastructure.

## Layout

- `inst/extdata/5.4/duckdb/*.sql` — OMOP CDM v5.4 DDL, generated once
  via the `CommonDataModel` R package. This is the schema source of
  truth for **both** the R and Python ETL paths — neither re-declares
  `CREATE TABLE` by hand.
- `inst/sql/mapping_macros.sql` — DuckDB SQL macros for source-code →
  standard-concept resolution, shared by both ETLs.
- `R/` — the R package: schema build, vocabulary load/refresh, PCORnet
  ETL, Google Drive snapshot sync, BigQuery live connector.
- `inst/scripts/` — thin command-line wrappers around the R package’s
  functions, for scripting/automation use without writing R code.
- `python/omop_etl/` — the Python package (`build_omop_cdm.py`), mirrors
  the R package table-for-table.
- `tests/fixtures/pcornet_sample/` — small fabricated sample rows used
  to smoke-test both ETLs against each other.
- `config/` — non-secret config templates (`*.yml.example`). Copy to
  `*.yml` and fill in locally; the real files are gitignored.

The generated database file and any vocabulary download archive are
**not** committed to git — they’re large binaries. See “Large files”
below.

## Installation

R:

``` r
# install.packages("remotes")
remotes::install_github("jkylearmstrong/omop-duck-db")
```

Python:

``` bash
pip install "omop-duck-db @ git+https://github.com/jkylearmstrong/omop-duck-db"
```

## Example

A schema builds into a fresh, empty DuckDB file with a single call:

``` r
library(omopduckdb)

db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
#> Schema built at C:\Users\jkyle\AppData\Local\Temp\Rtmpmo9cOr\filea8b4539272c.duckdb

con <- DBI::dbConnect(duckdb::duckdb(), db_path, read_only = TRUE)
DBI::dbListTables(con)[1:10]
#>  [1] "care_site"            "cdm_source"           "cohort"              
#>  [4] "cohort_definition"    "concept"              "concept_ancestor"    
#>  [7] "concept_class"        "concept_relationship" "concept_synonym"     
#> [10] "condition_era"
DBI::dbDisconnect(con, shutdown = TRUE)
```

From there, `load_vocabulary()` loads a vocabulary download, and
`etl_pcornet()` maps a PCORnet-format source directory into the CDM
tables just created — see “Building the database” below for the full
workflow, or `inst/scripts/` for command-line equivalents of each step.

## Building the database

1.  Generate the CDM v5.4 schema and load the vocabulary:

    ``` r
    library(omopduckdb)
    build_schema("omop_cdm.duckdb")
    load_vocabulary("path/to/vocabulary_download", "omop_cdm.duckdb")
    ```

2.  Map a source directory into the CDM, with either implementation
    (they read the same source layout and produce equivalent output):

    ``` r
    etl_pcornet("path/to/source_extract", "omop_cdm.duckdb")
    ```

    ``` bash
    python -m omop_etl.build_omop_cdm --source-dir path/to/source_extract
    ```

Command-line equivalents of the R functions live in `inst/scripts/`,
e.g.
`Rscript inst/scripts/etl_pcornet.R --source-dir path/to/source_extract`.

## Refreshing the vocabulary

- From a new download:
  `load_vocabulary("path/to/new_download", "omop_cdm.duckdb")`. Each
  table is truncated and reloaded inside its own transaction with a
  row-count sanity check, so a bad/partial file can only fail that one
  table (rolled back, left untouched), not half-clobber the rest of the
  vocabulary.

- Live, from BigQuery (e.g. a public or your own OMOP-shaped dataset):
  `load_from_bigquery(billing_project = "...", dataset = "...", tables = c("concept", "concept_ancestor", "concept_relationship"))`.
  A GCP project ID is required for query billing/quota even against
  public datasets (set one in `config/bigquery.yml`, copied from
  `config/bigquery.yml.example`).

  **First-time auth**: the BigQuery OAuth flow needs an interactive R
  session to open a browser — it fails silently if run non-interactively
  before a token exists. Run this once, in an interactive session (not
  `Rscript`), and approve the browser prompt:

  ``` r
  bigrquery::bq_auth()
  ```

  The resulting token is cached and reused by later non-interactive
  runs.

## Testing

`tests/fixtures/pcornet_sample/` is a small set of fabricated (not real)
sample rows used to smoke-test both ETLs and check they produce
identical output.

R: `devtools::test()` (or `R CMD check`) runs the `testthat` suite.

Python: `pytest` runs the Python-side tests, including a cross-language
parity check that runs both ETLs against the fixture and diffs every row
(skipped automatically if R isn’t available on the machine running the
tests).

## Large files (Google Drive)

Instead of git/git-LFS, the generated database and vocabulary archive
can be synced through Google Drive:

``` r
gdrive_upload_snapshot()    # push the current database + vocabulary archive
gdrive_download_latest()    # pull the latest snapshot instead of rebuilding
```

Configure the target Drive folder in `config/gdrive.yml` (copy from
`config/gdrive.yml.example`).

## Known limitations

- Athena vocabulary downloads do not include CPT4 concept names (AMA
  copyright) — a separate licensed tool (bundled with the download)
  populates them if you have a UMLS license. Until that’s run,
  CPT4-coded procedures map to concept_id 0.
- A handful of source fields (e.g. admission source) aren’t mapped yet;
  see inline comments in the ETL code for specifics.
- The Python side doesn’t yet have its own BigQuery connector — use the
  R connector for that, either way, into the same database file.
