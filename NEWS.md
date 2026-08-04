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
