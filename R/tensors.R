#' Longitudinal sequence tensors for deep sequence models
#'
#' Builds a 3D integer array `(N x max_visits x max_tokens)` of concept tokens for a cohort, one slice per prior
#' visit (most recent first) within the look-back window, using DuckDB queries. Tokens are
#' `hash(concept_id) % (vocab_size - 1) + 1` for conditions, drugs and procedures (and, optionally, labs); `0` pads.
#' R port of `omop_etl.tensor.extract_longitudinal_tensors()`; token ids are identical to the Python ones for the
#' same DuckDB version. Rows follow `ORDER BY subject_id, cohort_start_date`. Use the result with torch/keras, e.g.
#' `torch::torch_tensor(x, dtype = torch::torch_long())`.
#'
#' @param con A DuckDB DBI connection to the CDM.
#' @param cohort_table Cohort table holding `subject_id` and `cohort_start_date`.
#' @param max_visits Maximum number of prior encounters per patient (default 20).
#' @param max_tokens Maximum number of concept tokens per encounter (default 30).
#' @param vocab_size Token dictionary size (default 7477).
#' @param lookback_days Look-back window in days before the index date (default 365).
#' @param include_index_stay If `TRUE`, also tokenize a stay that starts ON `cohort_start_date` (default window is
#'   `visit_start_date < cohort_start_date`). For cohorts anchored at admission this adds the index stay, whose later
#'   records may not be known at prediction time (leakage risk).
#' @param include_labs If `TRUE`, add measurement tokens `hash(measurement_concept_id * 3 + flag)` with `flag` 0 =
#'   within or no reference range, 1 = below `range_low`, 2 = above `range_high`. Token ids come from DuckDB `hash()`,
#'   so they are stable only within one DuckDB version: train and score with the same one.
#' @return An integer array of dimension `c(N, max_visits, max_tokens)`.
#' @export
extract_longitudinal_tensors <- function(con, cohort_table = "cohort", max_visits = MAX_LONGITUDINAL_VISITS,
                                         max_tokens = MAX_TOKENS_PER_VISIT, vocab_size = VOCAB_SIZE,
                                         lookback_days = 365L, include_index_stay = FALSE, include_labs = FALSE) {
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*([.][A-Za-z_][A-Za-z0-9_]*)?$", cohort_table)) {
    stop("Invalid cohort_table: ", cohort_table, call. = FALSE)
  }
  max_visits <- as.integer(max_visits)
  max_tokens <- as.integer(max_tokens)
  vocab_size <- as.integer(vocab_size)
  lookback_days <- as.integer(lookback_days)

  cohort <- DBI::dbGetQuery(con, sprintf(
    "SELECT subject_id, CAST(cohort_start_date AS VARCHAR) AS d FROM %s ORDER BY subject_id, cohort_start_date",
    cohort_table))
  n <- nrow(cohort)
  out <- array(0L, dim = c(n, max_visits, max_tokens))
  if (n == 0L) return(out)
  keys <- paste(cohort$subject_id, substr(cohort$d, 1, 10))

  visit_window <- if (isTRUE(include_index_stay)) "v.visit_start_date <= c.cohort_start_date" else
    "v.visit_start_date < c.cohort_start_date"
  lab_block <- if (isTRUE(include_labs)) sprintf("
      UNION ALL
      SELECT ev.subject_id, ev.cohort_start_date, ev.visit_rank,
             (hash(m.measurement_concept_id * 3 + CASE WHEN m.range_low IS NOT NULL AND m.value_as_number < m.range_low THEN 1
                                                       WHEN m.range_high IS NOT NULL AND m.value_as_number > m.range_high THEN 2
                                                       ELSE 0 END)
                  %% (%d - 1) + 1)::BIGINT AS token_id
      FROM eligible_visits ev
      JOIN measurement m ON m.visit_occurrence_id = ev.visit_occurrence_id
      WHERE m.measurement_concept_id IS NOT NULL AND m.measurement_concept_id > 0", vocab_size) else ""
  token <- function(alias, col) sprintf("(hash(%s.%s) %% (%d - 1) + 1)::BIGINT", alias, col, vocab_size)

  sql <- sprintf("
    WITH cohort_visits AS (
        SELECT c.subject_id, c.cohort_start_date, v.visit_occurrence_id, v.visit_start_date,
               DENSE_RANK() OVER (PARTITION BY c.subject_id, c.cohort_start_date
                                  ORDER BY v.visit_start_date DESC, v.visit_occurrence_id DESC) AS visit_rank
        FROM %s c
        JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE %s AND v.visit_start_date >= c.cohort_start_date - INTERVAL (%d) DAY
    ),
    eligible_visits AS (SELECT * FROM cohort_visits WHERE visit_rank <= %d),
    all_events AS (
        SELECT ev.subject_id, ev.cohort_start_date, ev.visit_rank, %s AS token_id
        FROM eligible_visits ev JOIN condition_occurrence co ON co.visit_occurrence_id = ev.visit_occurrence_id
        WHERE co.condition_concept_id IS NOT NULL AND co.condition_concept_id > 0
        UNION ALL
        SELECT ev.subject_id, ev.cohort_start_date, ev.visit_rank, %s AS token_id
        FROM eligible_visits ev JOIN drug_exposure de ON de.visit_occurrence_id = ev.visit_occurrence_id
        WHERE de.drug_concept_id IS NOT NULL AND de.drug_concept_id > 0
        UNION ALL
        SELECT ev.subject_id, ev.cohort_start_date, ev.visit_rank, %s AS token_id
        FROM eligible_visits ev JOIN procedure_occurrence po ON po.visit_occurrence_id = ev.visit_occurrence_id
        WHERE po.procedure_concept_id IS NOT NULL AND po.procedure_concept_id > 0
        %s
    ),
    ranked_tokens AS (
        SELECT subject_id, cohort_start_date, visit_rank, token_id,
               ROW_NUMBER() OVER (PARTITION BY subject_id, cohort_start_date, visit_rank ORDER BY token_id) AS token_rank
        FROM all_events
    )
    SELECT subject_id, CAST(cohort_start_date AS VARCHAR) AS d, visit_rank, token_id, token_rank
    FROM ranked_tokens WHERE token_rank <= %d
    ORDER BY subject_id, cohort_start_date, visit_rank, token_rank",
    cohort_table, visit_window, lookback_days, max_visits,
    token("co", "condition_concept_id"), token("de", "drug_concept_id"), token("po", "procedure_concept_id"),
    lab_block, max_tokens)

  # Partial CDMs (no procedure_occurrence / measurement) yield an all-zero tensor, as in the Python implementation.
  ev <- tryCatch(DBI::dbGetQuery(con, sql), error = function(e) data.frame())
  if (nrow(ev) > 0L) {
    row <- match(paste(ev$subject_id, substr(ev$d, 1, 10)), keys)
    v <- as.integer(ev$visit_rank)
    t <- as.integer(ev$token_rank)
    ok <- !is.na(row) & v >= 1L & v <= max_visits & t >= 1L & t <= max_tokens
    out[cbind(row[ok], v[ok], t[ok])] <- as.integer(ev$token_id[ok])
  }
  out
}

#' @rdname extract_longitudinal_tensors
#' @format Integer constants: `MAX_LONGITUDINAL_VISITS` (20), `MAX_TOKENS_PER_VISIT` (30), `VOCAB_SIZE` (7477).
#' @export
MAX_LONGITUDINAL_VISITS <- 20L

#' @rdname extract_longitudinal_tensors
#' @export
MAX_TOKENS_PER_VISIT <- 30L

#' @rdname extract_longitudinal_tensors
#' @export
VOCAB_SIZE <- 7477L
