"""omop_etl: PCORnet to OMOP CDM v5.4 ETL, vocabulary loaders, federation, cohort helpers, DQD, and ML feature extraction in DuckDB."""

from omop_etl.build_omop_cdm import (
    attach_central_vocabulary,
    build_condition_era,
    build_drug_era,
    build_observation_period,
    build_schema,
    etl_pcornet,
    load_care_site,
    load_location,
    load_cdm_source,
    load_condition_occurrence,
    load_death,
    load_drug_exposure,
    load_measurement,
    load_person,
    load_procedure_occurrence,
    load_provider,
    load_visit_occurrence,
    load_vital,
)
from omop_etl.cohort import (
    build_end_of_life_cohort,
    build_mortality_cohort,
    build_readmission_cohort,
    combine_cohorts,
    compute_attrition,
    create_cohort,
    ensure_cohort_tables,
    get_cohort_summary,
    get_concept_ancestors,
    get_concept_descendants,
    get_concept_relationships,
    resolve_concept_set,
)
from omop_etl.dqd import (
    run_dqd,
)
from omop_etl.export import (
    export_cdm,
)
from omop_etl.features import (
    aggregate_concept_sets,
    extract_measurements,
    extract_patient_features,
    extract_temporal_features,
)
from omop_etl.federation import (
    create_federated_consortium,
)
from omop_etl.ml_features import (
    as_omop_learn_batch,
    build_concept_tokenizer,
    extract_sard_visit_tensors,
    extract_sparse_concept_matrix,
)
from omop_etl.omop_learn_backend import (
    DuckDBBackend,
    cohort_frame_from_parquet,
    cohort_from_parquet,
    default_features,
)
from omop_etl.mapping import (
    export_unmapped_codes,
    import_source_to_concept_map,
    import_usagi_mappings,
)
from omop_etl.remapping import (
    remap_all,
    remap_cdm_table,
)
from omop_etl.table1 import (
    DEFAULT_LAB_LOINCS,
    DEFAULT_MEDICATION_CONCEPTS,
    DEFAULT_VITAL_LOINCS,
    generate_table1,
    validate_table1_reconciliation,
)
from omop_etl.vocabulary import (
    check_vocabulary_version,
    load_vocabulary,
)

__all__ = [
    "attach_central_vocabulary",
    "build_schema",
    "create_federated_consortium",
    "etl_pcornet",
    "load_care_site",
    "load_location",
    "load_provider",
    "load_person",
    "load_visit_occurrence",
    "load_condition_occurrence",
    "load_procedure_occurrence",
    "load_measurement",
    "load_vital",
    "load_drug_exposure",
    "load_death",
    "build_observation_period",
    "build_drug_era",
    "build_condition_era",
    "load_cdm_source",
    "load_vocabulary",
    "check_vocabulary_version",
    "import_source_to_concept_map",
    "import_usagi_mappings",
    "export_unmapped_codes",
    "remap_cdm_table",
    "remap_all",
    # Cohort & Hierarchy Helpers
    "ensure_cohort_tables",
    "get_concept_descendants",
    "get_concept_ancestors",
    "get_concept_relationships",
    "resolve_concept_set",
    "create_cohort",
    "compute_attrition",
    "combine_cohorts",
    "get_cohort_summary",
    "build_readmission_cohort",
    "build_end_of_life_cohort",
    "build_mortality_cohort",
    # DQD Engine
    "run_dqd",
    # Feature Extractors & Concept Aggregators
    "extract_patient_features",
    "extract_temporal_features",
    "aggregate_concept_sets",
    "extract_measurements",
    # omop-learn / SARD / PLP bridge: set-based sparse & sequence feature extraction
    "extract_sparse_concept_matrix",
    "extract_sard_visit_tensors",
    "as_omop_learn_batch",
    "build_concept_tokenizer",
    "DuckDBBackend",
    "default_features",
    "cohort_from_parquet",
    "cohort_frame_from_parquet",
    # Table 1 & Reconciliation
    "generate_table1",
    "validate_table1_reconciliation",
    "DEFAULT_LAB_LOINCS",
    "DEFAULT_VITAL_LOINCS",
    "DEFAULT_MEDICATION_CONCEPTS",
    # Multi-Target Export
    "export_cdm",
]

__version__ = "0.5.1"

