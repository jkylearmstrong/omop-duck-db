# N9-N14 of the portable-cohort / all-stays requests (twin of tests/test_cohort_requests_n9_n14.py).

repeated_stays <- function(seed = 0, patients = 120, per = 4) {
  set.seed(seed)
  n <- patients * per
  pid <- rep(seq_len(patients), each = per)
  frailty <- rep(stats::rnorm(patients), each = per)
  sbp <- 120 + 8 * frailty + stats::rnorm(n, 0, 12)  # patient-level component, so clustering matters
  eta <- -1 + 0.04 * (sbp - 120) + 0.9 * frailty
  y <- as.integer(stats::runif(n) < 1 / (1 + exp(-eta)))
  data.frame(pid = pid, sbp = sbp, male = sample(0:1, n, replace = TRUE), y = y)
}

test_that("N9: concept_ancestor is found in any attached catalog", {
  vocab <- tempfile(fileext = ".duckdb")
  v <- DBI::dbConnect(duckdb::duckdb(), vocab)
  DBI::dbExecute(v, "CREATE TABLE concept_ancestor AS SELECT 1 AS ancestor_concept_id, 2 AS descendant_concept_id")
  DBI::dbDisconnect(v, shutdown = TRUE)
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  r <- omopduckdb:::.comorb_resolve_concept_ancestor(con)
  expect_null(r$ref); expect_match(r$reason, "attached catalog")
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS my_site_vocab (READ_ONLY)", gsub("\\\\", "/", vocab)))
  r <- omopduckdb:::.comorb_resolve_concept_ancestor(con)
  expect_match(r$ref, "my_site_vocab")
  expect_equal(DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM %s", r$ref))$n, 1)
})

test_that("N10: features can be keyed by stay", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, "CREATE TABLE person AS SELECT 1 AS person_id, 1960 AS year_of_birth, NULL::INT AS month_of_birth,
    NULL::INT AS day_of_birth, 8507 AS gender_concept_id, 0 AS race_concept_id, 0 AS ethnicity_concept_id")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (1, 1, DATE '2020-01-05'),
    (2, 1, DATE '2020-03-05')) t(condition_occurrence_id, person_id, condition_start_date)")
  DBI::dbExecute(con, "CREATE TABLE stays AS SELECT * FROM (VALUES
    (1, DATE '2020-01-10', DATE '2020-01-12', 101), (1, DATE '2020-03-10', DATE '2020-03-12', 102))
    t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id)")
  df <- extract_patient_features(con, cohort_table = "stays", index_key = "visit_occurrence_id",
                                 include_drugs = FALSE, include_procedures = FALSE, include_measurements = FALSE,
                                 lookback_days = 30)
  expect_equal(df$visit_occurrence_id, c(101, 102))
  expect_equal(df$condition_count_30d, c(1, 1))
  DBI::dbExecute(con, "CREATE TABLE plain AS SELECT subject_id, cohort_start_date, cohort_end_date FROM stays")
  expect_error(extract_patient_features(con, cohort_table = "plain", index_key = "visit_occurrence_id"),
               "visit_occurrence_id")
  expect_error(extract_patient_features(con, cohort_table = "stays", index_key = "nonsense"), "index_key")
})

test_that("N11: cross-site duplicates are counted without exposing identifiers", {
  ids <- list(A = paste0("p", 0:39), B = paste0("p", 20:59), C = c("z1", "z2"))
  out <- consortium_cross_site_duplicates(ids, key = "secret", min_cell_count = 11)
  expect_equal(out$shared_patients[out$site_1 == "A" & out$site_2 == "B"], "20")
  expect_equal(out$shared_patients[out$site_1 == "A" & out$site_2 == "C"], "0")
  total <- out[out$site_1 == "ALL", ]
  expect_equal(total$n_site_1, 62); expect_equal(total$shared_patients, "20")
  small <- consortium_cross_site_duplicates(list(A = c("x", "y"), B = "y"), key = "k")
  expect_equal(small$shared_patients[small$site_1 == "A"], "<11")
  expect_error(consortium_cross_site_duplicates(ids, key = ""))
  # HMAC-SHA256 test vector (RFC 4231, case 2): key "Jefe", data "what do ya want for nothing?"
  tok <- omopduckdb:::.hmac_sha256_hex(c("what do ya want for nothing?"), "Jefe")
  expect_equal(tok, "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")
})

