SPEC_MLOPS <- list(features = list(
  list(name = "lab_creatinine", dtype = "float", bounds = c(0.1, 30), impute = 1),
  list(name = "heart_rate", dtype = "float", bounds = c(20, 250), impute = 80),
  list(name = "prior_ip_count", dtype = "int", impute = 0)))
COEF_MLOPS <- c(0.8, 0.02, 0.5); B0_MLOPS <- -3.5
p_mlops <- function(creat, hr, prior) 1 / (1 + exp(-(B0_MLOPS + sum(COEF_MLOPS * c(creat, hr, prior)))))

mlops_bundle <- function() {
  write_model_bundle(file.path(tempfile(), "b"), name = "demo", version = "1", feature_spec = SPEC_MLOPS,
                     components = list(list(kind = "logistic", filename = "glm.json", content = list(intercept = B0_MLOPS, coef = as.list(COEF_MLOPS)))))
}

mlops_cdm <- function() {
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1950, 8527, 38003564)")
  for (r in list(c(10, "2020-01-01", "2020-01-04"), c(11, "2020-06-01", "2020-06-04"), c(12, "2020-07-01", "2020-07-01"))) {
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%s, 1, 9201, DATE '%s', DATE '%s', 32827)", r[1], r[2], r[3]))
  }
  m <- list(c(3016723, "2020-06-01", 1), c(3016723, "2020-06-03", 3), c(3027018, "2020-06-01", 90), c(3027018, "2020-06-04", 70))
  for (i in seq_along(m)) {
    DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, measurement_type_concept_id) VALUES (%d, 1, %s, DATE '%s', %s, 44818702)", i, m[[i]][1], m[[i]][2], m[[i]][3]))
  }
  con
}

test_that("score_encounter uses the latest values before discharge", {
  con <- mlops_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  b <- mlops_bundle()
  r <- score_encounter(con, 11, b)
  expect_equal(r$features$lab_creatinine, 3); expect_equal(r$features$heart_rate, 70)
  expect_equal(r$measurement_dates$lab_creatinine, "2020-06-03")
  expect_equal(r$prob, p_mlops(3, 70, 1), tolerance = 1e-6)
  expect_equal(r$person_id, 1L); expect_true(r$risk_tier %in% c("low", "moderate", "high"))
  early <- score_encounter(con, 11, b, as_of = "2020-06-02")
  expect_equal(c(early$features$lab_creatinine, early$features$heart_rate), c(1, 90))
  expect_equal(early$prob, p_mlops(1, 90, 1), tolerance = 1e-6)
})

test_that("score_encounter: custom tiers, no readings, errors, no leftover view", {
  con <- mlops_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  b <- mlops_bundle()
  r <- score_encounter(con, 12, b, as_of = "2020-07-02", risk_tiers = list(list(name = "standard", min = 0), list(name = "high", min = 1e-4)))
  expect_equal(r$risk_tier, "high"); expect_null(r$measurement_dates$lab_creatinine)
  expect_error(score_encounter(con, 999, b), "not found")
  expect_error(score_encounter(con, 11, b, as_of = "2020-05-01"), "before the admission")
  expect_false("_score_encounter_cohort" %in% DBI::dbGetQuery(con, "SHOW TABLES")$name)
})

test_that("PSI is ~0 for the same distribution and large for a shift", {
  set.seed(0)
  base <- rbeta(5000, 2, 8)
  expect_lt(population_stability_index(base, rbeta(5000, 2, 8)), 0.05)
  expect_gt(population_stability_index(base, rbeta(5000, 4, 6)), 0.25)
})

test_that("calibration_summary detects over-prediction and withholds unstable statistics", {
  set.seed(1)
  p <- runif(4000, 0.05, 0.5)
  good <- calibration_summary(rbinom(4000, 1, p), p); bad <- calibration_summary(rbinom(4000, 1, p / 2), p)
  expect_true(good$oe_ratio > 0.9 && good$oe_ratio < 1.1); expect_true(good$slope > 0.85 && good$slope < 1.15); expect_gt(good$auroc, 0.6)
  expect_lt(bad$oe_ratio, 0.6)
  expect_true(is.na(calibration_summary(c(0, 1, 0), c(0.1, 0.2, 0.3))$slope))
})

