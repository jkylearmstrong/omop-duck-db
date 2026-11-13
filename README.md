omop-duck-db
================

# omop-duck-db <a href="https://jkylearmstrong.github.io/omop-duck-db/"><img src="man/figures/logo.png" align="right" height="139" alt="omop-duck-db website" /></a>

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
pip install omop-duck-db
# or for ML dependencies: pip install 'omop-duck-db[ml]'
```

### Developer Note: `renv` Workflow

This repository includes an `.Rprofile` that activates `renv`
automatically (`renv/activate.R`). - **If working within the project’s
isolated environment**: Run `renv::restore()` to populate local package
libraries. - **If using pre-installed system R packages**: Set
`RENV_CONFIG_AUTOLOADER = FALSE` in `.Renviron` or invoke
`Rscript --no-init-file` so standalone scripts do not fail looking for
unpopulated local `renv` libraries.

## Example

A schema builds into a fresh, empty DuckDB file with a single call:

``` r
library(omopduckdb)

db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
#> Schema built at C:omop_cdm.duckdb

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
  table is deleted and reloaded inside its own transaction with a
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

## Transparent Central Vocabulary & Single-Store Architecture (`omop_connect`)

Instead of duplicating 10–15 GB of Athena vocabulary tables inside every
site-specific CDM database, `omop_connect()` attaches a shared central
vocabulary database read-only (`central_vocab`) and automatically
injects DuckDB’s `search_path = 'main,central_vocab'`. All queries
seamlessly resolve `concept`, `concept_ancestor`, and vocabulary macros
without schema qualification:

``` r
library(omopduckdb)

# Auto-discovers sibling central_vocabulary.duckdb or connects with explicit path
con <- omop_connect("omop_cdm.duckdb")
DBI::dbGetQuery(con, "SELECT COUNT(*) FROM concept;")
```

In Python:

``` python
from omop_etl import omop_connect

con = omop_connect("omop_cdm.duckdb")
con.execute("SELECT COUNT(*) FROM concept").fetchone()
```

## Custom mapping and retroactive remapping

When standard Athena vocabularies lack mappings for site-specific or
unmapped codes:

1.  **Audit unmapped codes**:

    ``` r
    con <- omop_connect("omop_cdm.duckdb")
    export_unmapped_codes(con, "condition_occurrence", "unmapped_conditions.csv")
    ```

2.  **Import custom crosswalks or Usagi exports**:

    ``` r
    # Load custom source_to_concept_map CSV
    import_source_to_concept_map(con, "mappings.csv")

    # Or import OHDSI Usagi review export CSV
    import_usagi_mappings(con, "usagi_reviewed.csv")
    ```

3.  **Retroactively remap populated CDM tables**:

    ``` r
    # Preview changes without modifying data (dry run)
    remap_cdm_table(con, "condition_occurrence", dry_run = TRUE)

    # Apply remapping across all tables and rebuild condition/drug eras
    remap_all(con)
    ```

Equivalent functions are available in Python via
`from omop_etl import remap_all, import_usagi_mappings`.

## Study Cohorts, Phenotyping & Clinical Pipeline

The package provides an end-to-end epidemiological pipeline for
observational studies, featuring flexible index stay definition,
validated comorbidity profiling, physiologic range sanitization, Athena
concept hierarchy rollups, LOINC laboratory consolidation, and
publication Table 1 generation.

### 1. Unified Study Cohort Engine (`define_study_cohort`)

Generalizes acute inpatient, emergency department, and outpatient index
cohorts with customizable wash-in windows, right-censoring verification,
and outcome definitions:

``` r
library(omopduckdb)

con <- DBI::dbConnect(duckdb::duckdb(), "omop_cdm.duckdb")

# Build general study cohort (or specialized build_readmission_cohort / build_end_of_life_cohort)
cohort_df <- define_study_cohort(
  con,
  visit_type = "inpatient",
  washin_days = 365,
  followup_days = 30,
  min_age = 18,
  min_los_days = 1,
  sampling_rule = "first",
  require_verified_followup = TRUE,
  table_name = "cohort"
)
```

In Python:

``` python
from omop_etl import define_study_cohort

cohort_df = define_study_cohort(
    con,
    visit_type="inpatient",
    washin_days=365,
    followup_days=30,
    min_age=18,
    min_los_days=1,
    sampling_rule="first",
    require_verified_followup=True,
    table_name="cohort"
)
```

### 2. Comorbidity Profiling: 31 Elixhauser Domains & Charlson Index

Computes validated comorbidity profiles in one set-based DuckDB query
over the cohort with zero object leakage on read-only connections.
Automatically evaluates both ICD-9/10 prefixes
(`condition_source_value`) and SNOMED descendants
(`condition_concept_id`):

``` r
# Extract 31 Elixhauser domains + composite van Walraven score
elix_df <- extract_elixhauser_comorbidities(con, cohort_table = "cohort", lookback_days = 365)

# Extract 17 Quan-Charlson categories + Charlson Comorbidity Index
cci_df <- extract_charlson_index(con, cohort_table = "cohort", lookback_days = 365)
```

In Python:

``` python
from omop_etl import extract_elixhauser_comorbidities, extract_charlson_index

elix_df = extract_elixhauser_comorbidities(con, cohort_table="cohort", lookback_days=365)
cci_df = extract_charlson_index(con, cohort_table="cohort", lookback_days=365)
```

### 3. Physiologic Measurement Sanitization & Outlier Winsorization (`sanitize_measurements`)

Cleans laboratory and vital measurements against 136 standard OHDSI
DataQualityDashboard biological plausibility limits or statistical
IQR/z-score winsorization:

``` r
# Clamps out-of-bounds measurements to DQD biological plausibility limits
clean_labs <- sanitize_measurements(
  con,
  method = "dqd_biologic_limits",
  action = "clamp"
)
```

In Python:

``` python
from omop_etl import sanitize_measurements

clean_labs = sanitize_measurements(
    con,
    method="dqd_biologic_limits",
    action="clamp"
)
```

### 4. Build Inpatient Readmission Cohort (`build_readmission_cohort`)

Constructs index inpatient stays (`visit_concept_id = 9201`) among adult
patients ($\ge 18$) with length of stay $\ge 1$ day, discharged alive,
and verified 30-day follow-up (requiring either a readmission or
confirmed clinical contact $\ge 30$ days post-discharge to prevent
right-censoring):

``` r
library(omopduckdb)

con <- DBI::dbConnect(duckdb::duckdb(), "omop_cdm.duckdb")

# Build cohort into OMOP cohort table (cohort_id = 1)
cohort_df <- build_readmission_cohort(
  con,
  washin_days = 365,
  followup_window_days = 30,
  index_selection_rule = "random",
  random_state = 42,
  require_verified_followup = TRUE,
  table_name = "cohort"
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
    followup_window_days=30,
    index_selection_rule="random",
    random_state=42,
    require_verified_followup=True,
    table_name="cohort"
)
```

### 5. Extract Temporal Utilization & Demographic Features

Aggregates windowed baseline healthcare utilization (IP, ED, OP
encounter counts, days since prior visit, prior 30-day readmissions in
lookback), index length of stay, discharge disposition, and
demographics:

``` r
temporal_features <- extract_temporal_features(
  con,
  cohort_table = "cohort",
  cohort_id = 1,
  lookback_days = 365
)
```

