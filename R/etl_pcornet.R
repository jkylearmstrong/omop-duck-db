#' Run the PCORnet -> OMOP CDM v5.4 ETL (R implementation)
#'
#' Mirrors `python/omop_etl/build_omop_cdm.py` table-for-table. Both read the
#' same PCORnet-format source directory, share the same schema and concept-resolution
#' logic, and generate 100% deterministic surrogate keys and OHDSI v5.4 tables.
#'
#' @param source_dir Directory containing PCORnet-format CSVs (`demographic.csv`,
#'   `encounter.csv`, `diagnosis.csv`, `procedures.csv`, `lab_result_cm.csv`,
#'   `prescribing.csv`, `provider.csv`, and optionally `death.csv`), matched
#'   case-insensitively. Any table whose source file isn't found is skipped.
#' @param db_path Path to the DuckDB database file (schema + vocabulary must
#'   already exist).
#' @return Invisibly, `db_path`.
#' @export
etl_pcornet <- function(source_dir, db_path = "omop_cdm.duckdb") {
  if (!dir.exists(source_dir)) {
    stop("Source directory not found: ", source_dir)
  }

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    try(DBI::dbDisconnect(con, shutdown = TRUE), silent = TRUE)
    gc()
  })
  load_mapping_macros(con)

  .load_provider(con, source_dir)
  .load_person(con, source_dir)
  .load_visit_occurrence(con, source_dir)
  .load_condition_occurrence(con, source_dir)
  .load_procedure_occurrence(con, source_dir)
  .load_measurement(con, source_dir)
  .load_drug_exposure(con, source_dir)
  .load_death(con, source_dir)

  build_observation_period(con)
  build_drug_era(con)
  build_condition_era(con)
  .load_cdm_source(con)

  cat("PCORnet ETL complete.\n")
  invisible(db_path)
}

# EHR-derived record, per the OMOP Type Concept vocabulary.
.TYPE_CONCEPT_EHR_ENCOUNTER <- 32827
.TYPE_CONCEPT_EHR_CONDITION <- 32020
.TYPE_CONCEPT_EHR_PROCEDURE <- 32817
.TYPE_CONCEPT_EHR_MEASUREMENT <- 32817
.TYPE_CONCEPT_EHR_DRUG <- 32838
.TYPE_CONCEPT_EHR_DEATH <- 32815
.TYPE_CONCEPT_PRIMARY_DX <- 44786627 # "Primary admission diagnosis"

.today_iso <- function() format(Sys.Date(), "%Y-%m-%d")

.load_provider <- function(con, source_dir) {
  path <- find_source_file(source_dir, "provider")
  if (is.null(path)) {
    cat("Skipping PROVIDER - no provider.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_provider", path,
    c("PROVIDERID", "PROVIDER_NPI", "PROVIDER_SPECIALTY_PRIMARY", "PROVIDER_SEX", "RAW_PROVIDER_SPECIALTY_PRIMARY")
  )
  run_insert(con, "PROVIDER", sprintf("
    INSERT INTO provider
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.PROVIDERID) + (SELECT COALESCE(MAX(provider_id), 0) FROM provider) AS provider_id,
        NULL AS provider_name,
        src.PROVIDER_NPI AS npi,
        NULL AS dea,
        COALESCE(
            stcm.target_concept_id,
            cr.concept_id_2,
            CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
            0
        ) AS specialty_concept_id,
        NULL AS care_site_id,
        NULL AS year_of_birth,
        CASE UPPER(src.PROVIDER_SEX) WHEN 'M' THEN 8507 WHEN 'F' THEN 8532 ELSE 0 END AS gender_concept_id,
        src.PROVIDERID AS provider_source_value,
        src.RAW_PROVIDER_SPECIALTY_PRIMARY AS specialty_source_value,
        COALESCE(c.concept_id, 0) AS specialty_source_concept_id,
        src.PROVIDER_SEX AS gender_source_value,
        0 AS gender_source_concept_id
    FROM _temp_provider src
    LEFT JOIN concept c
      ON c.vocabulary_id = 'NUCC'
     AND c.concept_code = src.PROVIDER_SPECIALTY_PRIMARY
    LEFT JOIN (
        SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
        FROM concept_relationship
        WHERE relationship_id = 'Maps to'
        GROUP BY concept_id_1
    ) cr
      ON cr.concept_id_1 = c.concept_id
    LEFT JOIN (
        SELECT source_code, MIN(target_concept_id) AS target_concept_id
        FROM source_to_concept_map
        WHERE source_vocabulary_id = 'NUCC'
          AND (invalid_reason IS NULL OR invalid_reason = '')
          AND (valid_end_date IS NULL OR valid_end_date >= DATE '%s')
        GROUP BY source_code
    ) stcm
      ON stcm.source_code = src.PROVIDER_SPECIALTY_PRIMARY
    WHERE src.PROVIDERID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PROVIDERID) = 1;
  ", .today_iso()))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_provider;")
}

