test_that("Propensity score weighting balances confounded cohort (post-SMD < 0.10)", {
  set.seed(42)
  n <- 2000

  age <- stats::rnorm(n, mean = 60, sd = 10)
  hypertension <- stats::rbinom(n, size = 1, prob = 0.4)
  diabetes <- stats::rbinom(n, size = 1, prob = 0.25)
  bmi <- stats::rnorm(n, mean = 28, sd = 5)

  log_odds <- -2.5 + 0.04 * (age - 50) + 0.7 * hypertension + 0.5 * diabetes + 0.03 * (bmi - 28)
  prob_treatment <- 1.0 / (1.0 + exp(-log_odds))
  treatment <- stats::rbinom(n, size = 1, prob = prob_treatment)
  cohort_def_id <- ifelse(treatment == 1, 1L, 2L)

  df <- data.frame(
    subject_id = seq_len(n),
    cohort_definition_id = cohort_def_id,
    age = age,
    hypertension = hypertension,
    diabetes = diabetes,
    bmi = bmi
  )

  res <- generate_propensity_weights(
    cohort_table = df,
    target_id = 1L,
    comparator_id = 2L,
    covariate_cols = c("age", "hypertension", "diabetes", "bmi")
  )

  expect_s3_class(res, "omop_propensity_result")
  expect_true("propensity_score" %in% names(res$data))
  expect_true("iptw_ate" %in% names(res$data))
  expect_true("iptw_att" %in% names(res$data))

  expect_true(all(res$data$propensity_score > 0))
  expect_true(all(res$data$propensity_score < 1))
  expect_true(all(res$data$iptw_ate >= 1.0))

  # Pre-weighting has substantial confounding
  expect_true(any(res$balance$pre_smd > 0.15))

  # Acceptance criteria: post-weighting SMD < 0.10 across all covariates
  expect_true(res$max_post_smd < 0.10)
  expect_true(all(res$balance$post_smd_ate < 0.10))
})
