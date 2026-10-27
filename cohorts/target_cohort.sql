/* ============================================================================================
   Target cohort: adult inpatient (IP) and ED-to-IP index stays, one per patient (FR-D1 / FR-D2)

   OHDSI SQL (SqlRender dialect). Translate with SqlRender::translate(sql, targetDialect = ...) for
   DuckDB, PostgreSQL, SQL Server, Oracle, Snowflake. Parameters:
     @cdm_database_schema     schema holding the OMOP CDM
     @cohort_database_schema  schema holding the cohort table
     @cohort_table            cohort table (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
     @target_cohort_id        cohort_definition_id written for this cohort
     @followup_days           verified follow-up after discharge (30)
     @min_age                 minimum age in years at discharge (18)

   Rules
     1. Index stay: visit_concept_id 9201 (Inpatient Visit) or 262 (Emergency Room and Inpatient Visit),
        with a visit_end_date (discharge date) and visit_end_date >= visit_start_date.
     2. Adult: YEAR(discharge) - year_of_birth >= @min_age.
     3. Discharge exclusions: discharged_to_concept_id = 4216643 (Patient died), or a death record on or before
        the discharge date.
     4. Verified follow-up: an observation_period covers the discharge date through discharge + @followup_days,
        or the person died within that window.
     5. One index per patient: the EARLIEST qualifying stay (ties broken by the lowest visit_occurrence_id).
        This is deterministic and needs no random number generator, so every engine selects the same stay.
     6. cohort_start_date = cohort_end_date = discharge date.
   ============================================================================================ */
DELETE FROM @cohort_database_schema.@cohort_table WHERE cohort_definition_id = @target_cohort_id;

INSERT INTO @cohort_database_schema.@cohort_table (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
SELECT @target_cohort_id AS cohort_definition_id,
       ranked.person_id AS subject_id,
       ranked.discharge_date AS cohort_start_date,
       ranked.discharge_date AS cohort_end_date
FROM (
    SELECT q.person_id,
           q.discharge_date,
           ROW_NUMBER() OVER (PARTITION BY q.person_id ORDER BY q.visit_start_date ASC, q.visit_occurrence_id ASC) AS rn
    FROM (
        SELECT v.person_id,
               v.visit_occurrence_id,
               v.visit_start_date,
               CAST(v.visit_end_date AS DATE) AS discharge_date
        FROM @cdm_database_schema.visit_occurrence v
        INNER JOIN @cdm_database_schema.person p ON p.person_id = v.person_id
        WHERE v.visit_concept_id IN (9201, 262)
          AND v.visit_end_date IS NOT NULL
          AND v.visit_end_date >= v.visit_start_date
          AND YEAR(v.visit_end_date) - p.year_of_birth >= @min_age
          AND COALESCE(v.discharged_to_concept_id, 0) <> 4216643
          AND NOT EXISTS (
              SELECT 1 FROM @cdm_database_schema.death d
              WHERE d.person_id = v.person_id AND d.death_date <= v.visit_end_date)
          AND (
              EXISTS (
                  SELECT 1 FROM @cdm_database_schema.observation_period op
                  WHERE op.person_id = v.person_id
                    AND op.observation_period_start_date <= v.visit_end_date
                    AND op.observation_period_end_date >= DATEADD(day, @followup_days, v.visit_end_date))
              OR EXISTS (
                  SELECT 1 FROM @cdm_database_schema.death d2
                  WHERE d2.person_id = v.person_id
                    AND d2.death_date > v.visit_end_date
                    AND d2.death_date <= DATEADD(day, @followup_days, v.visit_end_date))
          )
    ) q
) ranked
WHERE ranked.rn = 1;
