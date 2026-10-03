-- omop-learn NON-temporal Feature (DuckDB dialect). Male = 1, female = 0, anything else = -1.
select
    case
        when a.gender_concept_id = 8507 then 1
        when a.gender_concept_id = 8532 then 0
        else -1
    end as ntmp_val,
    'Gender M(1)/F(0)' as concept_name
from
    {cdm_schema}.person a
where
    a.person_id = {person_id}
