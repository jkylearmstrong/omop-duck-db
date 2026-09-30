-- ============================================================================
-- OMOP DuckDB Cohort & 30-Day Readmission Engine
-- Generic SQL macros and parameterized templates for index stay selection,
-- verified follow-up window (lost-to-follow-up filter), and 30-day readmissions.
-- ============================================================================

-- Macro: Calculate Length of Stay in days
CREATE OR REPLACE MACRO calc_los_days(start_date, end_date) AS 
  date_diff('day', CAST(start_date AS DATE), CAST(end_date AS DATE));

-- Macro: Calculate Age at reference date
CREATE OR REPLACE MACRO calc_age_at_date(birth_year, birth_month, birth_day, ref_date) AS 
  date_diff('year', make_date(birth_year, COALESCE(birth_month, 1), COALESCE(birth_day, 1)), CAST(ref_date AS DATE));

-- Macro: Standard Adult check (Age >= 18)
CREATE OR REPLACE MACRO is_adult(birth_year, birth_month, birth_day, ref_date) AS 
  (date_diff('year', make_date(birth_year, COALESCE(birth_month, 1), COALESCE(birth_day, 1)), CAST(ref_date AS DATE)) >= 18);

-- Macro: Discharged alive check (OMOP concept 4216643 = 'Patient died')
CREATE OR REPLACE MACRO is_discharged_alive(discharged_to_concept_id) AS 
  (COALESCE(discharged_to_concept_id, 0) != 4216643);

-- Macro: Categorize discharge destination into standard groups
CREATE OR REPLACE MACRO categorize_discharge(discharged_to_concept_id) AS
  CASE 
    WHEN discharged_to_concept_id IN (8536, 4132319) THEN 'Home'
    WHEN discharged_to_concept_id IN (581476) THEN 'Home Health'
    WHEN discharged_to_concept_id IN (38004284, 8676, 8863, 8920, 38004285) THEN 'SNF/Rehab'
    WHEN discharged_to_concept_id IN (44814650) THEN 'AMA'
    WHEN discharged_to_concept_id = 4216643 THEN 'Expired'
    WHEN discharged_to_concept_id IS NULL THEN 'Unknown'
    ELSE 'Other'
  END;

-- Macro: Age Group Categorization (18-44, 45-64, 65-74, 75+)
CREATE OR REPLACE MACRO categorize_age_group(age) AS
  CASE 
    WHEN age < 18 THEN '<18'
    WHEN age BETWEEN 18 AND 44 THEN '18-44'
    WHEN age BETWEEN 45 AND 64 THEN '45-64'
    WHEN age BETWEEN 65 AND 74 THEN '65-74'
    WHEN age >= 75 THEN '75+'
    ELSE 'Unknown'
  END;
