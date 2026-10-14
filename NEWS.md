# omopduckdb 0.5.9

Request 2 ships with **dual Python and R usage**. R functions are snake_case mirrors of the Python classes: `filter_bounds`, `harmonize_measurement_units`, `medication_extractor`, `table1_generator`, `cohort_flow`, `generate_table2`, `as_plp_arrow` (Python: `filter_bounds`, `harmonize_measurement_units`, `MedicationExtractor`, `Table1Generator`, `CohortFlow`, `Table2Generator`, `as_plp_arrow`). The domain profiles live once, in `inst/extdata/domain_profiles.yml`, and a test keeps the Python dict in sync.

### Mapping bug fixes (audited against the OMOP vocabulary)
- **LOINC `48642-3` (eGFR, MDRD) was in `anion_gap`**, giving an Anion Gap mean of ~59.5 mEq/L. Moved to `egfr`; `anion_gap` is now `1863-0` + `33037-3`.
- **`DEFAULT_MEDICATION_CONCEPTS` had wrong concept ids** (found by the same audit): `insulins` held isoniazid (1782521) and tolazamide plus the whole A10 "diabetes drugs" class; `metformin` held A10B (all non-insulin glucose-lowering drugs); `sulfonylureas` held nateglinide; `statins` held ACE inhibitors (C09A); `raas_inhibitors` held calcium-channel blockers (C08); `antihypertensives` held only beta blockers. Now A10A, A10BA, A10BB, C10AA, C09 and C02+C07+C08+C09. **Behaviour change:** flag counts for these classes will change.
- Still open (not changed): `33914-3` is a deprecated LOINC (kept for legacy data); `59260-0` hemoglobin is mmol/L, so mixing it with `718-7` is only safe after unit harmonization; wide-vital `HT`/`WT` use `[in_us]`/`[lb_av]` labels with UCUM concept ids 9326 (British inch) / 8739 (US pound); `PHYSIOLOGIC_LAB_BOUNDS` (e.g. potassium 1.5-10, hemoglobin 1-25) differs from the new `DEFAULT_PHYSIOLOGIC_BOUNDS`.

### New
- `filter_bounds(df, bounds=None | {...} | False, action="nullify"|"clip"|"drop")` with `DEFAULT_PHYSIOLOGIC_BOUNDS`; wide or long frames; `.attrs["bounds_report"]`.
- `harmonize_measurement_units(con, ...)`: unit preflight report plus a `measurement_harmonized` view (cells/uL -> 10^3/uL, umol/L -> mg/dL, mmol/L glucose etc.). Unrecognized units are flagged, never guessed. `extract_measurements(measurement_table=)` reads from it.
- `extract_medication_classes(window="pre_admission"|"inpatient"|"discharge")` and `MedicationExtractor` (concept_ancestor track, crosswalk track, per-window flags).
- `Table1Generator` with `DOMAIN_PROFILES` (heart_failure, nephrology, sepsis_icu, oncology, pulmonary, pharmacotherapy_safety), `add_custom_lab`, YAML/dict profiles. Profile code lists are a starting point for clinician review; LOINC and ATC codes were verified to exist in the OMOP vocabulary.
- `CohortFlow` (sequential steps, attrition table, Markdown/Mermaid/TikZ/Graphviz output, visit-type / anchor / follow-up preflight), `Table2Generator` (completeness, sentinel and zero-SD flags), `as_plp_arrow` (in-memory Arrow covariate tables for PatientLevelPrediction; no file round-trip).
- R: `to_graphviz()` for attrition tables; `extract_measurements(measurement_table=)`; `extract_medication_classes(window=, end_date_col=)`. R's `as_plp_arrow()` builds the Arrow tables from DuckDB query results held in R memory (no files); it is not zero-copy.

