#' Extensible Hierarchical Rollups, Concept Pattern Matching, and Crosswalk Materialization
#'
#' Provides declarative, multi-domain hierarchical rollups across medications, conditions,
#' procedures, and laboratories with DuckDB-native execution, bidirectional hierarchy expansion,
#' dose form exclusions, and concept drift auditing.
#'
#' @name rollups
NULL

#' Define a Concept Pattern Specification
#'
#' @param vocabulary_id Optional vocabulary identifier (e.g. 'ATC', 'RxNorm', 'ICD10CM', 'CPT4').
#' @param code_pattern Pattern for `concept_code` (supports SQL LIKE/ILIKE wildcards or regex).
#' @param name_pattern Pattern for `concept_name` (supports SQL LIKE/ILIKE wildcards or regex).
#' @param domain_id Optional domain filter (e.g. 'Drug', 'Condition', 'Measurement', 'Procedure').
#' @param regex Logical. If `TRUE`, evaluates pattern as a regular expression.
#' @param expand Direction of hierarchy expansion ('descendants' | 'ancestors' | NULL).
#' @param target_vocabulary Target vocabulary ID for expanded descendants (e.g. 'RxNorm').
#' @param include_retired Logical. If `TRUE`, queries backward 'Maps to' relationships for deprecated codes.
#' @param standard_only Logical. If `TRUE`, restricts to standard concepts ('S').
#' @return A list representing the concept pattern.
#' @export
concept_pattern <- function(vocabulary_id = NULL,
                            code_pattern = NULL,
                            name_pattern = NULL,
                            domain_id = NULL,
                            regex = FALSE,
                            expand = NULL,
                            target_vocabulary = NULL,
                            include_retired = FALSE,
                            standard_only = FALSE) {
  structure(
    list(
      vocabulary_id = vocabulary_id,
      code_pattern = code_pattern,
      name_pattern = name_pattern,
      domain_id = domain_id,
      regex = isTRUE(regex),
      expand = expand,
      target_vocabulary = target_vocabulary,
      include_retired = isTRUE(include_retired),
      standard_only = isTRUE(standard_only)
    ),
    class = "concept_pattern"
  )
}

.resolve_vocab_table_r <- function(con, name, vocab_db = NULL) {
  candidates <- character(0)
  if (!is.null(vocab_db) && nzchar(vocab_db)) {
    candidates <- c(candidates, sprintf("%s.%s", vocab_db, name), sprintf("%s.main.%s", vocab_db, name))
  }
  candidates <- c(candidates, name, sprintf("central_vocab.main.%s", name), sprintf("central_vocab.%s", name))
  for (ref in candidates) {
    res <- tryCatch(DBI::dbGetQuery(con, sprintf("SELECT 1 FROM %s LIMIT 1", ref)), error = function(e) NULL)
    if (!is.null(res)) return(ref)
  }
  stop(sprintf("Vocabulary table '%s' is not reachable on this connection.", name), call. = FALSE)
}

