#' Sparse concept matrix for ML / PLP-style models
#'
#' Builds a high-dimensional sparse matrix of condition / drug / procedure concept counts inside a
#' per-row lookback window, driven by a cohort parquet (one row per index event). It is the R
#' counterpart of Python `omop_etl.extract_sparse_concept_matrix()` and follows the same rules, so the
#' two return the same columns in the same order for the same data.
#'
#' The whole cohort is processed in a single set-based DuckDB query. By default a record is used only if
#' it is dated strictly before the row's index date (`[index - lookback_days, index)`), so nothing at or
#' after the index leaks into the features. Concept id 0 ("No matching concept") is always dropped.
#'
#' Column `j` of `X` is the `j`-th token of `concepts$token`. Tokens use omop-learn's convention
#' (`"<concept_id> - <domain> - <concept_name>"`) and are sorted in C-locale order, which is what
#' omop-learn's `ConceptTokenizer` does, so R and Python agree regardless of the R session's locale.
#'
#' @param con Active DBI connection to DuckDB holding the OMOP CDM tables (read-only is fine). If the
#'   vocabulary lives in an attached `central_vocab` catalog, concept names are picked up automatically.
#' @param features_parquet Path (or glob) of the cohort parquet, one row per index event, with a person id
#'   and an index date column; an outcome column is carried through if present. Columns are auto-detected
#'   (`person_id`/`subject_id`/`patid`; `index_date`/`cohort_start_date`/`end_date`/`admit_date`...;
#'   `y`/`outcome_flag`/`label`) or set with `person_col`, `index_date_col`, `outcome_col`.
#' @param lookback_days Window length in days, or `NULL` for all prior history.
#' @param min_patient_freq Keep a concept only if at least this many distinct patients have it in their
#'   window (computed on the cohort passed in). To apply a train-fitted vocabulary to a test cohort pass
#'   the train result's `concepts$token` as `tokens` instead.
#' @param domains Any of `"condition"`, `"drug"`, `"procedure"`.
#' @param value `"count"` (records per concept) or `"binary"` (presence).
#' @param tokens Optional character vector of tokens from a previous call. Fixes the columns and their
#'   order; `min_patient_freq` is then not applied and concepts outside it are dropped.
#' @param include_index_date Also use events dated exactly on the index date (default `FALSE`).
#' @param person_col,index_date_col,outcome_col Optional explicit column names in the parquet.
#' @param concept_table Concept table to read names from (default: `concept`, then `central_vocab`).
#' @param anchor_date Strategy for selecting index date column from cohort parquet:
#'   `"auto"` (default), `"admit_date"` (prioritizes admission date to prevent in-hospital leakage),
#'   or `"discharge_date"` (prioritizes discharge date).
#' @param washin_buffer_days Post-anchor observation buffer days to include in the window (default 0).
#' @param washin_buffer_hours Post-anchor observation buffer hours (e.g. 24 for 24h post-admission, default 0).
#' @return A list with `X` (a `Matrix::dgCMatrix`, rows aligned to the parquet), `concepts` (one row per
#'   column of `X`: `column_index` (1-based), `token`, `feature` (syntactic name `<domain>_<concept_id>` used by
#'   [as_tidymodels_data()]), `domain`, `concept_id`, `concept_name`, `domain_id`,
#'   `vocabulary_id`, `standard_concept`, `n_patients`), `cohort` (`person_id`, `index_date`, optional
#'   `y`), `y` (or `NULL`) and `params`.
#' @examples
#' \dontrun{
#' con <- DBI::dbConnect(duckdb::duckdb(), "omop_Temple.duckdb", read_only = TRUE)
#' DBI::dbExecute(con, "ATTACH 'central_vocabulary.duckdb' AS central_vocab (READ_ONLY);")
#' res <- extract_sparse_concept_matrix(con, "ederri_features_Temple.parquet", min_patient_freq = 50)
#' dim(res$X)
#' }
#' @export
extract_sparse_concept_matrix <- function(con,
                                          features_parquet,
                                          lookback_days = 365,
                                          min_patient_freq = 1,
                                          domains = c("condition", "drug"),
                                          value = "count",
                                          tokens = NULL,
                                          include_index_date = FALSE,
                                          person_col = NULL,
                                          index_date_col = NULL,
                                          outcome_col = NULL,
                                          concept_table = NULL,
                                          anchor_date = c("auto", "admit_date", "discharge_date"),
                                          washin_buffer_days = 0,
                                          washin_buffer_hours = 0) {
  if (!requireNamespace("Matrix", quietly = TRUE)) {
    stop("extract_sparse_concept_matrix() needs the 'Matrix' package.", call. = FALSE)
  }
  if (!value %in% c("count", "binary")) stop("value must be 'count' or 'binary'", call. = FALSE)
  domains <- .mlf_validate_options(domains, lookback_days, min_patient_freq)

  anchor <- if (is.character(anchor_date)) anchor_date[1] else "auto"
  buffer_days <- as.integer(washin_buffer_days) + as.integer(washin_buffer_hours %/% 24)
  cohort <- .mlf_load_cohort(con, features_parquet, person_col, index_date_col, outcome_col, anchor_date = anchor)

  pairs <- NULL
  if (!is.null(tokens)) {
    parsed <- .mlf_parse_tokens(tokens)
    keep <- !is.na(parsed$concept_id) & parsed$domain %in% domains
    pairs <- parsed[keep, c("domain", "concept_id")]
  }
  sql <- paste(
    .mlf_events_ctes(cohort$sql, domains, lookback_days, include_index_date, min_patient_freq, pairs, buffer_days = buffer_days),
    "SELECT row_idx, domain, concept_id, COUNT(*) AS n FROM ev GROUP BY row_idx, domain, concept_id"
  )
  triples <- DBI::dbGetQuery(con, sql)
  triples$row_idx <- as.numeric(triples$row_idx)
  triples$concept_id <- as.numeric(triples$concept_id)
  triples$n <- as.numeric(triples$n)

  vocab <- .mlf_assemble_vocab(con, triples, cohort$df$person_id, tokens, concept_table)

  n_rows <- nrow(cohort$df)
  n_cols <- nrow(vocab)
  if (nrow(triples) > 0 && n_cols > 0) {
    col <- match(.mlf_pair_key(triples$domain, triples$concept_id), .mlf_pair_key(vocab$domain, vocab$concept_id))
    ok <- !is.na(col)
    X <- Matrix::sparseMatrix(
      i = as.integer(triples$row_idx[ok]) + 1L,
      j = col[ok],
      x = if (value == "count") triples$n[ok] else rep(1, sum(ok)),
      dims = c(n_rows, n_cols)
    )
  } else {
    X <- Matrix::sparseMatrix(i = integer(0), j = integer(0), x = numeric(0), dims = c(n_rows, n_cols))
  }
  colnames(X) <- vocab$token

  cohort_out <- cohort$df[, setdiff(names(cohort$df), "row_idx"), drop = FALSE]
  list(
    X = X,
    concepts = vocab,
    cohort = cohort_out,
    y = if ("y" %in% names(cohort_out)) cohort_out$y else NULL,
    params = list(
      lookback_days = lookback_days, min_patient_freq = min_patient_freq, domains = domains,
      value = value, include_index_date = include_index_date,
      anchor_date = anchor, washin_buffer_days = washin_buffer_days,
      washin_buffer_hours = washin_buffer_hours
    )
  )
}

