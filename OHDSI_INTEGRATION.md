# omop-duck-db and the OHDSI tools

omop-duck-db exists to run OMOP studies on DuckDB databases. OHDSI already has tools for most of what a study needs, so the
rule for this package is:

1. **Study logic goes in standard OHDSI artifacts** (Circe/ATLAS cohort definitions, concept sets, OHDSI SQL, HADES
   settings objects), so it is portable to ATLAS and to sites that do not run this package.
2. **The package supplies what the OHDSI tools do not**: PCORnet-to-OMOP ETL, DuckDB/Arrow/lakehouse plumbing, vocabulary loading,
   a Java-free Circe compiler, bridges that make a DuckDB CDM usable by HADES, and checks that the data are fit for the study.
3. **Where the package re-implements an OHDSI tool, it says so, covers only a subset, and is checked against the tool.**

This page audits the package against that rule, and records what was checked against the real tools. The checks ran with
CohortGenerator, CirceR, FeatureExtraction, Achilles 1.7.2 and DataQualityDashboard (DatabaseConnector 8.0.0, duckdb 2.0.0).
Not run: PatientLevelPrediction, PhenotypeLibrary and CohortDiagnostics, which are described from their documentation.

| Area | omop-duck-db | OHDSI tool | Relationship | Recommendation |
|---|---|---|---|---|
| PCORnet to OMOP ETL, vocabulary load, observation periods | `etl_pcornet`, `omop_etl.build_omop_cdm`, `load_vocabulary` | Athena vocabularies (downloaded); no HADES ETL | Package-only | **Keep.** This is the package's core job. Keep concept choices (visit, discharge) checked against Athena. |
| DuckDB as a HADES backend | `hades_preflight`, `readonly_*`, `plp_database_details`, integer64 helpers | DatabaseConnector, CohortGenerator, PatientLevelPrediction | Complementary | **Keep.** Tested with CohortGenerator on DuckDB. |
| Cohort definitions | `cohorts/*.circe.json`, `cohorts/*.sql`, `cohort_definition_set()`, `compile_circe_to_duckdb()` | ATLAS/Circe, CirceR, CohortGenerator | Same job (portable form) | **Prefer Circe.** New study logic is written as Circe JSON (see below). The native compiler supports a subset of Circe; unsupported constructs (for example discharge disposition, `DateAdjustment`, nested groups, `includeMapped`) must not be relied on. |
| Study cohort engine | `define_study_cohort`, `build_readmission_cohort`, `build_end_of_life_cohort` | CohortGenerator + Circe (cohort), PLP (outcome windows) | Overlaps for entry/exclusion; adds one-stay-per-patient sampling, verified follow-up, attrition | **Keep as a convenience** for replication studies, document that Circe is the interoperable route, and keep new exclusions in Circe (`metformin_only_prediabetes_pcos_*.circe.json`). |
| Phenotype DSL | `phenotype_dsl` | Circe, PhenotypeLibrary | Same job | **Prefer Circe / PhenotypeLibrary** for anything meant to be shared; keep the DSL for quick exploration. |
| Metformin-only prediabetes/PCOS | `find_metformin_only_prediabetes_pcos` and the Circe files | Circe cohort | Same job | **Circe is the definition**; the function stays as a check that also reads source values, which Circe cannot. |
| Diabetes phenotype | `diabetes_phenotype.circe.json`, `require_diabetes` | PhenotypeLibrary (T1DM/T2DM phenotypes), Circe | Same job | **Prefer the Circe file**; consider PhenotypeLibrary definitions for a published phenotype. |
| Concept sets, descendants | `concept_optimizer`, `get_concept_*`, `resolve_concept_set` | Circe concept sets, ConceptSetDiagnostics | Overlaps | **Wrap**: express sets as Circe concept sets; keep helpers for DuckDB-side use. |
| Covariates / features | `extract_patient_features`, `comorbidity` (Elixhauser, Charlson), `utilization`, `velocity`, labs, `extract_sparse_concept_matrix` | FeatureExtraction (demographics, condition/drug groups, Charlson, DCSI, CHADS2, visit counts, measurement values) | Overlaps for Charlson, demographics, counts; Elixhauser is not in FeatureExtraction as far as known (not checked here) | **Keep Elixhauser and the velocity set** (no equivalent). Visit counts match FeatureExtraction exactly. **Charlson does not match** FeatureExtraction's Romano adaptation (see below): use one or the other in a study, not both. |
| Area-level covariates (RUCA, SDI) | `derive_geography_features`, `person_address_history` | None | Package-only | **Keep, and integrate**: `write_geography_measurements()` stores the derived values as `measurement` rows with local concept ids, and FeatureExtraction reads them as covariates (tested). |
| Model training, calibration, evaluation | `ml`, `evaluate`, `scoring`, `model_bundle`, `feature_contract`, `categorical_encoding` | PatientLevelPrediction, DeepPatientLevelPrediction | Overlaps | **Prefer PLP** for development and validation. Keep bundles, contracts and fixed-level encoding for scoring outside HADES (for example an XGBoost model at a site). |
| Data quality | `dqd`, `dqd_catalog`, `run_dqd_full` (JSON in DQD shape) | DataQualityDashboard | Same job, subset | **Use the official DQD where it is installed**; keep the in-DuckDB catalog as a fast gate. State the check subset when reporting. |
| Data characterization | `run_achilles` (about 25 of about 170 analyses) | Achilles | Same job, subset | **Use Achilles for published characterization**; keep the subset for quick checks on a DuckDB file. `min_cell_count` removes cells where Achilles masks them. |
| Cohort diagnostics, attrition | `cohort_diagnostics`, `cohort_flow`, `compute_attrition` | CohortDiagnostics | Overlaps | **Wrap or defer** to CohortDiagnostics when the study is shared; keep attrition for CONSORT tables. |
| Table 1 | `table1`, profiles | FeatureExtraction `createTable1`, CohortDiagnostics | Overlaps | **Keep** (study-specific layout); do not claim equivalence. |
| Effect estimation | `propensity`, `survival`, `federated_survival`, `federated_learning` | CohortMethod, SelfControlledCaseSeries, Strategus | Overlaps | **Prefer HADES** for estimates that will be published; the federated functions are research tools, not a replacement for Strategus. |
| Negative controls, empirical calibration | `negative_controls` | EmpiricalCalibration, Strategus modules | Overlaps | **Prefer EmpiricalCalibration** for published results. |
| Cohort SQL on other engines | `cohort_sql` (sqlglot), Oracle/T-SQL translation | SqlRender | Overlaps | **Prefer SqlRender** for OHDSI SQL; the sqlglot path is for engines SqlRender handles badly; both are tested only where noted in `NEWS.md`. |
| BigQuery, lakehouse, Arrow sync | `bigquery_sync`, `load_from_bigquery`, `lakehouse` | None | Package-only | **Keep.** Unit-tested only against fakes. |
| Reconciliation with a legacy pipeline | `reconciliation` (`compare_feature_tables`, `cohort_overlap`) | None | Package-only | **Keep.** |

