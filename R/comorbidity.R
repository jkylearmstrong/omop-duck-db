#' Elixhauser comorbidity profile (31 domains, van Walraven score)
#'
#' Flags the 31 Elixhauser comorbidity domains (Quan et al. 2005 coding algorithms) in the look-back window
#' before each cohort row's index date and scores them with the van Walraven 2009 weights. It is the R
#' counterpart of Python `omop_etl.extract_elixhauser_comorbidities()`: both read the same curated code table,
#' `system.file("extdata", "comorbidity", "elixhauser_codes.csv", package = "omopduckdb")` (sources, weights,
#' hierarchy rules and known deviations are in `PROVENANCE.md` next to it), build the same SQL, and return the same
#' columns and values.
#'
#' The result has one row per DISTINCT (person, index date) of the cohort table, ordered by person then index date.
#' Persons without a qualifying condition still appear, with every flag 0 and a score of 0.
#'
#' A condition record counts for a domain when its `condition_source_value`, upper-cased with `.` and whitespace
#' removed, STARTS WITH one of the domain's ICD-10-CM / ICD-9-CM prefixes (`I50` matches `I50.9` and `i 50.9`; `4280`
#' matches `428.0`), or (`source_and_standard`) its `condition_concept_id` is a SNOMED ancestor of the domain or a
#' descendant of one in `concept_ancestor`. Only records dated strictly before the index date are used
#' (`[index - lookback_days, index)`), so nothing at or after the index leaks in.
#'
#' The profile is one statement of CTEs: nothing is created, so it works on read-only connections and leaves no
#' objects behind.
#'
#' **Hierarchy.** With `hierarchy_adjusted = TRUE`, `dm_comp` supersedes `dm_uncomp`, `mets` supersedes `solid_tumor`
#' and `htn_comp` supersedes `htn_uncomp` in the score and in the condition count; the 0/1 domain columns always stay
#' the raw flags. Only the mets rule changes the score (12 instead of 16 together with a solid tumour); the diabetes and
#' hypertension rules change the count. The standard-concept side of `dm_uncomp` is the set of diabetes types, which
#' includes every complicated form, so its raw flag is only meaningful after the hierarchy is applied.
#'
#' Prefixes are matched against any source value, whatever its coding system, so an ICD-9-CM `V` prefix can match an
#' ICD-10-CM transport-accident code and the ICD-10-CM prefixes E00-E03, E86, E87 and E890 can match ICD-9-CM
#' external-cause E codes (`PROVENANCE.md` lists them); profile data whose coding system you know.
#'
#' @param con Active DBI connection to DuckDB holding `condition_occurrence` and the cohort table (read-only is
#'   fine). `concept_ancestor` is looked up as `concept_ancestor` (main, a TEMP view or the connection's
#'   `search_path`, e.g. after [omop_connect()]), then as `central_vocab.main.concept_ancestor`.
#' @param cohort_table Table or view with one row per index event (default `"cohort"`); may be schema-qualified.
#' @param cohort_id Optional integer. Keep only rows with `cohort_definition_id = cohort_id`. If the table has no
#'   `cohort_definition_id` column a warning is raised and every row is profiled.
#' @param person_col Person id column of `cohort_table`, joined to `condition_occurrence.person_id`. It must exist:
#'   columns are never guessed.
#' @param index_date_col Index date column of `cohort_table`; must exist. It is cast to DATE to place the window.
#' @param lookback_days Window `[index - lookback_days, index)`: `condition_start_date` strictly before the index
#'   date and no more than `lookback_days` days earlier. `NULL` means all history before the index date.
#' @param source_and_standard Also match `condition_concept_id` against the domain's SNOMED ancestors and their
#'   descendants (default `TRUE`). If `concept_ancestor` cannot be resolved on the connection (or is empty) a warning is
#'   raised and only the source side is used. `FALSE` uses the source side only, without a warning.
#' @param hierarchy_adjusted Apply the hierarchy rules to the score and the condition count (default `TRUE`).
#'   `FALSE` gives the plain weighted sum and the plain count of raw flags.
#' @return A `data.frame` with the columns, in order: `person_col`, `index_date_col`, the 31 `elix_<domain>` flags
#'   (integer 0/1: chf, arrhythmia, valvular, pulm_circ, pvd, htn_uncomp, htn_comp, paralysis, neuro_other, copd,
#'   dm_uncomp, dm_comp, hypothyroid, renal_failure, liver_disease, pud, hiv, lymphoma, mets, solid_tumor, rheumatic,
#'   coagulopathy, obesity, weight_loss, fluid_electrolyte, blood_loss_anemia, deficiency_anemia, alcohol_abuse,
#'   drug_abuse, psychoses, depression), `elix_van_walraven_score` (integer; the weights run from -7 to 12, so it can
#'   be negative) and `elix_total_conditions` (integer count of flagged domains).
#' @seealso [extract_charlson_index()]
#' @examples
#' con <- DBI::dbConnect(duckdb::duckdb())
#' DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS
#'   SELECT 1 AS person_id, 0 AS condition_concept_id, DATE '2021-05-01' AS condition_start_date,
#'          'I50.9' AS condition_source_value")
#' DBI::dbExecute(con, "CREATE TABLE cohort AS
#'   SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
#' extract_elixhauser_comorbidities(con, source_and_standard = FALSE)[
#'   , c("subject_id", "elix_chf", "elix_van_walraven_score")]
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' @export
extract_elixhauser_comorbidities <- function(con,
                                             cohort_table = "cohort",
                                             cohort_id = NULL,
                                             person_col = "subject_id",
                                             index_date_col = "cohort_start_date",
                                             lookback_days = 365,
                                             source_and_standard = TRUE,
                                             hierarchy_adjusted = TRUE) {
  .comorb_profile(con, .comorb_elixhauser, "extract_elixhauser_comorbidities", cohort_table, cohort_id, person_col,
                  index_date_col, lookback_days, source_and_standard, hierarchy_adjusted)
}

