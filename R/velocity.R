#' Days used when a patient has no prior visit of a type (the 365-day sentinel)
#'
#' `COALESCE(date_diff('day', last_visit_end, admission), 365)`. Mirror of `omop_etl.velocity.NO_PRIOR_VISIT_DAYS`.
#' @format An integer constant.
#' @export
NO_PRIOR_VISIT_DAYS <- 365L

#' Names of the columns returned by [extract_velocity_features()]
#' @format A character vector.
#' @export
VELOCITY_FEATURES <- c("ip_velocity", "ed_velocity", "recent_ip_ratio", "readmission_intensity", "days_since_last_ip",
                       "days_since_last_ed", "shock_index", "bun_to_creatinine", "lace_proxy")

#' Default measurement concepts used by [extract_velocity_features()]
#'
#' Standard OMOP concepts (LOINC): heart rate, systolic BP, BUN, serum creatinine.
#' @format A named list of integer vectors.
#' @export
VELOCITY_CONCEPTS <- list(heart_rate = 3027018L, systolic_bp = 3004249L, bun = 3013682L, creatinine = 3016723L)

#' Clinical-velocity feature set (utilization trajectory and bedside ratios)
#'
#' One row per distinct cohort (`subject_id`, `cohort_start_date`), using only visits strictly before the admission
#' date (nothing from the index stay leaks). Mirror of `omop_etl.velocity.extract_velocity_features` (identical SQL).
#'
#' * `ip_velocity`, `ed_velocity`: prior inpatient / emergency visits per 30 days over the look-back window
#' * `recent_ip_ratio`: inpatient visits in the last `recent_days` / inpatient visits in the window (0 if none)
#' * `readmission_intensity`: prior `readmit_gap_days` readmissions / prior inpatient visits (0 if none)
#' * `days_since_last_ip`, `days_since_last_ed`: days from the last prior discharge to admission;
#'   [NO_PRIOR_VISIT_DAYS] (365) when there is none
#' * `shock_index`: heart rate / systolic BP (first admission-day values; `NA` if either is missing or SBP <= 0)
#' * `bun_to_creatinine`: BUN / creatinine (first admission-day values; `NA` if either missing or creatinine <= 0)
#' * `lace_proxy`: L + A + E of the LACE index (length of stay, acuity, ED visits in 6 months), no Charlson term
#'
#' @param con DuckDB DBI connection with `visit_occurrence`, `measurement` and the cohort table.
#' @param cohort_table Table or view with `subject_id` and `cohort_start_date` (optionally `cohort_end_date`,
#'   `cohort_definition_id`).
#' @param lookback_days,recent_days,readmit_gap_days Window lengths in days (`lookback_days >= recent_days >= 1`).
#' @param ip_concepts,ed_concepts Visit concept ids counted as inpatient / emergency.
#' @param concepts Overrides for [VELOCITY_CONCEPTS] (names `heart_rate`, `systolic_bp`, `bun`, `creatinine`).
#' @param cohort_id Optional `cohort_definition_id` filter.
#' @return A `data.frame` with `subject_id`, `cohort_start_date` and the [VELOCITY_FEATURES].
#' @export
extract_velocity_features <- function(con, cohort_table = "cohort", lookback_days = 365, recent_days = 90,
                                      readmit_gap_days = 30, ip_concepts = c(9201L, 262L),
                                      ed_concepts = c(9203L, 262L), concepts = NULL, cohort_id = NULL) {
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)?$", cohort_table)) {
    stop("Invalid cohort_table '", cohort_table, "'.", call. = FALSE)
  }
  lb <- as.integer(lookback_days); rc <- as.integer(recent_days); gap <- as.integer(readmit_gap_days)
  if (lb < 1L || rc < 1L || rc > lb || gap < 1L) {
    stop("Need lookback_days >= recent_days >= 1 and readmit_gap_days >= 1.", call. = FALSE)
  }
  cc <- utils::modifyList(VELOCITY_CONCEPTS, as.list(concepts))
  ids <- function(x) paste(as.integer(x), collapse = ", ")
  cols <- tolower(DBI::dbGetQuery(con, paste0("DESCRIBE SELECT * FROM ", cohort_table))$column_name)
  if (!all(c("subject_id", "cohort_start_date") %in% cols)) {
    stop("cohort_table needs subject_id and cohort_start_date.", call. = FALSE)
  }
  where <- if (!is.null(cohort_id) && "cohort_definition_id" %in% cols) {
    sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_id))
  } else ""
  end_expr <- if ("cohort_end_date" %in% cols) "cohort_end_date" else "cohort_start_date"
  pick <- function(concept_ids) {
    sprintf(paste0("arg_min(CASE WHEN m.measurement_concept_id IN (%1$s) THEN m.value_as_number END, ",
                   "CASE WHEN m.measurement_concept_id IN (%1$s) AND m.value_as_number IS NOT NULL THEN m.measurement_id END)"),
            ids(concept_ids))
  }
  sql <- sprintf("
    WITH c AS (
        SELECT subject_id, cohort_start_date, max(%1$s) AS cohort_end_date
        FROM %2$s %3$s GROUP BY subject_id, cohort_start_date
    ),
    v AS (
        SELECT c.subject_id, c.cohort_start_date, v.visit_concept_id AS vc, v.visit_start_date AS vs,
               COALESCE(v.visit_end_date, v.visit_start_date) AS ve
        FROM c JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE v.visit_start_date < c.cohort_start_date
          AND v.visit_start_date >= c.cohort_start_date - INTERVAL (%4$d) DAY
    ),
    ipv AS (SELECT * FROM v WHERE vc IN (%5$s)),
    edv AS (SELECT * FROM v WHERE vc IN (%6$s)),
    ipa AS (
        SELECT subject_id, cohort_start_date, count(*) AS n_ip,
               count(*) FILTER (WHERE vs >= cohort_start_date - INTERVAL (%7$d) DAY) AS n_ip_recent,
               max(ve) AS last_ip_end
        FROM ipv GROUP BY 1, 2
    ),
    eda AS (
        SELECT subject_id, cohort_start_date, count(*) AS n_ed, max(ve) AS last_ed_end,
               count(*) FILTER (WHERE vs >= cohort_start_date - INTERVAL (183) DAY) AS n_ed_6m
        FROM edv GROUP BY 1, 2
    ),
    readm AS (
        SELECT i.subject_id, i.cohort_start_date, count(DISTINCT i.vs) AS n_readmit
        FROM ipv i
        JOIN visit_occurrence p ON p.person_id = i.subject_id AND p.visit_concept_id IN (%5$s)
         AND COALESCE(p.visit_end_date, p.visit_start_date) < i.vs
         AND i.vs <= COALESCE(p.visit_end_date, p.visit_start_date) + INTERVAL (%8$d) DAY
        GROUP BY 1, 2
    ),
    m AS (
        SELECT c.subject_id, c.cohort_start_date,
               %9$s AS hr, %10$s AS sbp, %11$s AS bun, %12$s AS creat
        FROM c JOIN measurement m ON m.person_id = c.subject_id AND m.measurement_date = c.cohort_start_date
        GROUP BY 1, 2
    ),
    idx AS (
        SELECT c.subject_id, c.cohort_start_date,
               GREATEST(date_diff('day', c.cohort_start_date, c.cohort_end_date), 0) AS los,
               EXISTS (SELECT 1 FROM visit_occurrence x WHERE x.person_id = c.subject_id
                       AND x.visit_start_date = c.cohort_start_date AND x.visit_concept_id IN (%6$s)) AS emergency
        FROM c
    )
    SELECT c.subject_id, c.cohort_start_date,
           COALESCE(ipa.n_ip, 0) * 30.0 / %4$d                                  AS ip_velocity,
           COALESCE(eda.n_ed, 0) * 30.0 / %4$d                                  AS ed_velocity,
           CASE WHEN COALESCE(ipa.n_ip, 0) = 0 THEN 0.0 ELSE ipa.n_ip_recent * 1.0 / ipa.n_ip END AS recent_ip_ratio,
           CASE WHEN COALESCE(ipa.n_ip, 0) = 0 THEN 0.0 ELSE COALESCE(readm.n_readmit, 0) * 1.0 / ipa.n_ip END
                                                                                AS readmission_intensity,
           COALESCE(date_diff('day', ipa.last_ip_end, c.cohort_start_date), %13$d) AS days_since_last_ip,
           COALESCE(date_diff('day', eda.last_ed_end, c.cohort_start_date), %13$d) AS days_since_last_ed,
           CASE WHEN m.sbp > 0 AND m.hr IS NOT NULL THEN m.hr / m.sbp END       AS shock_index,
           CASE WHEN m.creat > 0 AND m.bun IS NOT NULL THEN m.bun / m.creat END AS bun_to_creatinine,
           (CASE WHEN idx.los < 1 THEN 0 WHEN idx.los < 4 THEN idx.los WHEN idx.los < 7 THEN 4
                 WHEN idx.los < 14 THEN 5 ELSE 7 END)
           + (CASE WHEN idx.emergency THEN 3 ELSE 0 END)
           + LEAST(COALESCE(eda.n_ed_6m, 0), 4)                                 AS lace_proxy
    FROM c
    LEFT JOIN ipa  USING (subject_id, cohort_start_date)
    LEFT JOIN eda  USING (subject_id, cohort_start_date)
    LEFT JOIN readm USING (subject_id, cohort_start_date)
    LEFT JOIN m    USING (subject_id, cohort_start_date)
    LEFT JOIN idx  USING (subject_id, cohort_start_date)
    ORDER BY c.subject_id, c.cohort_start_date",
    end_expr, cohort_table, where, lb, ids(ip_concepts), ids(ed_concepts), rc, gap,
    pick(cc$heart_rate), pick(cc$systolic_bp), pick(cc$bun), pick(cc$creatinine), NO_PRIOR_VISIT_DAYS)
  DBI::dbGetQuery(con, sql)
}
