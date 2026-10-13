#' Composable phenotype cohort DSL compiled to single-pass DuckDB SQL
#'
#' Build a cohort definition from clinical criteria and logical operators, then compile it to one DuckDB
#' query (or run it into a cohort table) - the R mirror of `omop_etl.phenotype_dsl` in Python:
#'
#' ```
#' t2dm <- condition_criteria(icd_codes = "E11", concept_ids = 201826) |
#'   measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5) |
#'   drug_criteria(concept_ids = 21600712, min_days_supply = 30)
#' execute_phenotype(con, t2dm & !condition_criteria(icd_codes = "E10"), "cohort", cohort_definition_id = 7)
#' ```
#'
#' Criteria ([condition_criteria()], [measurement_criteria()], [drug_criteria()], [everyone()]) are leaves;
#' `|` / [phenotype_union()], `&` / [phenotype_intersect()] and `!` / [phenotype_not()] compose them.
#'
#' **Single pass.** Every leaf emits `(person_id, event_date, leaf)` rows into one `UNION ALL`; one
#' `GROUP BY person_id` computes each leaf's qualifying date with `FILTER` clauses, and the boolean tree is
#' evaluated as one `WHERE` over those columns. No leaf is re-scanned per operator and no intermediate cohort
#' tables are created.
#'
#' **Index date.** A person's index date is the earliest qualifying event for a union, and the latest of the
#' operands' qualifying dates for an intersection (the moment every criterion is first satisfied). `!` only
#' excludes. With `index_table =` the index date is that table's date instead, and a leaf's `lookback_days` /
#' `lookahead_days` are measured relative to it: lookback only gives `[index - n, index]`, lookahead only
#' `[index, index + m]`, both `[index - n, index + m]`.
#'
#' @param concept_ids Standard concept ids (descendants are included via `concept_ancestor` when
#'   `include_descendants = TRUE`).
#' @param icd_codes ICD-9/10 code prefixes (`"E11"` matches `E11.9`), matched against
#'   `condition_source_value` and the source concept's code.
#' @param icd_vocabularies Vocabularies the source-concept match is restricted to.
#' @param diagnosis `"any"`, `"primary"` or `"secondary"` (via `condition_status_concept_id`).
#' @param loinc_codes LOINC codes, matched on `measurement_source_value` and the concept's code.
#' @param value_gte,value_gt,value_lte,value_lt Thresholds on `value_as_number` (all must hold).
#' @param unit_concept_ids Restrict measurements to these units.
#' @param atc_codes ATC codes resolved through `concept` as additional ancestor concepts.
#' @param min_days_supply Require `days_supply` (or end - start when unset) of at least this many days.
#' @param include_descendants Expand `concept_ids` through `concept_ancestor`.
#' @param min_occurrences Require at least this many qualifying records; the index date is the n-th one.
#' @param lookback_days,lookahead_days Window around the index table's date (requires `index_table`).
#' @name phenotype_criteria
NULL

#' @keywords internal
#' @noRd
.pheno_state <- new.env(parent = emptyenv())
.pheno_state$counter <- 0L

#' @keywords internal
#' @noRd
.pheno_leaf <- function(type, fields) {
  .pheno_state$counter <- .pheno_state$counter + 1L
  structure(c(list(type = type, .id = .pheno_state$counter), fields),
            class = c(paste0(type, "_criteria"), "phenotype_leaf", "phenotype_expr"))
}

#' @rdname phenotype_criteria
#' @export
condition_criteria <- function(concept_ids = NULL, icd_codes = NULL, icd_vocabularies = c("ICD9CM", "ICD10CM"),
                               diagnosis = c("any", "primary", "secondary"), include_descendants = TRUE,
                               min_occurrences = 1L, lookback_days = NULL, lookahead_days = NULL) {
  .pheno_leaf("condition", list(
    concept_ids = concept_ids, icd_codes = icd_codes, icd_vocabularies = icd_vocabularies,
    diagnosis = match.arg(diagnosis), include_descendants = include_descendants,
    min_occurrences = min_occurrences, lookback_days = lookback_days, lookahead_days = lookahead_days
  ))
}