#' Charlson comorbidity profile (17 categories, Charlson index)
#'
#' Flags the 17 Charlson categories (Quan et al. 2005 coding algorithms) in the look-back window before each cohort
#' row's index date and scores them with the original Charlson 1987 weights. It is the R counterpart of Python
#' `omop_etl.extract_charlson_index()`: both read the same curated code table,
#' `system.file("extdata", "comorbidity", "charlson_codes.csv", package = "omopduckdb")` (sources, weights, hierarchy
#' rules and known deviations are in `PROVENANCE.md` next to it), build the same SQL, and return the same columns and
#' values.
#'
#' The result has one row per DISTINCT (person, index date) of the cohort table, ordered by person then index date.
#' Persons without a qualifying condition still appear, with every flag 0 and an index of 0.
#'
#' A condition record counts for a category when its `condition_source_value`, upper-cased with `.` and whitespace
#' removed, STARTS WITH one of the category's ICD-10-CM / ICD-9-CM prefixes (`I50` matches `I50.9` and `i 50.9`; `4280`
#' matches `428.0`), or (`source_and_standard`) its `condition_concept_id` is a SNOMED ancestor of the category or a
#' descendant of one in `concept_ancestor`. Only records dated strictly before the index date are used
#' (`[index - lookback_days, index)`), so nothing at or after the index leaks in.
#'
#' The profile is one statement of CTEs: nothing is created, so it works on read-only connections and leaves no
#' objects behind.
#'
#' **Hierarchy.** With `hierarchy_adjusted = TRUE`, `mod_severe_liver` supersedes `mild_liver`, `dm_comp` supersedes
#' `dm_uncomp` and `mets` supersedes `malignancy` in the index and in the condition count; the 0/1 category columns
#' always stay the raw flags.
#'
#' Metastatic solid tumour has no standard-concept side and is detected from ICD source values only. Non-melanoma skin
#' cancer cannot be excluded on the standard side. ICD-9-CM prefixes beginning with `V` can collide with ICD-10-CM
#' transport-accident codes. All three caveats are described in `PROVENANCE.md`.
#'
#' @inheritParams extract_elixhauser_comorbidities
#' @param hierarchy_adjusted Apply the hierarchy rules to the index and the condition count (default `TRUE`).
#'   `FALSE` gives the plain weighted sum and the plain count of raw flags.
#' @return A `data.frame` with the columns, in order: `person_col`, `index_date_col`, the 17 `cci_<category>` flags
#'   (integer 0/1: mi, chf, pvd, cevd, dementia, copd, rheum, pud, mild_liver, dm_uncomp, dm_comp, plegia, renal,
#'   malignancy, mod_severe_liver, mets, hiv), `charlson_index` (integer weighted sum) and `cci_total_conditions`
#'   (integer count of flagged categories).
#' @seealso [extract_elixhauser_comorbidities()]
#' @examples
#' con <- DBI::dbConnect(duckdb::duckdb())
#' DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS
#'   SELECT 1 AS person_id, 0 AS condition_concept_id, DATE '2021-05-01' AS condition_start_date,
#'          'C78.0' AS condition_source_value")
#' DBI::dbExecute(con, "CREATE TABLE cohort AS
#'   SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
#' extract_charlson_index(con, source_and_standard = FALSE)[
#'   , c("subject_id", "cci_mets", "charlson_index")]
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' @export
extract_charlson_index <- function(con,
                                   cohort_table = "cohort",
                                   cohort_id = NULL,
                                   person_col = "subject_id",
                                   index_date_col = "cohort_start_date",
                                   lookback_days = 365,
                                   source_and_standard = TRUE,
                                   hierarchy_adjusted = TRUE) {
  .comorb_profile(con, .comorb_charlson, "extract_charlson_index", cohort_table, cohort_id, person_col,
                  index_date_col, lookback_days, source_and_standard, hierarchy_adjusted)
}

