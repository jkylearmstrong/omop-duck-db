#' Data Integrity, Statistical Leakage Scan, Informative Missingness, Provenance, and Overlap Tools
#'
#' @name integrity
#' @keywords internal
NULL

# -----------------------------------------------------------------------------
# 1. Statistical Leakage Scan
# -----------------------------------------------------------------------------

#' Statistical Leakage Scan
#'
#' Scans features for statistical and temporal data leakage against a binary outcome.
#' Checks:
#' 1. Zero variance.
#' 2. Deterministic stratum separation (pure 0 or 1 outcome rates in feature strata).
#' 3. Single-feature AUC above threshold.
#' 4. Temporal leakage (feature dates occurring after index date).
#'
#' @param X Data frame of clinical features (or data frame containing features and outcome).
#' @param y Binary outcome indicator (0 or 1), or string column name in X.
#' @param index_date Optional index admission/anchor date(s) to verify temporal sequencing.
#' @param feature_dates Optional named list or data frame mapping feature names to dates.
#' @param max_auc Maximum allowable single-feature AUC before flagging (default 0.98).
#' @param min_stratum_n Minimum stratum size to evaluate pure 0/1 separation (default 10).
#' @return A data frame with columns: \code{feature}, \code{check}, \code{value}, \code{threshold}, \code{flagged}, \code{details}.
#' @export
leakage_scan <- function(X, y, index_date = NULL, feature_dates = NULL, max_auc = 0.98, min_stratum_n = 10) {
  df <- as.data.frame(X)

  if (is.character(y) && length(y) == 1 && y %in% names(df)) {
    outcome <- as.integer(df[[y]])
    df[[y]] <- NULL
  } else {
    outcome <- as.integer(y)
  }

  n <- length(outcome)
  records <- list()

  # Check temporal leakage if dates supplied
  if (!is.null(index_date) && !is.null(feature_dates)) {
    idx_dt <- as.Date(index_date)
    for (feat in names(feature_dates)) {
      if (feat %in% names(df)) {
        f_dt <- as.Date(feature_dates[[feat]])
        post_count <- sum(f_dt > idx_dt, na.rm = TRUE)
        flagged <- post_count > 0
        records[[length(records) + 1]] <- data.frame(
          feature = feat,
          check = "temporal_leakage",
          value = post_count,
          threshold = 0,
          flagged = flagged,
          details = if (flagged) sprintf("%d observations occurred after index date", post_count) else "Clean temporal sequence",
          stringsAsFactors = FALSE
        )
      }
    }
  }

  for (col in names(df)) {
    vals <- df[[col]]
    valid <- !is.na(vals)
    if (!any(valid)) {
      records[[length(records) + 1]] <- data.frame(
        feature = col,
        check = "all_missing",
        value = 1.0,
        threshold = 0.0,
        flagged = TRUE,
        details = "Feature is 100 percent missing",
        stringsAsFactors = FALSE
      )
      next
    }

    v <- vals[valid]
    o <- outcome[valid]

    # Zero variance
    if (length(unique(v)) <= 1) {
      records[[length(records) + 1]] <- data.frame(
        feature = col,
        check = "zero_variance",
        value = 0.0,
        threshold = 0.0,
        flagged = TRUE,
        details = "Feature has constant value across all rows",
        stringsAsFactors = FALSE
      )
      next
    }

    # Deterministic stratum separation
    unique_vals <- unique(v)
    strat_pure <- FALSE
    strat_details <- c()

    if (length(unique_vals) <= 10) {
      for (u in unique_vals) {
        mask_u <- (v == u)
        sub_n <- sum(mask_u)
        if (sub_n >= min_stratum_n) {
          rate <- mean(o[mask_u])
          if (rate == 0.0 || rate == 1.0) {
            strat_pure <- TRUE
            strat_details <- c(strat_details, sprintf("val=%s (N=%d, rate=%.2f)", as.character(u), sub_n, rate))
          }
        }
      }
    } else {
      q10 <- stats::quantile(v, 0.10, na.rm = TRUE)
      q90 <- stats::quantile(v, 0.90, na.rm = TRUE)
      for (q_info in list(list(mask = (v <= q10), name = "<= q10"), list(mask = (v >= q90), name = ">= q90"))) {
        sub_n <- sum(q_info$mask)
        if (sub_n >= min_stratum_n) {
          rate <- mean(o[q_info$mask])
          if (rate == 0.0 || rate == 1.0) {
            strat_pure <- TRUE
            strat_details <- c(strat_details, sprintf("%s (N=%d, rate=%.2f)", q_info$name, sub_n, rate))
          }
        }
      }
    }

    if (strat_pure) {
      records[[length(records) + 1]] <- data.frame(
        feature = col,
        check = "pure_stratum_separation",
        value = 1.0,
        threshold = 0.0,
        flagged = TRUE,
        details = paste("Deterministic outcome in strata:", paste(strat_details, collapse = "; ")),
        stringsAsFactors = FALSE
      )
    }

    # AUC via Wilcoxon rank-sum
    v_pos <- v[o == 1]
    v_neg <- v[o == 0]
    if (length(v_pos) > 0 && length(v_neg) > 0) {
      ranked <- rank(v)
      rank_sum_pos <- sum(ranked[o == 1])
      n1 <- length(v_pos)
      n0 <- length(v_neg)
      u_stat <- rank_sum_pos - (n1 * (n1 + 1)) / 2.0
      auc <- u_stat / (n1 * n0)
      max_effective_auc <- max(auc, 1.0 - auc)

      flagged_auc <- max_effective_auc >= max_auc
      records[[length(records) + 1]] <- data.frame(
        feature = col,
        check = "high_auc",
        value = round(max_effective_auc, 4),
        threshold = max_auc,
        flagged = flagged_auc,
        details = if (flagged_auc) sprintf("Single-feature AUC = %.4f (>= %.2f)", max_effective_auc, max_auc) else sprintf("AUC = %.4f", max_effective_auc),
        stringsAsFactors = FALSE
      )
    }
  }

  if (length(records) == 0) {
    return(data.frame(
      feature = character(),
      check = character(),
      value = numeric(),
      threshold = numeric(),
      flagged = logical(),
      details = character(),
      stringsAsFactors = FALSE
    ))
  }

  out <- do.call(rbind, records)
  out <- out[order(out$flagged, out$value, decreasing = TRUE), ]
  rownames(out) <- NULL
  out
}

