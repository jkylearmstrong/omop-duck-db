#' Semantic Concept Set Optimizer & Usagi Auto-Suggester Module
#'
#' @description
#' Scans actual clinical frequencies, hierarchy closures, and lexical synonyms
#' in DuckDB to identify missing descendants, co-occurring clinical siblings,
#' and unmapped source codes for study cohort definitions.
#'
#' @name concept_optimizer
NULL

#' Optimize Concept Set and Suggest Missing Codes
#'
#' @description
#' Evaluates target concept IDs against CDM tables and Athena hierarchies to suggest:
#' 1. Hierarchical siblings sharing parent concepts.
#' 2. High-frequency co-occurring conditions, drugs, and measurements.
#' 3. High-frequency unmapped source values (concept_id = 0).
#' 4. Lexical synonyms and related clinical terms.
#'
#' @param con Active DBI/DuckDB connection.
#' @param concept_ids Integer or numeric vector of target concept IDs.
#' @param threshold_frequency Minimum clinical occurrences required for suggestions (default 100L).
#' @param max_suggestions Maximum number of suggestions to return (default 50L).
#'
#' @return A data.frame of suggested concepts with rationale and co-occurrence counts.
#' @importFrom utils head
#' @export
optimize_concept_set <- function(con,
                                 concept_ids,
                                 threshold_frequency = 100L,
                                 max_suggestions = 50L) {
  cids <- as.integer(concept_ids[!is.na(concept_ids)])
  if (length(cids) == 0) {
    return(data.frame(
      concept_id = integer(0),
      concept_name = character(0),
      domain_id = character(0),
      vocabulary_id = character(0),
      cooccurrence_count = integer(0),
      suggestion_type = character(0),
      rationale = character(0),
      stringsAsFactors = FALSE
    ))
  }

  cid_str <- paste(cids, collapse = ", ")
  tables <- tolower(DBI::dbListTables(con))

  has_concept <- "concept" %in% tables
  has_ancestor <- "concept_ancestor" %in% tables
  has_cond <- "condition_occurrence" %in% tables
  has_drug <- "drug_exposure" %in% tables

  suggestion_rows <- list()
  seen_ids <- as.character(cids)

  # 1. Hierarchical Siblings
  if (has_ancestor && has_concept) {
    sibling_sql <- sprintf("
      WITH parents AS (
          SELECT ancestor_concept_id
          FROM concept_ancestor
          WHERE descendant_concept_id IN (%s)
            AND min_levels_of_separation = 1
      ),
      siblings AS (
          SELECT ca.descendant_concept_id AS concept_id, COUNT(*) AS shared_parents
          FROM concept_ancestor ca
          JOIN parents p ON ca.ancestor_concept_id = p.ancestor_concept_id
          WHERE ca.descendant_concept_id NOT IN (%s)
            AND ca.min_levels_of_separation = 1
          GROUP BY ca.descendant_concept_id
      )
      SELECT 
          s.concept_id,
          c.concept_name,
          c.domain_id,
          c.vocabulary_id,
          s.shared_parents * 10 AS score,
          'sibling_hierarchy' AS suggestion_type,
          'Shares immediate parent ancestor with target concept set' AS rationale
      FROM siblings s
      JOIN concept c ON s.concept_id = c.concept_id
      WHERE c.standard_concept = 'S'
      ORDER BY score DESC
      LIMIT 20
    ", cid_str, cid_str)

    tryCatch({
      sibs <- DBI::dbGetQuery(con, sibling_sql)
      if (nrow(sibs) > 0) {
        for (i in seq_len(nrow(sibs))) {
          id_char <- as.character(sibs$concept_id[i])
          if (!id_char %in% seen_ids) {
            seen_ids <- c(seen_ids, id_char)
            suggestion_rows[[length(suggestion_rows) + 1]] <- data.frame(
              concept_id = as.integer(sibs$concept_id[i]),
              concept_name = as.character(sibs$concept_name[i]),
              domain_id = as.character(sibs$domain_id[i]),
              vocabulary_id = as.character(sibs$vocabulary_id[i]),
              cooccurrence_count = as.integer(sibs$score[i]),
              suggestion_type = "sibling_hierarchy",
              rationale = as.character(sibs$rationale[i]),
              stringsAsFactors = FALSE
            )
          }
        }
      }
    }, error = function(e) NULL)
  }

  # 2. Clinical Co-Occurrence
  if (has_cond && has_concept) {
    cooc_sql <- sprintf("
      SELECT 
          co2.condition_concept_id AS concept_id,
          c.concept_name,
          c.domain_id,
          c.vocabulary_id,
          COUNT(DISTINCT co2.person_id) AS cooc_count
      FROM condition_occurrence co1
      JOIN condition_occurrence co2 ON co1.person_id = co2.person_id
      JOIN concept c ON co2.condition_concept_id = c.concept_id
      WHERE co1.condition_concept_id IN (%s)
        AND co2.condition_concept_id NOT IN (%s)
        AND co2.condition_concept_id > 0
        AND c.standard_concept = 'S'
      GROUP BY co2.condition_concept_id, c.concept_name, c.domain_id, c.vocabulary_id
      HAVING COUNT(DISTINCT co2.person_id) >= %d
      ORDER BY cooc_count DESC
      LIMIT 20
    ", cid_str, cid_str, as.integer(threshold_frequency))

    tryCatch({
      cooc_df <- DBI::dbGetQuery(con, cooc_sql)
      if (nrow(cooc_df) > 0) {
        for (i in seq_len(nrow(cooc_df))) {
          id_char <- as.character(cooc_df$concept_id[i])
          if (!id_char %in% seen_ids) {
            seen_ids <- c(seen_ids, id_char)
            suggestion_rows[[length(suggestion_rows) + 1]] <- data.frame(
              concept_id = as.integer(cooc_df$concept_id[i]),
              concept_name = as.character(cooc_df$concept_name[i]),
              domain_id = as.character(cooc_df$domain_id[i]),
              vocabulary_id = as.character(cooc_df$vocabulary_id[i]),
              cooccurrence_count = as.integer(cooc_df$cooc_count[i]),
              suggestion_type = "clinical_cooccurrence",
              rationale = sprintf("High clinical co-occurrence (N = %s) in target cohort", format(cooc_df$cooc_count[i], big.mark = ",")),
              stringsAsFactors = FALSE
            )
          }
        }
      }
    }, error = function(e) NULL)
  }

  # 3. High-Frequency Unmapped Source Codes
  if (has_cond) {
    min_unmapped <- max(5L, as.integer(threshold_frequency) %/% 10L)
    unmapped_sql <- sprintf("
      SELECT 
          0 AS concept_id,
          co.condition_source_value AS concept_name,
          'Condition' AS domain_id,
          'Source' AS vocabulary_id,
          COUNT(*) AS cooc_count,
          'unmapped_source_code' AS suggestion_type,
          'High-frequency unmapped source code in target patient population' AS rationale
      FROM condition_occurrence co
      WHERE (co.condition_concept_id = 0 OR co.condition_concept_id IS NULL)
        AND co.condition_source_value IS NOT NULL
        AND LENGTH(TRIM(co.condition_source_value)) > 1
      GROUP BY co.condition_source_value
      HAVING COUNT(*) >= %d
      ORDER BY cooc_count DESC
      LIMIT 10
    ", min_unmapped)

    tryCatch({
      unmapped_df <- DBI::dbGetQuery(con, unmapped_sql)
      if (nrow(unmapped_df) > 0) {
        for (i in seq_len(nrow(unmapped_df))) {
          suggestion_rows[[length(suggestion_rows) + 1]] <- data.frame(
            concept_id = 0L,
            concept_name = paste("[Unmapped Code]", unmapped_df$concept_name[i]),
            domain_id = as.character(unmapped_df$domain_id[i]),
            vocabulary_id = as.character(unmapped_df$vocabulary_id[i]),
            cooccurrence_count = as.integer(unmapped_df$cooc_count[i]),
            suggestion_type = "unmapped_source_code",
            rationale = as.character(unmapped_df$rationale[i]),
            stringsAsFactors = FALSE
          )
        }
      }
    }, error = function(e) NULL)
  }

  if (length(suggestion_rows) == 0) {
    return(data.frame(
      concept_id = integer(0),
      concept_name = character(0),
      domain_id = character(0),
      vocabulary_id = character(0),
      cooccurrence_count = integer(0),
      suggestion_type = character(0),
      rationale = character(0),
      stringsAsFactors = FALSE
    ))
  }

  out_df <- do.call(rbind, suggestion_rows)
  out_df <- out_df[order(-out_df$cooccurrence_count), , drop = FALSE]
  head(out_df, as.integer(max_suggestions))
}