## What to change next

1. **Cohorts**: add every new inclusion/exclusion rule as Circe JSON first (done for the metformin-only exclusion and the diabetes
   phenotype). `cohorts/target_cohort.circe.json` still lacks discharge disposition and an end-date index (documented), so its SQL stays authoritative.
2. **Run the official tools in CI** where HADES is installed (the R tests above skip cleanly otherwise).
3. **Charlson**: compare the two implementations on real mapped data and decide which one the study reports.
4. **DQD**: add the concept foreign-key, domain and class checks, or state in every report that they are not run.
5. **Keep the claim honest**: every function that overlaps an OHDSI tool says in its help page what it covers and what it does not.

## Checked against the official tools

| Tool | What was compared | Result |
|---|---|---|
| CirceR + CohortGenerator | The four `cohorts/*.circe.json` files, rendered by CirceR and run on DuckDB, against the native compiler and `find_metformin_only_prediabetes_pcos()` | Same people in every cohort (`tests/testthat/test-circe-definitions.R`). The check found that the first version of `diabetes_phenotype.circe.json` was **not valid Circe** (age was written as a `DemographicCriteria` criterion instead of `DemographicCriteriaList`); fixed, and the native compiler now reads `DemographicCriteriaList`. |
| FeatureExtraction, visit counts | `extract_prior_utilization` against `useVisitConceptCountLongTerm` (365 days before, admission day excluded) | Identical counts (`test-ohdsi-parity.R`). |
| FeatureExtraction, measurement values | RUCA and SDI written by `write_geography_measurements()` read back as covariates | Works once `unit_concept_id` is 0: FeatureExtraction joins on the unit and silently skips rows where it is NULL (`test-geography.R`). |
| FeatureExtraction, Charlson | `extract_charlson_index` against `useCharlsonIndex` ("Romano adaptation") on the repo's comorbidity fixture, ICD codes mapped to standard concepts through the vocabulary | **Not equivalent.** 135 of 180 persons get the same index. Many of the 45 differences are the fixture's own probes (code normalization such as lower case and spaces, which FeatureExtraction cannot see, window-edge cases, Elixhauser probes) and 33 of 103 codes had no standard mapping, so this is not a clean estimate; but the two use different category definitions and different data (source codes vs standard concepts), so **do not mix them in one model or table**. A clean comparison needs real mapped data. |
| Achilles 1.7.2 | 14 analyses (0-5, 101, 103, 105, 108, 111-113, 200-202) on a small fixture | Counts identical. Two real differences, now fixed: the package used the population standard deviation and interpolated percentiles; Achilles uses the sample standard deviation (NULL for one value) and the discrete percentile (`tests/test_achilles.py` holds the official values). Cosmetic: analysis 0 stores the Achilles version in `stratum_2`; analysis 108 writes `12.0` on DuckDB. Achilles itself needs a workaround on this stack: with duckdb 2.0.0 and DatabaseConnector 8.0.0 `achilles()` fails with "Column ANALYSIS_ID does not exist in target table" (upper-case column names in `insertTable`). |
| DataQualityDashboard | Table and field checks on a planted-defect fixture | Agrees on person foreign keys, start-before-end and after-birth. Differences, now fixed: primary-key violations counted only the surplus copies (DQD counts every row of a duplicated key); after-birth ignored end dates and datetime fields. Still different: the package has no concept foreign-key, domain or class checks. `plausibleFutureDate` and `visitPersonMatch` are **not DQD checks**; they are labelled `check_origin = "omop-duck-db"` in the output. |