In Python:

``` python
from omop_etl import extract_temporal_features

temporal_features = extract_temporal_features(
    con,
    cohort_table="cohort",
    cohort_id=1,
    lookback_days=365
)
```

### 6. Athena Concept Hierarchy Rollup & Consolidated Measurements

Traverse Athena’s `concept_ancestor` table to aggregate medication or
condition classes, and extract consolidated lab panels and physical
vitals using acute pre-discharge aggregation strategies:

``` r
# Aggregate medication exposure using Athena ancestor traversal
med_features <- aggregate_concept_sets(
  con,
  cohort_table = "cohort",
  cohort_id = 1,
  domain = "drug",
  concept_sets = DEFAULT_MEDICATION_CONCEPTS,
  lookback_days = 365
)

# Extract acute pre-discharge laboratory and vital measurements
lab_features <- extract_measurements(
  con,
  cohort_table = "cohort",
  cohort_id = 1,
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
    cohort_id=1,
    domain="drug",
    concept_sets=DEFAULT_MEDICATION_CONCEPTS,
    lookback_days=365
)

lab_features = extract_measurements(
    con,
    cohort_table="cohort",
    cohort_id=1,
    loinc_map=DEFAULT_LAB_LOINCS,
    strategy="last_before_discharge"
)
```

### 7. Stratified Table 1 Generation & Completeness Matrix

Generates baseline descriptive tables (continuous: Mean +/- SD, Median
\[IQR\]; categorical: N \[%\]) stratified by outcome (e.g.,
`readmitted_30d`), calculating Standardized Mean Differences (SMD) and
statistical tests, along with a Table 1b completeness audit:

``` r
table1_out <- generate_table1(
  patient_df,
  strata_col = "readmitted_30d",
  continuous_vars = c("age_at_admission", "los_days", "prior_ed_visits"),
  categorical_vars = c("gender", "race", "tobacco_status")
)

# Baseline summary with SMD and p-values
print(table1_out$table1)

# Missingness and completeness audit
print(table1_out$table1b)
```

In Python:

``` python
from omop_etl import generate_table1

t1 = generate_table1(
    patient_df,
    strata_col="readmitted_30d",
    continuous_vars=["age_at_admission", "los_days", "prior_ed_visits"],
    categorical_vars=["gender", "race", "tobacco_status"]
)
```

### 8. Benchmark Reconciliation & Validation Harness

Validates prospective OMOP Table 1 statistics against benchmark datasets
with configurable absolute tolerance thresholds, flagging distribution
drift and missing features:

``` r
reconciliation <- validate_table1_reconciliation(
  omop_table1 = table1_out$table1,
  source_table1 = benchmark_table1,
  tolerance = 0.05
)

if (!reconciliation$is_concordant) {
  warning("Table 1 discrepancy detected!")
  print(reconciliation$feature_comparisons)
}
```

In Python:

``` python
from omop_etl import validate_table1_reconciliation

reconciliation = validate_table1_reconciliation(
    omop_table1=t1["table1"],
    source_table1=benchmark_table1,
    tolerance=0.05
)
print("Reconciliation concordant:", reconciliation["is_concordant"])
```

## Running on the NVIDIA DGX Spark (deep models) and Oracle transfers

**Status (0.20.0): written and tested on CPU only. The GPU path and the arm64 / GB10 base-image tag have not been run on a Spark yet.**

On the Spark, from the parent `eDerri_data` directory (so omop-learn and `pipeline/` are in the build context):

```bash
docker build -f omop-duck-db/Dockerfile.dgx -t omop-duck-db:dgx .
docker run --rm --gpus all --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 \
    -v /workspace/ederri:/workspace/ederri omop-duck-db:dgx dgx-smoke
```

`dgx-smoke` (`scripts/dgx_smoke_test.py --require-gpu`) prints the torch / CUDA report, trains and scores a small MLP, and fails if no GPU is visible. If the build cannot pull the base image, set `--build-arg NGC_TAG=<tag you have verified>`; do not pip-install torch over the NGC build. Then:

```python
from omop_etl import make_mlp_fit_predict, deep_study_tables, device_report
res = deep_study_tables(make_mlp_fit_predict(), X, y, sites=site_labels, model_name="MLP")   # Model_Performance row + leave-one-site-out
```

Any trainer (SARD, DeepPLP, `pipeline/*_dgx.py`) can be scored the same way by wrapping it as `fit_predict(x_train, y_train, x_test) -> probabilities`; such wrappers are not written yet.

Oracle / server transfers (`to_sql`, `verify_transfer`, and `import_omop` / `from_sql` back) were run against a live Oracle 19c with `oracledb` in thick mode; credentials come from a SQLAlchemy URL or `env:NAME`, never from code.

## ML bridge: omop-learn, scikit-learn, SARD & OHDSI PLP / DeepPLP

omop-duck-db connects an OMOP-on-DuckDB database to three modelling
ecosystems without ad-hoc SQL or per-patient query loops. Every
extractor runs as one set-based DuckDB query over the whole cohort, and
every window is **strictly before the index date** by default
(`[index - lookback_days, index)`), so nothing at or after the index
leaks into the features. Concept id `0` (“No matching concept”) is
always dropped.

| You use | Entry point |
|----|----|
| scikit-learn / glmnet / LASSO | `extract_sparse_concept_matrix()` (Python: `scipy.sparse.csr_matrix`; R: `Matrix::dgCMatrix`) |
| SARD / sequence models | `extract_sard_visit_tensors()` (padded `(N, max_nvisits, max_visit_len)`) |
| omop-learn (`OMOPDataset`, its torch / sparse / windowed datasets and models) | `DuckDBBackend`, a DuckDB implementation of omop-learn’s backend interface |
| OHDSI PatientLevelPrediction / DeepPatientLevelPrediction | `plp_database_details()`, `hades_preflight()`, `as_plp_data()` |
| tidymodels / in-database scoring | `as_tidymodels_data()`, `model_concepts()`, `materialize_concept_features()` |

### 1. Sparse concept matrix and SARD visit tensors

Both take a cohort parquet (one row per index event; person id and index
date columns are auto-detected, an outcome column is carried through)
and return arrays whose row `i` is parquet row `i`.

``` python
import duckdb
from omop_etl import extract_sparse_concept_matrix, extract_sard_visit_tensors

con = duckdb.connect("derived/omop_duckdb/omop_site1.duckdb", read_only=True)
con.execute("ATTACH 'derived/omop_duckdb/central_vocabulary.duckdb' AS central_vocab (READ_ONLY)")
cohort = "derived/ederri_features/ederri_features_site1.parquet"

res = extract_sparse_concept_matrix(con, cohort, min_patient_freq=50)
res["X"].shape            # (N, V) csr_matrix; res["concepts"] has names, domains, patient counts

sard = extract_sard_visit_tensors(con, cohort, min_patient_freq=50)
sard["concept_tensor"].shape   # (N, max_nvisits, max_visit_len); also visit_lengths, n_visits, times
```