.load_person <- function(con, source_dir) {
  path <- find_source_file(source_dir, "demographic")
  if (is.null(path)) {
    cat("Skipping PERSON - no demographic.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_demographic", path,
    c("PATID", "SEX", "BIRTH_DATE", "RACE", "HISPANIC", "PROVIDERID")
  )
  run_insert(con, "PERSON (from DEMOGRAPHIC)", "
    INSERT INTO person
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.PATID) + (SELECT COALESCE(MAX(person_id), 0) FROM person) AS person_id,
        CASE UPPER(src.SEX) WHEN 'M' THEN 8507 WHEN 'F' THEN 8532 ELSE 0 END AS gender_concept_id,
        YEAR(parse_omop_date(src.BIRTH_DATE)) AS year_of_birth,
        MONTH(parse_omop_date(src.BIRTH_DATE)) AS month_of_birth,
        DAY(parse_omop_date(src.BIRTH_DATE)) AS day_of_birth,
        parse_omop_datetime(src.BIRTH_DATE, NULL) AS birth_datetime,
        CASE src.RACE
            WHEN '05' THEN 8527 -- White
            WHEN '03' THEN 8516 -- Black or African American
            WHEN '02' THEN 8515 -- Asian
            ELSE 0
        END AS race_concept_id,
        CASE UPPER(src.HISPANIC) WHEN 'Y' THEN 38003563 ELSE 38003564 END AS ethnicity_concept_id,
        NULL AS location_id,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        NULL AS care_site_id,
        src.PATID AS person_source_value,
        src.SEX AS gender_source_value,
        0 AS gender_source_concept_id,
        src.RACE AS race_source_value,
        0 AS race_source_concept_id,
        src.HISPANIC AS ethnicity_source_value,
        0 AS ethnicity_source_concept_id
    FROM _temp_demographic src
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    WHERE src.PATID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PATID ORDER BY parse_omop_date(src.BIRTH_DATE) NULLS LAST) = 1;
  ")
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_demographic;")
}

.load_visit_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "encounter")
  if (is.null(path)) {
    cat("Skipping VISIT_OCCURRENCE - no encounter.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_encounter", path,
    c("ENCOUNTERID", "PATID", "ENC_TYPE", "ADMIT_DATE", "ADMIT_TIME",
      "DISCHARGE_DATE", "DISCHARGE_TIME", "PROVIDERID", "FACILITYID", "DISCHARGE_STATUS")
  )
  run_insert(con, "VISIT_OCCURRENCE (from ENCOUNTER)", sprintf("
    INSERT INTO visit_occurrence
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.ENCOUNTERID) + (SELECT COALESCE(MAX(visit_occurrence_id), 0) FROM visit_occurrence) AS visit_occurrence_id,
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        CASE UPPER(src.ENC_TYPE)
            WHEN 'IP' THEN 9201 -- Inpatient
            WHEN 'ED' THEN 9203 -- Emergency
            WHEN 'AV' THEN 9202 -- Outpatient
            WHEN 'OA' THEN 9202 -- Outpatient
            ELSE 0
        END AS visit_concept_id,
        parse_omop_date(src.ADMIT_DATE) AS visit_start_date,
        parse_omop_datetime(src.ADMIT_DATE, src.ADMIT_TIME) AS visit_start_datetime,
        COALESCE(parse_omop_date(src.DISCHARGE_DATE), parse_omop_date(src.ADMIT_DATE)) AS visit_end_date,
        parse_omop_datetime(COALESCE(src.DISCHARGE_DATE, src.ADMIT_DATE), src.DISCHARGE_TIME) AS visit_end_datetime,
        %d AS visit_type_concept_id,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        pcornet_id(src.FACILITYID) AS care_site_id,
        src.ENCOUNTERID AS visit_source_value,
        0 AS visit_source_concept_id,
        0 AS admitted_from_concept_id,
        NULL AS admitted_from_source_value,
        CASE UPPER(src.DISCHARGE_STATUS)
            WHEN 'A' THEN 8536   -- Home
            WHEN 'E' THEN 4216643 -- Expired
            ELSE 0
        END AS discharged_to_concept_id,
        src.DISCHARGE_STATUS AS discharged_to_source_value,
        NULL AS preceding_visit_occurrence_id
    FROM _temp_encounter src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    WHERE src.ENCOUNTERID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.ENCOUNTERID) = 1;
  ", .TYPE_CONCEPT_EHR_ENCOUNTER))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_encounter;")
}

