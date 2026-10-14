#' Create a Zero-Copy Federated Consortium Connection
#'
#' Attaches multiple site DuckDB databases in read-only mode and generates unified
#' `v_*` views (e.g. `v_person`, `v_visit_occurrence`) with `site_id` and `site_anon`
#' provenance columns. Optionally attaches a central Athena vocabulary.
#'
#' @param site_dbs Named character vector or list mapping site labels/pseudonyms
#'   (e.g. `c("Site A" = "site1.duckdb", "Site B" = "site2.duckdb")`) to DuckDB database paths.
#' @param central_vocab_db Optional path to an Athena vocabulary DuckDB database.
#' @param output_con Optional DuckDB connection. If `NULL`, creates a new in-memory connection.
#' @return DuckDB connection with attached site databases and unified `v_*` views.
#' @export
create_federated_consortium <- function(site_dbs, central_vocab_db = NULL, output_con = NULL) {
  if (length(site_dbs) == 0) {
    stop("site_dbs cannot be empty.")
  }

  con <- if (!is.null(output_con)) output_con else DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")

  federated_tables <- c(
    "person", "visit_occurrence", "condition_occurrence", "procedure_occurrence",
    "drug_exposure", "measurement", "observation", "death", "observation_period",
    "drug_era", "condition_era", "cohort", "cohort_definition", "provider", "care_site", "cdm_source"
  )

  site_names <- names(site_dbs)
  if (is.null(site_names)) {
    site_names <- as.character(seq_along(site_dbs))
  }

  attached_sites <- list()

  for (i in seq_along(site_dbs)) {
    db_path <- as.character(site_dbs[[i]])
    if (!file.exists(db_path)) {
      stop("Site database not found: ", db_path)
    }

    schema_alias <- sprintf("site_%d", i)
    DBI::dbExecute(con, sprintf("ATTACH %s AS %s (READ_ONLY);", sql_path_literal(db_path), schema_alias))

    site_anon <- site_names[i]
    if (is.na(site_anon) || site_anon == "") {
      site_anon <- sprintf("Site %d", i)
    }

    # Resolve site_id
    site_id <- suppressWarnings(as.integer(site_anon))
    if (is.na(site_id)) {
      # Try care_site or person
      care_site_res <- tryCatch(
        DBI::dbGetQuery(con, sprintf("SELECT MIN(care_site_id) AS id FROM %s.care_site WHERE care_site_id IS NOT NULL", schema_alias)),
        error = function(e) NULL
      )
      if (!is.null(care_site_res) && nrow(care_site_res) > 0 && !is.na(care_site_res$id[1])) {
        site_id <- as.integer(care_site_res$id[1])
      } else {
        person_res <- tryCatch(
          DBI::dbGetQuery(con, sprintf("SELECT MIN(care_site_id) AS id FROM %s.person WHERE care_site_id IS NOT NULL", schema_alias)),
          error = function(e) NULL
        )
        if (!is.null(person_res) && nrow(person_res) > 0 && !is.na(person_res$id[1])) {
          site_id <- as.integer(person_res$id[1])
        } else {
          site_id <- i
        }
      }
    }

    attached_sites[[length(attached_sites) + 1]] <- list(
      schema = schema_alias,
      site_id = site_id,
      site_anon = site_anon
    )
  }

  # Attach central vocabulary if provided
  if (!is.null(central_vocab_db)) {
    attach_central_vocabulary(con, central_vocab_db, temporary = FALSE)
  }

  # Build federated views for each CDM table
  for (tbl in federated_tables) {
    first_schema <- NULL
    for (s in attached_sites) {
      has_tbl <- DBI::dbGetQuery(
        con,
        sprintf("SELECT 1 FROM information_schema.tables WHERE table_catalog = '%s' AND table_name = '%s'", s$schema, tbl)
      )
      if (nrow(has_tbl) > 0) {
        if (is.null(first_schema)) {
          first_schema <- s$schema
        }
      }
    }

    if (is.null(first_schema)) {
      next
    }

    cols_df <- DBI::dbGetQuery(
      con,
      sprintf("SELECT column_name FROM information_schema.columns WHERE table_catalog = '%s' AND table_name = '%s' ORDER BY ordinal_position", first_schema, tbl)
    )
    cols_sql <- paste(sprintf('t."%s"', cols_df$column_name), collapse = ", ")

    site_selects <- c()
    for (s in attached_sites) {
      has_tbl <- DBI::dbGetQuery(
        con,
        sprintf("SELECT 1 FROM information_schema.tables WHERE table_catalog = '%s' AND table_name = '%s'", s$schema, tbl)
      )
      if (nrow(has_tbl) > 0) {
        s_anon_esc <- gsub("'", "''", s$site_anon)
        site_selects <- c(
          site_selects,
          sprintf("SELECT %d AS site_id, '%s' AS site_anon, %s FROM %s.%s t", s$site_id, s_anon_esc, cols_sql, s$schema, tbl)
        )
      }
    }

    if (length(site_selects) > 0) {
      union_sql <- paste(site_selects, collapse = " UNION ALL\n")
      view_sql <- sprintf("CREATE OR REPLACE VIEW v_%s AS\n%s;", tbl, union_sql)
      DBI::dbExecute(con, view_sql)

      # Create convenience alias view for base CDM table name if it does not already exist
      has_base <- DBI::dbGetQuery(
        con,
        sprintf("SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() AND table_schema = 'main' AND table_name = '%s' AND table_type = 'BASE TABLE'", tbl)
      )
      if (nrow(has_base) == 0) {
        DBI::dbExecute(con, sprintf("CREATE OR REPLACE VIEW %s AS SELECT * FROM v_%s;", tbl, tbl))
      }
    }
  }

  invisible(con)
}