#' @rdname phenotype_criteria
#' @export
measurement_criteria <- function(concept_ids = NULL, loinc_codes = NULL, value_gte = NULL, value_gt = NULL,
                                 value_lte = NULL, value_lt = NULL, unit_concept_ids = NULL,
                                 include_descendants = FALSE, min_occurrences = 1L,
                                 lookback_days = NULL, lookahead_days = NULL) {
  .pheno_leaf("measurement", list(
    concept_ids = concept_ids, loinc_codes = loinc_codes, value_gte = value_gte, value_gt = value_gt,
    value_lte = value_lte, value_lt = value_lt, unit_concept_ids = unit_concept_ids,
    include_descendants = include_descendants, min_occurrences = min_occurrences,
    lookback_days = lookback_days, lookahead_days = lookahead_days
  ))
}

#' @rdname phenotype_criteria
#' @export
drug_criteria <- function(concept_ids = NULL, atc_codes = NULL, min_days_supply = NULL, include_descendants = TRUE,
                          min_occurrences = 1L, lookback_days = NULL, lookahead_days = NULL) {
  .pheno_leaf("drug", list(
    concept_ids = concept_ids, atc_codes = atc_codes, min_days_supply = min_days_supply,
    include_descendants = include_descendants, min_occurrences = min_occurrences,
    lookback_days = lookback_days, lookahead_days = lookahead_days
  ))
}

#' All persons (base population for `!x`)
#'
#' Index date is the start of observation, or the index table's date when one is given.
#'
#' @return A phenotype expression.
#' @export
everyone <- function() .pheno_leaf("everyone", list())

#' Logical operators over phenotype expressions
#'
#' @param ... Two or more phenotype expressions (nested unions / intersections are flattened).
#' @param x A phenotype expression.
#' @return A phenotype expression.
#' @name phenotype_operators
#' @export
phenotype_union <- function(...) {
  kids <- list(...)
  if (length(kids) < 2) stop("phenotype_union needs at least two operands.")
  flat <- unlist(lapply(kids, function(k) if (inherits(k, "phenotype_union")) k$children else list(k)), recursive = FALSE)
  structure(list(type = "union", children = flat), class = c("phenotype_union", "phenotype_expr"))
}

#' @rdname phenotype_operators
#' @export
phenotype_intersect <- function(...) {
  kids <- list(...)
  if (length(kids) < 2) stop("phenotype_intersect needs at least two operands.")
  flat <- unlist(lapply(kids, function(k) if (inherits(k, "phenotype_intersect")) k$children else list(k)), recursive = FALSE)
  structure(list(type = "intersect", children = flat), class = c("phenotype_intersect", "phenotype_expr"))
}

#' @rdname phenotype_operators
#' @export
phenotype_not <- function(x) {
  structure(list(type = "not", child = x), class = c("phenotype_not", "phenotype_expr"))
}

#' @export
Ops.phenotype_expr <- function(e1, e2) {
  switch(.Generic,
    "|" = phenotype_union(e1, e2),
    "&" = phenotype_intersect(e1, e2),
    "!" = phenotype_not(e1),
    stop("Operator '", .Generic, "' is not defined for phenotype expressions; use |, & and !.")
  )
}

#' @keywords internal
#' @noRd
.pheno_is_leaf <- function(x) inherits(x, "phenotype_leaf")

#' @keywords internal
#' @noRd
.pheno_collect_leaves <- function(node, acc = list()) {
  if (.pheno_is_leaf(node)) {
    if (!any(vapply(acc, function(l) identical(l$.id, node$.id), logical(1)))) acc[[length(acc) + 1L]] <- node
    return(acc)
  }
  if (inherits(node, "phenotype_union") || inherits(node, "phenotype_intersect")) {
    for (ch in node$children) acc <- .pheno_collect_leaves(ch, acc)
    return(acc)
  }
  if (inherits(node, "phenotype_not")) return(.pheno_collect_leaves(node$child, acc))
  stop("Unsupported phenotype node: ", class(node)[[1]])
}

#' @keywords internal
#' @noRd
.pheno_windowed <- function(leaf) !is.null(leaf$lookback_days) || !is.null(leaf$lookahead_days)