``` r
library(DBI); library(duckdb)
con <- dbConnect(duckdb::duckdb(), "derived/omop_duckdb/omop_site1.duckdb", read_only = TRUE)
dbExecute(con, "ATTACH 'derived/omop_duckdb/central_vocabulary.duckdb' AS central_vocab (READ_ONLY);")
res <- extract_sparse_concept_matrix(con, "derived/ederri_features/ederri_features_site1.parquet",
                                     min_patient_freq = 50)
dim(res$X)
```

Conventions follow omop-learn so outputs drop into its models: special
tokens `[BOS]=0 [EOS]=1 [SEP]=2 [PAD]=3 [UNK]=4`, concept tokens from 5
(`"<concept_id> - <domain> - <concept_name>"`, sorted like omop-learn’s
`ConceptTokenizer`), visit times as days since 1900-01-01 with `-1`
padding. Pass a train result’s `tokenizer` (R: `tokens`) to a test
cohort to get identical columns; pass explicit `max_nvisits` /
`max_visit_len` for a fixed tensor shape.
`pip install "omop-duck-db[ml]"` adds scipy.

### 2. omop-learn backend

omop-learn ships Postgres, BigQuery and Spark backends behind one
interface; `DuckDBBackend` adds DuckDB. omop-learn’s per-patient feature
SQL runs as a single `LATERAL` join instead of one query per patient
(about 70x faster in our benchmark), with DuckDB-dialect feature SQL
bundled in `inst/sql/omop_learn`.

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

PLP, DeepPLP and FeatureExtraction read the CDM through
DatabaseConnector, which opens the DuckDB file itself and **cannot
`ATTACH` a central vocabulary**. The vocabulary tables must therefore be
inside the file. It also opens the file read-write, so no *other
process* (for example a Python session that still has the database open,
even read-only) may hold it. `hades_preflight()` checks both, from a
fresh connection. Files distributed for read-only use must be cleanly closed
and checkpointed (`omop-duckdb checkpoint site.duckdb`, `omop_etl.checkpoint_database()` or
`omopduckdb::checkpoint_database()`): if a `.duckdb.wal` sits beside the file,
`ATTACH ... (READ_ONLY)` fails with "WAL replay is required". Hand-written
cohort SQL does not create inclusion-rule statistics tables, so call
`CohortGenerator::generateCohortSet(..., generateStats = FALSE)`:

``` r
hades_preflight("omop_site1.duckdb")
db <- plp_database_details("omop_site1.duckdb", target_id = 1, outcome_ids = 2)

# Native OHDSI features (including DeepPLP's temporal transformer input):
plp <- PatientLevelPrediction::getPlpData(db, FeatureExtraction::createCovariateSettings(
         useConditionOccurrenceLongTerm = TRUE, longTermStartDays = -365, endDays = -1),
       PatientLevelPrediction::createRestrictPlpDataSettings())

# ...or hand PLP / DeepPLP the very same matrix you used in Python or glmnet:
res <- extract_sparse_concept_matrix(con, cohort_parquet, value = "binary")
plp <- as_plp_data(res, con, target_id = 1, outcome_id = 2)
```

`as_plp_data()` uses covariate ids `concept_id * 1000 + analysis_id`
with FeatureExtraction’s long-term analysis ids (102 / 302 / 502), so
they match natively extracted covariates for the 365-day window
(verified against FeatureExtraction in the test suite).

### 4. tidymodels and in-database scoring (orbital, tidypredict)

`as_tidymodels_data()` bridges sparse concept matrices into a tibble
ready for tidymodels (`rsample`, `recipes`, `parsnip`, `workflows`,
`yardstick`), with outcome `y` formatted with the event as the first
factor level (`event_level = "first"`), and concept predictors named
`<domain>_<concept_id>`.

Once a model or workflow is fitted, `model_concepts()` identifies which
concepts have non-zero coefficients. `materialize_concept_features()`
exports matching wide feature tables inside DuckDB using identical
windowing logic. Scoring can then run directly in-database with
`orbital::orbital()` or `tidypredict` without feature drift:

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

## Wide tables, messy timestamps, medication classes, phenotypes & resource limits (Python and R)

``` python
import duckdb
from omop_etl import (configure_resources, load_wide_measurement, vectorized_strptime,
                      extract_medication_classes, audit_medication_strategies,
                      ConditionCriteria, MeasurementCriteria, DrugCriteria, execute_phenotype,
                      DEFAULT_VITAL_CONCEPT_MAPPING, DEFAULT_VITAL_PLAUSIBILITY_FILTERS)

con = duckdb.connect("omop_cdm.duckdb")
configure_resources(con)                       # 75% of available RAM, best spill volume, cores - 1

# staged, one-concept-per-commit unpivot of a wide table, with clinical plausibility bounds
load_wide_measurement(con, "vital.csv", DEFAULT_VITAL_CONCEPT_MAPPING,
                      value_plausibility_filters=DEFAULT_VITAL_PLAUSIBILITY_FILTERS, missing_columns="skip")

# vectorized parsing of '05JAN2020' + '4:15', '01/05/2020' + '4:15 PM', ...
con.sql(f"SELECT {vectorized_strptime('MEASURE_DATE', 'MEASURE_TIME', on_error='default_date')} FROM vital_raw")

# two independent medication-class strategies and a per-site agreement audit
flags, audit = extract_medication_classes(con, "my_cohort", {"statin": {"atc_codes": ["C10AA"]}},
                                          strategy="both", crosswalk="site_drug_crosswalk.csv", return_audit=True)
print(audit[audit.flagged])                    # kappa, concordance, reason per site/class

# composable phenotype, compiled to one query
t2dm = (ConditionCriteria(icd_codes=["E11"], concept_ids=[201826])
        | MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5)
        | DrugCriteria(concept_ids=[21600712], min_days_supply=30))
execute_phenotype(con, t2dm & ~ConditionCriteria(icd_codes=["E10"]), "cohort", cohort_definition_id=7)
```

The same functions exist in R with identical semantics (snake_case,
`library(omopduckdb)`):

``` r
library(omopduckdb)
con <- DBI::dbConnect(duckdb::duckdb(), "omop_cdm.duckdb")
configure_resources(con)
load_wide_measurement(con, "vital.csv", DEFAULT_VITAL_CONCEPT_MAPPING,
                      value_plausibility_filters = DEFAULT_VITAL_PLAUSIBILITY_FILTERS, missing_columns = "skip")
DBI::dbGetQuery(con, paste("SELECT", vectorized_strptime("MEASURE_DATE", "MEASURE_TIME", on_error = "default_date"), "FROM vital_raw"))
res <- extract_medication_classes(con, "my_cohort", list(statin = list(atc_codes = "C10AA")),
                                  strategy = "both", crosswalk = "site_drug_crosswalk.csv", return_audit = TRUE)
res$audit[res$audit$flagged, ]
t2dm <- condition_criteria(icd_codes = "E11", concept_ids = 201826) |
  measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5) |
  drug_criteria(concept_ids = 21600712, min_days_supply = 30)
execute_phenotype(con, t2dm & !condition_criteria(icd_codes = "E10"), "cohort", cohort_definition_id = 7)
```

All file paths handed to DuckDB go through
`omop_etl.paths.sanitize_sql_path`, so Windows paths work in ATTACH,
`read_csv`, `read_parquet` and `COPY`.

## Clinical Risk Scores, Anthropometrics & Consortium Federation (Python & R)

### 1. Bedside Benchmark Heuristics (LACE Index & HOSPITAL Score)