# ---------------------------------------------------------------------------------------------
# Internals. Base R + DBI only (qualified calls); the SQL mirrors python/omop_etl/comorbidity.py.
# ---------------------------------------------------------------------------------------------

.comorb_csv_header <- c("index", "domain", "column", "label", "weight", "code_system", "code", "note")

# (dominant, suppressed): the suppressed domain adds nothing to the score or the count when the dominant one is present
# (inst/extdata/comorbidity/PROVENANCE.md, "Hierarchy rules").
.comorb_elixhauser <- list(
  index = "elixhauser", file = "elixhauser_codes.csv",
  score_col = "elix_van_walraven_score", total_col = "elix_total_conditions",
  hierarchy = list(c("dm_comp", "dm_uncomp"), c("mets", "solid_tumor"), c("htn_comp", "htn_uncomp"))
)
.comorb_charlson <- list(
  index = "charlson", file = "charlson_codes.csv",
  score_col = "charlson_index", total_col = "cci_total_conditions",
  hierarchy = list(c("mod_severe_liver", "mild_liver"), c("dm_comp", "dm_uncomp"), c("mets", "malignancy"))
)

# Where concept_ancestor may live, in order: the connection's search path (main, a TEMP view, or the vocabulary schema
# omop_connect() puts on it), then an attached central vocabulary.
.comorb_concept_ancestor_candidates <- c(
  "concept_ancestor", "central_vocab.main.concept_ancestor", "central_vocab.concept_ancestor"
)
.comorb_condition_columns <- c("person_id", "condition_concept_id", "condition_start_date", "condition_source_value")

.comorb_qi <- function(x) paste0('"', gsub('"', '""', x, fixed = TRUE), '"')
.comorb_qs <- function(x) paste0("'", gsub("'", "''", x, fixed = TRUE), "'")
.comorb_num <- function(x) sprintf("%.0f", x)

# Installed copy of a code table, else the source-tree copy under the working directory.
.comorb_code_path <- function(file) {
  path <- system.file("extdata", "comorbidity", file, package = "omopduckdb")
  if (!nzchar(path) || !file.exists(path)) path <- file.path(getwd(), "inst", "extdata", "comorbidity", file)
  path
}