#' @keywords internal
#' @noRd
.pheno_window_sql <- function(leaf, event_expr) {
  if (!.pheno_windowed(leaf)) return("")
  before <- if (!is.null(leaf$lookback_days)) paste0(" - ", as.integer(leaf$lookback_days)) else ""
  after <- if (!is.null(leaf$lookahead_days)) paste0(" + ", as.integer(leaf$lookahead_days)) else ""
  paste0(event_expr, " >= idx.index_date", before, " AND ", event_expr, " <= idx.index_date", after)
}

#' @keywords internal
#' @noRd
.pheno_q <- function(x) paste0("'", gsub("'", "''", as.character(x), fixed = TRUE), "'")

#' @keywords internal
#' @noRd
.pheno_concept_set <- function(name, ids, include_descendants, ancestor_table, code_seeds = NULL) {
  seeds <- character(0)
  if (length(ids)) {
    seeds <- c(seeds, paste0("SELECT * FROM (VALUES ", paste0("(", as.integer(ids), ")", collapse = ", "), ") t(concept_id)"))
  }
  if (!is.null(code_seeds) && length(code_seeds$codes)) {
    seeds <- c(seeds, sprintf("SELECT concept_id FROM %s WHERE vocabulary_id LIKE %s AND concept_code IN (%s)",
                              code_seeds$concept_table, .pheno_q(code_seeds$vocab),
                              paste(.pheno_q(code_seeds$codes), collapse = ", ")))
  }
  if (!length(seeds)) return(NULL)
  seed_sql <- paste(seeds, collapse = " UNION ")
  if (!isTRUE(include_descendants)) return(sprintf("%s AS (%s)", name, seed_sql))
  sprintf(paste0("%s AS (SELECT concept_id FROM (%s) UNION SELECT descendant_concept_id FROM %s ",
                 "WHERE ancestor_concept_id IN (SELECT concept_id FROM (%s)))"),
          name, seed_sql, ancestor_table, seed_sql)
}

