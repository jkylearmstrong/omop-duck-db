fix_dir <- function() file.path(testthat::test_path(), "..", "fixtures")

test_that("engineer_clinical_features reproduces the reference script's golden output", {
  inp <- utils::read.csv(file.path(fix_dir(), "velocity_input.csv"), stringsAsFactors = FALSE)
  gold <- utils::read.csv(file.path(fix_dir(), "velocity_golden.csv"), stringsAsFactors = FALSE)
  got <- engineer_clinical_features(inp)
  expect_true(all(names(gold) %in% names(got)))
  for (col in names(gold)) {
    if (col == "site_source") expect_identical(got[[col]], inp[[col]])  # the reference NaN-s text columns; the port does not
    else expect_equal(as.numeric(got[[col]]), as.numeric(gold[[col]]), tolerance = 1e-12, info = col)
  }
  expect_true(all(VELOCITY_FEATURES %in% names(got)))
})

test_that("reference quirks are kept and absent columns are reported", {
  out <- suppressWarnings(engineer_clinical_features(data.frame(index_los_days = c(0, 1, 1.5, 2, 6, 7, 14, NA))))
  expect_equal(out$lace_los_score, c(0, 1, 0, 2, 4, 5, 7, 1))
  expect_warning(o <- engineer_clinical_features(data.frame(prior_ip_count = 2)), "all-missing")
  expect_equal(o$shock_index, 75 / 120)
  expect_equal(o$bun_to_creatinine, 15)
  expect_equal(o$ip_velocity, 2 / (365 / 30 + 1))
})

test_that("site_baseline_risk uses training rows only", {
  df <- data.frame(site_source = strsplit("AAAABBBB", "")[[1]], readmitted_30d = c(1, 1, 0, 0, 0, 0, 0, 1))
  r <- site_baseline_risk(df, train_mask = c(TRUE, TRUE, TRUE, FALSE, TRUE, TRUE, FALSE, FALSE))
  expect_equal(r[1], 2 / 3); expect_equal(r[8], 0)
  expect_equal(site_baseline_risk(data.frame(site_source = c("A", "Z"), readmitted_30d = c(1, 0)), train_mask = c(TRUE, FALSE))[2], 1)
})

test_that("extract_velocity_features: raw inputs, engineered values, sentinel, no leakage", {
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  for (p in 1:3) DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1960, 8527, 38003564)", p))
  v <- function(id, p, c, s, e) DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d,%d,%d,DATE '%s',DATE '%s',32827)", id, p, c, s, e))
  v(1, 1, 9201, "2019-09-01", "2019-09-05"); v(2, 1, 9201, "2019-09-20", "2019-09-22")
  v(3, 1, 9201, "2020-02-01", "2020-02-03"); v(4, 1, 9201, "2020-03-20", "2020-03-25")
  v(5, 1, 9203, "2020-02-10", "2020-02-10"); v(6, 1, 9202, "2020-03-01", "2020-03-01")
  v(7, 1, 9203, "2020-04-10", "2020-04-12"); v(8, 2, 9201, "2020-04-10", "2020-04-12")
  v(9, 3, 9201, "2020-04-10", "2020-04-20"); v(10, 3, 9201, "2020-05-01", "2020-05-02")
  DBI::dbExecute(con, "CREATE TABLE coh AS SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date FROM visit_occurrence WHERE visit_occurrence_id IN (7,8,9)")
  m <- function(id, p, c, val) DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) VALUES (%d,%d,%d,DATE '2020-04-10',44818702,%s)", id, p, c, val))
  m(1, 1, 3027018, 100); m(2, 1, 3004249, 80); m(3, 1, 3012888, 50); m(4, 1, 3013682, 40); m(5, 1, 3016723, 2); m(6, 2, 3004249, 0)
  expect_warning(f <- extract_velocity_features(con, "coh"), "default-imputed")
  p1 <- f[f$subject_id == 1, ]; p2 <- f[f$subject_id == 2, ]; p3 <- f[f$subject_id == 3, ]
  expect_equal(c(p1$prior_ip_count, p1$prior_ed_count, p1$prior_encounters_all_count, p1$prior_ip_os_ed_count), c(4, 1, 6, 6))
  expect_equal(p1$prior_ip_los_days, 13); expect_equal(p1$readmission_count_twelve_months, 1)
  expect_equal(c(p1$days_since_prior_encounter, p1$days_since_prior_ip_ed, p1$index_los_days), c(16, 16, 2))
  expect_equal(p1$ip_velocity, 4 / (16 / 30 + 1)); expect_equal(p1$recent_ip_ratio, 4 / 7)
  expect_equal(p1$readmission_intensity, 1 / 5)
  expect_equal(p1$shock_index, 100 / 80); expect_equal(p1$bun_to_creatinine, 20)
  expect_equal(p1$lace_proxy_total, 6)
  expect_true(is.na(p2$days_since_prior_encounter)); expect_equal(c(p2$ip_velocity, p2$recent_ip_ratio, p2$utilization_density), c(0, 0, 0))
  expect_equal(p2$shock_index, 75 / 60)
  expect_equal(p3$prior_encounters_all_count, 0); expect_equal(p3$lace_los_score, 5)
  raw <- extract_velocity_features(con, "coh", engineer = FALSE)
  expect_false("ip_velocity" %in% names(raw)); expect_equal(nrow(raw), 3)
})
