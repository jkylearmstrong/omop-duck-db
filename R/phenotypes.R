#' Pre-compiled OHDSI Phenotype Library Concept Bundles
#'
#' Provides zero-dependency access to curated standard concept sets, ICD-9/10 source codes,
#' and domain classifications for major clinical phenotypes.
#'
#' @docType class
#' @name phenotypes
NULL

.PHENOTYPE_BUNDLES <- list(
  type_2_diabetes = list(
    name = "Type 2 Diabetes Mellitus",
    domain_id = "Condition",
    standard_concept_ids = c(201826L, 443238L, 316866L, 4058243L, 4099651L),
    primary_snomed_id = 201826L,
    icd10_prefixes = c("E11", "E11.0", "E11.1", "E11.2", "E11.3", "E11.4", "E11.5", "E11.6", "E11.8", "E11.9"),
    icd9_prefixes = c("250.00", "250.02", "250.10", "250.20"),
    description = "Type 2 diabetes mellitus diagnosis including chronic complications."
  ),
  heart_failure = list(
    name = "Heart Failure",
    domain_id = "Condition",
    standard_concept_ids = c(316139L, 434056L, 433435L, 4185932L, 314378L, 439693L),
    primary_snomed_id = 316139L,
    icd10_prefixes = c("I50", "I50.1", "I50.2", "I50.3", "I50.4", "I50.9", "I11.0", "I13.0", "I13.2"),
    icd9_prefixes = c("428.0", "428.1", "428.2", "428.3", "428.4", "428.9"),
    description = "Heart failure (systolic, diastolic, combined, and hypertensive heart failure)."
  ),
  sepsis = list(
    name = "Sepsis & Septic Shock",
    domain_id = "Condition",
    standard_concept_ids = c(132797L, 435308L, 4132546L, 438134L),
    primary_snomed_id = 132797L,
    icd10_prefixes = c("A41", "A41.9", "R65.2", "R65.20", "R65.21"),
    icd9_prefixes = c("038.9", "995.91", "995.92", "785.52"),
    description = "Severe sepsis, systemic inflammatory response syndrome, and septic shock."
  ),
  copd = list(
    name = "Chronic Obstructive Pulmonary Disease",
    domain_id = "Condition",
    standard_concept_ids = c(255573L, 317009L, 440383L, 313296L),
    primary_snomed_id = 255573L,
    icd10_prefixes = c("J44", "J44.0", "J44.1", "J44.9", "J43"),
    icd9_prefixes = c("491.2", "492.8", "496"),
    description = "Chronic obstructive pulmonary disease, emphysema, and chronic bronchitis."
  ),
  acute_kidney_injury = list(
    name = "Acute Kidney Injury",
    domain_id = "Condition",
    standard_concept_ids = c(197320L, 444094L, 4010850L, 432961L),
    primary_snomed_id = 197320L,
    icd10_prefixes = c("N17", "N17.0", "N17.1", "N17.2", "N17.8", "N17.9"),
    icd9_prefixes = c("584.5", "584.6", "584.7", "584.8", "584.9"),
    description = "Acute kidney injury and acute renal failure."
  ),
  atrial_fibrillation = list(
    name = "Atrial Fibrillation & Flutter",
    domain_id = "Condition",
    standard_concept_ids = c(313217L, 4154290L, 4108832L, 4214956L),
    primary_snomed_id = 313217L,
    icd10_prefixes = c("I48", "I48.0", "I48.1", "I48.2", "I48.9", "I48.91", "I48.92"),
    icd9_prefixes = c("427.31", "427.32"),
    description = "Atrial fibrillation and atrial flutter."
  ),
  hypertension = list(
    name = "Essential (Primary) Hypertension",
    domain_id = "Condition",
    standard_concept_ids = c(316866L, 320128L, 4326442L, 4028244L),
    primary_snomed_id = 320128L,
    icd10_prefixes = c("I10", "I11", "I12", "I13", "I15"),
    icd9_prefixes = c("401.1", "401.9", "402.90"),
    description = "Essential hypertension and hypertensive vascular disease."
  ),
  ischemic_stroke = list(
    name = "Ischemic Stroke",
    domain_id = "Condition",
    standard_concept_ids = c(443454L, 4253880L, 372924L, 375557L, 4110192L),
    primary_snomed_id = 443454L,
    icd10_prefixes = c("I63", "I63.0", "I63.1", "I63.2", "I63.3", "I63.4", "I63.5", "I63.9"),
    icd9_prefixes = c("434.91", "434.11", "433.11"),
    description = "Acute ischemic stroke, cerebral infarction, and occlusion of cerebral arteries."
  ),
  chronic_kidney_disease = list(
    name = "Chronic Kidney Disease",
    domain_id = "Condition",
    standard_concept_ids = c(46271022L, 193782L, 192359L, 443601L, 443612L, 443611L),
    primary_snomed_id = 46271022L,
    icd10_prefixes = c("N18", "N18.1", "N18.2", "N18.3", "N18.4", "N18.5", "N18.6", "N18.9"),
    icd9_prefixes = c("585.1", "585.2", "585.3", "585.4", "585.5", "585.6", "585.9"),
    description = "Chronic kidney disease stages 1-5 and end-stage renal disease (ESRD)."
  ),
  asthma = list(
    name = "Asthma",
    domain_id = "Condition",
    standard_concept_ids = c(317009L, 4051466L, 4052029L, 4178431L),
    primary_snomed_id = 317009L,
    icd10_prefixes = c("J45", "J45.0", "J45.1", "J45.2", "J45.3", "J45.4", "J45.5", "J45.8", "J45.9"),
    icd9_prefixes = c("493.0", "493.1", "493.2", "493.9"),
    description = "Asthma including allergic, non-allergic, and status asthmaticus."
  ),
  rheumatoid_arthritis = list(
    name = "Rheumatoid Arthritis",
    domain_id = "Condition",
    standard_concept_ids = c(80809L, 4035611L, 4160162L, 4218890L),
    primary_snomed_id = 80809L,
    icd10_prefixes = c("M05", "M06", "M05.0", "M05.1", "M05.2", "M05.3", "M06.0", "M06.9"),
    icd9_prefixes = c("714.0", "714.1", "714.2"),
    description = "Rheumatoid arthritis with and without rheumatoid factor or organ involvement."
  ),
  major_depressive_disorder = list(
    name = "Major Depressive Disorder",
    domain_id = "Condition",
    standard_concept_ids = c(440383L, 436665L, 4152280L, 4282316L),
    primary_snomed_id = 440383L,
    icd10_prefixes = c("F32", "F33", "F32.0", "F32.1", "F32.2", "F32.3", "F32.9", "F33.0", "F33.1", "F33.2", "F33.3", "F33.9"),
    icd9_prefixes = c("296.2", "296.3"),
    description = "Single episode and recurrent major depressive disorder."
  ),
  dementia_alzheimers = list(
    name = "Dementia & Alzheimer's Disease",
    domain_id = "Condition",
    standard_concept_ids = c(4182210L, 374888L, 4148906L, 4268612L),
    primary_snomed_id = 4182210L,
    icd10_prefixes = c("G30", "G30.0", "G30.1", "G30.8", "G30.9", "F01", "F02", "F03"),
    icd9_prefixes = c("331.0", "290.0", "290.1", "290.2", "290.4"),
    description = "Alzheimer's disease, vascular dementia, and unspecified senile dementia."
  ),
  liver_cirrhosis = list(
    name = "Liver Cirrhosis & Portal Hypertension",
    domain_id = "Condition",
    standard_concept_ids = c(4064161L, 4245975L, 4153359L, 197494L),
    primary_snomed_id = 4064161L,
    icd10_prefixes = c("K74", "K74.0", "K74.1", "K74.2", "K74.3", "K74.4", "K74.5", "K74.6", "K70.3", "K76.6"),
    icd9_prefixes = c("571.2", "571.5", "572.3"),
    description = "Cirrhosis of liver (alcoholic and non-alcoholic) and portal hypertension."
  ),
  breast_cancer = list(
    name = "Malignant Neoplasm of Breast",
    domain_id = "Condition",
    standard_concept_ids = c(137809L, 4112853L, 4116041L, 4273629L),
    primary_snomed_id = 137809L,
    icd10_prefixes = c("C50", "C50.0", "C50.1", "C50.2", "C50.3", "C50.4", "C50.5", "C50.6", "C50.8", "C50.9"),
    icd9_prefixes = c("174.0", "174.1", "174.2", "174.3", "174.4", "174.5", "174.6", "174.8", "174.9"),
    description = "Invasive primary malignant neoplasm of female and male breast."
  ),
  colorectal_cancer = list(
    name = "Colorectal Cancer",
    domain_id = "Condition",
    standard_concept_ids = c(4028741L, 4114488L, 4124940L, 4132431L),
    primary_snomed_id = 4028741L,
    icd10_prefixes = c("C18", "C19", "C20", "C18.0", "C18.2", "C18.7", "C18.9", "C20"),
    icd9_prefixes = c("153.0", "153.1", "153.2", "153.3", "153.4", "153.9", "154.0", "154.1"),
    description = "Primary malignant neoplasm of colon, rectosigmoid junction, and rectum."
  ),
  lung_cancer = list(
    name = "Malignant Neoplasm of Lung and Bronchus",
    domain_id = "Condition",
    standard_concept_ids = c(4115276L, 4118804L, 4112852L, 4030616L),
    primary_snomed_id = 4115276L,
    icd10_prefixes = c("C34", "C34.0", "C34.1", "C34.2", "C34.3", "C34.8", "C34.9"),
    icd9_prefixes = c("162.2", "162.3", "162.4", "162.5", "162.8", "162.9"),
    description = "Primary malignant neoplasm of bronchus and lung (NSCLC and SCLC)."
  ),
  prostate_cancer = list(
    name = "Malignant Neoplasm of Prostate",
    domain_id = "Condition",
    standard_concept_ids = c(4163261L, 4116043L, 4030617L, 4314337L),
    primary_snomed_id = 4163261L,
    icd10_prefixes = c("C61"),
    icd9_prefixes = c("185"),
    description = "Primary malignant adenocarcinoma of prostate gland."
  ),
  covid_19 = list(
    name = "COVID-19 Acute Infection",
    domain_id = "Condition",
    standard_concept_ids = c(37311061L, 705076L, 37311060L, 439676L),
    primary_snomed_id = 37311061L,
    icd10_prefixes = c("U07.1", "U07.2", "J12.82"),
    icd9_prefixes = c("079.82"),
    description = "Coronavirus disease 2019 (SARS-CoV-2 acute infection)."
  ),
  venous_thromboembolism = list(
    name = "Venous Thromboembolism (DVT & PE)",
    domain_id = "Condition",
    standard_concept_ids = c(444094L, 440417L, 314443L, 4134440L),
    primary_snomed_id = 444094L,
    icd10_prefixes = c("I82", "I82.4", "I82.9", "I26", "I26.0", "I26.9"),
    icd9_prefixes = c("453.4", "453.8", "453.9", "415.11", "415.19"),
    description = "Acute deep vein thrombosis (DVT) and pulmonary embolism (PE)."
  ),
  peripheral_artery_disease = list(
    name = "Peripheral Artery Disease",
    domain_id = "Condition",
    standard_concept_ids = c(321887L, 4138487L, 4185932L, 4216118L),
    primary_snomed_id = 321887L,
    icd10_prefixes = c("I73.9", "I70.2", "I70.20", "I70.21", "I70.22"),
    icd9_prefixes = c("443.9", "440.20", "440.21"),
    description = "Peripheral artery occlusive disease and arteriosclerosis of native extremities."
  ),
  inflammatory_bowel_disease = list(
    name = "Inflammatory Bowel Disease (Crohn's & Ulcerative Colitis)",
    domain_id = "Condition",
    standard_concept_ids = c(4058243L, 197494L, 192359L, 4124940L),
    primary_snomed_id = 4058243L,
    icd10_prefixes = c("K50", "K50.0", "K50.1", "K50.9", "K51", "K51.0", "K51.9"),
    icd9_prefixes = c("555.0", "555.1", "555.9", "556.0", "556.9"),
    description = "Crohn's disease (regional enteritis) and ulcerative colitis."
  ),
  severe_aortic_stenosis = list(
    name = "Severe Aortic Stenosis",
    domain_id = "Condition",
    standard_concept_ids = c(314378L, 4148906L, 4222384L, 4310564L),
    primary_snomed_id = 314378L,
    icd10_prefixes = c("I35.0", "I35.2", "I06.0", "I06.2"),
    icd9_prefixes = c("424.1", "395.0", "395.2"),
    description = "Aortic valve stenosis (calcific, rheumatic, or non-rheumatic)."
  ),
  atopic_dermatitis = list(
    name = "Atopic Dermatitis & Eczema",
    domain_id = "Condition",
    standard_concept_ids = c(133834L, 4028244L, 4141157L, 4324887L),
    primary_snomed_id = 133834L,
    icd10_prefixes = c("L20", "L20.0", "L20.8", "L20.84", "L20.9"),
    icd9_prefixes = c("691.8"),
    description = "Atopic dermatitis, flexural eczema, and related allergic dermatitides."
  ),
  osteoarthritis = list(
    name = "Osteoarthritis",
    domain_id = "Condition",
    standard_concept_ids = c(4014295L, 4178431L, 4079750L, 4287240L),
    primary_snomed_id = 4014295L,
    icd10_prefixes = c("M15", "M16", "M17", "M18", "M19", "M15.0", "M16.0", "M17.0", "M19.9"),
    icd9_prefixes = c("715.00", "715.11", "715.15", "715.16", "715.90"),
    description = "Primary generalized and localized osteoarthritis (knee, hip, hand, spine)."
  )
)

