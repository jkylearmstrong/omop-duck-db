-- DuckDB twin of bigquery_demo_features.sql: the same features, row for row, for checking that the two backends agree
-- (omopduckdb::run_duckdb_demo(), omop_etl.backend_parity.run_duckdb_demo()). Change both files together:
-- tests/test_backend_parity.py runs this file and the GoogleSQL one (transpiled back to DuckDB) on the same CDM and
-- fails if they disagree.
-- Placeholders: {{cdm}} (schema holding the CDM), {{cohort}} (qualified cohort table), {{features_table}},
-- {{cohort_id}}. Ids stay BIGINT end to end.
CREATE OR REPLACE TABLE {{features_table}} AS
WITH cohort AS (
  SELECT subject_id, MIN(cohort_start_date) AS index_date
  FROM {{cohort}}
  WHERE cohort_definition_id = {{cohort_id}}
  GROUP BY subject_id
),
prior_visits AS (
  SELECT
    c.subject_id,
    c.index_date,
    v.visit_occurrence_id,
    v.visit_concept_id,
    v.visit_start_date,
    COALESCE(v.visit_end_date, v.visit_start_date) AS visit_end_date
  FROM cohort AS c
  JOIN {{cdm}}.visit_occurrence AS v ON v.person_id = c.subject_id
  WHERE v.visit_start_date < c.index_date
),
inpatient AS (
  SELECT * FROM prior_visits WHERE visit_concept_id IN (9201, 262)
),
readmissions AS (
  -- an inpatient stay in the look-back that began 0-30 days after the discharge of an earlier inpatient stay
  SELECT i.subject_id, COUNT(DISTINCT i.visit_occurrence_id) AS n
  FROM inpatient AS i
  JOIN inpatient AS p
    ON p.subject_id = i.subject_id
   AND p.visit_start_date < i.visit_start_date
   AND i.visit_start_date BETWEEN p.visit_end_date AND p.visit_end_date + 30
  WHERE i.visit_start_date >= i.index_date - 365
  GROUP BY i.subject_id
),
utilization AS (
  SELECT
    subject_id,
    COUNT(*) FILTER (WHERE visit_concept_id IN (9201, 262)) AS prior_ip_count,
    COUNT(*) FILTER (WHERE visit_concept_id = 9203) AS prior_ed_count,
    COUNT(*) FILTER (WHERE visit_concept_id IN (9202, 581385)) AS prior_os_count
  FROM prior_visits
  WHERE visit_start_date >= index_date - 365
  GROUP BY subject_id
)
SELECT
  CAST(c.subject_id AS BIGINT) AS subject_id,
  c.index_date,
  CAST(EXTRACT(YEAR FROM c.index_date) - p.year_of_birth AS BIGINT) AS age_at_index,
  CASE p.gender_concept_id WHEN 8507 THEN 'Male' WHEN 8532 THEN 'Female' ELSE 'Other/Unknown' END AS sex,
  CASE p.race_concept_id
    WHEN 8527 THEN 'White' WHEN 8516 THEN 'Black or African American' WHEN 8515 THEN 'Asian'
    WHEN 8657 THEN 'American Indian or Alaska Native' WHEN 8557 THEN 'Native Hawaiian or Other Pacific Islander'
    ELSE 'Other/Unknown' END AS race,
  CAST(COALESCE(u.prior_ip_count, 0) AS BIGINT) AS prior_ip_count,
  CAST(COALESCE(u.prior_ed_count, 0) AS BIGINT) AS prior_ed_count,
  CAST(COALESCE(u.prior_os_count, 0) AS BIGINT) AS prior_os_count,
  CAST(COALESCE(u.prior_ip_count, 0) + COALESCE(u.prior_ed_count, 0) + COALESCE(u.prior_os_count, 0) AS BIGINT) AS prior_ip_os_ed_count,
  CAST(COALESCE(r.n, 0) AS BIGINT) AS readmission_count_twelve_months
FROM cohort AS c
JOIN {{cdm}}.person AS p ON p.person_id = c.subject_id
LEFT JOIN utilization AS u ON u.subject_id = c.subject_id
LEFT JOIN readmissions AS r ON r.subject_id = c.subject_id
