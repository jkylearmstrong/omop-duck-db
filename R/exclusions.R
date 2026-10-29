#' Metformin-only prediabetes / PCOS patients
#'
#' A patient is *metformin-only prediabetes / PCOS* when ALL hold: (1) a prediabetes or PCOS condition, (2) metformin
#' exposure, (3) NO other diabetes drug (insulins, sulfonylureas, ... and every metformin combination product), and
#' (4) NO diabetes mellitus condition. The legacy study excluded these patients (all their encounters) through a per-site
#' file produced upstream; this builds the same set from OMOP so it can be validated against that file.
#'
#' Concept sets (ids checked against an Athena vocabulary; descendants come from `concept_ancestor`): prediabetes =
#' 37018196 / 40316773 / 37201113 and source value R73.03; PCOS `"icd10"` = 40443308 / 35206936 and source value E28.2,
#' `"snomed"` = 36683296 / 195501 (SNOMED 69878008, "Polycystic ovary"), `"both"` = all; metformin = RxNorm ingredient 6809
#' (1503297) and descendants, or the words metformin / glucophage in `drug_source_value`; other diabetes drug = ATC A10
#' (21600712) descendants that are not ATC A10BA biguanides (21600745), so a metformin combination counts as another drug,
#' or other diabetes drug names in `drug_source_value`; diabetes = 201820 and descendants, or ICD-9 249/250 / ICD-10
#' E08-E13 source values. The whole history is used. Without a usable `concept_ancestor` the concept sets shrink to the
#' listed ids and the source-value patterns, with a warning.
#'
#' @param con A DBI connection to an OMOP DuckDB.
#' @param pcos PCOS concept set: `"icd10"` (default), `"snomed"` or `"both"`.
#' @param schema Optional schema holding the CDM tables.
#' @return A data frame with `person_id`, `has_prediabetes` and `has_pcos`.
#' @export
find_metformin_only_prediabetes_pcos <- function(con, pcos = "icd10", schema = NULL) {
  ca <- .resolve_concept_ancestor_ref(con, schema)
  if (is.null(ca)) {
    warning("find_metformin_only_prediabetes_pcos(): no usable concept_ancestor on this connection; concept sets are ",
            "limited to the listed concept ids and source-value patterns (attach the vocabulary, e.g. omop_connect(), ",
            "for descendants).", call. = FALSE)
  }
  DBI::dbGetQuery(con, .metformin_only_sql(pcos, schema, ca))
}

.PCOS_SETS <- c("icd10", "snomed", "both")

#' @keywords internal
#' @noRd
.resolve_concept_ancestor_ref <- function(con, schema = NULL) {
  if (!is.null(schema)) {
    ref <- paste0(schema, ".concept_ancestor")
    ok <- tryCatch(nrow(DBI::dbGetQuery(con, sprintf("SELECT 1 FROM %s LIMIT 1", ref))) > 0L, error = function(e) FALSE)
    if (isTRUE(ok)) return(ref)
  }
  .comorb_resolve_concept_ancestor(con)$ref
}

.mf_ids <- function(x) paste(as.integer(x), collapse = ", ")

.mf_concept_set <- function(ca, ancestors, exclude = integer(0)) {
  base <- sprintf("SELECT unnest([%s]) AS id", .mf_ids(ancestors))
  if (is.null(ca)) return(base)
  sql <- sprintf("%s UNION SELECT descendant_concept_id AS id FROM %s WHERE ancestor_concept_id IN (%s)",
                 base, ca, .mf_ids(ancestors))
  if (length(exclude)) {
    sql <- sprintf("SELECT id FROM (%s) WHERE id NOT IN (SELECT descendant_concept_id FROM %s WHERE ancestor_concept_id IN (%s))",
                   sql, ca, .mf_ids(exclude))
  }
  sql
}

#' @keywords internal
#' @noRd
.metformin_only_sql <- function(pcos = "icd10", schema = NULL, concept_ancestor = NULL) {
  if (!pcos %in% .PCOS_SETS) stop("pcos must be one of ", paste(.PCOS_SETS, collapse = ", "), "; got '", pcos, "'.", call. = FALSE)
  pre <- if (is.null(schema)) "" else paste0(schema, ".")
  ca <- concept_ancestor
  prediabetes <- c(37018196L, 40316773L, 37201113L)
  pcos_ids <- switch(pcos, icd10 = c(40443308L, 35206936L), snomed = c(36683296L, 195501L),
                     both = c(40443308L, 35206936L, 36683296L, 195501L))
  pcos_src <- if (pcos %in% c("icd10", "both")) "OR regexp_matches(src, '^E282')" else ""
  other_words <- paste0("insulin|glipizide|glyburide|glibenclamide|glimepiride|gliclazide|sitagliptin|saxagliptin|linagliptin|",
                        "alogliptin|vildagliptin|empagliflozin|dapagliflozin|canagliflozin|ertugliflozin|liraglutide|semaglutide|",
                        "dulaglutide|exenatide|pioglitazone|rosiglitazone|repaglinide|nateglinide|acarbose|miglitol|pramlintide")
  other_set <- if (is.null(ca)) "SELECT NULL::BIGINT AS id WHERE FALSE" else .mf_concept_set(ca, 21600712L, 21600745L)
  sprintf("
WITH cond AS (
    SELECT person_id, condition_concept_id AS cid,
           upper(regexp_replace(coalesce(condition_source_value, ''), '[.\\s]', '', 'g')) AS src
    FROM %scondition_occurrence
), drug AS (
    SELECT person_id, drug_concept_id AS did, lower(coalesce(drug_source_value, '')) AS s FROM %sdrug_exposure
), pre_ids AS (%s),
   pcos_ids AS (%s),
   dm_ids AS (%s),
   met_ids AS (%s),
   other_ids AS (%s),
 prediab AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM pre_ids) OR regexp_matches(src, '^R7303')),
 pcosp AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM pcos_ids) %s),
 dm AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM dm_ids)
        OR regexp_matches(src, '^(249|250|E08|E09|E10|E11|E12|E13)')),
 met AS (SELECT DISTINCT person_id FROM drug WHERE did IN (SELECT id FROM met_ids) OR regexp_matches(s, 'metformin|glucophage')),
 other AS (SELECT DISTINCT person_id FROM drug WHERE did IN (SELECT id FROM other_ids) OR regexp_matches(s, '%s'))
