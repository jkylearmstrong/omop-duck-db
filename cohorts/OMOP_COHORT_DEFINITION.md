# Portable eDERRI cohort definitions (FR-D1, FR-D2)

Files in `cohorts/`:

| File | Purpose |
|---|---|
| `target_cohort.sql` | Adult IP and ED-to-IP index stays, one per patient (OHDSI SQL / SqlRender) |
| `outcome_cohort.sql` | Inpatient admissions (outcome events) |
| `*.circe.json` | ATLAS/Circe versions (see caveat below) |
| `run_plp_example.R` | SqlRender render + translate, then `PatientLevelPrediction::runPlp()` (writes `main.cohort`; needs a writable CDM) |
| `run_plp_readonly_example.R` | Same on a **read-only** CDM: attached `READ_ONLY`, cohort rows written to a scratch DuckDB |
| `omopduckdb::cohort_definition_set()` | CohortGenerator `cohortDefinitionSet` (`cohortId`, `cohortName`, `sql`, `json`) |

## Parameters

Both SQL files use CohortGenerator's names: `@cdm_database_schema`, `@target_database_schema`, `@target_cohort_table`, `@target_cohort_id`. The target cohort also takes `@followup_days` (30) and `@min_age` (18); `cohort_definition_set()` renders those two into the SQL so `CohortGenerator::generateCohortSet()` only has to fill the four above. The SQL creates no inclusion-rule statistics tables, so always pass `generateStats = FALSE`; the default fails. (v0.8.2 used `cohort_database_schema`, `cohort_table` and `outcome_cohort_id`; rename them in your own calls.)

## Target cohort

1. **Index stay**: `visit_concept_id` 9201 (Inpatient) or 262 (ER and Inpatient) with a discharge (`visit_end_date`).
2. **Adult**: `YEAR(discharge) - year_of_birth >= 18` (`@min_age`).
3. **Discharge exclusions**: `discharged_to_concept_id = 4216643` (Patient died), or any death record on or before discharge.
4. **Verified follow-up**: an `observation_period` covering discharge through discharge + 30 days (`@followup_days`), or death inside that window.
5. **One index per patient (FR-D2)**: the **earliest** qualifying stay, ties broken by the lowest `visit_occurrence_id`. No random number generator, so every engine and rebuild picks the same stay.
6. **Cohort start = cohort end = discharge date.**

## Outcome cohort

Every IP/ED-to-IP admission, start = admission date, end = discharge date. A 30-day readmission is the first outcome in
`(cohort_start_date + 1, cohort_start_date + 30]`; set this with `createStudyPopulationSettings(riskWindowStart = 1, riskWindowEnd = 30, startAnchor = "cohort start", endAnchor = "cohort start")`.

## Relation to `define_study_cohort()`

`define_study_cohort(sampling_rule="earliest")` selects the same index stay (earliest start, lowest id), but also applies
its own wash-in, minimum stay length and evidence rules; the SQL above does not, so counts differ unless those are
switched off. `sampling_rule="random"` uses DuckDB `hash()`, which is not stable across DuckDB versions; use
`"earliest"` or `"stable_random"` (MD5-based) for results that must reproduce across versions, and
`persist_index_visits(con)` to freeze the chosen stays in an `index_visit` table.

## Verification status

- `tests/test_v082_features.py` renders the SQL, translates it with `sqlglot` (T-SQL to DuckDB) and checks the cohorts on a fixture on DuckDB.
- v0.8.3: the SQL also runs with the CDM attached `READ_ONLY` and the cohort table in a separate database (tested on DuckDB).
- **Verified on servers (v0.8.5)**: the `live-db` workflow runs both files, translated with sqlglot, on PostgreSQL 16 and SQL Server 2022 and gets the DuckDB cohorts. **Not verified**: execution on Oracle (use `SqlRender::translate(..., targetDialect = "oracle")`, never sqlglot, which renders `DATEADD` wrongly for Oracle), and translation by SqlRender itself (sqlglot stands in for it).
- `cohort_definition_set()` and the `DatabaseConnector::connect` wrapper in `run_plp_readonly_example.R` have not been run with CohortGenerator, SqlRender or a real read-only file; only the SQL and the data-frame shape are tested.
- `tests/test_v082_features.py` checks the Circe JSON structurally and against the SQL (same visit concepts, age and follow-up defaults, and that the JSON carries the documented rule-3 gap). That is a consistency test, not an ATLAS import.
- The Circe JSON was written by hand and has **not been imported into ATLAS**. Circe cannot test `discharged_to_concept_id` (4216643, died), so an ATLAS-built cohort keeps patients who died on the index visit; the SQL is authoritative. `DateAdjustment` (start at visit end) needs Circe/ATLAS 2.12 or later.
