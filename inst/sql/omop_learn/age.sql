-- omop-learn NON-temporal Feature (DuckDB dialect). Columns, by position: value, feature name.
-- The feature name is constant so every patient record in data.json carries the same keys
-- (omop-learn's torch dataset requires identical keys per patient). Missing year of birth -> -1.
select
    coalesce(date_part('year', cast('{end_date}' as date)) - a.year_of_birth, -1) as ntmp_val,
    'Age at end_date' as concept_name
from
    {cdm_schema}.person a
where
    a.person_id = {person_id}