# The code table of `spec` -> list(domains = data.frame(domain, column, weight), icd = data.frame(domain, code),
# ancestors = data.frame(domain, concept_id)), validated.
.comorb_load_codes <- function(spec) {
  path <- .comorb_code_path(spec$file)
  if (!file.exists(path)) {
    stop("Comorbidity code table not found: expected inst/extdata/comorbidity/", spec$file, " (checked ", path, ").",
         call. = FALSE)
  }
  df <- utils::read.csv(path, colClasses = "character", na.strings = character(0), stringsAsFactors = FALSE,
                        check.names = FALSE, encoding = "UTF-8")
  if (!identical(names(df), .comorb_csv_header)) {
    stop(path, ": expected header ", paste(.comorb_csv_header, collapse = ","), ", got ", paste(names(df), collapse = ","),
         call. = FALSE)
  }
  fail <- function(rows, what) {
    stop(path, ": ", what, " (data row", if (length(rows) > 1L) "s" else "", " ",
         paste(utils::head(rows, 5L), collapse = ", "), ")", call. = FALSE)
  }
  bad <- which(df$index != spec$index)
  if (length(bad)) fail(bad, paste0("index must be '", spec$index, "'"))
  bad <- which(!grepl("^[a-z][a-z0-9_]*$", df$domain) | !grepl("^[a-z][a-z0-9_]*$", df$column))
  if (length(bad)) fail(bad, "domain and column must be lower-case identifiers")
  bad <- which(!grepl("^-?[0-9]+$", df$weight))
  if (length(bad)) fail(bad, "weight must be an integer")
  bad <- which(!df$code_system %in% c("ICD10CM", "ICD9CM", "SNOMED_ANCESTOR"))
  if (length(bad)) fail(bad, "unknown code_system")
  is_snomed <- df$code_system == "SNOMED_ANCESTOR"
  bad <- which(!is_snomed & !grepl("^[A-Z0-9]+$", df$code))
  if (length(bad)) fail(bad, "ICD prefixes must be upper-case letters/digits without dots")
  bad <- which(is_snomed & !grepl("^[0-9]+$", df$code))
  if (length(bad)) fail(bad, "SNOMED_ANCESTOR codes must be integer concept_ids")

  domain_order <- unique(df$domain)
  first <- match(domain_order, df$domain)
  domains <- data.frame(domain = domain_order, column = df$column[first], weight = as.integer(df$weight[first]),
                        stringsAsFactors = FALSE)
  same <- paste(df$domain, df$column, df$weight) == paste(domains$domain, domains$column, domains$weight)[match(df$domain, domains$domain)]
  if (!all(same)) fail(which(!same), "a domain has more than one column or weight")
  if (anyDuplicated(domains$column)) stop(path, ": two domains share a column name", call. = FALSE)
  if (!any(!is_snomed)) stop(path, " contains no ICD prefixes", call. = FALSE)
  for (rule in spec$hierarchy) {
    missing <- setdiff(rule, domains$domain)
    if (length(missing)) {
      stop(path, ": hierarchy rule ", rule[1], " > ", rule[2], " refers to missing domain(s) ",
           paste(missing, collapse = ", "), call. = FALSE)
    }
  }
  list(
    domains = domains,
    icd = data.frame(domain = df$domain[!is_snomed], code = df$code[!is_snomed], stringsAsFactors = FALSE),
    ancestors = data.frame(domain = df$domain[is_snomed], concept_id = as.numeric(df$code[is_snomed]),
                           stringsAsFactors = FALSE)
  )
}

# whole number (or NULL when allowed); minimum NULL means no lower bound
.comorb_check_int <- function(x, what, allow_null, minimum = NULL) {
  if (is.null(x) && allow_null) return(NULL)
  ok <- is.numeric(x) && length(x) == 1L && !is.na(x) && is.finite(x) && x == floor(x) &&
    (is.null(minimum) || x >= minimum)
  if (!ok) {
    stop(what, " must be ", if (identical(minimum, 0)) "a non-negative whole number" else "a whole number",
         if (allow_null) " or NULL", "; got ", paste(format(x), collapse = " "), call. = FALSE)
  }
  x
}

.comorb_check_flag <- function(x, what) {
  if (!(is.logical(x) && length(x) == 1L && !is.na(x))) {
    stop(what, " must be TRUE or FALSE; got ", paste(format(x), collapse = " "), call. = FALSE)
  }
  x
}

.comorb_qualified_table <- function(name) {
  if (!(is.character(name) && length(name) == 1L && !is.na(name) &&
        grepl("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){0,2}$", name))) {
    stop("cohort_table must be a table or view name, optionally schema-qualified (letters, digits and underscores ",
         "only); got ", paste(format(name), collapse = " "), call. = FALSE)
  }
  paste(.comorb_qi(strsplit(name, ".", fixed = TRUE)[[1]]), collapse = ".")
}

