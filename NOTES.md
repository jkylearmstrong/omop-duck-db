# Notes

Internal working notes on where this package came from and where it's headed. See `README.Rmd`/`README.md` for the technical package overview.

## Where it came from

`omop-duck-db` (R package `omopduckdb`) builds and populates an OMOP CDM v5.4 database in DuckDB, loads the OHDSI Athena vocabulary, and maps PCORnet-format source data into the CDM — with parallel R and Python ETL implementations sharing the same schema and concept-mapping SQL, so either language gets the same result. It's deliberately the public/generic piece of a bigger initiative: no institution names, no site specifics, no protected data, ever — real-world use against production data happens entirely outside this repo, against your own infrastructure (see `kyle_out_zip/omop_bridge/` for that private, site-aware layer).

## Key decisions (and why)

- **License: dual `GPL-3 | MIT`**, changed from plain MIT on 2026-08-05 to match the rest of the public track (`TempleCBE`, `pslongSim`, `ML-PScore`, `quarto_temple_brand`). Still local-only, not pushed.
- **Schema is generated, never hand-declared.** `inst/extdata/5.4/duckdb/*.sql` (from OHDSI's `CommonDataModel` R package) is the single source of truth for both ETL paths — this is why `kyle_out_zip`'s bridging script calls this package's real entrypoint instead of writing new mapping SQL; a hand-rolled alternative (`kyle_out_zip/omop_duckdb_gpu_server_integration_plan.md`) diverged from this and would have produced a broken database (wrong ID scheme, wrong glob paths, invented columns).

## Current status (v0.6.0)

Verified directly against source and comprehensive test suites:
- **Full 1:1 Parity**: All ETL capabilities, feature extractions, phenotyping, and reporting exist in both Python (`omop_etl` / `omop_duck_db`) and R (`omopduckdb`).
- **Requests 1–4 Fully Implemented & Tested**:
  1. *Staged Wide Measurements & Plausibility Bounds*: Sequential unpivot, BMI coalescing, physiologic plausible filters (`filter_bounds`, `DEFAULT_PHYSIOLOGIC_BOUNDS`, `POPULATION_PHYSIOLOGIC_BOUNDS`).
  2. *Standardized Phenotyping & Cohorts*: Composable criterion trees (`ConditionCriteria`, `DrugCriteria`, `MeasurementCriteria`), readmission cohorts (`define_study_cohort`), Elixhauser and Charlson comorbidity scoring with train/serve skew protection.
  3. *Unit & Anthropometric Harmonization*: Preflight auditing, conversion (`DEFAULT_UNIT_RULES`), and `harmonize_anthropometrics()` deriving BMI $= \text{weight (kg)} / (\text{height (m)})^2$ and correcting raw unit mismatches (inches/lbs vs cm/kg).
  4. *Clinical Benchmark Heuristics*: Standardized bedside score calculators `calculate_lace_index()` and `calculate_hospital_score()`.
  5. *Transitions of Care*: Trajectory flags (`extract_medication_transitions`, `MedicationTransitions`) capturing initiation, discontinuation, and continuation across acute stay transitions.
  6. *Multi-Site Consortium Federation*: Zero-copy multi-database connector (`FederatedConsortium` / `federated_consortium`) generating cross-site Table 1 and Table 2 missingness audits in a single call.
  7. *HADES & ML Bridges*: Zero-copy Arrow PLP bridge (`as_plp_arrow`, `plp_arrow_to_plp_data`) with Windows subprocess DLL isolation, tidymodels adapter, and `omop-learn` backend.

## Where it's heading

- Continuous deployment: Automated multi-site validation against production multi-hospital consortia (eDERRI).
- Extended OHDSI interoperability: In-database Achilles and DataQualityDashboard (DQD) runners leveraging DuckDB's vectorized query engine.