### External-stack integration tests
- Run against the real packages: **PatientLevelPrediction 6.7.0** (+ FeatureExtraction 3.14.0, Andromeda 1.2.1): all six previously skipped `test-hades-bridge.R` tests pass. **DeepPatientLevelPrediction 2.4.0** (torch 2.14.1 CPU through reticulate) fits an MLP on data from `as_plp_arrow()`. **omop-learn** (clinicalml, cloned): the 17 backend tests pass with its real classes (`OMOP_LEARN_SRC`). **OHDSI/OmopOnDuckDB 0.1.0**: an omop-duck-db file opens through `duckdbSource()` and our functions run on its connection.
- New `plp_arrow_to_plp_data()` turns `as_plp_arrow()` output into a `plpData`; a real `runPlp()` LASSO fit on it recovers the planted lab as its top coefficient.
- Windows caveat: with R's `arrow` already loaded, python's `pyarrow` fails to import ("DLL load failed while importing lib") and DeepPLP silently produces no model. Import pyarrow first (done in `tests/testthat/helper-plp-arrow.R`); the DeepPLP test asserts a metric exists so this failure cannot pass silently.
- OmopOnDuckDB 0.1.0 is an unfinished template (placeholder DESCRIPTION, `duckdbSource()` only). Its `validateCon()` returns `NULL` when handed an existing connection, so pass a file path.
- These tests skip when the package is absent; `DeepPatientLevelPrediction`, `OmopOnDuckDB` and `reticulate` are added to Suggests.

### Vocabulary guards (Python and R)
- Always on: no LOINC / concept id may be assigned to two features in the default maps; `domain_profiles.yml` must equal the Python profiles.
- Opt-in, set `OMOP_VOCAB_DB` to a DuckDB file holding the OMOP `concept` table: every LOINC and ATC code in the default maps and the domain profiles must exist, and its `concept_name` must contain a keyword for the feature it is filed under (`tests/fixtures/vocab_feature_keywords.csv`, shared by both languages); every default medication concept id must be the drug or class it claims to be. Re-introducing the `48642-3` anion-gap bug or the old statin id makes these tests fail. Adding a feature without keywords also fails, by design. The guard already caught a wrong DOAC code (`B01AE06` is bivalirudin) in the new profiles.

# omopduckdb 0.5.8

Closes the remaining items of the "outstanding requests" review of 0.5.7. Everything ships with **dual Python and R usage** (R functions are snake_case mirrors; Python lives in `omop_etl`). The readmission-cohort engine (`define_study_cohort`), Table 1 / missingness (`generate_table1`) and the Elixhauser / Charlson calculators were already present and are unchanged apart from the guard below.

### `load_wide_measurement(coalesce_columns=)` -- no more duplicate BMI rows
- Columns that carry the same measure (PCORnet `BMI` and `ORIGINAL_BMI` both map to concept 3038553) now yield ONE measurement row per source row. `coalesce_columns="auto"` (default) groups columns that share a `concept_id`, in `concept_mapping` order; `[("ORIGINAL_BMI", "BMI")]` / `list(c("ORIGINAL_BMI", "BMI"))` sets an explicit precedence; `None` / `NULL` restores the 0.5.7 behaviour. The first value inside its plausibility bounds wins, so an implausible primary never masks a plausible fallback. The report gains a `coalesced_into` column. **Behaviour change:** with the default mapping `ORIGINAL_BMI` no longer inserts its own rows.

### `extract_inpatient_labs` and `extract_measurements(outlier_bounds=, strategy="worst")`
- Index-stay labs per cohort row: acute labs (lactate, troponin, pO2, pCO2; `ACUTE_LAB_PANEL`) come only from the stay, routine labs from `routine_window` (`stay` / `lookback` / `all`). `selection_rule` is `last_in_stay`, `first_in_stay`, `mean`, `median`, `min`, `max` or `worst` (direction per lab in `WORST_LAB_DIRECTION`). Physiologic outlier bounds are applied BEFORE selection.
- `extract_measurements` gains `outlier_bounds`, `worst_direction` and the `worst` strategy.

### `extract_prior_utilization`
- Prior IP / ED / OS visit counts, summed length of stay, days since the last encounter and prior 30-day readmissions in the look-back (`lookback_days`, default 365) strictly before the index admission. The index visit is excluded; `anchor_date="discharge"` is rejected. Visit groups are configurable (`DEFAULT_VISIT_CONCEPT_GROUPS`).

### Train/serve skew guard
- `extract_elixhauser_comorbidities` and `extract_charlson_index` take `exclude_index_diagnoses=` (default **on**): diagnoses attached to the cohort row's own `visit_occurrence_id` are never counted (they are coded after discharge), and a discharge-like `index_date_col` (`cohort_end_date`, `*discharge*`) is an error. **Behaviour change:** callers that deliberately anchored at discharge must pass `exclude_index_diagnoses=False`.
- `OMOPScoringPipeline` (Python) / `omop_scoring_pipeline()`, `fit_scoring_pipeline()`, `predict_scoring_pipeline()` (R): a fitted imputer, scaler and model bundled with the ordered feature schema. Scoring raises on missing, unexpected or non-numeric columns and on NaN without an imputer; `out_of_range` / `scoring_pipeline_out_of_range()` is a drift check; bundles save and load (pickle / RDS -- only load files you trust).