# ---------------------------------------------------------------------------------------------
# Internals. Self-contained on purpose (base R + DBI + Matrix, qualified calls) so this file also
# works when sourced on its own: source("R/ml_features.R").
# ---------------------------------------------------------------------------------------------

.mlf_domains <- list(
  condition = list(table = "condition_occurrence", concept = "condition_concept_id", date = "condition_start_date"),
  drug = list(table = "drug_exposure", concept = "drug_concept_id", date = "drug_exposure_start_date"),
  procedure = list(table = "procedure_occurrence", concept = "procedure_concept_id", date = "procedure_date")
)
.mlf_person_aliases <- c("person_id", "subject_id", "patid", "patient_id")
.mlf_index_aliases <- c("index_date", "cohort_start_date", "end_date", "admit_date", "admission_date",
                        "admit_dt", "visit_start_date")
.mlf_admit_aliases <- c("admit_date", "admission_date", "cohort_start_date", "visit_start_date",
                        "admit_dt", "start_date", "index_date")
.mlf_discharge_aliases <- c("discharge_date", "disch_date", "cohort_end_date", "visit_end_date",
                            "end_date", "discharged_date", "index_date")
.mlf_outcome_aliases <- c("y", "outcome_flag", "label", "outcome")

.mlf_ident <- function(x) paste0('"', gsub('"', '""', x, fixed = TRUE), '"')
.mlf_quote_path <- function(p) gsub("'", "''", gsub("\\", "/", as.character(p), fixed = TRUE), fixed = TRUE)
.mlf_feature_name <- function(domain, concept_id) {
  if (length(concept_id) == 0) return(character(0))
  paste0(domain, "_", sprintf("%.0f", as.numeric(concept_id)))
}
.mlf_pair_key <- function(domain, concept_id) paste(domain, sprintf("%.0f", as.numeric(concept_id)), sep = "|")

