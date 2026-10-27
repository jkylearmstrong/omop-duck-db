# Notes

Internal working notes on where this package came from, architecture decisions, packaging gotchas, and where it's headed. See `README.Rmd`/`README.md` for the technical package overview.

## Where it came from

`omop-duck-db` (R package `omopduckdb`) builds and populates an OMOP CDM v5.4 database in DuckDB, loads the OHDSI Athena vocabulary, and maps PCORnet-format source data into the CDM — with parallel R and Python ETL implementations sharing the same schema and concept-mapping SQL, so either language gets the same result. It's deliberately the public/generic piece of a bigger initiative: no institution names, no site specifics, no protected data, ever — real-world use against production data happens entirely outside this repo, against your own infrastructure.

## Key decisions (and why)

- **License: dual `GPL-3 | MIT`**, changed from plain MIT on 2026-08-05 to match the rest of the public track (`TempleCBE`, `pslongSim`, `ML-PScore`, `quarto_temple_brand`).
- **Schema is generated, never hand-declared.** `inst/extdata/5.4/duckdb/*.sql` (from OHDSI's `CommonDataModel` R package) is the single source of truth for both ETL paths — this is why `kyle_out_zip`'s bridging script calls this package's real entrypoint instead of writing new mapping SQL; a hand-rolled alternative (`kyle_out_zip/omop_duckdb_gpu_server_integration_plan.md`) diverged from this and would have produced a broken database (wrong ID scheme, wrong glob paths, invented columns).
- **Consortium Zero-Copy Federation:** Base table views (`CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM v_{tbl}`) alongside prefixed `v_{tbl}` views allow standard ETL routines, scoring calculators, and measurement extractors to run transparently against attached multi-site consortia without bespoke SQL rewriting.

## Current status (v0.6.1)

Verified directly against source and comprehensive test suites:

- **Full 1:1 Parity**: All ETL capabilities, feature extractions, phenotyping, scoring, and reporting exist in both Python (`omop_etl` / `omop_duck_db`) and R (`omopduckdb`).
- **Core Capabilities (v0.6.0)**:
  1. *Staged Wide Measurements & Plausibility Bounds*: Sequential unpivot, BMI coalescing, physiologic plausible filters (`filter_bounds`, `DEFAULT_PHYSIOLOGIC_BOUNDS`, `POPULATION_PHYSIOLOGIC_BOUNDS`).
  2. *Standardized Phenotyping & Cohorts*: Composable criterion trees (`ConditionCriteria`, `DrugCriteria`, `MeasurementCriteria`), readmission cohorts (`define_study_cohort`), Elixhauser and Charlson comorbidity scoring with train/serve skew protection.
  3. *Unit & Anthropometric Harmonization*: Preflight auditing, conversion (`DEFAULT_UNIT_RULES`), and `harmonize_anthropometrics()` deriving BMI $= \text{weight (kg)} / (\text{height (m)})^2$ and correcting raw unit mismatches (inches/lbs vs cm/kg).
  4. *Clinical Benchmark Heuristics*: Standardized bedside score calculators `calculate_lace_index()` and `calculate_hospital_score()`.
  5. *Transitions of Care*: Trajectory flags (`extract_medication_transitions`, `MedicationTransitions`) capturing initiation, discontinuation, and continuation across acute stay transitions.
  6. *Multi-Site Consortium Federation*: Zero-copy multi-database connector (`FederatedConsortium` / `federated_consortium`) generating cross-site Table 1 and Table 2 missingness audits in a single call.
  7. *HADES & ML Bridges*: Zero-copy Arrow PLP bridge (`as_plp_arrow`, `plp_arrow_to_plp_data`) with Windows subprocess DLL isolation, tidymodels adapter, and `omop-learn` backend.
- **v0.6.1 High-Impact Analytical Additions**:
  1. *Consortium Bedside Scoring*: `FederatedConsortium.calculate_scores()` and `consortium_calculate_scores()` running LACE and HOSPITAL risk stratification simultaneously across all connected federation member sites.
  2. *Cross-Site Model Calibration & Decile Drift*: `consortium_calibration()` / `ConsortiumCalibration` computing site-stratified IRLS logistic calibration curves, Greenwood-Nam-D'Agostino $\chi^2$ goodness-of-fit, Brier scores, ECE/MCE, and decile assignment drift.
  3. *Clinical Decision Curve Analysis (DCA)*: `decision_curve_analysis()` / `DecisionCurveAnalysis` computing Net Benefit, Standardized Net Benefit, and Net Reduction in Interventions across clinical threshold ranges, with publication-grade plotting.
  4. *Automated European & IFCC Metric Unit Scaling*: LOINC-based automated harmonization in `harmonize_measurement_units()` for European Hemoglobin mmol/L (`59260-0` $\times 1.611 \to \text{g/dL}$), IFCC HbA1c mmol/mol (`59261-8` $(0.09148 \times x) + 2.152 \to \%$), and Serum Creatinine $\mu\text{mol/L}$ ($\div 88.4 \to \text{mg/dL}$), even when raw unit strings are missing or unstandardized.
  5. *Competing Risks Cumulative Incidence Function (CIF)*: `estimate_cif()`, `plot_cif()`, and `fine_gray_estimator()` providing non-parametric Aalen-Johansen CIF curves and Fine-Gray subdistribution hazard modeling for 30-day readmissions accounting for inpatient and out-of-hospital death.

## CI/CD Architecture & Local Validation with `act`

Workflows under `.github/workflows/`:

- `ci.yml`: On push and PR:
  - `r-check`: `R CMD check` + testthat on matrix (`ubuntu-latest`, `windows-latest`).
  - `python-test`: `pytest` on matrix (`ubuntu-latest`, `windows-latest` across Python 3.10, 3.11, 3.12).
  - `python-only`: Runs on `ubuntu-latest` with **no R installed** to ensure standalone operation on GPU/deep-learning clusters; R-dependent parity tests cleanly skip via `shutil.which("Rscript") is None`.
  - `docker-build`: Multi-stage Docker image build and in-container test execution on Linux.
- `pkgdown.yml`: Automated documentation site build and deployment to GitHub Pages on push to `main` (requires Pages source set to GitHub Actions).
- `release.yml`: Triggered on `v*` tags; executes build, `twine check`, and gated PyPI publishing requiring manual approval via the `pypi-release` environment.

### Local validation with `act`

- `python-only`: Passed locally with zero R present; standalone schema tests passed and cross-language parity tests skipped cleanly.
- `docker-build`: Passed; nested Docker build validated both test suites inside the image.
- `r-check`: Passed; full `R CMD check` via `r-lib/actions/check-r-package`.
- Note: `act` emulates Linux runners only; Windows runners and GitHub-specific secrets/environments (Pages, PyPI) remain verified via GitHub Actions runners.

## Multi-Stage Containerization (`Dockerfile`) Lessons

Generated initially via `usethis::use_dockerfile()` and customized for Python & R hybrid execution:

1. **Posit Package Manager (PPM) Snapshot Pinning**: Upstream generator pinned snapshots to `Sys.Date()`, which fails if clocks drift or snapshot metadata lags. Swapped to `/latest` rolling alias.
2. **PPM Linux Codename Mismatch (ICU 74 vs 70)**: `rocker/r-ver:4.4.2` is built on Ubuntu **noble** (24.04), not jammy (22.04). Hardcoded `__linux__/jammy` binaries (e.g. `stringi` linked to `libicui18n.so.70`) failed at runtime because noble provides ICU 74 (`libicui18n.so.74`). Fixed by pointing PPM to `noble`.
3. **`.dockerignore` and `README.md`**: Upstream default ignored `README.md`, breaking `pip install` during the metadata build phase (`pyproject.toml` references `readme = "README.md"`). Excluded from `.dockerignore`.
4. **`.Rprofile` Autoloader Bypass**: Project `.Rprofile` activates renv by default, which can hijack `.libPaths()` into an unpopulated project cache inside Docker. Resolved by setting `ENV RENV_CONFIG_AUTOLOADER_ENABLED=FALSE` in both builder and runtime stages.
5. **Testthat Directory Resolution in Containers**: Running `testthat::test_dir()` requires the installed package under test. Fixed with `R CMD INSTALL --install-tests`, executing `cd /build/tests && Rscript testthat.R`.
6. **Docker Context Exclusions**: Large generated assets (`*.duckdb`, `vocabulary_download_v5_*`, `config/*.yml` containing cloud credentials) are strictly excluded via `.dockerignore`.

## R & Python Packaging Gotchas

Technical lessons recorded during package build and deployment:

- **`renv` Target Isolation**: Installing packages after `renv::init()` without explicit library specification can inadvertently install into the ambient user library instead of the project library. Recommended practice: `renv::restore()` followed by explicit `renv::install(...)`.
- **Maintainer Email in `DESCRIPTION`**: `R CMD build` strictly requires a valid email address in `Authors@R`.
- **`.Rbuildignore` Completeness**: Without comprehensive exclusions, `R CMD build` attempts to tar large SQLite/DuckDB databases, Athena downloads, and Python environments. Maintained in `.Rbuildignore`.
- **Windows DuckDB File Locks**: Rapid close and immediate reconnect to a DuckDB database file within a single process on Windows can trigger transient locking conflicts (`dbDisconnect(shutdown=TRUE)`). A garbage collection call (`gc()`) before reconnect reliably releases the OS file lock.
- **Windows `Rscript` Subprocess Execution**: Windows `CreateProcess` cannot directly execute `Rscript` when resolved as `Rscript.bat`. Calling `subprocess.run(["Rscript", ...], shell=True)` properly delegates through `cmd.exe`.
- **Non-Editable Python Wheel Bundling**: Relocatable wheels cannot resolve SQL templates via git repository-relative paths. Resolved using `pyproject.toml` `[tool.setuptools.package-data]` / `force-include` combined with a package-relative first, repo-relative fallback resource locator.
- **Athena CPT4 Vocabulary**: Athena downloads do not include CPT4 descriptions directly due to AMA copyright; the Java utility `cpt4.jar` (requiring a UMLS license) or public CMS OMOP BigQuery mirrors (`bigquery-public-data.cms_synthetic_patient_data_omop`) are used to populate CPT4 concepts.

## Secret scanning and token revocation

CI (`secret-scan` job) runs `scripts/secret_scan.py` over tracked files, a planted-token self-test, and `gitleaks` over the full
history; `.pre-commit-config.yaml` runs both before each commit (`pip install pre-commit && pre-commit install`).
If a credential was ever committed (for example the TestPyPI token noted in QUESTIONS B11):

1. **Revoke it first** (TestPyPI/PyPI: Account settings -> API tokens -> Remove). Rewriting history does not un-leak a token that
   was pushed; revocation does.
2. Find it: `gitleaks detect --source . --redact` (full history).
3. Remove it from history: `git filter-repo --replace-text replacements.txt` (a file with `<the-token>==>REMOVED`), then
   force-push all branches/tags and ask collaborators to re-clone. Re-run step 2 to confirm.
4. Publish with a trusted publisher / a new scoped token stored only in GitHub Actions secrets.

## Shared vocabulary by reference (FR-B5)

A site file does not have to hold the 5-18 GB vocabulary. `omop-duckdb build --vocab central_vocab.duckdb` creates
`concept` and the other vocabulary tables as **views** into a catalog named `central_vocab`. Any client that opens
such a file must attach the vocabulary file under exactly that name, read-only:

```sql
ATTACH 'central_vocab.duckdb' AS central_vocab (READ_ONLY);   -- one line, on every connection
```

* Python: `omop_etl.attach_read_only(con, "site.duckdb", "central_vocab.duckdb")` attaches both files read-only.
* R / HADES: `omopduckdb::readonly_attach_sql("site.duckdb", "central_vocab.duckdb")` returns the statements; DatabaseConnector
  cannot ATTACH on connect, so run them on each new connection (see `cohorts/run_plp_readonly_example.R`).
* To make one file self-contained instead (and larger): `omop-duckdb materialize-vocab site.duckdb --vocab central_vocab.duckdb`
  copies the views into real tables, which needs a writable file.

## Where it's heading

- **Production Consortium Deployments**: Automated validation against multi-hospital health network nodes (eDERRI study).
- **Extended In-Database OHDSI Analytics**: Vectorized execution of Achilles characterization and DataQualityDashboard (DQD) directly inside DuckDB.
- **Distributed Federated Learning**: Iterative horizontal federated logistic regression and Cox proportional hazards across consortium member nodes without patient-level data sharing.
