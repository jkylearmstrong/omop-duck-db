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
    "chronic_kidney_disease": {
        "name": "Chronic Kidney Disease",
        "domain_id": "Condition",
        "standard_concept_ids": [46271022, 193782, 192359, 443601, 443612, 443611],
        "primary_snomed_id": 46271022,
        "icd10_prefixes": ["N18", "N18.1", "N18.2", "N18.3", "N18.4", "N18.5", "N18.6", "N18.9"],
        "icd9_prefixes": ["585.1", "585.2", "585.3", "585.4", "585.5", "585.6", "585.9"],
        "description": "Chronic kidney disease stages 1-5 and end-stage renal disease (ESRD).",
    },
    "asthma": {
        "name": "Asthma",
        "domain_id": "Condition",
        "standard_concept_ids": [317009, 4051466, 4052029, 4178431],
        "primary_snomed_id": 317009,
        "icd10_prefixes": ["J45", "J45.0", "J45.1", "J45.2", "J45.3", "J45.4", "J45.5", "J45.8", "J45.9"],
        "icd9_prefixes": ["493.0", "493.1", "493.2", "493.9"],
        "description": "Asthma including allergic, non-allergic, and status asthmaticus.",
    },
    "rheumatoid_arthritis": {
        "name": "Rheumatoid Arthritis",
        "domain_id": "Condition",
        "standard_concept_ids": [80809, 4035611, 4160162, 4218890],
        "primary_snomed_id": 80809,
        "icd10_prefixes": ["M05", "M06", "M05.0", "M05.1", "M05.2", "M05.3", "M06.0", "M06.9"],
        "icd9_prefixes": ["714.0", "714.1", "714.2"],
        "description": "Rheumatoid arthritis with and without rheumatoid factor or organ involvement.",
    },
    "major_depressive_disorder": {
        "name": "Major Depressive Disorder",
        "domain_id": "Condition",
        "standard_concept_ids": [440383, 436665, 4152280, 4282316],
        "primary_snomed_id": 440383,
        "icd10_prefixes": ["F32", "F33", "F32.0", "F32.1", "F32.2", "F32.3", "F32.9", "F33.0", "F33.1", "F33.2", "F33.3", "F33.9"],
        "icd9_prefixes": ["296.2", "296.3"],
        "description": "Single episode and recurrent major depressive disorder.",
    },
    "dementia_alzheimers": {
        "name": "Dementia & Alzheimer's Disease",
        "domain_id": "Condition",
        "standard_concept_ids": [4182210, 374888, 4148906, 4268612],
        "primary_snomed_id": 4182210,
        "icd10_prefixes": ["G30", "G30.0", "G30.1", "G30.8", "G30.9", "F01", "F02", "F03"],
        "icd9_prefixes": ["331.0", "290.0", "290.1", "290.2", "290.4"],
        "description": "Alzheimer's disease, vascular dementia, and unspecified senile dementia.",
    },
    "liver_cirrhosis": {
        "name": "Liver Cirrhosis & Portal Hypertension",
        "domain_id": "Condition",
        "standard_concept_ids": [4064161, 4245975, 4153359, 197494],
        "primary_snomed_id": 4064161,
        "icd10_prefixes": ["K74", "K74.0", "K74.1", "K74.2", "K74.3", "K74.4", "K74.5", "K74.6", "K70.3", "K76.6"],
        "icd9_prefixes": ["571.2", "571.5", "572.3"],
        "description": "Cirrhosis of liver (alcoholic and non-alcoholic) and portal hypertension.",
    },
    "breast_cancer": {
        "name": "Malignant Neoplasm of Breast",
        "domain_id": "Condition",
        "standard_concept_ids": [137809, 4112853, 4116041, 4273629],
        "primary_snomed_id": 137809,
        "icd10_prefixes": ["C50", "C50.0", "C50.1", "C50.2", "C50.3", "C50.4", "C50.5", "C50.6", "C50.8", "C50.9"],
        "icd9_prefixes": ["174.0", "174.1", "174.2", "174.3", "174.4", "174.5", "174.6", "174.8", "174.9"],
        "description": "Invasive primary malignant neoplasm of female and male breast.",
    },
    "colorectal_cancer": {
        "name": "Colorectal Cancer",
        "domain_id": "Condition",
        "standard_concept_ids": [4028741, 4114488, 4124940, 4132431],
        "primary_snomed_id": 4028741,
        "icd10_prefixes": ["C18", "C19", "C20", "C18.0", "C18.2", "C18.7", "C18.9", "C20"],
        "icd9_prefixes": ["153.0", "153.1", "153.2", "153.3", "153.4", "153.9", "154.0", "154.1"],
        "description": "Primary malignant neoplasm of colon, rectosigmoid junction, and rectum.",
    },
    "lung_cancer": {
        "name": "Malignant Neoplasm of Lung and Bronchus",
        "domain_id": "Condition",
        "standard_concept_ids": [4115276, 4118804, 4112852, 4030616],
        "primary_snomed_id": 4115276,
        "icd10_prefixes": ["C34", "C34.0", "C34.1", "C34.2", "C34.3", "C34.8", "C34.9"],
        "icd9_prefixes": ["162.2", "162.3", "162.4", "162.5", "162.8", "162.9"],
        "description": "Primary malignant neoplasm of bronchus and lung (NSCLC and SCLC).",
    },
    "prostate_cancer": {
        "name": "Malignant Neoplasm of Prostate",
        "domain_id": "Condition",
        "standard_concept_ids": [4163261, 4116043, 4030617, 4314337],
        "primary_snomed_id": 4163261,
        "icd10_prefixes": ["C61"],
        "icd9_prefixes": ["185"],
        "description": "Primary malignant adenocarcinoma of prostate gland.",
    },
    "covid_19": {
        "name": "COVID-19 Acute Infection",
        "domain_id": "Condition",
        "standard_concept_ids": [37311061, 705076, 37311060, 439676],
        "primary_snomed_id": 37311061,
        "icd10_prefixes": ["U07.1", "U07.2", "J12.82"],
        "icd9_prefixes": ["079.82"],
        "description": "Coronavirus disease 2019 (SARS-CoV-2 acute infection).",
    },
    "venous_thromboembolism": {
        "name": "Venous Thromboembolism (DVT & PE)",
        "domain_id": "Condition",
        "standard_concept_ids": [444094, 440417, 314443, 4134440],
        "primary_snomed_id": 444094,
        "icd10_prefixes": ["I82", "I82.4", "I82.9", "I26", "I26.0", "I26.9"],
        "icd9_prefixes": ["453.4", "453.8", "453.9", "415.11", "415.19"],
        "description": "Acute deep vein thrombosis (DVT) and pulmonary embolism (PE).",
    },
    "peripheral_artery_disease": {
        "name": "Peripheral Artery Disease",
        "domain_id": "Condition",
        "standard_concept_ids": [321887, 4138487, 4185932, 4216118],
        "primary_snomed_id": 321887,
        "icd10_prefixes": ["I73.9", "I70.2", "I70.20", "I70.21", "I70.22"],
        "icd9_prefixes": ["443.9", "440.20", "440.21"],
        "description": "Peripheral artery occlusive disease and arteriosclerosis of native extremities.",
    },
    "inflammatory_bowel_disease": {
        "name": "Inflammatory Bowel Disease (Crohn's & Ulcerative Colitis)",
        "domain_id": "Condition",
        "standard_concept_ids": [4058243, 197494, 192359, 4124940],
        "primary_snomed_id": 4058243,
        "icd10_prefixes": ["K50", "K50.0", "K50.1", "K50.9", "K51", "K51.0", "K51.9"],
        "icd9_prefixes": ["555.0", "555.1", "555.9", "556.0", "556.9"],
        "description": "Crohn's disease (regional enteritis) and ulcerative colitis.",
    },
    "severe_aortic_stenosis": {
        "name": "Severe Aortic Stenosis",
        "domain_id": "Condition",
        "standard_concept_ids": [314378, 4148906, 4222384, 4310564],
        "primary_snomed_id": 314378,
        "icd10_prefixes": ["I35.0", "I35.2", "I06.0", "I06.2"],
        "icd9_prefixes": ["424.1", "395.0", "395.2"],
        "description": "Aortic valve stenosis (calcific, rheumatic, or non-rheumatic).",
    },
    "atopic_dermatitis": {
        "name": "Atopic Dermatitis & Eczema",
        "domain_id": "Condition",
        "standard_concept_ids": [133834, 4028244, 4141157, 4324887],
        "primary_snomed_id": 133834,
        "icd10_prefixes": ["L20", "L20.0", "L20.8", "L20.84", "L20.9"],
        "icd9_prefixes": ["691.8"],
        "description": "Atopic dermatitis, flexural eczema, and related allergic dermatitides.",
    },
    "osteoarthritis": {
        "name": "Osteoarthritis",
        "domain_id": "Condition",
        "standard_concept_ids": [4014295, 4178431, 4079750, 4287240],
        "primary_snomed_id": 4014295,
        "icd10_prefixes": ["M15", "M16", "M17", "M18", "M19", "M15.0", "M16.0", "M17.0", "M19.9"],
        "icd9_prefixes": ["715.00", "715.11", "715.15", "715.16", "715.90"],
        "description": "Primary generalized and localized osteoarthritis (knee, hip, hand, spine).",
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
