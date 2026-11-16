# Feature definitions as OHDSI concept sets, resolved through the vocabulary (v0.29.0).
# Mirror of python/omop_etl/concept_sets.py: the files in inst/extdata/concept_sets are shared by both packages.

.cs_dir <- function() {
  d <- system.file("extdata", "concept_sets", package = "omopduckdb")
  if (!nzchar(d)) d <- file.path("inst", "extdata", "concept_sets")
  if (!dir.exists(d)) stop("the bundled concept sets (inst/extdata/concept_sets) were not found", call. = FALSE)
  d
}

#' OHDSI concept sets behind the feature columns
#'
#' Labs, vitals and medication classes are defined by concept-set files in `inst/extdata/concept_sets/<domain>/<name>.json`,
#' in the format ATLAS, Circe and Capr use (`list(items = list(list(concept = list(CONCEPT_ID, CONCEPT_CODE, VOCABULARY_ID),
#' includeDescendants, includeMapped, isExcluded))))`), so a set can be pasted into ATLAS and a set exported from ATLAS can be
#' dropped in. `manifest.json` lists each domain's sets in feature-column order and carries the version.
#'
#' `resolve_concept_sets()` gives the concept ids the way Circe does: each item's concept (looked up by `VOCABULARY_ID` +
#' `CONCEPT_CODE` when it has no id), plus its descendants in `concept_ancestor` when `includeDescendants`, plus the source
#' concepts that map to those (`Maps to`) when `includeMapped`, minus the same for excluded items.
#'
#' A LOINC lab test has no descendants, so the codes of one lab are linked through their LOINC parts instead:
#' `loinc_coverage()` lists the codes a site's in-stay rows use for a set's analyte (same `Has component`) that the set does
#' not hold, and `extract_benchmark_features()` turns each into a `concept_set_coverage` warning. The Python script
#' `scripts/build_concept_sets.py` expands the bundled lab sets from the vocabulary.
#' @param name `"<domain>/<name>"` of a bundled set, or a path to an ATLAS / Circe JSON file.
#' @param domain One of `"labs"`, `"vitals"`, `"medications_ederri"`, `"medications"`.
#' @param expression A concept-set expression (`list(items = ...)`).
#' @param vocabulary Vocabulary of the codes.
#' @param codes,ids Codes of one vocabulary, or concept ids.
#' @param include_descendants Whether each item includes its descendants.
#' @param con A DBI connection holding `concept` (and `concept_ancestor`, `concept_relationship` when needed).
#' @param sets A named list of expressions.
#' @param warn Whether unknown codes and a missing `concept_ancestor` raise a warning.
#' @param cohort A data frame with `subject_id`, `cohort_start_date`, `cohort_end_date` (only in-stay rows are counted), or NULL.
#' @param pre_days Days before admission that count as in stay.
#' @param min_share Smallest share of an analyte's rows a missed code must hold to be listed.
#' @return `load_concept_set()`: a list with `name`, `domain`, `expression`. `load_concept_sets()`: a named list of
#'   expressions. `concept_set_codes()`, `concept_set_ids()`: vectors. `resolve_concept_sets()`: a data frame `name, concept_id`.
#'   `loinc_coverage()`: a data frame `name, concept_code, concept_name, same_property, blood, share`, or NULL when the
#'   vocabulary has no LOINC part links.
#' @name concept_sets
NULL

#' @rdname concept_sets
#' @export
concept_set_manifest <- function() jsonlite::fromJSON(file.path(.cs_dir(), "manifest.json"), simplifyVector = TRUE)

#' @rdname concept_sets
#' @export
list_concept_sets <- function(domain = NULL) {
  m <- concept_set_manifest()
  doms <- if (is.null(domain)) names(m$domains) else domain
  out <- unlist(lapply(doms, function(d) if (length(m$domains[[d]])) paste0(d, "/", m$domains[[d]])), use.names = FALSE)
  if (is.null(domain)) c(out, unlist(m$other)) else as.character(out)
}

.cs_check <- function(expr, where) {
  items <- expr$items
  if (!is.list(items) || !length(items)) stop(sprintf("concept set %s: 'items' must be a non-empty list", where), call. = FALSE)
  for (i in seq_along(items)) {
    c <- items[[i]]$concept
    if (is.null(c$CONCEPT_ID) && (is.null(c$VOCABULARY_ID) || is.null(c$CONCEPT_CODE)))
      stop(sprintf("concept set %s, item %d: needs CONCEPT_ID or VOCABULARY_ID + CONCEPT_CODE", where, i - 1L), call. = FALSE)
  }
  invisible(TRUE)
}

