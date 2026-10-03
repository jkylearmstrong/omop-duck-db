# omopduckdb 0.5.0

### Machine Learning Feature Representation & Ecosystem Bridges
- **High-Dimensional Sparse Concept Matrices (`extract_sparse_concept_matrix`)**:
  - Implemented set-based sparse concept matrix extraction in both Python (`scipy.sparse.csr_matrix`) and R (`Matrix::dgCMatrix`) driven by a cohort parquet.
  - Generates condition, drug, and procedure concept features within lookback windows strictly before the index date (`[index - lookback_days, index)`), eliminating temporal feature leakage.
  - Automatically drops concept `0`, ensures C-locale token string ordering matching `omop-learn`'s `ConceptTokenizer`, and supports train/test column stabilization via token preservation.
- **Deep Sequential Visit Tensors for SARD (`extract_sard_visit_tensors`)**:
  - Padded 3D visit arrays `(N, max_nvisits, max_visit_len)` for Transformer-based architectures (SARD, Kodialam et al., AAAI 2021).
  - Emits visit lengths, chronological visit times (days since 1900-01-01), days before index, and tokenized concept IDs using standard omop-learn special tokens (`[BOS]=0`, `[EOS]=1`, `[SEP]=2`, `[PAD]=3`, `[UNK]=4`).
  - Helper `as_omop_learn_batch` to format tensors directly for PyTorch collation.
- **DuckDB Backend for `omop-learn` (`DuckDBBackend`)**:
  - Added DuckDB backend implementing omop-learn's backend interface, replacing per-patient query loops with a single set-based `LATERAL` join (over 70x faster execution).
  - Bundled DuckDB-dialect feature SQL macros under `inst/sql/omop_learn` for age, gender, conditions, drugs, and procedures.
- **OHDSI HADES / PatientLevelPrediction / DeepPLP Bridge**:
  - Added `hades_preflight()` to verify DuckDB CDM file readiness, table row counts, in-database vocabulary availability, and cross-process read-write exclusivity.
  - Added `plp_database_details()` to wire DuckDB configurations into `PatientLevelPrediction::createDatabaseDetails()`.
  - Added `as_plp_data()` to bridge sparse concept matrices directly into OHDSI `plpData` objects (covariate IDs `concept_id * 1000 + analysis_id` matching FeatureExtraction long-term analysis IDs 102/302/502).
- **tidymodels Bridge & In-Database Scoring**:
  - Added `as_tidymodels_data()` to convert sparse concept matrices into tidymodels-ready tibbles with `y` factor levels (`event_level = "first"`) and syntactic predictor names `<domain>_<concept_id>`, with sparse column support via `sparsevctrs`.
  - Added `model_concepts()` to inspect fitted parsnip models or workflows and identify non-zero coefficient predictors.
  - Added `materialize_concept_features()` to create wide feature tables inside DuckDB using identical windowing rules, enabling direct in-database scoring via `orbital::orbital()` or `tidypredict` without feature drift.

# omopduckdb 0.4.0

### Generic Readmission Cohort & Outcome Builder
- **Inpatient Readmission Cohort (`build_readmission_cohort`)**: Added reproducible, parameterized cohort generation in both Python (`omop_etl.cohort`) and R (`omopduckdb::build_readmission_cohort`) anchored on adult index inpatient stays (`visit_concept_id = 9201`, age >= 18, length of stay >= 1 day, discharged alive).
- **Lost-to-Follow-Up / Right-Censoring Filter (`require_verified_followup`)**: Enforces verified observation by requiring either an inpatient readmission within post-discharge follow-up (e.g. 30 days) or confirmed subsequent clinical activity (IP, ED, OP, lab, vital) after the window, eliminating differential follow-up attrition.
- **Reproducible Index Stay Selection**: Supports `'random'` sampling with reproducible random seed, `'first'`, or `'last'` eligible inpatient admission per patient.
- **Leak-Free Temporal Scoping**: Guarantees strict temporal separation between baseline history ($t \le t_{\text{admit}}$) and readmission outcome window ($t > t_{\text{discharge}}$).
- **Cohort Readmission SQL Macros (`inst/sql/cohort_readmission.sql`)**: Implements pure DuckDB macros `calc_los_days()`, `calc_age_at_date()`, `is_adult()`, `is_discharged_alive()`, `categorize_discharge()`, and `categorize_age_group()`.

