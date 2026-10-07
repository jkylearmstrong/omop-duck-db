"""Pre-compiled OHDSI Phenotype Library Concept Bundles.

Provides zero-dependency access to curated standard concept sets, ICD-9/10 source codes,
and domain classifications for major clinical phenotypes:
- type_2_diabetes
- heart_failure
- sepsis
- copd
- acute_kidney_injury
- atrial_fibrillation
- hypertension
- ischemic_stroke
"""

from __future__ import annotations

from typing import Any

PHENOTYPE_BUNDLES: dict[str, dict[str, Any]] = {
    "type_2_diabetes": {
        "name": "Type 2 Diabetes Mellitus",
        "domain_id": "Condition",
        "standard_concept_ids": [201826, 443238, 316866, 4058243, 4099651],
        "primary_snomed_id": 201826,
        "icd10_prefixes": ["E11", "E11.0", "E11.1", "E11.2", "E11.3", "E11.4", "E11.5", "E11.6", "E11.8", "E11.9"],
        "icd9_prefixes": ["250.00", "250.02", "250.10", "250.20"],
        "description": "Type 2 diabetes mellitus diagnosis including chronic complications.",
    },
    "heart_failure": {
        "name": "Heart Failure",
        "domain_id": "Condition",
        "standard_concept_ids": [316139, 434056, 433435, 4185932, 314378, 439693],
        "primary_snomed_id": 316139,
        "icd10_prefixes": ["I50", "I50.1", "I50.2", "I50.3", "I50.4", "I50.9", "I11.0", "I13.0", "I13.2"],
        "icd9_prefixes": ["428.0", "428.1", "428.2", "428.3", "428.4", "428.9"],
        "description": "Heart failure (systolic, diastolic, combined, and hypertensive heart failure).",
    },
    "sepsis": {
        "name": "Sepsis & Septic Shock",
        "domain_id": "Condition",
        "standard_concept_ids": [132797, 435308, 4132546, 438134],
        "primary_snomed_id": 132797,
        "icd10_prefixes": ["A41", "A41.9", "R65.2", "R65.20", "R65.21"],
        "icd9_prefixes": ["038.9", "995.91", "995.92", "785.52"],
        "description": "Severe sepsis, systemic inflammatory response syndrome, and septic shock.",
    },
    "copd": {
        "name": "Chronic Obstructive Pulmonary Disease",
        "domain_id": "Condition",
        "standard_concept_ids": [255573, 317009, 440383, 313296],
        "primary_snomed_id": 255573,
        "icd10_prefixes": ["J44", "J44.0", "J44.1", "J44.9", "J43"],
        "icd9_prefixes": ["491.2", "492.8", "496"],
        "description": "Chronic obstructive pulmonary disease, emphysema, and chronic bronchitis.",
    },
    "acute_kidney_injury": {
        "name": "Acute Kidney Injury",
        "domain_id": "Condition",
        "standard_concept_ids": [197320, 444094, 4010850, 432961],
        "primary_snomed_id": 197320,
        "icd10_prefixes": ["N17", "N17.0", "N17.1", "N17.2", "N17.8", "N17.9"],
        "icd9_prefixes": ["584.5", "584.6", "584.7", "584.8", "584.9"],
        "description": "Acute kidney injury and acute renal failure.",
    },
    "atrial_fibrillation": {
        "name": "Atrial Fibrillation & Flutter",
        "domain_id": "Condition",
        "standard_concept_ids": [313217, 4154290, 4108832, 4214956],
        "primary_snomed_id": 313217,
        "icd10_prefixes": ["I48", "I48.0", "I48.1", "I48.2", "I48.9", "I48.91", "I48.92"],
        "icd9_prefixes": ["427.31", "427.32"],
        "description": "Atrial fibrillation and atrial flutter.",
    },
    "hypertension": {
        "name": "Essential (Primary) Hypertension",
        "domain_id": "Condition",
        "standard_concept_ids": [316866, 320128, 4326442, 4028244],
        "primary_snomed_id": 320128,
        "icd10_prefixes": ["I10", "I11", "I12", "I13", "I15"],
        "icd9_prefixes": ["401.1", "401.9", "402.90"],
        "description": "Essential hypertension and hypertensive vascular disease.",
    },
    "ischemic_stroke": {
        "name": "Ischemic Stroke",
        "domain_id": "Condition",
        "standard_concept_ids": [443454, 4253880, 372924, 375557, 4110192],
        "primary_snomed_id": 443454,
        "icd10_prefixes": ["I63", "I63.0", "I63.1", "I63.2", "I63.3", "I63.4", "I63.5", "I63.9"],
        "icd9_prefixes": ["434.91", "434.11", "433.11"],
        "description": "Acute ischemic stroke, cerebral infarction, and occlusion of cerebral arteries.",
    },
}


def get_phenotype_concept_set(phenotype_name: str, version: str = "ohdsi_v2.0") -> dict[str, Any]:
    """Retrieve pre-compiled standard concept set definition for an OHDSI phenotype.

    Args:
        phenotype_name: Standard phenotype key (e.g., 'heart_failure', 'type_2_diabetes',
            'sepsis', 'copd', 'acute_kidney_injury', 'atrial_fibrillation',
            'hypertension', 'ischemic_stroke').
        version: Phenotype library bundle version identifier (default 'ohdsi_v2.0').

    Returns:
        dict: Concept set dictionary containing standard_concept_ids, icd10_prefixes,
            icd9_prefixes, domain_id, and descriptive metadata.

    Raises:
        KeyError: If phenotype_name is not recognized.
    """
    key = phenotype_name.lower().strip().replace(" ", "_").replace("-", "_")
    if key not in PHENOTYPE_BUNDLES:
        available = sorted(PHENOTYPE_BUNDLES.keys())
        raise KeyError(
            f"Unknown phenotype '{phenotype_name}'. Available phenotypes: {', '.join(available)}"
        )
    bundle = dict(PHENOTYPE_BUNDLES[key])
    bundle["version"] = version
    return bundle


def list_available_phenotypes() -> list[str]:
    """List all available pre-compiled phenotype bundle keys."""
    return sorted(PHENOTYPE_BUNDLES.keys())