# Code-point (C locale) ordering, matching Python's sorted() and omop-learn's ConceptTokenizer.
.mlf_sort_tokens <- function(x) x[order(x, method = "radix")]

.mlf_make_token <- function(concept_id, domain, concept_name) {
  if (length(concept_id) == 0) return(character(0))   # paste0() would otherwise recycle the " - " literal
  name <- trimws(ifelse(is.na(concept_name), "", concept_name))
  paste0(sprintf("%.0f", as.numeric(concept_id)), " - ", domain, ifelse(nzchar(name), paste0(" - ", name), ""))
}

.mlf_parse_tokens <- function(tokens) {
  parts <- strsplit(tokens, " - ", fixed = TRUE)
  domain <- vapply(parts, function(p) if (length(p) >= 2) p[2] else NA_character_, character(1))
  id <- suppressWarnings(vapply(parts, function(p) as.numeric(p[1]), numeric(1)))
  domain[!domain %in% names(.mlf_domains)] <- NA_character_
  id[is.na(domain)] <- NA_real_
  data.frame(token = tokens, domain = domain, concept_id = id, stringsAsFactors = FALSE)
}

.mlf_resolve_column <- function(cols, explicit, aliases, what, param, required = TRUE) {
  lower <- tolower(cols)
  if (!is.null(explicit)) {
    i <- match(tolower(explicit), lower)
    if (is.na(i)) {
      stop(sprintf("%s column '%s' not found in cohort parquet. Available columns: %s",
                   what, explicit, paste(cols, collapse = ", ")), call. = FALSE)
    }
    return(cols[i])
  }
  for (a in aliases) {
    i <- match(a, lower)
    if (!is.na(i)) return(cols[i])
  }
  if (required) {
    stop(sprintf("Could not find a %s column in cohort parquet (looked for %s). Available columns: %s. Pass %s=... explicitly.",
                 what, paste(aliases, collapse = ", "), paste(cols, collapse = ", "), param), call. = FALSE)
  }
  NULL
}

.mlf_validate_options <- function(domains, lookback_days, min_patient_freq) {
  unknown <- setdiff(domains, names(.mlf_domains))
  if (length(domains) == 0 || length(unknown) > 0) {
    stop(sprintf("domains must be a non-empty subset of %s; got %s",
                 paste(names(.mlf_domains), collapse = ", "), paste(domains, collapse = ", ")), call. = FALSE)
  }
  if (!is.null(lookback_days) && lookback_days < 0) stop("lookback_days must be >= 0 or NULL (unbounded)", call. = FALSE)
  if (min_patient_freq < 1) stop("min_patient_freq must be >= 1", call. = FALSE)
  domains
}

