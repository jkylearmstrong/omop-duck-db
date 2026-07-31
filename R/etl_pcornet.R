#' Run the PCORnet -> OMOP CDM v5.4 ETL (R implementation)
#'
#' Mirrors `python/omop_etl/build_omop_cdm.py` table-for-table. Both read the
#' same PCORnet-format source directory and call the same
#' `inst/sql/mapping_macros.sql` concept-resolution macros, so their output
#' should be equivalent -- see `tests/fixtures/pcornet_sample/` for the shared
#' smoke-test fixture.
#'
#' Requires [build_schema()] and [load_vocabulary()] to have been run first
#' (the vocabulary must be loaded for the underlying `map_to_standard_concept_id()`/
#' `source_concept_id()` SQL macros to resolve anything).
#'
#' The `*_TYPE_CONCEPT` constants below are the commonly-used OMOP "Type
#' Concept" vocabulary entries for EHR-derived data. Verify them against your
#' own loaded vocabulary before trusting this for real data:
#' `SELECT concept_id, concept_name FROM concept WHERE vocabulary_id = 'Type Concept'`.
#'
#' @param source_dir Directory containing PCORnet-format CSVs (`demographic.csv`,
#'   `encounter.csv`, `diagnosis.csv`, `procedures.csv`, `lab_result_cm.csv`,
#'   `prescribing.csv`, `provider.csv`), matched case-insensitively. Any table
#'   whose source file isn't found is skipped.
#' @param db_path Path to the DuckDB database file (schema + vocabulary must
#'   already exist).
#' @return Invisibly, `db_path`.
#' @export
etl_pcornet <- function(source_dir, db_path = "omop_cdm.duckdb") {
  if (!dir.exists(source_dir)) {
    stop("Source directory not found: ", source_dir)
  }

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  load_mapping_macros(con)

  .load_person(con, source_dir)
  .load_provider(con, source_dir)
  .load_visit_occurrence(con, source_dir)
  .load_condition_occurrence(con, source_dir)
  .load_procedure_occurrence(con, source_dir)
  .load_measurement(con, source_dir)
  .load_drug_exposure(con, source_dir)

  cat("PCORnet ETL complete.\n")
  invisible(db_path)
}

# EHR-derived record, per the OMOP Type Concept vocabulary.
.TYPE_CONCEPT_EHR_ENCOUNTER <- 32827
.TYPE_CONCEPT_EHR_PROCEDURE <- 32817
.TYPE_CONCEPT_EHR_MEASUREMENT <- 32817
.TYPE_CONCEPT_EHR_DRUG <- 32838
.TYPE_CONCEPT_PRIMARY_DX <- 44786627 # "Primary admission diagnosis"