# omopduckdb 0.5.7

All request-3 features ship with **dual Python and R usage** (R functions are snake_case mirrors in the `omopduckdb` package; Python lives in `omop_etl`).

### Staged wide-table ingestion (`load_wide_measurement`) (Python `omop_etl` / R `omopduckdb`)
- Stages a wide table (PCORnet `vital`, flowsheets, device streams) once into an integer-keyed intermediate table, then inserts into `measurement` one concept at a time with a commit between passes (`batch_execution_mode="sequential"`), instead of one giant uncommitted `UNION ALL`. Per-measurement plausibility bounds (`value_plausibility_filters`, e.g. SBP 30-300, DBP 10-200, BMI 5-120) and a per-column report (read / out-of-range / unmatched person / inserted). `DEFAULT_VITAL_CONCEPT_MAPPING` and `DEFAULT_VITAL_PLAUSIBILITY_FILTERS` cover PCORnet VITAL.

### Vectorized datetime normalizer (`vectorized_strptime`) (Python `omop_etl` / R `omopduckdb`)
- One SQL expression, no UDFs: normalizes single-digit hours, missing minutes, `0415`, and 12/24-hour notation, then tries a prioritized format list in a single `try_strptime(x, [...])`. `on_error='null' | 'default_date' | 'raise'`. `install_vectorized_strptime(con)` registers it as a macro.

### Dual-strategy medication classifier (`extract_medication_classes`, `audit_medication_strategies`) (Python `omop_etl` / R `omopduckdb`)
- Strategy A (user crosswalk of source strings/CUI/NDC to classes) and Strategy B (`concept_ancestor` traversal from ATC 3/4/5 classes or RxNorm ingredients), `strategy='source' | 'standard' | 'both'`.
- The auditor reports Cohen's kappa, raw concordance, prevalence gap and a reason (`source_blind`, `standard_blind`, `low_kappa`, `low_concordance`, `prevalence_gap`, `peer_outlier`) per site and class, so sites with broken drug mappings stand out.

### Path sanitizer (`omop_etl.paths`) (Python `omop_etl` / R `omopduckdb`)
- `sanitize_sql_path`, `sql_path_literal`, `attach_database`, `read_csv_sql`, `read_parquet_sql`, `copy_to`. All internal ATTACH / `read_csv` / `read_parquet` / `COPY` call sites now go through them (Windows backslashes, spaces, single quotes; remote URIs untouched).

### RAM governor and spill manager (`configure_resources`) (Python `omop_etl` / R `omopduckdb`)
- Sets `memory_limit` to a fraction (default 75%) of available RAM, picks the temp directory on the writable volume with the most free space, and sets `threads` from usable cores. `etl_pcornet(auto_resources=True)` / `--auto-resources` on the ETL CLI; explicit `memory_limit`, `threads` and `temp_dir` still win.

### Phenotype cohort DSL (`omop_etl.phenotype_dsl`) (Python `omop_etl` / R `omopduckdb`)
- `ConditionCriteria` (concept sets, ICD prefixes, primary/secondary), `MeasurementCriteria` (LOINC / thresholds / lookback), `DrugCriteria` (ingredients / ATC / supply days / lookback) composed with `|`, `&`, `~` (`PhenotypeUnion`, `PhenotypeIntersect`, `PhenotypeNot`, `Everyone`). `compile_phenotype` emits one single-pass DuckDB query; `execute_phenotype` writes a cohort table; dict round-trip via `to_dict` / `phenotype_from_dict`.

### Roadmap gap closures
- **PCORnet `VITAL.SMOKING`** is now mapped to an `OBSERVATION` row: `observation_concept_id = 43054909` (LOINC 72166-2, *Tobacco smoking status*) with the LOINC answer concept in `value_as_concept_id` (01 -> 45881517, 02 -> 45884037, 03 -> 45883458, 04 -> 45879404, 05 -> 45881518, 06 -> 45885135, 07 -> 45884038, 08 -> 45878118). `NI` / `UN` / `OT` carry no status and produce no row. Python `load_vital`, R `etl_pcornet`.
- **`load_vocabulary(tables =, drop_indexes =)`** (Python and R): reload only the named vocabulary tables, and drop / recreate user indexes around the bulk load.
- **`build_safe_source_view()`** (Python and R): `src_<table>` view with header aliasing and NULL padding, as specified in RFC-3.
- **`python -m omop_etl.quality_check --db-path ... --output dq_report.json`**: the Duck-DQD entry point named in RFC-5 (R: `run_dqd()`).
- **R ETL** now logs aliased and null-padded columns like the Python ETL, and `etl_pcornet()` gains `auto_resources`, `memory_limit`, `threads` and `temp_dir`.