# Reads the cohort parquet in file order. `sql` assigns row_idx from file_row_number so row i of every
# output is row i of the parquet (multi-file globs are ordered by filename, then row).
.mlf_load_cohort <- function(con, features_parquet, person_col, index_date_col, outcome_col,
                             anchor_date = "auto") {
  src <- sprintf("read_parquet('%s', file_row_number = true, filename = true)", .mlf_quote_path(features_parquet))
  cols <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", src))$column_name
  p <- .mlf_resolve_column(cols, person_col, .mlf_person_aliases, "person id", "person_col")

  anchor <- tolower(trimws(if (is.null(anchor_date)) "auto" else anchor_date[1]))
  aliases <- if (anchor %in% c("admit_date", "admit", "admission", "admission_date", "cohort_start_date")) {
    .mlf_admit_aliases
  } else if (anchor %in% c("discharge_date", "discharge", "disch_date", "cohort_end_date")) {
    .mlf_discharge_aliases
  } else {
    .mlf_index_aliases
  }

  d <- .mlf_resolve_column(cols, index_date_col, aliases, "index date", "index_date_col")
  y <- .mlf_resolve_column(cols, outcome_col, .mlf_outcome_aliases, "outcome", "outcome_col", required = FALSE)

  y_sql <- if (!is.null(y)) sprintf(", %s AS y", .mlf_ident(y)) else ""
  sql <- sprintf(paste0(
    "SELECT CAST(ROW_NUMBER() OVER (ORDER BY filename, file_row_number) - 1 AS BIGINT) AS row_idx, ",
    "TRY_CAST(%s AS BIGINT) AS person_id, TRY_CAST(%s AS DATE) AS index_date%s FROM %s"),
    .mlf_ident(p), .mlf_ident(d), y_sql, src)
  df <- DBI::dbGetQuery(con, paste(sql, "ORDER BY row_idx"))
  bad_person <- sum(is.na(df$person_id))
  bad_date <- sum(is.na(df$index_date))
  if (bad_person > 0 || bad_date > 0) {
    stop(sprintf(paste0("Cohort parquet has %d rows with a missing/non-integer person id ('%s') and %d rows with a ",
                        "missing/unparseable index date ('%s'). Rows are aligned to the parquet by position, so they ",
                        "cannot be silently dropped; clean or filter them first."), bad_person, p, bad_date, d),
         call. = FALSE)
  }
  df$row_idx <- as.numeric(df$row_idx)
  df$person_id <- as.numeric(df$person_id)
  df$index_date <- as.Date(df$index_date)
  list(sql = sql, df = df)
}

# CTE chain cohort -> ev0 -> ev: windowed, vocabulary-filtered events for every cohort row.
.mlf_events_ctes <- function(cohort_sql, domains, lookback_days, include_index_date, min_patient_freq, pairs,
                             buffer_days = 0) {
  unions <- paste(vapply(domains, function(dm) {
    t <- .mlf_domains[[dm]]
    sprintf("SELECT person_id, %s AS concept_id, %s AS event_date, visit_occurrence_id, '%s' AS domain FROM %s",
            t$concept, t$date, dm, t$table)
  }, character(1)), collapse = " UNION ALL ")
  lower <- if (!is.null(lookback_days)) sprintf("AND e.event_date >= c.index_date - %d ", as.integer(lookback_days)) else ""
  upper <- if (isTRUE(include_index_date) || buffer_days > 0) "<=" else "<"
  date_offset <- if (buffer_days > 0) sprintf(" + %d", as.integer(buffer_days)) else ""

  vocab <- ""
  if (!is.null(pairs)) {
    values <- if (nrow(pairs) > 0) {
      sprintf("SELECT * FROM (VALUES %s) AS t(domain, concept_id)",
              paste(sprintf("('%s', %s)", pairs$domain, sprintf("%.0f", pairs$concept_id)), collapse = ", "))
    } else {
      "SELECT CAST(NULL AS VARCHAR) AS domain, CAST(NULL AS BIGINT) AS concept_id WHERE FALSE"
    }
    vocab <- sprintf("JOIN (%s) v ON ev0.domain = v.domain AND ev0.concept_id = v.concept_id", values)
  } else if (min_patient_freq > 1) {
    vocab <- sprintf(paste0("JOIN (SELECT domain, concept_id FROM ev0 GROUP BY domain, concept_id ",
                            "HAVING COUNT(DISTINCT person_id) >= %d) v ",
                            "ON ev0.domain = v.domain AND ev0.concept_id = v.concept_id"), as.integer(min_patient_freq))
  }
  sprintf(paste0(
    "WITH cohort AS (%s), ev_all AS (%s), ",
    "ev0 AS (SELECT c.row_idx, c.person_id, c.index_date, e.domain, e.concept_id, e.event_date, ",
    "e.visit_occurrence_id FROM cohort c JOIN ev_all e ON e.person_id = c.person_id ",
    "%sAND e.event_date %s c.index_date%s ",
    "WHERE e.concept_id IS NOT NULL AND e.concept_id <> 0), ",
    "ev AS (SELECT ev0.* FROM ev0 %s)"),
    cohort_sql, unions, lower, upper, date_offset, vocab)
}