Evaluate standardized clinical heuristics for 30-day readmissions and
mortality directly inside DuckDB:

``` python
from omop_duck_db.scores import calculate_lace_index, calculate_hospital_score

# Compute LACE (Length of stay, Acuity, Charlson, ED visits)
lace_df = calculate_lace_index(con, cohort_table="inpatient_cohort")

# Compute HOSPITAL score (Hemoglobin, Oncology, Sodium, Procedure, Index type, Admissions, LOS)
hosp_df = calculate_hospital_score(con, cohort_table="inpatient_cohort")
```

In R:

``` r
lace_df <- calculate_lace_index(con, cohort_table = "inpatient_cohort")
hosp_df <- calculate_hospital_score(con, cohort_table = "inpatient_cohort")
```

### 2. Anthropometric Harmonization & Derived BMI

Standardize inches (`[in_us]`) and pounds (`[lb_av]`) to metric units
(`cm` and `kg`), recalculate BMI $= \text{weight} / \text{height}^2$,
and replace implausible raw values ($< 10$ or $> 120$):

``` python
from omop_etl import harmonize_anthropometrics

report = harmonize_anthropometrics(con, create_view=True, replace_implausible_bmi=True)
# Created view: measurement_harmonized_anthropometrics
```

In R:

``` r
report <- harmonize_anthropometrics(con, create_view = TRUE, replace_implausible_bmi = TRUE)
```

### 3. Transitions-of-Care Medication Trajectories

Track medication continuation, initiation, and discontinuation across
care transitions:

``` python
from omop_etl import extract_medication_transitions

transitions = extract_medication_transitions(con, cohort_table="inpatient_cohort")
trans_df = transitions.to_dataframe()
# Generates {class}_initiated_inpatient, {class}_discontinued_at_discharge, {class}_continued_across_stay
summary = transitions.summary()
ct = transitions.transition_matrix("raas_inhibitors")
```

In R:

``` r
trans <- extract_medication_transitions(con, cohort_table = "inpatient_cohort")
df <- trans$to_dataframe()
summ <- trans$summary()
matrix <- trans$transition_matrix("raas_inhibitors")
```

### 4. Multi-Site Federated Consortium Connector

Orchestrate multi-center studies (e.g. eDERRI) with independent site
databases attached to a shared Athena vocabulary:

``` python
from omop_duck_db import FederatedConsortium

consortium = FederatedConsortium(
    sites={
        "Site A": "omop_site1.duckdb",
        "Site B": "omop_site2.duckdb",
        "Site C": "omop_site3.duckdb"
    },
    central_vocabulary="central_vocabulary.duckdb"
)

# Cross-site Table 1 and Table 2 in a single call:
t1 = consortium.generate_table1(domain="heart_failure")
t2 = consortium.generate_table2(cohort_table="v_visit_occurrence")
audit = consortium.site_summaries()

# 5. Consortium Bedside Scoring
scores = consortium.calculate_scores(cohort_table="inpatient_cohort")
print(scores.summary())
consortium.close()
```

In R:

``` r
fc <- federated_consortium(
  sites = c("Site A" = "omop_site1.duckdb", "Site B" = "omop_site2.duckdb"),
  central_vocabulary = "central_vocabulary.duckdb"
)
t1 <- consortium_generate_table1(fc, domain = "heart_failure")
t2 <- consortium_generate_table2(fc, cohort_table = "v_visit_occurrence")
audit <- consortium_site_summaries(fc)
scores <- consortium_calculate_scores(fc, cohort_table = "inpatient_cohort")
summary(scores)
consortium_close(fc)
```

### 5. Cross-Site Model Calibration & Decile Drift

Quantify model transportability and risk calibration drift across consortium sites via Greenwood-Nam-D'Agostino $\chi^2$ goodness-of-fit, Brier score decomposition, and decile tracking:

``` python
from omop_duck_db.ml import consortium_calibration

cal = consortium_calibration(
    y_true=y_val,
    y_prob=oof_probs,
    site_labels=site_series,
    n_bins=10
)
print(cal.summary())
fig = cal.plot()
```

In R:

``` r
cal <- consortium_calibration(
  y_true = y_val,
  y_prob = oof_probs,
  site_labels = site_series,
  n_bins = 10
)
summary(cal)
plot(cal)
```

### 6. Clinical Decision Curve Analysis (DCA)

Demonstrate clinical utility of risk stratification models across actionable decision thresholds compared to "treat all" and "treat none" defaults:

``` python
from omop_duck_db.ml import decision_curve_analysis

dca = decision_curve_analysis(
    y_true=y_val,
    y_prob=oof_probs,
    threshold_range=(0.05, 0.35)
)
print(dca.summary())
fig = dca.plot()
```

In R:

``` r
dca <- decision_curve_analysis(
  y_true = y_val,
  y_prob = oof_probs,
  threshold_range = c(0.05, 0.35)
)
summary(dca)
plot(dca)
```

### 7. Competing Risks Cumulative Incidence Function (CIF) & Fine-Gray Modeling

Estimate non-parametric Aalen-Johansen CIF curves and Fine-Gray subdistribution hazards for 30-day readmissions accounting for inpatient and out-of-hospital mortality:

``` python
from omop_duck_db.survival import estimate_cif, fine_gray_estimator, plot_cif

cif = estimate_cif(surv_df, time_col="time_days", status_col="status")
print(cif.summary())
fig = plot_cif(cif)

fg = fine_gray_estimator(surv_df, time_col="time_days", status_col="status", covar_cols=["age", "charlson_score"])
print(fg.summary())
```

In R:

``` r
cif <- estimate_cif(surv_df, time_col = "time_days", status_col = "status")
summary(cif)
plot_cif(cif)

fg <- fine_gray_estimator(surv_df, time_col = "time_days", status_col = "status", covar_cols = c("age", "charlson_score"))
summary(fg)
```

## Benchmark features, deep-learning datasets and leave-one-site-out evaluation (v0.13.0)

Python, with R versions of the cohort arguments, `extract_benchmark_features()`, recalibration / capacity / leave-one-site-out and the vocabulary manifest (not the tensor dataset). All of it was tested on synthetic data; nothing here has been run on a production-size database.

```python
from omop_etl import (define_study_cohort, extract_benchmark_features, extract_multimodal_deep_dataset,
                      export_concept_vocabulary, import_concept_vocabulary, evaluate_loso_transport,
                      capacity_risk_tiers, map_risk_tiers)

# 1. Cohort: calendar bounds (logged in the attrition table), one stay per patient drawn by a seeded MD5 rule
cohort = define_study_cohort(con, min_admit_date="2010-01-01", max_admit_date="2022-09-30",
                             sample_stay_rule="random", random_seed=20261104, table_name="cohort")

# 2. One call for the leakage-free modelling table (labs: index-admission mean and latest in-stay value)
df = extract_benchmark_features(con, "cohort", include_sdoh=False)       # df.attrs["feature_groups"] lists the groups

# 3. Multimodal tensors; a lead site freezes the vocabulary, other sites import it so token ids match
ds = extract_multimodal_deep_dataset(con, df, max_visits=20, max_tokens_per_visit=30, lookback_days=365,
                                     output_format="torch_dict", include_intra_stay=True, pretraining_targets=True)
export_concept_vocabulary(ds["tokenizer"], "vocabulary.json")
tok = import_concept_vocabulary("vocabulary.json")                         # at the other sites: pass tokenizer=tok
# big cohorts: output_format="memmap", "arrow" or "zarr" with output_dir=..., chunk_size=50_000 streams to disk

# 4. Leave-one-site-out transport: balanced training on N-1 sites, natural-prevalence scoring of the held-out site
res = evaluate_loso_transport(df, site_col="site_anon", model_type="xgboost", balance_ratio=1.0, n_bootstraps=100)
res.per_site                                                                # AUROC, Brier, O/E, top-10% PPV with 95% CIs
tiers = capacity_risk_tiers(res.predictions["p"], top_fraction=0.10)       # "the riskiest 10% get a care-management call"
```