# -----------------------------------------------------------------------------
# 2. Informative Missingness Helpers
# -----------------------------------------------------------------------------

#' Add Missing Indicator Columns
#'
#' Adds binary 1/0 indicator columns for specified features where 1 denotes a missing value.
#'
#' @param df Input data frame.
#' @param cols Optional vector of column names. If NULL, applies to all columns with missing values.
#' @param prefix Prefix for the new indicator columns (default 'miss_').
#' @return Data frame with additional missing indicator columns.
#' @export
add_missing_indicators <- function(df, cols = NULL, prefix = "miss_") {
  out <- as.data.frame(df)
  if (is.null(cols)) {
    cols <- names(out)[vapply(out, function(col) any(is.na(col)), logical(1))]
  }

  for (c in cols) {
    if (c %in% names(out)) {
      out[[paste0(prefix, c)]] <- as.integer(is.na(out[[c]]))
    }
  }
  out
}

#' Missingness Stratified by Outcome
#'
#' Profiles missingness proportions in outcome = 0 vs outcome = 1 groups.
#'
#' @param df Input data frame.
#' @param outcome_col Name of binary outcome column (0 / 1).
#' @param cols Optional vector of columns to evaluate.
#' @param by Optional site column to stratify by.
#' @param diff_threshold Absolute missingness difference threshold to flag (default 0.05 = 5 percent).
#' @param fill_rule_notes Optional named list mapping column names to fill-rule documentation.
#' @return Audit data frame with missingness rates, differences, and flags.
#' @export
missingness_by_outcome <- function(df, outcome_col, cols = NULL, by = NULL, diff_threshold = 0.05, fill_rule_notes = NULL) {
  if (!outcome_col %in% names(df)) {
    stop(sprintf("Outcome column '%s' not found in dataframe.", outcome_col))
  }

  exclude <- c(outcome_col)
  if (!is.null(by) && by %in% names(df)) {
    exclude <- c(exclude, by)
  }

  if (is.null(cols)) {
    eval_cols <- setdiff(names(df), exclude)
  } else {
    eval_cols <- intersect(cols, setdiff(names(df), exclude))
  }

  notes <- if (is.null(fill_rule_notes)) list() else fill_rule_notes

  strata <- list(list(site = "ALL", data = df))
  if (!is.null(by) && by %in% names(df)) {
    for (s_val in unique(df[[by]])) {
      strata[[length(strata) + 1]] <- list(site = as.character(s_val), data = df[df[[by]] == s_val, ])
    }
  }

  records <- list()
  for (st in strata) {
    s_label <- st$site
    sub <- st$data

    ctrl <- sub[sub[[outcome_col]] == 0, ]
    case <- sub[sub[[outcome_col]] == 1, ]
    n_ctrl <- nrow(ctrl)
    n_case <- nrow(case)

    for (col in eval_cols) {
      m_ctrl <- if (n_ctrl > 0) sum(is.na(ctrl[[col]])) else 0
      m_case <- if (n_case > 0) sum(is.na(case[[col]])) else 0
      r_ctrl <- if (n_ctrl > 0) m_ctrl / n_ctrl else 0.0
      r_case <- if (n_case > 0) m_case / n_case else 0.0
      diff <- r_case - r_ctrl
      flagged <- abs(diff) >= diff_threshold

      disp_ctrl <- if (m_ctrl > 0 && m_ctrl < 11) "<11" else as.character(m_ctrl)
      disp_case <- if (m_case > 0 && m_case < 11) "<11" else as.character(m_case)

      rule_val <- if (!is.null(notes[[col]])) notes[[col]] else "None"

      records[[length(records) + 1]] <- data.frame(
        site = s_label,
        feature = col,
        n_ctrl = n_ctrl,
        miss_ctrl = disp_ctrl,
        rate_ctrl = round(r_ctrl, 4),
        n_case = n_case,
        miss_case = disp_case,
        rate_case = round(r_case, 4),
        missingness_diff = round(diff, 4),
        flagged = flagged,
        fill_rule = rule_val,
        stringsAsFactors = FALSE
      )
    }
  }

  if (length(records) == 0) {
    return(data.frame())
  }
  do.call(rbind, records)
}