### Temporal Feature Extraction & Concept Rollup
- **Temporal Historical Utilization (`extract_temporal_features`)**: Windowed baseline encounter counts (IP, ED, OP), days since prior encounter, days since prior IP/ED stay, prior 30-day readmissions in lookback window, index length of stay, discharge disposition, and demographics.
- **Athena Transitive Closure Rollup (`aggregate_concept_sets`)**: Hierarchical rollup across condition and drug domains traversing `concept_ancestor` to extract binary or count indicators for clinical classes (e.g., insulins, metformin, sulfonylureas, statins, antihypertensives, RAAS inhibitors, beta blockers, systemic corticosteroids).
- **Consolidated Lab & Vital Extraction (`extract_measurements`)**: Maps LOINC concept groups (albumin, ALT, AST, bicarbonate, BUN, creatinine, eGFR, glucose, HbA1c, hematocrit, WBC, sodium, BP, BMI) with configurable acute window aggregation strategies (`last_before_discharge`, `first_on_admission`, `mean`, `median`, `min`, `max`).

### Baseline Table 1 Generation & Reconciliation Harness
- **Comprehensive Baseline Summarizer (`generate_table1`)**: Generates publication-ready Table 1 baseline characteristics stratified by outcome (e.g., 30-day readmission status), reporting overall and stratified Mean (SD), Median [IQR], N (%), Standardized Mean Differences (SMD), and hypothesis test p-values. Emits Table 1b completeness and missingness matrix.
- **Table 1 Aggregation SQL Macros (`inst/sql/table1_aggregations.sql`)**: In-engine computation of continuous and binary SMDs, BMI classification, and tobacco use categorization.
- **Reconciliation & Drift Validation Harness (`validate_table1_reconciliation`)**: Validates prospective OMOP-derived Table 1 distributions against external benchmark extracts with configurable tolerance thresholds, flagging statistical drift, missingness discrepancy, and feature dropouts.

# omopduckdb 0.3.0

### Phase 1: Location & SDoH Address History Ingestion
- **Address & Geography Ingestion (`load_location`)**: Added `load_location(con, source_dir, site_id)` in both Python (`omop_etl.build_omop_cdm`) and R (`omopduckdb::load_location`), ingesting PCORnet `lds_address_history.csv` into the OMOP `location` table. Uses deterministic surrogate keys (`location_id = pcornet_id(addressid)` or sequential numbering), deduplicates addresses, and retroactively populates `person.location_id` with each patient's primary residential address.
- **Address Column Aliases**: Updated `DEFAULT_ALIASES` with `ADDRESS_CITY`, `ADDRESS_STATE`, `ADDRESS_ZIP5`, and `ADDRESS_PERIOD_START` for robust ingestion across PCORnet schema variants.

### Phase 2: Concept Polyhierarchy & Cohort Definition Helpers
- **Standard OHDSI Cohort Tables (`ensure_cohort_tables`)**: Ensures `cohort` and `cohort_definition` tables exist according to OMOP CDM v5.4 specifications.
- **Polyhierarchy Traversal**: Added `get_concept_descendants()`, `get_concept_ancestors()`, and `get_concept_relationships()` to query transitive closures in `concept_ancestor` and relationship tables directly in DuckDB.
- **Concept Set Resolution (`resolve_concept_set`)**: Resolves concept sets with full descendant expansion and exclusion logic using pure set-based SQL.
- **Cohort Creation & Management (`create_cohort`)**: Ingests entry criterion SQL, supports fixed or event-based exit dates, and tracks metadata in `cohort_definition`.
- **CONSORT Attrition Analysis (`compute_attrition`)**: Sequentially tracks patient counts and percentage retention across arbitrary phenotyping criteria.
- **Cohort Set Operations (`combine_cohorts`)**: Combines cohorts using set algebra (`UNION`, `INTERSECT`, `DIFFERENCE`).
- **Cohort Summarization (`get_cohort_summary`)**: Computes cohort statistics (unique subjects, total entries, min/max/mean/median observation duration).

### Phase 3: Native DuckDB DataQualityDashboard (DQD) Engine
- **Pure-SQL OHDSI DQD (`run_dqd`)**: Implemented a standalone, zero-dependency DataQualityDashboard engine that runs in seconds in DuckDB without Java/JDBC or DatabaseConnector.
- **Four Core Check Levels**: Executes `TABLE` (presence & row count), `FIELD` (not-null violations & orphan foreign keys), `CONCEPT` (standard concept compliance), and `TEMPORAL` (event start before end, birth before event) checks.
- **OHDSI DQD JSON Output**: Writes standard OHDSI DQD JSON files with complete `Overview`, `Metadata`, and `CheckResults` schemas compatible with OHDSI visualization frontends.

