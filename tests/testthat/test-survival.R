test_that("Kaplan-Meier estimates match analytical and survival::survfit within 1e-6 tolerance", {
  df <- data.frame(
    time = c(1, 2, 3, 4, 5, 6),
    status = c(1, 0, 1, 1, 0, 1)
  )

  res <- estimate_km_survival(df, time_col = "time", status_col = "status")
  expect_s3_class(res, "omop_survival_result")
  tl <- res$timeline

  # Analytical checks
  # t = 1: S = 5/6 = 0.8333333
  row_1 <- tl[tl$time == 1, ]
  expect_equal(row_1$survival, 5 / 6, tolerance = 1e-6)
  expect_equal(row_1$std_err, (5 / 6) * sqrt(1 / 30), tolerance = 1e-6)

  # t = 3: S = 5/8 = 0.625
  row_3 <- tl[tl$time == 3, ]
  expect_equal(row_3$survival, 5 / 8, tolerance = 1e-6)
  expect_equal(row_3$std_err, (5 / 8) * sqrt(1 / 30 + 1 / 12), tolerance = 1e-6)

  # t = 4: S = 5/12
  row_4 <- tl[tl$time == 4, ]
  expect_equal(row_4$survival, 5 / 12, tolerance = 1e-6)

  # If survival package is available, verify exact equivalence
  if (requireNamespace("survival", quietly = TRUE)) {
    fit <- survival::survfit(survival::Surv(time, status) ~ 1, data = df)
    # Compare survival estimates at event times
    sub_tl <- tl[tl$time %in% fit$time, ]
    expect_equal(sub_tl$survival, fit$surv, tolerance = 1e-6)
    # Note: survival::survfit reports std.err on log hazard scale (SE(S) / S)
    expected_greenwood_se <- ifelse(fit$surv == 0, 0, fit$surv * fit$std.err)
    expect_equal(sub_tl$std_err, expected_greenwood_se, tolerance = 1e-6)
  }
})

test_that("Fine-Gray competing risks CIF satisfies sum-to-incidence and dominates naive KM", {
  df <- data.frame(
    time_days = c(2, 3, 5, 7, 8, 10, 12, 15),
    status =    c(1, 2, 0, 1, 2,  0,  1,  2)
  )

  res <- estimate_km_survival(df)
  expect_true(res$has_competing_risks)
  tl <- res$timeline

  expect_true("cif_primary" %in% names(tl))
  expect_true("cif_competing" %in% names(tl))
  expect_true("naive_km_primary" %in% names(tl))

  # Sum of CIFs equals 1 - S_overall
  cif_sum <- tl$cif_primary + tl$cif_competing
  one_minus_s <- 1.0 - tl$survival
  expect_equal(cif_sum, one_minus_s, tolerance = 1e-6)

  # Naive KM >= true primary CIF
  expect_true(all(tl$naive_km_primary >= tl$cif_primary - 1e-6))
})