#' Compile a phenotype expression to DuckDB SQL
#'
#' Returns one `SELECT` yielding `(cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)`;
#' the end date equals the start date (an event cohort). No connection is needed - table names are
#' parameters. A top-level `!x` is compiled as `everyone() & !x`.
#'
#' @param expr A phenotype expression (see [phenotype_criteria]).
#' @param cohort_definition_id Cohort definition id stamped on every row.
#' @param index_table Optional table with a person column and an index date; leaf lookback / lookahead
#'   windows are relative to its (earliest per person) date and the population is restricted to it.
#' @param index_person_col,index_date_col Column names in `index_table`.
#' @param concept_ancestor,concept Table references for the vocabulary tables.
#' @return A SQL string.
#' @export
compile_phenotype <- function(expr, cohort_definition_id = 1L, index_table = NULL, index_person_col = "subject_id",
                              index_date_col = "cohort_start_date", concept_ancestor = "concept_ancestor",
                              concept = "concept") {
  if (inherits(expr, "phenotype_not")) expr <- phenotype_intersect(everyone(), expr)
  leaves <- .pheno_collect_leaves(expr)
  position <- stats::setNames(seq_along(leaves) - 1L, vapply(leaves, function(l) as.character(l$.id), character(1)))
  pos_of <- function(node) position[[as.character(node$.id)]]
  has_index <- !is.null(index_table)
  ctes <- character(0)
  selects <- character(0)

  if (has_index) {
    ctes <- c(ctes, sprintf("idx AS (SELECT %s AS person_id, MIN(CAST(%s AS DATE)) AS index_date FROM %s GROUP BY 1)",
                            index_person_col, index_date_col, index_table))
  }

  for (leaf in leaves) {
    k <- pos_of(leaf)
    if (identical(leaf$type, "everyone")) {
      selects <- c(selects, if (has_index) {
        sprintf("SELECT person_id, index_date AS event_date, %d AS leaf FROM idx", k)
      } else {
        sprintf("SELECT person_id, MIN(observation_period_start_date) AS event_date, %d AS leaf FROM observation_period GROUP BY person_id", k)
      })
      next
    }
    if (as.integer(leaf$min_occurrences) < 1L) stop("min_occurrences must be >= 1.")
    for (nm in c("lookback_days", "lookahead_days")) {
      if (!is.null(leaf[[nm]]) && leaf[[nm]] < 0) stop(nm, " must be >= 0.")
    }
    if (.pheno_windowed(leaf) && !has_index) {
      stop("lookback_days / lookahead_days are relative to an index date: pass index_table = ... to compile_phenotype().")
    }
    join_idx <- if (.pheno_windowed(leaf)) " JOIN idx ON idx.person_id = t.person_id" else ""
    where <- character(0)
    extra_join <- ""
    cs_name <- sprintf("cs%d", k)

    if (identical(leaf$type, "condition")) {
      table <- "condition_occurrence"
      date_col <- "t.condition_start_date"
      ors <- character(0)
      cs <- .pheno_concept_set(cs_name, leaf$concept_ids, leaf$include_descendants, concept_ancestor)
      if (!is.null(cs)) {
        ctes <- c(ctes, cs)
        ors <- c(ors, sprintf("t.condition_concept_id IN (SELECT concept_id FROM %s)", cs_name))
      }
      if (length(leaf$icd_codes)) {
        extra_join <- sprintf(" LEFT JOIN %s sc ON sc.concept_id = t.condition_source_concept_id AND sc.vocabulary_id IN (%s)",
                              concept, paste(.pheno_q(leaf$icd_vocabularies), collapse = ", "))
        for (code in leaf$icd_codes) {
          ors <- c(ors, sprintf("t.condition_source_value LIKE %s", .pheno_q(paste0(code, "%"))),
                   sprintf("sc.concept_code LIKE %s", .pheno_q(paste0(code, "%"))))
        }
      }
      if (!length(ors)) stop("condition_criteria needs concept_ids and/or icd_codes.")
      where <- c(where, paste0("(", paste(ors, collapse = " OR "), ")"))
      if (!identical(leaf$diagnosis, "any")) {
        status <- if (identical(leaf$diagnosis, "primary")) 32902L else 32908L
        where <- c(where, sprintf("t.condition_status_concept_id IN (%d)", status))
      }
    } else if (identical(leaf$type, "measurement")) {
      table <- "measurement"
      date_col <- "t.measurement_date"
      ors <- character(0)
      cs <- .pheno_concept_set(cs_name, leaf$concept_ids, leaf$include_descendants, concept_ancestor)
      if (!is.null(cs)) {
        ctes <- c(ctes, cs)
        ors <- c(ors, sprintf("t.measurement_concept_id IN (SELECT concept_id FROM %s)", cs_name))
      }
      if (length(leaf$loinc_codes)) {
        codes <- paste(.pheno_q(leaf$loinc_codes), collapse = ", ")
        extra_join <- sprintf(" LEFT JOIN %s mc ON mc.concept_id = t.measurement_concept_id", concept)
        ors <- c(ors, sprintf("t.measurement_source_value IN (%s)", codes),
                 sprintf("(mc.vocabulary_id = 'LOINC' AND mc.concept_code IN (%s))", codes))
      }
      if (!length(ors)) stop("measurement_criteria needs concept_ids and/or loinc_codes.")
      where <- c(where, paste0("(", paste(ors, collapse = " OR "), ")"))
      for (op in list(c("value_gte", ">="), c("value_gt", ">"), c("value_lte", "<="), c("value_lt", "<"))) {
        v <- leaf[[op[[1]]]]
        if (!is.null(v)) where <- c(where, sprintf("t.value_as_number %s %s", op[[2]], format(as.numeric(v), scientific = FALSE, digits = 15)))
      }
      if (length(leaf$unit_concept_ids)) {
        where <- c(where, sprintf("t.unit_concept_id IN (%s)", paste(as.integer(leaf$unit_concept_ids), collapse = ", ")))
      }
    } else {
      table <- "drug_exposure"
      date_col <- "t.drug_exposure_start_date"
      code_seeds <- if (length(leaf$atc_codes)) list(vocab = "ATC%", codes = leaf$atc_codes, concept_table = concept) else NULL
      cs <- .pheno_concept_set(cs_name, leaf$concept_ids, leaf$include_descendants, concept_ancestor, code_seeds)
      if (is.null(cs)) stop("drug_criteria needs concept_ids and/or atc_codes.")
      ctes <- c(ctes, cs)
      where <- c(where, sprintf("t.drug_concept_id IN (SELECT concept_id FROM %s)", cs_name))
      if (!is.null(leaf$min_days_supply)) {
        where <- c(where, sprintf("COALESCE(t.days_supply, date_diff('day', t.drug_exposure_start_date, t.drug_exposure_end_date)) >= %d",
                                  as.integer(leaf$min_days_supply)))
      }
    }

    window <- .pheno_window_sql(leaf, date_col)
    if (nzchar(window)) where <- c(where, window)
    selects <- c(selects, sprintf("SELECT t.person_id AS person_id, CAST(%s AS DATE) AS event_date, %d AS leaf FROM %s t%s%s WHERE %s",
                                  date_col, k, table, extra_join, join_idx, paste(where, collapse = " AND ")))
  }

  ctes <- c(ctes, paste0("events AS (\n    ", paste(selects, collapse = "\n    UNION ALL\n    "), "\n)"))
  agg_cols <- vapply(leaves, function(leaf) {
    k <- pos_of(leaf)
    n <- if (is.null(leaf$min_occurrences)) 1L else as.integer(leaf$min_occurrences)
    if (n == 1L) {
      sprintf("MIN(event_date) FILTER (WHERE leaf = %d) AS d%d", k, k)
    } else {
      sprintf("(array_agg(event_date ORDER BY event_date) FILTER (WHERE leaf = %d))[%d] AS d%d", k, n, k)
    }
  }, character(1))
  ctes <- c(ctes, paste0("agg AS (SELECT person_id, ", paste(agg_cols, collapse = ", "), " FROM events GROUP BY person_id)"))

  truth <- function(node) {
    if (.pheno_is_leaf(node)) return(sprintf("a.d%d IS NOT NULL", pos_of(node)))
    if (inherits(node, "phenotype_union")) return(paste0("(", paste(vapply(node$children, truth, character(1)), collapse = " OR "), ")"))
    if (inherits(node, "phenotype_intersect")) return(paste0("(", paste(vapply(node$children, truth, character(1)), collapse = " AND "), ")"))
    paste0("(NOT ", truth(node$child), ")")
  }
  date_of <- function(node) {
    if (inherits(node, "phenotype_not") || identical(node$type, "everyone")) return(NULL)
    if (.pheno_is_leaf(node)) return(sprintf("a.d%d", pos_of(node)))
    parts <- character(0)
    for (ch in node$children) {
      d <- date_of(ch)
      if (is.null(d)) next
      # a composite branch must be satisfied for its date to count toward a union's earliest date
      if (inherits(node, "phenotype_union") && !.pheno_is_leaf(ch)) d <- sprintf("(CASE WHEN %s THEN %s END)", truth(ch), d)
      parts <- c(parts, d)
    }
    if (!length(parts)) return(NULL)
    if (length(parts) == 1) return(parts[[1]])
    paste0(if (inherits(node, "phenotype_union")) "LEAST(" else "GREATEST(", paste(parts, collapse = ", "), ")")
  }

  start <- date_of(expr)
  everyone_leaf <- Filter(function(l) identical(l$type, "everyone"), leaves)
  fallback <- if (length(everyone_leaf)) sprintf("a.d%d", pos_of(everyone_leaf[[1]])) else NULL
  start <- if (!is.null(start) && !is.null(fallback)) sprintf("COALESCE(%s, %s)", start, fallback) else (start %||% fallback)
  if (is.null(start)) stop("Phenotype has no criterion that yields an index date.")
  if (has_index) start <- "idx.index_date"
  join <- if (has_index) " JOIN idx ON idx.person_id = a.person_id" else ""
  paste0(
    "WITH ", paste(ctes, collapse = ",\n"), "\n",
    sprintf("SELECT %d AS cohort_definition_id, a.person_id AS subject_id, CAST(%s AS DATE) AS cohort_start_date, CAST(%s AS DATE) AS cohort_end_date\n",
            as.integer(cohort_definition_id), start, start),
    sprintf("FROM agg a%s\nWHERE %s AND %s IS NOT NULL", join, truth(expr), start)
  )
}

