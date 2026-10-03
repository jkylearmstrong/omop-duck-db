-- omop-learn temporal Feature (DuckDB dialect): one row per procedure occurrence.
select
    cast(a.procedure_concept_id as varchar) || ' - procedure'
        || coalesce(' - ' || nullif(trim(c.concept_name), ''), '') as concept_name,
    a.procedure_date as feature_start_date
from
    {cdm_schema}.procedure_occurrence a
left join
    {vocab_schema}.concept c
on
    c.concept_id = a.procedure_concept_id
where
    a.person_id = {person_id}
    and a.procedure_concept_id <> 0
