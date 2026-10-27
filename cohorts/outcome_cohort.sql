/* ============================================================================================
   Outcome cohort: inpatient admissions (FR-D1)

   OHDSI SQL (SqlRender dialect). Every IP (9201) and ED-to-IP (262) admission is written as an event
   (cohort_start_date = admission date, cohort_end_date = discharge date, or the admission date when open).
   PatientLevelPrediction restricts events to the risk window after each target cohort start date, so a
   "30-day readmission" is the first outcome in (cohort_start_date + 1, cohort_start_date + @followup_days].
   Parameters: @cdm_database_schema, @cohort_database_schema, @cohort_table, @outcome_cohort_id.
   ============================================================================================ */
DELETE FROM @cohort_database_schema.@cohort_table WHERE cohort_definition_id = @outcome_cohort_id;

INSERT INTO @cohort_database_schema.@cohort_table (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
SELECT @outcome_cohort_id AS cohort_definition_id,
       v.person_id AS subject_id,
       CAST(v.visit_start_date AS DATE) AS cohort_start_date,
       CAST(COALESCE(v.visit_end_date, v.visit_start_date) AS DATE) AS cohort_end_date
FROM @cdm_database_schema.visit_occurrence v
WHERE v.visit_concept_id IN (9201, 262);
