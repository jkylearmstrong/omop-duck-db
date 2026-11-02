"""Re-export of omop_etl.integrity functions for top-level omop_duck_db access."""

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
    planned_readmission_filter,
    observation_period_diagnostics,
)

__all__ = [
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
    "planned_readmission_filter",
    "observation_period_diagnostics",
]
