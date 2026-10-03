# Tests for the tidymodels bridge: as_tidymodels_data(), model_concepts(),
# and materialize_concept_features() for in-database scoring with orbital / tidypredict.

skip_if_no_tidymodels <- function() {
  for (pkg in c("tibble", "Matrix", "rsample", "recipes", "parsnip", "workflows", "yardstick")) {
    testthat::skip_if_not_installed(pkg)
  }
}

safe_disconnect <- function(syn) {
  if (DBI::dbIsValid(syn$con)) DBI::dbDisconnect(syn$con, shutdown = TRUE)
}

test_that("as_tidymodels_data builds a valid tidymodels tibble with correct columns and roles", {
  skip_if_no_tidymodels()
  syn <- synthetic_cdm()
  on.exit(safe_disconnect(syn), add = TRUE)

  res <- extract_sparse_concept_matrix(syn$con, syn$parquet, domains = c("condition", "drug"), value = "binary")
  dat <- as_tidymodels_data(res, event = "yes", non_event = "no")

  expect_s3_class(dat, "tbl_df")
  expect_equal(nrow(dat), 400)
  expect_true(all(c("person_id", "index_date", "y") %in% names(dat)))
  expect_true(is.factor(dat$y))
  expect_equal(levels(dat$y), c("yes", "no")) # event is first level for yardstick
  expect_equal(as.character(dat$y[1]), if (syn$y[1] == 1) "yes" else "no")

  # Predictor naming convention: <domain>_<concept_id>
  meta <- attr(dat, "concepts")
  expect_true(is.data.frame(meta))
  expect_true(all(c("feature", "token", "domain", "concept_id", "concept_name") %in% names(meta)))
  expect_true(all(meta$feature %in% names(dat)))
  expect_true("condition_201" %in% names(dat))

  # Test without outcome column (e.g. prospective scoring cohort)
  no_y_res <- res
  no_y_res$y <- NULL
  dat_no_y <- as_tidymodels_data(no_y_res)
  expect_false("y" %in% names(dat_no_y))
  expect_true(all(c("person_id", "index_date") %in% names(dat_no_y)))
})

test_that("as_tidymodels_data supports both sparse and dense predictor representations", {
  skip_if_no_tidymodels()
  syn <- synthetic_cdm()
  on.exit(safe_disconnect(syn), add = TRUE)

  res <- extract_sparse_concept_matrix(syn$con, syn$parquet, domains = c("condition", "drug"), value = "binary")

  # Dense
  dat_dense <- as_tidymodels_data(res, sparse = FALSE)
  expect_s3_class(dat_dense, "tbl_df")
  expect_true(is.numeric(dat_dense$condition_201))

  # Sparse (when sparsevctrs is installed)
  if (requireNamespace("sparsevctrs", quietly = TRUE)) {
    dat_sparse <- as_tidymodels_data(res, sparse = TRUE)
    expect_s3_class(dat_sparse, "tbl_df")
    expect_true(sparsevctrs::is_sparse_vector(dat_sparse$condition_201))
    # Numeric values match between sparse and dense
    expect_equal(as.numeric(dat_sparse$condition_201), as.numeric(dat_dense$condition_201))
  }
})

test_that("materialize_concept_features creates DuckDB wide table matching sparse matrix features", {
  syn <- synthetic_cdm()
  on.exit(safe_disconnect(syn), add = TRUE)

  res <- extract_sparse_concept_matrix(syn$con, syn$parquet, domains = c("condition", "drug"), value = "binary")
  mat <- materialize_concept_features(syn$con, syn$parquet, res$concepts, table = "test_features", value = "binary")

  expect_equal(mat$table, "test_features")
  expect_equal(mat$n_rows, 400)
  expect_true("condition_201" %in% mat$features)

  db_df <- DBI::dbGetQuery(syn$con, "SELECT * FROM test_features ORDER BY row_id")
  expect_equal(nrow(db_df), 400)
  expect_true(all(c("row_id", "person_id", "index_date", "y", "condition_201") %in% names(db_df)))

  # Verify values match the sparse matrix exactly row by row
  col_idx <- match("condition_201", res$concepts$feature)
  matrix_vals <- as.numeric(res$X[, col_idx])
  expect_equal(as.numeric(db_df$condition_201), matrix_vals)
  expect_equal(as.numeric(db_df$y), as.numeric(syn$y))
})

test_that("model_concepts extracts active predictors and enables in-database scoring parity", {
  skip_if_no_tidymodels()
  testthat::skip_if_not_installed("glmnet")
  testthat::skip_if_not_installed("broom")

  syn <- synthetic_cdm()
  on.exit(safe_disconnect(syn), add = TRUE)

  res <- extract_sparse_concept_matrix(syn$con, syn$parquet, domains = c("condition", "drug"), value = "binary")
  dat <- as_tidymodels_data(res, sparse = FALSE)

  rec <- recipes::recipe(y ~ ., data = dat) |>
    recipes::update_role(person_id, index_date, new_role = "id")

  spec <- parsnip::logistic_reg(penalty = 0.01, mixture = 1) |>
    parsnip::set_engine("glmnet")

  wf <- workflows::workflow() |>
    workflows::add_recipe(rec) |>
    workflows::add_model(spec)

  fit <- parsnip::fit(wf, data = dat)

  used <- model_concepts(fit, res$concepts)
  expect_s3_class(used, "data.frame")
  expect_true("condition_201" %in% used$feature) # signal feature should have non-zero coefficient

  # Materialize wide features in DuckDB only for active model concepts
  mat <- materialize_concept_features(syn$con, syn$parquet, used, table = "score_cohort", value = "binary")
  expect_true("condition_201" %in% mat$features)

  # If orbital is installed, test orbital in-database compilation
  if (requireNamespace("orbital", quietly = TRUE)) {
    orb <- orbital::orbital(fit)
    expect_s3_class(orb, "orbital_class")
    db_tbl <- dplyr::tbl(syn$con, "score_cohort")
    scored <- orbital::augment(orb, db_tbl) |> dplyr::collect()
    expect_true(any(grepl(".pred", names(scored))))
    expect_equal(nrow(scored), 400)

    # Verify score agreement between R memory predict and DuckDB orbital evaluation
    r_pred <- predict(fit, new_data = dat, type = "prob")
    if (".pred_yes" %in% names(r_pred) && ".pred_yes" %in% names(scored)) {
      expect_equal(scored$.pred_yes, r_pred$.pred_yes, tolerance = 1e-4)
    }
  }
})
