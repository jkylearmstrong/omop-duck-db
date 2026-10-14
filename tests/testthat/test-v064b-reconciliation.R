test_that("cohort_from_index_stays maps external stays with audit tallies", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (
      person_id BIGINT PRIMARY KEY,
      gender_concept_id INTEGER,
      year_of_birth INTEGER,
      race_concept_id INTEGER,
      ethnicity_concept_id INTEGER,
      person_source_value VARCHAR
    );
    CREATE TABLE visit_occurrence (
      visit_occurrence_id BIGINT PRIMARY KEY,
      person_id BIGINT,
      visit_concept_id INTEGER,
      visit_start_date DATE,
      visit_end_date DATE,
      visit_type_concept_id INTEGER,
      visit_source_value VARCHAR
    );
  ")

  for (pid in 1:10) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO person VALUES (%d, 8507, 1970, 8527, 38003563, 'SRC_PAT_%d');
    ", pid, pid))
    DBI::dbExecute(con, sprintf("
      INSERT INTO visit_occurrence VALUES (%d, %d, 9201, DATE '2023-05-01', DATE '2023-05-05', 44818518, 'ENC_%d');
    ", 100 + pid, pid, 100 + pid))
  }

  stays <- data.frame(
    person_source_value = c(paste0("SRC_PAT_", 1:8), "UNMAPPED_A", "UNMAPPED_B"),
    visit_source_value = c(paste0("ENC_", 101:108), "ENC_999", "ENC_998"),
    index_date = rep(as.Date("2023-05-01"), 10),
    end_date = rep(as.Date("2023-05-05"), 10),
    site = c(rep("SiteA", 5), rep("SiteB", 5)),
    stringsAsFactors = FALSE
  )

  res <- cohort_from_index_stays(con, stays, id_type = "person_source_value", target_table = "forced_cohort")
  cohort_df <- res$cohort
  audit <- res$audit

  expect_equal(nrow(cohort_df), 8)
  expect_equal(audit$total_input_stays, 10)
  expect_equal(audit$matched_patients, 8)
  expect_equal(audit$unmatched_patients, 2)
  expect_equal(audit$patient_match_rate, 0.8)
  expect_equal(audit$matched_encounters, 8)

  db_count <- DBI::dbGetQuery(con, "SELECT count(*) AS n FROM forced_cohort")$n[1]
  expect_equal(db_count, 8)
})

test_that("compare_feature_tables calculates parity ledger, SMDs, and flags discrepancies", {
  n <- 100
  pids <- 1:n

  set.seed(42)
  f_same <- rnorm(n, mean = 50.0, sd = 5.0)
  f_diff_l <- rnorm(n, mean = 10.0, sd = 2.0)
  f_diff_r <- rnorm(n, mean = 15.0, sd = 2.0)

  f_scale_l <- rnorm(n, mean = 100.0, sd = 10.0)
  f_scale_r <- rnorm(n, mean = 100.0, sd = 1.0)

  f_miss_l <- rep(NA_real_, n)
  f_miss_l[1:80] <- 5.0
  f_miss_r <- rep(5.0, n)

  f_zero <- rep(0, n)

  left <- data.frame(
    person_id = pids,
    f_same = f_same,
    f_diff = f_diff_l,
    f_scale = f_scale_l,
    f_miss = f_miss_l,
    f_zero = f_zero,
    site = c(rep("SiteA", 50), rep("SiteB", 50)),
    stringsAsFactors = FALSE
  )

  right <- data.frame(
    person_id = pids,
    f_same = f_same,
    f_diff = f_diff_r,
    f_scale = f_scale_r,
    f_miss = f_miss_r,
    f_zero = f_zero,
    site = c(rep("SiteA", 50), rep("SiteB", 50)),
    stringsAsFactors = FALSE
  )

  ledger <- compare_feature_tables(left, right, keys = "person_id", by = "site")
  expect_true(nrow(ledger) > 0)

  all_reasons <- paste(unique(ledger$reason), collapse = ",")
  expect_true(grepl("large_smd", all_reasons))
  expect_true(grepl("missingness_gap", all_reasons))
  expect_true(grepl("scale_mismatch", all_reasons))
  expect_true(grepl("all_zero", all_reasons))

  same_rows <- ledger[ledger$feature == "f_same", ]
  expect_false(any(same_rows$flagged))
})

test_that("cohort_overlap evaluates membership intersection and concordance", {
  cohort_a <- data.frame(
    subject_id = 1:15,
    site = c(rep("SiteA", 10), rep("SiteB", 5)),
    outcome = c(1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1),
    stringsAsFactors = FALSE
  )

  cohort_b <- data.frame(
    subject_id = 10:20,
    site = c(rep("SiteA", 5), rep("SiteB", 6)),
    outcome = c(1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1),
    stringsAsFactors = FALSE
  )

  ov <- cohort_overlap(NULL, cohort_a, cohort_b, by = "site", outcome_col = "outcome")
  expect_true(nrow(ov) > 0)

  all_row <- ov[ov$site == "ALL", ]
  expect_equal(all_row$n_cohort_a[1], 15)
  expect_equal(all_row$n_cohort_b[1], 11)
  expect_equal(all_row$both_count[1], 6)
  expect_equal(all_row$a_only_count[1], 9)
  expect_equal(all_row$b_only_count[1], 5)
  expect_equal(round(all_row$jaccard_index[1], 2), round(6 / 20, 2))
  expect_true("both_event_rate" %in% names(all_row))
})

test_that("run_study_grid executes sensitivity variants across grid parameters", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  mock_study <- function(sampling_rule = "first", lookback_days = 365) {
    n <- if (sampling_rule == "first") 10 else 8
    data.frame(
      subject_id = 1:n,
      site = c(rep("SiteA", n %/% 2), rep("SiteB", n - n %/% 2)),
      outcome = rep(c(1, 0), length.out = n),
      stringsAsFactors = FALSE
    )
  }

  variants <- list(
    sampling_rule = c("first", "random"),
    lookback_days = c(180, 365)
  )

  grid <- run_study_grid(con, mock_study, variants)
  expect_true(nrow(grid) > 0)
  expect_equal(nrow(grid), 12)
  expect_true("sampling_rule" %in% names(grid))
  expect_true("lookback_days" %in% names(grid))
  expect_true("event_rate" %in% names(grid))
})