.load_person <- function(con, source_dir) {
  path <- find_source_file(source_dir, "demographic")
  if (is.null(path)) {
    cat("Skipping PERSON - no demographic.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "PERSON (from DEMOGRAPHIC)", sprintf("
    INSERT INTO person
    SELECT
        pcornet_id(PATID) AS person_id,
        CASE UPPER(SEX) WHEN 'M' THEN 8507 WHEN 'F' THEN 8532 ELSE 0 END AS gender_concept_id,
        YEAR(TRY_CAST(BIRTH_DATE AS DATE)) AS year_of_birth,
        MONTH(TRY_CAST(BIRTH_DATE AS DATE)) AS month_of_birth,
        DAY(TRY_CAST(BIRTH_DATE AS DATE)) AS day_of_birth,
        TRY_CAST(BIRTH_DATE AS TIMESTAMP) AS birth_datetime,
        CASE RACE
            WHEN '05' THEN 8527 -- White
            WHEN '03' THEN 8516 -- Black or African American
            WHEN '02' THEN 8515 -- Asian
            ELSE 0
        END AS race_concept_id,
        CASE UPPER(HISPANIC) WHEN 'Y' THEN 38003563 ELSE 38003564 END AS ethnicity_concept_id,
        NULL AS location_id,
        pcornet_id(PROVIDERID) AS provider_id,
        NULL AS care_site_id,
        PATID AS person_source_value,
        SEX AS gender_source_value,
        0 AS gender_source_concept_id,
        RACE AS race_source_value,
        0 AS race_source_concept_id,
        HISPANIC AS ethnicity_source_value,
        0 AS ethnicity_source_concept_id
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", path))
}

.load_visit_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "encounter")
  if (is.null(path)) {
    cat("Skipping VISIT_OCCURRENCE - no encounter.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "VISIT_OCCURRENCE (from ENCOUNTER)", sprintf("
    INSERT INTO visit_occurrence
    SELECT
        pcornet_id(ENCOUNTERID) AS visit_occurrence_id,
        pcornet_id(PATID) AS person_id,
        CASE UPPER(ENC_TYPE)
            WHEN 'IP' THEN 9201 -- Inpatient
            WHEN 'ED' THEN 9203 -- Emergency
            WHEN 'AV' THEN 9202 -- Outpatient
            WHEN 'OA' THEN 9202 -- Outpatient
            ELSE 0
        END AS visit_concept_id,
        TRY_CAST(ADMIT_DATE AS DATE) AS visit_start_date,
        TRY_CAST(ADMIT_DATE || ' ' || COALESCE(ADMIT_TIME, '00:00') AS TIMESTAMP) AS visit_start_datetime,
        COALESCE(TRY_CAST(DISCHARGE_DATE AS DATE), TRY_CAST(ADMIT_DATE AS DATE)) AS visit_end_date,
        TRY_CAST(DISCHARGE_DATE || ' ' || COALESCE(DISCHARGE_TIME, '00:00') AS TIMESTAMP) AS visit_end_datetime,
        %d AS visit_type_concept_id,
        pcornet_id(PROVIDERID) AS provider_id,
        pcornet_id(FACILITYID) AS care_site_id,
        ENCOUNTERID AS visit_source_value,
        0 AS visit_source_concept_id,
        0 AS admitted_from_concept_id, -- ADMIT_SOURCE not mapped yet, see README
        NULL AS admitted_from_source_value,
        CASE UPPER(DISCHARGE_STATUS)
            WHEN 'A' THEN 8536   -- Home
            WHEN 'E' THEN 4216643 -- Expired
            ELSE 0
        END AS discharged_to_concept_id,
        DISCHARGE_STATUS AS discharged_to_source_value,
        NULL AS preceding_visit_occurrence_id
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", .TYPE_CONCEPT_EHR_ENCOUNTER, path))
}

.load_condition_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "diagnosis")
  if (is.null(path)) {
    cat("Skipping CONDITION_OCCURRENCE - no diagnosis.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "CONDITION_OCCURRENCE (from DIAGNOSIS)", sprintf("
    INSERT INTO condition_occurrence
    SELECT
        pcornet_id(DIAGNOSISID) AS condition_occurrence_id,
        pcornet_id(PATID) AS person_id,
        map_to_standard_concept_id(
            CASE UPPER(DX_TYPE) WHEN '09' THEN 'ICD9CM' ELSE 'ICD10CM' END,
            DX
        ) AS condition_concept_id,
        TRY_CAST(COALESCE(DX_DATE, ADMIT_DATE) AS DATE) AS condition_start_date,
        TRY_CAST(COALESCE(DX_DATE, ADMIT_DATE) AS TIMESTAMP) AS condition_start_datetime,
        NULL AS condition_end_date,
        NULL AS condition_end_datetime,
        CASE UPPER(PDX) WHEN 'P' THEN %d ELSE 0 END AS condition_type_concept_id,
        0 AS condition_status_concept_id,
        NULL AS stop_reason,
        pcornet_id(PROVIDERID) AS provider_id,
        pcornet_id(ENCOUNTERID) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        DX AS condition_source_value,
        source_concept_id(
            CASE UPPER(DX_TYPE) WHEN '09' THEN 'ICD9CM' ELSE 'ICD10CM' END,
            DX
        ) AS condition_source_concept_id,
        PDX AS condition_status_source_value
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", .TYPE_CONCEPT_PRIMARY_DX, path))
}

.load_procedure_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "procedures")
  if (is.null(path)) {
    cat("Skipping PROCEDURE_OCCURRENCE - no procedures.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "PROCEDURE_OCCURRENCE (from PROCEDURES)", sprintf("
    INSERT INTO procedure_occurrence
    SELECT
        pcornet_id(PROCEDURESID) AS procedure_occurrence_id,
        pcornet_id(PATID) AS person_id,
        map_to_standard_concept_id(px_vocabulary_id, PX) AS procedure_concept_id,
        TRY_CAST(PX_DATE AS DATE) AS procedure_date,
        TRY_CAST(PX_DATE AS TIMESTAMP) AS procedure_datetime,
        NULL AS procedure_end_date,
        NULL AS procedure_end_datetime,
        %d AS procedure_type_concept_id,
        0 AS modifier_concept_id,
        NULL AS quantity,
        pcornet_id(PROVIDERID) AS provider_id,
        pcornet_id(ENCOUNTERID) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        PX AS procedure_source_value,
        source_concept_id(px_vocabulary_id, PX) AS procedure_source_concept_id,
        NULL AS modifier_source_value
    FROM (
        SELECT *,
            CASE PX_TYPE
                WHEN '01' THEN 'CPT4'
                WHEN '02' THEN 'HCPCS'
                WHEN '09' THEN 'ICD9Proc'
                WHEN '10' THEN 'ICD10PCS'
                ELSE PX_TYPE
            END AS px_vocabulary_id
        FROM read_csv_auto('%s', union_by_name = true, all_varchar = true)
    ) src;
  ", .TYPE_CONCEPT_EHR_PROCEDURE, path))
}

.load_measurement <- function(con, source_dir) {
  path <- find_source_file(source_dir, "lab_result_cm")
  if (is.null(path)) {
    cat("Skipping MEASUREMENT - no lab_result_cm.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "MEASUREMENT (from LAB_RESULT_CM)", sprintf("
    INSERT INTO measurement
    SELECT
        pcornet_id(LAB_RESULT_CM_ID) AS measurement_id,
        pcornet_id(PATID) AS person_id,
        map_to_standard_concept_id('LOINC', LAB_LOINC) AS measurement_concept_id,
        TRY_CAST(RESULT_DATE AS DATE) AS measurement_date,
        TRY_CAST(RESULT_DATE AS TIMESTAMP) AS measurement_datetime,
        NULL AS measurement_time,
        %d AS measurement_type_concept_id,
        0 AS operator_concept_id,
        TRY_CAST(RESULT_NUM AS DOUBLE) AS value_as_number,
        0 AS value_as_concept_id,
        0 AS unit_concept_id,
        NULL AS range_low,
        NULL AS range_high,
        pcornet_id(PROVIDERID) AS provider_id,
        pcornet_id(ENCOUNTERID) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        RAW_LAB_NAME AS measurement_source_value,
        source_concept_id('LOINC', LAB_LOINC) AS measurement_source_concept_id,
        RESULT_UNIT AS unit_source_value,
        0 AS unit_source_concept_id,
        RESULT_NUM AS value_source_value,
        NULL AS measurement_event_id,
        0 AS meas_event_field_concept_id
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", .TYPE_CONCEPT_EHR_MEASUREMENT, path))
}

.load_drug_exposure <- function(con, source_dir) {
  path <- find_source_file(source_dir, "prescribing")
  if (is.null(path)) {
    cat("Skipping DRUG_EXPOSURE - no prescribing.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "DRUG_EXPOSURE (from PRESCRIBING)", sprintf("
    INSERT INTO drug_exposure
    SELECT
        pcornet_id(PRESCRIBINGID) AS drug_exposure_id,
        pcornet_id(PATID) AS person_id,
        map_to_standard_concept_id('RxNorm', RXNORM_CUI) AS drug_concept_id,
        TRY_CAST(RX_START_DATE AS DATE) AS drug_exposure_start_date,
        TRY_CAST(RX_START_DATE AS TIMESTAMP) AS drug_exposure_start_datetime,
        COALESCE(TRY_CAST(RX_END_DATE AS DATE), TRY_CAST(RX_START_DATE AS DATE)) AS drug_exposure_end_date,
        TRY_CAST(RX_END_DATE AS TIMESTAMP) AS drug_exposure_end_datetime,
        NULL AS verbatim_end_date,
        %d AS drug_type_concept_id,
        NULL AS stop_reason,
        NULL AS refills,
        NULL AS quantity,
        NULL AS days_supply,
        NULL AS sig,
        0 AS route_concept_id,
        NULL AS lot_number,
        pcornet_id(PROVIDERID) AS provider_id,
        pcornet_id(ENCOUNTERID) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        RAW_RX_MED_NAME AS drug_source_value,
        source_concept_id('RxNorm', RXNORM_CUI) AS drug_source_concept_id,
        NULL AS route_source_value,
        NULL AS dose_unit_source_value
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", .TYPE_CONCEPT_EHR_DRUG, path))
}

.load_provider <- function(con, source_dir) {
  path <- find_source_file(source_dir, "provider")
  if (is.null(path)) {
    cat("Skipping PROVIDER - no provider.csv found\n")
    return(invisible(NULL))
  }
  run_insert(con, "PROVIDER", sprintf("
    INSERT INTO provider
    SELECT
        pcornet_id(PROVIDERID) AS provider_id,
        NULL AS provider_name,
        PROVIDER_NPI AS npi,
        NULL AS dea,
        map_to_standard_concept_id('NUCC', PROVIDER_SPECIALTY_PRIMARY) AS specialty_concept_id,
        NULL AS care_site_id,
        NULL AS year_of_birth,
        CASE UPPER(PROVIDER_SEX) WHEN 'M' THEN 8507 WHEN 'F' THEN 8532 ELSE 0 END AS gender_concept_id,
        PROVIDERID AS provider_source_value,
        RAW_PROVIDER_SPECIALTY_PRIMARY AS specialty_source_value,
        source_concept_id('NUCC', PROVIDER_SPECIALTY_PRIMARY) AS specialty_source_concept_id,
        PROVIDER_SEX AS gender_source_value,
        0 AS gender_source_concept_id
    FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);
  ", path))
}
