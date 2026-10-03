-- omop-learn temporal Feature (DuckDB dialect): one row per condition occurrence.
-- Columns, by position: concept token, event date. Placeholders {cdm_schema}, {vocab_schema}, {person_id}
-- are filled in by omop_etl.omop_learn_backend.DuckDBBackend. Concept 0 ("No matching concept") is excluded.
select
    cast(a.condition_concept_id as varchar) || ' - condition'
        || coalesce(' - ' || nullif(trim(c.concept_name), ''), '') as concept_name,
    a.condition_start_date as feature_start_date
from
    {cdm_schema}.condition_occurrence a
left join
    {vocab_schema}.concept c
on
    c.concept_id = a.condition_concept_id
where
    a.person_id = {person_id}
    and a.condition_concept_id <> 0