#' Create Privacy-Preserving View with Small-Cell Masking
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param view_name Source view or table name.
#' @param output_view Target view name (default: `"safe_" || view_name`).
#' @param min_cell_size Minimum count threshold to display (default 10).
#' @param count_columns Optional character vector of count columns to redact.
#' @param fill_value Replacement value for suppressed counts (default `"<10"`).
#' @return Name of created privacy view.
#' @export
with_cell_suppression <- function(con,
                                  view_name,
                                  output_view = NULL,
                                  min_cell_size = 10L,
                                  count_columns = NULL,
                                  fill_value = "<10") {
  if (!inherits(con, "duckdb_connection")) stop("`con` must be a DuckDB connection.", call. = FALSE)
  target <- if (!is.null(output_view)) output_view else paste0("safe_", view_name)
  cols_df <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0;", view_name))
  col_names <- cols_df$column_name

  target_counts <- if (!is.null(count_columns)) {
    count_columns
  } else {
    col_names[grepl("(?i)count|n_patients|n_subjects|_n|subjects", col_names)]
  }

  select_exprs <- vapply(col_names, function(col) {
    if (col %in% target_counts) {
      sprintf("CASE WHEN CAST(%s AS DOUBLE) < %d THEN '%s' ELSE CAST(%s AS VARCHAR) END AS %s",
              col, as.integer(min_cell_size), fill_value, col, col)
    } else {
      col
    }
  }, character(1))

  sql <- sprintf("CREATE OR REPLACE VIEW %s AS SELECT %s FROM %s;",
                 target, paste(select_exprs, collapse = ", "), view_name)
  DBI::dbExecute(con, sql)
  target
}

#' Create Privacy-Preserving View with Differential Privacy Laplace Noise
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param view_name Source view or table name.
#' @param output_view Target view name (default: `"dp_" || view_name`).
#' @param epsilon Differential privacy budget parameter (default 1.0).
#' @param delta Privacy failure bound (default 1e-5).
#' @param count_columns Optional character vector of count columns to privatize.
#' @return Name of created privacy view.
#' @export
with_differential_privacy <- function(con,
                                      view_name,
                                      output_view = NULL,
                                      epsilon = 1.0,
                                      delta = 1e-5,
                                      count_columns = NULL) {
  if (!inherits(con, "duckdb_connection")) stop("`con` must be a DuckDB connection.", call. = FALSE)
  if (epsilon <= 0) stop("`epsilon` must be strictly positive.", call. = FALSE)

  target <- if (!is.null(output_view)) output_view else paste0("dp_", view_name)
  cols_df <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0;", view_name))
  col_names <- cols_df$column_name

  target_counts <- if (!is.null(count_columns)) {
    count_columns
  } else {
    col_names[grepl("(?i)count|n_patients|n_subjects|_n|subjects", col_names)]
  }

  scale <- 1.0 / as.numeric(epsilon)
  select_exprs <- vapply(col_names, function(col) {
    if (col %in% target_counts) {
      laplace_sql <- sprintf(
        "(CASE WHEN random() <= 0.5 THEN %f * ln(GREATEST(1e-12, 2.0 * random())) ELSE -%f * ln(GREATEST(1e-12, 2.0 * (1.0 - random()))) END)",
        scale, scale
      )
      sprintf("GREATEST(0, ROUND(CAST(%s AS DOUBLE) + %s)) AS %s", col, laplace_sql, col)
    } else {
      col
    }
  }, character(1))

  sql <- sprintf("CREATE OR REPLACE VIEW %s AS SELECT %s FROM %s;",
                 target, paste(select_exprs, collapse = ", "), view_name)
  DBI::dbExecute(con, sql)
  target
}