### ETL
- `prepare_source_view` now prints which source columns were aliased (e.g. `ssid -> PATID`) and which were null-padded.

# omopduckdb 0.5.6

### Automated OHDSI PLP Study Package Generator
- **PLP Study Package Exporter (`export_plp_study_package`)**:
  - Bundles target cohorts, outcome cohorts, covariate settings, and cross-validation workflows into an end-to-end reproducible R study package adhering to standard OHDSI `PatientLevelPrediction` (PLP) conventions.
  - Automatically generates package scaffolding including `DESCRIPTION`, `NAMESPACE`, `R/RunStudy.R`, `R/ShinyApp.R`, pre-configured Shiny diagnostic viewers, cross-validation configurations, study JSON manifests, and an interactive CLI / Shiny launcher (`extras/launch_shiny.R`).
  - Available with full 1:1 parity in Python (`omop_etl.plp_study_package`) and R (`omopduckdb::export_plp_study_package`).

### Direct Andromeda Zero-Copy View Adapter
- **Direct Andromeda Adapter (`duckdb_to_andromeda`, `as_andromeda`, `export_to_andromeda`, `AndromedaAdapter`)**:
  - Eliminates the SQLite disk serialization bottleneck inside OHDSI Andromeda by mounting DuckDB tables or chunked streaming into Andromeda containers without intermediate disk round-trips.
  - Supports DuckDB SQLite extension virtual table registration (`sqlite_scanner`) where available, with graceful streaming chunk fallback.
  - 1:1 cross-language parity in Python (`omop_etl.andromeda`) and R (`omopduckdb::duckdb_to_andromeda`, `omopduckdb::as_andromeda`).

### Automated CONSORT / STROBE Flowchart Exporters
- **CONSORT / STROBE Attrition Diagrams (`ConsortAttrition`, `to_typst`, `to_tikz`, `to_mermaid`)**:
  - Extends `CohortExtractor` with step-by-step attrition tracking capturing starting populations, excluded counts, remaining counts, and inclusion reasons across every cohort definition step.
  - Native export of publication-ready flowcharts to Typst (`#block()`, `#rect()`, arrows), LaTeX TikZ (`\begin{tikzpicture}`, `\node`, `\draw`), and Mermaid (`graph TD`) for direct insertion into Quarto and LaTeX manuscripts.
  - Accessible via `.attrition.to_typst()` / `.attrition.to_tikz()` on `CohortExtractor` in Python and S3 generics `to_typst()` / `to_tikz()` in R.

### Foundation Model Tokenizer Integration (EHR-SHOT / CEHR-BERT / FEMR)
- **Clinical Sequence Tokenizer (`extract_clinical_sequence_tokens`, `to_clinical_sequence_tokens`, `export_clinical_sequence_tokens`)**:
  - Expands `extract_sard_visit_tensors()` to export HuggingFace/PyTorch-compatible clinical event token sequences (`input_ids`, `attention_mask`, `token_type_ids`, `labels`).
  - Configurable special tokens (`[PAD] = 0`, `[UNK] = 1`, `[CLS] = 2`, `[SEP] = 3`, `[MASK] = 4`, `[VISIT] = 5`, `[DISCHARGE] = 6`).
  - Preserves event visit boundaries with configurable maximum sequence lengths and padding modes.
  - Full parity in Python (`omop_etl.ml_features`) and R (`omopduckdb::extract_clinical_sequence_tokens`).

### Federated Consortium Meta-Analysis Aggregator
- **Federated Meta-Analysis Aggregator (`federated_meta_analysis`, `FederatedMetaAnalysisResult`)**:
  - Multi-site summary estimator calculating DerSimonian-Laird random-effects pooled estimates ($\hat{\theta}_{\text{DL}}$, SE, 95% CI), Cochran's $Q$ test of heterogeneity, between-study variance ($\tau^2$), and Higgins' $I^2$ across federated DuckDB nodes without sharing patient-level data.
  - Supports odds ratios (OR), relative risks (RR), hazard ratios (HR), and mean differences.
  - Built-in ASCII forest plot generation (`to_forest_text()`), Mermaid diagram generation (`to_forest_mermaid()` / `to_mermaid()`), and DataFrame conversion.
  - Full parity in Python (`omop_etl.federation`) and R (`omopduckdb::federated_meta_analysis`).