# DuckDB reports a missing table as a "Catalog Error" and a missing catalog or column as a "Binder Error".
.comorb_unresolved <- function(e) grepl("(Catalog|Binder) Error", conditionMessage(e))

# Column names of a table or view, NULL when it does not exist.
.comorb_columns_of <- function(con, qualified) {
  tryCatch(
    DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", qualified))$column_name,
    error = function(e) if (grepl("Catalog Error", conditionMessage(e))) NULL else stop(e)
  )
}

# The table's own spelling of column `wanted` (matched case-insensitively); error if absent.
.comorb_find_column <- function(columns, wanted, param, table) {
  if (!(is.character(wanted) && length(wanted) == 1L && !is.na(wanted) && nzchar(wanted))) {
    stop(param, " must be a column name; got ", paste(format(wanted), collapse = " "), call. = FALSE)
  }
  hit <- match(tolower(wanted), tolower(columns))
  if (is.na(hit)) {
    stop(param, " '", wanted, "' is not a column of '", table, "' (columns: ", paste(columns, collapse = ", "),
         "). Set ", param, "= explicitly; columns are never guessed.", call. = FALSE)
  }
  columns[[hit]]
}

# First reachable, non-empty concept_ancestor -> list(ref, reason = ""); else list(ref = NULL, reason).
.comorb_resolve_concept_ancestor <- function(con) {
  seen_empty <- FALSE
  for (ref in .comorb_concept_ancestor_candidates) {
    probe <- tryCatch(
      DBI::dbGetQuery(con, sprintf("SELECT ancestor_concept_id, descendant_concept_id FROM %s LIMIT 1", ref)),
      error = function(e) if (.comorb_unresolved(e)) NULL else stop(e)
    )
    if (is.null(probe)) next
    if (nrow(probe) > 0L) return(list(ref = ref, reason = ""))
    seen_empty <- TRUE
  }
  list(ref = NULL, reason = if (seen_empty) {
    "concept_ancestor exists on this connection but is empty"
  } else {
    paste0("concept_ancestor could not be resolved on this connection (looked for ",
           paste(.comorb_concept_ancestor_candidates, collapse = ", "), ")")
  })
}

