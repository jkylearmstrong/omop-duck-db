# Tests for extract_sparse_concept_matrix(). Expected values are hand-derived from the fixture in
# helper-ml-fixture.R and are identical to the Python tests in tests/test_ml_features.py.

skip_if_not_installed("Matrix")

t2d <- "201 - condition - Type 2 diabetes"
met <- "301 - drug - Metformin"

col_of <- function(res) stats::setNames(res$concepts$column_index, res$concepts$token)
# X[i, "token"] keeps the column name (colnames(X) are set on purpose); compare values only
cell <- function(X, i, col) unname(as.matrix(X)[i, col])

test_that("counts respect the window, never leak, and rows align with the parquet", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet)
  X <- as.matrix(res$X)
  cols <- col_of(res)

  expect_s4_class(res$X, "dgCMatrix")
  expect_equal(dim(X), c(4L, length(cols)))
  expect_equal(res$cohort$person_id, c(2, 1, 3, 1))     # parquet order, not sorted
  expect_equal(res$y, c(1, 0, 0, 1))
  expect_equal(colnames(res$X), res$concepts$token)

  # person 1 @ 2021-06-01: 201 twice, metformin once.
  expect_equal(unname(X[2, cols[[t2d]]]), 2)
  expect_equal(unname(X[2, cols[[met]]]), 1)
  expect_equal(unname(X[2, cols[["202 - condition"]]]), 0, info = "index-date event leaked")
  expect_equal(unname(X[2, cols[["203 - condition"]]]), 0, info = "post-index event leaked")
  expect_false("204 - condition" %in% names(cols))      # >365d old for everyone
  expect_equal(sum(X[2, ]), 3)
  # person 2: 201, metformin, and the visit-less 205 record all count
  expect_equal(unname(X[1, c(cols[[t2d]], cols[[met]], cols[["205 - condition"]])]), c(1, 1, 1))
  # person 3: no events -> all-zero row kept
  expect_equal(sum(X[3, ]), 0)
  # person 1's later index row sees 202, 203 and the earlier history
  expect_equal(unname(X[4, c(cols[["203 - condition"]], cols[[t2d]], cols[["202 - condition"]])]), c(1, 2, 1))
})

test_that("concept 0 is never a feature", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet)
  expect_false(0 %in% res$concepts$concept_id)
})

test_that("include_index_date and lookback bounds", {
  fx <- make_ml_fixture()
  inc <- extract_sparse_concept_matrix(fx$con, fx$parquet, include_index_date = TRUE)
  expect_equal(cell(inc$X, 2, col_of(inc)[["202 - condition"]]), 1)

  short <- extract_sparse_concept_matrix(fx$con, fx$parquet, lookback_days = 7)
  expect_equal(cell(short$X, 2, col_of(short)[[t2d]]), 1)
  expect_false("205 - condition" %in% short$concepts$token)

  unbounded <- extract_sparse_concept_matrix(fx$con, fx$parquet, lookback_days = NULL)
  expect_true("204 - condition" %in% unbounded$concepts$token)
})

test_that("binary values and domain selection", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, value = "binary",
                                       domains = c("condition", "drug", "procedure"))
  cols <- col_of(res)
  expect_equal(cell(res$X, 2, cols[[t2d]]), 1)
  expect_equal(cell(res$X, 2, cols[["401 - procedure"]]), 1)
  drugs <- extract_sparse_concept_matrix(fx$con, fx$parquet, domains = "drug")
  expect_true(all(drugs$concepts$domain == "drug"))
})

test_that("min_patient_freq counts distinct patients, not rows", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, min_patient_freq = 2)
  expect_setequal(res$concepts$concept_id, c(201, 301))
  expect_true(all(res$concepts$n_patients == 2))
  expect_equal(ncol(extract_sparse_concept_matrix(fx$con, fx$parquet, min_patient_freq = 3)$X), 0L)
})

