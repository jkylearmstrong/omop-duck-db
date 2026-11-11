# v0.13.0 R side: admit-date bounds / sample_stay_rule, benchmark features, frozen vocabulary, pre-training targets,
# recalibration, capacity thresholds and leave-one-site-out transport. Synthetic data only.

bf_cohort_args <- list(min_age = 18, washin_days = 0, min_los_days = 0, followup_days = 30, require_verified_followup = FALSE)

bf_db <- function(n_people = 40) {
  db_path <- tempfile(fileext = ".duckdb")
  invisible(utils::capture.output(build_schema(db_path)))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  set.seed(3)
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES
    (3016723,'Creatinine','Measurement','LOINC','Lab Test','2160-0','1970-01-01','2099-12-31'),
    (3019550,'Sodium','Measurement','LOINC','Lab Test','2951-2','1970-01-01','2099-12-31'),
    (3014576,'Chloride','Measurement','LOINC','Lab Test','2075-0','1970-01-01','2099-12-31'),
    (3015632,'Bicarbonate','Measurement','LOINC','Lab Test','1963-8','1970-01-01','2099-12-31'),
    (21600713,'Insulins','Drug','ATC','ATC 3rd','A10A','1970-01-01','2099-12-31'),
    (1516766,'Insulin X','Drug','RxNorm','Ingredient','1','1970-01-01','2099-12-31')")
  DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (21600713,21600713,0,0),(21600713,1516766,1,1)")
  vid <- 0L; mid <- 0L; did <- 0L
  base <- c(`3016723` = 1.0, `3019550` = 140, `3014576` = 104, `3015632` = 24)
  for (pid in seq_len(n_people)) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, %d, %d, 8527, 38003564)",
                                pid, if (pid %% 2) 8532L else 8507L, sample(1940:1989, 1)))
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period VALUES (%d, %d, DATE '2015-01-01', DATE '2024-12-31', 32817)", pid, pid))
    starts <- sort(as.Date("2018-01-01") + sample(0:1500, 3))
    for (k in seq_along(starts)) {
      s <- starts[[k]]
      vid <- vid + 1L; e <- s + sample(2:6, 1)
      DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, discharged_to_concept_id) VALUES (%d, %d, 9201, DATE '%s', DATE '%s', 32817, %d)",
                                  vid, pid, s, e, sample(c(8536L, 581476L, 8863L, 32216L, 8717L), 1)))
      for (cid in names(base)) for (d in 0:as.integer(e - s)) {
        mid <- mid + 1L
        DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES (%d, %d, %s, DATE '%s', 32817, %f, %d)",
                                    mid, pid, cid, s + d, base[[cid]] + d + stats::rnorm(1, 0, 0.1), vid))
      }
      mid <- mid + 1L   # planted creatinine 99 three days after discharge: must never reach a feature
      DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) VALUES (%d, %d, 3016723, DATE '%s', 32817, 99)", mid, pid, e + 3))
      if (pid %% 3 == 0) {
        did <- did + 1L
        DBI::dbExecute(con, sprintf("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id) VALUES (%d, %d, 1516766, DATE '%s', DATE '%s', 32817)", did, pid, s - 30, s - 1))
      }
    }
  }
  list(con = con, path = db_path)
}

test_that("admit-date bounds equal study_window and sample_stay_rule works", {
  skip_if_not_installed("duckdb")
  fx <- bf_db(); con <- fx$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  call <- function(...) suppressWarnings(do.call(define_study_cohort, c(list(con, materialise = FALSE), bf_cohort_args, list(...))))
  a <- call(min_admit_date = "2019-01-01", max_admit_date = "2020-12-31")
  b <- call(study_window = c("2019-01-01", "2020-12-31"))
  expect_equal(as.data.frame(a)[, c("subject_id", "visit_occurrence_id")], as.data.frame(b)[, c("subject_id", "visit_occurrence_id")])
  expect_true(all(a$cohort_start_date >= as.Date("2019-01-01") & a$cohort_start_date <= as.Date("2020-12-31")))
  expect_lt(nrow(a), nrow(call()))
  expect_error(call(study_window = c("2019-01-01", "2021-01-01"), min_admit_date = "2018-01-01"), "disagrees")
  first <- call(sample_stay_rule = "earliest"); last <- call(sample_stay_rule = "latest")
  expect_true(all(first$cohort_start_date <= last$cohort_start_date))
  r1 <- call(sample_stay_rule = "random", random_seed = 7); r2 <- call(sample_stay_rule = "random", random_seed = 7)
  expect_identical(r1$visit_occurrence_id, r2$visit_occurrence_id)
  expect_identical(attr(r1, "definition")$sampling_rule, "stable_random")
  every <- call(sample_stay_rule = "all")
  expect_equal(nrow(every), 120L)
  expect_equal(sum(every$stay_rank == 1), 40L)
  expect_error(call(sample_stay_rule = "newest"), "sample_stay_rule")
})