test_that("N12: cluster-robust errors widen with repeated stays and match for one row per cluster", {
  df <- repeated_stays()
  naive <- consortium_multivariable_risk_factors(df[, c("sbp", "male")], df$y, regularize = FALSE)
  clus <- consortium_multivariable_risk_factors(df[, c("pid", "sbp", "male")], df$y, regularize = FALSE, cluster_col = "pid")
  expect_equal(clus$metadata$cov_type, "cluster"); expect_equal(clus$metadata$n_clusters, 120)
  expect_false("pid" %in% clus$table$variable)
  expect_true(any(clus$standard_errors > naive$standard_errors))
  expect_equal(unname(clus$coefficients), unname(naive$coefficients))
  one <- df[!duplicated(df$pid), ]
  a <- consortium_multivariable_risk_factors(one[, c("sbp", "male")], one$y, regularize = FALSE, cluster_col = one$pid)
  b <- consortium_multivariable_risk_factors(one[, c("sbp", "male")], one$y, regularize = FALSE)
  expect_equal(unname(a$standard_errors), unname(b$standard_errors), tolerance = 0.25)
  expect_error(consortium_multivariable_risk_factors(df[, c("sbp", "male")], df$y, cluster_col = "missing"), "cluster_col")
  base <- consortium_multivariable_risk_factors(df[, c("sbp", "male")], df$y)
  expect_equal(base$metadata$cov_type, "model"); expect_false("std_beta" %in% names(base$table))
})

test_that("N13: odds ratio per increment and standardized beta", {
  df <- repeated_stays()
  res <- consortium_multivariable_risk_factors(df[, c("sbp", "male")], df$y, regularize = FALSE,
                                               increments = list(sbp = list(10, "per 10 mmHg")))
  row <- res$table[res$table$variable == "sbp", ]
  expect_equal(row$increment, "per 10 mmHg")
  expect_equal(row$or_increment, exp(row$beta / stats::sd(df$sbp) * 10))
  expect_equal(row$std_beta, row$beta)
  expect_true(row$or_increment_ci_95_lower < row$or_increment && row$or_increment < row$or_increment_ci_95_upper)
  expect_true(is.na(res$table$or_increment[res$table$variable == "male"]))
  raw <- consortium_multivariable_risk_factors(df[, "sbp", drop = FALSE], df$y, regularize = FALSE,
                                               standardize_continuous = FALSE,
                                               increments = list(sbp = list(10, "per 10 mmHg")))$table
  expect_equal(raw$or_increment, row$or_increment, tolerance = 0.02)
  expect_equal(raw$std_beta, row$std_beta, tolerance = 0.02)
  expect_error(consortium_multivariable_risk_factors(df[, "sbp", drop = FALSE], df$y,
                                                     increments = list(dbp = list(5, "x"))), "unknown feature")
})

test_that("N14: group_col gives patient-grouped internal performance", {
  frames <- lapply(c("A", "B"), function(site) {
    d <- repeated_stays(seed = utf8ToInt(site), patients = 80, per = 4)
    d$site <- site; d$subject_id <- paste0(site, d$pid); d$noise <- stats::rnorm(nrow(d)); d
  })
  data <- do.call(rbind, frames)
  plain <- consortium_cross_site_validation(data, c("sbp", "noise"), "y")
  grouped <- consortium_cross_site_validation(data, c("sbp", "noise"), "y", group_col = "subject_id")
  off <- row(as.matrix(plain$auroc_matrix)) != col(as.matrix(plain$auroc_matrix))
  expect_equal(as.matrix(grouped$auroc_matrix)[off], as.matrix(plain$auroc_matrix)[off])
  expect_true(all(is.finite(as.matrix(grouped$auroc_matrix))))
  again <- consortium_cross_site_validation(data, c("sbp", "noise"), "y", group_col = "subject_id")
  expect_equal(grouped$auroc_matrix, again$auroc_matrix)
  expect_error(consortium_cross_site_validation(data, c("sbp", "noise"), "y", group_col = "nope"), "group_col")
  groups <- rep(1:30, each = 3); y <- as.numeric(groups %% 3 == 0)
  folds <- omopduckdb:::.grouped_folds(y, groups, 5, 42)
  expect_true(all(tapply(folds, groups, function(f) length(unique(f))) == 1))
  expect_equal(sort(unique(folds)), 0:4)
})
