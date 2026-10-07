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