SELECT m.person_id, (m.person_id IN (SELECT person_id FROM prediab)) AS has_prediabetes,
       (m.person_id IN (SELECT person_id FROM pcosp)) AS has_pcos
FROM met AS m
WHERE (m.person_id IN (SELECT person_id FROM prediab) OR m.person_id IN (SELECT person_id FROM pcosp))
  AND m.person_id NOT IN (SELECT person_id FROM other)
  AND m.person_id NOT IN (SELECT person_id FROM dm)
ORDER BY m.person_id
", pre, pre, .mf_concept_set(ca, prediabetes), .mf_concept_set(ca, pcos_ids), .mf_concept_set(ca, 201820L),
          .mf_concept_set(ca, 1503297L), other_set, pcos_src, other_words)
}

# ---- cohort helpers (define_study_cohort) -----------------------------------------------------------

.STUDY_METFORMIN_TABLE <- "_study_metformin_only"
.DIABETES_DRUG_RULES <- c("names", "atc_a10", "rxnorm_cuis")
.ATC_A10_CONCEPT <- 21600712L

.read_cuis <- function(value) {
  if (is.character(value) && length(value) == 1L && file.exists(value)) {
    txt <- paste(readLines(value, warn = FALSE), collapse = "\n")
    items <- regmatches(txt, gregexpr("[0-9]+", txt))[[1]]
  } else if (is.character(value) && length(value) == 1L && !grepl("^[0-9]+$", value)) {
    stop("diabetes_rxnorm_cuis file '", value, "' does not exist.", call. = FALSE)
  } else {
    items <- as.character(as.numeric(value))
  }
  cuis <- sort(unique(sub("\\.0+$", "", items)))
  if (!length(cuis)) stop("diabetes_rxnorm_cuis is empty.", call. = FALSE)
  cuis
}

#' @keywords internal
#' @noRd
.resolve_diabetes_drug_rule <- function(con, rule, cuis, schema) {
  if (!is.character(rule) || length(rule) != 1L || !rule %in% .DIABETES_DRUG_RULES) {
    stop("diabetes_drug_rule must be one of ", paste(.DIABETES_DRUG_RULES, collapse = ", "), "; got ", deparse(rule), ".", call. = FALSE)
  }
  if (rule != "rxnorm_cuis" && !is.null(cuis)) stop("diabetes_rxnorm_cuis is only used with diabetes_drug_rule = 'rxnorm_cuis'.", call. = FALSE)
  if (rule == "names") return(list(ancestors = integer(0), ca = NULL))
  ca <- .resolve_concept_ancestor_ref(con, schema)
  if (is.null(ca)) {
    stop("diabetes_drug_rule = '", rule, "' needs a vocabulary with concept_ancestor on this connection ",
         "(load or attach it, e.g. omop_connect()).", call. = FALSE)
  }
  if (rule == "atc_a10") {
    has <- nrow(DBI::dbGetQuery(con, sprintf("SELECT 1 FROM %s WHERE ancestor_concept_id = %d LIMIT 1", ca, .ATC_A10_CONCEPT))) > 0L
    if (!has) stop("concept_ancestor has no descendants of ATC A10 (concept 21600712); the ATC vocabulary is missing.", call. = FALSE)
    return(list(ancestors = .ATC_A10_CONCEPT, ca = ca))
  }
  if (is.null(cuis)) stop("diabetes_drug_rule = 'rxnorm_cuis' needs diabetes_rxnorm_cuis (a list of RxNorm CUIs or a file of them).", call. = FALSE)
  wanted <- .read_cuis(cuis)
  concept <- sub("concept_ancestor$", "concept", ca)
  codes <- paste0("'", gsub("'", "''", wanted), "'", collapse = ", ")
  ids <- DBI::dbGetQuery(con, sprintf("SELECT concept_id FROM %s WHERE vocabulary_id = 'RxNorm' AND concept_code IN (%s)", concept, codes))$concept_id
  if (!length(ids)) stop("None of diabetes_rxnorm_cuis is an RxNorm concept_code in this vocabulary.", call. = FALSE)
  if (length(ids) < length(wanted)) {
    warning(sprintf("diabetes_rxnorm_cuis: %d of %d CUIs are not RxNorm concepts in this vocabulary and are ignored.",
                    length(wanted) - length(ids), length(wanted)), call. = FALSE)
  }
  list(ancestors = sort(as.integer(ids)), ca = ca)
}

#' @keywords internal
#' @noRd
.create_metformin_only_table <- function(con, spec) {
  ca <- .resolve_concept_ancestor_ref(con, spec$schema)
  if (is.null(ca)) {
    warning("exclude_metformin_only_prediabetes_pcos: no usable concept_ancestor, so the concept sets are limited to the ",
            "listed ids and source-value patterns.", call. = FALSE)
  }
  DBI::dbExecute(con, sprintf("CREATE OR REPLACE TEMP TABLE %s AS SELECT person_id FROM (%s)", .STUDY_METFORMIN_TABLE,
                              .metformin_only_sql(spec$metformin_only_pcos, spec$schema, ca)))
  invisible(NULL)
}
