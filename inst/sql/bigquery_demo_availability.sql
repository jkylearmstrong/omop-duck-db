-- GoogleSQL: how many numeric lab / vital values the cohort has. On claims data (CMS SynPUF) this is expected to be 0,
-- which the demo reports as 100% missing instead of imputing anything. Same placeholders as bigquery_demo_features.sql.
SELECT
  COUNT(*) AS n_measurement_rows,
  COUNTIF(m.value_as_number IS NOT NULL) AS n_numeric_values,
  COUNTIF(m.value_as_number IS NOT NULL AND m.measurement_concept_id = 3016723) AS n_creatinine,
  COUNTIF(m.value_as_number IS NOT NULL AND m.measurement_concept_id = 3004501) AS n_glucose,
  COUNTIF(m.value_as_number IS NOT NULL AND m.measurement_concept_id = 3024561) AS n_albumin,
  COUNTIF(m.value_as_number IS NOT NULL AND m.measurement_concept_id = 3004249) AS n_systolic_bp,
  COUNTIF(m.value_as_number IS NOT NULL AND m.measurement_concept_id = 3012888) AS n_diastolic_bp
FROM `{{scratch}}.{{cohort_table}}` AS c
JOIN `{{cdm}}.measurement` AS m ON m.person_id = c.subject_id
WHERE c.cohort_definition_id = {{cohort_id}}