test_that("reusing tokens fixes the column space (train/test)", {
  fx <- make_ml_fixture()
  train <- extract_sparse_concept_matrix(fx$con, fx$parquet, min_patient_freq = 2)
  test_pq <- tempfile(fileext = ".parquet")
  on.exit(unlink(test_pq), add = TRUE)
  write_cohort_parquet(fx$con, test_pq, "SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
  test <- extract_sparse_concept_matrix(fx$con, test_pq, tokens = train$concepts$token)
  expect_equal(ncol(test$X), ncol(train$X))
  expect_equal(test$concepts$token, train$concepts$token)
  expect_null(test$y)
  expect_equal(sum(test$X), 3)        # 203/205 are outside the train vocabulary and dropped
})

test_that("vocabulary metadata comes from the concept table", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, min_patient_freq = 2)
  row <- res$concepts[res$concepts$concept_id == 201, ]
  expect_equal(row$concept_name, "Type 2 diabetes")
  expect_equal(row$vocabulary_id, "SNOMED")
})

test_that("explicit columns and error messages", {
  fx <- make_ml_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, person_col = "subject_id",
                                       index_date_col = "cohort_start_date", outcome_col = "outcome_flag")
  expect_equal(res$y, c(1, 0, 0, 1))
  expect_error(extract_sparse_concept_matrix(fx$con, fx$parquet, person_col = "nope"), "Available columns")
  expect_error(extract_sparse_concept_matrix(fx$con, fx$parquet, domains = "measurement"), "domains")

  bad <- tempfile(fileext = ".parquet")
  on.exit(unlink(bad), add = TRUE)
  write_cohort_parquet(fx$con, bad, "SELECT * FROM (VALUES (1, DATE '2021-06-01'), (NULL, DATE '2021-06-01')) t(subject_id, cohort_start_date)")
  expect_error(extract_sparse_concept_matrix(fx$con, bad), "missing")
})

test_that("tokens sort in C-locale order like Python / omop-learn", {
  sorter <- get(".mlf_sort_tokens", envir = asNamespace("omopduckdb"))
  # digit-string order ("10" < "9") and ASCII order (uppercase before lowercase) regardless of session locale
  expect_equal(sorter(c("9 - drug", "10 - drug", "b", "B", "a")), c("10 - drug", "9 - drug", "B", "a", "b"))
})

test_that("the file also works when sourced on its own", {
  path <- testthat::test_path("..", "..", "R", "ml_features.R")
  skip_if_not(file.exists(path), "R sources not available (installed-package test run)")
  env <- new.env(parent = globalenv())
  sys.source(path, envir = env)
  fx <- make_ml_fixture()
  res <- env$extract_sparse_concept_matrix(fx$con, fx$parquet, min_patient_freq = 2)
  expect_setequal(res$concepts$concept_id, c(201, 301))
})

test_that("anchor_date and washin buffer bounds in R", {
  fx <- make_ml_fixture()
  pq <- tempfile(fileext = ".parquet")
  on.exit(unlink(pq), add = TRUE)
  write_cohort_parquet(fx$con, pq, "
    SELECT 1 AS person_id, DATE '2021-05-30' AS admit_date, DATE '2021-06-05' AS discharge_date, 1 AS y
  ")

  # 1. With anchor_date='admit_date', 2021-06-01 (concept 202) is strictly after admit_date -> excluded
  res_admit <- extract_sparse_concept_matrix(fx$con, pq, anchor_date = "admit_date", domains = "condition")
  expect_false("202 - condition" %in% res_admit$concepts$token)

  # 2. With anchor_date='discharge_date', 2021-06-01 is before discharge -> included
  res_disch <- extract_sparse_concept_matrix(fx$con, pq, anchor_date = "discharge_date", domains = "condition")
  expect_true("202 - condition" %in% res_disch$concepts$token)

  # 3. With anchor_date='admit_date' and washin_buffer_hours = 48 (2 days), included
  res_buf <- extract_sparse_concept_matrix(fx$con, pq, anchor_date = "admit_date", washin_buffer_hours = 48, domains = "condition")
  expect_true("202 - condition" %in% res_buf$concepts$token)
})