# omopduckdb 0.5.5

### In-Engine Propensity Score & IPTW Weighting
- **Propensity & IPTW Module (`generate_propensity_weights`, `PropensityResult`)**:
  - In-engine logistic regression propensity score estimation ($e_i$) with Newton-Raphson/IRLS and L2 stabilization.
  - Generates Inverse Probability of Treatment Weighting (IPTW) for Average Treatment Effect (ATE), Average Treatment Effect on the Treated (ATT/SMR), and Stabilized ATE.
  - Implements pre- and post-weighting Standardized Mean Difference (SMD) covariate balance auditing, guaranteeing maximum post-weighting SMD $< 0.10$.
  - 1:1 cross-language parity in Python (`omop_etl.propensity`) and R (`omopduckdb::generate_propensity_weights`).

### Kaplan-Meier Survival & Competing Risks Cumulative Incidence (CIF)
- **Survival & Competing Risks Estimator (`estimate_km_survival`, `SurvivalResult`)**:
  - Non-parametric Kaplan-Meier survival curve estimation $S(t)$ with Greenwood standard error confidence intervals (log-log and linear Wald).
  - Fine-Gray / Aalen-Johansen non-parametric Cumulative Incidence Function (CIF) estimation for 3-state competing risks outcomes (censored vs. primary vs. competing mortality).
  - Validated to within $10^{-6}$ numerical tolerance against R `survival::survfit`.
  - Publication-ready plotting hooks and `.to_dataframe()` export in Python and R.

### Delta Lake & Apache Iceberg Lakehouse Connector
- **Lakehouse Connector (`omop_connect_lakehouse`)**:
  - Dynamic zero-copy querying of remote cloud object storage (S3, MinIO, Azure Blob, GCS) or local lakehouse stores.
  - Registers remote/partitioned Delta Lake, Apache Iceberg, or Parquet datasets as unified OMOP views (`v_person`, `v_condition_occurrence`, etc.) with S3 credential handling.

### Federated Consortium Governance & Dynamic Differential Privacy
- **$(\epsilon, \delta)$-Differential Privacy Noise Injector (`with_differential_privacy`)**:
  - Injects calibrated Laplace noise ($L \sim \text{Laplace}(0, 1/\epsilon)$) directly into dynamic DuckDB SQL views without external library overhead.
  - Ensures repeated aggregate queries vary predictably within mathematical privacy bounds without leaking exact patient counts.

### Deep Learning & Zero-Copy GPU Streaming
- **Zero-Copy CUDA IPC Tensors (`arrow_to_cuda_tensors`)**:
  - Direct buffer streaming from DuckDB via PyArrow C Data Interface / DLPack into PyTorch GPU tensors in VRAM without intermediate host-RAM bounce buffering, with graceful fallback to CPU tensors.

### Full ATLAS/CIRCE AST Compiler Expansion
- **CIRCE `InclusionRules` Support**:
  - Expanded `compile_circe_to_duckdb` to parse multi-criteria `InclusionRules` arrays supporting `DemographicCriteria` (Age, Gender), `ConditionOccurrence`, `DrugExposure`, `Measurement` (value comparisons), `ProcedureOccurrence`, observation lookback/lookahead windows, and `ALL` / `ANY` Boolean logic.

### Upward-Tree Vocabulary Auto-Discovery
- **Nested Directory Traversal in `omop_connect()`**:
  - Searches up to 4 parent directory levels for `central_vocabulary.duckdb`, `vocabulary.duckdb`, or `vocab.duckdb`, falling back to the `OMOP_CENTRAL_VOCAB` environment variable.

### Top 25 OHDSI Phenotype Library Coverage
- **Expanded Phenotypes (`list_available_phenotypes`, `get_phenotype_concept_set`)**:
  - Expanded library from 8 to 25 standard OHDSI Phenotype Library concept bundles, adding CKD (stages 1-5, ESRD), Asthma, Rheumatoid Arthritis, Major Depressive Disorder, Dementia/Alzheimer's, Liver Cirrhosis, Breast Cancer, Colorectal Cancer, Lung Cancer, Prostate Cancer, COVID-19, VTE/DVT/PE, PAD, IBD (Crohn's/UC), Severe Aortic Stenosis, Atopic Dermatitis, and Osteoarthritis.