.load_condition_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "diagnosis")
  if (is.null(path)) {
    cat("Skipping CONDITION_OCCURRENCE - no diagnosis.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_diagnosis", path,
    c("DIAGNOSISID", "PATID", "DX_TYPE", "DX", "DX_DATE", "ADMIT_DATE", "PDX", "PROVIDERID", "ENCOUNTERID")
  )
  run_insert(con, "CONDITION_OCCURRENCE (from DIAGNOSIS)", sprintf("
    INSERT INTO condition_occurrence
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.DIAGNOSISID) + (SELECT COALESCE(MAX(condition_occurrence_id), 0) FROM condition_occurrence) AS condition_occurrence_id,
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        COALESCE(
            stcm.target_concept_id,
            cr.concept_id_2,
            CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
            0
        ) AS condition_concept_id,
        parse_omop_date(COALESCE(src.DX_DATE, src.ADMIT_DATE)) AS condition_start_date,
        parse_omop_datetime(COALESCE(src.DX_DATE, src.ADMIT_DATE), NULL) AS condition_start_datetime,
        NULL AS condition_end_date,
        NULL AS condition_end_datetime,
        CASE UPPER(src.PDX) WHEN 'P' THEN %d ELSE 0 END AS condition_type_concept_id,
        0 AS condition_status_concept_id,
        NULL AS stop_reason,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        src.DX AS condition_source_value,
        COALESCE(c.concept_id, 0) AS condition_source_concept_id,
        src.PDX AS condition_status_source_value
    FROM (
        SELECT *,
            CASE UPPER(DX_TYPE) WHEN '09' THEN 'ICD9CM' ELSE 'ICD10CM' END AS dx_vocabulary_id
        FROM _temp_diagnosis
    ) src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    LEFT JOIN visit_occurrence vo
      ON vo.visit_source_value = src.ENCOUNTERID
    LEFT JOIN concept c
      ON c.vocabulary_id = src.dx_vocabulary_id
     AND c.concept_code = src.DX
    LEFT JOIN (
        SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
        FROM concept_relationship
        WHERE relationship_id = 'Maps to'
        GROUP BY concept_id_1
    ) cr
      ON cr.concept_id_1 = c.concept_id
    LEFT JOIN (
        SELECT source_vocabulary_id, source_code, MIN(target_concept_id) AS target_concept_id
        FROM source_to_concept_map
        WHERE (invalid_reason IS NULL OR invalid_reason = '')
          AND (valid_end_date IS NULL OR valid_end_date >= DATE '%s')
        GROUP BY source_vocabulary_id, source_code
    ) stcm
      ON stcm.source_vocabulary_id = src.dx_vocabulary_id
     AND stcm.source_code = src.DX
    WHERE src.DIAGNOSISID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.DIAGNOSISID) = 1;
  ", .TYPE_CONCEPT_PRIMARY_DX, .today_iso()))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_diagnosis;")
}

