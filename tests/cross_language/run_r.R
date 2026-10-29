# Runs the R side of tests/test_cross_language.py: the same fixtures, the same inputs, results written as JSON.
#   Rscript --vanilla tests/cross_language/run_r.R <work dir>
args <- commandArgs(trailingOnly = TRUE)
work <- args[[1]]
suppressMessages({
  if (requireNamespace("devtools", quietly = TRUE) && file.exists("DESCRIPTION")) devtools::load_all(".", quiet = TRUE) else library(omopduckdb)
})
`%||%` <- function(a, b) if (is.null(a)) b else a
ns <- asNamespace("omopduckdb")
inp <- jsonlite::fromJSON(file.path(work, "inputs.json"), simplifyVector = FALSE)
connect <- function(name) DBI::dbConnect(duckdb::duckdb(), dbdir = file.path(work, "r_copy", name))
out <- list()

# 1. native Circe compiler on each definition
con <- connect("cons.duckdb")
out$circe <- lapply(inp$definitions, function(d) {
  r <- DBI::dbGetQuery(con, suppressWarnings(compile_circe_to_duckdb(d, cdm_schema = "main")))
  sort(sprintf("%d@%s", r$subject_id, format(as.Date(r$cohort_start_date))))
})
DBI::dbDisconnect(con, shutdown = TRUE)

# 2. DQD catalog
con <- connect("dqd.duckdb")
res <- run_dqd_catalog(con, today = as.Date(inp$today))
out$dqd <- lapply(res$CheckResults, function(r) list(name = r$check_name, table = r$cdm_table_name, field = r$cdm_field_name %||% "",
                                                     violated = r$num_violated_rows, denominator = r$num_denominator_rows, status = r$execution_status))
DBI::dbDisconnect(con, shutdown = TRUE)

# 3. reconcile / explain gaps, and the custom concept registry
con <- connect("target.duckdb")
ref <- data.frame(person_source_value = unlist(inp$reference))
rec <- reconcile_cohort(con, ref, definition = inp$target_definition)
out$reconcile_counts <- rec$counts
out$reconcile_gaps <- lapply(seq_len(nrow(rec$gaps)), function(i) list(reason = rec$gaps$reason[[i]], detail = rec$gaps$detail[[i]], n = rec$gaps$n[[i]]))
feats <- data.frame(person_id = c(1, 2), date = as.Date("2020-01-01"), elixhauser_site = c(1, 2))
write_custom_features(con, feats)
feats2 <- data.frame(person_id = c(1, 2), date = as.Date("2020-01-01"), charlson_site = c(3, 4), frailty = c(0.1, 0.2))
ids <- write_custom_features(con, feats2)
out$concept_ids <- lapply(ids, function(v) as.numeric(v[[1]]))
spec <- stamp_feature_spec_concepts(list(features = list(list(name = "charlson_site"), list(name = "age"))), con)
out$stamped <- lapply(spec$features, function(f) f$concept_id %||% NA)
stale <- list(features = list(list(name = "charlson_site", concept_id = 2001000000), list(name = "frailty", concept_id = 2001000099), list(name = "nothere", concept_id = 5e9)))
out$bundle_check <- as.list(validate_bundle_concepts(stale, con, error = FALSE)$status)
DBI::dbDisconnect(con, shutdown = TRUE)

# 4. metrics and the default model on fixed numbers
y <- unlist(inp$y); p <- unlist(inp$p)
out$metrics <- list(auroc = ns$.m_auroc(y, p), auprc = ns$.m_auprc(y, p), brier = ns$.m_brier(y, p))
out$weighted <- ns$.weighted_metrics(ns$.weighted_prepare(y, unlist(inp$p_tied)), unlist(inp$weights))
x <- do.call(rbind, lapply(inp$x, unlist))
cut <- as.integer(inp$cut)
out$ridge <- as.list(ridge_logistic_fit_predict(x[seq_len(cut), ], unlist(inp$y_train), x[(cut + 1):nrow(x), ]))

# 5. monitoring and risk tiers on fixed numbers
base <- unlist(inp$baseline)
th <- fit_capacity_thresholds(base, list(list(name = "high", top_fraction = 0.10), list(name = "elevated", top_fraction = 0.30), list(name = "standard")))
cal <- calibration_summary(y, p, min_events = 5)
out$monitoring <- list(psi = population_stability_index(base, p), psi_tied = population_stability_index(base, unlist(inp$p_tied)),
                       calibration = lapply(cal, function(v) if (is.na(v)) NULL else v),
                       thresholds = lapply(th, function(t) list(t$name, t$min)), tiers = as.list(map_risk_tiers(p, th)))

jsonlite::write_json(out, file.path(work, "r_results.json"), auto_unbox = TRUE, digits = NA, na = "null", null = "null")