#' Compile and run a phenotype into a cohort table
#'
#' Writes rows to `cohort_table` (created if missing) and returns them. `replace = TRUE` first deletes any
#' existing rows with the same `cohort_definition_id`. Vocabulary tables are looked up on the connection
#' (main schema, then the attached central vocabulary) unless `concept_ancestor =` / `concept =` are passed.
#'
#' @param con DuckDB DBI connection holding the OMOP CDM.
#' @param expr A phenotype expression.
#' @param cohort_table Destination table.
#' @param cohort_definition_id Cohort definition id.
#' @param replace Replace existing rows of this definition.
#' @param ... Passed to [compile_phenotype()].
#' @return A data frame of the cohort rows for this definition.
#' @export
execute_phenotype <- function(con, expr, cohort_table = "cohort", cohort_definition_id = 1L, replace = TRUE, ...) {
  args <- list(...)
  leaves <- .pheno_collect_leaves(if (inherits(expr, "phenotype_not")) phenotype_intersect(everyone(), expr) else expr)
  if (is.null(args$concept_ancestor) && any(vapply(leaves, function(l) isTRUE(l$include_descendants) && length(l$concept_ids) > 0, logical(1)))) {
    args$concept_ancestor <- .med_resolve_table(con, "concept_ancestor")
  }
  if (is.null(args$concept) && any(vapply(leaves, function(l) length(l$atc_codes) + length(l$loinc_codes) + length(l$icd_codes) > 0, logical(1)))) {
    args$concept <- tryCatch(.med_resolve_table(con, "concept"), error = function(e) "concept")
  }
  sql <- do.call(compile_phenotype, c(list(expr, cohort_definition_id = cohort_definition_id), args))
  DBI::dbExecute(con, sprintf(paste0("CREATE TABLE IF NOT EXISTS %s (cohort_definition_id INTEGER, subject_id BIGINT, ",
                                     "cohort_start_date DATE, cohort_end_date DATE)"), cohort_table))
  if (isTRUE(replace)) {
    DBI::dbExecute(con, sprintf("DELETE FROM %s WHERE cohort_definition_id = %d", cohort_table, as.integer(cohort_definition_id)))
  }
  DBI::dbExecute(con, paste0("INSERT INTO ", cohort_table, " ", sql))
  DBI::dbGetQuery(con, sprintf("SELECT * FROM %s WHERE cohort_definition_id = %d ORDER BY subject_id",
                               cohort_table, as.integer(cohort_definition_id)))
}