.load_procedure_occurrence <- function(con, source_dir) {
  path <- find_source_file(source_dir, "procedures")
  if (is.null(path)) {
    cat("Skipping PROCEDURE_OCCURRENCE - no procedures.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_procedures", path,
    c("PROCEDURESID", "PATID", "PX_TYPE", "PX", "PX_DATE", "PROVIDERID", "ENCOUNTERID")
  )
  run_insert(con, "PROCEDURE_OCCURRENCE (from PROCEDURES)", sprintf("
    INSERT INTO procedure_occurrence
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.PROCEDURESID) + (SELECT COALESCE(MAX(procedure_occurrence_id), 0) FROM procedure_occurrence) AS procedure_occurrence_id,
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        COALESCE(
            stcm.target_concept_id,
            cr.concept_id_2,
            CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
            0
        ) AS procedure_concept_id,
        parse_omop_date(src.PX_DATE) AS procedure_date,
        parse_omop_datetime(src.PX_DATE, NULL) AS procedure_datetime,
        NULL AS procedure_end_date,
        NULL AS procedure_end_datetime,
        %d AS procedure_type_concept_id,
        0 AS modifier_concept_id,
        NULL AS quantity,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        src.PX AS procedure_source_value,
        COALESCE(c.concept_id, 0) AS procedure_source_concept_id,
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
        FROM _temp_procedures
    ) src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    LEFT JOIN visit_occurrence vo
      ON vo.visit_source_value = src.ENCOUNTERID
    LEFT JOIN concept c
      ON c.vocabulary_id = src.px_vocabulary_id
     AND c.concept_code = src.PX
    LEFT JOIN (
        SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
        FROM concept_relationship
        WHERE relationship_id = 'Maps to'
        GROUP BY concept_id_1
    ) cr
      ON cr.concept_id_1 = c.concept_id
    LEFT JOIN (
        SELECT source_vocabulary_id, source_code, MIN(target_concept_id) AS target_concept_id
        FROM source_to_concept_map
        WHERE (invalid_reason IS NULL OR invalid_reason = '')
          AND (valid_end_date IS NULL OR valid_end_date >= DATE '%s')
        GROUP BY source_vocabulary_id, source_code
    ) stcm
      ON stcm.source_vocabulary_id = src.px_vocabulary_id
     AND stcm.source_code = src.PX
    WHERE src.PROCEDURESID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PROCEDURESID) = 1;
  ", .TYPE_CONCEPT_EHR_PROCEDURE, .today_iso()))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_procedures;")
}

.load_measurement <- function(con, source_dir) {
  path <- find_source_file(source_dir, "lab_result_cm")
  if (is.null(path)) {
    cat("Skipping MEASUREMENT - no lab_result_cm.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_lab_result", path,
    c("LAB_RESULT_CM_ID", "PATID", "LAB_LOINC", "RESULT_DATE", "RESULT_TIME",
      "RESULT_NUM", "RESULT_UNIT", "PROVIDERID", "ENCOUNTERID", "RAW_LAB_NAME", "RAW_LAB_CODE")
  )
  run_insert(con, "MEASUREMENT (from LAB_RESULT_CM)", sprintf("
    INSERT INTO measurement
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.LAB_RESULT_CM_ID) + (SELECT COALESCE(MAX(measurement_id), 0) FROM measurement) AS measurement_id,
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        COALESCE(
            stcm.target_concept_id,
            cr.concept_id_2,
            CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
            0
        ) AS measurement_concept_id,
        parse_omop_date(src.RESULT_DATE) AS measurement_date,
        parse_omop_datetime(src.RESULT_DATE, src.RESULT_TIME) AS measurement_datetime,
        src.RESULT_TIME AS measurement_time,
        %d AS measurement_type_concept_id,
        0 AS operator_concept_id,
        TRY_CAST(src.RESULT_NUM AS DOUBLE) AS value_as_number,
        0 AS value_as_concept_id,
        0 AS unit_concept_id,
        NULL AS range_low,
        NULL AS range_high,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        COALESCE(src.LAB_LOINC, src.RAW_LAB_NAME, src.RAW_LAB_CODE) AS measurement_source_value,
        COALESCE(c.concept_id, 0) AS measurement_source_concept_id,
        src.RESULT_UNIT AS unit_source_value,
        0 AS unit_source_concept_id,
        src.RESULT_NUM AS value_source_value,
        NULL AS measurement_event_id,
        0 AS meas_event_field_concept_id
    FROM _temp_lab_result src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    LEFT JOIN visit_occurrence vo
      ON vo.visit_source_value = src.ENCOUNTERID
    LEFT JOIN concept c
      ON c.vocabulary_id = 'LOINC'
     AND c.concept_code = src.LAB_LOINC
    LEFT JOIN (
        SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
        FROM concept_relationship
        WHERE relationship_id = 'Maps to'
        GROUP BY concept_id_1
    ) cr
      ON cr.concept_id_1 = c.concept_id
    LEFT JOIN (
        SELECT source_code, MIN(target_concept_id) AS target_concept_id
        FROM source_to_concept_map
        WHERE source_vocabulary_id = 'LOINC'
          AND (invalid_reason IS NULL OR invalid_reason = '')
          AND (valid_end_date IS NULL OR valid_end_date >= DATE '%s')
        GROUP BY source_code
    ) stcm
      ON stcm.source_code = src.LAB_LOINC
    WHERE src.LAB_RESULT_CM_ID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.LAB_RESULT_CM_ID) = 1;
  ", .TYPE_CONCEPT_EHR_MEASUREMENT, .today_iso()))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_lab_result;")
}