# The profiler as one statement of CTEs (see the Python twin for the walk-through).
.comorb_build_sql <- function(spec, codes, qualified, person, index_col, person_out, index_out, cohort_where,
                              lookback_days, concept_ancestor, hierarchy_adjusted) {
  icd_values <- paste(sprintf("(%s, %s)", .comorb_qs(codes$icd$domain), .comorb_qs(codes$icd$code)),
                      collapse = ",\n      ")
  ctes <- c(
    sprintf("icd AS (\n  SELECT domain, code, length(code) AS len FROM (VALUES\n      %s\n  ) AS v(domain, code)\n)",
            icd_values),
    sprintf(paste0("cohort_base AS (\n  SELECT DISTINCT c.%s AS person_key, c.%s AS index_key,\n",
                   "         CAST(c.%s AS DATE) AS idx_d\n  FROM %s AS c%s\n)"),
            .comorb_qi(person), .comorb_qi(index_col), .comorb_qi(index_col), qualified,
            if (nzchar(cohort_where)) paste0(" WHERE ", cohort_where) else ""),
    paste0("cond AS (\n",
           "  SELECT co.person_id AS pid, co.condition_concept_id AS cid,\n",
           "         CAST(co.condition_start_date AS DATE) AS cdate,\n",
           "         upper(replace(regexp_replace(coalesce(co.condition_source_value, ''), '\\s+', '', 'g'), '.', '')) AS src\n",
           "  FROM condition_occurrence AS co\n",
           "  WHERE co.condition_start_date IS NOT NULL\n",
           "    AND co.person_id IN (SELECT person_key FROM cohort_base)\n)"),
    paste0("src_hit AS (\n",
           "  SELECT DISTINCT s.src, i.domain\n",
           "  FROM (SELECT DISTINCT src FROM cond) AS s\n",
           "  CROSS JOIN (SELECT DISTINCT len FROM icd) AS l\n",
           "  JOIN icd AS i ON i.len = l.len AND i.code = left(s.src, l.len)\n)")
  )
  cond_hit <- "SELECT c.pid, c.cdate, h.domain FROM cond AS c JOIN src_hit AS h ON h.src = c.src"
  if (!is.null(concept_ancestor)) {
    anc_values <- paste(sprintf("(%s, %s)", .comorb_qs(codes$ancestors$domain), .comorb_num(codes$ancestors$concept_id)),
                        collapse = ",\n      ")
    ctes <- c(
      ctes,
      sprintf(paste0("anc AS (\n  SELECT domain, CAST(concept_id AS BIGINT) AS concept_id FROM (VALUES\n      %s\n",
                     "  ) AS v(domain, concept_id)\n)"), anc_values),
      paste0("std_hit AS (\n",
             "  SELECT DISTINCT domain, concept_id FROM (\n",
             "    SELECT a.domain, ca.descendant_concept_id AS concept_id FROM anc AS a\n",
             "    JOIN ", concept_ancestor, " AS ca ON ca.ancestor_concept_id = a.concept_id\n",
             "    UNION ALL\n",
             "    SELECT domain, concept_id FROM anc\n",
             "  )\n)")
    )
    cond_hit <- paste0(cond_hit, "\n    UNION ALL\n",
                       "    SELECT c.pid, c.cdate, h.domain FROM cond AS c JOIN std_hit AS h ON h.concept_id = c.cid")
  }
  window <- "ch.cdate < cb.idx_d"
  if (!is.null(lookback_days)) window <- paste0(window, " AND ch.cdate >= cb.idx_d - ", .comorb_num(lookback_days))
  ctes <- c(
    ctes,
    sprintf("cond_hit AS (\n    %s\n)", cond_hit),
    sprintf(paste0("hits AS (\n  SELECT DISTINCT cb.person_key, cb.index_key, ch.domain\n  FROM cohort_base AS cb\n",
                   "  JOIN cond_hit AS ch ON ch.pid = cb.person_key AND %s\n)"), window)
  )

  dom <- codes$domains
  flag_aggs <- paste(sprintf("MAX(CASE WHEN domain = %s THEN 1 ELSE 0 END) AS %s", .comorb_qs(dom$domain),
                             .comorb_qi(dom$column)), collapse = ",\n         ")
  flag_cols <- paste(sprintf("CAST(COALESCE(f.%s, 0) AS INTEGER) AS %s", .comorb_qi(dom$column), .comorb_qi(dom$column)),
                     collapse = ",\n         ")
  ctes <- c(
    ctes,
    sprintf("flags AS (\n  SELECT person_key, index_key,\n         %s\n  FROM hits GROUP BY person_key, index_key\n)",
            flag_aggs),
    paste0("flat AS (\n  SELECT cb.person_key, cb.index_key,\n         ", flag_cols, "\n  FROM cohort_base AS cb\n",
           "  LEFT JOIN flags AS f ON f.person_key = cb.person_key AND f.index_key = cb.index_key\n)")
  )

  counted <- .comorb_qi(dom$column)
  if (hierarchy_adjusted) {
    for (rule in spec$hierarchy) {
      i <- match(rule[2], dom$domain)
      counted[i] <- sprintf("(CASE WHEN %s = 1 THEN 0 ELSE %s END)", .comorb_qi(dom$column[match(rule[1], dom$domain)]),
                            .comorb_qi(dom$column[i]))
    }
  }
  total_sql <- paste(counted, collapse = " + ")
  score_sql <- paste(sprintf("(%s * (%d))", counted, dom$weight), collapse = " + ")
  paste0(
    "WITH\n", paste(ctes, collapse = ",\n"), "\n",
    sprintf("SELECT person_key AS %s, index_key AS %s, %s,\n", .comorb_qi(person_out), .comorb_qi(index_out),
            paste(.comorb_qi(dom$column), collapse = ", ")),
    sprintf("       CAST((%s) AS INTEGER) AS %s,\n", score_sql, .comorb_qi(spec$score_col)),
    sprintf("       CAST((%s) AS INTEGER) AS %s\n", total_sql, .comorb_qi(spec$total_col)),
    "FROM flat\n",
    "ORDER BY person_key, index_key"
  )
}