# -----------------------------------------------------------------------------
# 3. Provenance Stamp and Vocabulary Consistency
# -----------------------------------------------------------------------------

#' Provenance Stamp
#'
#' Generates a reproducible system provenance stamp capturing package version, DuckDB version,
#' vocabulary release versions, seed, and git commit.
#'
#' @param con Optional active DuckDB connection.
#' @param seed Optional random seed.
#' @param vocab_schema Optional schema or attached database name for vocabulary tables.
#' @param vocab_db Alias for vocab_schema.
#' @return Named list of provenance metadata.
#' @export
provenance <- function(con = NULL, seed = NULL, vocab_schema = NULL, vocab_db = NULL) {
  v_schema <- if (!is.null(vocab_schema)) vocab_schema else vocab_db
  prov <- list(
    package_name = "omopduckdb",
    package_version = "0.6.5",
    duckdb_version = as.character(utils::packageVersion("duckdb")),
    timestamp_utc = format(as.POSIXlt(Sys.time(), tz = "UTC"), "%Y-%m-%dT%H:%M:%SZ"),
    seed = seed,
    git_commit = "c072171c3c33098101ba7eeff99d3b466abf1da9",
    vocabularies = list()
  )

  # Try git HEAD from repository root or current directory
  tryCatch({
    candidate_dirs <- c(getwd(), tryCatch(rprojroot::find_root(rprojroot::is_git_root), error = function(e) ""))
    found_git <- FALSE
    for (d in candidate_dirs) {
      if (nzchar(d) && dir.exists(file.path(d, ".git"))) {
        head <- system(sprintf("git -C %s rev-parse HEAD", shQuote(d)), intern = TRUE, ignore.stderr = TRUE)
        if (length(head) > 0 && nchar(head[1]) > 0) {
          prov$git_commit <- head[1]
          found_git <- TRUE
          break
        }
      }
    }
    if (!found_git) {
      head <- system("git rev-parse HEAD", intern = TRUE, ignore.stderr = TRUE)
      if (length(head) > 0 && nchar(head[1]) > 0) {
        prov$git_commit <- head[1]
      }
    }
  }, error = function(e) NULL)

  if (!is.null(con)) {
    tryCatch({
      vocab_tbl <- if (!is.null(v_schema)) sprintf("%s.vocabulary", v_schema) else "vocabulary"
      has_vocab <- tryCatch({
        DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM %s", vocab_tbl))$n[1] >= 0
      }, error = function(e) {
        if (is.null(v_schema)) {
          tryCatch({
            DBI::dbGetQuery(con, "SELECT count(*) AS n FROM central_vocab.vocabulary")$n[1] >= 0
          }, error = function(e2) FALSE)
        } else FALSE
      })
      if (is.null(v_schema) && !has_vocab) {
        # check if central_vocab has it
        has_central <- tryCatch(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM central_vocab.vocabulary")$n[1] >= 0, error = function(e) FALSE)
        if (has_central) {
          vocab_tbl <- "central_vocab.vocabulary"
          has_vocab <- TRUE
        }
      }

      if (has_vocab) {
        rows <- DBI::dbGetQuery(con, sprintf("
          SELECT vocabulary_id, vocabulary_version
          FROM %s
          WHERE vocabulary_id IN ('None', 'RxNorm', 'SNOMED', 'LOINC', 'ATC', 'ICD10CM')
             OR vocabulary_version IS NOT NULL
        ", vocab_tbl))
        for (i in seq_len(nrow(rows))) {
          vid <- rows$vocabulary_id[i]
          vver <- rows$vocabulary_version[i]
          prov$vocabularies[[vid]] <- if (is.na(vver)) "unspecified" else vver
        }
      }
    }, error = function(e) NULL)
  }

  prov$vocabulary <- list(
    reachable = length(prov$vocabularies) > 0,
    version_string = if ("RxNorm" %in% names(prov$vocabularies)) prov$vocabularies[["RxNorm"]] else if (length(prov$vocabularies) > 0) prov$vocabularies[[1]] else NULL
  )

  prov
}

#' Check Vocabulary Provenance Between Databases
#'
#' Verifies that two DuckDB OMOP instances share identical vocabulary releases.
#'
#' @param con1 First DuckDB connection.
#' @param con2 Second DuckDB connection.
#' @param vocab_schema Optional schema or attached database name for vocabulary tables.
#' @param vocab_db Alias for vocab_schema.
#' @return A list with \code{matched} (logical) and \code{comparison} (data frame).
#' @export
check_vocabulary_provenance <- function(con1, con2, vocab_schema = NULL, vocab_db = NULL) {
  p1 <- provenance(con1, vocab_schema = vocab_schema, vocab_db = vocab_db)$vocabularies
  p2 <- provenance(con2, vocab_schema = vocab_schema, vocab_db = vocab_db)$vocabularies

  all_vocabs <- sort(unique(c(names(p1), names(p2))))
  matched <- TRUE
  records <- list()

  for (v in all_vocabs) {
    v1 <- if (!is.null(p1[[v]])) p1[[v]] else "MISSING"
    v2 <- if (!is.null(p2[[v]])) p2[[v]] else "MISSING"
    is_match <- (v1 == v2) && (v1 != "MISSING")
    if (!is_match) matched <- FALSE

    records[[length(records) + 1]] <- data.frame(
      vocabulary_id = v,
      version_db1 = v1,
      version_db2 = v2,
      matched = is_match,
      stringsAsFactors = FALSE
    )
  }

  comp_df <- if (length(records) > 0) do.call(rbind, records) else data.frame()
  list(matched = matched, comparison = comp_df)
}

# -----------------------------------------------------------------------------
# 4. Numbers-of-Record Manifest and Stale-Number Scanner
# -----------------------------------------------------------------------------

#' Numbers-of-Record Manifest
#'
#' Generates an authoritative manifest of cohort sample sizes and outcome event counts.
#'
#' @param cohorts Named list of cohort data frames.
#' @param outcome_col Name of outcome column (default 'outcome').
#' @param site_col Name of site column (default 'site').
#' @param path Optional JSON file path to write manifest.
#' @return Named list manifest.
#' @export
numbers_of_record <- function(cohorts, outcome_col = "outcome", site_col = "site", path = NULL) {
  manifest <- list(
    manifest_version = "1.0",
    created_at = format(as.POSIXlt(Sys.time(), tz = "UTC"), "%Y-%m-%dT%H:%M:%SZ"),
    cohorts = list()
  )

  for (name in names(cohorts)) {
    df <- as.data.frame(cohorts[[name]])
    n <- nrow(df)
    events <- if (!is.null(outcome_col) && outcome_col %in% names(df)) sum(df[[outcome_col]], na.rm = TRUE) else NULL
    rate <- if (!is.null(events) && n > 0) round(events / n, 6) else NULL

    sites <- list()
    if (!is.null(site_col) && site_col %in% names(df)) {
      for (s_val in unique(df[[site_col]])) {
        sub <- df[df[[site_col]] == s_val, ]
        sn <- nrow(sub)
        sevents <- if (!is.null(outcome_col) && outcome_col %in% names(sub)) sum(sub[[outcome_col]], na.rm = TRUE) else NULL
        srate <- if (!is.null(sevents) && sn > 0) round(sevents / sn, 6) else NULL
        sites[[as.character(s_val)]] <- list(total_n = sn, events = sevents, event_rate = srate)
      }
    }

    manifest$cohorts[[name]] <- list(
      total_n = n,
      events = events,
      event_rate = rate,
      sites = sites
    )
  }

  if (!is.null(path)) {
    json_txt <- if (requireNamespace("jsonlite", quietly = TRUE)) {
      jsonlite::toJSON(manifest, auto_unbox = TRUE, pretty = TRUE)
    } else {
      # Fallback minimal JSON formatting
      paste0('{"manifest_version":"1.0","created_at":"', manifest$created_at, '"}')
    }
    writeLines(json_txt, path)
  }

  manifest
}

#' Scan Document Numbers for Stale Figures
#'
#' Scans document text for numbers and cross-references against authoritative manifests.
#'
#' @param doc_text_or_path Document text string or file path.
#' @param manifest Current authoritative numbers manifest.
#' @param previous_manifest Optional previous manifest to flag stale numbers.
#' @return Data frame with columns: \code{number}, \code{status}, \code{source}, \code{context}.
#' @export
scan_document_numbers <- function(doc_text_or_path, manifest, previous_manifest = NULL) {
  text <- if (file.exists(doc_text_or_path)) {
    paste(readLines(doc_text_or_path, warn = FALSE), collapse = "\n")
  } else {
    doc_text_or_path
  }

  extract_nums <- function(m) {
    s <- c()
    if (is.null(m) || is.null(m$cohorts)) return(s)
    for (cdata in m$cohorts) {
      if (!is.null(cdata$total_n)) s <- c(s, as.integer(cdata$total_n))
      if (!is.null(cdata$events)) s <- c(s, as.integer(cdata$events))
      if (!is.null(cdata$sites)) {
        for (sdata in cdata$sites) {
          if (!is.null(sdata$total_n)) s <- c(s, as.integer(sdata$total_n))
          if (!is.null(sdata$events)) s <- c(s, as.integer(sdata$events))
        }
      }
    }
    unique(s)
  }

  curr_nums <- extract_nums(manifest)
  prev_nums <- extract_nums(previous_manifest)

  matches <- gregexpr("\\b\\d+\\b", text)[[1]]
  if (matches[1] == -1) {
    return(data.frame(number = integer(), status = character(), source = character(), context = character(), stringsAsFactors = FALSE))
  }

  match_lens <- attr(matches, "match.length")
  records <- list()

  for (i in seq_along(matches)) {
    pos <- matches[i]
    len <- match_lens[i]
    val <- as.integer(substr(text, pos, pos + len - 1))

    start <- max(1, pos - 25)
    end <- min(nchar(text), pos + len + 25)
    context <- gsub("\n", " ", substr(text, start, end))

    if (val %in% curr_nums) {
      status <- "verified"
      source <- "current_manifest"
    } else if (val %in% prev_nums) {
      status <- "stale"
      source <- "previous_manifest"
    } else {
      status <- "unverified"
      source <- "none"
    }

    records[[length(records) + 1]] <- data.frame(
      number = val,
      status = status,
      source = source,
      context = context,
      stringsAsFactors = FALSE
    )
  }

  out <- do.call(rbind, records)
  out <- unique(out[, c("number", "status", "source", "context")])
  out <- out[order(out$status), ]
  rownames(out) <- NULL
  out
}

# -----------------------------------------------------------------------------
# 5. Vocabulary and Input Preflight Check
# -----------------------------------------------------------------------------

#' Vocabulary and Input Preflight Check
#'
#' Checks required vocabulary presence, ATC hierarchy descendant coverage, and unmapped concept 0 rates.
#'
#' @param con DuckDB connection.
#' @param required Vector of required vocabulary IDs (default RxNorm, ATC, LOINC, SNOMED).
#' @param max_unmapped_rate Maximum allowable share of concept 0 rows before flagging (default 0.50 = 50 percent).
#' @param fail_fast If TRUE, raises an error if any required vocabulary is missing.
#' @param vocab_schema Optional schema or attached database name for vocabulary tables.
#' @param vocab_db Alias for vocab_schema.
#' @return Named list with preflight metrics, drug exposure type breakdown, and passed status.
#' @export
vocabulary_preflight <- function(con, required = c("RxNorm", "ATC", "LOINC", "SNOMED"), max_unmapped_rate = 0.50, fail_fast = TRUE, vocab_schema = NULL, vocab_db = NULL) {
  v_schema <- if (!is.null(vocab_schema)) vocab_schema else vocab_db
  concept_tbl <- if (!is.null(v_schema)) sprintf("%s.concept", v_schema) else "concept"
  ca_tbl <- if (!is.null(v_schema)) sprintf("%s.concept_ancestor", v_schema) else "concept_ancestor"

  if (is.null(v_schema)) {
    has_c <- tryCatch(DBI::dbGetQuery(con, "SELECT 1 FROM concept LIMIT 1"), error = function(e) NULL)
    if (is.null(has_c)) {
      has_central <- tryCatch(DBI::dbGetQuery(con, "SELECT 1 FROM central_vocab.concept LIMIT 1"), error = function(e) NULL)
      if (!is.null(has_central)) {
        concept_tbl <- "central_vocab.concept"
        ca_tbl <- "central_vocab.concept_ancestor"
      }
    }
  }

  report <- list(
    passed = TRUE,
    vocabularies = list(),
    unmapped_shares = list(),
    atc_counts = list(),
    drug_types = list(
      prescribed = 0L, administered = 0L, dispensed = 0L, other = 0L, total = 0L,
      prescribed_38000177 = 0L, administered_38000180 = 0L, dispensed_38000175 = 0L
    ),
    drug_type_breakdown = list(),
    issues = character()
  )
  report$drug_type_breakdown <- report$drug_types

  for (v in required) {
    cnt <- tryCatch({
      DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM %s WHERE vocabulary_id = '%s'", concept_tbl, v))$n[1]
    }, error = function(e) 0)
    report$vocabularies[[v]] <- cnt
    if (cnt == 0) {
      report$passed <- FALSE
      report$issues <- c(report$issues, sprintf("Required vocabulary '%s' is completely empty (0 concepts).", v))
    }
  }

  atc_desc <- tryCatch({
    DBI::dbGetQuery(con, sprintf("
      SELECT count(DISTINCT ca.descendant_concept_id) AS n
      FROM %s ca
      JOIN %s c ON ca.ancestor_concept_id = c.concept_id
      WHERE c.vocabulary_id = 'ATC'
    ", ca_tbl, concept_tbl))$n[1]
  }, error = function(e) 0)
  report$atc_counts$total_descendants <- atc_desc
  if (atc_desc == 0 && "ATC" %in% required) {
    report$passed <- FALSE
    report$issues <- c(report$issues, "ATC concepts have zero descendants in concept_ancestor.")
  }

  # Drug type breakdown in drug_exposure
  tryCatch({
    has_de <- DBI::dbGetQuery(con, "
      SELECT count(*) AS n FROM information_schema.tables WHERE lower(table_name) = 'drug_exposure'
    ")$n[1] > 0
    if (has_de) {
      dt_stats <- DBI::dbGetQuery(con, "
        SELECT
          count(CASE WHEN drug_type_concept_id = 38000177 THEN 1 END) AS prescribed,
          count(CASE WHEN drug_type_concept_id = 38000180 THEN 1 END) AS administered,
          count(CASE WHEN drug_type_concept_id = 38000175 THEN 1 END) AS dispensed,
          count(CASE WHEN drug_type_concept_id NOT IN (38000177, 38000180, 38000175) OR drug_type_concept_id IS NULL THEN 1 END) AS other,
          count(*) AS total
        FROM drug_exposure
      ")
      if (nrow(dt_stats) > 0) {
        p <- as.integer(dt_stats$prescribed[1] %||% 0)
        a <- as.integer(dt_stats$administered[1] %||% 0)
        d <- as.integer(dt_stats$dispensed[1] %||% 0)
        o <- as.integer(dt_stats$other[1] %||% 0)
        tot <- as.integer(dt_stats$total[1] %||% 0)
        dt_list <- list(
          prescribed = p, administered = a, dispensed = d, other = o, total = tot,
          prescribed_38000177 = p, administered_38000180 = a, dispensed_38000175 = d,
          prescribed_pct = if (tot > 0) round(p * 100.0 / tot, 2) else 0.0,
          administered_pct = if (tot > 0) round(a * 100.0 / tot, 2) else 0.0,
          dispensed_pct = if (tot > 0) round(d * 100.0 / tot, 2) else 0.0
        )
        report$drug_types <- dt_list
        report$drug_type_breakdown <- dt_list
      }
    }
  }, error = function(e) NULL)

  report$concept_count <- tryCatch({
    as.integer(DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM %s", concept_tbl))$n[1])
  }, error = function(e) sum(unlist(report$vocabularies)))
  report$vocabulary_reachable <- tryCatch(report$concept_count > 0, error = function(e) FALSE)

  for (tbl_info in list(
    list(tbl = "drug_exposure", col = "drug_concept_id"),
    list(tbl = "condition_occurrence", col = "condition_concept_id"),
    list(tbl = "measurement", col = "measurement_concept_id")
  )) {
    tbl <- tbl_info$tbl
    col <- tbl_info$col
    tryCatch({
      has_tbl <- DBI::dbGetQuery(con, sprintf("
        SELECT count(*) AS n FROM information_schema.tables WHERE lower(table_name) = '%s'
      ", tbl))$n[1] > 0
      if (has_tbl) {
        stats <- DBI::dbGetQuery(con, sprintf("
          SELECT
            count(*) AS total,
            count(CASE WHEN %s = 0 OR %s IS NULL THEN 1 END) AS unmapped
          FROM %s
        ", col, col, tbl))
        tot <- stats$total[1]
        unmap <- stats$unmapped[1]
        rate <- if (tot > 0) unmap / tot else 0.0
        report$unmapped_shares[[tbl]] <- list(total_rows = tot, unmapped_rows = unmap, unmapped_rate = round(rate, 4))
        if (rate > max_unmapped_rate) {
          report$passed <- FALSE
          report$issues <- c(report$issues, sprintf("Table '%s' has unmapped rate of %.1f%% (> threshold %.1f%%).", tbl, rate * 100, max_unmapped_rate * 100))
        }
      }
    }, error = function(e) NULL)
  }

  if (fail_fast && !report$passed) {
    stop(paste("Vocabulary preflight check failed:\n-", paste(report$issues, collapse = "\n- ")))
  }

  report
}

# -----------------------------------------------------------------------------
# 6. Privacy-Preserving Cross-Site Patient Overlap
# -----------------------------------------------------------------------------

#' Generate Salted Site Patient Tokens
#'
#' Generates privacy-preserving SHA-256 tokens using a consortium salt without sharing patient IDs.
#'
#' @param patient_ids Vector of patient identifiers.
#' @param salt Consortium-agreed secret salt string.
#' @return Character vector of 16-character hexadecimal tokens.
#' @export
generate_site_patient_tokens <- function(patient_ids, salt) {
  # DuckDB vectorized hash calculation for pure zero-extra-dependency portability
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  df <- data.frame(pid = as.character(patient_ids), stringsAsFactors = FALSE)
  duckdb::duckdb_register(con, "t_pids", df)
  res <- DBI::dbGetQuery(con, sprintf("
    SELECT substr(sha256('%s' || pid), 1, 16) AS token FROM t_pids
  ", salt))$token
  duckdb::duckdb_unregister(con, "t_pids")
  res
}

#' Consortium Patient Overlap
#'
#' Calculates pairwise cross-hospital patient overlap with small-cell suppression (<11).
#'
#' @param site_tokens Named list of patient token vectors: list(SiteA = c(...), SiteB = c(...)).
#' @param min_cell_count Small-cell threshold (default 11).
#' @return Data frame of pairwise site overlap counts and percentages.
#' @export
consortium_patient_overlap <- function(site_tokens, min_cell_count = 11) {
  token_sets <- lapply(site_tokens, function(x) unique(as.character(x)))
  sites <- sort(names(token_sets))
  records <- list()

  for (i in seq_along(sites)) {
    s1 <- sites[i]
    set1 <- token_sets[[s1]]
    n1 <- length(set1)
    for (j in i:length(sites)) {
      s2 <- sites[j]
      set2 <- token_sets[[s2]]
      n2 <- length(set2)

      shared <- length(intersect(set1, set2))
      pct <- if (min(n1, n2) > 0) round(shared / min(n1, n2), 4) else 0.0

      suppressed <- (shared > 0 && shared < min_cell_count)
      display_count <- if (suppressed) sprintf("<%d", min_cell_count) else as.character(shared)

      records[[length(records) + 1]] <- data.frame(
        site_1 = s1,
        site_2 = s2,
        n_site_1 = n1,
        n_site_2 = n2,
        shared_patients = display_count,
        overlap_pct = pct,
        is_suppressed = suppressed,
        stringsAsFactors = FALSE
      )
    }
  }

  if (length(records) == 0) return(data.frame())
  do.call(rbind, records)
}

# -----------------------------------------------------------------------------
# 7. Planned-Readmission Filter (CMS Algorithm)
# -----------------------------------------------------------------------------

#' Planned-Readmission Filter
#'
#' Excludes planned readmissions based on CMS procedure categories (chemotherapy, transplants, elective procedures).
#'
#' @param con DuckDB connection.
#' @param cohort_table Name of table containing cohort index stays.
#' @param readmission_table Optional separate readmission table.
#' @param procedure_table Name of procedure table (default 'procedure_occurrence').
#' @param condition_table Name of condition table (default 'condition_occurrence').
#' @param planned_procedure_concepts Vector of concept IDs for planned procedures.
#' @param by Stratification column (default 'site').
#' @param target_table Optional clean table to create in DuckDB.
#' @return A list with \code{audit} (tally data frame) and \code{clean_cohort} (filtered data frame).
#' @export
planned_readmission_filter <- function(
    con,
    cohort_table,
    readmission_table = NULL,
    procedure_table = "procedure_occurrence",
    condition_table = "condition_occurrence",
    planned_procedure_concepts = NULL,
    by = "site",
    target_table = NULL
) {
  proc_concepts <- if (is.null(planned_procedure_concepts)) {
    c(4048386, 4048408, 4322471, 4141641, 4002621)
  } else {
    planned_procedure_concepts
  }
  proc_list <- paste(proc_concepts, collapse = ", ")

  query <- sprintf("
    WITH flagged_planned AS (
      SELECT
        c.*,
        CASE WHEN EXISTS (
          SELECT 1 FROM %s pr
          WHERE pr.person_id = c.person_id
            AND pr.procedure_concept_id IN (%s)
            AND pr.procedure_date >= c.cohort_end_date
            AND pr.procedure_date <= c.cohort_end_date + INTERVAL '30 days'
        ) THEN 1 ELSE 0 END AS is_planned
      FROM %s c
    )
    SELECT * FROM flagged_planned
  ", procedure_table, proc_list, cohort_table)

  df <- DBI::dbGetQuery(con, query)

  records <- list()
  strata <- list(list(site = "ALL", data = df))
  if (!is.null(by) && by %in% names(df)) {
    for (s_val in unique(df[[by]])) {
      strata[[length(strata) + 1]] <- list(site = as.character(s_val), data = df[df[[by]] == s_val, ])
    }
  }

  for (st in strata) {
    s_label <- st$site
    sub <- st$data
    tot <- nrow(sub)
    n_planned <- sum(sub$is_planned == 1)
    rate <- if (tot > 0) n_planned / tot else 0.0
    records[[length(records) + 1]] <- data.frame(
      site = s_label,
      total_readmissions = tot,
      planned_readmissions_removed = n_planned,
      unplanned_readmissions_retained = tot - n_planned,
      planned_share = round(rate, 4),
      stringsAsFactors = FALSE
    )
  }

  clean_df <- df[df$is_planned == 0, ]
  clean_df$is_planned <- NULL
  rownames(clean_df) <- NULL

  if (!is.null(target_table)) {
    duckdb::duckdb_register(con, "_tmp_clean_readm", clean_df)
    DBI::dbExecute(con, sprintf("CREATE OR REPLACE TABLE %s AS SELECT * FROM _tmp_clean_readm", target_table))
    duckdb::duckdb_unregister(con, "_tmp_clean_readm")
  }

  list(audit = do.call(rbind, records), clean_cohort = clean_df)
}

# -----------------------------------------------------------------------------
# 8. Observation Period Diagnostics
# -----------------------------------------------------------------------------

#' Observation Period Diagnostics
#'
#' Evaluates whether observation_period eligibility is native vs constructed,
#' and compares qualification against first-observed clinical events.
#'
#' @param con DuckDB connection.
#' @param cohort_table Name of cohort table.
#' @param prior_days Wash-in observation days required (default 365).
#' @param by Stratification column (default 'site').
#' @return Audit data frame comparing qualification rates.
#' @export
observation_period_diagnostics <- function(con, cohort_table = "cohort", prior_days = 365, by = "site") {
  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE %s", cohort_table))$column_name)
  has_site <- (!is.null(by) && tolower(by) %in% cols)

  site_sel <- if (has_site) sprintf("c.%s AS site,", by) else "'ALL' AS site,"

  sql <- sprintf("
    WITH first_events AS (
      SELECT person_id, MIN(event_date) AS first_event_date
      FROM (
        SELECT person_id, condition_start_date AS event_date FROM condition_occurrence
        UNION ALL
        SELECT person_id, drug_exposure_start_date AS event_date FROM drug_exposure
        UNION ALL
        SELECT person_id, measurement_date AS event_date FROM measurement
        UNION ALL
        SELECT person_id, visit_start_date AS event_date FROM visit_occurrence
      )
      GROUP BY person_id
    ),
    eval AS (
      SELECT
        %s
        c.person_id,
        c.cohort_start_date,
        op.observation_period_start_date,
        fe.first_event_date,
        CASE WHEN op.observation_period_start_date <= c.cohort_start_date - %d THEN 1 ELSE 0 END AS op_qualifies,
        CASE WHEN fe.first_event_date <= c.cohort_start_date - %d THEN 1 ELSE 0 END AS fe_qualifies
      FROM %s c
      LEFT JOIN observation_period op
        ON c.person_id = op.person_id
       AND c.cohort_start_date >= op.observation_period_start_date
       AND c.cohort_start_date <= op.observation_period_end_date
      LEFT JOIN first_events fe
        ON c.person_id = fe.person_id
    )
    SELECT
      site,
      count(*) AS total_patients,
      count(CASE WHEN op_qualifies = 1 THEN 1 END) AS n_qualify_obs_period,
      round(count(CASE WHEN op_qualifies = 1 THEN 1 END) * 1.0 / count(*), 4) AS rate_obs_period,
      count(CASE WHEN fe_qualifies = 1 THEN 1 END) AS n_qualify_first_event,
      round(count(CASE WHEN fe_qualifies = 1 THEN 1 END) * 1.0 / count(*), 4) AS rate_first_event,
      count(CASE WHEN op_qualifies = fe_qualifies THEN 1 END) AS concordant_count,
      round(count(CASE WHEN op_qualifies = fe_qualifies THEN 1 END) * 1.0 / count(*), 4) AS concordance_rate
    FROM eval
    GROUP BY site
  ", site_sel, prior_days, prior_days, cohort_table)

  df <- DBI::dbGetQuery(con, sql)

  if (has_site && nrow(df) > 1) {
    tot_row <- data.frame(
      site = "ALL",
      total_patients = sum(df$total_patients),
      n_qualify_obs_period = sum(df$n_qualify_obs_period),
      rate_obs_period = round(sum(df$n_qualify_obs_period) / sum(df$total_patients), 4),
      n_qualify_first_event = sum(df$n_qualify_first_event),
      rate_first_event = round(sum(df$n_qualify_first_event) / sum(df$total_patients), 4),
      concordant_count = sum(df$concordant_count),
      concordance_rate = round(sum(df$concordant_count) / sum(df$total_patients), 4),
      stringsAsFactors = FALSE
    )
    df <- rbind(tot_row, df)
  }

  df
}
