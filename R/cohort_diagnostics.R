#' OHDSI Cohort Diagnostics in Pure DuckDB
#'
#' Characterizes study cohorts across federated network sites directly in DuckDB
#' without requiring R/Java dependencies or DatabaseConnector.
#'
#' @docType class
#' @name cohort_diagnostics
NULL

#' Generate Pure DuckDB OHDSI Cohort Diagnostics
#'
#' @description
#' Executes vectorized DuckDB SQL queries against OMOP CDM tables to generate:
#' 1. Calendar-year incidence rates (new cases, person-years, incidence rate per 1,000 PY).
#' 2. Time-to-event and observation duration distributions (min, p25, median, p75, max, mean, sd).
#' 3. Age/sex population pyramids (10-year age bins stratified by gender).
#' 4. Baseline top condition concept prevalence.
#' Automatically applies complementary secondary cell suppression for HIPAA/GDPR compliance.
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param cohort_table Name of the cohort table (default `"cohort"`).
#' @param cohort_id Target cohort definition ID (default `1L`).
#' @param baseline_window_days Lookback window in days for concept prevalence (default `365L`).
#' @param top_n_concepts Number of top concepts to profile (default `10L`).
#' @param secondary_suppression Logical; whether to apply complementary cell suppression (default `TRUE`).
#' @param min_cell_count Small cell count threshold for redaction (default `11L`).
#'
#' @return An S3 object of class `consortium_cohort_diagnostics` containing `$incidence`,
#'   `$time_to_event`, `$population_pyramid`, `$concept_prevalence`, and `$metadata`.
#' @export
consortium_cohort_diagnostics <- function(con,
                                          cohort_table = "cohort",
                                          cohort_id = 1L,
                                          baseline_window_days = 365L,
                                          top_n_concepts = 10L,
                                          secondary_suppression = TRUE,
                                          min_cell_count = 11L) {
  if (!inherits(con, "duckdb_connection")) {
    stop("`con` must be an active DuckDB connection.", call. = FALSE)
  }

  tables_in_db <- tolower(DBI::dbListTables(con))
  if (!tolower(cohort_table) %in% tables_in_db) {
    stop(sprintf("Cohort table '%s' not found in DuckDB database.", cohort_table), call. = FALSE)
  }

  cols_info <- DBI::dbGetQuery(con, sprintf("PRAGMA table_info('%s')", cohort_table))
  c_names <- tolower(cols_info$name)

  subj_col <- if ("subject_id" %in% c_names) "subject_id" else if ("person_id" %in% c_names) "person_id" else "subject_id"
  start_col <- if ("cohort_start_date" %in% c_names) "cohort_start_date" else "cohort_start_date"
  end_col <- if ("cohort_end_date" %in% c_names) "cohort_end_date" else "cohort_end_date"

  cid_filter <- if ("cohort_definition_id" %in% c_names) {
    sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_id))
  } else ""

  n_sub_res <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(DISTINCT %s) AS n FROM %s %s", subj_col, cohort_table, cid_filter))
  n_subjects <- as.integer(n_sub_res$n[1])

  if (is.na(n_subjects) || n_subjects == 0) {
    stop(sprintf("No subjects found in %s for cohort_id = %d.", cohort_table, as.integer(cohort_id)), call. = FALSE)
  }

  # 1. Calendar-Year Incidence Rates
  inc_sql <- sprintf("
    SELECT
      EXTRACT(year FROM %s) AS calendar_year,
      COUNT(DISTINCT %s) AS new_cases,
      ROUND(SUM(DATEDIFF('day', %s, COALESCE(%s, %s + 1))) / 365.25, 2) AS person_years
    FROM %s
    %s
    GROUP BY EXTRACT(year FROM %s)
    ORDER BY calendar_year;
  ", start_col, subj_col, start_col, end_col, start_col, cohort_table, cid_filter, start_col)

  df_inc <- DBI::dbGetQuery(con, inc_sql)
  if (nrow(df_inc) > 0) {
    df_inc$person_years <- pmax(df_inc$person_years, 0.01)
    df_inc$incidence_rate_per_1k_py <- round(df_inc$new_cases / df_inc$person_years * 1000.0, 2)
  } else {
    df_inc <- data.frame(
      calendar_year = integer(0),
      new_cases = integer(0),
      person_years = numeric(0),
      incidence_rate_per_1k_py = numeric(0),
      stringsAsFactors = FALSE
    )
  }

  # 2. Time-to-Event Distributions
  dur_sql <- sprintf("
    WITH durations AS (
      SELECT DATEDIFF('day', %s, COALESCE(%s, %s)) AS dur
      FROM %s
      %s
    )
    SELECT
      COUNT(*) AS n,
      ROUND(MIN(dur), 1) AS min_days,
      ROUND(quantile_cont(dur, 0.25), 1) AS p25_days,
      ROUND(quantile_cont(dur, 0.50), 1) AS median_days,
      ROUND(quantile_cont(dur, 0.75), 1) AS p75_days,
      ROUND(MAX(dur), 1) AS max_days,
      ROUND(AVG(dur), 1) AS mean_days,
      ROUND(COALESCE(STDDEV(dur), 0.0), 1) AS sd_days
    FROM durations;
  ", start_col, end_col, start_col, cohort_table, cid_filter)

  df_dur <- DBI::dbGetQuery(con, dur_sql)

  # 3. Age / Sex Population Pyramid
  has_person <- "person" %in% tables_in_db
  if (has_person) {
    p_info <- DBI::dbGetQuery(con, "PRAGMA table_info('person')")
    p_cols <- tolower(p_info$name)
    p_id <- if ("person_id" %in% p_cols) "person_id" else "person_id"
    yob_col <- if ("year_of_birth" %in% p_cols) "year_of_birth" else "year_of_birth"
    gender_col <- if ("gender_concept_id" %in% p_cols) "gender_concept_id" else "gender_concept_id"

    c_extra <- if ("cohort_definition_id" %in% c_names) sprintf("AND c.cohort_definition_id = %d", as.integer(cohort_id)) else ""

    pyr_sql <- sprintf("
      WITH cohort_pts AS (
        SELECT
          c.%s AS person_id,
          EXTRACT(year FROM c.%s) - p.%s AS age,
          CASE
            WHEN p.%s = 8507 THEN 'Male'
            WHEN p.%s = 8532 THEN 'Female'
            ELSE 'Other/Unknown'
          END AS gender
        FROM %s c
        JOIN person p ON c.%s = p.%s
        WHERE 1=1 %s
      ),
      binned AS (
        SELECT
          gender,
          CASE
            WHEN age < 10 THEN '00-09'
            WHEN age < 20 THEN '10-19'
            WHEN age < 30 THEN '20-29'
            WHEN age < 40 THEN '30-39'
            WHEN age < 50 THEN '40-49'
            WHEN age < 60 THEN '50-59'
            WHEN age < 70 THEN '60-69'
            WHEN age < 80 THEN '70-79'
            ELSE '80+'
          END AS age_group
        FROM cohort_pts
      )
      SELECT
        age_group,
        gender,
        COUNT(*) AS count,
        ROUND(COUNT(*) * 100.0 / %d, 1) AS percent
      FROM binned
      GROUP BY age_group, gender
      ORDER BY age_group, gender;
    ", subj_col, start_col, yob_col, gender_col, gender_col, cohort_table, subj_col, p_id, c_extra, n_subjects)

    df_pyramid <- DBI::dbGetQuery(con, pyr_sql)
  } else {
    df_pyramid <- data.frame(
      age_group = character(0),
      gender = character(0),
      count = integer(0),
      percent = numeric(0),
      stringsAsFactors = FALSE
    )
  }

  # 4. Top Concept Prevalence (Condition Occurrence in Baseline Window)
  has_condition <- "condition_occurrence" %in% tables_in_db
  has_concept <- "concept" %in% tables_in_db

  if (has_condition) {
    c_extra_c <- if ("cohort_definition_id" %in% c_names) sprintf("AND c.cohort_definition_id = %d", as.integer(cohort_id)) else ""
    cpt_join <- if (has_concept) "LEFT JOIN concept cpt ON co.condition_concept_id = cpt.concept_id" else ""
    cpt_sel <- if (has_concept) "COALESCE(cpt.concept_name, 'Concept ' || CAST(co.condition_concept_id AS VARCHAR))" else "'Concept ' || CAST(co.condition_concept_id AS VARCHAR)"

    prev_sql <- sprintf("
      SELECT
        co.condition_concept_id AS concept_id,
        %s AS concept_name,
        'Condition' AS domain,
        COUNT(DISTINCT c.%s) AS count,
        ROUND(COUNT(DISTINCT c.%s) * 100.0 / %d, 1) AS percent
      FROM %s c
      JOIN condition_occurrence co ON c.%s = co.person_id
        AND co.condition_start_date >= c.%s - INTERVAL '%d' DAY
        AND co.condition_start_date <= c.%s
      %s
      WHERE 1=1 %s
      GROUP BY co.condition_concept_id, %s
      ORDER BY count DESC
      LIMIT %d;
    ", cpt_sel, subj_col, subj_col, n_subjects, cohort_table, subj_col, start_col, as.integer(baseline_window_days), start_col, cpt_join, c_extra_c, cpt_sel, as.integer(top_n_concepts))

    df_prev <- tryCatch({
      DBI::dbGetQuery(con, prev_sql)
    }, error = function(e) {
      data.frame(
        concept_id = integer(0),
        concept_name = character(0),
        domain = character(0),
        count = integer(0),
        percent = numeric(0),
        stringsAsFactors = FALSE
      )
    })
  } else {
    df_prev <- data.frame(
      concept_id = integer(0),
      concept_name = character(0),
      domain = character(0),
      count = integer(0),
      percent = numeric(0),
      stringsAsFactors = FALSE
    )
  }

  # 5. Apply secondary cell suppression
  if (secondary_suppression) {
    tables_list <- list(
      incidence = df_inc,
      population_pyramid = df_pyramid,
      concept_prevalence = df_prev
    )
    aud_res <- audit_cell_suppression(
      tables = tables_list,
      min_cell_count = min_cell_count,
      secondary_suppression = TRUE,
      return_audit = TRUE
    )
    df_inc <- aud_res$tables$incidence
    df_pyramid <- aud_res$tables$population_pyramid
    df_prev <- aud_res$tables$concept_prevalence
  }

  meta <- list(
    cohort_id = cohort_id,
    cohort_table = cohort_table,
    n_subjects = n_subjects,
    secondary_suppression = secondary_suppression,
    min_cell_count = min_cell_count,
    baseline_window_days = baseline_window_days
  )

  res <- list(
    incidence = df_inc,
    time_to_event = df_dur,
    population_pyramid = df_pyramid,
    concept_prevalence = df_prev,
    metadata = meta
  )
  class(res) <- c("consortium_cohort_diagnostics", "list")
  res
}

#' @method summary consortium_cohort_diagnostics
#' @export
summary.consortium_cohort_diagnostics <- function(object, ...) {
  data.frame(
    cohort_id = object$metadata$cohort_id,
    total_subjects = object$metadata$n_subjects,
    median_followup_days = if (!is.null(object$time_to_event$median_days) && length(object$time_to_event$median_days) > 0) object$time_to_event$median_days[1] else 0.0,
    calendar_years_observed = nrow(object$incidence),
    top_concepts_profiled = nrow(object$concept_prevalence),
    secondary_suppression_applied = object$metadata$secondary_suppression,
    stringsAsFactors = FALSE
  )
}

#' @method print consortium_cohort_diagnostics
#' @export
print.consortium_cohort_diagnostics <- function(x, ...) {
  cat(sprintf("=== OHDSI Cohort Diagnostics [Cohort ID: %d, N = %d] ===\n",
              x$metadata$cohort_id, x$metadata$n_subjects))
  print(summary(x), row.names = FALSE)
  invisible(x)
}

#' @method as.data.frame consortium_cohort_diagnostics
#' @export
as.data.frame.consortium_cohort_diagnostics <- function(x, ...) {
  x$incidence
}

#' @method plot consortium_cohort_diagnostics
#' @export
plot.consortium_cohort_diagnostics <- function(x, ...) {
  df <- x$population_pyramid
  if (is.null(df) || nrow(df) == 0) {
    if (!is.null(x$incidence) && nrow(x$incidence) > 0) {
      inc <- x$incidence
      graphics::plot(
        inc$calendar_year, inc$incidence_rate_per_1k_py,
        type = "b", pch = 19, col = "#08519c", lwd = 2,
        xlab = "Calendar Year", ylab = "Incidence Rate (per 1k person-years)",
        main = sprintf("Cohort %d: Annual Incidence Rate", x$metadata$cohort_id)
      )
    }
    return(invisible(x))
  }

  age_groups <- sort(unique(df$age_group))
  males <- df[df$gender == "Male", ]
  females <- df[df$gender == "Female", ]

  clean_num <- function(v) {
    if (length(v) == 0 || is.na(v[1])) return(0)
    str_val <- as.character(v[1])
    clean_str <- gsub("[^0-9.]", "", str_val)
    val <- suppressWarnings(as.numeric(clean_str))
    if (is.na(val)) 0 else val
  }

  male_counts <- sapply(age_groups, function(ag) {
    clean_num(males$count[males$age_group == ag])
  })
  female_counts <- sapply(age_groups, function(ag) {
    clean_num(females$count[females$age_group == ag])
  })

  max_count <- max(c(male_counts, female_counts, 10), na.rm = TRUE) * 1.15
  y_pos <- seq_along(age_groups)

  old_par <- graphics::par(mar = c(5, 6, 4, 2))
  on.exit(graphics::par(old_par))

  graphics::plot(
    0, 0, type = "n",
    xlim = c(-max_count, max_count),
    ylim = c(0.5, length(age_groups) + 0.5),
    xlab = "Subjects (Male < 0 | Female > 0)",
    ylab = "",
    yaxt = "n",
    xaxt = "n",
    main = sprintf("OMOP Cohort Diagnostics: Population Pyramid [Cohort %d]", x$metadata$cohort_id)
  )

  for (i in seq_along(age_groups)) {
    graphics::rect(-male_counts[i], y_pos[i] - 0.35, 0, y_pos[i] + 0.35, col = "#2b83ba", border = "white")
    graphics::rect(0, y_pos[i] - 0.35, female_counts[i], y_pos[i] + 0.35, col = "#d7191c", border = "white")
  }

  graphics::abline(v = 0, col = "gray40", lwd = 1.5)
  graphics::axis(2, at = y_pos, labels = age_groups, las = 2, cex.axis = 0.85)

  ticks <- pretty(c(-max_count, max_count), n = 5)
  graphics::axis(1, at = ticks, labels = abs(ticks))

  graphics::legend(
    "topright",
    legend = c("Male", "Female"),
    fill = c("#2b83ba", "#d7191c"),
    bty = "n"
  )

  invisible(x)
}