## Disclosure control, CONSORT counts, parity audit & site summary packages (v0.12.0)

Everything below works on synthetic or aggregate data and has no study-, site- or cohort-specific content. Python
functions are in `omop_etl`; R has the disclosure helpers, `summarize_attrition_consort()` and `audit_matrix_parity()`.

```python
from omop_etl import (generate_consort_attrition, suppress_small_counts, audit_matrix_parity, summarize_table1_site,
                      build_table1, export_site_summary, pool_site_packages, mapping_coverage_report)

# CONSORT attrition from raw counts (one site), or pooled across sites. Counts below 11 are suppressed, and so is
# anything that could be recovered from them by subtraction (retained[i-1] - dropped[i] == retained[i]).
att = generate_consort_attrition(initial_n=10_000, exclusions=[("Wash-in", 1200), ("Age < 18", 400),
                                                              ("Died in hospital", 310), ("Transferred out", 9)], min_cell=11)
att.to_csv("attrition.csv"); att.to_mermaid(); att.to_tikz()
pooled = generate_consort_attrition(sites={"site_a": table_a, "site_b": {"initial_n": 5000, "exclusions": [...]}})

# Parity between a legacy table and its OMOP-derived counterpart: SMDs, missingness gaps, CCC, kappa, per-level counts
audit = audit_matrix_parity(legacy_df, omop_df, key_col="person_id", by="site_label")
audit.ledger, audit.flagged

# Table 1 from aggregates: one site or many give the same table (exact pooled means/SDs), SMD between two groups
kw = dict(continuous=["age"], categorical=["sex"], binary=["diabetes"], group_col="exposed")
table1 = build_table1(pool_table1_summaries({"a": summarize_table1_site(df_a, **kw), "b": summarize_table1_site(df_b, **kw)}))

# Aggregate-only site package (refuses identifier/date columns, suppresses small cells, hashes every file) and its pooling
export_site_summary("out/site_a", "site_a", attrition=att, table1_summary=summarize_table1_site(df_a, **kw), parity=audit, con=con)
pooled = pool_site_packages({"a": "out/site_a", "b": "out/site_b"})      # verifies each manifest first

# After an ETL: how much of the source vocabulary reached a standard concept
mapping_coverage_report(con)["summary"]
```

```r
att <- summarize_attrition_consort(10000, c(`Wash-in` = 1200, `Age < 18` = 400, `Died in hospital` = 310), min_cell = 11)
write_consort_csv(att, "attrition.csv"); to_mermaid(att)
suppress_small_counts(df, "n", threshold = 11, also = "pct", group_by = "stratum")
audit_matrix_parity(legacy_df, omop_df, "person_id")$ledger
```

- **Release the pooled table or the per-site tables, not both.** With a hidden site cell, the pooled total minus the other
  sites gives it back. A site that suppressed a cell leaves the pooled cell unavailable; if the sites trust the coordinator,
  export exact aggregates (inside the agreement) and suppress after pooling.
- `load_vital()` maps `vital.csv` to the standard concepts 3004249 (systolic BP), 3012888 (diastolic BP), 3036277 (height),
  3025315 (weight) and 3038553 (BMI) even when the vocabulary tables are not loaded yet, derives BMI where only height
  and weight were recorded (`derive_bmi=True`) and can drop implausible readings (`plausible_ranges=True`); it returns the row counts.
- `RunManifest(..., con=con, outputs=[...])` records the vocabulary release the run used and the SHA-256 of the files it wrote;
  `verify_manifest()` checks both.
- `run_bigquery_demo()` (Python and R) is the entry point of the CMS SynPUF smoke test (`vignettes/bigquery_synthetic_demo.Rmd`).
- The Achilles-style checks (missingness, plausibility, orphaned foreign keys) are `run_dqd()`; `sanitize_measurements()` screens implausible values.

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
`OMOP_LEARN_SRC` at a source checkout’s `src/` directory, or install it)
and the HADES packages (FeatureExtraction, PatientLevelPrediction,
Andromeda, DatabaseConnector).

## Large files (Google Drive)

Instead of git/git-LFS, the generated database and vocabulary archive
can be synced through Google Drive:

``` r
gdrive_upload_snapshot()    # push the current database + vocabulary archive
gdrive_download_latest()    # pull the latest snapshot instead of rebuilding
```

Configure the target Drive folder in `config/gdrive.yml` (copy from
`config/gdrive.yml.example`).

## Study-design features: add your own person-level features (v0.14.0, Python and R)

A protocol often needs something OMOP does not define: the rural-urban code of the home ZIP, an area deprivation index, ACS median
household income. A `feature_set` collects such *study-design features*, computes them for the index stays (the address in effect at
admission is resolved once), and writes them as `measurement` rows under site-defined concepts so **OHDSI FeatureExtraction,
PatientLevelPrediction and DeepPatientLevelPrediction read them like any other covariate**. The package ships the framework and
worked examples (RUCA, SDI, any ZIP-keyed table), never the reference data. SDI and RUCA keep the concept ids of
`GEOGRAPHY_CONCEPT_IDS`, so earlier work stays compatible.

```r
fs <- feature_set() |>
  add_feature(ruca_feature("ruca_crosswalk.csv")) |>
  add_feature(sdi_feature("sdi.csv", cut_points = c(20.8, 40.6, 60.4, 80.2))) |>   # the training cohort's fixed cuts
  add_feature(area_feature("income", "acs_b19013.csv", "median_income", transform = "log1p"))

wide     <- compute_features(fs, con, stays_from_cohort(con, "cohort"))   # one row per stay, leakage-free
concepts <- write_feature_set(fs, con, wide)                              # measurement rows
cov      <- FeatureExtraction::getDbCovariateData(
  connection = conn, cdmDatabaseSchema = "main", cohortTable = "cohort", cohortIds = 1, rowIdField = "subject_id",
  covariateSettings = feature_set_covariate_settings(fs, concepts))       # or feature_set_extraction_r(fs, concepts) as text
```

```python
fs = (FeatureSet().add(ruca_feature("ruca_crosswalk.csv"))
      .add(sdi_feature("sdi.csv", cut_points=[20.8, 40.6, 60.4, 80.2]))
      .add(area_feature("income", "acs_b19013.csv", "median_income", transform="log1p")))
wide = fs.compute(con, stays_from_cohort(con, "cohort")); concepts = fs.write(con, wide)
```