#' Retrieve pre-compiled concept set definition for an OHDSI phenotype
#'
#' @param phenotype_name Standard phenotype name (e.g., `"heart_failure"`, `"type_2_diabetes"`,
#'   `"sepsis"`, `"copd"`, `"acute_kidney_injury"`, `"atrial_fibrillation"`, `"hypertension"`,
#'   `"ischemic_stroke"`).
#' @param version Bundle version string (default `"ohdsi_v2.0"`).
#' @return A list containing `name`, `domain_id`, `standard_concept_ids`, `icd10_prefixes`,
#'   `icd9_prefixes`, `description`, and `version`.
#' @export
get_phenotype_concept_set <- function(phenotype_name, version = "ohdsi_v2.0") {
  key <- tolower(gsub("[- ]", "_", trimws(phenotype_name)))
  if (!key %in% names(.PHENOTYPE_BUNDLES)) {
    stop(sprintf("Unknown phenotype '%s'. Available: %s", phenotype_name,
                 paste(names(.PHENOTYPE_BUNDLES), collapse = ", ")), call. = FALSE)
  }
  bundle <- .PHENOTYPE_BUNDLES[[key]]
  bundle$version <- version
  bundle
}

#' List available pre-compiled phenotype bundle names
#'
#' @return Character vector of phenotype names.
#' @export
list_available_phenotypes <- function() {
  sort(names(.PHENOTYPE_BUNDLES))
}