#' @rdname concept_sets
#' @export
load_concept_set <- function(name) {
  p <- if (grepl("\\.json$", name, ignore.case = TRUE)) name else file.path(.cs_dir(), paste0(name, ".json"))
  if (!file.exists(p)) stop(sprintf("no concept set '%s'", name), call. = FALSE)
  data <- jsonlite::fromJSON(p, simplifyVector = FALSE)
  if (!is.null(data$items) && is.null(data$expression)) data <- list(name = sub("\\.json$", "", basename(p)), expression = data)
  if (is.null(data$name)) data$name <- sub("\\.json$", "", basename(p))
  .cs_check(data$expression, name)
  data
}

#' @rdname concept_sets
#' @export
load_concept_sets <- function(domain) {
  full <- list_concept_sets(domain)
  if (!length(full)) stop(sprintf("no bundled concept sets for domain '%s'", domain), call. = FALSE)
  out <- lapply(full, function(f) load_concept_set(f)$expression)
  stats::setNames(out, sub("^[^/]+/", "", full))
}

#' @rdname concept_sets
#' @export
concept_set_codes <- function(expression, vocabulary = "LOINC") {
  keep <- Filter(function(it) !isTRUE(it$isExcluded) && identical(it$concept$VOCABULARY_ID, vocabulary) && !is.null(it$concept$CONCEPT_CODE),
                 expression$items)
  vapply(keep, function(it) as.character(it$concept$CONCEPT_CODE), character(1))
}

#' @rdname concept_sets
#' @export
concept_set_ids <- function(expression) {
  keep <- Filter(function(it) !isTRUE(it$isExcluded) && !is.null(it$concept$CONCEPT_ID), expression$items)
  vapply(keep, function(it) as.numeric(it$concept$CONCEPT_ID), numeric(1))
}

.cs_item <- function(concept, descendants = FALSE) {
  list(concept = concept, includeDescendants = isTRUE(descendants), includeMapped = FALSE, isExcluded = FALSE)
}

#' @rdname concept_sets
#' @export
concept_set_from_codes <- function(codes, vocabulary = "LOINC", include_descendants = FALSE) {
  list(items = lapply(as.character(codes), function(c) .cs_item(list(CONCEPT_CODE = c, VOCABULARY_ID = vocabulary), include_descendants)))
}

#' @rdname concept_sets
#' @export
concept_set_from_ids <- function(ids, include_descendants = TRUE) {
  list(items = lapply(as.numeric(ids), function(i) .cs_item(list(CONCEPT_ID = i), include_descendants)))
}

.cs_has <- function(con, table) {
  tryCatch(nrow(DBI::dbGetQuery(con, sprintf("SELECT 1 FROM %s LIMIT 1", table))) > 0, error = function(e) FALSE)
}

.cs_items_frame <- function(sets) {
  rows <- lapply(names(sets), function(n) {
    .cs_check(sets[[n]], n)
    do.call(rbind, lapply(sets[[n]]$items, function(it) {
      c <- it$concept
      data.frame(name = n, cid = if (is.null(c$CONCEPT_ID)) NA_real_ else as.numeric(c$CONCEPT_ID),
                 vocab = if (is.null(c$VOCABULARY_ID)) NA_character_ else c$VOCABULARY_ID,
                 code = if (is.null(c$CONCEPT_CODE)) NA_character_ else as.character(c$CONCEPT_CODE),
                 descendants = isTRUE(it$includeDescendants), mapped = isTRUE(it$includeMapped), excluded = isTRUE(it$isExcluded),
                 stringsAsFactors = FALSE)
    }))
  })
  do.call(rbind, rows)
}

