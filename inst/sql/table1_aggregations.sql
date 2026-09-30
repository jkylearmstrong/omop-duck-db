-- ============================================================================
-- OMOP DuckDB Table 1 & Statistical Aggregation Macros
-- Standardized Mean Difference (SMD), distribution statistics, and
-- missingness metrics for epidemiologic Table 1 reporting.
-- ============================================================================

-- Macro: Standardized Mean Difference (Cohen's d) for continuous features
CREATE OR REPLACE MACRO calc_smd_continuous(mean1, sd1, mean2, sd2) AS 
  ABS(mean1 - mean2) / NULLIF(SQRT((POWER(COALESCE(sd1, 0.0), 2) + POWER(COALESCE(sd2, 0.0), 2)) / 2.0), 0.0);

-- Macro: Standardized Mean Difference for binary proportions
CREATE OR REPLACE MACRO calc_smd_binary(p1, p0) AS 
  ABS(p1 - p0) / NULLIF(SQRT((p1 * (1.0 - p1) + p0 * (1.0 - p0)) / 2.0), 0.0);

-- Macro: BMI Category
CREATE OR REPLACE MACRO categorize_bmi(bmi) AS
  CASE 
    WHEN bmi IS NULL THEN 'Missing'
    WHEN bmi < 18.5 THEN '<18.5 (Underweight)'
    WHEN bmi BETWEEN 18.5 AND 24.99 THEN '18.5-24.9 (Normal)'
    WHEN bmi BETWEEN 25.0 AND 29.99 THEN '25.0-29.9 (Overweight)'
    WHEN bmi >= 30.0 THEN '>=30.0 (Obese)'
    ELSE 'Unknown'
  END;

-- Macro: Tobacco Use Category
CREATE OR REPLACE MACRO categorize_tobacco(code_or_name) AS
  CASE 
    WHEN LOWER(CAST(code_or_name AS VARCHAR)) LIKE '%current%' THEN 'Current'
    WHEN LOWER(CAST(code_or_name AS VARCHAR)) LIKE '%former%' THEN 'Former'
    WHEN LOWER(CAST(code_or_name AS VARCHAR)) LIKE '%never%' THEN 'Never'
    ELSE 'Unknown'
  END;
