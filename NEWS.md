# omopduckdb (development)

- **The cohort definitions ship with the package (`cohorts/` moved to `inst/cohorts/`).** The Circe JSON and OHDSI SQL files were in a top-level folder that an installed R package does not contain, so the tests that read them (the committed Circe definitions, `cohort_definition_set()`, the cohort comparison) were skipped in R CMD check and in the coverage run, and the compiler code they exercise looked uncovered. They are now found everywhere: `cohort_definitions_dir()` (R and Python) returns the folder, `cohort_definition_set()` uses it by default, and the wheel bundles it. **Path change for scripts that referenced `cohorts/...` in the repository: it is now `inst/cohorts/...`.**
- **Does a feature group help, and are two model runs really different?** `ablate_feature_groups()` (Python and R) refits a model with each named group of columns removed (or kept alone) on the same cross-validation folds and reports the change in AUROC, AUPRC and Brier score with a paired bootstrap interval, so a group such as RUCA/SDI or a site's Charlson index is judged against noise rather than by eye. `compare_model_runs()` compares two sets of predictions for the same patients (for example 0.706 against 0.710): the metrics of each, the paired differences with bootstrap intervals, the Spearman correlation of the two risk scores and how much of the top-risk group both flag. The model is any `fit_predict(x_train, y_train, x_test)` function (scikit-learn, xgboost, a PLP model); the default is a small ridge logistic regression, `ridge_logistic_fit_predict()`, that needs only numpy / base R. Both return aggregate tables only, so a site can send the result back; a 0.004 AUROC gap whose interval spans zero is reported as such.
- **Reconcile a reference patient list with a cohort, and say why patients are missing.** For a study that replaces a legacy extraction: `reconcile_cohort()` (Python and R) compares any list of identifiers (`person_source_value` or `person_id`, optionally with an index date) with a cohort table or a Circe definition and returns the patient-level overlap (`both`, `reference_only`, `cohort_only`, Jaccard) and how many matched patients share the index date. `explain_cohort_gaps()` gives every reference patient the first step that excludes them: not in the CDM, no observation period, no entry event, failed an entry correlated criterion, entry outside the observation window, or a named inclusion rule. It walks the Circe definition with the native compiler, whose `compile_circe_to_duckdb(stage = )` now returns the events of an intermediate step. Reports are counts by reason; patient-level rows are returned only with `include_person_rows`, and `min_cell` / `suppress_small_cells()` blank small counts so a report can leave a site. Nothing is specific to one study.
- **Public BigQuery demonstration / smoke test on CMS SynPUF.** `run_bigquery_demo()` (R, `inst/scripts/run_bigquery_demo.R`) and `python -m omop_etl.demo_bigquery` (`omop-bigquery-demo`) build `cohorts/diabetes_phenotype.circe.json` and baseline features (age, sex, race, `prior_ip_os_ed_count`, `readmission_count_twelve_months`) on `bigquery-public-data.cms_synthetic_patient_data_omop` in a scratch dataset of the caller's own project and write a report with the synthetic-claims disclaimer. R compiles with CirceR + SqlRender (`compile_cohort_bigquery()`); Python transpiles the native compiler's SQL to GoogleSQL with sqlglot. Labs and vital signs are reported as unavailable, never imputed. `--dry-run` writes the SQL without any BigQuery call. Offline tests check the generated GoogleSQL parses; a live test runs only when `OMOP_BQ_BILLING_PROJECT` is set. Not yet run against live BigQuery by the author of this change; see `vignettes/bigquery_synthetic_demo.Rmd`.