#' Check Cross-Database Concept Frequency Discrepancy
#'
#' @param con Active DuckDB connection with `v_*` federated views.
#' @param table_name CDM table name (default `"condition_occurrence"`).
#' @param concept_col Concept ID column (default `"condition_concept_id"`).
#' @param top_n Number of top concepts to evaluate (default 25).
#' @return A list containing `prevalence`, `sites`, and `evaluated_concepts`.
#' @export
check_cross_database_discrepancy <- function(con,
                                             table_name = "condition_occurrence",
                                             concept_col = "condition_concept_id",
                                             top_n = 25L) {
  if (!inherits(con, "duckdb_connection")) stop("`con` must be a DuckDB connection.", call. = FALSE)
  view <- paste0("v_", table_name)

  top_df <- DBI::dbGetQuery(con, sprintf("
    SELECT %s AS concept_id, COUNT(*) AS total_count
    FROM %s
    WHERE %s IS NOT NULL AND %s != 0
    GROUP BY 1
    ORDER BY 2 DESC
    LIMIT %d
  ", concept_col, view, concept_col, concept_col, as.integer(top_n)))

  if (nrow(top_df) == 0) {
    return(list(prevalence = data.frame(), sites = character(0), evaluated_concepts = 0L))
  }

  cids_str <- paste(top_df$concept_id, collapse = ", ")
  cross_df <- DBI::dbGetQuery(con, sprintf("
    WITH site_totals AS (
        SELECT site_anon, COUNT(*) AS site_total_rows
        FROM %s
        GROUP BY 1
    ),
    site_concepts AS (
        SELECT site_anon, %s AS concept_id, COUNT(*) AS concept_count
        FROM %s
        WHERE %s IN (%s)
        GROUP BY 1, 2
    )
    SELECT
        sc.concept_id,
        sc.site_anon,
        sc.concept_count,
        st.site_total_rows,
        ROUND(sc.concept_count * 100.0 / st.site_total_rows, 3) AS prevalence_pct
    FROM site_concepts sc
    JOIN site_totals st ON sc.site_anon = st.site_anon
    ORDER BY sc.concept_id, sc.site_anon;
  ", view, concept_col, view, concept_col, cids_str))

  sites <- if (nrow(cross_df) > 0) sort(unique(cross_df$site_anon)) else character(0)
  list(
    prevalence = cross_df,
    sites = sites,
    evaluated_concepts = nrow(top_df)
  )
}

#' Multi-Site Federated Meta-Analysis Aggregator
#'
#' Computes pooled effect estimates across federated DuckDB nodes using the
#' DerSimonian-Laird random-effects or inverse-variance fixed-effect method,
#' without sharing patient-level data. Computes Cochran's Q, tau^2, and Higgins' I^2.
#'
#' @param estimates Vector of study-level parameter estimates (e.g. log OR, log HR, beta).
#' @param std_errors Vector of study standard errors.
#' @param variances Optional vector of study variances (used if `std_errors` is NULL).
#' @param sites Optional character vector of site identifiers.
#' @param site_summaries Optional data.frame with columns `site`, `estimate`, `std_error`.
#' @param con Optional active federated DuckDB connection.
#' @param query Optional SQL query to evaluate across site tables or `v_*` views.
#' @param model Model type: `"random"` (DerSimonian-Laird, default) or `"fixed"`.
#' @param effect_type Effect metric: `"beta"`, `"log_or"`, `"log_hr"`, or `"mean_diff"`.
#' @param alpha Significance level for confidence intervals (default `0.05`).
#' @return An object of class `federated_meta_analysis` containing pooled estimates and statistics.
#' @export
federated_meta_analysis <- function(estimates = NULL,
                                    std_errors = NULL,
                                    variances = NULL,
                                    sites = NULL,
                                    site_summaries = NULL,
                                    con = NULL,
                                    query = NULL,
                                    model = "random",
                                    effect_type = "beta",
                                    alpha = 0.05) {
  site_vec <- character(0)
  est_vec <- numeric(0)
  se_vec <- numeric(0)

  if (!is.null(site_summaries) && is.data.frame(site_summaries)) {
    cols <- tolower(names(site_summaries))
    s_col <- if ("site" %in% cols) "site" else if ("site_anon" %in% cols) "site_anon" else names(site_summaries)[1]
    e_col <- if ("estimate" %in% cols) "estimate" else if ("coef" %in% cols) "coef" else names(site_summaries)[2]
    se_col <- if ("std_error" %in% cols) "std_error" else if ("se" %in% cols) "se" else names(site_summaries)[3]
    site_vec <- as.character(site_summaries[[s_col]])
    est_vec <- as.numeric(site_summaries[[e_col]])
    se_vec <- as.numeric(site_summaries[[se_col]])
  } else if (!is.null(con) && !is.null(query)) {
    q_df <- DBI::dbGetQuery(con, query)
    cols <- tolower(names(q_df))
    s_col <- if ("site" %in% cols) "site" else if ("site_anon" %in% cols) "site_anon" else names(q_df)[1]
    e_col <- if ("estimate" %in% cols) "estimate" else if ("coef" %in% cols) "coef" else names(q_df)[2]
    se_col <- if ("std_error" %in% cols) "std_error" else if ("se" %in% cols) "se" else names(q_df)[3]
    site_vec <- as.character(q_df[[s_col]])
    est_vec <- as.numeric(q_df[[e_col]])
    se_vec <- as.numeric(q_df[[se_col]])
  } else if (!is.null(estimates)) {
    est_vec <- as.numeric(estimates)
    if (!is.null(std_errors)) {
      se_vec <- as.numeric(std_errors)
    } else if (!is.null(variances)) {
      se_vec <- sqrt(as.numeric(variances))
    } else {
      stop("Must provide either std_errors or variances alongside estimates.", call. = FALSE)
    }
    site_vec <- if (!is.null(sites)) as.character(sites) else sprintf("Site %d", seq_along(est_vec))
  } else {
    stop("Must provide estimates, site_summaries, or con with query.", call. = FALSE)
  }

  k <- length(est_vec)
  if (k == 0) stop("No study estimates provided for meta-analysis.", call. = FALSE)

  vars <- se_vec^2
  fe_weights <- 1.0 / pmax(1e-12, vars)
  sum_fe_w <- sum(fe_weights)
  theta_fe <- sum(fe_weights * est_vec) / sum_fe_w
  se_fe <- 1.0 / sqrt(sum_fe_w)

  q_stat <- sum(fe_weights * ((est_vec - theta_fe)^2))
  df <- max(1L, k - 1L)
  q_p_val <- if (k > 1L) stats::pchisq(q_stat, df = df, lower.tail = FALSE) else 1.0

  sum_fe_w_sq <- sum(fe_weights^2)
  c_factor <- if (sum_fe_w > 0) sum_fe_w - (sum_fe_w_sq / sum_fe_w) else 1.0
  tau_sq <- if (k > 1L && c_factor > 0) max(0.0, (q_stat - df) / c_factor) else 0.0
  tau <- sqrt(tau_sq)

  i_sq <- if (k > 1L && q_stat > 0) max(0.0, (q_stat - df) / q_stat) * 100.0 else 0.0

  re_weights <- 1.0 / pmax(1e-12, vars + tau_sq)
  sum_re_w <- sum(re_weights)
  theta_re <- sum(re_weights * est_vec) / sum_re_w
  se_re <- 1.0 / sqrt(sum_re_w)

  use_random <- identical(tolower(trimws(model)), "random")
  pooled_est <- if (use_random) theta_re else theta_fe
  pooled_se <- if (use_random) se_re else se_fe
  final_weights <- if (use_random) re_weights else fe_weights
  sum_final_w <- if (use_random) sum_re_w else sum_fe_w

  z_crit <- stats::qnorm(1.0 - alpha / 2.0)
  ci_lower <- pooled_est - z_crit * pooled_se
  ci_upper <- pooled_est + z_crit * pooled_se
  z_score <- if (pooled_se > 0) pooled_est / pooled_se else 0.0
  p_val <- 2.0 * stats::pnorm(abs(z_score), lower.tail = FALSE)

  study_rows <- data.frame(
    site = site_vec,
    estimate = est_vec,
    std_error = se_vec,
    ci_lower = est_vec - z_crit * se_vec,
    ci_upper = est_vec + z_crit * se_vec,
    weight = final_weights,
    weight_pct = final_weights / sum_final_w * 100.0,
    stringsAsFactors = FALSE
  )

  res <- list(
    model = if (use_random) "random" else "fixed",
    effect_type = effect_type,
    k_studies = k,
    pooled_estimate = pooled_est,
    se = pooled_se,
    ci_lower = ci_lower,
    ci_upper = ci_upper,
    z_score = z_score,
    p_value = p_val,
    q_statistic = q_stat,
    df = df,
    q_p_value = q_p_val,
    tau_squared = tau_sq,
    tau = tau,
    i_squared = i_sq,
    studies = study_rows
  )
  class(res) <- c("federated_meta_analysis", "list")
  res
}

#' @export
print.federated_meta_analysis <- function(x, ...) {
  cat(sprintf("=== Federated Meta-Analysis (%s Effects) ===\n", toupper(x$model)))
  cat(sprintf("Pooled Estimate: %.4f (SE = %.4f, 95%% CI: [%.4f, %.4f])\n",
              x$pooled_estimate, x$se, x$ci_lower, x$ci_upper))
  cat(sprintf("Test of Effect: Z = %.2f, p = %.4e\n", x$z_score, x$p_value))
  cat(sprintf("Heterogeneity: Q = %.2f (df = %d, p = %.4f); I2 = %.1f%%; tau2 = %.4f\n",
              x$q_statistic, x$df, x$q_p_value, x$i_squared, x$tau_squared))
  cat(sprintf("Number of participating sites: %d\n", x$k_studies))
  invisible(x)
}

#' @export
as.data.frame.federated_meta_analysis <- function(x, ...) {
  s <- x$studies
  s$type <- "Study"
  pooled_row <- data.frame(
    site = sprintf("Pooled (%s)", tools::toTitleCase(x$model)),
    estimate = x$pooled_estimate,
    std_error = x$se,
    ci_lower = x$ci_lower,
    ci_upper = x$ci_upper,
    weight = sum(s$weight),
    weight_pct = 100.0,
    type = "Pooled",
    stringsAsFactors = FALSE
  )
  rbind(s, pooled_row)
}

#' @export
to_mermaid.federated_meta_analysis <- function(x, ...) {
  lines <- c(
    "flowchart TD",
    "    subgraph MetaAnalysis [Federated Meta-Analysis Summary]",
    sprintf('        Header["I^2 = %.1f%%, tau^2 = %.4f, Q = %.2f"]', x$i_squared, x$tau_squared, x$q_statistic)
  )
  for (i in seq_len(nrow(x$studies))) {
    s <- x$studies[i, ]
    lines <- c(lines, sprintf('        S%d["%s: %+.2f [%+.2f, %+.2f] (%.1f%%)"]',
                              i, s$site, s$estimate, s$ci_lower, s$ci_upper, s$weight_pct))
    lines <- c(lines, sprintf("        Header --> S%d", i))
  }
  lines <- c(lines, sprintf('        Pooled["* POOLED: %+.2f [%+.2f, %+.2f] (p = %.4f)"]',
                            x$pooled_estimate, x$ci_lower, x$ci_upper, x$p_value))
  for (i in seq_len(nrow(x$studies))) {
    lines <- c(lines, sprintf("        S%d --> Pooled", i))
  }
  lines <- c(lines, "    end")
  paste(lines, collapse = "\n")
}

#' Multi-Site Federated Consortium Connector
#'
#' Orchestrates multi-site observational analytics across independent site databases
#' (e.g. in eDERRI consortium studies) attached to a central Athena vocabulary.
#' Provides unified querying and one-call generation of federated Table 1 (demographics & clinical profiles)
#' and Table 2 (missingness audits).
#'
#' @param sites Named character vector or list of site database paths (e.g. `c("Site A" = "site_a.duckdb")`).
#' @param central_vocabulary Path to central Athena vocabulary DuckDB database.
#' @param auto_resources Logical. If `TRUE` (default), configure memory and thread limits via [configure_resources()].
#' @return A `federated_consortium` S3 object.
#' @export
federated_consortium <- function(sites, central_vocabulary = NULL, auto_resources = TRUE) {
  if (length(sites) == 0) {
    stop("sites cannot be empty.")
  }
  con <- create_federated_consortium(sites, central_vocab_db = central_vocabulary)
  if (isTRUE(auto_resources)) {
    tryCatch(configure_resources(con), error = function(e) NULL)
  }
  structure(
    list(
      sites = sites,
      central_vocabulary = central_vocabulary,
      con = con
    ),
    class = "federated_consortium"
  )
}

#' Execute a Query Across the Federated Consortium Views
#'
#' @param fc A `federated_consortium` object.
#' @param sql SQL query string.
#' @return A `data.frame` of query results.
#' @export
consortium_query <- function(fc, sql) {
  if (!inherits(fc, "federated_consortium")) stop("`fc` must be a federated_consortium object.", call. = FALSE)
  DBI::dbGetQuery(fc$con, sql)
}

#' Audit Record Counts Across Participating Consortium Sites
#'
#' @param fc A `federated_consortium` object.
#' @return A `data.frame` summarizing patient, visit, condition, measurement, and drug counts per site.
#' @export
consortium_site_summaries <- function(fc) {
  if (!inherits(fc, "federated_consortium")) stop("`fc` must be a federated_consortium object.", call. = FALSE)
  site_names <- names(fc$sites)
  if (is.null(site_names)) site_names <- as.character(seq_along(fc$sites))

  rows <- list()
  for (i in seq_along(fc$sites)) {
    s_name <- site_names[i]
    if (is.na(s_name) || s_name == "") s_name <- sprintf("Site %d", i)
    s_esc <- gsub("'", "''", s_name, fixed = TRUE)

    sql <- sprintf("
      SELECT
        '%s' AS site,
        (SELECT COUNT(DISTINCT person_id) FROM v_person WHERE site_anon = '%s') AS n_persons,
        (SELECT COUNT(*) FROM v_visit_occurrence WHERE site_anon = '%s') AS n_visits,
        (SELECT COUNT(*) FROM v_condition_occurrence WHERE site_anon = '%s') AS n_conditions,
        (SELECT COUNT(*) FROM v_measurement WHERE site_anon = '%s') AS n_measurements,
        (SELECT COUNT(*) FROM v_drug_exposure WHERE site_anon = '%s') AS n_drugs
    ", s_esc, s_esc, s_esc, s_esc, s_esc, s_esc)

    res <- DBI::dbGetQuery(fc$con, sql)
    rows[[length(rows) + 1]] <- res
  }
  do.call(rbind, rows)
}

#' Generate Federated Table 1 Baseline Demographics and Clinical Profiles
#'
#' @param fc A `federated_consortium` object.
#' @param cohort_table Name of the cohort table or view (default `"v_visit_occurrence"`).
#' @param domain Optional domain profile to add (e.g. `"heart_failure"`, `"diabetes"`).
#' @param stratify_by Optional stratification column.
#' @param ... Additional arguments passed to `table1_generator()$generate()`.
#' @return A `data.frame` representing Table 1.
#' @export
consortium_generate_table1 <- function(fc, cohort_table = "v_visit_occurrence", domain = NULL, stratify_by = NULL, ...) {
  if (!inherits(fc, "federated_consortium")) stop("`fc` must be a federated_consortium object.", call. = FALSE)
  t1 <- table1_generator(con = fc$con)
  if (!is.null(domain)) {
    t1$add_domain_profile(domain)
  }
  t1$generate(cohort_table = cohort_table, stratify_by = stratify_by, ...)
}

#' Generate Federated Table 2 Completeness and Missingness Audit
#'
#' @param fc A `federated_consortium` object.
#' @param cohort_table Name of the cohort table or view (default `"v_visit_occurrence"`).
#' @param strata_col Optional stratification column.
#' @param columns Optional subset of columns to evaluate.
#' @param ... Additional arguments passed to [generate_table2()].
#' @return A `data.frame` representing Table 2 completeness audit.
#' @export
consortium_generate_table2 <- function(fc, cohort_table = "v_visit_occurrence", strata_col = NULL, columns = NULL, ...) {
  if (!inherits(fc, "federated_consortium")) stop("`fc` must be a federated_consortium object.", call. = FALSE)
  generate_table2(fc$con, table_name = cohort_table, strata_col = strata_col, columns = columns, ...)
}

#' Close Federated Consortium DuckDB Connection
#'
#' @param fc A `federated_consortium` object.
#' @export
consortium_close <- function(fc) {
  if (inherits(fc, "federated_consortium") && !is.null(fc$con)) {
    try(DBI::dbDisconnect(fc$con, shutdown = TRUE), silent = TRUE)
  }
}

#' @export
close.federated_consortium <- function(con, ...) {
  consortium_close(con)
}

#' @export
print.federated_consortium <- function(x, ...) {
  cat(sprintf("Federated Consortium Connector [%d participating site(s)]\n", length(x$sites)))
  invisible(x)
}

#' Generate Consortium-Wide and Site-Stratified Bedside Risk Scores
#'
#' Evaluates LACE Index and HOSPITAL bedside risk scores across all participating sites
#' in a federated consortium in a single pass.
#'
#' @param fc A `federated_consortium` object.
#' @param cohort_table Name of the cohort table or view (default `"cohort"`).
#' @param scores Character vector of scores to compute (`"lace"`, `"hospital"`, or both).
#' @param stratify_by_site Logical. If `TRUE` (default), produces site-stratified audit statistics.
#' @return A `consortium_scores` S3 object containing `$scores`, `$site_summary`, and `$consortium_summary`.
#' @export
consortium_calculate_scores <- function(fc, cohort_table = "cohort", scores = c("lace", "hospital"), stratify_by_site = TRUE) {
  if (!inherits(fc, "federated_consortium")) stop("`fc` must be a federated_consortium object.", call. = FALSE)
  scores_lower <- tolower(scores)
  actual_table <- cohort_table
  has_tbl <- nrow(DBI::dbGetQuery(fc$con, sprintf("SELECT 1 FROM information_schema.tables WHERE table_name = '%s'", actual_table))) > 0
  if (!has_tbl) {
    has_v <- nrow(DBI::dbGetQuery(fc$con, sprintf("SELECT 1 FROM information_schema.tables WHERE table_name = 'v_%s'", actual_table))) > 0
    if (has_v) actual_table <- paste0("v_", actual_table)
  }

  scores_df <- calculate_bedside_scores(fc$con, cohort_table = actual_table, scores = scores)

  # Attach site identifier if available
  cols_lower <- tolower(names(scores_df))
  if (!("site" %in% cols_lower) && ("site_anon" %in% cols_lower)) {
    scores_df$site <- scores_df$site_anon
  } else if (!("site" %in% cols_lower)) {
    cohort_cols <- tolower(DBI::dbGetQuery(fc$con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0;", cohort_table))$column_name)
    if ("site" %in% cohort_cols || "site_anon" %in% cohort_cols) {
      s_col <- if ("site" %in% cohort_cols) "site" else "site_anon"
      p_col <- if ("subject_id" %in% cohort_cols) "subject_id" else "person_id"
      st_col <- if ("cohort_start_date" %in% cohort_cols) "cohort_start_date" else "visit_start_date"
      site_map <- DBI::dbGetQuery(fc$con, sprintf("SELECT DISTINCT %s AS person_id, %s AS cohort_start_date, %s AS site FROM %s", p_col, st_col, s_col, cohort_table))
      scores_df <- merge(scores_df, site_map, by = c("person_id", "cohort_start_date"), all.x = TRUE)
    } else {
      tryCatch({
        site_map <- DBI::dbGetQuery(fc$con, "SELECT DISTINCT person_id, site_anon AS site FROM v_person")
        scores_df <- merge(scores_df, site_map, by = "person_id", all.x = TRUE)
      }, error = function(e) {
        scores_df$site <<- "Consortium"
      })
    }
  }

  if (!("site" %in% names(scores_df)) || all(is.na(scores_df$site))) {
    scores_df$site <- "Site 1"
  } else {
    scores_df$site[is.na(scores_df$site)] <- "Unknown"
  }

  calc_stats <- function(sub_df, site_name) {
    n_pts <- nrow(sub_df)
    row_data <- data.frame(
      site = site_name,
      n_patients = n_pts,
      los_mean = if (n_pts > 0 && "los_days" %in% names(sub_df)) mean(sub_df$los_days, na.rm = TRUE) else 0.0,
      los_median = if (n_pts > 0 && "los_days" %in% names(sub_df)) stats::median(sub_df$los_days, na.rm = TRUE) else 0.0,
      stringsAsFactors = FALSE
    )
    if ("lace" %in% scores_lower && "lace_score" %in% names(sub_df)) {
      row_data$mean_lace_score <- if (n_pts > 0) mean(sub_df$lace_score, na.rm = TRUE) else 0.0
      row_data$median_lace_score <- if (n_pts > 0) stats::median(sub_df$lace_score, na.rm = TRUE) else 0.0
      high_lace <- if (n_pts > 0) sum(sub_df$lace_score >= 10, na.rm = TRUE) else 0L
      mod_lace <- if (n_pts > 0) sum(sub_df$lace_score >= 5 & sub_df$lace_score <= 9, na.rm = TRUE) else 0L
      row_data$high_lace_count <- high_lace
      row_data$high_lace_pct <- if (n_pts > 0) high_lace / n_pts * 100.0 else 0.0
      row_data$moderate_lace_pct <- if (n_pts > 0) mod_lace / n_pts * 100.0 else 0.0
    }
    if ("hospital" %in% scores_lower && "hospital_score" %in% names(sub_df)) {
      row_data$mean_hospital_score <- if (n_pts > 0) mean(sub_df$hospital_score, na.rm = TRUE) else 0.0
      row_data$median_hospital_score <- if (n_pts > 0) stats::median(sub_df$hospital_score, na.rm = TRUE) else 0.0
      high_hosp <- if (n_pts > 0) sum(sub_df$hospital_score >= 7, na.rm = TRUE) else 0L
      int_hosp <- if (n_pts > 0) sum(sub_df$hospital_score >= 5 & sub_df$hospital_score <= 6, na.rm = TRUE) else 0L
      row_data$high_hospital_count <- high_hosp
      row_data$high_hospital_pct <- if (n_pts > 0) high_hosp / n_pts * 100.0 else 0.0
      row_data$intermediate_hospital_pct <- if (n_pts > 0) int_hosp / n_pts * 100.0 else 0.0
    }
    row_data
  }

  sites_unique <- sort(unique(scores_df$site))
  site_rows <- lapply(sites_unique, function(s) calc_stats(scores_df[scores_df$site == s, , drop = FALSE], as.character(s)))
  site_summary_df <- do.call(rbind, site_rows)
  consortium_summary_df <- calc_stats(scores_df, "Consortium (All Sites)")

  res <- list(
    scores = scores_df,
    site_summary = site_summary_df,
    consortium_summary = consortium_summary_df
  )
  class(res) <- c("consortium_scores", "list")
  res
}

#' @export
as.data.frame.consortium_scores <- function(x, ...) {
  x$scores
}

#' @export
summary.consortium_scores <- function(object, ...) {
  rbind(object$site_summary, object$consortium_summary)
}

#' @export
print.consortium_scores <- function(x, ...) {
  cat(sprintf("=== Consortium Bedside Scoring Audit [%d patients across %d site(s)] ===\n",
              nrow(x$scores), nrow(x$site_summary)))
  comb <- rbind(x$site_summary, x$consortium_summary)
  print(comb, row.names = FALSE)
  invisible(x)
}

#' Privacy-Preserving Cell Suppression Auditor
#'
#' @description
#' Scans and redacts small cell counts (e.g. N < 11 or N < 20) across exported
#' consortium summary tables (Table 1, Table 2, risk factor tables) in compliance
#' with HIPAA Safe Harbor, GDPR, and CMS de-identification cell suppression policies.
#'
#' @param tables Single `data.frame` or named `list` of `data.frame`s to scan.
#' @param min_cell_count Threshold below which patient counts are masked (default `11L`).
#' @param mask_value String replacement for suppressed counts (default `paste0("<", min_cell_count)`).
#' @param count_cols Optional character vector of specific count columns to redact.
#' @param suppress_zero Logical; whether to suppress zero counts (default `FALSE`).
#' @param preserve_pct Logical; whether to preserve the percentage in formatted cells like `"5 (2.3\\%)"` (default `FALSE`).
#' @param secondary_suppression Logical; whether to apply complementary cell suppression to prevent algebraic disclosure from marginal totals (default `FALSE`).
#' @param return_audit Logical; whether to return the full privacy audit result object instead of the cleaned data frame when a single table is passed (default `FALSE`).
#'
#' @return An S3 object of class `consortium_privacy_audit` containing `$tables` (sanitized tables),
#'   `$violations` (data.frame of redacted cells), `$violation_count`, `$primary_count`, `$secondary_count`, and `$summary`.
#' @export
audit_cell_suppression <- function(tables,
                                   min_cell_count = 11L,
                                   mask_value = NULL,
                                   count_cols = NULL,
                                   suppress_zero = FALSE,
                                   preserve_pct = FALSE,
                                   secondary_suppression = FALSE,
                                   return_audit = FALSE) {
  is_single <- is.data.frame(tables)
  if (is_single) {
    tbl_list <- list(table = tables)
  } else if (is.list(tables)) {
    tbl_list <- tables
  } else {
    stop("`tables` must be a data.frame or named list of data.frames.", call. = FALSE)
  }

  min_cnt <- as.integer(min_cell_count)
  mask_str <- if (!is.null(mask_value)) as.character(mask_value) else paste0("<", min_cnt)

  skip_cols <- c(
    "p_value", "p_val", "smd", "mean", "sd", "median", "iqr", "hr", "shr",
    "auc", "c_index", "brier", "ece", "mce", "threshold", "step", "variable",
    "feature", "category", "site", "site_anon", "metric", "unit", "concept_id", "loinc"
  )

  count_kw <- c("count", "n_patients", "n_subjects", "_n", "subjects", "patients", "cases", "events", "num", "freq", "total", "high_risk", "readmitted", "deaths")

  should_suppress <- function(n) {
    if (is.na(n)) return(FALSE)
    if (suppress_zero) {
      n >= 0 && n < min_cnt
    } else {
      n >= 1 && n < min_cnt
    }
  }

  parse_cell_count <- function(v) {
    if (is.na(v)) return(list(cnt = NA_integer_, pct = NA_character_))
    if (is.numeric(v) && v == as.integer(v)) return(list(cnt = as.integer(v), pct = NA_character_))
    if (is.character(v) || is.factor(v)) {
      v_s <- trimws(as.character(v))
      m_p <- regmatches(v_s, regexec("^([0-9]+)\\s*\\(([0-9.]+%?)\\)$", v_s))[[1]]
      if (length(m_p) == 3) return(list(cnt = as.integer(m_p[2]), pct = m_p[3]))
      m_i <- regmatches(v_s, regexec("^([0-9]+)$", v_s))[[1]]
      if (length(m_i) == 2) return(list(cnt = as.integer(m_i[2]), pct = NA_character_))
    }
    list(cnt = NA_integer_, pct = NA_character_)
  }

  clean_tables <- list()
  violations_list <- list()

  for (t_name in names(tbl_list)) {
    df <- as.data.frame(tbl_list[[t_name]], stringsAsFactors = FALSE)
    df_clean <- df
    target_count_cols <- character(0)

    for (c_name in names(df_clean)) {
      c_lower <- tolower(trimws(c_name))

      if (!is.null(count_cols)) {
        is_target <- c_name %in% count_cols
      } else {
        if (c_lower %in% skip_cols || grepl("(_pct|_percent|_smd|_rate)$", c_lower)) {
          is_target <- FALSE
        } else if (any(sapply(count_kw, function(kw) grepl(kw, c_lower, fixed = TRUE)))) {
          is_target <- TRUE
        } else {
          is_target <- TRUE
        }
      }

      if (!is_target) next
      target_count_cols <- c(target_count_cols, c_name)

      col_vals <- df_clean[[c_name]]
      col_mod <- FALSE

      for (r_idx in seq_along(col_vals)) {
        v <- col_vals[r_idx]
        if (is.na(v)) next

        masked <- FALSE
        new_v <- v

        if (is.numeric(v) && (v == as.integer(v)) && (!is.null(count_cols) || any(sapply(count_kw, function(kw) grepl(kw, c_lower, fixed = TRUE))))) {
          n_val <- as.integer(v)
          if (should_suppress(n_val)) {
            new_v <- mask_str
            masked <- TRUE
          }
        } else if (is.character(v) || is.factor(v)) {
          v_str <- trimws(as.character(v))
          m_pct <- regexec("^([0-9]+)\\s*\\(([0-9.]+%?)\\)$", v_str)
          matches <- regmatches(v_str, m_pct)[[1]]
          if (length(matches) == 3) {
            n_val <- as.integer(matches[2])
            if (should_suppress(n_val)) {
              new_v <- if (preserve_pct) paste0(mask_str, " (", matches[3], ")") else mask_str
              masked <- TRUE
            }
          } else {
            m_int <- regexec("^([0-9]+)$", v_str)
            m_int_matches <- regmatches(v_str, m_int)[[1]]
            if (length(m_int_matches) == 2 && (!is.null(count_cols) || any(sapply(count_kw, function(kw) grepl(kw, c_lower, fixed = TRUE))) || (r_idx == 1 && grepl("total", tolower(as.character(df_clean[1, 1])))))) {
              n_val <- as.integer(m_int_matches[2])
              if (should_suppress(n_val)) {
                new_v <- mask_str
                masked <- TRUE
              }
            }
          }
        }

        if (masked) {
          col_mod <- TRUE
          violations_list[[length(violations_list) + 1]] <- data.frame(
            table = t_name,
            row_index = r_idx,
            column = c_name,
            original_value = as.character(v),
            masked_value = as.character(new_v),
            suppression_type = "primary",
            stringsAsFactors = FALSE
          )
          col_vals[r_idx] <- new_v
        }
      }

      if (col_mod) {
        df_clean[[c_name]] <- col_vals
      }
    }

    # Secondary / complementary cell suppression
    if (secondary_suppression && length(target_count_cols) > 0) {
      current_v <- if (length(violations_list) > 0) do.call(rbind, violations_list) else NULL

      # a) Row-level secondary suppression
      for (r_idx in seq_len(nrow(df_clean))) {
        supp_row <- if (!is.null(current_v)) {
          current_v[current_v$table == t_name & current_v$row_index == r_idx & current_v$column %in% target_count_cols, , drop = FALSE]
        } else NULL

        if (!is.null(supp_row) && nrow(supp_row) == 1) {
          supp_col <- supp_row$column[1]
          cand_cols <- setdiff(target_count_cols, supp_col)
          cand_counts <- integer(0)
          cand_pcts <- character(0)
          valid_cols <- character(0)

          for (cc in cand_cols) {
            val <- df_clean[r_idx, cc]
            parsed <- parse_cell_count(val)
            if (!is.na(parsed$cnt) && parsed$cnt > 0) {
              cand_counts <- c(cand_counts, parsed$cnt)
              cand_pcts <- c(cand_pcts, if (is.na(parsed$pct)) "" else parsed$pct)
              valid_cols <- c(valid_cols, cc)
            }
          }

          if (length(valid_cols) > 0) {
            min_idx <- which.min(cand_counts)
            sec_col <- valid_cols[min_idx]
            sec_pct <- cand_pcts[min_idx]
            sec_orig <- as.character(df_clean[r_idx, sec_col])
            sec_masked <- if (preserve_pct && nzchar(sec_pct)) paste0(mask_str, " (", sec_pct, ")") else mask_str

            df_clean[r_idx, sec_col] <- sec_masked
            violations_list[[length(violations_list) + 1]] <- data.frame(
              table = t_name,
              row_index = r_idx,
              column = sec_col,
              original_value = sec_orig,
              masked_value = sec_masked,
              suppression_type = "secondary",
              stringsAsFactors = FALSE
            )
            current_v <- do.call(rbind, violations_list)
          }
        }
      }

      # b) Column-level secondary suppression
      for (cc in target_count_cols) {
        supp_col <- if (!is.null(current_v)) {
          current_v[current_v$table == t_name & current_v$column == cc, , drop = FALSE]
        } else NULL

        if (!is.null(supp_col) && nrow(supp_col) == 1) {
          supp_row_idx <- supp_col$row_index[1]
          cand_rows <- setdiff(seq_len(nrow(df_clean)), supp_row_idx)
          cand_counts <- integer(0)
          cand_pcts <- character(0)
          valid_rows <- integer(0)

          for (rr in cand_rows) {
            val <- df_clean[rr, cc]
            parsed <- parse_cell_count(val)
            if (!is.na(parsed$cnt) && parsed$cnt > 0) {
              cand_counts <- c(cand_counts, parsed$cnt)
              cand_pcts <- c(cand_pcts, if (is.na(parsed$pct)) "" else parsed$pct)
              valid_rows <- c(valid_rows, rr)
            }
          }

          if (length(valid_rows) > 0) {
            min_idx <- which.min(cand_counts)
            sec_r <- valid_rows[min_idx]
            sec_pct <- cand_pcts[min_idx]
            sec_orig <- as.character(df_clean[sec_r, cc])
            sec_masked <- if (preserve_pct && nzchar(sec_pct)) paste0(mask_str, " (", sec_pct, ")") else mask_str

            df_clean[sec_r, cc] <- sec_masked
            violations_list[[length(violations_list) + 1]] <- data.frame(
              table = t_name,
              row_index = sec_r,
              column = cc,
              original_value = sec_orig,
              masked_value = sec_masked,
              suppression_type = "secondary",
              stringsAsFactors = FALSE
            )
            current_v <- do.call(rbind, violations_list)
          }
        }
      }
    }

    clean_tables[[t_name]] <- df_clean
  }

  violations_df <- if (length(violations_list) > 0) {
    do.call(rbind, violations_list)
  } else {
    data.frame(
      table = character(0),
      row_index = integer(0),
      column = character(0),
      original_value = character(0),
      masked_value = character(0),
      suppression_type = character(0),
      stringsAsFactors = FALSE
    )
  }

  summary_rows <- lapply(names(clean_tables), function(nm) {
    df_t <- clean_tables[[nm]]
    tbl_v <- violations_df[violations_df$table == nm, , drop = FALSE]
    n_v <- nrow(tbl_v)
    prim_n <- if ("suppression_type" %in% names(tbl_v)) sum(tbl_v$suppression_type == "primary") else n_v
    sec_n <- if ("suppression_type" %in% names(tbl_v)) sum(tbl_v$suppression_type == "secondary") else 0L

    data.frame(
      table = nm,
      table_name = nm,
      n_rows = nrow(df_t),
      n_columns = ncol(df_t),
      suppressed_cells = n_v,
      primary_suppressed = prim_n,
      secondary_suppressed = sec_n,
      violations = n_v,
      min_cell_count = min_cnt,
      status = if (n_v == 0) "CLEAN" else paste0("SUPPRESSED_", n_v),
      stringsAsFactors = FALSE
    )
  })
  summary_df <- do.call(rbind, summary_rows)

  res <- list(
    tables = clean_tables,
    violations = violations_df,
    violation_count = nrow(violations_df),
    primary_count = if ("suppression_type" %in% names(violations_df)) sum(violations_df$suppression_type == "primary") else nrow(violations_df),
    secondary_count = if ("suppression_type" %in% names(violations_df)) sum(violations_df$suppression_type == "secondary") else 0L,
    is_clean = (nrow(violations_df) == 0L),
    min_cell_count = min_cnt,
    mask_value = mask_str,
    is_single_df = is_single,
    summary = summary_df
  )
  class(res) <- c("consortium_privacy_audit", "list")

  if (is_single && !return_audit) {
    single_df <- clean_tables[[1]]
    attr(single_df, "suppressed_cell_count") <- nrow(violations_df)
    attr(single_df, "primary_suppressed_count") <- res$primary_count
    attr(single_df, "secondary_suppressed_count") <- res$secondary_count
    attr(single_df, "is_clean") <- (nrow(violations_df) == 0L)
    return(single_df)
  }

  res
}

#' @rdname audit_cell_suppression
#' @export
consortium_privacy_audit <- audit_cell_suppression

#' @export
print.consortium_privacy_audit <- function(x, ...) {
  cat(sprintf("=== Consortium Privacy Audit [%d table(s), %d suppressed cell(s) (N < %d)] ===\n",
              length(x$tables), x$violation_count, x$min_cell_count))
  print(x$summary, row.names = FALSE)
  invisible(x)
}

#' @export
summary.consortium_privacy_audit <- function(object, ...) {
  object$summary
}

#' @export
as.data.frame.consortium_privacy_audit <- function(x, ...) {
  if (x$is_single_df && length(x$tables) == 1) {
    return(x$tables[[1]])
  }
  x$violations
}