#' Convert a phenotype expression to / from a plain list
#'
#' `phenotype_to_list()` returns a nested list (serializable with `jsonlite::toJSON()`);
#' `phenotype_from_list()` rebuilds the expression.
#'
#' @param x A phenotype expression (`phenotype_to_list`) or a nested list (`phenotype_from_list`).
#' @return A list, or a phenotype expression.
#' @name phenotype_serialize
#' @export
phenotype_to_list <- function(x) {
  if (.pheno_is_leaf(x)) {
    out <- unclass(x)
    out$.id <- NULL
    return(Filter(Negate(is.null), out))
  }
  if (inherits(x, "phenotype_not")) return(list(type = "not", criteria = phenotype_to_list(x$child)))
  list(type = x$type, criteria = lapply(x$children, phenotype_to_list))
}

#' @rdname phenotype_serialize
#' @export
phenotype_from_list <- function(x) {
  kind <- x$type
  body <- x[setdiff(names(x), "type")]
  if (kind %in% c("condition", "measurement", "drug")) {
    body <- lapply(body, function(v) if (is.list(v)) unlist(v) else v)
  }
  switch(kind,
    union = do.call(phenotype_union, lapply(body$criteria, phenotype_from_list)),
    intersect = do.call(phenotype_intersect, lapply(body$criteria, phenotype_from_list)),
    "not" = phenotype_not(phenotype_from_list(body$criteria)),
    everyone = everyone(),
    condition = do.call(condition_criteria, body),
    measurement = do.call(measurement_criteria, body),
    drug = do.call(drug_criteria, body),
    stop("Unknown phenotype node type '", kind, "'.")
  )
}
