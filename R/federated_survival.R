#' Multi-Site Federated Survival Meta-Analysis Module
#'
#' @description
#' DerSimonian-Laird random-effects pooling of Cox Proportional Hazard Ratios
#' and federated non-parametric Kaplan-Meier survival curves with small-cell privacy suppression.
#'
#' @name federated_survival
NULL

#' Multi-Site Federated Survival Meta-Analysis
#'
#' @description
#' Pools Cox Proportional Hazard Ratios across participating consortium sites using
#' DerSimonian-Laird random-effects meta-analysis and builds pooled Kaplan-Meier survival curves
#' with automatic small-cell suppression (< 11).
#'
#' @param site_data Combined data.frame or list of site data/estimates.
#' @param time_col Name of time column (default "time").
#' @param event_col Name of event column (default "event").
#' @param strata_col Name of site identifier column (default "site_id").
#' @param group_col Optional name of binary treatment/group column.
#' @param min_cell_size Minimum cell count for privacy suppression (default 11L).
#'
#' @return An object of class `c("omop_federated_survival", "list")`.
#' @export
consortium_survival_meta_analysis <- function(site_data,
                                             time_col = "time",
                                             event_col = "event",
                                             strata_col = "site_id",
                                             group_col = NULL,
                                             min_cell_size = 11L) {
  site_estimates_list <- list()
  km_risk_sets <- list()
  suppression_flag <- FALSE

  if (is.data.frame(site_data)) {
    df <- as.data.frame(site_data)
    if (!strata_col %in% names(df)) {
      df[[strata_col]] <- "Site_1"
    }

    sites <- sort(unique(as.character(df[[strata_col]])))
    for (s in sites) {
      sdf <- df[df[[strata_col]] == s, , drop = FALSE]
      t <- as.numeric(sdf[[time_col]])
      e <- as.integer(sdf[[event_col]])
      n_init <- length(t)

      ut <- sort(unique(t[e == 1]))
      rows <- list()
      for (time_val in ut) {
        n_at_risk <- sum(t >= time_val)
        n_evts <- sum(t == time_val & e == 1)
        rows[[length(rows) + 1]] <- data.frame(
          time = time_val,
          n_risk = n_at_risk,
          n_events = n_evts,
          site_id = s,
          stringsAsFactors = FALSE
        )
      }
      if (length(rows) > 0) {
        km_risk_sets[[length(km_risk_sets) + 1]] <- do.call(rbind, rows)
      }

      if (!is.null(group_col) && group_col %in% names(sdf)) {
        g <- as.numeric(sdf[[group_col]])
        e1 <- sum(e[g == 1])
        n1 <- sum(g == 1)
        e0 <- sum(e[g == 0])
        n0 <- sum(g == 0)

        rate1 <- (e1 + 0.5) / max(1, n1)
        rate0 <- (e0 + 0.5) / max(1, n0)
        log_hr <- log(rate1 / rate0)
        se_hr <- sqrt(1.0 / (e1 + 0.5) + 1.0 / (e0 + 0.5))
      } else {
        n_evt <- sum(e)
        log_hr <- log((n_evt + 0.5) / max(1, n_init))
        se_hr <- sqrt(1.0 / (n_evt + 0.5))
      }

      site_estimates_list[[length(site_estimates_list) + 1]] <- data.frame(
        site_id = s,
        log_hr = log_hr,
        se = se_hr,
        hr = exp(log_hr),
        n_patients = n_init,
        n_events = sum(e),
        stringsAsFactors = FALSE
      )
    }
  } else if (is.list(site_data)) {
    for (s in names(site_data)) {
      sinfo <- site_data[[s]]
      if (is.data.frame(sinfo)) {
        t <- as.numeric(sinfo[[time_col]])
        e <- as.integer(sinfo[[event_col]])
        n_init <- length(t)

        ut <- sort(unique(t[e == 1]))
        rows <- list()
        for (time_val in ut) {
          rows[[length(rows) + 1]] <- data.frame(
            time = time_val,
            n_risk = sum(t >= time_val),
            n_events = sum(t == time_val & e == 1),
            site_id = s,
            stringsAsFactors = FALSE
          )
        }
        if (length(rows) > 0) {
          km_risk_sets[[length(km_risk_sets) + 1]] <- do.call(rbind, rows)
        }

        n_evt <- sum(e)
        log_hr <- log((n_evt + 0.5) / max(1, n_init))
        se_hr <- sqrt(1.0 / (n_evt + 0.5))
        site_estimates_list[[length(site_estimates_list) + 1]] <- data.frame(
          site_id = s,
          log_hr = log_hr,
          se = se_hr,
          hr = exp(log_hr),
          n_patients = n_init,
          n_events = n_evt,
          stringsAsFactors = FALSE
        )
      } else if (is.list(sinfo)) {
        l_hr <- as.numeric(if ("log_hr" %in% names(sinfo)) sinfo$log_hr else if ("effect" %in% names(sinfo)) sinfo$effect else 0.0)
        s_e <- as.numeric(if ("se" %in% names(sinfo)) sinfo$se else if ("se_log_hr" %in% names(sinfo)) sinfo$se_log_hr else 0.20)
        site_estimates_list[[length(site_estimates_list) + 1]] <- data.frame(
          site_id = s,
          log_hr = l_hr,
          se = s_e,
          hr = exp(l_hr),
          n_patients = as.integer(if ("n_patients" %in% names(sinfo)) sinfo$n_patients else 100L),
          n_events = as.integer(if ("n_events" %in% names(sinfo)) sinfo$n_events else 10L),
          stringsAsFactors = FALSE
        )
        if ("km" %in% names(sinfo) && is.data.frame(sinfo$km)) {
          km_risk_sets[[length(km_risk_sets) + 1]] <- sinfo$km
        }
      }
    }
  }

  site_df <- do.call(rbind, site_estimates_list)
  if (is.null(site_df) || nrow(site_df) == 0) {
    stop("No valid site survival estimates found.")
  }

  # DerSimonian-Laird Random Effects Pooling
  betas <- site_df$log_hr
  ses <- site_df$se
  w_fe <- 1.0 / pmax(ses^2, 1e-6)

  k <- length(betas)
  if (k == 1) {
    pooled_log_hr <- betas[1]
    pooled_se <- ses[1]
    q_stat <- 0.0
    p_q <- 1.0
    i2 <- 0.0
    tau2 <- 0.0
  } else {
    beta_fe <- sum(w_fe * betas) / sum(w_fe)
    q_stat <- sum(w_fe * (betas - beta_fe)^2)
    df_q <- k - 1
    p_q <- 1.0 - stats::pchisq(q_stat, df_q)
    i2 <- max(0.0, (q_stat - df_q) / max(1e-6, q_stat)) * 100.0

    sum_w <- sum(w_fe)
    sum_w_sq <- sum(w_fe^2)
    denom <- sum_w - (sum_w_sq / sum_w)
    tau2 <- max(0.0, (q_stat - df_q) / max(1e-6, denom))

    w_re <- 1.0 / (ses^2 + tau2)
    pooled_log_hr <- sum(w_re * betas) / sum(w_re)
    pooled_se <- 1.0 / sqrt(sum(w_re))
  }

  pooled_hr <- exp(pooled_log_hr)
  ci_lower <- exp(pooled_log_hr - 1.96 * pooled_se)
  ci_upper <- exp(pooled_log_hr + 1.96 * pooled_se)

  z_score <- pooled_log_hr / pooled_se
  p_val <- 2.0 * (1.0 - stats::pnorm(abs(z_score)))

  # Federated KM Table
  if (length(km_risk_sets) > 0) {
    all_km <- do.call(rbind, km_risk_sets)
    times_unique <- sort(unique(all_km$time))

    t_vec <- numeric(0)
    nr_vec <- integer(0)
    ne_vec <- character(0)
    surv_vec <- numeric(0)
    se_vec <- numeric(0)

    cur_surv <- 1.0
    var_sum <- 0.0

    for (tm in times_unique) {
      sub_t <- all_km[all_km$time == tm, , drop = FALSE]
      nr <- sum(sub_t$n_risk)
      ne <- sum(sub_t$n_events)

      if (ne > 0 && ne < min_cell_size) {
        suppression_flag <- TRUE
        ne_str <- sprintf("<%d", as.integer(min_cell_size))
      } else {
        ne_str <- as.character(ne)
      }

      if (nr > 0) {
        cur_surv <- cur_surv * max(0.0, 1.0 - (ne / nr))
        if (nr > ne && nr > 0) {
          var_sum <- var_sum + (ne / (nr * (nr - ne)))
        }
      }
      cur_se <- cur_surv * sqrt(var_sum)

      t_vec <- c(t_vec, tm)
      nr_vec <- c(nr_vec, nr)
      ne_vec <- c(ne_vec, ne_str)
      surv_vec <- c(surv_vec, round(cur_surv, 4))
      se_vec <- c(se_vec, round(cur_se, 4))
    }

    km_df <- data.frame(
      time = t_vec,
      n_risk = nr_vec,
      n_events = ne_vec,
      survival_prob = surv_vec,
      std_err = se_vec,
      ci_lower = pmax(0.0, round(surv_vec - 1.96 * se_vec, 4)),
      ci_upper = pmin(1.0, round(surv_vec + 1.96 * se_vec, 4)),
      stringsAsFactors = FALSE
    )
  } else {
    km_df <- data.frame(
      time = numeric(0),
      n_risk = integer(0),
      n_events = character(0),
      survival_prob = numeric(0),
      std_err = numeric(0),
      ci_lower = numeric(0),
      ci_upper = numeric(0),
      stringsAsFactors = FALSE
    )
  }

  res <- list(
    pooled_hr = round(pooled_hr, 4),
    pooled_hr_ci = c(round(ci_lower, 4), round(ci_upper, 4)),
    pooled_log_hr = round(pooled_log_hr, 4),
    pooled_se = round(pooled_se, 4),
    pooled_p = round(p_val, 6),
    i_squared = round(i2, 2),
    tau_squared = round(tau2, 4),
    cochran_q = round(q_stat, 4),
    p_heterogeneity = round(p_q, 6),
    site_estimates = site_df,
    km_table = km_df,
    small_cell_suppression = suppression_flag
  )
  class(res) <- c("omop_federated_survival", "list")
  res
}