#' Resolve Concept Patterns with Wildcards and Hierarchy Closures
#'
#' Evaluates wildcard patterns (LIKE/ILIKE) and regular expressions on concept codes and names.
#'
#' @param con Active DuckDB DBI connection.
#' @param patterns A list of `concept_pattern` objects, lists, or strings (e.g. 'ATC:A10B%').
#' @param vocab_db Optional attached vocabulary schema/database name.
#' @param standard_only Logical. Restricts results to standard concepts.
#' @param expand Hierarchy expansion ('descendants' | 'ancestors' | NULL).
#' @param include_retired Logical. Expands backward 'Maps to' mappings for retired codes.
#' @return A data.frame of resolved concepts.
#' @export
resolve_concept_patterns <- function(con,
                                     patterns,
                                     vocab_db = NULL,
                                     standard_only = FALSE,
                                     expand = NULL,
                                     include_retired = FALSE) {
  if (is.null(patterns) || length(patterns) == 0) {
    return(data.frame(
      concept_id = numeric(0),
      concept_name = character(0),
      domain_id = character(0),
      vocabulary_id = character(0),
      concept_class_id = character(0),
      standard_concept = character(0),
      concept_code = character(0),
      valid_start_date = as.Date(character(0)),
      valid_end_date = as.Date(character(0)),
      invalid_reason = character(0),
      matched_pattern = character(0),
      stringsAsFactors = FALSE
    ))
  }

  concept_table <- .resolve_vocab_table_r(con, "concept", vocab_db)
  concept_ancestor <- tryCatch(.resolve_vocab_table_r(con, "concept_ancestor", vocab_db), error = function(e) NULL)
  concept_relationship <- tryCatch(.resolve_vocab_table_r(con, "concept_relationship", vocab_db), error = function(e) NULL)

  norm_patterns <- list()
  if (inherits(patterns, "concept_pattern") || is.character(patterns)) {
    patterns <- list(patterns)
  }

  for (p in patterns) {
    if (inherits(p, "concept_pattern")) {
      norm_patterns[[length(norm_patterns) + 1]] <- p
    } else if (is.list(p)) {
      norm_patterns[[length(norm_patterns) + 1]] <- do.call(concept_pattern, p)
    } else if (is.character(p)) {
      if (grepl(":", p)) {
        parts <- strsplit(p, ":", fixed = TRUE)[[1]]
        norm_patterns[[length(norm_patterns) + 1]] <- concept_pattern(vocabulary_id = trimws(parts[1]), code_pattern = trimws(parts[2]))
      } else {
        norm_patterns[[length(norm_patterns) + 1]] <- concept_pattern(code_pattern = trimws(p))
      }
    }
  }

  ctes <- character(0)
  unions <- character(0)

  for (idx in seq_along(norm_patterns)) {
    pat <- norm_patterns[[idx]]
    conds <- character(0)
    if (!is.null(pat$vocabulary_id) && nzchar(pat$vocabulary_id)) {
      conds <- c(conds, sprintf("c.vocabulary_id = '%s'", pat$vocabulary_id))
    }
    if (!is.null(pat$domain_id) && nzchar(pat$domain_id)) {
      conds <- c(conds, sprintf("c.domain_id = '%s'", pat$domain_id))
    }
    if (!is.null(pat$code_pattern) && nzchar(pat$code_pattern)) {
      if (isTRUE(pat$regex)) {
        conds <- c(conds, sprintf("regexp_matches(c.concept_code, '%s')", pat$code_pattern))
      } else if (grepl("[%_]", pat$code_pattern)) {
        conds <- c(conds, sprintf("c.concept_code ILIKE '%s'", pat$code_pattern))
      } else {
        conds <- c(conds, sprintf("c.concept_code = '%s'", pat$code_pattern))
      }
    }
    if (!is.null(pat$name_pattern) && nzchar(pat$name_pattern)) {
      if (isTRUE(pat$regex)) {
        conds <- c(conds, sprintf("regexp_matches(c.concept_name, '%s')", pat$name_pattern))
      } else if (grepl("[%_]", pat$name_pattern)) {
        conds <- c(conds, sprintf("c.concept_name ILIKE '%s'", pat$name_pattern))
      } else {
        conds <- c(conds, sprintf("c.concept_name = '%s'", pat$name_pattern))
      }
    }
    if (isTRUE(pat$standard_only) || isTRUE(standard_only)) {
      conds <- c(conds, "c.standard_concept = 'S'")
    }

    where_clause <- if (length(conds) > 0) paste(conds, collapse = " AND ") else "1=1"
    pat_label <- if (!is.null(pat$code_pattern)) pat$code_pattern else (if (!is.null(pat$name_pattern)) pat$name_pattern else sprintf("pattern_%d", idx))
    seed_cte <- sprintf("seed_%d", idx)

    ctes <- c(ctes, sprintf("
      %s AS (
        SELECT c.*, '%s' AS matched_pattern
        FROM %s c
        WHERE %s
      )
    ", seed_cte, pat_label, concept_table, where_clause))

    eff_expand <- if (!is.null(pat$expand)) pat$expand else expand
    eff_retired <- isTRUE(pat$include_retired) || isTRUE(include_retired)

    curr_cte <- seed_cte
    if (identical(eff_expand, "descendants") && !is.null(concept_ancestor)) {
      target_filter <- if (!is.null(pat$target_vocabulary) && nzchar(pat$target_vocabulary)) sprintf("AND c_desc.vocabulary_id = '%s'", pat$target_vocabulary) else ""
      exp_cte <- sprintf("exp_%d", idx)
      ctes <- c(ctes, sprintf("
        %s AS (
          SELECT 
            c_desc.concept_id, c_desc.concept_name, c_desc.domain_id, c_desc.vocabulary_id,
            c_desc.concept_class_id, c_desc.standard_concept, c_desc.concept_code,
            c_desc.valid_start_date, c_desc.valid_end_date, c_desc.invalid_reason,
            s.matched_pattern
          FROM %s s
          JOIN %s ca ON s.concept_id = ca.ancestor_concept_id
          JOIN %s c_desc ON ca.descendant_concept_id = c_desc.concept_id
          WHERE 1=1 %s
        )
      ", exp_cte, seed_cte, concept_ancestor, concept_table, target_filter))
      curr_cte <- exp_cte
    } else if (identical(eff_expand, "ancestors") && !is.null(concept_ancestor)) {
      target_filter <- if (!is.null(pat$target_vocabulary) && nzchar(pat$target_vocabulary)) sprintf("AND c_anc.vocabulary_id = '%s'", pat$target_vocabulary) else ""
      exp_cte <- sprintf("exp_%d", idx)
      ctes <- c(ctes, sprintf("
        %s AS (
          SELECT 
            c_anc.concept_id, c_anc.concept_name, c_anc.domain_id, c_anc.vocabulary_id,
            c_anc.concept_class_id, c_anc.standard_concept, c_anc.concept_code,
            c_anc.valid_start_date, c_anc.valid_end_date, c_anc.invalid_reason,
            s.matched_pattern
          FROM %s s
          JOIN %s ca ON s.concept_id = ca.descendant_concept_id
          JOIN %s c_anc ON ca.ancestor_concept_id = c_anc.concept_id
          WHERE 1=1 %s
        )
      ", exp_cte, seed_cte, concept_ancestor, concept_table, target_filter))
      curr_cte <- exp_cte
    }

    if (isTRUE(eff_retired) && !is.null(concept_relationship)) {
      ret_cte <- sprintf("ret_%d", idx)
      ctes <- c(ctes, sprintf("
        %s AS (
          SELECT * FROM %s
          UNION ALL
          SELECT 
            c_src.concept_id, c_src.concept_name, c_src.domain_id, c_src.vocabulary_id,
            c_src.concept_class_id, c_src.standard_concept, c_src.concept_code,
            c_src.valid_start_date, c_src.valid_end_date, c_src.invalid_reason,
            curr.matched_pattern
          FROM %s curr
          JOIN %s cr ON curr.concept_id = cr.concept_id_2 AND cr.relationship_id = 'Maps to'
          JOIN %s c_src ON cr.concept_id_1 = c_src.concept_id
          WHERE c_src.invalid_reason IS NOT NULL OR c_src.standard_concept IS NULL
        )
      ", ret_cte, curr_cte, curr_cte, concept_relationship, concept_table))
      curr_cte <- ret_cte
    }

    unions <- c(unions, sprintf("SELECT * FROM %s", curr_cte))
  }

  full_sql <- sprintf("
    WITH %s
    SELECT DISTINCT 
      concept_id, concept_name, domain_id, vocabulary_id,
      concept_class_id, standard_concept, concept_code,
      valid_start_date, valid_end_date, invalid_reason, matched_pattern
    FROM (
      %s
    )
    ORDER BY vocabulary_id, concept_code
  ", paste(ctes, collapse = ",\n"), paste(unions, collapse = " UNION ALL "))

  DBI::dbGetQuery(con, full_sql)
}

#' Define a Hierarchical Rollup Specification
#'
#' @param name Name of the clinical rollup (e.g. 'antidiabetic_agents').
#' @param domain Clinical domain ('Drug', 'Condition', 'Measurement', 'Procedure').
#' @param root_vocabulary Vocabulary ID of roots (e.g. 'ATC', 'SNOMED', 'LOINC').
#' @param roots Vector of root concept codes or IDs.
#' @param target_vocabulary Target vocabulary ID (default 'RxNorm' for Drug).
#' @param expansion_direction 'descendants' | 'ancestors' | NULL.
#' @param include_retired_maps_to Logical. Include backward 'Maps to' deprecated concepts.
#' @param exclude_dose_forms Character vector of dose forms to exclude (e.g. 'Topical', 'Ophthalmic').
#' @param combination_policy Combination product attribution policy ('attribute_all' | 'split' | 'disallow').
#' @param rules Optional list of `concept_pattern` objects.
#' @return A list representing the hierarchical rollup.
#' @export
hierarchical_rollup <- function(name,
                                domain = "Drug",
                                root_vocabulary = NULL,
                                roots = NULL,
                                target_vocabulary = NULL,
                                expansion_direction = "descendants",
                                include_retired_maps_to = TRUE,
                                exclude_dose_forms = NULL,
                                combination_policy = "attribute_all",
                                rules = NULL) {
  norm_rules <- list()
  if (!is.null(rules)) {
    norm_rules <- rules
  } else if (!is.null(roots)) {
    for (r in roots) {
      norm_rules[[length(norm_rules) + 1]] <- concept_pattern(
        vocabulary_id = root_vocabulary,
        code_pattern = as.character(r),
        domain_id = domain,
        expand = expansion_direction,
        target_vocabulary = target_vocabulary,
        include_retired = isTRUE(include_retired_maps_to)
      )
    }
  }

  spec <- list(
    name = name,
    domain = domain,
    root_vocabulary = root_vocabulary,
    roots = roots,
    target_vocabulary = target_vocabulary,
    expansion_direction = expansion_direction,
    include_retired_maps_to = isTRUE(include_retired_maps_to),
    exclude_dose_forms = exclude_dose_forms,
    combination_policy = combination_policy,
    rules = norm_rules
  )
  class(spec) <- "hierarchical_rollup"
  spec
}

.strip_class <- function(x) {
  if (is.list(x)) {
    x <- unclass(x)
    lapply(x, .strip_class)
  } else {
    unclass(x)
  }
}

#' Compute Cryptographic SHA-256 Hash of Rollup Specification
#'
#' @param rollup A hierarchical_rollup or rollup_engine object.
#' @return Character string of hex SHA-256 digest.
#' @export
spec_hash <- function(rollup) {
  clean_list <- .strip_class(rollup)
  json_str <- jsonlite::toJSON(clean_list, auto_unbox = TRUE)
  digest::digest(json_str, algo = "sha256", serialize = FALSE)
}

.filter_dose_forms_r <- function(con, df, exclude_dose_forms, vocab_db = NULL) {
  if (nrow(df) == 0 || is.null(exclude_dose_forms) || length(exclude_dose_forms) == 0) return(df)
  norm_forms <- tolower(trimws(exclude_dose_forms))

  # Name-based filter
  keep <- rep(TRUE, nrow(df))
  cnames <- tolower(df$concept_name)
  for (f in norm_forms) {
    keep <- keep & !grepl(f, cnames, fixed = TRUE)
  }
  df <- df[keep, , drop = FALSE]

  # Relationship-based filter if concept_relationship exists
  cr_table <- tryCatch(.resolve_vocab_table_r(con, "concept_relationship", vocab_db), error = function(e) NULL)
  c_table <- tryCatch(.resolve_vocab_table_r(con, "concept", vocab_db), error = function(e) NULL)

  if (!is.null(cr_table) && !is.null(c_table) && nrow(df) > 0) {
    cids <- unique(stats::na.omit(as.numeric(df$concept_id)))
    if (length(cids) > 0) {
      DBI::dbExecute(con, "CREATE TEMPORARY TABLE IF NOT EXISTS _tmp_cand_concepts_r (concept_id BIGINT)")
      DBI::dbExecute(con, "DELETE FROM _tmp_cand_concepts_r")
      DBI::dbAppendTable(con, "_tmp_cand_concepts_r", data.frame(concept_id = cids))
      form_clauses <- paste(sprintf("lower(c_form.concept_name) LIKE '%%%s%%'", norm_forms), collapse = " OR ")
      bad_cids <- DBI::dbGetQuery(con, sprintf("
        SELECT DISTINCT cand.concept_id
        FROM _tmp_cand_concepts_r cand
        JOIN %s cr ON cand.concept_id = cr.concept_id_1 AND cr.relationship_id = 'RxNorm has dose form'
        JOIN %s c_form ON cr.concept_id_2 = c_form.concept_id
        WHERE %s
      ", cr_table, c_table, form_clauses))$concept_id
      if (length(bad_cids) > 0) {
        df <- df[!df$concept_id %in% bad_cids, , drop = FALSE]
      }
    }
  }
  df
}

#' Define a Rollup Engine Specification
#'
#' @param name Name of the engine/rollup.
#' @param rules List of concept patterns or pattern specs.
#' @param exclude_forms Alias for exclude_dose_forms.
#' @param exclude_dose_forms Character vector of dose forms to exclude.
#' @param domain Domain ID (default 'Drug').
#' @param combination_policy Combination product policy.
#' @return A rollup_engine object.
#' @export
rollup_engine <- function(name = "custom_rollup_engine",
                          rules = NULL,
                          exclude_forms = NULL,
                          exclude_dose_forms = NULL,
                          domain = "Drug",
                          combination_policy = "attribute_all") {
  forms <- if (!is.null(exclude_forms)) exclude_forms else exclude_dose_forms
  obj <- list(
    name = name,
    domain = domain,
    rules = rules,
    exclude_dose_forms = forms,
    combination_policy = combination_policy
  )
  class(obj) <- c("rollup_engine", "hierarchical_rollup")
  obj
}

#' Materialize Crosswalk from Rollup Specification
#'
#' @param rollup A hierarchical_rollup or rollup_engine object.
#' @param con Active DuckDB DBI connection.
#' @param output_path Optional file path to write Parquet cache.
#' @param vocab_db Optional attached vocabulary schema/database name.
#' @return A data.frame containing resolved crosswalk concepts.
#' @export
materialize_crosswalk <- function(rollup, con, output_path = NULL, vocab_db = NULL) {
  df <- resolve_concept_patterns(
    con,
    patterns = rollup$rules,
    vocab_db = vocab_db,
    expand = rollup$expansion_direction,
    include_retired = isTRUE(rollup$include_retired_maps_to)
  )

  if (!is.null(rollup$exclude_dose_forms) && length(rollup$exclude_dose_forms) > 0) {
    df <- .filter_dose_forms_r(con, df, rollup$exclude_dose_forms, vocab_db = vocab_db)
  }

  df$rollup_name <- rollup$name
  df$combination_policy <- rollup$combination_policy
  h <- spec_hash(rollup)
  df$spec_hash <- h

  if (!is.null(output_path) && nzchar(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    duckdb::duckdb_register(con, "_tmp_xwalk_mat", df)
    on.exit(tryCatch(duckdb::duckdb_unregister(con, "_tmp_xwalk_mat"), error = function(e) NULL), add = TRUE)
    DBI::dbExecute(con, sprintf("COPY _tmp_xwalk_mat TO '%s' (FORMAT PARQUET)", gsub("\\\\", "/", output_path)))
  }

  df
}

#' Extract Cohort Exposures Matched to Rollup
#'
#' @param rollup A rollup_engine or hierarchical_rollup object.
#' @param con Active DuckDB connection.
#' @param site_db Optional site database schema.
#' @param cohort_table Cohort table name.
#' @param prescribed_types Vector of prescribed drug type concept IDs.
#' @param vocab_db Vocabulary database name.
#' @return A data.frame of matched exposure events.
#' @export
extract_rollup_exposures <- function(rollup,
                                    con,
                                    site_db = NULL,
                                    cohort_table = "cohort",
                                    prescribed_types = NULL,
                                    vocab_db = NULL) {
  mat <- materialize_crosswalk(rollup, con, vocab_db = vocab_db)
  if (nrow(mat) == 0) return(data.frame())

  de_prefix <- if (!is.null(site_db) && nzchar(site_db)) paste0(site_db, ".") else ""
  de_table <- paste0(de_prefix, "drug_exposure")

  types <- if (!is.null(prescribed_types)) as.numeric(prescribed_types) else c(32838, 38000177)
  type_str <- paste(types, collapse = ", ")

  cids <- unique(stats::na.omit(as.numeric(mat$concept_id)))
  DBI::dbExecute(con, "CREATE TEMPORARY TABLE IF NOT EXISTS _tmp_xwalk_ids_r (concept_id BIGINT)")
  DBI::dbExecute(con, "DELETE FROM _tmp_xwalk_ids_r")
  DBI::dbAppendTable(con, "_tmp_xwalk_ids_r", data.frame(concept_id = cids))

  query <- sprintf("
    SELECT 
      de.drug_exposure_id,
      de.person_id,
      de.drug_concept_id,
      de.drug_exposure_start_date,
      de.drug_exposure_end_date,
      de.drug_type_concept_id,
      '%s' AS rollup_name
    FROM %s de
    JOIN _tmp_xwalk_ids_r x ON de.drug_concept_id = x.concept_id
    WHERE de.drug_type_concept_id IN (%s)
  ", rollup$name, de_table, type_str)

  DBI::dbGetQuery(con, query)
}

#' Audit Concept Drift Across Vocabulary Releases
#'
#' @param con_old DuckDB connection with older vocabulary release.
#' @param con_new DuckDB connection with newer vocabulary release.
#' @param rollup Rollup specification object.
#' @param vocab_db_old Optional schema for old vocabulary.
#' @param vocab_db_new Optional schema for new vocabulary.
#' @return A list detailing added, removed, and status-changed concepts.
#' @export
audit_concept_drift <- function(con_old,
                                con_new,
                                rollup,
                                vocab_db_old = NULL,
                                vocab_db_new = NULL) {
  df_old <- materialize_crosswalk(rollup, con_old, vocab_db = vocab_db_old)
  df_new <- materialize_crosswalk(rollup, con_new, vocab_db = vocab_db_new)

  old_ids <- unique(as.numeric(df_old$concept_id))
  new_ids <- unique(as.numeric(df_new$concept_id))

  added <- sort(setdiff(new_ids, old_ids))
  removed <- sort(setdiff(old_ids, new_ids))
  shared <- intersect(new_ids, old_ids)

  status_changed <- list()
  if (length(shared) > 0) {
    rownames(df_old) <- as.character(df_old$concept_id)
    rownames(df_new) <- as.character(df_new$concept_id)
    for (cid in shared) {
      c_str <- as.character(cid)
      row_old <- df_old[c_str, ]
      row_new <- df_new[c_str, ]
      old_stat <- row_old$standard_concept[1]
      new_stat <- row_new$standard_concept[1]
      old_inv <- row_old$invalid_reason[1]
      new_inv <- row_new$invalid_reason[1]

      if (!identical(old_stat, new_stat) || !identical(old_inv, new_inv)) {
        status_changed[[length(status_changed) + 1]] <- list(
          concept_id = cid,
          concept_name = row_new$concept_name[1],
          old_standard = old_stat,
          new_standard = new_stat,
          old_invalid_reason = old_inv,
          new_invalid_reason = new_inv
        )
      }
    }
  }

  list(
    rollup_name = rollup$name,
    old_total_concepts = length(old_ids),
    new_total_concepts = length(new_ids),
    added_count = length(added),
    removed_count = length(removed),
    status_changed_count = length(status_changed),
    added_concept_ids = added,
    removed_concept_ids = removed,
    status_changed = status_changed
  )
}