Transforms that learn from data (SDI cut points, a percentile grid, a mean and sd, an imputation median) are fitted once on the
training sites with `fit = TRUE`, stored as the feature's state, and shipped in a manifest; at another site they are loaded, never
refitted, and refuse to run without them. This prevents quintile drift and keeps income comparable across sites (a *percentile of
the training distribution* transports, raw dollars can identify the site). The manifest is the same JSON in Python and R.

```r
export_feature_manifest(fs_train, "study_features.json")     # after compute_features(fs_train, con, fit = TRUE)
load_feature_state(fs_site, "study_features.json")           # at the replicating site
```

Add your own with `sql_feature()` (a query over a `_stays` view), `custom_feature()` (an R or Python function) or a full
`study_feature()`, and register new kinds with `register_feature_factory()`. Vignettes: *Study-design features*, *Study features
across sites* and *Study-design features in Python*.

## Tools for a downstream study

The package supports studies that use the validated OHDSI tools (FeatureExtraction, PatientLevelPrediction, CohortGenerator) on DuckDB
and adds what they lack. `inst/examples/study_walkthrough.R` / `.py` run all of the following on a small synthetic CDM; each step is a
few lines to copy.

``` r
# 1. Cohorts as Circe JSON, evaluated without Java or a WebAPI (the definitions ship in cohort_definitions_dir())
cohort <- execute_circe_cohort(con, file.path(cohort_definitions_dir(), "target_cohort.circe.json"))

# 2. A legacy patient list against that cohort: who is found, and the first step that excludes everyone else
rec <- reconcile_cohort(con, "legacy_patients.csv", definition = file.path(cohort_definitions_dir(), "target_cohort.circe.json"),
                        id_col = "patient_id", index_date_col = "admit_date", min_cell = 11)

# 3. Your own version of a feature (a Charlson index, RUCA/SDI) next to the standard one, readable by FeatureExtraction / PLP
#    (see 'Study-design features' above for feature_set(), which also fits and ships the transforms);
#    concept ids are per database, so share a registry across sites and check it
ids <- write_custom_features(con, features)               # person_id, date, one numeric column per feature
export_custom_concept_registry(con, "custom_concepts.json")
check_custom_concepts(con, "other_site_registry.json")

# 4. Is the feature worth keeping? Are two model runs really different? (aggregate output only)
ablate_feature_groups(x, y, list(charlson_site = "charlson_site"))
compare_model_runs(run_a, run_b)

# 5. A bundle that records the concept ids it was trained with and refuses a site where they differ
score_cohort(bundle, new_patients, check_concepts = con)

# data quality: the schema-driven DQD catalog (checked against the official DataQualityDashboard)
run_dqd_catalog(con)
```

The same functions exist in Python (`from omop_etl import ...`). The native Circe compiler evaluates a subset of Circe and refuses the
rest with an error naming the construct; see `OHDSI_INTEGRATION.md` for what was checked against the official engines.

## Disclosure policy for aggregate helpers

Consortium helpers return aggregates, and an aggregate can leak through what sits next to it (complementary
disclosure). The rules that hold today:

- Counts from 1 to `min_cell_count - 1` (default 11) are shown as `<11`, and the percentage next to a suppressed count
  is withheld (`overlap_pct` is NA).
- `consortium_cross_site_duplicates()` withholds the pooled `ALL` row, including its pooled `n_site_1` / `n_site_2`,
  whenever any pair or the pooled count is suppressed: the sum of the site sizes minus the pooled distinct count is
  the number of patients seen at several sites. Use `consortium_duplicate_report()` to get pairs, pooled row
  and this policy in one object; publish it whole and never recompute pooled totals from the pair rows.
- Only the duplicate and overlap helpers were audited for this. Other helpers that print group sizes beside
  suppressed cells have not been audited; treat their output the same way before publishing.

## Reproducing the delivered manuscript: what "similar" means, and what is still open

The package is meant to be the thing delivered going forward: the default federated cohort (one random stay per patient,
2017-01-01 to 2022-09-30, 50/50 balanced training), the standard OHDSI features and the eDERRI features Dr. Rubin asked for,
and the tables, listings and figures of the current manuscript. "The defaults give results very similar to the ones we
already have" is a claim that has to be measured, so this section records the measurement and the rule.

**Timed run with repo defaults** (`scripts/reproduce_study.py`, 5 sites, N = 225,939, 100 bootstraps): 4,086 s, about 68 minutes
(per-site extraction 40 s to 1,274 s depending on site size, tables and figures 1,240 s). The sample was not pinned,
so it is a fresh random draw.

**What was found when that run was compared with the delivered workbook** (`compare_workbooks` counts every cell that moved):

| Sheet | Cells that differ | Reading |
|---|---|---|
| Table 2, Table 4 | 0 | Identical. Every Table 2 AUC is within 0.001 of the delivered value. |
| Table 1 | 29 of 189 | Small: median relative difference about 2%. |
| Table 3, S1, S2 | 3 of 72, 17 of 84, 9 of 63 | Small. |
| Table 5 | 23 of 25 | **Mostly a labelling and metric issue, see below.** |
| Table 6 | 126 of 281 | Real. P-values move most; the SDI and RUCA terms drive it. |
| Table S3 | 75 of 277 | Follows the site labels, plus one extra row. |
| Tables 6, S3, S4 | one extra row each | The `SDI Quintile: Missing / Unknown` term and the new attrition step. |

Three causes were separated:

1. **Site labels.** The delivered workbook uses anonymous labels (Site A to E) that are not the command-line order, so the run's
   "Site A" was a different health system and Table 5 and S3 rows were compared against the wrong sites. The site-to-label
   mapping is deliberately **not** stored in this repository, because publishing it would identify the sites in the manuscript.
   `run_study(site_labels={...})` takes it from the caller; keep that mapping in a private, untracked file. Without it the labels
   are `Site A, B, ...` in command-line order.
2. **Table 5 used a different model and balancing from the delivered table (found and fixed).** The delivered Table 5 comes from
   `pipeline/evaluate_loso_harmonized.py`: the F1-tuned XGBoost (300 trees, learning rate 0.05, depth 6), trained on the other four
   sites with the minority class **upsampled with replacement** to 50/50 (the rule of Tables 2 to 4), median imputation fitted on the
   training sites, scored at the 0.5 threshold on the natural held-out site with **no calibration**. The package's leave-one-site-out
   used HistGBM and **undersampled** the majority class, so sensitivity, specificity and Brier (and the largest site, below) moved. This was
   established by running the legacy script on the legacy feature file (it reproduces the delivered Table 5 to the third
   decimal) and then the package on the same file: with the old balancing the package gave an AUC of 0.713 at the largest site and sensitivity 0.598;
   with `balance_method="upsample"` and the tuned XGBoost it reproduces all five delivered rows exactly (AUC, Brier,
   sensitivity, specificity). `build_study_tables(loso_model="tuned_xgboost")` is now the default (the XGBoost configuration chosen
   by F1 in Table 3, upsampled, median-imputed; `"hist_gbm"` keeps the previous model) and `evaluate_loso_transport` has a
   `balance_method` argument (`"undersample"` default, `"upsample"`). The "manuscript" Table 5 option (0.5 threshold, uncalibrated) is
   the delivered rule; "calibrated" is an alternative view, not what was delivered.
