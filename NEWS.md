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
