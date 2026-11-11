"""omop_etl: PCORnet to OMOP CDM v5.4 ETL, vocabulary loaders, federation, cohort helpers, DQD, and ML feature extraction in DuckDB."""

from omop_etl.build_omop_cdm import (
    attach_central_vocabulary,
    build_condition_era,
    build_drug_era,
    attach_read_only,
    checkpoint_database,
    build_observation_period,
    cap_observation_period,
    materialize_vocabulary,
    build_safe_source_view,
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
    cohort_attrition,
    build_ederri_diabetes_cohort,
    build_end_of_life_cohort,
    build_mortality_cohort,
    build_readmission_cohort,
    build_treatment_episodes,
    combine_cohorts,
    compute_attrition,
    remap_discharge_status,
    check_observation_period_ends,
    create_cohort,
    define_study_cohort,
    persist_index_visits,
    ensure_cohort_tables,
    generate_consort_attrition,
    export_consort_flowchart,
    get_cohort_summary,
    get_concept_ancestors,
    get_concept_descendants,
    get_concept_relationships,
    prepare_competing_risks_data,
    resolve_concept_set,
    DEFAULT_INPATIENT_VISIT_TYPE_CONCEPT_IDS,
    DEFAULT_OUTPATIENT_VISIT_TYPE_CONCEPT_IDS,
    DEFAULT_EMERGENCY_VISIT_TYPE_CONCEPT_IDS,
    VISIT_PROVENANCE_MAP,
    DEFAULT_PRIMARY_CONDITION_TYPE_CONCEPT_IDS,
    DEFAULT_ADMITTING_CONDITION_TYPE_CONCEPT_IDS,
    DEFAULT_PROBLEM_LIST_CONDITION_TYPE_CONCEPT_IDS,
    CONDITION_PROVENANCE_MAP,
    DEFAULT_LAB_RESULT_MEASUREMENT_TYPE_CONCEPT_IDS,
    DEFAULT_ORDER_MEASUREMENT_TYPE_CONCEPT_IDS,
    DEFAULT_POC_MEASUREMENT_TYPE_CONCEPT_IDS,
    MEASUREMENT_PROVENANCE_MAP,
    filter_visits,
    filter_conditions,
    filter_measurements,
)
from omop_etl.circe import (
    compile_circe_to_duckdb,
    execute_circe_cohort,
    export_to_circe_json,
)
from omop_etl.cluster import (
    build_cluster_command,
    cluster_submit,
)
from omop_etl.comorbidity import (
    extract_charlson_index,
    extract_elixhauser_comorbidities,
)
from omop_etl.scoring import FeatureSchemaError, OMOPScoringPipeline
from omop_etl.utilization import extract_prior_utilization
from omop_etl.measurement_filter import (
    DEFAULT_PHYSIOLOGIC_BOUNDS,
    POPULATION_PHYSIOLOGIC_BOUNDS,
    filter_bounds,
    harmonize_anthropometrics,
    harmonize_measurement_units,
)
from omop_etl.medication_classes import (
    MedicationExtractor,
    MedicationTransitions,
    extract_medication_transitions,
)
from omop_etl.table1_profiles import DOMAIN_PROFILES, Table1Generator
from omop_etl.table1 import generate_table1, export_table1, table1_to_quarto, table1_to_typst
from omop_etl.odds_ratios import generate_table3_odds_ratios, export_table3  # noqa: E402
from omop_etl.model_bundle import export_to_onnx  # noqa: E402
from omop_etl.cohort_flow import (
    CohortFlow,
    Table2Generator,
    as_plp_arrow,
    plot_consort_flow,
    plot_multisite_attrition_heatmap,
)
from omop_etl.risk_scores import (
    calculate_bedside_scores,
)
from omop_etl.scores import (
    calculate_hospital_score,
    calculate_lace_index,
)
from omop_etl.dqd import (
    run_dqd,
    sanitize_measurements,
)
from omop_etl.export import (
    export_cdm,
    export_to_parquet,
)
from omop_etl.paths import (
    attach_database,
    copy_to,
    read_csv_sql,
    read_parquet_sql,
    sanitize_sql_path,
    sql_path_literal,
)
from omop_etl.resources import (
    ResourceConfig,
    configure_resources,
    detect_system_ram,
)
from omop_etl.datetime_utils import (
    DEFAULT_DATETIME_FORMATS,
    install_vectorized_strptime,
    normalize_time_sql,
    vectorized_strptime,
)
from omop_etl.wide_measurement import (
    DEFAULT_VITAL_CONCEPT_MAPPING,
    DEFAULT_VITAL_PLAUSIBILITY_FILTERS,
    load_wide_measurement,
)
from omop_etl.medication_classes import (
    DEFAULT_ADMINISTERED_TYPE_CONCEPT_IDS,
    DEFAULT_DISPENSED_TYPE_CONCEPT_IDS,
    DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS,
    assert_exposure_coverage,
    audit_combination_products,
    audit_medication_strategies,
    compute_adherence_pdc,
    compute_drug_eras,
    explain_medication_disagreement,
    export_medication_crosswalk,
    extract_medication_classes,
)
from omop_etl.quality import (
    audit_code_list_coverage,
    detect_redundant_features,
    detect_unit_discordance,
    dqd_summary,
    normalize_units,
    panel_missingness_audit,
    plausibility_report,
    suggest_source_mappings,
)
from omop_etl.rollups import (
    ConceptPattern,
    HierarchicalRollup,
    RollupEngine,
    audit_concept_drift,
    materialize_crosswalk,
)
from omop_etl.reconciliation import (
    cohort_from_index_stays,
    compare_feature_tables,
    cohort_overlap,
    export_companion_manifest,
    run_study_grid,
)
from omop_etl.integrity import (
    leakage_scan,
    add_missing_indicators,
    missingness_by_outcome,
    provenance,
    check_vocabulary_provenance,
    numbers_of_record,
    scan_document_numbers,
    vocabulary_preflight,
    generate_site_patient_tokens,
    consortium_patient_overlap,
    consortium_cross_site_duplicates,
    consortium_duplicate_report,
    planned_readmission_filter,
    observation_period_diagnostics,
)
from omop_etl.phenotype_dsl import (
    ConditionCriteria,
    DrugCriteria,
    Everyone,
    Intersect as PhenotypeIntersect,
    MeasurementCriteria,
    Not as PhenotypeNot,
    PhenotypeExpr,
    Union as PhenotypeUnion,
    compile_phenotype,
    execute_phenotype,
    phenotype_from_dict,
)
from omop_etl.plp_study_package import (
    export_plp_study_package,
)
from omop_etl.andromeda import (
    AndromedaAdapter,
    export_to_andromeda,
)
from omop_etl.cohort_sql import compare_cohort_definitions, render_cohort_sql, run_cohort_sql, translate_cohort_sql, translate_to_duckdb
from omop_etl.key_checks import KeyCollisionError, check_key_collisions
from omop_etl.features import (
    ACUTE_LAB_PANEL,
    CORE_14_LAB_PANEL,
    aggregate_concept_sets,
    extract_features,
    extract_inpatient_labs,
    extract_measurements,
    extract_patient_features,
    extract_standard_labs,
    ANION_GAP_LOINCS,
    EGFR_LOINCS,
    extract_temporal_features,
)
from omop_etl.federation import (
    ConsortiumScoreResult,
    FederatedConsortium,
    FederatedMetaAnalysisResult,
    check_cross_database_discrepancy,
    create_federated_consortium,
    federated_meta_analysis,
    with_cell_suppression,
    with_differential_privacy,
    PrivacyAuditResult,
    audit_cell_suppression,
    consortium_privacy_audit,
)
from omop_etl.cohort_diagnostics import (
    ConsortiumCohortDiagnostics,
    consortium_cohort_diagnostics,
)
from omop_etl.ml import (
    CohortExtractor,
    ConsortiumCalibration,
    ConsortiumCrossSiteValidation,
    ConsortiumExplainability,
    ConsortiumRiskFactors,
    DecisionCurveAnalysis,
    FeatureMatrixBuilder,
    consortium_calibration,
    consortium_cross_site_validation,
    consortium_explainability,
    consortium_multivariable_risk_factors,
    decision_curve_analysis,
)
from omop_etl.manuscript import (
    ConsortiumManuscript,
    build_manuscript_scaffold,
)
from omop_etl.ml_features import (
    ClinicalSequenceTokens,
    arrow_to_cuda_tensors,
    arrow_to_pytorch,
    as_omop_learn_batch,
    build_concept_tokenizer,
    export_clinical_sequence_tokens,
    extract_sard_visit_tensors,
    extract_sparse_concept_matrix,
    impute_encounter_observations,
    to_clinical_sequence_tokens,
)
from omop_etl.study_report import (
    build_study_tables,
    explain_models,
    export_workbook,
    index_stay_dates,
    los_by_site,
    plot_cohort_flowchart,
    plot_shap_beeswarm,
    plot_shap_panels,
    random_stay_draw,
    seed_sensitivity,
    stay_type_auc,
    tune_models,
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
    consortium_propensity_score,
    match_nearest_neighbor,
    plot_love_plot,
)
from omop_etl.survival import (
    CIFResult,
    FineGrayResult,
    SurvivalResult,
    estimate_cif,
    estimate_km_survival,
    fine_gray_estimator,
    plot_cif,
)
from omop_etl.negative_controls import (
    EmpiricalCalibrationResult,
    calibrate_estimates,
    generate_negative_controls,
)
from omop_etl.concept_optimizer import (
    optimize_concept_set,
)
from omop_etl.trajectories import (
    extract_acute_trajectories,
    extract_temporal_trajectories,
)
from omop_etl.achilles import ACHILLES_ANALYSES, achilles_analysis_catalog, run_achilles
from omop_etl.categorical_encoding import encode_categoricals, encoded_column_names
from omop_etl.exclusions import find_metformin_only_prediabetes_pcos
from omop_etl.geography import (
    assign_sdi_quintile,
    derive_geography_features,
    fit_sdi_cut_points,
    read_ruca_crosswalk,
    read_sdi_table,
    ruca_category,
    validate_sdi_cut_points,
    write_geography_measurements,
)
from omop_etl.study_features import (
    FeatureSet,
    StudyFeature,
    area_feature,
    concept_feature,
    custom_feature,
    feature_list,
    ruca_feature,
    sdi_feature,
    sql_feature,
    stays_from_cohort,
)
from omop_etl.census_acs import acs_feature, acs_zip3_table, fetch_acs_zcta
from omop_etl.model_ablation import ablate_feature_groups, compare_model_runs, ridge_logistic_fit_predict
from omop_etl.cohort_sql import cohort_definitions_dir
from omop_etl.bundle_concepts import stamp_feature_spec_concepts, validate_bundle_concepts
from omop_etl.cohort_reconcile import explain_cohort_gaps, reconcile_cohort, suppress_small_cells
from omop_etl.custom_covariates import (check_custom_concepts, custom_concept_registry, export_custom_concept_registry,
                                        register_custom_concepts, write_custom_covariates, write_custom_features)