.load_drug_exposure <- function(con, source_dir) {
  path <- find_source_file(source_dir, "prescribing")
  if (is.null(path)) {
    cat("Skipping DRUG_EXPOSURE - no prescribing.csv found\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_prescribing", path,
    c("PRESCRIBINGID", "PATID", "RXNORM_CUI", "RX_START_DATE", "RX_END_DATE",
      "RAW_RX_MED_NAME", "RAW_RX_NDC", "PROVIDERID", "ENCOUNTERID")
  )
  run_insert(con, "DRUG_EXPOSURE (from PRESCRIBING)", sprintf("
    INSERT INTO drug_exposure
    SELECT
        ROW_NUMBER() OVER (ORDER BY src.PRESCRIBINGID) + (SELECT COALESCE(MAX(drug_exposure_id), 0) FROM drug_exposure) AS drug_exposure_id,
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        COALESCE(
            stcm.target_concept_id,
            cr.concept_id_2,
            CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE 0 END,
            0
        ) AS drug_concept_id,
        parse_omop_date(src.RX_START_DATE) AS drug_exposure_start_date,
        parse_omop_datetime(src.RX_START_DATE, NULL) AS drug_exposure_start_datetime,
        COALESCE(parse_omop_date(src.RX_END_DATE), parse_omop_date(src.RX_START_DATE)) AS drug_exposure_end_date,
        parse_omop_datetime(src.RX_END_DATE, NULL) AS drug_exposure_end_datetime,
        NULL AS verbatim_end_date,
        %d AS drug_type_concept_id,
        NULL AS stop_reason,
        NULL AS refills,
        NULL AS quantity,
        NULL AS days_supply,
        NULL AS sig,
        0 AS route_concept_id,
        NULL AS lot_number,
        COALESCE(pr.provider_id, pcornet_id(src.PROVIDERID)) AS provider_id,
        COALESCE(vo.visit_occurrence_id, pcornet_id(src.ENCOUNTERID)) AS visit_occurrence_id,
        NULL AS visit_detail_id,
        COALESCE(src.RXNORM_CUI, src.RAW_RX_NDC, src.RAW_RX_MED_NAME) AS drug_source_value,
        COALESCE(c.concept_id, 0) AS drug_source_concept_id,
        NULL AS route_source_value,
        NULL AS dose_unit_source_value
    FROM _temp_prescribing src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    LEFT JOIN provider pr
      ON pr.provider_source_value = src.PROVIDERID
    LEFT JOIN visit_occurrence vo
      ON vo.visit_source_value = src.ENCOUNTERID
    LEFT JOIN concept c
      ON c.vocabulary_id = 'RxNorm'
     AND c.concept_code = src.RXNORM_CUI
    LEFT JOIN (
        SELECT concept_id_1, MIN(concept_id_2) AS concept_id_2
        FROM concept_relationship
        WHERE relationship_id = 'Maps to'
        GROUP BY concept_id_1
    ) cr
      ON cr.concept_id_1 = c.concept_id
    LEFT JOIN (
        SELECT source_code, MIN(target_concept_id) AS target_concept_id
        FROM source_to_concept_map
        WHERE source_vocabulary_id = 'RxNorm'
          AND (invalid_reason IS NULL OR invalid_reason = '')
          AND (valid_end_date IS NULL OR valid_end_date >= DATE '%s')
        GROUP BY source_code
    ) stcm
      ON stcm.source_code = src.RXNORM_CUI
    WHERE src.PRESCRIBINGID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PRESCRIBINGID) = 1;
  ", .TYPE_CONCEPT_EHR_DRUG, .today_iso()))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_prescribing;")
}

.load_death <- function(con, source_dir) {
  path <- find_source_file(source_dir, "death")
  if (is.null(path)) {
    cat("Skipping DEATH - no death.csv found (optional table)\n")
    return(invisible(NULL))
  }
  prepare_source_view(
    con, "_temp_death", path,
    c("PATID", "DEATH_DATE", "DEATH_DATE_IMPUTE", "DEATH_SOURCE", "DEATH_MATCH_CONFIDENCE")
  )
  run_insert(con, "DEATH", sprintf("
    INSERT INTO death (
        person_id,
        death_date,
        death_datetime,
        death_type_concept_id,
        cause_concept_id,
        cause_source_value,
        cause_source_concept_id
    )
    SELECT
        COALESCE(p.person_id, pcornet_id(src.PATID)) AS person_id,
        parse_omop_date(src.DEATH_DATE) AS death_date,
        parse_omop_datetime(src.DEATH_DATE, NULL) AS death_datetime,
        %d AS death_type_concept_id,
        0 AS cause_concept_id,
        src.DEATH_SOURCE AS cause_source_value,
        0 AS cause_source_concept_id
    FROM _temp_death src
    LEFT JOIN person p
      ON p.person_source_value = src.PATID
    WHERE src.PATID IS NOT NULL 
      AND src.DEATH_DATE IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY src.PATID ORDER BY parse_omop_date(src.DEATH_DATE) DESC) = 1;
  ", .TYPE_CONCEPT_EHR_DEATH))
  DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_death;")
}

#' Synthesize Observation Period envelopes
#' @param con Active DuckDB connection
#' @export
build_observation_period <- function(con) {
  DBI::dbExecute(con, "DELETE FROM observation_period;")
  run_insert(con, "OBSERVATION_PERIOD (synthesized)", "
    INSERT INTO observation_period (
        observation_period_id,
        person_id,
        observation_period_start_date,
        observation_period_end_date,
        period_type_concept_id
    )
    WITH clinical_events AS (
        SELECT person_id, visit_start_date AS event_date FROM visit_occurrence WHERE visit_start_date IS NOT NULL
        UNION ALL
        SELECT person_id, visit_end_date AS event_date FROM visit_occurrence WHERE visit_end_date IS NOT NULL
        UNION ALL
        SELECT person_id, condition_start_date AS event_date FROM condition_occurrence WHERE condition_start_date IS NOT NULL
        UNION ALL
        SELECT person_id, COALESCE(condition_end_date, condition_start_date) AS event_date FROM condition_occurrence WHERE condition_start_date IS NOT NULL
        UNION ALL
        SELECT person_id, procedure_date AS event_date FROM procedure_occurrence WHERE procedure_date IS NOT NULL
        UNION ALL
        SELECT person_id, drug_exposure_start_date AS event_date FROM drug_exposure WHERE drug_exposure_start_date IS NOT NULL
        UNION ALL
        SELECT person_id, COALESCE(drug_exposure_end_date, drug_exposure_start_date) AS event_date FROM drug_exposure WHERE drug_exposure_start_date IS NOT NULL
        UNION ALL
        SELECT person_id, measurement_date AS event_date FROM measurement WHERE measurement_date IS NOT NULL
    ),
    envelopes AS (
        SELECT
            person_id,
            MIN(event_date) AS obs_start_date,
            MAX(event_date) AS obs_end_date
        FROM clinical_events
        GROUP BY person_id
    )
    SELECT
        ROW_NUMBER() OVER (ORDER BY person_id) AS observation_period_id,
        person_id,
        obs_start_date AS observation_period_start_date,
        obs_end_date AS observation_period_end_date,
        32827 AS period_type_concept_id
    FROM envelopes;
  ")
}

#' Synthesize Drug Eras
#' @param con Active DuckDB connection
#' @export
build_drug_era <- function(con) {
  DBI::dbExecute(con, "DELETE FROM drug_era;")
  run_insert(con, "DRUG_ERA (synthesized)", "
    INSERT INTO drug_era (
        drug_era_id,
        person_id,
        drug_concept_id,
        drug_era_start_date,
        drug_era_end_date,
        drug_exposure_count,
        gap_days
    )
    WITH exposures AS (
        SELECT
            person_id,
            drug_concept_id,
            drug_exposure_start_date AS start_date,
            COALESCE(drug_exposure_end_date, drug_exposure_start_date) AS end_date
        FROM drug_exposure
        WHERE drug_concept_id != 0 AND drug_exposure_start_date IS NOT NULL
    ),
    lagged AS (
        SELECT
            person_id,
            drug_concept_id,
            start_date,
            end_date,
            MAX(end_date) OVER (
                PARTITION BY person_id, drug_concept_id
                ORDER BY start_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prev_max_end_date
        FROM exposures
    ),
    era_starts AS (
        SELECT
            person_id,
            drug_concept_id,
            start_date,
            end_date,
            CASE
                WHEN prev_max_end_date IS NULL THEN 1
                WHEN start_date > prev_max_end_date + INTERVAL 30 DAY THEN 1
                ELSE 0
            END AS is_new_era
        FROM lagged
    ),
    era_groups AS (
        SELECT
            person_id,
            drug_concept_id,
            start_date,
            end_date,
            SUM(is_new_era) OVER (
                PARTITION BY person_id, drug_concept_id
                ORDER BY start_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS era_group
        FROM era_starts
    )
    SELECT
        ROW_NUMBER() OVER (ORDER BY person_id, drug_concept_id, MIN(start_date)) AS drug_era_id,
        person_id,
        drug_concept_id,
        MIN(start_date) AS drug_era_start_date,
        MAX(end_date) AS drug_era_end_date,
        COUNT(*) AS drug_exposure_count,
        NULL AS gap_days
    FROM era_groups
    GROUP BY person_id, drug_concept_id, era_group;
  ")
}

#' Synthesize Condition Eras
#' @param con Active DuckDB connection
#' @export
build_condition_era <- function(con) {
  DBI::dbExecute(con, "DELETE FROM condition_era;")
  run_insert(con, "CONDITION_ERA (synthesized)", "
    INSERT INTO condition_era (
        condition_era_id,
        person_id,
        condition_concept_id,
        condition_era_start_date,
        condition_era_end_date,
        condition_occurrence_count
    )
    WITH conditions AS (
        SELECT
            person_id,
            condition_concept_id,
            condition_start_date AS start_date,
            COALESCE(condition_end_date, condition_start_date) AS end_date
        FROM condition_occurrence
        WHERE condition_concept_id != 0 AND condition_start_date IS NOT NULL
    ),
    lagged AS (
        SELECT
            person_id,
            condition_concept_id,
            start_date,
            end_date,
            MAX(end_date) OVER (
                PARTITION BY person_id, condition_concept_id
                ORDER BY start_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prev_max_end_date
        FROM conditions
    ),
    era_starts AS (
        SELECT
            person_id,
            condition_concept_id,
            start_date,
            end_date,
            CASE
                WHEN prev_max_end_date IS NULL THEN 1
                WHEN start_date > prev_max_end_date + INTERVAL 30 DAY THEN 1
                ELSE 0
            END AS is_new_era
        FROM lagged
    ),
    era_groups AS (
        SELECT
            person_id,
            condition_concept_id,
            start_date,
            end_date,
            SUM(is_new_era) OVER (
                PARTITION BY person_id, condition_concept_id
                ORDER BY start_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS era_group
        FROM era_starts
    )
    SELECT
        ROW_NUMBER() OVER (ORDER BY person_id, condition_concept_id, MIN(start_date)) AS condition_era_id,
        person_id,
        condition_concept_id,
        MIN(start_date) AS condition_era_start_date,
        MAX(end_date) AS condition_era_end_date,
        COUNT(*) AS condition_occurrence_count
    FROM era_groups
    GROUP BY person_id, condition_concept_id, era_group;
  ")
}

.load_cdm_source <- function(con, cdm_source_name = "PCORnet -> DuckDB OMOP CDM", cdm_holder = "omop-duck-db") {
  DBI::dbExecute(con, "DELETE FROM cdm_source;")
  today_str <- .today_iso()
  run_insert(con, "CDM_SOURCE", sprintf("
    INSERT INTO cdm_source
    SELECT
        '%s' AS cdm_source_name,
        'PCORNET' AS cdm_source_abbreviation,
        '%s' AS cdm_holder,
        'PCORnet extract mapped to OMOP CDM v5.4 in DuckDB' AS source_description,
        'https://github.com/jkylearmstrong/omop-duck-db' AS source_documentation_reference,
        'https://github.com/jkylearmstrong/omop-duck-db' AS cdm_etl_reference,
        DATE '%s' AS source_release_date,
        DATE '%s' AS cdm_release_date,
        'v5.4' AS cdm_version,
        756265 AS cdm_version_concept_id,
        COALESCE((SELECT vocabulary_version FROM vocabulary WHERE vocabulary_id = 'None' LIMIT 1), 'Unknown') AS vocabulary_version;
  ", cdm_source_name, cdm_holder, today_str, today_str))
}
