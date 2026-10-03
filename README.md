# omop-duck-db


<!-- badges: start -->

[![CI](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/ci.yml/badge.svg)](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/ci.yml)
[![R
Coverage](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/test-coverage.yaml/badge.svg)](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/test-coverage.yaml)
[![Python
Coverage](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/python-test-coverage.yaml/badge.svg)](https://github.com/jkylearmstrong/omop-duck-db/actions/workflows/python-test-coverage.yaml)
[![Codecov](https://codecov.io/gh/jkylearmstrong/omop-duck-db/graph/badge.svg)](https://app.codecov.io/gh/jkylearmstrong/omop-duck-db)
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
#> Schema built at C:\Users\jkyle\AppData\Local\Temp\RtmpSkaxb0\file86b0753d2af6.duckdb

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

## Custom mapping and retroactive remapping

When standard Athena vocabularies lack mappings for site-specific or
unmapped codes:

1.  **Audit unmapped codes**:

    ``` r
    export_unmapped_codes("omop_cdm.duckdb", "condition_occurrence", "unmapped_conditions.csv")
    ```

2.  **Import custom crosswalks or Usagi exports**:

    ``` r
    # Load custom source_to_concept_map CSV
    import_source_to_concept_map("mappings.csv", "omop_cdm.duckdb")

    # Or import OHDSI Usagi review export CSV
    import_usagi_mappings("usagi_reviewed.csv", "omop_cdm.duckdb")
    ```

3.  **Retroactively remap populated CDM tables**:

    ``` r
    # Preview changes without modifying data (dry run)
    remap_cdm_table("condition_occurrence", "omop_cdm.duckdb", dry_run = TRUE)

    # Apply remapping across all tables and rebuild condition/drug eras
    remap_all("omop_cdm.duckdb")
    ```

Equivalent functions are available in Python via
`from omop_etl import remap_all, import_usagi_mappings`.

## Readmission Cohorts & Table 1 Pipeline

The package provides an end-to-end epidemiological pipeline for acute
inpatient studies, featuring leak-free index stay sampling, temporal
feature extraction, Athena concept hierarchy rollups, LOINC laboratory
consolidation, stratified Table 1 generation, and data drift
reconciliation.

### 1. Build Inpatient Readmission Cohort

Constructs index inpatient stays (`visit_concept_id = 9201`) among adult
patients ($\ge 18$) with length of stay $\ge 1$ day, discharged alive,
and verified 30-day follow-up (requiring either a readmission or
confirmed clinical contact $\ge 30$ days post-discharge to prevent
right-censoring):

``` r
library(omopduckdb)

con <- DBI::dbConnect(duckdb::duckdb(), "omop_cdm.duckdb")

# Build cohort into OMOP cohort table (cohort_definition_id = 1)
cohort_df <- build_readmission_cohort(
  con,
  washin_days = 365,
  followup_days = 30,
  index_rule = "random",
  random_seed = 42,
  require_verified_followup = TRUE,
  target_table = "cohort"
)
```

In Python:

``` python
import duckdb
from omop_etl import build_readmission_cohort

con = duckdb.connect("omop_cdm.duckdb")
cohort_df = build_readmission_cohort(
    con,
    washin_days=365,
    followup_days=30,
    index_rule="random",
    random_seed=42,
    require_verified_followup=True,
    target_table="cohort"
)
```

### 2. Extract Temporal Utilization & Demographic Features

Aggregates windowed baseline healthcare utilization (IP, ED, OP
encounter counts, days since prior visit, prior 30-day readmissions in
lookback), index length of stay, discharge disposition, and
demographics:

``` r
temporal_features <- extract_temporal_features(
  con,
  cohort_table = "cohort",
  cohort_definition_id = 1,
  lookback_days = 365
)
```

In Python:

``` python
from omop_etl import extract_temporal_features

temporal_features = extract_temporal_features(
    con,
    cohort_table="cohort",
    cohort_definition_id=1,
    lookback_days=365
)
```

### 3. Athena Concept Hierarchy Rollup & Consolidated Measurements

Traverse Athena’s `concept_ancestor` table to aggregate medication or
condition classes, and extract consolidated lab panels and physical
vitals using acute pre-discharge aggregation strategies:

``` r
# Aggregate medication exposure using Athena ancestor traversal
med_features <- aggregate_concept_sets(
  con,
  cohort_table = "cohort",
  cohort_definition_id = 1,
  domain = "drug",
  concept_sets = DEFAULT_MEDICATION_CONCEPTS,
  lookback_days = 365
)

# Extract acute pre-discharge laboratory and vital measurements
lab_features <- extract_measurements(
  con,
  cohort_table = "cohort",
  cohort_definition_id = 1,
  loinc_map = DEFAULT_LAB_LOINCS,
  strategy = "last_before_discharge"
)
```

In Python:

``` python
from omop_etl import (
    aggregate_concept_sets,
    extract_measurements,
    DEFAULT_MEDICATION_CONCEPTS,
    DEFAULT_LAB_LOINCS
)

med_features = aggregate_concept_sets(
    con,
    cohort_table="cohort",
    cohort_definition_id=1,
    domain="drug",
    concept_sets=DEFAULT_MEDICATION_CONCEPTS,
    lookback_days=365
)

lab_features = extract_measurements(
    con,
    cohort_table="cohort",
    cohort_definition_id=1,
    loinc_map=DEFAULT_LAB_LOINCS,
    strategy="last_before_discharge"
)
```

### 4. Stratified Table 1 Generation & Completeness Matrix

Generates baseline descriptive tables (continuous: Mean +/- SD, Median
\[IQR\]; categorical: N \[%\]) stratified by outcome (e.g.,
`readmitted_30d`), calculating Standardized Mean Differences (SMD) and
statistical tests, along with a Table 1b completeness audit:

``` r
table1_out <- generate_table1(
  patient_df,
  strata = "readmitted_30d",
  continuous_vars = c("age_at_admission", "los_days", "prior_ed_visits"),
  cat_vars = c("gender", "race", "tobacco_status")
)

# Baseline summary with SMD and p-values
print(table1_out$table1)

# Missingness and completeness audit
print(table1_out$table1b_missingness)
```

In Python:

``` python
from omop_etl import generate_table1

t1_df, t1b_df = generate_table1(
    patient_df,
    strata="readmitted_30d",
    continuous_vars=["age_at_admission", "los_days", "prior_ed_visits"],
    cat_vars=["gender", "race", "tobacco_status"]
)
```

### 5. Benchmark Reconciliation & Validation Harness

Validates prospective OMOP Table 1 statistics against benchmark datasets
with configurable absolute tolerance thresholds, flagging distribution
drift and missing features:

``` r
reconciliation <- validate_table1_reconciliation(
  omop_table1 = table1_out$table1,
  source_table1 = benchmark_table1,
  tolerance = 0.05
)

if (!reconciliation$passed) {
  warning("Table 1 discrepancy detected!")
  print(reconciliation$discrepancies)
}
```

In Python:

``` python
from omop_etl import validate_table1_reconciliation

reconciliation = validate_table1_reconciliation(
    omop_table1=t1_df,
    source_table1=benchmark_table1,
    tolerance=0.05
)
print("Reconciliation Passed:", reconciliation["passed"])
```

## ML bridge: omop-learn, scikit-learn, SARD & OHDSI PLP / DeepPLP

omop-duck-db connects an OMOP-on-DuckDB database to three modelling
ecosystems without ad-hoc SQL or per-patient query loops. Every extractor
runs as one set-based DuckDB query over the whole cohort, and every window
is **strictly before the index date** by default (`[index - lookback_days,
index)`), so nothing at or after the index leaks into the features.
Concept id `0` ("No matching concept") is always dropped.

| You use | Entry point |
|---|---|
| scikit-learn / glmnet / LASSO | `extract_sparse_concept_matrix()` (Python: `scipy.sparse.csr_matrix`; R: `Matrix::dgCMatrix`) |
| SARD / sequence models | `extract_sard_visit_tensors()` (padded `(N, max_nvisits, max_visit_len)`) |
| omop-learn (`OMOPDataset`, its torch / sparse / windowed datasets and models) | `DuckDBBackend`, a DuckDB implementation of omop-learn's backend interface |
| OHDSI PatientLevelPrediction / DeepPatientLevelPrediction | `plp_database_details()`, `hades_preflight()`, `as_plp_data()` |
| tidymodels / in-database scoring | `as_tidymodels_data()`, `model_concepts()`, `materialize_concept_features()` |

### 1. Sparse concept matrix and SARD visit tensors

Both take a cohort parquet (one row per index event; person id and index
date columns are auto-detected, an outcome column is carried through) and
return arrays whose row `i` is parquet row `i`.

``` python
import duckdb
from omop_etl import extract_sparse_concept_matrix, extract_sard_visit_tensors

con = duckdb.connect("derived/omop_duckdb/omop_Temple.duckdb", read_only=True)
con.execute("ATTACH 'derived/omop_duckdb/central_vocabulary.duckdb' AS central_vocab (READ_ONLY)")
cohort = "derived/ederri_features/ederri_features_Temple.parquet"

res = extract_sparse_concept_matrix(con, cohort, min_patient_freq=50)
res["X"].shape            # (N, V) csr_matrix; res["concepts"] has names, domains, patient counts

sard = extract_sard_visit_tensors(con, cohort, min_patient_freq=50)
sard["concept_tensor"].shape   # (N, max_nvisits, max_visit_len); also visit_lengths, n_visits, times
```

``` r
library(DBI); library(duckdb)
con <- dbConnect(duckdb::duckdb(), "derived/omop_duckdb/omop_Temple.duckdb", read_only = TRUE)
dbExecute(con, "ATTACH 'derived/omop_duckdb/central_vocabulary.duckdb' AS central_vocab (READ_ONLY);")
res <- extract_sparse_concept_matrix(con, "derived/ederri_features/ederri_features_Temple.parquet",
                                     min_patient_freq = 50)
dim(res$X)
```

Conventions follow omop-learn so outputs drop into its models: special
tokens `[BOS]=0 [EOS]=1 [SEP]=2 [PAD]=3 [UNK]=4`, concept tokens from 5
(`"<concept_id> - <domain> - <concept_name>"`, sorted like omop-learn's
`ConceptTokenizer`), visit times as days since 1900-01-01 with `-1` padding.
Pass a train result's `tokenizer` (R: `tokens`) to a test cohort to get
identical columns; pass explicit `max_nvisits` / `max_visit_len` for a fixed
tensor shape. `pip install "omop-duck-db[ml]"` adds scipy.

### 2. omop-learn backend

omop-learn ships Postgres, BigQuery and Spark backends behind one
interface; `DuckDBBackend` adds DuckDB. omop-learn's per-patient feature SQL
runs as a single `LATERAL` join instead of one query per patient (about 70x
faster in our benchmark), with DuckDB-dialect feature SQL bundled in
`inst/sql/omop_learn`.

``` python
from pathlib import Path
from omop_learn.omop import OMOPDataset
from omop_learn.utils.config import Config
from omop_etl import DuckDBBackend, default_features, cohort_from_parquet

backend = DuckDBBackend(con=con)
dataset = OMOPDataset(name="temple", config=Config({"cdm_schema": "main"}),
                      cohort=cohort_from_parquet(con, cohort), features=default_features(),
                      backend=backend, data_dir=Path("datasets"))
torch_ds = dataset.to_torch()      # omop-learn's own datasets and models from here on
```

omop-learn is not on PyPI:
`pip install git+https://github.com/clinicalml/omop-learn`.

### 3. OHDSI PatientLevelPrediction and DeepPatientLevelPrediction

PLP, DeepPLP and FeatureExtraction read the CDM through DatabaseConnector,
which opens the DuckDB file itself and **cannot `ATTACH` a central
vocabulary**. The vocabulary tables must therefore be inside the file. It
also opens the file read-write, so no *other process* (for example a Python
session that still has the database open, even read-only) may hold it.
`hades_preflight()` checks both, from a fresh connection:

``` r
hades_preflight("omop_Temple.duckdb")
db <- plp_database_details("omop_Temple.duckdb", target_id = 1, outcome_ids = 2)

# Native OHDSI features (including DeepPLP's temporal transformer input):
plp <- PatientLevelPrediction::getPlpData(db, FeatureExtraction::createCovariateSettings(
         useConditionOccurrenceLongTerm = TRUE, longTermStartDays = -365, endDays = -1),
       PatientLevelPrediction::createRestrictPlpDataSettings())

# ...or hand PLP / DeepPLP the very same matrix you used in Python or glmnet:
res <- extract_sparse_concept_matrix(con, cohort_parquet, value = "binary")
plp <- as_plp_data(res, con, target_id = 1, outcome_id = 2)
```

`as_plp_data()` uses covariate ids `concept_id * 1000 + analysis_id` with
FeatureExtraction's long-term analysis ids (102 / 302 / 502), so they match natively extracted
covariates for the 365-day window (verified against FeatureExtraction in the
test suite).

### 4. tidymodels and in-database scoring (orbital, tidypredict)

`as_tidymodels_data()` bridges sparse concept matrices into a tibble ready for
tidymodels (`rsample`, `recipes`, `parsnip`, `workflows`, `yardstick`), with outcome `y`
formatted with the event as the first factor level (`event_level = "first"`), and concept
predictors named `<domain>_<concept_id>`.

Once a model or workflow is fitted, `model_concepts()` identifies which concepts
have non-zero coefficients. `materialize_concept_features()` exports matching wide
feature tables inside DuckDB using identical windowing logic. Scoring can then run
directly in-database with `orbital::orbital()` or `tidypredict` without feature drift:

``` r
library(tidymodels)

res <- extract_sparse_concept_matrix(con, "cohort.parquet", min_patient_freq = 50, value = "binary")
dat <- as_tidymodels_data(res)

split <- rsample::group_initial_split(dat, group = person_id)
rec <- recipes::recipe(y ~ ., data = rsample::training(split)) |>
  recipes::update_role(person_id, index_date, new_role = "id")
spec <- parsnip::logistic_reg(penalty = 0.01, mixture = 1) |> parsnip::set_engine("glmnet")
fit <- workflows::workflow(rec, spec) |> parsnip::fit(rsample::training(split))

# In-database scoring on new patients without feature drift:
used <- model_concepts(fit, res$concepts)
materialize_concept_features(con, "new_cohort.parquet", used, table = "to_score", value = "binary")
orb <- orbital::orbital(fit)
scores <- orbital::augment(orb, dplyr::tbl(con, "to_score")) |> dplyr::collect()
```

## Testing

`tests/fixtures/pcornet_sample/` is a small set of fabricated (not real)
sample rows used to smoke-test both ETLs and check they produce
identical output.

R: `devtools::test()` (or `R CMD check`) runs the `testthat` suite.

Python: `pytest` runs the Python-side tests, including a cross-language
parity check that runs both ETLs against the fixture and diffs every row
(skipped automatically if R isn’t available on the machine running the
tests).

The ML bridge tests skip what they cannot import: omop-learn (point
`OMOP_LEARN_SRC` at a source checkout's `src/` directory, or install it) and
the HADES packages (FeatureExtraction, PatientLevelPrediction, Andromeda,
DatabaseConnector).

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
