-- Shared OMOP concept-mapping macros, used by both the R and Python PCORnet ETLs
-- so source-code -> standard-concept resolution logic lives in exactly one place.
--
-- Load once per DuckDB connection:
--   R:      dbExecute(con, paste(readLines(here::here("sql/mapping_macros.sql")), collapse = "\n"))
--   Python: con.execute(open("sql/mapping_macros.sql").read())
--
-- Requires the vocabulary tables (concept, concept_relationship) to already be
-- loaded -- see R/load_vocabulary.R.

-- Resolve a source vocabulary code (e.g. ICD10CM, RxNorm, LOINC, NDC) to its
-- standard concept_id via the "Maps to" relationship. Returns 0 (unmapped) if
-- no standard mapping exists, matching OMOP's convention for unmapped concepts.
CREATE OR REPLACE MACRO map_to_standard_concept_id(source_vocabulary_id, source_code) AS (
    COALESCE(
        (
            SELECT cr.concept_id_2
            FROM concept c
            JOIN concept_relationship cr
              ON cr.concept_id_1 = c.concept_id
             AND cr.relationship_id = 'Maps to'
            WHERE c.vocabulary_id = source_vocabulary_id
              AND c.concept_code = source_code
            LIMIT 1
        ),
        0
    )
);

-- Resolve a source vocabulary code to its own (non-standard) concept_id, for
-- populating *_source_concept_id columns (e.g. condition_source_concept_id).
-- Returns 0 if the code isn't found in the vocabulary at all.
CREATE OR REPLACE MACRO source_concept_id(source_vocabulary_id, source_code) AS (
    COALESCE(
        (
            SELECT concept_id
            FROM concept
            WHERE vocabulary_id = source_vocabulary_id
              AND concept_code = source_code
            LIMIT 1
        ),
        0
    )
);

-- Derive a surrogate integer ID from a source identifier (e.g. PCORnet PATID).
-- CDM v5.4's *_id columns are 32-bit `integer`, but DuckDB's hash() returns a
-- 64-bit UBIGINT -- this folds it into the 32-bit-safe range [0, 2000000000)
-- so both ETLs generate identical, non-overflowing surrogate keys.
CREATE OR REPLACE MACRO pcornet_id(source_id) AS (
    (hash(source_id) % 2000000000)::INTEGER
);