3. **The largest site in Table 5.** Its delivered AUC of 0.687 (and low sensitivity, 0.364) is not a feature problem; it
   is what the upsampled tuned XGBoost gives when the largest site is held out. The package reproduces it exactly on the
   legacy feature file. The first full run with the new default has not been done yet.

**The rule for "similar".** `omop_etl.workbook_similarity(new, old)` returns one pass/fail row per check, with the thresholds in
`SIMILARITY_TOLERANCES`. `run_study` writes it as `<prefix>_Similarity.csv` when `compare_to` is given.

| Check | Default threshold | Result on the unpinned default run (sites paired as delivered) |
|---|---|---|
| Table 2 AUC, every model | within 0.01 | pass (worst 0.001) |
| Table 5 AUC, every site | within 0.02 | **fail** on the first run (4 of 5; the largest site differs by 0.032) because of the model and balancing in item 2; exact on the legacy feature file after the fix |
| Table 6 odds ratio | within 10% relative, for 95% of rows | pass (96.5%) |
| Table 6 direction (old OR outside 0.97 to 1.03) | agrees for 95% of rows | pass (98.1%) |
| Table 6 p < 0.05 verdict | agrees for 95% of rows | **fail** (86%; the 12 disagreements are SDI quintiles, RUCA, smoking, beta blockers, corticosteroids, CKD, diabetes, HIV and overweight BMI) |
| Table 6 rows missing from the new workbook | none | pass |
| Table 1 cells | within 5% for 90% of cells | pass (97.9%) |

The thresholds are a proposal; nothing here has been agreed with Dr. Rubin. Change them in one place (`SIMILARITY_TOLERANCES`) once
they are. The check is a regression gate: it says how far two workbooks are apart, not which one is right. The Table 5 line
above was computed after re-labelling the saved workbook; the next full run will use the manuscript labels directly.

Reading of the result: the model-performance story reproduces (Table 2 to 0.001) and the cross-site story reproduces for four of
five sites. What does not reproduce is which Table 6 covariates are significant, mostly the area-level (SDI, RUCA) terms. That
is consistent with the address `tie_break` choice and with a fresh random sample, and it is untested which of the two it is. The
next step is a run with the stays pinned (`pin_stays`) to separate sampling noise from definition changes. If the pinned run
matches Table 6, the remaining gap is sampling, and the manuscript profile should ship a pinned stay list or a seed manifest.

## Decisions that move the numbers, and how to recreate or change each

Several choices made while reproducing the delivered manuscript change the published numbers. None is hard-wired: each is an option
with a name, a list of values and a record in `<prefix>_run.json` (`decisions`). `python scripts/reproduce_study.py --list-decisions`
prints them; `omop_etl.STUDY_DECISIONS` and `omop_etl.STUDY_PRESETS` hold the same in code.

| Decision (`run_study` argument) | Options | `package` preset | `delivered_2026_11_11` preset | Effect |
|---|---|---|---|---|
| `address_tie_break` | `deterministic`, `highest_zip`, `ambiguous_missing`, `arbitrary`, `first_listed` | `deterministic` | `first_listed` | Which ZIP wins when addresses tie on every rule key. `first_listed` takes the address stored first in the address table (`location_id`), which is what a stable pandas sort does; it is the same on every run. `arbitrary` is engine order and is not reproducible. |
| `address_undated` | `last`, `first` | `last` | `first` | Rank of an address with no start date among those in effect. The study's pandas pipeline ranks it ahead of every dated address (an operator-precedence quirk in `index_address`); `last` is the documented rule. |
| `sdi_missing_term` | `True`, `False` | `True` | `False` | `False` leaves the SDI-missing term out of Table 6, as the delivered workbook did. |
| `table5` | `manuscript`, `calibrated` | `manuscript` | `manuscript` | Delivered rule: tuned XGBoost, 50/50 upsampled training, 0.5 threshold, uncalibrated. |
| `attrition_mode` | `windowed`, `full_history` | `windowed` | `full_history` | CONSORT layout (Table S3). |
| `consort_final_steps` | `True`, `False` | `True` | `False` | Table S3 ends with the window / one-stay and pinned-list steps, or with the last eligibility rule. |
| `table6_increments` | `sd`, `clinical` | `sd` | `clinical` | Continuous Table 6 rows per 1 SD, or per 10 mmHg, 30 days, 5 mEq/L and so on (`CLINICAL_INCREMENTS`; a mapping `label -> (text, size)` gives your own). Same model: p-values and standardised betas do not change. |

Other choices with their own switch: `pinned_stays` (reuse the delivered stay-per-patient draw, or leave unset for a fresh random
draw), `sites` and `site_labels` (the label mapping is supplied by the caller and never stored in the repository),
`sheet_site_labels` (relabel Tables S1 and S3 only: the delivered workbook labels the sites one way in Table 5 and another in S1 and S3;
on the command line `--s1-s3-labels`), the date window (`start`, `end`), `cohort_params`, `profile` (`ederri`, `both`, `none`),
`n_bootstraps`, `min_cell`, `SIMILARITY_TOLERANCES`, and in `build_study_tables` `loso_model` (`tuned_xgboost`, `hist_gbm`) with
`evaluate_loso_transport(balance_method=)`. In R: `derive_geography_features(tie_break=, undated=)` and
`extract_benchmark_features(address_tie_break=, address_undated=)`.

```bash
python scripts/reproduce_study.py --db-dir derived/omop_duckdb --out out --sites A B C --preset delivered_2026_11_11 \
    --pinned-stays selected_stays.csv --tie-break deterministic      # the delivered recipe, with one decision changed
```

**Why the delivered Table 6 differed in the SDI and RUCA rows, and what fixed it.** The regression code was right: given the legacy
feature file, `manuscript_regression(sdi_missing_term=False)` reproduces every delivered Table 6 row. The difference was the per-patient
geography. With the stays pinned, the package's documented address rule disagreed with the delivered SDI quintile for 22,440 stays at
one site, 2,297 at another and 29 at a third; `arbitrary` agreed only in the totals. `first_listed` + `address_undated="first"` gives 0
differing SDI quintiles and 0 differing RUCA categories at all five sites (225,939 stays).

**Measured: pinned stays + `delivered_2026_11_11` + `--s1-s3-labels` (5 sites, N = 225,939, 100 bootstraps, 69 minutes plus a 17-minute
re-run of the tables).** Cells that differ from the November 11 workbook, with `compare_workbooks` tolerances (0.01 absolute and 2%
relative): Tables 1, 2, 4, S1 and S2: 0. Table 6: 2 of 281 (BUN standardised beta 0.13 against 0.12, eGFR p 0.578 against 0.563; three
stays have a different eGFR in the legacy file). Table 3: 1 of 72 (the logistic-regression AUC-tuned row picked `C=0.01` instead of the
near-tied `C=0.1`). Table 5: 3 of 29, all sensitivities (Site C 0.385 against 0.364); every AUC is within 0.003. Table S3: 50 of 252, all in
the per-site rows: the delivered per-site table has ten steps, with the "not metformin-only" step listed twice (steps 7 and 8, same
count), so its later steps are one number off from the combined table, which has nine; the package keeps nine for every site.
`workbook_similarity` passes every check at 100% (worst Table 2 AUC difference 0.002, worst Table 6 odds-ratio difference 1.1%, all 86
p < 0.05 verdicts agree). Reproduce the table with `scripts/check_golden.py --new ... --golden ... --deltas deltas.csv`.