.comorb_profile <- function(con, spec, fn, cohort_table, cohort_id, person_col, index_date_col, lookback_days,
                            source_and_standard, hierarchy_adjusted) {
  lookback <- .comorb_check_int(lookback_days, "lookback_days", allow_null = TRUE, minimum = 0)
  cohort <- .comorb_check_int(cohort_id, "cohort_id", allow_null = TRUE)
  .comorb_check_flag(source_and_standard, "source_and_standard")
  .comorb_check_flag(hierarchy_adjusted, "hierarchy_adjusted")
  codes <- .comorb_load_codes(spec)
  qualified <- .comorb_qualified_table(cohort_table)
  if (!inherits(con, "DBIConnection") || !DBI::dbIsValid(con)) stop("con must be an open DBI connection", call. = FALSE)

  cohort_cols <- .comorb_columns_of(con, qualified)
  if (is.null(cohort_cols)) {
    stop("cohort_table '", cohort_table, "' does not exist on this connection", call. = FALSE)
  }
  person <- .comorb_find_column(cohort_cols, person_col, "person_col", cohort_table)
  index_col <- .comorb_find_column(cohort_cols, index_date_col, "index_date_col", cohort_table)
  if (identical(person, index_col)) stop("person_col and index_date_col must be different columns", call. = FALSE)
  clash <- intersect(c(tolower(person_col), tolower(index_date_col)),
                     c(codes$domains$column, spec$score_col, spec$total_col))
  if (length(clash)) {
    stop("person_col / index_date_col would collide with output column(s) ", paste(clash, collapse = ", "),
         call. = FALSE)
  }

  condition_cols <- .comorb_columns_of(con, .comorb_qi("condition_occurrence"))
  if (is.null(condition_cols)) stop("condition_occurrence does not exist on this connection", call. = FALSE)
  missing <- setdiff(.comorb_condition_columns, tolower(condition_cols))
  if (length(missing)) {
    stop("condition_occurrence is missing column(s) ", paste(missing, collapse = ", "), call. = FALSE)
  }

  cohort_where <- ""
  if (!is.null(cohort)) {
    cdef <- cohort_cols[match("cohort_definition_id", tolower(cohort_cols))]
    if (is.na(cdef)) {
      warning(fn, "(): cohort_id=", .comorb_num(cohort), " was ignored because '", cohort_table,
              "' has no cohort_definition_id column; every row of the table is profiled.", call. = FALSE)
    } else {
      cohort_where <- sprintf("c.%s = %s", .comorb_qi(cdef), .comorb_num(cohort))
    }
  }
  null_rows <- DBI::dbGetQuery(con, paste0(
    sprintf("SELECT count(*) AS n FROM %s AS c WHERE (c.%s IS NULL OR c.%s IS NULL)", qualified, .comorb_qi(person),
            .comorb_qi(index_col)),
    if (nzchar(cohort_where)) paste0(" AND ", cohort_where) else ""
  ))$n
  if (null_rows > 0) {
    stop("'", cohort_table, "' has ", null_rows, " row(s) with a NULL ", person_col, " or ", index_date_col,
         "; an index date is required to place the look-back window. Remove those rows first.", call. = FALSE)
  }

  concept_ancestor <- NULL
  if (source_and_standard && nrow(codes$ancestors) > 0L) {
    resolved <- .comorb_resolve_concept_ancestor(con)
    concept_ancestor <- resolved$ref
    if (is.null(concept_ancestor)) {
      warning(fn, "(): source_and_standard = TRUE but ", resolved$reason, ". Falling back to source-value (ICD prefix) ",
              "matching only; standard-concept (SNOMED descendant) matches are NOT included. Load or attach the ",
              "vocabulary (e.g. omop_connect()) or pass source_and_standard = FALSE to silence this warning.",
              call. = FALSE)
    }
  }

  sql <- .comorb_build_sql(spec, codes, qualified, person, index_col, person_col, index_date_col, cohort_where,
                           lookback, concept_ancestor, hierarchy_adjusted)
  DBI::dbGetQuery(con, sql)
}