test_that("extract_benchmark_features returns dual labs, derived labs and nothing after discharge", {
  skip_if_not_installed("duckdb")
  fx <- bf_db(); con <- fx$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  suppressWarnings(define_study_cohort(con, materialise = TRUE, table_name = "cohort_t", min_age = 18, washin_days = 0,
                                       min_los_days = 0, followup_days = 30, require_verified_followup = FALSE))
  df <- suppressWarnings(extract_benchmark_features(con, "cohort_t", vital_concept_ids = list(systolic_bp = 3004249, diastolic_bp = 3012888, bmi = 3038553)))
  groups <- attr(df, "feature_groups")
  expect_setequal(names(groups), c("demographics", "elixhauser", "medications", "utilization", "vitals", "labs", "disposition"))
  expect_equal(nrow(df), 40L)
  expect_equal(length(groups$medications), 8L)
  expect_true(all(c("lab_egfr", "lab_egfr_last", "lab_anion_gap", "lab_anion_gap_last") %in% names(df)))
  expect_gt(mean(df$lab_creatinine_last >= df$lab_creatinine), 0.95)   # overlapping random stays can break it for a row
  expect_gt(mean(df$lab_egfr_last <= df$lab_egfr), 0.95)
  expect_lt(max(df$lab_creatinine_last), 20)          # the planted 99 after discharge is never used
  disp <- df[, grepl("^disp_", names(df))]
  expect_true(all(rowSums(disp) == 1))
  expect_true("y" %in% names(df))
  expect_error(extract_benchmark_features(con, "cohort_t", include_sdoh = TRUE), "sdi_cut_points")
})

test_that("concept vocabulary round-trips and reads the Python manifest", {
  skip_if_not_installed("jsonlite")
  tok <- build_concept_tokenizer(c("201826 - condition - T2DM", "1516766 - drug - Insulin X"))
  expect_equal(tok$vocab_size, 7L); expect_equal(unname(tok$concept_map[["[PAD]"]]), 3L)
  path <- tempfile(fileext = ".json")
  m <- export_concept_vocabulary(tok, path)
  again <- import_concept_vocabulary(path)
  expect_identical(again$concept_list, tok$concept_list)
  expect_equal(m$mask_token_id, 7L)
  bad <- sub("201826 - condition", "201827 - condition", paste(readLines(path), collapse = "\n"), fixed = TRUE)
  tmp_bad <- tempfile(fileext = ".json"); writeLines(bad, tmp_bad)
  expect_error(import_concept_vocabulary(tmp_bad), "corrupted")
  other <- tempfile(fileext = ".json"); writeLines('{"format": "x"}', other)
  expect_error(import_concept_vocabulary(other), "not a concept vocabulary")
  py <- test_path("..", "fixtures", "concept_vocabulary_python.json")
  skip_if_not(file.exists(py))
  from_py <- import_concept_vocabulary(py)                           # written by omop_etl.export_concept_vocabulary
  expect_equal(from_py$vocab_size, 8L)
  expect_identical(from_py$concept_list[6:8], c("1516766 - drug - Insulin X", "201826 - condition - Type 2 diabetes", "4 - procedure"))
})

test_that("pre-training targets have the documented properties", {
  set.seed(1)
  t <- array(sample.int(60, 3000 * 4 * 6, replace = TRUE) - 1L, dim = c(3000, 4, 6)); t[t < 5] <- 3L
  nv <- sample(0:4, 3000, replace = TRUE)
  r <- make_pretraining_targets(t, nv, vocab_size = 60, mask_prob = 0.15, seed = 1)
  sel <- r$mlm_labels != -100L
  expect_true(all(r$mlm_labels[sel] == t[sel]) && all(t[sel] >= 5L))
  expect_lt(abs(sum(sel) / sum(t >= 5L) - 0.15), 0.01)
  expect_lt(abs(mean(r$masked_concept_tensor[sel] == 60L) - 0.8), 0.03)
  expect_true(all(r$masked_concept_tensor[!sel] == t[!sel]))
  expect_equal(rowSums(r$next_visit_flag), pmax(nv - 1L, 0L))
  expect_identical(r$masked_concept_tensor, make_pretraining_targets(t, nv, 60, 0.15, 1)$masked_concept_tensor)
})

