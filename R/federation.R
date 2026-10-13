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
    "drug_era", "condition_era", "provider", "care_site", "cdm_source"
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