live_scores <- function(months, shift = 1, deflate = 1, n = 1500) {
  do.call(rbind, lapply(months, function(m) {
    p <- pmin(rbeta(n, 2, 8) * shift, 1)
    data.frame(score_date = as.Date(m) + sample(0:27, n, replace = TRUE), prob = p, outcome_flag = rbinom(n, 1, pmin(p * deflate, 1)))
  }))
}

test_that("monitor_scores flags drift and miscalibration and waits for outcomes", {
  set.seed(2)
  base <- rbeta(5000, 2, 8)
  rep <- monitor_scores(rbind(live_scores("2020-01-01"), live_scores("2020-02-01", shift = 2), live_scores("2020-03-01", deflate = 0.5)),
                        base, as_of = "2020-06-30")
  rownames(rep) <- rep$month
  expect_equal(rep["2020-01", "status"], "ok")
  expect_true(rep["2020-02", "status"] %in% c("drift", "miscalibrated")); expect_gt(rep["2020-02", "psi"], 0.25)
  expect_equal(rep["2020-03", "status"], "miscalibrated"); expect_lt(rep["2020-03", "oe_ratio"], 0.8)
  open <- monitor_scores(live_scores("2020-06-01"), base, as_of = "2020-06-30")
  expect_false(open$outcomes_matured); expect_equal(open$status, "insufficient_outcomes"); expect_true(is.na(open$oe_ratio))
})

test_that("update_monitoring_log upserts by model version and month", {
  set.seed(3)
  base <- rbeta(5000, 2, 8); path <- tempfile(fileext = ".csv")
  update_monitoring_log(monitor_scores(live_scores(c("2020-01-01", "2020-02-01")), base, as_of = "2020-02-15"), path, model_version = "1.0")
  log <- update_monitoring_log(monitor_scores(live_scores("2020-02-01"), base, as_of = "2020-06-30"), path, model_version = "1.0")
  expect_equal(log$month, c("2020-01", "2020-02"))
  expect_true(log$outcomes_matured[log$month == "2020-02"])
  expect_equal(nrow(utils::read.csv(path)), 2)
})

test_that("fit_capacity_thresholds hits the target shares and map_risk_tiers applies them", {
  set.seed(4)
  ref <- rbeta(10000, 2, 8)
  ts <- fit_capacity_thresholds(ref, list(list(name = "high", top_fraction = 0.10), list(name = "elevated", top_fraction = 0.30), list(name = "standard")))
  expect_equal(vapply(ts, function(t) t$name, ""), c("standard", "elevated", "high")); expect_equal(ts[[1]]$min, 0)
  share <- vapply(ts, function(t) t$fraction, 0); names(share) <- vapply(ts, function(t) t$name, "")
  expect_equal(unname(share["high"]), 0.10, tolerance = 0.005); expect_equal(unname(share["elevated"]), 0.20, tolerance = 0.005)
  expect_equal(mean(map_risk_tiers(ref, ts) == "high"), 0.10, tolerance = 0.005)
})

test_that("map_risk_tiers boundaries, scalars and validation; tier_load flags capacity", {
  ts <- list(list(name = "standard", min = 0), list(name = "high", min = 0.2))
  expect_equal(c(map_risk_tiers(0.2, ts), map_risk_tiers(0.1999, ts), map_risk_tiers(1, ts)), c("high", "standard", "high"))
  expect_equal(as.character(map_risk_tiers(c(0, 0.5), ts, as_category = TRUE)), c("standard", "high"))
  expect_error(map_risk_tiers(1.2, ts), "probabilities")
  expect_error(map_risk_tiers(0.5, list(list(name = "high", min = 0.2))), "min = 0")
  expect_error(fit_capacity_thresholds(c(0.1, 0.2, 0.3), list(list(name = "a", top_fraction = 0.3), list(name = "b", top_fraction = 0.2), list(name = "c"))), "increase")
  df <- tier_load(c(rep(0.9, 30), rep(0.1, 70)), list(list(name = "standard", min = 0), list(name = "high", min = 0.5)), capacity = c(high = 5), periods = 5)
  expect_equal(df$per_period[df$tier == "high"], 6); expect_true(df$over_capacity[df$tier == "high"]); expect_false(df$over_capacity[df$tier == "standard"])
})