tr_sim <- function(n = 20000, prev = 0.15, seed = 0) {
  set.seed(seed)
  y <- as.numeric(runif(n) < prev)
  list(y = y, s = rnorm(n, mean = y * 1.2))
}

test_that("prior shift, Platt and isotonic recalibration map to the natural rate", {
  d <- tr_sim(); p <- 1 / (1 + exp(-(d$s - 0.6)))
  expect_lt(abs(mean(adjust_for_prevalence(p, 0.5, mean(d$y))) - mean(d$y)), 0.05)
  pl <- platt_scaling_apply(p, platt_scaling_fit(d$y, p))
  iso <- isotonic_apply(p, isotonic_fit(d$y, p))
  for (q in list(pl, iso)) {
    expect_lt(abs(mean(q) - mean(d$y)), 0.01)
    expect_lt(mean((q - d$y)^2), mean((p - d$y)^2))
  }
  expect_true(all(diff(isotonic_fit(d$y, p)$y) >= -1e-12))
  out <- isotonic_apply(seq(0.1, 0.7, 0.1), isotonic_fit(c(0, 1, 0, 1, 1, 0, 1), seq(0.1, 0.7, 0.1)))
  expect_equal(out[1:3], c(0, 0.5, 0.5)); expect_true(all(diff(out) >= 0))
  expect_error(platt_scaling_fit(rep(0, 10), seq(0, 1, length.out = 10)))
  expect_equal(fit_recalibrator("none")$predict(c(0.2, 0.4)), c(0.2, 0.4))
})

test_that("capacity thresholds flag the requested share", {
  d <- tr_sim(); p <- 1 / (1 + exp(-d$s))
  cut <- capacity_threshold(p, 0.10)
  expect_lt(abs(mean(p >= cut) - 0.10), 0.001)
  ev <- evaluate_at_capacity(d$y, p, 0.10)
  expect_gt(ev$ppv, mean(d$y)); expect_gt(ev$lift, 1)
  tiers <- capacity_risk_tiers(p, 0.10)
  expect_lt(abs(mean(map_risk_tiers(p, tiers) == "high_risk") - 0.10), 0.001)
  expect_equal(evaluate_at_capacity(d$y, p, 0.10, cutoff = cut)$cutoff, cut)
  expect_error(capacity_threshold(p, 1.5))
})

tr_sites <- function() {
  set.seed(2)
  do.call(rbind, lapply(list(list("A", 0.10, 3000), list("B", 0.18, 2500), list("C", 0.14, 2800)), function(s) {
    n <- s[[3]]; x1 <- rnorm(n); x2 <- rnorm(n)
    y <- as.integer(runif(n) < 1 / (1 + exp(-(-2.4 + 0.9 * x1 + 0.5 * x2 + (s[[2]] - 0.14) * 6))))
    data.frame(site_anon = s[[1]], y = y, x1 = x1, x2 = x2, person_id = seq_len(n))
  }))
}

test_that("leave-one-site-out trains balanced, scores naturally and recalibrates", {
  df <- tr_sites()
  res <- evaluate_loso_transport(df, "site_anon", n_bootstraps = 20, keep_predictions = TRUE)
  ps <- res$per_site
  expect_equal(sort(ps$site), c("A", "B", "C"))
  expect_true(all(round(ps$train_prevalence_balanced, 3) == 0.5))
  expect_true(all(ps$auroc > 0.65) && all(ps$auroc_lo <= ps$auroc) && all(ps$auroc <= ps$auroc_hi))
  expect_equal(ps$n, c(3000, 2500, 2800))
  expect_true(all(ps$oe_ratio > 0.6 & ps$oe_ratio < 1.6))
  uncal <- evaluate_loso_transport(df, "site_anon", calibration = "none", n_bootstraps = 0)
  expect_true(all(uncal$per_site$oe_ratio < 0.6))
  expect_true(all(ps$top10_lift > 1))
  expect_equal(nrow(res$predictions), nrow(df))
  expect_false("person_id" %in% res$settings$features)
  for (cal in c("platt", "isotonic")) {
    r <- evaluate_loso_transport(df, "site_anon", calibration = cal, n_bootstraps = 0)
    expect_true(all(r$per_site$oe_ratio > 0.6 & r$per_site$oe_ratio < 1.6))
  }
  expect_error(evaluate_loso_transport(df[df$site_anon == "A", ], "site_anon"), "at least two sites")
  expect_error(evaluate_loso_transport(df, "site_anon", calibration = "magic"), "calibration")
})