# Best-effort concept_id -> name/domain/vocabulary. Tries `concept`, then the `central_vocab` catalog,
# filling only ids still missing so a stub local `concept` table does not mask a populated central one.
.mlf_concept_metadata <- function(con, concept_ids, concept_table = NULL) {
  wanted <- sort(unique(as.numeric(concept_ids[!is.na(concept_ids)])))
  found <- NULL
  if (length(wanted) == 0) return(found)
  if (!is.null(concept_table) && !grepl("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){0,2}$", concept_table)) {
    stop(sprintf("Invalid concept_table identifier: %s", concept_table), call. = FALSE)
  }
  candidates <- if (!is.null(concept_table)) concept_table else c("concept", "central_vocab.concept", "central_vocab.main.concept")
  for (cand in candidates) {
    missing_ids <- if (is.null(found)) wanted else setdiff(wanted, found$concept_id)
    if (length(missing_ids) == 0) break
    rows <- tryCatch(
      DBI::dbGetQuery(con, sprintf(
        "SELECT concept_id, concept_name, domain_id, vocabulary_id, standard_concept FROM %s WHERE concept_id IN (%s)",
        cand, paste(sprintf("%.0f", missing_ids), collapse = ", "))),
      error = function(e) NULL)
    if (!is.null(rows) && nrow(rows) > 0) {
      rows$concept_id <- as.numeric(rows$concept_id)
      found <- rbind(found, rows)
    }
  }
  found
}

.mlf_assemble_vocab <- function(con, triples, person_of_row, tokens, concept_table) {
  # distinct persons per (domain, concept_id)
  if (nrow(triples) > 0) {
    person <- person_of_row[triples$row_idx + 1]
    key <- .mlf_pair_key(triples$domain, triples$concept_id)
    uniq <- !duplicated(data.frame(key = key, person = person))
    counts_tab <- table(key[uniq])
    pairs <- triples[!duplicated(key), c("domain", "concept_id")]
    pairs$n_patients <- as.numeric(counts_tab[.mlf_pair_key(pairs$domain, pairs$concept_id)])
  } else {
    pairs <- data.frame(domain = character(0), concept_id = numeric(0), n_patients = numeric(0),
                        stringsAsFactors = FALSE)
  }

  if (is.null(tokens)) {
    meta <- .mlf_concept_metadata(con, pairs$concept_id, concept_table)
    nm <- if (!is.null(meta) && nrow(pairs) > 0) meta$concept_name[match(pairs$concept_id, meta$concept_id)] else rep(NA_character_, nrow(pairs))
    pairs$token <- .mlf_make_token(pairs$concept_id, pairs$domain, nm)
    pairs <- pairs[order(pairs$token, method = "radix"), , drop = FALSE]
    vocab <- data.frame(token = pairs$token, domain = pairs$domain, concept_id = pairs$concept_id,
                        n_patients = pairs$n_patients, stringsAsFactors = FALSE)
  } else {
    base <- .mlf_parse_tokens(tokens)
    idx <- match(.mlf_pair_key(base$domain, base$concept_id), .mlf_pair_key(pairs$domain, pairs$concept_id))
    idx[is.na(base$domain)] <- NA_integer_
    vocab <- data.frame(token = base$token, domain = base$domain, concept_id = base$concept_id,
                        n_patients = ifelse(is.na(idx), 0, pairs$n_patients[idx]), stringsAsFactors = FALSE)
  }

  meta <- .mlf_concept_metadata(con, vocab$concept_id, concept_table)
  m <- if (!is.null(meta)) match(vocab$concept_id, meta$concept_id) else rep(NA_integer_, nrow(vocab))
  pick <- function(col) if (!is.null(meta)) meta[[col]][m] else rep(NA_character_, nrow(vocab))
  vocab$concept_name <- pick("concept_name")
  vocab$domain_id <- pick("domain_id")
  vocab$vocabulary_id <- pick("vocabulary_id")
  vocab$standard_concept <- pick("standard_concept")
  vocab$column_index <- seq_len(nrow(vocab))
  vocab$feature <- .mlf_feature_name(vocab$domain, vocab$concept_id)
  vocab[, c("column_index", "token", "feature", "domain", "concept_id", "concept_name", "domain_id", "vocabulary_id",
            "standard_concept", "n_patients")]
}