**Anonymous site letters.** `run_study(site_labels="random")` (or `--labels random`) gives each site a random letter, so "Site A" does
not follow the order the sites were typed in; `label_seed` / `--label-seed` makes the draw repeatable, `"ordered"` follows the command
line, and a JSON file written by `save_site_labels()` (`labels_out` / `--labels-out`) is read back with `site_labels=<path>`. The mapping
is never written to the workbook or to `<prefix>_run.json` (which holds only the list of labels): keep the file outside the repository and
the shared results. For a consortium, assign the letters once at the coordinating centre and give each site its own, otherwise every
site's "Site A" is a different system. `assign_site_labels(sites, mode=, seed=)` returns the mapping without running anything.

**Golden workbook.** `tests/golden/synthetic_manuscript_tables.xlsx` is a committed workbook built from a seeded synthetic cohort;
`tests/test_golden_workbook.py` fails when any cell moves and shows that a changed decision is caught. Regenerate it on purpose with
`OMOP_GOLDEN_UPDATE=1 pytest tests/test_golden_workbook.py`. `scripts/check_golden.py` does the same against a real delivered workbook
(not in the repository). `workbook_deltas()` / `delta_summary()` list the cells and rows that moved, sorted by relative change;
`run_study(compare_to=)` writes them as `<prefix>_Deltas.csv` and `<prefix>_Delta_Rows.csv`.

**Results report.** `scripts/render_manuscript.py --workbook ... --out ...` (`omop_etl.render_results_report`) writes
`manuscript_facts.json` (cohort size, readmission rate, AUC ranges, site sizes), one CSV per table, `Results.md` and a self-contained
`Results.html`, so the numbers a text quotes come from the run. It does not render the Quarto manuscript, whose narrative modules
(`manuscript_params_grant_scoped.py`) live outside this repository.

## Status and roadmap (what is claimed, what is verified, what is next)

What is in the package and what has been run:

- **Loading and building**: PCORnet to OMOP ETL, vocabulary loading, and building and "inflating" OMOP tables in DuckDB are the
  core of the package and are field-tested on five production databases (see *Verification status*). Inflating with a full
  vocabulary was not re-run for this section.
- **OHDSI tools**: `OHDSI_INTEGRATION.md` is the authority. It records what was compared against the official tools
  (CirceR, CohortGenerator, FeatureExtraction, Achilles, DataQualityDashboard, PhenotypeLibrary, CohortDiagnostics,
  PatientLevelPrediction) and where the package overlaps or defers. omop-learn, scikit-learn, SARD and the PLP / DeepPLP bridge
  are documented in the ML bridge section. **Not covered by any check in the repo:** Atlas 3, CohortConstructor, Data2Evidence,
  Phenelope and GIS integration. CohortMethod and Strategus appear only as "prefer HADES for published estimates". Treat those
  as planned, not delivered, until a row appears in `OHDSI_INTEGRATION.md`.
- **Cohort and Circe**: "one random stay per patient" cannot be written in Circe. The eligibility part is available as Circe JSON
  (`target_cohort.circe.json`); the random draw is a documented post-step in `define_study_cohort`.
- **Federation**: sites run the same code locally and share aggregate outputs. Disclosure control (`min_cell`, small-cell
  suppression) and site summary packages exist (v0.12.0). Not yet done: a run manifest stamped into every output (package
  version, git commit, seed, options, vocabulary version, timings) and a check that all sites ran identical versions and options
  before pooling. Deep models are evaluated leave-one-site-out on pooled data; training them federated (sharing weights or
  gradients) is a separate design that does not exist yet.
- **Defaults can drift the manuscript.** The 0.18 options (`tie_break`, `table5`, `sdi_missing_term`, `attrition_mode`) change
  the published numbers. Done: the named presets in *Decisions that move the numbers*. Done: a golden-workbook test (synthetic, committed) and `scripts/check_golden.py` for the real delivered workbook.
- **R**: `derive_geography_features(tie_break=, undated=)` and `extract_benchmark_features(address_tie_break=, address_undated=)` take the same options as Python (tested in `tests/testthat/test-geography.R`). Not yet checked: that R and Python give identical ZIPs on the real sites under `arbitrary` (engine order is not guaranteed to agree).
- **Deep models and the DGX Spark**: see the DGX Spark section. The CPU path is tested; the GPU path, the arm64 container tag and
  real-cohort training are not. The existing SARD and DeepPLP trainers (`pipeline/dgx_spark_sard_trainer.py`,
  `ederri_sard_deep_model.py`, `train_clinical_deep_dgx.py`, `train_residual_deep_dgx.py`) still need `fit_predict` wrappers
  before `deep_study_tables` can use them. A deep-model report builder needs a spec: there is no manuscript template for it.
- **Data quality**: run DataQualityDashboard and Achilles on each site database and record the vocabulary version in the manifest.
- **Oracle**: `to_sql`, `verify_transfer` and `from_sql` were verified on live Oracle 19c; `tests/test_live_databases.py` against
  Oracle has not been run.

Order of work: (1) agree the similarity thresholds and run once with pinned stays (and the new Table 5 default) to separate
sampling from definition changes; (2) the manuscript profile and the run manifest; (3) an integration status row for every tool
named above; (4) the Spark GPU run and the SARD / DeepPLP wrappers.

## Verification status

What the project's own tests cover and what they do not:

- Python: the unit tests run on small synthetic DuckDB databases (`pytest tests`). R: the testthat suite runs on small
  synthetic databases.
- Field-tested on five production databases: the v0.11.0 cohort equivalence (Python). The 0.11.1 and later changes
  (per-table feature aggregates, `cap_observation_period`, `remap_discharge_status`, the attrition list) were checked on
  synthetic data only, and the R half of 0.11.1 and later on small synthetic databases only (the 0.11.2 R ports were run with testthat, not on production data).
- Oracle (`translate_cohort_sql`) and the BigQuery sync are unit-tested only. `extract_patient_features` timing
  on production-size cohorts: `scripts/benchmark_features.py` (synthetic fixture: 1.1M stays in about 4 s; a production
  run will differ with the number of events per patient).

## Known limitations

- Athena vocabulary downloads do not include CPT4 concept names (AMA
  copyright) — a separate licensed tool (bundled with the download)
  populates them if you have a UMLS license. Until that’s run,
  CPT4-coded procedures map to concept_id 0.
- A handful of source fields (e.g. admission source) aren’t mapped yet;
  see inline comments in the ETL code for specifics.
- The Python side syncs vocabulary tables from BigQuery with `sync_vocabulary_from_bigquery()` (needs
  `pip install omop-duck-db[bigquery]`); the R connector remains the route for other BigQuery tables. The Python
  BigQuery sync and the Oracle cohort-SQL translation are unit-tested only (fake Arrow reader, generated SQL); neither
  has been run against a live BigQuery project or an Oracle database.
- `translate_cohort_sql(..., "oracle")` raises on constructs it cannot translate, but the translated SQL has never been
  executed on Oracle. The R functions that mirror the Python ones (the cohort, consortium and feature functions) are
  tested on small synthetic databases in both languages; the field tests on production databases so far ran the Python
  side only, so R behaviour on production-size data is not yet verified.
