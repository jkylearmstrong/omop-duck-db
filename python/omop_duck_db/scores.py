"""Clinical Benchmark Risk Score Calculators (LACE Index and HOSPITAL Score)."""

from omop_etl.scores import (
    calculate_lace_index,
    calculate_hospital_score,
)

__all__ = ["calculate_lace_index", "calculate_hospital_score"]
