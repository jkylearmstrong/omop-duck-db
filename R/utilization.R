#' Default visit-concept groups for `extract_prior_utilization()`
#'
#' 9201 inpatient, 9203 emergency room, 9202 outpatient, 262 emergency room + inpatient (counts as both IP and ED).
#'
#' @format A named list of integer vectors (`ip`, `ed`, `os`).
#' @export
DEFAULT_VISIT_CONCEPT_GROUPS <- list(
  ip = c(9201L, 262L),
  ed = c(9203L, 262L),
  os = 9202L
)

#' Prior healthcare utilization per cohort row, strictly before the index admission
#'
#' Prior utilization is the strongest single family of readmission predictors. For every cohort row this counts the
#' visits that precede the index admission: inpatient (IP), emergency (ED) and outpatient (OS) counts, cumulative
#' length of stay, days since the last encounter and the number of prior 30-day readmissions. The look-back is
#' strictly before the admission date, so nothing from the index stay (or anything later) can leak into the
#' features. Mirror of `omop_etl.utilization.extract_prior_utilization` in Python.
#'
#' Output columns, one row per distinct (`subject_id`, `cohort_start_date`) ordered by both:
#' * `prior_<group>_count`: visits of that group starting in `[admission - lookback_days, admission)`
#' * `prior_<group>_los_days`: summed `visit_end_date - visit_start_date` of those visits (missing end = same day)
#' * `days_since_last_encounter`: days from the end of the most recent visit of ANY type that started within the
#'   look-back to the admission (`NA` when there is none)
#' * `prior_readmit_<gap>d_count`: prior `readmission_group` visits in the look-back that began within
#'   `readmission_gap_days` days after the discharge of an earlier visit of that group (any earlier date)
#'
#' @param con DuckDB DBI connection with `visit_occurrence` and the cohort table.
#' @param cohort_table Table or view with `subject_id` and `cohort_start_date`. If it also has
#'   `visit_occurrence_id` that visit is excluded explicitly.
#' @param cohort_id Optional `cohort_definition_id` filter.
#' @param lookback_days Window length in days (non-negative integer).
#' @param anchor_date Only `"admission"` is supported: a window anchored at discharge would count the index stay.
#' @param visit_concept_groups Named list `group -> visit_concept_ids`. Defaults to
#'   [DEFAULT_VISIT_CONCEPT_GROUPS]. A visit concept may belong to several groups.
#' @param readmission_gap_days Gap for the prior-readmission count.
#' @param readmission_group Group used for the prior-readmission count (must be a name of the groups).
#' @param format `"df"` or `"arrow"`.
#' @return A `data.frame` (or Arrow Table).
#' @export
extract_prior_utilization <- function(con,
                                      cohort_table = "cohort",
                                      cohort_id = NULL,
                                      lookback_days = 365,
                                      anchor_date = "admission",
                                      visit_concept_groups = NULL,
                                      readmission_gap_days = 30,
                                      readmission_group = "ip",
                                      format = "df") {
  if (!identical(anchor_date, "admission")) {
    stop("anchor_date must be 'admission': a look-back anchored at discharge would include the index stay and ",
         "leak post-admission information.", call. = FALSE)
  }
  whole <- function(x, what) {
    if (!is.numeric(x) || length(x) != 1L || is.na(x) || x < 0 || x != floor(x)) {
      stop(what, " must be a non-negative integer; got ", format(x), call. = FALSE)
    }
    as.integer(x)
  }
  lookback_days <- whole(lookback_days, "lookback_days")
  gap <- whole(readmission_gap_days, "readmission_gap_days")
  groups <- if (is.null(visit_concept_groups)) DEFAULT_VISIT_CONCEPT_GROUPS else visit_concept_groups
  if (length(groups) == 0) stop("visit_concept_groups must not be empty.", call. = FALSE)
  for (nm in names(groups)) {
    if (!grepl("^[A-Za-z][A-Za-z0-9_]*$", nm)) {
      stop("visit group name '", nm, "' must be a plain identifier (letters, digits, underscore).", call. = FALSE)
    }
    if (length(groups[[nm]]) == 0) stop("visit group '", nm, "' has no visit_concept_ids.", call. = FALSE)
  }
  if (!readmission_group %in% names(groups)) {
    stop("readmission_group '", readmission_group, "' is not one of ", paste(names(groups), collapse = ", "), ".",
         call. = FALSE)
  }
  fmt <- tolower(trimws(format))
  if (!fmt %in% c("df", "arrow")) stop("format must be 'df' or 'arrow'.", call. = FALSE)

  cols <- tolower(DBI::dbGetQuery(con, paste0("DESCRIBE SELECT * FROM ", cohort_table))$column_name)
  for (needed in c("subject_id", "cohort_start_date")) {
    if (!needed %in% cols) stop("cohort_table '", cohort_table, "' has no '", needed, "' column.", call. = FALSE)
  }
  where <- ""
  if (!is.null(cohort_id)) {
    if (!"cohort_definition_id" %in% cols) {
      stop("cohort_id given but '", cohort_table, "' has no cohort_definition_id column.", call. = FALSE)
    }
    where <- sprintf("WHERE c.cohort_definition_id = %d", as.integer(cohort_id))
  }
  index_ids <- if ("visit_occurrence_id" %in% cols) "c.visit_occurrence_id" else "CAST(NULL AS BIGINT)"

  in_list <- function(ids) paste(as.integer(ids), collapse = ", ")
  ra <- in_list(groups[[readmission_group]])
  aggs <- character()
  for (nm in names(groups)) {
    il <- in_list(groups[[nm]])
    aggs <- c(aggs,
              sprintf("COUNT(*) FILTER (WHERE pv.visit_concept_id IN (%s)) AS prior_%s_count", il, nm),
              sprintf("COALESCE(SUM(pv.los) FILTER (WHERE pv.visit_concept_id IN (%s)), 0) AS prior_%s_los_days", il, nm))
  }
  aggs <- c(aggs, sprintf("COUNT(*) FILTER (WHERE pv.visit_concept_id IN (%s) AND pv.is_readmit) AS prior_readmit_%dd_count",
                          ra, gap))
  select_groups <- paste(vapply(names(groups), function(n) {
    sprintf("CAST(COALESCE(a.prior_%s_count, 0) AS INTEGER) AS prior_%s_count, CAST(COALESCE(a.prior_%s_los_days, 0) AS INTEGER) AS prior_%s_los_days",
            n, n, n, n)
  }, character(1)), collapse = ", ")

  sql <- sprintf("
    WITH base AS (
        SELECT DISTINCT c.subject_id AS person_key, CAST(c.cohort_start_date AS DATE) AS adm, %s AS idx_vid
        FROM %s AS c %s
    ),
    keys AS (SELECT DISTINCT person_key, adm FROM base),
    visits AS (
        SELECT v.person_id, v.visit_occurrence_id, v.visit_concept_id,
               CAST(v.visit_start_date AS DATE) AS s,
               CAST(COALESCE(v.visit_end_date, v.visit_start_date) AS DATE) AS e
        FROM visit_occurrence AS v
        WHERE v.person_id IN (SELECT person_key FROM keys) AND v.visit_start_date IS NOT NULL
    ),
    flagged AS (
        SELECT vi.*, vi.e - vi.s AS los,
               EXISTS (SELECT 1 FROM visits AS u
                       WHERE u.person_id = vi.person_id AND u.visit_occurrence_id <> vi.visit_occurrence_id
                         AND u.visit_concept_id IN (%s) AND vi.visit_concept_id IN (%s)
                         AND u.e <= vi.s AND vi.s - u.e <= %d) AS is_readmit
        FROM visits AS vi
    ),
    prior AS (
        SELECT k.person_key, k.adm, f.*
        FROM keys AS k
        JOIN flagged AS f ON f.person_id = k.person_key AND f.s < k.adm AND f.s >= k.adm - %d
        WHERE NOT EXISTS (SELECT 1 FROM base AS b WHERE b.person_key = k.person_key AND b.adm = k.adm
                          AND b.idx_vid = f.visit_occurrence_id)
    ),
    agg AS (
        SELECT person_key, adm, %s,
               MIN(adm - e) AS days_since_last_encounter
        FROM (SELECT person_key, adm, visit_concept_id, los, is_readmit, e FROM prior) AS pv
        GROUP BY person_key, adm
    )
    SELECT k.person_key AS subject_id, k.adm AS cohort_start_date,
           %s,
           CAST(COALESCE(a.prior_readmit_%dd_count, 0) AS INTEGER) AS prior_readmit_%dd_count,
           a.days_since_last_encounter
    FROM keys AS k LEFT JOIN agg AS a ON a.person_key = k.person_key AND a.adm = k.adm
    ORDER BY k.person_key, k.adm
  ", index_ids, cohort_table, where, ra, ra, gap, lookback_days, paste(aggs, collapse = ", "),
                 select_groups, gap, gap)
  res <- DBI::dbGetQuery(con, sql)
  if (fmt == "arrow" && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(res))
  }
  res
}