from omop_etl.dqd_catalog import run_dqd_catalog, run_dqd_full
from omop_etl.bigquery_sync import read_bigquery_table, sync_vocabulary_from_bigquery
from omop_etl.federated_learning import FederatedFitResult, FederatedNode, federated_cox, federated_logistic_regression
from omop_etl.federated_survival import (
    FederatedSurvivalResult,
    consortium_survival_meta_analysis,
)
from omop_etl.synthetic import (
    generate_synthetic_omop_twin,
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
from omop_etl.rollups import (
    ConceptPattern,
    HierarchicalRollup,
    RollupEngine,
    audit_concept_drift,
    compute_file_sha256,
    materialize_crosswalk,
)

from omop_etl.extract import from_sql, import_omop  # noqa: E402
from omop_etl.feature_contract import validate_feature_frame  # noqa: E402
from omop_etl.model_bundle import load_model_bundle, score_cohort  # noqa: E402
from omop_etl.encounter import score_encounter  # noqa: E402
from omop_etl.drift_report import write_drift_report  # noqa: E402
from omop_etl.fhir import LOINC_TO_FEATURE, VOCABULARY_BY_SYSTEM, resolve_codes, resolve_fhir_codes, score_fhir_bundle  # noqa: E402
from omop_etl.unit_guard import REFERENCE_MEDIANS, check_unit_sanity, harmonize_contract_units  # noqa: E402
from omop_etl.monitoring import (  # noqa: E402
    calibration_summary,
    monitor_scores,
    population_stability_index,
    update_monitoring_log,
)
from omop_etl.risk_tiers import fit_capacity_thresholds, map_risk_tiers, tier_load  # noqa: E402
from omop_etl.run_manifest import RunManifest, verify_manifest, vocabulary_info  # noqa: E402
from omop_etl.disclosure import (  # noqa: E402
    DEFAULT_MIN_CELL, DisclosureError, assert_disclosure_safe, check_disclosure, format_count, small_cell_mask,
    suppress_attrition_table, suppress_small_counts,
)
from omop_etl.consort import consort_from_counts, consort_table_to_csv, pool_consort_tables  # noqa: E402
from omop_etl.parity import ParityAudit, audit_matrix_parity  # noqa: E402
from omop_etl.table1_pooled import build_table1, pool_table1_summaries, summarize_table1_site  # noqa: E402
from omop_etl.site_export import SiteSummaryError, assert_aggregate_only, export_site_summary, pool_site_packages  # noqa: E402
from omop_etl.mapping_coverage import mapping_coverage_report  # noqa: E402


def run_bigquery_demo(*args, **kwargs):
    """Runs the CMS SynPUF BigQuery smoke test (alias of :func:`omop_etl.demo_bigquery.run_demo`; imported lazily)."""
    from omop_etl.demo_bigquery import run_demo

    return run_demo(*args, **kwargs)

from omop_etl.source_check import check_source  # noqa: E402
from omop_etl.telemetry import aggregate_telemetry, site_telemetry  # noqa: E402
from omop_etl.temporal_split import split_by_date  # noqa: E402
from omop_etl.velocity import extract_velocity_features  # noqa: E402

__all__ = [
    "from_sql", "import_omop", "validate_feature_frame", "load_model_bundle", "score_cohort", "RunManifest",
    "score_encounter", "score_fhir_bundle", "resolve_codes", "resolve_fhir_codes", "VOCABULARY_BY_SYSTEM", "LOINC_TO_FEATURE", "REFERENCE_MEDIANS", "write_drift_report", "check_unit_sanity", "harmonize_contract_units", "calibration_summary", "monitor_scores", "population_stability_index", "update_monitoring_log",
    "fit_capacity_thresholds", "map_risk_tiers", "tier_load",
    "verify_manifest", "check_source", "aggregate_telemetry", "site_telemetry", "split_by_date",
    "extract_velocity_features",
    "DEFAULT_PHYSIOLOGIC_BOUNDS", "POPULATION_PHYSIOLOGIC_BOUNDS", "filter_bounds",
    "harmonize_measurement_units", "harmonize_anthropometrics", "MedicationExtractor",
    "MedicationTransitions", "extract_medication_transitions",
    "calculate_lace_index", "calculate_hospital_score",
    "DOMAIN_PROFILES", "Table1Generator", "CohortFlow", "Table2Generator", "as_plp_arrow",
    "plot_consort_flow", "plot_multisite_attrition_heatmap",
    "generate_table1", "export_table1", "table1_to_quarto", "table1_to_typst", "generate_table3_odds_ratios", "export_table3", "export_to_onnx",
    "ACUTE_LAB_PANEL",
    "extract_inpatient_labs",
    "extract_prior_utilization",
    "OMOPScoringPipeline",
    "FeatureSchemaError",
    "attach_central_vocabulary",
    "omop_connect",
    "build_schema",
    "create_federated_consortium",
    "FederatedConsortium",
    "with_cell_suppression",
    "with_differential_privacy",
    "PrivacyAuditResult",
    "audit_cell_suppression",
    "consortium_privacy_audit",
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
    "attach_read_only",
    "checkpoint_database",
    "build_observation_period",
    "cap_observation_period",
    "cohort_attrition",
    "materialize_vocabulary",
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
    "persist_index_visits",
    "compute_attrition",
    "remap_discharge_status",
    "ConsortAttrition",
    "generate_consort_attrition",
    "export_consort_flowchart",
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
    "ANION_GAP_LOINCS",
    "EGFR_LOINCS",
    "check_key_collisions",
    "compare_cohort_definitions",
    "render_cohort_sql",
    "run_cohort_sql",
    "translate_cohort_sql",
    "translate_to_duckdb",
    "KeyCollisionError",
    "CORE_14_LAB_PANEL",
    # ML Feature Extraction & Builders (RFC-6)
    "CohortExtractor",
    "FeatureMatrixBuilder",
    # omop-learn / SARD / PLP bridge: set-based sparse & sequence feature extraction
    "extract_sparse_concept_matrix",
    "extract_sard_visit_tensors",
    "build_study_tables",
    "explain_models",
    "plot_cohort_flowchart",
    "plot_shap_beeswarm",
    "plot_shap_panels",
    "export_workbook",
    "index_stay_dates",
    "los_by_site",
    "random_stay_draw",
    "seed_sensitivity",
    "stay_type_auc",
    "tune_models",
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
    "consortium_propensity_score",
    "match_nearest_neighbor",
    "plot_love_plot",
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
    # OHDSI PLP Study Package Generator
    "export_plp_study_package",
    # Andromeda Adapter
    "export_to_andromeda",
    "AndromedaAdapter",
    # Foundation Model Sequence Tokenizer
    "ClinicalSequenceTokens",
    "export_clinical_sequence_tokens",
    "to_clinical_sequence_tokens",
    # Federated Meta-Analysis Aggregator
    "federated_meta_analysis",
    "FederatedMetaAnalysisResult",
    # Path sanitizer, resource governor, datetime normalizer, wide loader, medication classes, phenotype DSL
    "attach_database",
    "copy_to",
    "read_csv_sql",
    "read_parquet_sql",
    "sanitize_sql_path",
    "sql_path_literal",
    "ResourceConfig",
    "configure_resources",
    "detect_system_ram",
    "DEFAULT_DATETIME_FORMATS",
    "install_vectorized_strptime",
    "normalize_time_sql",
    "vectorized_strptime",
    "DEFAULT_VITAL_CONCEPT_MAPPING",
    "DEFAULT_VITAL_PLAUSIBILITY_FILTERS",
    "load_wide_measurement",
    "audit_medication_strategies",
    "extract_medication_classes",
    "ConditionCriteria",
    "DrugCriteria",
    "Everyone",
    "MeasurementCriteria",
    "PhenotypeExpr",
    "PhenotypeIntersect",
    "PhenotypeNot",
    "PhenotypeUnion",
    "compile_phenotype",
    "execute_phenotype",
    "phenotype_from_dict",
    "build_safe_source_view",
    # 0.6.1 Additions
    "ConsortiumScoreResult",
    "ConsortiumCalibration",
    "consortium_calibration",
    "ConsortiumExplainability",
    "consortium_explainability",
    "DecisionCurveAnalysis",
    "decision_curve_analysis",
    "CIFResult",
    "FineGrayResult",
    "estimate_cif",
    "fine_gray_estimator",
    "plot_cif",
    # 0.6.3 Forward-Looking Consortium Intelligence Additions
    "export_to_circe_json",
    "ConsortiumRiskFactors",
    "consortium_multivariable_risk_factors",
    "ConsortiumCohortDiagnostics",
    "consortium_cohort_diagnostics",
    "ConsortiumCrossSiteValidation",
    "consortium_cross_site_validation",
    "ConsortiumManuscript",
    "build_manuscript_scaffold",
    # 0.6.4 Medication & Quality Guards Additions
    "assert_exposure_coverage",
    "explain_medication_disagreement",
    "export_medication_crosswalk",
    "audit_code_list_coverage",
    "panel_missingness_audit",
    "plausibility_report",
    "detect_redundant_features",
    "dqd_summary",
    # Reconciliation & Comparison Additions
    "cohort_from_index_stays",
    "audit_combination_products",
    "export_companion_manifest",
    "compare_feature_tables",
    "cohort_overlap",
    "run_study_grid",
    # Integrity, Leakage, Missingness & Provenance Additions
    "leakage_scan",
    "add_missing_indicators",
    "missingness_by_outcome",
    "provenance",
    "check_vocabulary_provenance",
    "numbers_of_record",
    "scan_document_numbers",
    "vocabulary_preflight",
    "generate_site_patient_tokens",
    "consortium_patient_overlap",
    "consortium_cross_site_duplicates",
    "consortium_duplicate_report",
    "check_observation_period_ends",
    "planned_readmission_filter",
    "observation_period_diagnostics",
    # Advanced Causal, Calibration, Trajectories, Survival & Synthetic Additions (v0.6.4)
    "generate_negative_controls",
    "calibrate_estimates",
    "EmpiricalCalibrationResult",
    "optimize_concept_set",
    "extract_temporal_trajectories",
    "consortium_survival_meta_analysis",
    "ACHILLES_ANALYSES",
    "achilles_analysis_catalog",
    "run_achilles",
    "encode_categoricals",
    "find_metformin_only_prediabetes_pcos",
    "encoded_column_names",
    "assign_sdi_quintile",
    "derive_geography_features",
    "fit_sdi_cut_points",
    "read_ruca_crosswalk",
    "read_sdi_table",
    "ruca_category",
    "validate_sdi_cut_points",
    "write_geography_measurements",
    "FeatureSet",
    "StudyFeature",
    "area_feature",
    "concept_feature",
    "acs_feature",
    "acs_zip3_table",
    "fetch_acs_zcta",
    "custom_feature",
    "feature_list",
    "ruca_feature",
    "sdi_feature",
    "sql_feature",
    "stays_from_cohort",
    "write_custom_covariates",
    "ablate_feature_groups",
    "compare_model_runs",
    "ridge_logistic_fit_predict",
    "cohort_definitions_dir",
    "stamp_feature_spec_concepts",
    "validate_bundle_concepts",
    "explain_cohort_gaps",
    "reconcile_cohort",
    "suppress_small_cells",
    "check_custom_concepts",
    "custom_concept_registry",
    "export_custom_concept_registry",
    "write_custom_features",
    "register_custom_concepts",
    "run_dqd_catalog",
    "run_dqd_full",
    "read_bigquery_table",
    "sync_vocabulary_from_bigquery",
    "FederatedFitResult",
    "FederatedNode",
    "federated_cox",
    "federated_logistic_regression",
    "FederatedSurvivalResult",
    "generate_synthetic_omop_twin",
    # 0.6.6 EHR Prescriptions, Multi-Anchor, Unit Discordance, Outliers, Eras & Acute Trajectories Additions
    "DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS",
    "DEFAULT_ADMINISTERED_TYPE_CONCEPT_IDS",
    "DEFAULT_DISPENSED_TYPE_CONCEPT_IDS",
    "compute_drug_eras",
    "compute_adherence_pdc",
    "detect_unit_discordance",
    "normalize_units",
    "extract_acute_trajectories",
    "impute_encounter_observations",
    "extract_features",
    # 0.6.7 Hierarchical Rollups, Concept Code Patterns, Fuzzy Matching & Provenance Additions
    "ConceptPattern",
    "HierarchicalRollup",
    "RollupEngine",
    "materialize_crosswalk",
    "audit_concept_drift",
    "compute_file_sha256",
    "suggest_source_mappings",
    "DEFAULT_INPATIENT_VISIT_TYPE_CONCEPT_IDS",
    "DEFAULT_OUTPATIENT_VISIT_TYPE_CONCEPT_IDS",
    "DEFAULT_EMERGENCY_VISIT_TYPE_CONCEPT_IDS",
    "VISIT_PROVENANCE_MAP",
    "DEFAULT_PRIMARY_CONDITION_TYPE_CONCEPT_IDS",
    "DEFAULT_ADMITTING_CONDITION_TYPE_CONCEPT_IDS",
    "DEFAULT_PROBLEM_LIST_CONDITION_TYPE_CONCEPT_IDS",
    "CONDITION_PROVENANCE_MAP",
    "DEFAULT_LAB_RESULT_MEASUREMENT_TYPE_CONCEPT_IDS",
    "DEFAULT_ORDER_MEASUREMENT_TYPE_CONCEPT_IDS",
    "DEFAULT_POC_MEASUREMENT_TYPE_CONCEPT_IDS",
    "MEASUREMENT_PROVENANCE_MAP",
    "filter_visits",
    "filter_conditions",
    "filter_measurements",
    # v0.12.0: disclosure control, CONSORT from counts, parity audit, pooled Table 1, site summaries, mapping coverage
    "DEFAULT_MIN_CELL", "DisclosureError", "assert_disclosure_safe", "check_disclosure", "format_count", "small_cell_mask",
    "suppress_attrition_table", "suppress_small_counts", "consort_from_counts", "consort_table_to_csv", "pool_consort_tables",
    "ParityAudit", "audit_matrix_parity", "build_table1", "pool_table1_summaries", "summarize_table1_site",
    "SiteSummaryError", "assert_aggregate_only", "export_site_summary", "pool_site_packages", "mapping_coverage_report",
    "run_bigquery_demo", "vocabulary_info",
    # v0.13.0: benchmark feature table, deep-learning datasets, frozen vocabulary, LOSO transport and recalibration
    "extract_benchmark_features", "DEFAULT_DISPOSITION_MAP", "extract_multimodal_deep_dataset", "extract_intra_stay_tokens", "extract_intra_stay_measurements",
    "export_concept_vocabulary", "import_concept_vocabulary", "make_pretraining_targets", "evaluate_loso_transport",
    "LosoTransportResult", "Recalibrator", "fit_recalibrator", "platt_scaling_fit", "platt_scaling_apply", "isotonic_fit",
    "isotonic_apply", "adjust_for_prevalence", "capacity_threshold", "capacity_risk_tiers", "evaluate_at_capacity",
]

__version__ = "0.15.0"
__commit__ = "c072171c3c33098101ba7eeff99d3b466abf1da9"


from omop_etl.benchmark_features import DEFAULT_DISPOSITION_MAP, extract_benchmark_features  # noqa: E402
from omop_etl.deep_dataset import (  # noqa: E402
    export_concept_vocabulary, extract_intra_stay_measurements, extract_intra_stay_tokens, extract_multimodal_deep_dataset, import_concept_vocabulary,
    make_pretraining_targets,
)
from omop_etl.transport import (  # noqa: E402
    LosoTransportResult, Recalibrator, adjust_for_prevalence, capacity_risk_tiers, capacity_threshold, evaluate_at_capacity,
    evaluate_loso_transport, fit_recalibrator, isotonic_apply, isotonic_fit, platt_scaling_apply, platt_scaling_fit,
)
from omop_etl.tensor import (
    extract_longitudinal_tensors,
    MAX_LONGITUDINAL_VISITS,
    MAX_TOKENS_PER_VISIT,
    VOCAB_SIZE,
)