### ML Cohort Extractor & Feature Matrix Builder
- **Cohort & Feature Extraction (`CohortExtractor`, `FeatureMatrixBuilder`)**:
  - Standardized admission index cohort definition with configurable selection rules (`first`, `last`, `random_eligible`) and verified post-discharge follow-up horizons.
  - Strict lookback window separation for demographics, prior utilization, acute inpatient labs, chronic conditions, and medications, avoiding future data leakage.
  - Dual export modes for tabular modeling (`to_tabular`) and 3D temporal sequence tensors (`to_tensor`).

### Standalone CLI Entry Point
- **`omop-duckdb` CLI**:
  - Registered CLI console scripts `omop-duckdb` and `omop-duckdb-cli` supporting subcommands: `build`, `dqd`, `cohort`, `table1`, and `export`.

### Partitioned Parquet Export & Synthetic Test Fixtures
- **`export_to_parquet`**: Native single-file and Hive-partitioned Parquet export directly from DuckDB.
- **Mock PCORnet Test Fixture Generator (`generate_mock_pcornet.py`)**: Deterministic multi-site synthetic PCORnet data generator simulating hospital variations, header aliases (`ssid`, `enc_id`), and missing optional columns.

# omopduckdb 0.5.4

### Pre-compiled OHDSI Phenotype Bundles & Native CIRCE Compiler
- **Phenotype Library (`get_phenotype_concept_set`, `list_available_phenotypes`)**:
  - Pre-compiled OHDSI-standard concept sets and ICD-10 source prefixes for core clinical conditions: Type 2 Diabetes, Heart Failure, Sepsis, COPD, Acute Kidney Injury, Atrial Fibrillation, Hypertension, and Ischemic Stroke.
- **Native CIRCE Compiler (`compile_circe_to_duckdb`, `execute_circe_cohort`)**:
  - Compiles ATLAS/CIRCE JSON cohort definitions directly to native DuckDB SQL without requiring Java or WebAPI runtime dependencies.

### Bedside Clinical Risk Scores & Core Inpatient Lab Harmonizer
- **Clinical Risk Scores (`calculate_bedside_scores`)**:
  - Implemented in-engine computation for LACE Index, HOSPITAL Score, SOFA Score, and CHA2DS2-VASc risk stratifications directly from OMOP CDM tables.
- **Core 14 Inpatient Lab Harmonizer (`extract_standard_labs`, `CORE_14_LAB_PANEL`)**:
  - Standardized measurement extraction covering the 14 essential inpatient acute labs with physiologic outlier winsorization.

### Study Attrition Reporting & Longitudinal Treatment Episodes
- **CONSORT Flow Generator (`generate_consort_attrition`)**:
  - Generates reproducible patient attrition funnels from sequential SQL criteria with automated rendering to Mermaid flowcharts, LaTeX tables, Markdown, and tabular data.
- **Treatment Episode Collapser (`build_treatment_episodes`)**:
  - Collapses longitudinal drug dispensing/exposure records into persistent treatment episodes with customizable gap thresholds (`max_gap_days`).

### Reporting Exporters, Heuristic Remapping & Federation Auditing
- **Publication Table 1 / Table 2 Exporters (`export_table1`)**:
  - Multi-format exporter rendering baseline demographic and comorbidity comparison tables to Markdown, LaTeX, Quarto, and CSV formats.
- **Heuristic Concept Remapping (`auto_remap_unmapped`)**:
  - Automated repair and normalization for unmapped source values (dot stripping, spacing normalization, Athena mapping table lookups).
- **Consortium Privacy & Discrepancy Auditing (`with_cell_suppression`, `check_cross_database_discrepancy`)**:
  - Small-cell suppression wrapper for privacy-preserving federated analytics and cross-site vocabulary discrepancy auditing.

# omopduckdb 0.5.3

### Transparent Central Vocabulary Single-Store & Connection Engine
- **`omop_connect()`**:
  - Implemented unified connection helper in Python (`omop_etl.omop_connect`) and R (`omopduckdb::omop_connect`).
  - Auto-discovers sibling or repository central vocabulary DuckDB instances (`central_vocabulary.duckdb` / `vocabulary.duckdb`).
  - Attaches vocabulary read-only as `central_vocab` and automatically sets DuckDB's `search_path = 'main,central_vocab'`, allowing queries to reference `concept`, `concept_ancestor`, and other vocabulary tables directly without schema qualification.
  - Automatically loads SQL macros into temporary session scope on read-only databases without throwing write errors.