#' @rdname concept_sets
#' @export
resolve_concept_sets <- function(con, sets, warn = TRUE) {
  items <- .cs_items_frame(sets)
  if (is.null(items) || !nrow(items)) return(data.frame(name = character(), concept_id = numeric()))
  has_concept <- .cs_has(con, "concept")
  ca <- if (.cs_has(con, "concept_ancestor")) "concept_ancestor" else NULL
  duckdb::duckdb_register(con, "_cs_items", items)
  on.exit({
    duckdb::duckdb_unregister(con, "_cs_items")
    for (t in c("_cs_base", "_cs_exp")) DBI::dbExecute(con, sprintf("DROP TABLE IF EXISTS %s", t))
  }, add = TRUE)
  lookup <- if (has_concept) "LEFT JOIN concept c ON i.cid IS NULL AND c.vocabulary_id = i.vocab AND c.concept_code = i.code"
            else "LEFT JOIN (SELECT CAST(NULL AS BIGINT) AS concept_id) c ON FALSE"
  DBI::dbExecute(con, paste("CREATE OR REPLACE TEMP TABLE _cs_base AS SELECT i.name, COALESCE(CAST(i.cid AS BIGINT), c.concept_id) AS concept_id,",
                            "i.code, i.descendants, i.mapped, i.excluded FROM _cs_items i", lookup))
  missing <- DBI::dbGetQuery(con, "SELECT DISTINCT name, code FROM _cs_base WHERE concept_id IS NULL ORDER BY 1, 2")
  need_ca <- DBI::dbGetQuery(con, "SELECT count(*) AS n FROM _cs_base WHERE descendants AND concept_id IS NOT NULL")$n > 0
  expanded <- "SELECT name, concept_id, mapped, excluded FROM _cs_base WHERE concept_id IS NOT NULL"
  if (!is.null(ca) && need_ca)
    expanded <- paste(expanded, "UNION SELECT b.name, ca.descendant_concept_id, b.mapped, b.excluded FROM _cs_base b",
                      "JOIN concept_ancestor ca ON ca.ancestor_concept_id = b.concept_id WHERE b.descendants")
  DBI::dbExecute(con, paste("CREATE OR REPLACE TEMP TABLE _cs_exp AS", expanded))
  mapped <- if (.cs_has(con, "concept_relationship"))
    paste("UNION SELECT e.name, cr.concept_id_1, e.excluded FROM _cs_exp e JOIN concept_relationship cr",
          "ON cr.concept_id_2 = e.concept_id AND cr.relationship_id = 'Maps to' WHERE e.mapped") else ""
  got <- DBI::dbGetQuery(con, paste(
    "WITH allc AS (SELECT name, concept_id, excluded FROM _cs_exp", mapped, ")",
    "SELECT DISTINCT name, CAST(concept_id AS DOUBLE) AS concept_id FROM allc a WHERE NOT a.excluded",
    "AND NOT EXISTS (SELECT 1 FROM allc x WHERE x.excluded AND x.name = a.name AND x.concept_id = a.concept_id)",
    "ORDER BY name, concept_id"))
  if (warn && nrow(missing))
    warning("concept-set codes not in this vocabulary (dropped): ",
            paste(vapply(split(missing$code, missing$name), paste, character(1), collapse = ", "), collapse = "; "), call. = FALSE)
  if (warn && need_ca && is.null(ca))
    warning("concept_ancestor not reachable: concept sets that include descendants keep only their listed concepts", call. = FALSE)
  got
}

.cs_parts_available <- function(con) {
  if (!.cs_has(con, "concept") || !.cs_has(con, "concept_relationship")) return(FALSE)
  DBI::dbGetQuery(con, paste("SELECT count(DISTINCT relationship_id) AS n FROM concept_relationship WHERE relationship_id IN",
                             "('Has component', 'Has property', 'Has scale type', 'Has system')"))$n == 4
}

.cs_blood <- function(x) {
  !is.na(x) & grepl("^(serum|plasma|blood|bld|ser|plas)\\b", trimws(x), ignore.case = TRUE, perl = TRUE) &
    !grepl("cord|fetus|fetal|donor|spot|product|unit|newborn|\\^", x, ignore.case = TRUE)
}

.cs_parts <- function(con) {
  DBI::dbGetQuery(con, paste(
    "SELECT CAST(t.concept_id AS DOUBLE) AS concept_id, t.concept_code, t.concept_name,",
    "CAST(MAX(CASE WHEN r.relationship_id = 'Has component' THEN r.concept_id_2 END) AS DOUBLE) AS component,",
    "CAST(MAX(CASE WHEN r.relationship_id = 'Has property' THEN r.concept_id_2 END) AS DOUBLE) AS property,",
    "CAST(MAX(CASE WHEN r.relationship_id = 'Has scale type' THEN r.concept_id_2 END) AS DOUBLE) AS scale,",
    "MAX(CASE WHEN r.relationship_id = 'Has system' THEN s.concept_name END) AS system_name",
    "FROM concept t JOIN concept_relationship r ON r.concept_id_1 = t.concept_id",
    "AND r.relationship_id IN ('Has component', 'Has property', 'Has scale type', 'Has time aspect', 'Has system')",
    "LEFT JOIN concept s ON s.concept_id = r.concept_id_2",
    "WHERE t.vocabulary_id = 'LOINC' AND (t.invalid_reason IS NULL OR t.invalid_reason = '')",
    "GROUP BY t.concept_id, t.concept_code, t.concept_name"))
}