### Phase 4: Machine Learning Feature Matrix Extractor
- **Patient Feature Extraction (`extract_patient_features`)**: Generates temporal, ML-ready feature matrices anchored on cohort index dates ($T_0$).
- **Retrospective Lookback Windows**: Aggregates condition, drug, procedure, and measurement occurrence counts across customizable lookback windows (e.g., 30, 90, 365 days).
- **Demographics & Target Outcomes**: Extracts baseline patient demographics (age, gender, race, ethnicity) and optional binary outcome flags from target cohorts.
- **Flexible Matrix Formats**: Exports as standard `df` (Pandas / R `data.frame`), Apache Arrow (`arrow`), or OHDSI FeatureExtraction sparse matrix format (`sparse`: `row_id`, `covariate_id`, `covariate_value`).

### Phase 5: Enterprise Multi-Target Export
- **High-Throughput CDM Exporter (`export_cdm`)**: Exports CDM tables to production data warehouses and analytical formats via DuckDB's vectorized copy engine:
  - **Parquet**: Snappy/ZSTD compressed Parquet files with optional partition-by-year on clinical occurrence dates.
  - **PostgreSQL**: Direct in-engine streaming into PostgreSQL via DuckDB's `postgres` extension.
  - **Oracle**: Generates Oracle SQL*Loader control files (`.ctl`), Oracle DDL, and loader shell scripts.
  - **Delimited Files**: Exports standard RFC 4180 CSV and TSV formats.

# omopduckdb 0.2.2

### Multi-Site Consortium Provenance & Federation
- **Pluggable Site Identification**: Added `--site-id <int>`, `--site-anon <str>`, and `--site-name <str>` to `etl_pcornet()` and CLI scripts (`build_omop_cdm.py` and `inst/scripts/etl_pcornet.R`). Automatically populates `cdm_source` metadata (`cdm_source_name`, `cdm_source_abbreviation`, `cdm_holder`), seeds a primary institutional `care_site` record, and links `person.care_site_id = site_id` for downstream OHDSI tools (ATLAS, PLP, DQD).
- **Patient ID Disambiguation (`--disambiguate-patids`)**: Formats `person_source_value` as `src.PATID || '-' || site_id` and derives deterministic surrogate keys via `pcornet_id(src.PATID || '-' || site_id)` across all CDM clinical occurrence tables (`person`, `visit_occurrence`, `condition_occurrence`, `procedure_occurrence`, `drug_exposure`, `measurement`, `death`), preventing cross-site key collisions when pooling extracts.
- **Formal `CARE_SITE` Ingestion**: Added modular `load_care_site()` ingesting `facility.csv` (`care_site_id = pcornet_id(src.FACILITYID)`) and/or creating the root institution care site record, eliminating missing table and orphan key warnings in OHDSI DQD. In `visit_occurrence`, encounters gracefully link to their facility or fall back to the institutional site when facility records are absent.
- **Zero-Copy Federated Consortium Views (`create_federated_consortium`)**: Added `create_federated_consortium(site_dbs, central_vocab_db=None, output_con=None)` in both Python (`omop_etl.federation`) and R (`omopduckdb::create_federated_consortium`). Attaches multiple site databases in `READ_ONLY` mode, optionally attaches a central Athena vocabulary, and constructs unified zero-copy `v_*` views (`v_person`, `v_visit_occurrence`, etc.) projecting `site_id` and `site_anon` provenance without duplicating data.

# omopduckdb 0.2.1

### Real-World Multi-Center Consortium Enhancements
- **Extended Encounter Type Mapping**: Mapped additional high-volume PCORnet encounter types to standard OMOP visit concepts: Telehealth (`TH` $\rightarrow$ `5083`), Observation Services (`OS` $\rightarrow$ `9201`), and Other Ambulatory (`OT` $\rightarrow$ `9202`), preventing millions of clinical encounters from dropping to unmapped `0`.
- **Gender Standardization**: Expanded PCORnet `SEX` mapping to standard OHDSI concepts for Other (`OT` $\rightarrow$ `8521`), Unknown (`UN` $\rightarrow$ `8551`), and No Information (`NI` $\rightarrow$ `8551`), reducing OHDSI DQD unmapped gender flags.
- **Vital Signs Ingestion (`vital.csv`)**: Added modular `load_vital()` step unpivoting PCORnet vital signs into standard LOINC measurements in the `measurement` table:
  - Height (`HT`) $\rightarrow$ LOINC `8302-2` (Body height, unit `[in_us]`, concept `9326`)
  - Weight (`WT`) $\rightarrow$ LOINC `29463-7` (Body weight, unit `[lb_av]`, concept `8739`)
  - Body Mass Index (`ORIGINAL_BMI` / `BMI`) $\rightarrow$ LOINC `39156-5` (BMI, unit `kg/m2`, concept `9531`)
  - Systolic Blood Pressure (`SYSTOLIC`) $\rightarrow$ LOINC `8480-6` (Systolic BP, unit `mm[Hg]`, concept `8876`)
  - Diastolic Blood Pressure (`DIASTOLIC`) $\rightarrow$ LOINC `8462-4` (Diastolic BP, unit `mm[Hg]`, concept `8876`)
  - Skips gracefully if `vital.csv` is absent, preserving modular pipeline execution.