### Concept Ancestry & Physiologic Clamping SQL Macros
- **Recursive Hierarchy & DQD Clamping Macros (`mapping_macros.sql`)**:
  - `descendants_of(ancestor_id)`: Table macro returning all descendant concept IDs from `concept_ancestor` (including self rows).
  - `ancestors_of(descendant_id)`: Table macro returning all ancestor concept IDs from `concept_ancestor` (including self rows).
  - `clamp_physiologic(val, min_val, max_val)`: Scalar macro for in-engine SQL boundary clamping and outlier winsorization.
  - Designed with name-capture protection and subquery correlation safety.

### Standardized 31 Elixhauser Domains & Charlson Comorbidity Profiler
- **Validated Comorbidity Profilers (`extract_elixhauser_comorbidities`, `extract_charlson_index`)**:
  - Implemented 1:1 dual-language comorbidity profiling in Python (`omop_etl.comorbidity`) and R (`omopduckdb::comorbidity`).
  - Covers all 31 Elixhauser domains (AHRQ / Quan et al.) and composite van Walraven score.
  - Covers all 17 Quan-Charlson categories, hierarchical condition adjustment, and composite Charlson Comorbidity Index.
  - Dual matching mode: evaluates normalized ICD-9/10 prefixes via `condition_source_value` and standard SNOMED descendants via `condition_concept_id`.
  - Bundled curated mapping tables with documented provenance in `inst/extdata/comorbidity/` (`elixhauser_codes.csv`, `charlson_codes.csv`, `PROVENANCE.md`).

### Physiologic Range Sanitization & Outlier Winsorization Engine
- **DQD Measurement Range Sanitizer (`sanitize_measurements`)**:
  - Implemented in Python (`omop_etl.sanitize_measurements`) and R (`omopduckdb::sanitize_measurements`).
  - Bundles 136 standard biological plausibility limits derived from OHDSI DataQualityDashboard in `inst/extdata/physiologic_limits.csv`.
  - Supports three sanitization methods: `dqd_biologic_limits`, `winsorize_iqr`, `z_score_cutoff`.
  - Configurable actions: `nullify`, `clamp`, or `drop_row`.
  - Tracks sanitization audit trail with `sanitize_status` and preserves raw values in `value_as_number_raw`.

### Unified Study Cohort Template Engine
- **General Study Cohort Builder (`define_study_cohort`)**:
  - Implemented in Python (`omop_etl.cohort.define_study_cohort`) and R (`omopduckdb::define_study_cohort`).
  - Parameterizes arbitrary visit types (`"inpatient"`, `"emergency"`, `"outpatient"`, or custom concept lists), prior wash-in window, follow-up window, minimum age, and minimum length of stay.
  - Supports reproducible sampling rules (`"first"`, `"last"`, `"random"`) and verified follow-up right-censoring checks.
  - Unifies and powers legacy `build_readmission_cohort()` and `build_end_of_life_cohort()` wrappers with row-for-row parity.

# omopduckdb 0.5.2

### Feature Engineering & Data Leakage Prevention
- **Formalized Anchor Date & Wash-in Buffers (`anchor_date`, `washin_buffer_days`, `washin_buffer_hours`)**:
  - Added `anchor_date = c("discharge_date", "admit_date", "auto")` to `extract_sparse_concept_matrix`, `extract_sard_visit_tensors`, and `materialize_concept_features` across both Python and R.
  - Automatically selects `discharge_date` for post-discharge readmission cohorts and `admit_date` for in-hospital mortality cohorts.
  - Added configurable wash-in buffers (`washin_buffer_days`, `washin_buffer_hours`) allowing feature collection within a bounded window (e.g. first 24 hours of admission) while strictly preventing terminal resuscitative codes (CPR, palliative care, intubation) on the day of death from leaking into in-hospital mortality predictors.

### Clinical Methodology & Competing Risks
- **Fine-Gray Competing Risks Survival Helper (`prepare_competing_risks_data`)**:
  - Implemented 1:1 cross-language competing risk dataset preparation in Python (`omop_etl.cohort.prepare_competing_risks_data`) and R (`omopduckdb::prepare_competing_risks_data`).
  - Standardizes 3-state survival outcome formatting for consortium sites:
    - Status 0: Censored alive without readmission within follow-up window.
    - Status 1: Primary event of interest (acute inpatient readmission).
    - Status 2: Competing terminal event (death prior to readmission).
  - Emits `time_days` to first event or censoring, `status` (0, 1, 2), and `composite_event` (Mode 4 readmit-or-death) to eliminate survivor bias in standard readmission models.

