-- Shared OMOP concept-mapping macros, used by both the R and Python PCORnet ETLs
-- so source-code -> standard-concept resolution logic lives in exactly one place.
--
-- Load once per DuckDB connection:
--   R:      dbExecute(con, paste(readLines(here::here("sql/mapping_macros.sql")), collapse = "\n"))
--   Python: con.execute(open("sql/mapping_macros.sql").read())
--
-- Requires the vocabulary tables (concept, concept_relationship, concept_ancestor)
-- to already be loaded -- see R/load_vocabulary.R -- or reachable through an
-- attached vocabulary database (omop_connect()). DuckDB validates the body of some
-- macros when they are *created*: map_to_standard_concept_id() and
-- source_concept_id() cannot be created while the table (or a column) they read is
-- not visible on the connection, whereas descendants_of() / ancestors_of() are
-- created regardless and fail when called without concept_ancestor. Once created,
-- the names are looked up again on every call through the connection's
-- search_path, so the table may live in the primary database or in an attached one.
--
-- Every definition must stay a `CREATE OR REPLACE MACRO` statement: against a
-- read-only database the loaders rewrite it to `CREATE OR REPLACE TEMP MACRO`
-- (load_macros() in Python, load_mapping_macros() in R).

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

-- Multi-format date parsing macro to prevent silent data loss (NULLs) across
-- disparate PCORnet date formats (e.g. '01JAN2020', '2020-01-01', '01/01/2020').
CREATE OR REPLACE MACRO parse_omop_date(col) AS (
    COALESCE(
        TRY_CAST(col AS DATE),
        TRY_STRPTIME(col, '%d%b%Y'),
        TRY_STRPTIME(col, '%d-%b-%Y'),
        TRY_STRPTIME(col, '%Y-%m-%d'),
        TRY_STRPTIME(col, '%m/%d/%Y'),
        TRY_CAST(TRY_CAST(col AS TIMESTAMP) AS DATE)
    )
);

-- Multi-format timestamp parsing macro
CREATE OR REPLACE MACRO parse_omop_datetime(date_col, time_col) AS (
    COALESCE(
        TRY_CAST(date_col || ' ' || COALESCE(time_col, '00:00:00') AS TIMESTAMP),
        TRY_STRPTIME(date_col || ' ' || COALESCE(time_col, '00:00:00'), '%d%b%Y %H:%M:%S'),
        TRY_STRPTIME(date_col || ' ' || COALESCE(time_col, '00:00:00'), '%d%b%Y %H:%M'),
        TRY_CAST(parse_omop_date(date_col) AS TIMESTAMP)
    )
);

-- Concept ancestry macros (RFC 1.2): one-line access to the OMOP concept hierarchy.
--
-- descendants_of(ancestor_id) -> TABLE (concept_id)
--   Every concept at or below `ancestor_id`, read from concept_ancestor.
-- ancestors_of(descendant_id) -> TABLE (concept_id)
--   Every concept at or above `descendant_id`, read from concept_ancestor.
--
-- Athena's concept_ancestor already holds a self row (ancestor = descendant, 0
-- levels of separation) for every standard concept, so both macros include the
-- concept itself; they do not add one. An id with no rows (unknown, or a
-- non-standard concept that is not in concept_ancestor) and NULL both return an
-- empty set. concept_ancestor is resolved at call time through the connection's
-- search_path, so it may live in the primary database or in an attached
-- vocabulary (see omop_connect()).
--
--   SELECT * FROM condition_occurrence
--   WHERE condition_concept_id IN (SELECT concept_id FROM descendants_of(201826));
--
-- Two name-capture hazards shape these definitions. DuckDB pastes the caller's
-- argument expression into the macro body, so an unqualified column in that
-- expression is resolved in the scope where it lands:
--   * Parameter names are deliberately not column names of concept_ancestor
--     (ancestor_concept_id, descendant_concept_id, min/max_levels_of_separation):
--     DuckDB substitutes a macro parameter for any identically-named column in
--     the body, which would silently turn the filter into a tautology.
--   * The argument is therefore evaluated in a derived table of its own, which
--     has no columns in scope, instead of in the WHERE clause next to
--     concept_ancestor and the output column `concept_id`. Pasted into that
--     WHERE clause, an unqualified caller column such as `concept_id` (the most
--     common column name in OMOP tables) or `ancestor_concept_id` would bind to
--     the macro's own column instead of the caller's, and the filter would
--     silently match only the self rows (or everything). Written this way,
--     descendants_of(concept_id) is a correlated call on the caller's column,
--     exactly like descendants_of(t.concept_id).
CREATE OR REPLACE MACRO descendants_of(ancestor_id) AS TABLE
SELECT ca.descendant_concept_id AS concept_id
FROM (SELECT ancestor_id) AS arg(id)
JOIN concept_ancestor AS ca ON ca.ancestor_concept_id = arg.id;

CREATE OR REPLACE MACRO ancestors_of(descendant_id) AS TABLE
SELECT ca.ancestor_concept_id AS concept_id
FROM (SELECT descendant_id) AS arg(id)
JOIN concept_ancestor AS ca ON ca.descendant_concept_id = arg.id;

-- Physiologic range clamping (RFC 6.1): clamp_physiologic(val, min_val, max_val)
-- winsorises `val` into the closed interval [min_val, max_val].
--   * NULL val stays NULL (a missing measurement is never invented).
--   * Both bounds are inclusive: a value equal to a bound is returned unchanged.
--   * A NULL bound leaves that side open, e.g. clamp_physiologic(x, 0, NULL)
--     only floors at 0.
--   * min_val > max_val is a caller error and raises (the result would
--     otherwise depend on which side of the inverted range a value fell on).
--     It raises even for a NULL val, but not on empty input.
--   * DuckDB orders NaN above every number, so a NaN DOUBLE clamps to max_val.
CREATE OR REPLACE MACRO clamp_physiologic(val, min_val, max_val) AS (
    CASE
        WHEN min_val > max_val THEN error(
            'clamp_physiologic: min_val (' || min_val || ') is greater than max_val (' || max_val || ')'
        )
        WHEN val IS NULL THEN NULL
        WHEN val < min_val THEN min_val
        WHEN val > max_val THEN max_val
        ELSE val
    END
);