- **Central Vocabulary Support (`attach_central_vocabulary`)**: Added `--central-vocab <path>` argument and helper function `attach_central_vocabulary()` enabling multi-site deployments to attach a shared, read-only 15 GB Athena vocabulary database via DuckDB zero-copy views, eliminating redundant disk duplication across consortium sites.

# omopduckdb 0.2.0

### Architecture & Production Hardening
- **Vectorized Set-Based Mapping**: Replaced correlated scalar subqueries in concept resolution macros with vectorized hash joins across `concept`, `concept_relationship` (`Maps to`), and `source_to_concept_map`, dramatically improving query execution speed on large extracts.
- **Deterministic Surrogate Primary Keys**: Eliminated hash collision risks and non-deterministic IDs by migrating all CDM occurrence tables to deterministic `ROW_NUMBER() OVER (...) + MAX(id)` sequence generation.
- **Date & Datetime Macro Parsing**: Added flexible multi-format date and datetime parsing macros (`parse_omop_date` and `parse_omop_datetime`) supporting `%Y-%m-%d`, `%Y%m%d`, `%d%b%Y`, and `%m/%d/%Y`.
- **Source Column Resilience**: Implemented automated source view preparation (`prepare_source_view`) handling case-insensitive column names, de-identified ID aliases (`ssid` to `PATID`), and projecting `NULL` for missing optional columns without failing execution.
- **Mandatory OHDSI Synthesizers**: Added automated generation for required OMOP CDM v5.4 tables: `observation_period` (computed from patient event boundaries), `drug_era` (30-day persistence window collapse), `condition_era` (30-day persistence window collapse), and `cdm_source` (`cdm_version_concept_id = 756265`).
- **Modular Death Table Handling**: Ingestion of `death.csv` is now optional and skips gracefully with an informative message if omitted, while deduplicating patient death records when provided.

### Vocabulary Management & Remapping Helpers
- **CPT4 Null Concept Name Sanitization**: Athena CPT4 concepts with null names are automatically replaced with `'CPT4 ' || concept_code` upon ingestion to prevent downstream failures.
- **Vocabulary Version Inspector**: Added `check_vocabulary_version()` in both R and Python to query vocabulary metadata and verify Athena release dates.
- **Usagi & Custom Mapping Tools**: Added `import_source_to_concept_map()` and `import_usagi_mappings()` to easily load custom crosswalks directly into `source_to_concept_map`, and `export_unmapped_codes()` to audit unmapped source codes into CSV format ready for OHDSI Usagi review.
- **In-Place Retroactive Remapping**: Added `remap_cdm_table()` and `remap_all()` supporting dry-run previews and transactional in-place re-mapping of existing CDM tables when vocabularies are updated, with automatic era synthesizer regeneration.

### Dual-Language Parity
- Verified 100% byte-for-byte parity across R and Python ETL implementations for all 12 CDM tables in `tests/test_etl_parity.py`.

# omopduckdb 0.1.1

- Added unified cross-language test coverage tracking (`covr` for R, `pytest-cov` for Python) and a dedicated `.github/workflows/test-coverage.yml` CI workflow.
- Created consolidated developer release scripts (`scripts/release.py` and `scripts/release.R`) for automated dual-package version bumping, Quarto rendering, test suite execution, wheel building, and git tagging.
- Enhanced Docker container configuration for multi-stage testing and deployment.

# omopduckdb 0.1.0

Initial packaged release.

- Build an OMOP CDM v5.4 schema in DuckDB from a shared, checked-in DDL.
- Load and refresh the OHDSI Athena vocabulary, transactionally per table.
- Map PCORnet-format source data into the CDM via parallel R and Python ETL
  implementations that share the same concept-mapping SQL, verified to
  produce identical output against a shared test fixture.
- Load OMOP-shaped tables directly from BigQuery (vocab refresh or demo data),
  as an alternative to a local vocabulary download.
- Sync the generated database and vocabulary archive through Google Drive
  instead of committing large binaries to git.
- Packaged as both an installable R package (`DESCRIPTION`/`NAMESPACE`,
  `testthat` suite, `pkgdown` site) and a Python package (`pyproject.toml`,
  `pytest` suite).
