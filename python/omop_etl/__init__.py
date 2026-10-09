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
    ConsortAttrition,
    build_end_of_life_cohort,
    build_mortality_cohort,
    build_readmission_cohort,
    build_treatment_episodes,
    combine_cohorts,
    compute_attrition,
    create_cohort,
    define_study_cohort,
    ensure_cohort_tables,
    generate_consort_attrition,
    get_cohort_summary,
    get_concept_ancestors,
    get_concept_descendants,
    get_concept_relationships,
    prepare_competing_risks_data,
    resolve_concept_set,
)
from omop_etl.circe import (
    compile_circe_to_duckdb,
    execute_circe_cohort,
)
from omop_etl.cluster import (
    build_cluster_command,
    cluster_submit,
)
from omop_etl.comorbidity import (
    extract_charlson_index,
    extract_elixhauser_comorbidities,
)
from omop_etl.risk_scores import (
    calculate_bedside_scores,
)
from omop_etl.dqd import (
    run_dqd,
    sanitize_measurements,
)
from omop_etl.export import (
    export_cdm,
    export_to_parquet,
)
from omop_etl.features import (
    CORE_14_LAB_PANEL,
    aggregate_concept_sets,
    extract_measurements,
    extract_patient_features,
    extract_standard_labs,
    extract_temporal_features,
)
from omop_etl.federation import (
    check_cross_database_discrepancy,
    create_federated_consortium,
    with_cell_suppression,
    with_differential_privacy,
)
from omop_etl.ml import (
    CohortExtractor,
    FeatureMatrixBuilder,
)
from omop_etl.ml_features import (
    arrow_to_cuda_tensors,
    arrow_to_pytorch,
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
from omop_etl.phenotypes import (
    get_phenotype_concept_set,
    list_available_phenotypes,
)
from omop_etl.propensity import (
    PropensityResult,
    generate_propensity_weights,
)
from omop_etl.survival import (
    SurvivalResult,
    estimate_km_survival,
)
from omop_etl.lakehouse import (
    omop_connect_lakehouse,
)
from omop_etl.remapping import (
    auto_remap_unmapped,
    remap_all,
    remap_cdm_table,
)
from omop_etl.table1 import (
    DEFAULT_LAB_LOINCS,
    DEFAULT_MEDICATION_CONCEPTS,
    DEFAULT_VITAL_LOINCS,
    export_table1,
    generate_table1,
    validate_table1_reconciliation,
)
from omop_etl.vocabulary import (
    check_vocabulary_version,
    load_vocabulary,
    omop_connect,
)

__all__ = [
    "attach_central_vocabulary",
    "omop_connect",
    "build_schema",
    "create_federated_consortium",
    "with_cell_suppression",
    "with_differential_privacy",
    "check_cross_database_discrepancy",
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
    "auto_remap_unmapped",
    # Cohort & Hierarchy Helpers
    "ensure_cohort_tables",
    "get_concept_descendants",
    "get_concept_ancestors",
    "get_concept_relationships",
    "resolve_concept_set",
    "create_cohort",
    "define_study_cohort",
    "compute_attrition",
    "ConsortAttrition",
    "generate_consort_attrition",
    "build_treatment_episodes",
    "combine_cohorts",
    "get_cohort_summary",
    "build_readmission_cohort",
    "build_end_of_life_cohort",
    "build_mortality_cohort",
    "prepare_competing_risks_data",
    # DQD Engine
    "run_dqd",
    "sanitize_measurements",
    # Comorbidity & Risk Score Profilers
    "extract_elixhauser_comorbidities",
    "extract_charlson_index",
    "calculate_bedside_scores",
    # Feature Extractors & Concept Aggregators
    "extract_patient_features",
    "extract_temporal_features",
    "aggregate_concept_sets",
    "extract_measurements",
    "extract_standard_labs",
    "CORE_14_LAB_PANEL",
    # ML Feature Extraction & Builders (RFC-6)
    "CohortExtractor",
    "FeatureMatrixBuilder",
    # omop-learn / SARD / PLP bridge: set-based sparse & sequence feature extraction
    "extract_sparse_concept_matrix",
    "extract_sard_visit_tensors",
    "as_omop_learn_batch",
    "arrow_to_pytorch",
    "arrow_to_cuda_tensors",
    "build_concept_tokenizer",
    "DuckDBBackend",
    "default_features",
    "cohort_from_parquet",
    "cohort_frame_from_parquet",
    # Table 1 & Reconciliation
    "generate_table1",
    "export_table1",
    "validate_table1_reconciliation",
    "DEFAULT_LAB_LOINCS",
    "DEFAULT_VITAL_LOINCS",
    "DEFAULT_MEDICATION_CONCEPTS",
    # CIRCE / Atlas Compiler
    "compile_circe_to_duckdb",
    "execute_circe_cohort",
    # Pre-compiled Phenotype Bundles
    "get_phenotype_concept_set",
    "list_available_phenotypes",
    # Propensity & IPTW Weighting
    "generate_propensity_weights",
    "PropensityResult",
    # Survival & Competing Risks CIF
    "estimate_km_survival",
    "SurvivalResult",
    # Lakehouse Connector
    "omop_connect_lakehouse",
    # Cluster Execution
    "cluster_submit",
    "build_cluster_command",
    # Multi-Target Export
    "export_cdm",
    "export_to_parquet",
]

__version__ = "0.5.5"