- **Fix: R sessions crashed intermittently (segfault) in the Windows R CMD check.** The cause was `omop_connect_lakehouse(format = "delta")` (and the Python `omop_connect_lakehouse`): it ran `INSTALL delta; LOAD delta` even for a plain parquet directory, and a failed extension install or load leaves the DuckDB R connection in a state that segfaults on disconnect or garbage collection (reproduced locally: a failed `LOAD delta` crashed 5 of 8 fresh R sessions; a bad SQL statement never did). The extension is now loaded only for a table that is a Delta (`_delta_log`) or Iceberg (`metadata`) table or a remote URI; a plain parquet directory is read with `read_parquet()` whatever `format` says. The crashing test file now completes 6 of 6 passes of the suspect test files where it crashed on the second before. The test run also prints each test as it starts (`tests/testthat.R`), so a future hard crash names the test. On a machine without internet access, install the extension beforehand (`INSTALL delta`).
- **The index-stay cohort (`cohorts/target_cohort.circe.json`) is now a real Circe definition, and the native compilers evaluate it.** `omop_etl.circe_definitions.target_cohort_circe()` builds it: the discharge date is the index date (`DateAdjustment` on the end date), the age is taken at discharge, a death on or before discharge disqualifies the stay, and these rules are correlated criteria of the entry event, so the primary limit picks the earliest stay that qualifies (inclusion rules would drop a patient whose first stay failed). CirceR + CohortGenerator and the native compilers (Python and R) return the same cohort on a fixture with the awkward cases (`scripts/ohdsi_parity_target_cohort.R`). It differs from `cohorts/target_cohort.sql`, which stays authoritative, in two documented cases that Circe cannot express: a patient discharged as died (`discharged_to_concept_id` 4216643) without a death record, and a death within 30 days of discharge when the observation period ends at death. The native compilers now evaluate `DateAdjustment`, the `Age` attribute and `CorrelatedCriteria` on an entry event, and `Death` as a domain; inclusion-rule criteria with `CorrelatedCriteria` are still refused.
- **DQD catalog for R.** `run_dqd_catalog()` and `run_dqd_full()` (R) run the same schema-driven catalog as the Python `run_dqd_catalog`, with the same check names, thresholds and result shape, and the same tests.
- **Custom concept ids stay aligned across sites.** A custom feature's concept id is assigned per database, so two sites can give the same feature different ids, or one id to different features; a model that stores the id then scores the wrong column at the other site without an error. `export_custom_concept_registry()` writes a site's registry as JSON, `register_custom_concepts(registry=)` and `write_custom_features(registry=)` pin the ids a consortium shares, and `check_custom_concepts()` lists `name_different_id`, `id_different_name`, `id_collision` (another vocabulary uses the id), `duplicate_id` and `below_local_range` conflicts against other sites' registries (`raise_on_conflict` / `error = TRUE` stops on them). `write_custom_covariates()` now refuses an id that a concept of another vocabulary already uses, and two features sharing one id, before writing anything. Python and R.
- **DQD catalog: the missing concept-class, value and completeness checks.** `run_dqd_catalog()` adds, under the official check names, `fkClass` (`drug_era.drug_concept_id` and `drug_strength.ingredient_concept_id` are Ingredients), `plausibleValueLow` / `plausibleValueHigh` (birth year, `days_supply`, `quantity`, and the bundled physiologic limits of `measurement.value_as_number` for rows whose unit matches a limit), `sourceConceptRecordCompleteness`, `measurePersonCompleteness` (clinical tables with no row for a person), `measureConditionEraCompleteness` and `measureObservationPeriodOverlap`. Still not run: the per-concept checks (`plausibleGender`, `plausibleUnitConceptIds`, ...).
- **Fix: the native Circe compiler silently returned wrong cohorts for definitions it did not understand.** Running real PhenotypeLibrary definitions through CirceR + CohortGenerator and the native compiler on the same data showed: it applied the inclusion rules before the primary-criteria limit (so a person whose first entry failed a rule could still enter later; "Type 2 diabetes indexed on diagnosis, treatment or lab result" gave 361 people instead of 231); it ignored `AdditionalCriteria`, criterion attributes it does not evaluate (`Unit`, `CorrelatedCriteria`, `ProcedureSourceConcept`, `First`, ...), `includeMapped`, nested groups and `EndWindow`; it had no `Observation` or `DeviceExposure` domain, so an `Observation` entry event fell back to "every visit" (1,887 people instead of 0); it turned an unknown operator such as `bt` into `=`; it did not require the entry event to fall in an observation period. It now follows the ATLAS order (observation window, primary limit, rules, end strategy, era collapse, `ExpressionLimit`), applies `DateOffset` end strategies, the default end of observation and era collapse with `EraPad`, supports `bt`/`!bt`/`!eq` and the `Observation` and `DeviceExposure` domains, ends an event without an end date the day after it starts, and **raises `NotImplementedError` (R: an error) naming the construct** for anything that would change who is in the cohort, instead of ignoring it. `CustomEra` and censoring criteria only change end dates and raise a warning. Of the 707 PhenotypeLibrary definitions, 346 are accepted; on 80 sampled ones the native compiler returned the same people as CirceR + CohortGenerator in 80 and the same start dates in 79 (the other uses `CustomEra`). `scripts/ohdsi_parity_phenotypelibrary.R` repeats the check. **Behaviour change:** entry events must lie in an observation period, so a CDM without `observation_period` rows now returns empty cohorts; `cohort_end_date` follows the end strategy (it was the event end date).
- **Add your own covariates to FeatureExtraction.** `write_custom_covariates()` (Python and R) writes any derived feature as `measurement` rows under a site-defined concept id (>= 2,000,000,000; `unit_concept_id` 0; registered in `concept`), and `custom_covariate_settings()` (R) builds the FeatureExtraction settings that read them. `write_custom_features()` takes a wide table (one column per feature) and gives each feature a stable named concept in the database (`register_custom_concepts()`), so a study adds its own Charlson, Elixhauser or frailty definition next to FeatureExtraction's and reads both through the same covariate settings (PatientLevelPrediction and DeepPatientLevelPrediction consume the same covariate data). The package does not choose between definitions. RUCA/SDI (`write_geography_measurements()`) are now just one use of it; an alternative definition of an existing feature can be written under its own concept id and compared with the standard one before a study chooses.
- **DQD: concept domain and standard/valid checks.** `run_dqd_catalog()` adds `fkDomain` (the concept of a clinical field is in the expected domain) and `isStandardValidConcept` (the concept is standard and not deprecated), both with the official DQD check names; they run when `concept` is populated. `strict_domains=TRUE` stops measurement and observation fields from accepting each other's domain. Still not run: `fkClass`, plausible-value and gap checks, `sourceConceptRecordCompleteness`.
- **Phenotype decisions made explicit.** Gestational diabetes (SNOMED 4024659) is a descendant of "Diabetes mellitus" (201820), so a plain descendant set admitted pregnancy-only diabetes, which the SAS extraction's ICD rule (249, 250, E08-E13) does not; `diabetes_phenotype_circe(include_gestational=False)` now subtracts it (`include_gestational=True` keeps it; a person with a diabetes drug or HbA1c >= 6.5 still enters through those arms). Whether outpatient visits (9202) count in `prior_ip_os_ed_count` is a study choice: `extract_velocity_features(os_concepts=...)` takes it (default `9202, 581385`; `os_concepts=581385` counts observation stays only), now covered by a test.
- **Checked against more official tools** (`OHDSI_INTEGRATION.md`): CohortDiagnostics and CohortGenerator report the same cohort counts as `execute_circe_cohort()`; PatientLevelPrediction's study population and outcome count equal a direct SQL count, its `getPlpData()` returns a custom covariate written with `write_custom_covariates()`, and `runPlp` finishes on the DuckDB-backed data (`scripts/ohdsi_parity_plp_cohortdiagnostics.R`).
- **RUCA / SDI as OHDSI covariates.** `write_geography_measurements()` (Python and R) stores the values from `derive_geography_features()` as `measurement` rows (SDI score, SDI quintile 1-5, RUCA primary code; local concept ids 2000100001-3, `unit_concept_id` 0, registered in `concept`), so FeatureExtraction and PatientLevelPrediction read them as ordinary covariates. `derive_geography_features()` now also returns `admit_date`.
- **Checked against the official OHDSI tools** (CirceR + CohortGenerator, FeatureExtraction, Achilles 1.7.2, DataQualityDashboard); see `OHDSI_INTEGRATION.md`. Fixed: `diabetes_phenotype.circe.json` was not valid Circe (age is now in `DemographicCriteriaList`, which the native compiler also reads); `run_achilles()` distribution statistics now use the sample standard deviation (NULL for one value) and discrete percentiles like Achilles; the DQD catalog counts every row of a duplicated primary key like DQD and checks end dates and datetime fields in `plausibleAfterBirth`/`plausibleStartBeforeEnd`; catalog results carry `check_origin` (`plausibleFutureDate` and `visitPersonMatch` are not DQD checks). Not equivalent: `extract_charlson_index()` and FeatureExtraction's Charlson.
- **Study logic as Circe definitions.** `cohorts/metformin_only_prediabetes_pcos_{icd10,snomed,both}.circe.json` (patients with prediabetes or PCOS whose only diabetes drug is metformin and who never had a diabetes diagnosis) and `cohorts/diabetes_phenotype.circe.json` (adult with a diabetes diagnosis, a diabetes drug or HbA1c >= 6.5, the SAS extraction's rule) are standard ATLAS/Circe JSON, so they import into ATLAS, run through CohortGenerator, and compile to DuckDB with the package's Java-free compiler. `omop_etl.circe_definitions` builds them (`write_circe_definitions()`); a test fails if the committed files drift. Tests show the compiled cohorts agree with `find_metformin_only_prediabetes_pcos()` on data whose codes are mapped to standard concepts. Circe sees standard concept ids only, so records that carry just a source value are invisible to it; that is the normal OHDSI contract.
- **Fix: the native Circe compiler.** Excluded concept-set items (`isExcluded`) were skipped instead of subtracted, so a set such as "ATC A10 without A10BA" still contained the biguanides; they are now removed with their descendants, and the concept itself is always part of an `includeDescendants` item. A measurement entry event's `ValueAsNumber` filter (HbA1c >= 6.5) was ignored for primary criteria and is now applied. Python and R.
- **Fixed-level categorical encoding.** New `encode_categoricals()` / `encoded_column_names()` (Python and R) one-hot encode model features from a level list shipped with the model (`feature_spec["categorical_levels"]`: levels plus an optional reference level), so a site always gets the same columns in the same order, named like pandas `get_dummies` (`<feature>_<level>`). `get_dummies(drop_first=True)` takes its columns and reference level from the data in hand, and `pd.to_numeric` silently turns string features into NaN; neither is safe for a model that must score at another site.
- **Fix (ETL): PCORnet `DISCHARGE_STATUS = 'HO'` is home / self-care, not hospice.** It was mapped to hospice (8546), so home discharges were mislabelled; `HO` now maps to Home (8536) and hospice is `HS`. Re-run the ETL (or `UPDATE visit_occurrence`) on existing databases.
- **Behaviour change (ETL): observation stays are no longer inpatient.** PCORnet `ENC_TYPE = 'OS'` was mapped to 9201 (inpatient), so observation stays could become index admissions and readmissions; it now maps to 581385 (Observation Room), which `extract_prior_utilization()` counts in its `os` group (still prior utilization, never an index stay). A PCORnet discharge to another acute-care hospital (`DISCHARGE_STATUS = 'IP'`) now maps to 8717 (Inpatient Hospital) instead of 0. Verify both concept ids in your Athena release. Existing databases need the ETL re-run (or an `UPDATE visit_occurrence`); cohorts built on the old mapping include observation stays.
- **Fix (ETL): AMA discharge concept.** PCORnet `DISCHARGE_STATUS = 'AM'` was written as 4155309, which is SNOMED "Ileal part" (an anatomic site; confirmed in Athena), so AMA was not a valid disposition and the legacy end-of-life convention `(4216643, 4155309)` counted AMA stays as deaths. The ETL now writes 32216 ("Left against medical advice or discontinued care", UB04 Pt dis status 07). The legacy convention is unchanged because golden files pin it; on databases built before this fix pass `death_discharge_concept_ids=(4216643,)`. `inst/sql/cohort_mortality.sql` no longer calls 4155309 "Hospice" (hospice is 8546). Concepts 8717 (Inpatient Hospital) and 581385 (Observation Room) were confirmed as valid standard Visit concepts.
- **Fix: diabetes phenotype HbA1c set.** `define_study_cohort(require_diabetes=TRUE)` / `build_ederri_diabetes_cohort()` matched only three HbA1c concepts (LOINC 4548-4, 17856-6, 59261-8), so patients qualifying on the other HbA1c codes of the SAS extraction were missed. It now covers all of them (LOINC 17855-8, 41995-2, 55454-3, 62388-4, 71875-9, 4549-2 added; the SAS list's `549-2` is a typo for 4549-2 and does not exist in the vocabulary). Still open: the medication arm only matches a short list of drug names in `drug_source_value`, not the RxNorm list of the extraction or an ATC A10 roll-up, and 41995-2 is a mass/volume unit (g/dL) so `>= 6.5` rarely applies to it. The PCORnet ETL also maps `NH` -> 8676, `HH` -> 32215 and `RH` -> 32228 now (they were 0).
- **Fix (ETL): PCORnet `ENC_TYPE = 'EI'` (emergency-to-inpatient) was dropped.** It mapped to 0, but at three of five consortium sites EI is the larger share of hospitalizations (955k EI vs 707k IP stays), so an ETL run lost over half of the inpatient stays there. EI now maps to 262 (Emergency Room and Inpatient Visit). `build_ederri_diabetes_cohort()` uses `visit_type=[9201, 262]` (IP and EI) and the phenotype's ICD-10 prefix list now includes E12, as in the SAS extraction. The generic `visit_type="inpatient"` still means 9201 only (the RFC example is pinned); pass `[9201, 262]` for IP + EI, and `outcome_visit_type=[9201, 262]` so an EI stay counts as a readmission. `IS` (non-acute institutional) stays unmapped on purpose.
- **Comorbidity window: `end_inclusive`.** `extract_elixhauser_comorbidities()` and `extract_charlson_index()` (Python and R) take `end_inclusive=` (default `FALSE`) to count diagnoses dated ON the index date. A discharge-anchored window (`index_date_col` = the discharge date, `exclude_index_diagnoses=FALSE`) was already possible but stopped one day short, so diagnoses coded on the discharge day were missed; with `end_inclusive=TRUE` and `lookback_days=730` it reproduces the legacy study's rule (`condition_start_date` in `[discharge - 730, discharge]`, index stay included). The defaults are unchanged: admission anchor, window ending strictly before the index date, index-visit diagnoses excluded.
- **Behaviour change: prior utilization counts EI (262) as inpatient only.** `DEFAULT_VISIT_CONCEPT_GROUPS` (`extract_prior_utilization()`) and `extract_velocity_features()` counted concept 262 (emergency-to-inpatient) as both an inpatient and an ED visit. The legacy study relabelled PCORnet `EI` as `IP`, so an earlier EI stay now increments `prior_ip_count` and `prior_ip_os_ed_count` but not `prior_ed_count` (defaults `ed = 9203`). `extract_velocity_features()` also counts observation stays (581385) in `prior_ip_os_ed_count`; before, only 9202 (outpatient) was in its third group, so observation was missed. Outpatient (9202) is still in that group, unlike the legacy definition (inpatient + observation + ED only); pass `os_concepts=581385` to match it. Pass `visit_concept_groups=` / `ed_concepts=` to restore the old counts.
- **Metformin-only prediabetes / PCOS exclusion.** New `find_metformin_only_prediabetes_pcos()` (Python `omop_etl.exclusions`, R) returns the patients with prediabetes or PCOS whose only diabetes drug is metformin and who have no diabetes diagnosis (all other diabetes drugs, including metformin combinations, and any diabetes diagnosis keep a patient in). `define_study_cohort(exclude_metformin_only_prediabetes_pcos=)` / `build_ederri_diabetes_cohort()` drop them as a patient-level attrition step. The PCOS concept set is a parameter (`"icd10"` = ICD-10 E28.2 / SNOMED "Polycystic ovary syndrome", `"snomed"` = SNOMED 69878008 / "Polycystic ovary", `"both"`) because the legacy file's definition is not known; validate each against the per-site legacy file. Needs the vocabulary (`concept_ancestor`) for descendants; without it only the listed ids and source-value patterns match, with a warning.
- **Diabetes phenotype: medication rule.** `define_study_cohort(diabetes_drug_rule=)` chooses the medication arm of `require_diabetes`: `"names"` (default, unchanged: ten drug names in `drug_source_value`), `"atc_a10"` (concepts descending from ATC A10, which brings in brands and combinations) or `"rxnorm_cuis"` with `diabetes_rxnorm_cuis=` (a list or a file of RxNorm CUIs, e.g. the SAS extraction's). The last two need the vocabulary on the connection and raise an error without it.
- **Transfer exclusion.** `define_study_cohort(transfer_discharge_concept_ids=)` (Python and R) excludes index stays discharged to another acute-care hospital and adds the attrition step "Not transferred to another acute-care hospital"; AMA discharges are kept. Off by default, so existing attrition tables (including the RFC example) are unchanged; `build_ederri_diabetes_cohort()` turns it on with 8717.
- **Geography features (RUCA, SDI).** New `derive_geography_features()` (Python `omop_etl.geography`, R `omopduckdb`) derives a RUCA category and SDI quintile for each index stay from the address in effect at admission, a USDA ZIP -> RUCA crosswalk and a Robert Graham Center ZCTA -> SDI table that the caller supplies. SDI quintiles use the **training cohort's fixed cut points** (`sdi_cut_points`, four ascending scores): cutting at local quantiles moves patients between quintiles whenever the local SDI distribution differs, so the function errors without cut points unless `fit_cut_points=True` (training site only; the fitted cuts come back in `result.attrs["sdi_cut_points"]` / `attr(, "sdi_cut_points")` for publication with the model). `load_location()` now also fills `person_address_history` (person, ZIP5, start, end, preferred) so the address in effect can be chosen; `person.location_id` is still a single address and is the fallback. RUCA 99 ("not coded") is Small Town / Rural, as in the reference coding; `code_99_missing=True` treats it as unknown. The reference cut-point values and the ZIP crosswalks are not bundled. `load_location()` also repairs address rows whose `ADDRESS_CITY` holds an unquoted comma ("St. Louis, MO"); before, such rows silently shifted state, ZIP and dates into the wrong columns.
- **BigQuery sync.** `read_bigquery_table()` now streams into a staging table inside one transaction and swaps it in, so a failed stream no longer truncates an existing table; it refuses a view target and resolves the existing table in the current schema only. `skip_missing=True` skips only not-found tables, not validation errors. The reader passes a BigQuery Storage client when `google-cloud-bigquery-storage` is installed. Still unit-tested only; not run against a live BigQuery project.
- **Achilles / DQD catalog.** `run_achilles(drop_existing=False)` replaces an analysis' earlier rows instead of appending. Both Achilles and the DQD catalog read columns from the current schema only. `min_cell_count` removes small cells rather than masking them as the R package does. DQD gains `visitPersonMatch` (a row's `visit_occurrence_id` belongs to the same person) and uses the CDM key for `death` and the vocabulary tables. `plausibleFutureDate` flags `observation_period` ends set to the build date; that is expected.
- **Federated learning.** `noise_scale` now also blurs the Cox event counts and the logistic log-likelihood; row and event totals stay exact bookkeeping. A noisy non-positive Cox risk-set sum raises. Logistic fits use step-halving and warn on non-convergence, naming likely (quasi-)separation. Cox covariance and log-likelihood come from the final coefficients; `federated_cox(min_site_events=)` added.
- **Observation-period cutoff (N2).** `etl_pcornet(data_cutoff=)` / `omop-duckdb build --data-cutoff` cap `observation_period` ends at an ISO date, or at the latest event dated before today with `"auto"`, so events filled with the build date no longer set period ends. The default is unchanged (today). The R ETL has the same `data_cutoff` argument, and `build_observation_period(max_date = "auto")` works there too.
- **R BigQuery loader.** `load_from_bigquery()` passed `billing_project=` to `bigrquery::bq_table_download()`, which only accepts `billing=`, so every call failed with "unused argument". Fixed, and 64-bit ids are now downloaded as `integer64`. New mocked test (no GCP credentials). The R loader still materializes each table in memory; use `sync_vocabulary_from_bigquery()` for full vocabularies.
- **HADES read-only wrapper (N5).** New `readonly_connect_wrapper()` holds the `DatabaseConnector::connect` wrapper that `cohorts/run_plp_readonly_example.R` used inline; it is tested against a real read-only DuckDB file. Docs: `cohorts/OMOP_COHORT_DEFINITION.md` now says the SQL is authoritative and the Circe JSON is not generation-verified in ATLAS (FR-D5), and explains how the follow-up rule behaves with enrolment-based periods (N3). The README now states that the BigQuery sync and Oracle translation are unit-tested only.
- **Oracle cohort SQL.** Constructs sqlglot cannot express in Oracle raise instead of emitting approximate SQL. Still not run against a real Oracle database.
- **ATLAS scripts.** Compose ports bind to 127.0.0.1, the unused DuckDB JDBC mount is gone, and `import_circe.py --cleanup` deletes the definitions it creates.

# omopduckdb 0.8.7

- **Python: Oracle cohort SQL.** New `omop_etl.cohort_sql.translate_cohort_sql(sql, dialect)`; for Oracle it rewrites `DATEADD` (day/week to `d + n`, month/year to `ADD_MONTHS`) and `YEAR()` to `EXTRACT(YEAR FROM ...)`, which plain sqlglot gets wrong. The live-db cohort test now includes Oracle (not yet run against a real Oracle container; unit-tested on the generated SQL only).
- **Python: Achilles in DuckDB.** `run_achilles()` / `omop-duckdb achilles` write `achilles_results`, `achilles_results_dist` and `achilles_analysis` with one set-based statement per analysis (demographics 0-5, observation periods 101/103/105/108/111-113, persons / records / by-month for the visit, condition, procedure, drug, observation, measurement and device domains, death, eras). This is a subset of the ~170 R-package analyses; `achilles_analysis_catalog()` lists exactly what is implemented.
- **Python: DQD catalog.** `run_dqd_catalog()` / `run_dqd_full()` / `omop-duckdb dqd --full` generate checks from the CDM DDL for every table and field (table/field existence, required, primary key, foreign key, value completeness, unmapped concepts, start-before-end, after-birth, before-death, future date) in DQD JSON shape. Concept-level checks that need vocabulary content are not included.
- **Python: iterative federated learning.** `federated_logistic_regression()` and `federated_cox()` (Breslow ties) run Newton-Raphson over per-site aggregates; results equal the pooled fit (tested against statsmodels). Optional `time_bin` and `noise_scale` reduce what aggregates reveal; they are not a formal differential-privacy guarantee.
- **Python: BigQuery to DuckDB.** `sync_vocabulary_from_bigquery()` / `read_bigquery_table()` stream Arrow batches into DuckDB with no CSV or disk staging (`pip install omop-duck-db[bigquery]`). Unit-tested with a fake Arrow reader and client; not run against a live BigQuery project.
- **HADES integration test.** `tests/testthat/test-cohortgenerator-integration.R` runs the real `CohortGenerator::generateCohortSet()` on `cohort_definition_set()` against a DuckDB CDM through DatabaseConnector (target cohort = persons 1 and 5, outcome events = 6, the same as the Python / PostgreSQL / SQL Server runs). Optional CI: `.github/workflows/hades-integration.yml`.
- **ATLAS.** `scripts/atlas/` holds a docker-compose (PostgreSQL 16, WebAPI 2.15, ATLAS 2.15 on non-default ports) and `import_circe.py`, which imports `cohorts/*.circe.json`, reads them back and has WebAPI render SQL. Verified on WebAPI 2.14.0 and 2.15.x: both definitions import, round-trip and render. **Not possible today:** generating cohorts inside ATLAS on a DuckDB CDM. WebAPI 2.14 and 2.15 reject `duckdb` as a source dialect ("Unsupported data source dialect: duckdb", even with the DuckDB JDBC driver mounted); WebAPI 3.0.0 did not start with this compose configuration and was not pursued. `make_cdm.py` writes the fixture CDM for when a WebAPI with DuckDB support is available; use `CohortGenerator` (see the HADES integration test) to run the definitions on DuckDB meanwhile.

# omopduckdb 0.8.6

- **Fix (CLI):** `omop-duckdb build` takes `--key-check`, `--use-enrollment` and `--enrollment-type-concept-id`; `run_manifest.json` records the `key_check:*` collision steps and `observation_period:enrollment_spans`.
- **Fix (R tests):** the `callr` child in the Andromeda test sets `andromedaThreads = 1` / `andromedaMemoryLimit = 0.5`.
- **Read-only distribution:** new `checkpoint_database()` (Python, R) and `omop-duckdb checkpoint FILE...` fold the `.wal` into the file; `attach_read_only()` / `readonly_attach_sql()` warn when a `.wal` is present. `cohort_definition_set()` docs and the example scripts state that `generateStats = FALSE` is required.
- **Fix (R tests):** removed `skip_on_os("windows")` from the three Andromeda tests in `test-v056-features.R` now that the `callr` child is constrained; re-add it if the Windows runner still aborts.
- **Docs:** Oracle caveat for sqlglot `DATEADD` in `translate_to_duckdb()` and `target_cohort.sql`.
- **Versions aligned** at 0.8.6 across `DESCRIPTION`, `pyproject.toml` and `omop_etl.__version__`.

- **Fix: `FeatureMatrixBuilder.to_tensor()`** falls back to the NumPy array when torch is installed but its DLL fails to initialise on Windows (`OSError`, not just `ImportError`).
- **Tests: no more cwd-dependent paths** in `test_v080_features.py` (resolved from the repo root).
- **Tests/CI (R, Windows): Andromeda teardown.** `test-hades-bridge.R` closes every `plpData$covariateData`; `helper-hades.R` sets `andromedaThreads = 1` / `andromedaMemoryLimit = 0.5` (GB); `duckdb_to_andromeda()` flushes after chunked appends. Targets the silent `R CMD check` exit on 7 GB runners.
- **Docs:** hand-written cohort SQL must be run with `CohortGenerator::generateCohortSet(..., generateStats = FALSE)`; read-only DuckDB files must be checkpointed (no `.wal`) before `ATTACH ... (READ_ONLY)`.
- **Build-time key check (R)**: `check_key_collisions()` / `check_etl_keys()` and `etl_pcornet(key_check = "warn"|"raise"|"ignore", key_check_callback = )`, ported from `omop_etl.key_checks`.
- **Enrolment-based observation periods (R)**: `etl_pcornet(use_enrollment = "auto"|TRUE|FALSE, enrollment_type_concept_id = )`, new `load_enrollment_spans()`, and `build_observation_period(use_enrollment=, enrollment_type_concept_id=)`; spans are clipped, merged when they overlap or touch, and people without enrolment keep the event-span period.
- **Cohort comparison (R)**: `compare_cohort_definitions()` and `run_cohort_sql()` (SqlRender render/translate/split on DuckDB TEMP tables), ported from `omop_etl.cohort_sql`.
- **Sequence tensors (R)**: `extract_longitudinal_tensors(include_index_stay=, include_labs=)` returns an `(N x 20 x 30)` integer array with the same DuckDB-hash tokens as Python; constants `MAX_LONGITUDINAL_VISITS`, `MAX_TOKENS_PER_VISIT`, `VOCAB_SIZE`. All four 0.8.5 Python-only features now have R equivalents.
# omopduckdb 0.8.5

Remaining requests from the 2026-10 consortium notes (FR-D6, FR-D7, FR-D8, FR-B5, FR-A7, enrolment-based observation periods).

- **FR-D7 follow-up rule option**: `cohorts/target_cohort.sql` takes `@followup_evidence` (`'observation_period'` by default, or `'observation_period_or_event'`): the second also accepts a later visit, measurement, condition or drug record dated on or after discharge + `@followup_days`, as `define_study_cohort()` does. `cohort_definition_set(followup_evidence=)` renders it. On the consortium data the two rules differ for 27 of 59,117 readmissions; sites with real enrolment windows should compare.
- **FR-D8 `compare_cohort_definitions()`** (Python): runs two target-cohort SQL files, optionally with the outcome SQL, on one CDM (TEMP table, so a read-only CDM works) and returns per-definition counts and outcome rates plus the `cohort_overlap()` table. Helpers `render_cohort_sql()`, `run_cohort_sql()` and `translate_to_duckdb()` execute the shipped SQL on DuckDB without SqlRender (needs `pip install omop-duck-db[cohorts]`, i.e. sqlglot).
- **FR-D6 cross-dialect test**: `tests/test_live_databases.py` runs the cohort SQL on PostgreSQL and SQL Server containers (translated with sqlglot, not SqlRender) and expects the DuckDB cohorts. It runs in the `live-db` workflow and passes on the PostgreSQL 16 and SQL Server 2022 service containers (target cohort and outcome cohort match the DuckDB result). Oracle is not covered (sqlglot renders `DATEADD` wrongly for it; use SqlRender).
- **Enrolment-based observation periods (Python)**: `etl_pcornet(use_enrollment="auto")` builds `observation_period` from the PCORnet ENROLLMENT file when present (clipped to `[1900-01-02, today]`, overlapping or touching spans merged, `enrollment_type_concept_id` sets the period type), and keeps the event-span rule for people with no enrolment record.
- **FR-A7 sequence tensors**: `extract_longitudinal_tensors(include_index_stay=, include_labs=)`. Lab tokens combine the concept with a low/normal/high flag from `range_low`/`range_high`. For cohorts anchored at discharge (`cohorts/target_cohort.sql`) the default window already contains the index stay; the option matters for admission-anchored cohorts and carries a leakage risk. Tokens use DuckDB `hash()` and are stable only within one DuckDB version.
- **FR-B5**: `NOTES.md` documents the one-line `ATTACH` for a shared vocabulary file and the helpers.

# omopduckdb 0.8.4

Requests FR-C10, FR-C12, FR-D4 and FR-D5 from the 2026-10 consortium notes. Not done: building `observation_period` from real enrolment data (the ETL has no PCORnet ENROLLMENT loader).

- **FR-C10 observation periods**: `build_observation_period(con, min_date = "1900-01-02", max_date = NULL)` (Python and R) ignores events before `min_date` (SAS/Excel zero dates, `1900-01-01` placeholders) or after `max_date` (default today); a person with no plausible event gets no period. Pass the source's data cutoff as `max_date` when events are filled with the build date. New DQD check `plausibleObservationPeriod` (`run_dqd(observation_period_min=)`) flags starts before the minimum, ends after the extraction date and inverted periods. Rebuild existing databases to apply the rule.
- **FR-C12 key check at build time (Python)**: `etl_pcornet(key_check="warn"|"raise"|"ignore", key_check_callback=)` runs `check_etl_keys()` on `ENCOUNTERID` (and `PATID` with `disambiguate_patids`), reports distinct ids against distinct keys and the `visit_occurrence` / `person` row count, and `omop-duckdb build` writes the results to `run_manifest.json` as `key_check:*` steps.
- **FR-D4 read-only vocabulary**: `attach_read_only()` (Python) and `readonly_attach_sql()` (R) attach the CDM and the shared vocabulary file `READ_ONLY` under the name `central_vocab`, so site files whose `concept` is a view into it work without `materialize-vocab`. `cohorts/run_plp_readonly_example.R` takes `vocab_file`.
- **FR-D5 Circe JSON**: both cohort SQL files now state they are the authoritative definition; tests check the Circe JSON's structure and consistency with the SQL. The JSON has still not been imported into ATLAS.

# omopduckdb 0.8.3

Cohort tooling follow-up to 0.8.2 (notes N4, N7, FR-D3).

- **Renamed SQL parameters (breaking for direct callers of the cohort SQL)**: `cohorts/target_cohort.sql` and `outcome_cohort.sql` now share CohortGenerator's names `@cdm_database_schema`, `@target_database_schema`, `@target_cohort_table`, `@target_cohort_id` (was `@cohort_database_schema`, `@cohort_table`, `@outcome_cohort_id`). Cohort behaviour is unchanged.
- **`cohort_definition_set()`** (R) returns a CohortGenerator-style `cohortDefinitionSet` (`cohortId`, `cohortName`, `sql`, `json`) with `@followup_days` / `@min_age` rendered. Not yet run through `CohortGenerator::generateCohortSet()`.
- **`cohorts/run_plp_readonly_example.R`**: runs the cohorts and PatientLevelPrediction on a read-only CDM (attached `READ_ONLY`, cohort rows in a scratch DuckDB). The SQL part is tested; the `DatabaseConnector::connect` wrapper is not.

# omopduckdb 0.8.2

> **Known CI gap (Windows)**: on the GitHub Windows runner the R process running the full test suite intermittently exits without a message (no testthat failure; output buffering hides where). It happened in 7 of 9 runs when tests wrote real Andromeda 1.x tables, and about 1 in 6 runs even with those tests skipped, always in the PatientLevelPrediction / FeatureExtraction / Andromeda / DuckDB tests (identical package versions in passing and failing runs; Ubuntu and a local Windows 11 machine are fine). The three tests that write Andromeda tables are skipped on Windows, so `duckdb_to_andromeda()` is **unverified on Windows CI**. Re-running the Windows `r-check` job usually passes. Cause unknown; an upstream (HADES/Andromeda/DuckDB) problem is the leading suspect.

Closes the gaps left by the 2026-10 consortium audit. Most audit items (FR-A1-A5, B1-B4, C1-C6) had already shipped in 0.7.0/0.8.x; this release adds what was missing.

- **FR-C9 (HADES)**: `hades_set_integer64()` (R) sets `databaseConnectorInteger64AsNumeric = FALSE` so 63-bit ids no longer stop `DatabaseConnector::querySql()` with "integers >= 2^53"; `plp_database_details()` and `cm_database_details()` call it when the database holds ids >= 2^53 (`integer64 = "auto"`; `hades_needs_integer64()` does the check), because forcing `integer64` for small ids crashed `getPlpData()` on Windows. New `pcornet_id53()` macro (53-bit keys, safe as R doubles) for callers who need it; the ETL still uses `pcornet_id()` and sequential person ids. `materialize_vocabulary()` / `omop-duckdb materialize-vocab` copies `central_vocab` views into real tables so a file opened alone resolves `concept`.
- **FR-D1**: portable `cohorts/target_cohort.sql`, `outcome_cohort.sql` (OHDSI SQL), Circe JSON, `cohorts/run_plp_example.R` and `cohorts/OMOP_COHORT_DEFINITION.md`. Verified on DuckDB only; see the doc.
- **FR-D2**: `sampling_rule="earliest"` (alias of `first`) and `"stable_random"` (MD5-based, version-stable); `persist_index_visits()` writes an `index_visit` table. `"random"` is unchanged (DuckDB `hash()`).
- **FR-C8**: `check_key_collisions()` compares distinct source ids with distinct surrogate keys (warn/raise).
- **FR-C7**: DQD `isPrimaryKeyDuplicate` check on `visit_occurrence_id`.
- **Fix (R): `duckdb_to_andromeda()` returned an empty Andromeda** whenever DuckDB's SQLite extension was available: it attached the Andromeda file as SQLite, but Andromeda 1.x is DuckDB-backed, so tables landed in an unrelated file. It now always writes through Andromeda's own interface in chunks (new `chunk_size`; `output_file` is deprecated and ignored). This was the `test-v056-features.R` failure in R CMD check and test-coverage.
- **FR-A6**: `extract_standard_labs(egfr="ckd_epi_2021"|"measured", anion_gap=True)`; complete `ANION_GAP_LOINCS` / `EGFR_LOINCS` sets (69405-9 and 77147-7 added to the Table 1 eGFR set); warns when a standard lab is empty.

# omopduckdb 0.8.1

- **Fix: `pcornet_id()` no longer uses DuckDB `hash()`**. Widening to 63 bits in 0.8.0 did not help: `hash()` itself collides on real encounter IDs (160 of 75,804,013 and 62 of 46,990,031 distinct raw encounters lost at two large sites; zero lost with MD5). The macro is now `(md5_number_lower(source_id::VARCHAR) & 9223372036854775807)::BIGINT`. Databases rebuilt with 0.8.0 should be rebuilt.

# omopduckdb 0.8.0

Release 0.8.0 resolves all five concrete engineering, data quality, and clinical replication gaps identified in the October 21 feature request RFC (FR-C5, FR-C6, FR-C7, FR-C8, FR-A6) and October 23 cross-site audit, enabling 120% feature-complete, autonomous, push-button replication for external consortium health systems across Python (`omop_etl`, `omop_duck_db`) and R (`omopduckdb`).

### 1. 63-Bit Signed BIGINT Surrogate Keys (FR-C8)
- **Eliminated 31-bit Integer Collision Loss**: Upgraded `pcornet_id(source_id)` macro in `inst/sql/mapping_macros.sql` from `(hash(source_id) % 2000000000)::INTEGER` to 63-bit signed BIGINT: `(hash(source_id) & 9223372036854775807)::BIGINT`.
- **DDL Widening**: Widened all 83 surrogate and foreign key identifier columns (`*_id`) in `OMOPCDM_duckdb_5.4_ddl.sql` from `integer` to `bigint` (retaining standard vocabulary concept identifiers as `integer`).
- **Multi-Dialect Export Alignment**: Updated `export_targets.py` and `export_targets.R` across all enterprise export dialects: Oracle (`NUMBER(19)`), SQL Server (`BIGINT`), Snowflake (`NUMBER(19,0)`), and BigQuery (`INT64`). Prevents silent encounter drops and cross-patient record contamination at high-volume sites (e.g. Site D / Site E).

### 2. ETL Crash Vulnerabilities Eliminated (FR-C5 & FR-C6)
- **Staged Vital Inserts**: Refactored `load_vital` in `build_omop_cdm.py` and `etl_pcornet.R` from a single massive 5-way `UNION ALL` with global sorting into 5 staged, single-analyte materializations (`HT`, `WT`, `BMI`, `SYSTOLIC`, `DIASTOLIC`). Completely eliminates memory exhaustion and DuckDB Windows access violations on multi-million-row vital files.
- **Regex-Guarded Datetime Parsing**: Re-engineered `parse_omop_datetime(d, t)` with multi-pattern format matching and `normalize_time_str(t)` to gracefully handle single-digit hours (e.g. `1:01` with `01AUG2002` -> `2002-08-01 01:01:00`) and arbitrary time variants without unhandled parse crashes.

### 3. Study Phenotyping & Dynamic Lab Gaps Resolved (FR-A6)
- **Authoritative eDERRI Diabetes Cohort Phenotype**: Added `build_ederri_diabetes_cohort()` in Python and R, and exposed `--type ederri_diabetes` / `diabetes` via `omop-duckdb cohort define`. Phenotype requires adult inpatient admission with wash-in, discharged alive, with explicit diabetes evidence:
  - ICD-9: `249`, `250`
  - ICD-10: `E08`, `E09`, `E10`, `E11`, `E13` (and standard concept descendants of `201820`)
  - Antidiabetic medications (ATC A10 / RxNorm classes: insulin, metformin, sulfonylureas, GLP-1, SGLT2)
  - $	ext{HbA}_{1c} \ge 6.5\%$ (LOINCs `4548-4`, `17856-6`, `4549-2`, concept IDs `3004410`, `3005673`, `40762352`).
- **Race-Free CKD-EPI 2021 Dynamic eGFR Equation**: Integrated `compute_ckd_epi_2021(creatinine, age, is_female)` into `extract_velocity_features` in Python and R. Dynamically evaluates eGFR whenever missing or capped at clinical reporting thresholds ($\ge 60	ext{ mL/min}$), eliminating cross-site missingness.
- **Expanded Anion Gap LOINC Set & Stoichiometric Fallback**: Expanded anion gap LOINCs in `table1.py` / `table1.R` to include `41276-7` alongside `1863-0` and `33037-3`. Added dynamic stoichiometric fallback `	ext{Na} - (	ext{Cl} + 	ext{HCO}_3)` to `extract_velocity_features`.

### 4. DataQualityDashboard (DQD) Person-Visit Consistency Check (FR-C7)
- **Foreign Key Person-Visit Integrity**: Implemented `fkPersonVisitConsistency` check (`FIELD_FK_PERSON_VISIT_<TABLE>`) in native `run_dqd()` across Python (`dqd.py`) and R (`dqd.R`). Cross-verifies that `clinical_table.person_id == visit_occurrence.person_id` across `condition_occurrence`, `procedure_occurrence`, `drug_exposure`, `measurement`, `observation`, and `device_exposure`, detecting and reporting mismatches before external validation.

### 5. Deep Learning Sequence Models & Tri-Model Ensemble Blend Support
- **Longitudinal Sequence Tensors**: Added `extract_longitudinal_tensors()` in `omop_etl.tensor` to build 3D $(N 	imes 20 	imes 30)$ concept token tensors directly in DuckDB from historical inpatient encounters.
- **Model Bundle Support for Blackwell Deep Learning**: Updated `model_bundle.py` to support `.pt` PyTorch model weights and TorchScript bundles under `deep_residual` and `torchscript`. Added automatic 3D tensor extraction and dispatch in `ModelBundle.predict_proba` and `omop-duckdb evaluate` when components require `input: "sequence_tensor"`.


- **`omop-duckdb evaluate`** (Python): one command chains `check_source`, cohort, `extract_velocity_features`, `validate_feature_frame`, `score_cohort`, `site_telemetry` and a run manifest; only the aggregate packet and manifest are written (`omop_etl.evaluate.evaluate_site()`).
- **Strict JSON**: every exchanged JSON file (feature specs, bundles, manifests, telemetry, DQD and integrity reports) now writes `null` instead of `NaN` / `Infinity`, in Python (`omop_etl.jsonio`) and R (`na = "null"`); specs that already contain `NaN` tokens are read as `null`.
- **Live-database CI** (`.github/workflows/live-db.yml`, `tests/test_live_databases.py`): PostgreSQL 16, SQL Server 2022 and Oracle Free service containers exercise chunked `from_sql`, `import_omop` and execution of the generated SQL Server / Oracle DDL. The `[oracle]` extra now requires SQLAlchemy 2.x.
- Fixed (found by the first live Oracle run): `from_sql()` truncated Oracle `NUMBER(p,s)` columns with decimals to integers (Oracle's `NUMBER` type is both Numeric and Integer); they now map to Arrow decimals.
- Tests: optional PyTorch tests skip cleanly when the import raises `OSError` (Windows DLL initialisation failure) as well as `ImportError`.

# omopduckdb 0.7.0

Release 0.7.0 adds source-database neutrality, hash-verified model deployment, consortium telemetry, run manifests, velocity features and more export targets, in Python and (mostly) R. It also changes a few defaults; read **Upgrade notes** first.

## Upgrade notes
- **R installs need `jsonlite`** (now in Imports, with `stats` and `utils`).
- **`run_dqd()` gains a `plausibleDateRange` check** on every clinical `*_date` column (default 1950-01-01 to the extraction date), so pass rates differ from earlier reports. Use `plausible_date_range=` for sites with a far-future sentinel such as 2099-12-31.
- **Oracle export changed**: typed `.ctl` files with `SKIP=1` and explicit date masks, `NUMBER(10)` / `VARCHAR2(n CHAR)` DDL, 30-character identifier check (`oracle_max_identifier=128` for 12.2+), reserved words quoted, `load_oracle.sh` expects `$ORA_CONN`. Regenerate old Oracle exports.
- **`omop-duckdb build` and `omop-duckdb cohort define --type inpatient` now work** (they previously called library functions with wrong arguments). `build` runs `build_schema`, so use a new target file.
- Not exercised against real PostgreSQL, Oracle, SQL Server, Snowflake or BigQuery databases, and the XGBoost/ONNX bundle paths are untested; please report problems.

## What is new
Python-side additions from the 2026-10 consortium audit (`FEATURE_REQUESTS_POST_0.6.8.md`). R ports are included for A1 (logistic and XGBoost-JSON bundles; ONNX/TorchScript components are Python-only), A2, A3, A4 and B1/B2 (`from_sql()` / `import_omop()` take an open DBI connection instead of a SQLAlchemy URL). Bundles and telemetry packets are interchangeable between the two languages. Also ported to R: B3 (`check_source()`, Parquet source files, `etl_pcornet(strict_source = TRUE)`), A5 (`run_manifest()`, `write_run_manifest()`, `verify_manifest()`), C1 (`run_dqd()` `plausibleDateRange`), C2 (`split_by_date()`) and C3 (`define_study_cohort(persist_keys=)`). Not ported to R: C4 secret scanning (repository-level, language independent) and the `omop-duckdb` CLI commands.

- **Source-database neutrality (FR-B1/B2/B3)**: `omop-duckdb extract` / `omop_etl.extract.from_sql()` streams any SQLAlchemy source to Parquet in chunks (typed from the reflected schema, lower-cased columns, case-insensitive table lookup, `--table-map`, `env:VAR` URLs, secrets never logged; extras `[postgres]`, `[mssql]`, `[oracle]`, `[snowflake]`). `omop-duckdb import-omop` / `import_omop()` copies an existing OMOP CDM from a server into DuckDB. The PCORnet ETL now reads `<table>.parquet` as well as `.csv`; `omop-duckdb check-source` / `check_source()` reports present tables/columns and `etl_pcornet(strict_source=True)` fails fast naming the missing column and table.
- **Model deployment (FR-A1/A2/A3/A5)**: pickle-free, SHA-256-verified model bundles (`load_model_bundle`, `score_cohort`, `write_model_bundle`; logistic JSON built in, XGBoost JSON / ONNX / TorchScript with the `[bundle]` extra); aggregate-only `site_telemetry()` packets (JSON-Schema validated, small-cell + secondary suppression) and a random-effects `aggregate_telemetry()` (Cochran Q, I2, tau2); `validate_feature_frame()` with raise/clip/nullify; `RunManifest` / `run_manifest.json`, `--manifest` / `--verify-manifest` and `omop-duckdb verify-manifest`; `omop-duckdb score`.
- **Features and operations (FR-A4/C1/C2/C3/C4)**: `engineer_clinical_features()` / `extract_velocity_features()` (a port of `scripts/engineer_clinical_features.py` sections 1-5, pinned to the script by a golden file shared by the Python and R tests; the 365-day sentinel is `NO_PRIOR_VISIT_DAYS`; the script's label-leaking `site_baseline_risk` is replaced by a training-rows-only `site_baseline_risk()`); DQD `plausibleDateRange` on every clinical `*_date` column; `split_by_date()` held-out split with per-table counts and checksums; `define_study_cohort(persist_keys=..., one_per_patient=True)`; CI `secret-scan` job, `.gitleaks.toml`, pre-commit hook, `scripts/secret_scan.py`.
- **Export targets (FR-B4, Python and R)**: `export_cdm()` now supports `sqlserver` (CSV + `BULK INSERT`), `snowflake` (Parquet + `PUT`/`COPY INTO`) and `bigquery` (Parquet + `bq load`) next to `oracle`. DDL, data files and loader scripts are generated from the single CDM DDL file (`load_cdm_schema()`, `generate_ddl()`), and `lint_ddl()` (plus `sqlglot` parsing in Python CI) checks them. Oracle: 30-character identifier check (`oracle_max_identifier`), reserved words quoted, explicit date masks instead of `NLS_DATE_FORMAT`, and the `.ctl` files now skip the CSV header (`SKIP=1`) and load typed fields.
- Fixed: `omop-duckdb build` and `omop-duckdb cohort define --type inpatient` called their library functions with wrong arguments.

# omopduckdb 0.6.8

Release 0.6.8 introduces native tabular file and workbook loaders (`.from_excel()`, `.from_csv()`, `.from_csvs()`, `.from_folder()`, `.from_bundled()`) for `HierarchicalRollup` and `RollupEngine`, bundled clinical reference code lists for multi-site studies (eDERRI / antidiabetic medications), cryptographic file-hash cache invalidation for Parquet crosswalks, and legacy headerless file support with complete 1:1 cross-language parity between Python (`omop_etl`, `omop_duck_db`) and R (`omopduckdb`):

### 1. Native Tabular File and Workbook Loaders for Rollups
- **Single-Class Tabular Ingestion (`HierarchicalRollup.from_excel`, `HierarchicalRollup.from_csv`, `HierarchicalRollup.from_bundled`)**:
  - Ingests single medication or condition classes directly from Excel workbook sheets or CSV files with automated column resolution (`code_col`, `class_col`, `vocabulary_col`).
  - Native R equivalents: `hierarchical_rollup_from_excel()`, `hierarchical_rollup_from_csv()`, `hierarchical_rollup_from_bundled()`.
- **Multi-Class Engine Loaders (`RollupEngine.from_excel`, `RollupEngine.from_csvs`, `RollupEngine.from_folder`, `RollupEngine.from_bundled`)**:
  - Automatically splits multi-class sheets, sequences of CSV files, or entire directories of CSV files into structured batteries of `HierarchicalRollup` objects.
  - Multi-class exposure extraction directly tags each drug exposure with its corresponding `rollup_name`.
  - Native R equivalents: `rollup_engine_from_excel()`, `rollup_engine_from_csvs()`, `rollup_engine_from_folder()`, `rollup_engine_from_bundled()`.
- **Legacy & Headerless Academic CSV Support**:
  - Supports 0-indexed column integer references (e.g., `code_col=1, class_col=0`) to ingest legacy two-column code lists without required headers.

### 2. Bundled Clinical Reference Workbooks & Code Lists
- **Packaged Study Reference Code Lists (`inst/extdata/code_lists/`)**:
  - Bundles standard clinical code lists for eDERRI antidiabetic medications (`ederri_medications.csv`, `diabetes_drug_categories.csv`, `rxnorm_categories_CUI_map_brief.csv`, `eDERRI_Clinical_Review_Package.xlsx`).
  - Readily accessible via `.from_bundled("ederri_medications")` in Python and `rollup_engine_from_bundled("ederri_medications")` in R without needing external file paths.

### 3. Cryptographic File-Hash Crosswalk Cache Invalidation
- **Automated Cache Invalidation**:
  - Stores SHA-256 digests of underlying source workbooks and CSV files within `source_file_hashes` and incorporates them into `spec_hash`.
  - When an investigator updates or modifies an underlying Excel workbook or CSV code list, cached Parquet crosswalks are automatically recognized as stale and re-materialized directly in DuckDB.

# omopduckdb 0.6.4

Release 0.6.4 introduces production medication classification guards, explainable strategy auditing, publication-grade data quality assertions, causal inference nearest-neighbor matching, automated negative controls, semantic concept set optimization, longitudinal trajectory features, and federated survival meta-analysis with complete 1:1 cross-language parity between Python (`omop_etl`, `omop_duck_db`) and R (`omopduckdb`):

### 1. Medication Classification Guards & Strategy Disagreement Explainer
- **Silent-Zero & Unresolved Concept Guards (`on_unresolved='raise'|'warn'`)**:
  - `extract_medication_classes()` detects medication classes whose ATC codes or ingredient concept IDs fail to resolve to any concepts or descendants in `concept_ancestor`, preventing silent-zero exposure drop-offs.
  - Added `route_concept_ids` and `exclude_forms` (e.g. `['Ophthalmic', 'Topical']`) to cleanly distinguish systemic from non-systemic formulations.
  - Added `by_type=True` emitting per-source exposure flags (`<class>_type_<id>`) for prescriptions vs. administrations.
- **Both-Blind Auditing & Exposure Assertions (`audit_medication_strategies`, `assert_exposure_coverage`)**:
  - Added `both_blind` reason when prevalence is 0 under both source and standard strategies in sites with sufficient sample size (`min_n`).
  - Added `min_prevalence` floor checks to flag unexpected exposure absences.
  - Added `assert_exposure_coverage(flags, classes, min_n=1000)` to prevent silent-zero exposure bugs across consortium hospital sites.
- **Disagreement Explainer with Small-Cell Suppression (`explain_medication_disagreement`)**:
  - Unpacks disagreements between source crosswalks and OMOP hierarchies, reporting top unmapped source values and standard concepts.
  - Automatically suppresses cells with counts under 11 (`min_cell_count=11`, `is_suppressed=True`), enabling safe cross-site export.
- **Standardized Crosswalk Exporter (`export_medication_crosswalk`)**:
  - Exports OMOP class definitions into standardized crosswalk tables (`source = 'current' | 'maps_to'`).
  - Supports older retired RxNorm concepts (`include_retired=True`) and strict validation (`must_contain=[...]`).

### 2. Rigorous Data Quality, Plausibility, and Feature Integrity Audits
- **Code-List Coverage Audit (`audit_code_list_coverage`)**:
  - Computes the proportion of clinical occurrences captured by supplied ICD/RxNorm/LOINC lists, identifying high-frequency uncaptured concepts ($\ge \text{min\_share}$) per site.
- **Panel Missingness Audit (`panel_missingness_audit`)**:
  - Detects component missingness discordance within standard multi-analyte laboratory panels (BMP/CBC) and flags multi-site peer outliers.
- **Table-Level Physiological Plausibility Report & CI Gate (`plausibility_report`)**:
  - Audits Table 1 summaries and raw feature matrices against clinical bounds (`DEFAULT_PHYSIOLOGIC_BOUNDS`), extreme variance/SD ratios, negative physiological values, and impossible percentages. Supports strict validation gates (`ci_gate=True`).
- **Redundant Feature Detector (`detect_redundant_features`)**:
  - Detects rule-of-three linear conversions (e.g. Hematocrit = 3 * Hemoglobin), constant differences, and near-perfect multicollinearities.
- **DQD Mathematical Consistency Summary (`dqd_summary`)**:
  - Recomputes total, passed, failed, and not-applicable checks to verify exact reproducibility of reported pass rates in 1/N increments.

### 3. Pipeline Reconciliation, Forced Index Stays, and Parity Ledger
- **Forced-Index Cohorts from External Stays (`cohort_from_index_stays`)**:
  - Ingests external legacy stay files (patient ID, encounter ID, index date, end date) and maps to OMOP `person_id` and `visit_occurrence_id`.
  - Emits full audit tallies (`total_input_stays`, `matched_patients`, `unmatched_patients`, `patient_match_rate`, `matched_encounters`, `unmatched_encounters`, `encounter_match_rate`).
  - Registers the forced cohort table in DuckDB for downstream study extraction.
- **Feature-Table Parity Ledger (`compare_feature_tables`)**:
  - Matches rows across legacy and new pipelines and evaluates feature-by-feature parity.
  - Computes Standardized Mean Differences (SMDs), Pearson correlation / binary kappa, and missingness gaps.
  - Automatically flags discrepancies with clear reason codes: `large_smd` ($>0.10$), `missingness_gap` ($>5\%$), `scale_mismatch` ($>2\times$ or $<0.5\times$ SD ratio), `zero_variance`, and `all_zero`.
  - Supports site stratification with HIPAA/GDPR-compliant small-cell suppression ($<11$) on sample counts.
- **Cohort Reconciliation & Overlap (`cohort_overlap`)**:
  - Reconciles cohort membership between divergent definitions or pipelines.
  - Reports counts in A-only, B-only, both, Jaccard similarity index, concordance rate, and outcome event rates in intersection and discordant strata.
- **Multi-Parameter Sensitivity Grid (`run_study_grid`)**:
  - Executes study definitions across parameter grids (e.g. sampling rules, lookback windows, wash-in periods).
  - Emits side-by-side N, event counts, and event rates overall and stratified by site.

### 4. Data Integrity, Statistical Leakage, Informative Missingness, and Provenance
- **Statistical Leakage Scan (`leakage_scan`)**:
  - Flags features with perfect or near-perfect stratum separation, single-feature AUC $\ge 0.98$, pure 0/1 outcome rates in non-trivial strata, and temporal sequence violations where data occurs after index date.
- **Informative Missingness Helpers (`add_missing_indicators`, `missingness_by_outcome`)**:
  - Adds binary missingness indicator columns (`miss_<col>`) and evaluates missingness proportions stratified by outcome ($p_1$ vs. $p_0$).
  - Evaluates missingness gaps and preserves explicit fill-rule metadata across consortium summaries.
- **System Provenance Stamp (`provenance`, `check_vocabulary_provenance`)**:
  - Captures library version, DuckDB version, active vocabulary releases per `vocabulary_id`, random seed, and git commit SHA.
  - Compares vocabulary provenance between databases to prevent silent version drift across network sites.
- **Numbers-of-Record Manifest & Stale-Number Scanner (`numbers_of_record`, `scan_document_numbers`)**:
  - Generates authoritative JSON manifests of cohort sample sizes and outcome event counts.
  - Scans rendered manuscripts (Markdown, Quarto, LaTeX) to detect stale figures left over from older cohort runs or unverified numbers.
- **Vocabulary & Input Preflight Verification (`vocabulary_preflight`)**:
  - Verifies presence of required vocabularies, ATC descendant coverage in `concept_ancestor`, and unmapped concept 0 rates across clinical tables with fast-fail safety guards.
- **Privacy-Preserving Cross-Site Overlap (`generate_site_patient_tokens`, `consortium_patient_overlap`)**:
  - Locally computes HMAC-SHA256 salted patient tokens without exposing raw IDs.
  - Computes pairwise cross-site overlap with automated HIPAA/GDPR small-cell suppression ($<11$).
- **CMS Planned-Readmission Filter (`planned_readmission_filter`)**:
  - Identifies and excludes planned maintenance chemotherapy, transplants, and elective procedures from readmission outcomes, reporting removal tallies by site.
- **Observation Period Diagnostics (`observation_period_diagnostics`)**:
  - Compares patient qualification under constructed `observation_period` vs. first-observed clinical events to assess wash-in validity.

### 5. Advanced Causal Inference, Calibration, Trajectories, Survival & Synthetic Data
- **Interactive Cohort Attrition Flow & Heatmaps (`plot_consort_flow`, `plot_multisite_attrition_heatmap`)**:
  - Interactive Plotly Sankey diagrams and camera-ready SVG alluvial flowcharts rendering sequential exclusions and retentions for publication and Quarto manuscripts.
  - Multi-site attrition heatmaps highlighting outlier patient loss across consortium network partners.
- **Propensity Score Nearest-Neighbor Matching & Love Plot (`consortium_propensity_score`, `match_nearest_neighbor`, `plot_love_plot`)**:
  - 1:k greedy nearest-neighbor caliper matching on logit(PS) executing directly on DuckDB feature tables.
  - Camera-ready SVG Love plots verifying pre- and post-adjustment covariate balance against the SMD $< 0.10$ clinical threshold.
- **Automated Negative Controls & Empirical Calibration (`generate_negative_controls`, `calibrate_estimates`)**:
  - Traverses OMOP concept hierarchies (`concept_ancestor`, `concept_relationship`) to identify candidate negative control outcome sets with zero mechanistic or ancestral links.
  - Fits empirical Gaussian null distributions to calibrate study p-values and confidence intervals against residual systematic bias.
- **Semantic Concept Set Optimizer & Usagi Auto-Suggester (`optimize_concept_set`)**:
  - Scans clinical occurrence co-occurrences, sibling hierarchy closures, and lexical synonyms to suggest missing clinical concepts and high-frequency unmapped source codes (`concept_id = 0`).
- **Longitudinal Temporal Trajectory Features (`extract_temporal_trajectories`)**:
  - Exploits DuckDB's vectorized `regr_slope`, `regr_r2`, and `stddev_samp` to compute longitudinal rates of change, goodness of fit, and volatility over look-back windows without row-by-row iteration.
- **Multi-Site Federated Survival Meta-Analysis (`consortium_survival_meta_analysis`)**:
  - DerSimonian-Laird random-effects pooling of Cox Proportional Hazard Ratios across participating sites.
  - Federated non-parametric Kaplan-Meier survival curves with small-cell privacy suppression ($< 11$).
- **Automated Synthetic OMOP Twin Cohort Generator (`generate_synthetic_omop_twin`)**:
  - Extracts marginal concept distributions, age-sex distributions, and event timings to emit structurally identical, anonymized synthetic OMOP DuckDB databases for open benchmarking without HIPAA/PHI exposure.
- **PCORnet MED_ADMIN and DISPENSING Loading**:
  - Maps inpatient administrations (`MED_ADMIN`) and outpatient dispensings (`DISPENSING`) to `drug_exposure` with distinct standard type concepts (`38000180` and `38000175`), dosage units, and administration timestamps.
- **Row-Level Provenance in Table 1 Footnotes**:
  - Extends `table1_to_quarto()` and `table1_to_typst()` with lookback windows, anchor dates, bounds, units, fill rules, and concept sets per row.

# omopduckdb 0.6.3



Release 0.6.3 delivers forward-looking consortium intelligence capabilities for federated clinical research, bridging OHDSI network standards and modern medical publishing with complete 1:1 parity between Python (`omop_etl`, `omop_duck_db`) and R (`omopduckdb`):

### 1. Complementary / Secondary Cell Suppression (`secondary_suppression = True`)
- `audit_cell_suppression()` and `consortium_privacy_audit()` (Python & R):
  - Prevents deductive algebraic disclosure ($Suppressed = Total - \sum Unsuppressed$) in multi-site summary tables with marginal totals.
  - When exactly one cell in a row or column is suppressed ($1 \le N < 11$), automatically identifies and masks the second-smallest non-zero cell in that row/column.
  - Distinguishes and tracks `primary_suppressed` vs. `secondary_suppressed` cell counts in `PrivacyAuditResult` and `consortium_privacy_audit` summary diagnostics.

### 2. Robust Multivariable Odds Ratios with L2 / Ridge Inversion (`consortium_multivariable_risk_factors`)
- `consortium_multivariable_risk_factors()` (Python & R) in `omop_duck_db.ml`:
  - Eliminates collinear and linearly dependent clinical features via QR orthogonalization.
  - Uses L2 regularized logistic regression (ridge inversion) to guarantee an invertible parameter covariance matrix $(X^T W X + \lambda I)^{-1}$, eliminating NaN standard errors and missing 95% CIs on high-dimensional clinical feature matrices.
  - Standardizes continuous features to 1-SD units ("Per 1-SD Increase") while preserving binary indicator features ("Presence vs. Absence").
  - Emits publication-grade Table 5 outputs with domain groupings (Demographics, Comorbidities, Medications, Procedures, Laboratory & Vitals), Odds Ratios, 95% CIs, and Wald p-values.
  - Includes camera-ready Forest Plot visualization (`.plot_forest()`, base-R `plot()`) and Quarto/Typst export.

### 3. Pure DuckDB OHDSI Cohort Diagnostics (`consortium_cohort_diagnostics`)
- `consortium_cohort_diagnostics()` (Python & R):
  - Fully characterizes study cohorts across federated network sites directly in DuckDB without R/Java, rJava, or DatabaseConnector dependencies.
  - Computes calendar-year incidence rates (new cases, person-years, incidence rate per 1,000 PY).
  - Computes time-to-event and follow-up duration distributions (min, p25, median, p75, max, mean, sd).
  - Generates 10-year age and sex population pyramids stratified by gender.
  - Identifies top baseline concept prevalence deltas for conditions and medications in the lookback window.
  - Integrates automated secondary cell suppression to guarantee HIPAA/GDPR compliance before diagnostic outputs leave local environments.

### 4. Internal-External Cross-Validation (IECV) Matrix (`consortium_cross_site_validation`)
- `consortium_cross_site_validation()` (Python & R):
  - Fits predictive models on Site $i$ and evaluates on Site $j$ across a $K \times K$ consortium matrix to evaluate multi-hospital transportability and domain shift.
  - Computes $K \times K$ AUROC and Brier score matrices, mean internal performance (diagonal), mean external performance (off-diagonal), and the multi-site generalization gap.
  - Provides publication-ready cross-site transportability heatmaps (`.plot_heatmap()`) and Quarto/Typst table exports.

### 5. Bidirectional OHDSI Circe JSON Exporter (`export_to_circe_json`)
- `export_to_circe_json()` (Python & R) and `CohortFlow.to_circe_json()`:
  - Exports computable cohorts defined in DuckDB/Python/R back into compliant OHDSI Circe JSON specifications.
  - Seamlessly integrates with OHDSI Atlas, ATLAS Network Studies, and the OHDSI Phenotype Library.
  - Achieves 100% round-trip fidelity verified against `compile_circe_to_duckdb`.

### 6. End-to-End Manuscript Scaffold Builder (`build_manuscript_scaffold`)
- `build_manuscript_scaffold()` (Python & R) and `FederatedConsortium.build_manuscript()`:
  - Bundles generated consortium study artifacts (Table 1, Table 2 CONSORT flow, Table 5 risk factors, IECV transportability matrix, cohort diagnostics, calibration curves, DCA net benefit, and explainability) into a unified, submission-ready Quarto (`.qmd`) or Typst (`.typ`) document.
  - Built-in structured formatting presets matching target journal requirements (JAMA, The Lancet Digital Health, JAMIA, and General).
  - Appends an itemized 22-item TRIPOD Statement reporting compliance checklist table mapped directly to the generated study sections and figures.

# omopduckdb 0.6.2

Release 0.6.2 adds three modular, zero-hard-dependency capabilities for federated health system intelligence, with complete 1:1 parity between Python (`omop_etl`, `omop_duck_db`) and R (`omopduckdb`):

### 1. Automated Privacy-Preserving Cell Suppression Auditor
- `audit_cell_suppression()` and `consortium_privacy_audit()` (Python & R):
  - Audits exported multi-site summary tables (Table 1, Table 2, risk factor tables, patient counts) for compliance with HIPAA Safe Harbor, CMS, and GDPR small-cell de-identification governance rules.
  - Automatically identifies small cell counts ($0 < N < \text{threshold}$, e.g. $N < 11$ or $N < 20$) in both raw counts and formatted count/percent strings (e.g. `"5 (2.3%)"`).
  - Preserves legitimate statistical values such as p-values, standardized mean differences (SMDs), means, medians, and IQRs.
  - Returns `PrivacyAuditResult` (Python) / `consortium_privacy_audit` (R) containing cleaned data frames, total violation counts, flagged column details, and summary audit diagnostics.
  - Integrated directly into `FederatedConsortium.audit_cell_suppression()` / `consortium$audit_privacy()`.

### 2. Direct Quarto and Typst Publication Styler
- `table1_to_quarto()`, `table1_to_typst()`, `Table1Generator.to_quarto()`, and `Table1Generator.to_typst()` (Python & R):
  - Directly outputs camera-ready Quarto markdown (`tbl-colwidths`, `#| label`, `#| tbl-cap`) and Typst markup (`#table(columns: ..., ...)`).
  - Formats bracketed confidence intervals and ranges cleanly (`[12.3, 45.6]`).
  - Supports automatic superscript footnote annotations for standard LOINC codes and custom mapping metadata (`LOINC 59260-0^1`).
  - Provides multi-level header spans and cross-site stratification without requiring manual LaTeX string post-processing.
  - Integrated into `export_table1(format = "quarto" | "typst")` and `Table1Generator` class/R6 instances.

### 3. Built-in Tree Model Explainability & TRIPOD Reporting
- `ConsortiumExplainability` and `consortium_explainability()` (Python & R):
  - Provides model-agnostic and tree-based feature attribution, direction of effect, TRIPOD Item 13b / STROBE Item 15a compliant variable importance tables, and cross-site feature importance drift auditing across federated health systems.
  - Zero heavy dependencies: supports SHAP (via optional `shap` library in Python), native tree gain/Gini importances (`randomForest`, `rpart`, `feature_importances_`, `coef_`), or robust multi-iteration permutation importance fallbacks.
  - Evaluates cross-site feature importance drift across member medical centers when `site_labels` are supplied, identifying ranking concordance and site-specific predictor variance.
  - Includes publication-grade visualization methods: `.plot_importance()`, `.plot_beeswarm()`, and base-R `plot.consortium_explainability()`.

# omopduckdb 0.6.1

Release 0.6.1 delivers high-impact multi-site consortium analytics, cross-site model calibration, clinical decision curve analysis, automated international unit scaling, and competing-risk cumulative incidence function modeling with complete 1:1 parity between Python (`omop_etl` / `omop_duck_db`) and R (`omopduckdb`).

### 1. Consortium-Level Bedside Scoring Wrapper
- `FederatedConsortium.calculate_scores()` (Python) and `consortium_calculate_scores()` (R):
  - Directly evaluates bedside clinical risk scores (`calculate_lace_index` and `calculate_hospital_score`) across multi-center federated consortia (e.g. eDERRI studies) in a single pass.
  - Automatically provisions transparent base table alias views (`CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM v_{tbl}`) alongside prefixed `v_{tbl}` views, enabling standard ETL and scoring functions to run on federated DuckDB connections without query modification.
  - Returns `ConsortiumScoreResult` (Python) / `consortium_scores` (R) with overall and site-stratified risk score distributions, high-risk counts, and risk tier breakdowns, supporting `.summary()`, `.to_dataframe()`, and S3 methods (`print`, `summary`, `as.data.frame`).

### 2. Cross-Site Model Calibration & Decile Drift
- `consortium_calibration()` and `ConsortiumCalibration` (Python & R):
  - Solves multi-site model transportability and risk miscalibration across academic medical centers and community health systems.
  - Computes site-stratified logistic calibration curves via robust pure Iteratively Reweighted Least Squares (IRLS).
  - Evaluates site-specific Brier scores, Expected Calibration Error (ECE), and Maximum Calibration Error (MCE).
  - Implements the multi-site Greenwood-Nam-D'Agostino $\chi^2$ goodness-of-fit test assessing cross-site calibration drift.
  - Quantifies decile drift matrix profiling how predicted risk bins shift across member health systems.
  - Publication-grade multi-panel calibration plotting via `.plot()` / `plot.consortium_calibration()`.

### 3. Clinical Decision Curve Analysis (DCA)
- `decision_curve_analysis()` and `DecisionCurveAnalysis` (Python & R):
  - Evaluates clinical net benefit across decision threshold ranges (Vickers et al., 2006) to demonstrate utility over "treat all" and "treat none" baseline strategies.
  - Computes Net Benefit ($\text{NB} = \frac{\text{TP}}{N} - \frac{\text{FP}}{N}\left(\frac{p_t}{1-p_t}\right)$), Standardized Net Benefit (sNB), and Net Reduction in Interventions per 100 patients.
  - Outputs summary tables, optimal clinical threshold range identification, and publication-ready multi-curve plotting (`.plot()` / `plot.decision_curve_analysis()`).

### 4. Automated European & IFCC Metric Unit Scaling
- Automated LOINC-aware harmonization in `harmonize_measurement_units()` across Python and R:
  - Hemoglobin in $\text{mmol/L}$ (LOINC `59260-0`): Automatic scaling factor $\times 1.611 \to \text{g/dL}$.
  - HbA1c in IFCC $\text{mmol/mol}$ (LOINC `59261-8`): Affine formula $\text{HbA1c (\%)} = (0.09148 \times \text{mmol/mol}) + 2.152$ harmonizing to NGSP percent.
  - Serum Creatinine in $\mu\text{mol/L}$: Multiplier $\div 88.4 \to \text{mg/dL}$ (with regex support for `µmol/l` / `μmol/l`).
  - Automated LOINC detection applies conversions even when source EHR databases leave `unit_source_value` NULL.
  - Added European LOINCs `59260-0` and `59261-8` to `DEFAULT_LAB_LOINCS`.

### 5. Competing Risks Cumulative Incidence Function (CIF) & Fine-Gray Estimator
- `estimate_cif()`, `fine_gray_estimator()`, and `plot_cif()` (Python & R):
  - Non-parametric Aalen-Johansen Cumulative Incidence Function estimation for 3-state competing risks outcomes (censored alive, 30-day acute readmission, terminal mortality).
  - Computes step-wise cumulative incidences and Greenwood-type standard errors across all event types.
  - `fine_gray_estimator()` computes subdistribution hazard ratios using subdistribution risk-set weighting.
  - Multi-curve CIF visualization via `plot_cif()` / `plot.cif_result()`, allowing direct publication of competing-risk adjusted 30-day readmission curves out of the box.

# omopduckdb 0.6.0

Release 0.6.0 closes all 7 feature gaps from request 4 with complete 1:1 cross-language parity across Python (`omop_etl` / `omop_duck_db`) and R (`omopduckdb`).

### 1. Anthropometric Harmonization & Calculated BMI
- `harmonize_anthropometrics()` (Python & R): standardizes raw height (`inches`, `[in_us]` $\times 2.54 \to \text{cm}$; `m` $\times 100 \to \text{cm}$) and weight (`lbs`, `[lb_av]` $\times 0.45359237 \to \text{kg}$; `oz` $\times 0.0283495 \to \text{kg}$; `g` $\times 0.001 \to \text{kg}$).
- Automatically derives BMI $= \text{weight (kg)} / (\text{height (m)})^2$ and optionally replaces implausible raw BMI measurements ($< 10$ or $> 120$) in the created `measurement_harmonized_anthropometrics` view.
- Provides robust fallback handling between `measurement_datetime` and `measurement_date`.

### 2. Clinical Benchmark Bedside Risk Scores
- `calculate_lace_index()` (Python & R): Evaluates the standard bedside LACE Index (0–19 points) for 30-day readmission and mortality risk:
  - **L**: Length of stay ($<1\text{d}=0$, $1\text{d}=1$, $2\text{d}=2$, $3\text{d}=3$, $4\text{--}6\text{d}=4$, $7\text{--}13\text{d}=5$, $\ge 14\text{d}=7$).
  - **A**: Acuity of admission ($3$ points for emergency admission).
  - **C**: Charlson Comorbidity Index score ($0=0$, $1=1$, $2=2$, $3=3$, $\ge 4=5$).
  - **E**: Emergency department visits in prior 6 months ($0=0$, $1=1$, $2=2$, $3=3$, $\ge 4=4$).
- `calculate_hospital_score()` (Python & R): Evaluates the bedside HOSPITAL Score (0–10 points) for 30-day potentially avoidable readmissions:
  - **H**: Hemoglobin at discharge $< 12.0\text{ g/dL}$ ($1$ pt).
  - **O**: Oncology discharge / neoplasm diagnosis in prior 365 days ($2$ pts).
  - **S**: Sodium level at discharge $< 135\text{ mEq/L}$ ($1$ pt).
  - **P**: Procedure during index stay ($1$ pt).
  - **I**: Index admission type urgent / emergency ($1$ pt).
  - **T**: Prior hospital admissions in past 12 months ($0=0$, $1=1$, $\ge 2=2$ pts).
  - **L**: Length of stay $\ge 5$ days ($2$ pts).
- Fixed query boundary bug where index admission ED visits were excluded from acuity evaluation.

### 3. Transitions-of-Care Medication Trajectories
- `extract_medication_transitions()` and `MedicationTransitions` (Python) / `medication_transitions` (R):
  - Trajectory flags per drug class: `{cls}_initiated_inpatient`, `{cls}_discontinued_at_discharge`, `{cls}_continued_across_stay`.
  - Consortium summary indicators: `med_initiated_inpatient`, `med_discontinued_at_discharge`, `med_continued_across_stay`.
  - Methods `.summary()` for transition rate percentages and `.transition_matrix(drug_class)` for $2\times 2$ contingency tables.

### 4. Multi-Site Federated Consortium Connector
- `FederatedConsortium` (Python) / `federated_consortium()` (R): Orchestrates multi-site observational analytics across independent DuckDB databases (e.g. eDERRI studies) attached to a central Athena vocabulary.
- Unified querying across federated views (`v_person`, `v_visit_occurrence`, etc.).
- One-call generation of federated Table 1 (`generate_table1()`) and Table 2 missingness audits (`generate_table2()`).
- Automated multi-site audit summaries (`site_summaries()` / `consortium_site_summaries()`).

### 5. European & Alternative Unit Scaling Factors
- `DEFAULT_UNIT_RULES` updated with affine linear formulas `(pattern, multiplier, intercept)`:
  - Hemoglobin in $\text{mmol/L}$ (LOINC `59260-0`): multiplier $\times 1.611 \to \text{g/dL}$.
  - HbA1c in IFCC $\text{mmol/mol}$ (LOINC `59261-8`): formula $\text{HbA1c (\%)} = (0.09148 \times \text{mmol/mol}) + 2.152$.
  - Creatinine in $\mu\text{mol/L}$: multiplier $\div 88.42 \to \text{mg/dL}$.
  - Height: `in`, `[in_us]`, `m` $\to \text{cm}$.
  - Weight: `lb`, `[lb_av]`, `oz`, `g` $\to \text{kg}$.

### 6. Population-Specific Bounds Profiles
- `POPULATION_PHYSIOLOGIC_BOUNDS` defines physiologically calibrated ranges for `adult`, `pediatric`, and `neonatal` cohorts.
- `filter_bounds(population="adult" | "pediatric" | "neonatal")` applies age-appropriate bounds (e.g. Neonatal heart rate 60–260 bpm, systolic BP 35–130 mmHg).
- `Table1Generator` / `table1_generator()` accepts `population="adult" | "pediatric" | "neonatal"`.

### 7. Windows Python pyarrow / R arrow Subprocess Worker
- `as_plp_arrow(subprocess_worker=True / FALSE)` provides defensive isolation to prevent in-process C++ DLL collision between R's `arrow.dll` and Python's `pyarrow` under Windows.

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
- These tests skip when the package is absent. `DeepPatientLevelPrediction` and `OmopOnDuckDB` are GitHub-only, so they are deliberately not in Suggests (CI's dependency solver cannot resolve them); `reticulate` is.

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