### Multi-Site Ingestion & PCORnet Mapping Parity
- **PCORnet Discharge Status Mapping Expansion**:
  - Updated PCORnet ETL across Python (`load_visit_occurrence`) and R (`etl_pcornet`) to comprehensively map standard 2-letter codes:
    - `'EX'`, `'EXPIRED'`, `'DIED'`, `'DECEASED'` -> `4216643` (Died)
    - `'AM'`, `'AMA'` -> `4155309` (Against medical advice)
    - `'HO'`, `'HOSPICE'` -> `8546` (Hospice)
    - `'SN'`, `'SNF'` -> `8863` (Skilled Nursing Facility)
    - `'A'`, `'HOME'`, `'ROUTINE'` -> `8536` (Home)
- **Proactive Vocabulary & Mapping Checks**:
  - Embedded proactive checks in `load_visit_occurrence` and `check_vocabulary_version` to alert consortium sites if inpatient visits have unmapped discharge dispositions (`discharged_to_concept_id = 0`).

### Repository & Developer Workflow
- **`renv` Autoloader Documentation**:
  - Documented `renv` activation behavior in `README.md` and `README.Rmd`, guiding consortium developers to run `renv::restore()` or set `RENV_CONFIG_AUTOLOADER = FALSE` in `.Renviron` when using system-installed R packages.

# omopduckdb 0.5.1

### Bug Fixes & CI/CD Stability
- **DuckDB Extension Autoloading**: Replaced dynamic `CURRENT_DATE` queries in cohort SQL with injected ISO date literals (`DATE 'YYYY-MM-DD'`), eliminating unexpected ICU extension autoloading failures on Linux runners.
- **Pkgdown Documentation**: Added all exported reference topics to `_pkgdown.yml`, categorized into logical sections across cohort definitions, ML feature extraction, HADES/tidymodels bridges, and DQD.
- **Docker Builder Dependencies**: Added missing suggested packages (`Matrix`, `arrow`, `jsonlite`, `dplyr`, `withr`) to the builder image.
- **PyPI Release Workflow**: Configured `skip-existing: true` in `pypa/gh-action-pypi-publish` action to handle idempotent publishes gracefully.

# omopduckdb 0.5.0

### End of Life & Mortality Cohort Suite
- **Standardized End of Life & Mortality Cohort Builder (`build_end_of_life_cohort`, alias `build_mortality_cohort`)**:
  - Implemented 1:1 cross-language mortality cohort generation in Python (`omop_etl.cohort`) and R (`omopduckdb::build_end_of_life_cohort`).
  - Supports four clinical mortality paradigms:
    - `"in_hospital"`: Ascertains in-hospital mortality during the index stay via `death` table or discharge disposition (`discharged_to_concept_id IN (4216643, 4155309)`).
    - `"post_discharge"`: Post-discharge mortality within `(t_discharge + gap_days, t_discharge + mortality_window_days]`, strictly excluding index stays resulting in death.
    - `"fixed_window"`: SARD EOL prediction protocol (Kodialam et al., AAAI 2021) within `(t_index + gap_days, t_index + mortality_window_days]`.
    - `"composite_readmit_or_death"`: Clinical composite of 30-day all-cause readmission or post-discharge mortality, capturing unfavorable discharge outcomes for eDERRI benchmarks and competing-risk adjustments.
  - **Dual Death Source Ascertainment**: Simultaneously queries both the OMOP `death` table (`death_date`) and `visit_occurrence` discharge status (`discharged_to_concept_id IN (4216643, 4155309)`).
  - **Right-Censoring Verification (`require_verified_followup`)**: Ensures non-deceased patients have confirmed observation or clinical contact at or beyond the follow-up window, preventing lost-to-follow-up bias.
  - **Standardized Cohort Table Materialization**: Automatically materializes target cohort (e.g. ID 1) and optional outcome cohort (e.g. ID 2) in OMOP `cohort` and `cohort_definition` tables with reproducible sampling (`"random"`, `"first"`, `"last"`).
  - Registered SQL macro `is_expired(discharged_to_concept_id)` in `inst/sql/cohort_mortality.sql`.

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
