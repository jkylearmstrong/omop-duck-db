-- ============================================================================
-- OMOP DuckDB Cohort & End of Life / Mortality Engine
-- Generic SQL macros for in-hospital mortality, post-discharge mortality,
-- fixed-window EOL, and composite readmission or death outcomes.
-- ============================================================================

-- Macro: Check if discharge concept indicates death (4216643 = Died). 4155309 is SNOMED 'Ileal part' (an anatomic site), kept only
-- for the pinned legacy end-of-life convention; the PCORnet ETL no longer writes it (AMA is 32216, hospice 8546).
CREATE OR REPLACE MACRO is_expired(discharged_to_concept_id) AS
  (COALESCE(discharged_to_concept_id, 0) IN (4216643, 4155309));