#' @rdname concept_sets
#' @export
loinc_coverage <- function(con, sets, cohort = NULL, pre_days = 0, min_share = 0.01) {
  empty <- data.frame(name = character(), concept_code = character(), concept_name = character(), same_property = logical(),
                      blood = logical(), share = numeric())
  if (!.cs_parts_available(con) || !.cs_has(con, "measurement")) return(NULL)
  resolved <- resolve_concept_sets(con, sets, warn = FALSE)
  parts <- .cs_parts(con)
  cand <- do.call(rbind, lapply(names(sets), function(n) {
    ids <- resolved$concept_id[resolved$name == n]
    seeds <- parts[parts$concept_id %in% ids, , drop = FALSE]
    if (!nrow(seeds)) return(NULL)
    k <- parts[parts$component %in% stats::na.omit(seeds$component), , drop = FALSE]
    props <- paste(seeds$property, seeds$scale)
    data.frame(name = n, concept_id = k$concept_id, concept_code = k$concept_code, concept_name = k$concept_name,
               in_set = k$concept_id %in% ids, same_property = paste(k$property, k$scale) %in% props, blood = .cs_blood(k$system_name),
               stringsAsFactors = FALSE)
  }))
  if (is.null(cand) || !nrow(cand)) return(empty)
  duckdb::duckdb_register(con, "_cs_cand", cand[, c("name", "concept_id")])
  on.exit(duckdb::duckdb_unregister(con, "_cs_cand"), add = TRUE)
  src <- "FROM measurement m"
  if (!is.null(cohort) && all(c("subject_id", "cohort_start_date", "cohort_end_date") %in% names(cohort))) {
    duckdb::duckdb_register(con, "_cs_coh", cohort[, c("subject_id", "cohort_start_date", "cohort_end_date")])
    on.exit(duckdb::duckdb_unregister(con, "_cs_coh"), add = TRUE)
    src <- sprintf(paste("FROM _cs_coh c JOIN measurement m ON m.person_id = c.subject_id",
                         "AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - %d",
                         "AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)"), as.integer(pre_days))
  }
  counts <- DBI::dbGetQuery(con, paste("SELECT k.name, CAST(k.concept_id AS DOUBLE) AS concept_id, CAST(count(*) AS DOUBLE) AS n", src,
                                       "JOIN _cs_cand k ON k.concept_id = m.measurement_concept_id GROUP BY k.name, k.concept_id"))
  out <- merge(cand, counts, by = c("name", "concept_id"))
  if (!nrow(out)) return(empty)
  out$share <- out$n / stats::ave(out$n, out$name, FUN = sum)
  out <- out[!out$in_set & out$share >= min_share, , drop = FALSE]
  out <- out[order(out$name, -out$share), c("name", "concept_code", "concept_name", "same_property", "blood", "share")]
  rownames(out) <- NULL
  out
}

.cs_check_lab_coverage <- function(con, sets, cohort, pre_days, min_share = 0.01) {
  if (!length(sets)) return(invisible(NULL))
  miss <- tryCatch(loinc_coverage(con, sets, cohort = cohort, pre_days = pre_days, min_share = min_share),
                   error = function(e) { warning("lab concept-set coverage not checked: ", strsplit(conditionMessage(e), "\n")[[1]][1], call. = FALSE); FALSE })
  if (isFALSE(miss)) return(invisible(NULL))
  if (is.null(miss)) {
    warning("lab concept-set coverage not checked: the vocabulary has no LOINC part links (Has component ...), so lab codes ",
            "outside the bundled concept sets cannot be found", call. = FALSE)
    return(invisible(NULL))
  }
  for (n in unique(miss$name)) {
    g <- miss[miss$name == n, , drop = FALSE]
    desc <- paste0(g$concept_code, " ", sprintf("%.1f%%", 100 * g$share), ifelse(g$same_property, "", " (other unit type)"),
                   ifelse(g$blood, "", " (not blood)"), collapse = "; ")
    warning(sprintf(paste0("[concept_set_coverage] lab_%s: in-stay rows for this analyte use LOINC codes outside its concept set (%s); ",
                           "add them to concept_sets/labs/%s.json (with a unit conversion for another unit type)"), n, desc, n), call. = FALSE)
  }
  invisible(miss)
}
