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
