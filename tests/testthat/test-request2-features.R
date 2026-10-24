# Request 2 (R mirror of tests/test_request2_features.py): LOINC fix, bounds / unit harmonization, medication
# windows, domain profiles, cohort_flow, generate_table2, as_plp_arrow, and the vocabulary guards.

r2_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf(paste0(
      "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, ",
      "person_source_value) VALUES (%d, 8507, %d, 8527, 38003563, 'P%d')"), pid, if (pid < 3) 1950L else 2015L, pid))
  }
  DBI::dbExecute(con, paste0(
    "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, ",
    "valid_start_date, valid_end_date) VALUES (3001, 'WBC', 'Measurement', 'LOINC', 'Lab Test', '6690-2', ",
    "DATE '1970-01-01', DATE '2099-12-31'), (3002, 'Creat', 'Measurement', 'LOINC', 'Lab Test', '2160-0', ",
    "DATE '1970-01-01', DATE '2099-12-31')"))
  con
}
r2_visit <- function(con, vid, pid, concept, s, e) {
  DBI::dbExecute(con, sprintf(paste0(
    "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, ",
    "visit_type_concept_id) VALUES (%d, %d, %d, DATE '%s', DATE '%s', 32817)"), vid, pid, concept, s, e))
}
r2_meas <- function(con, mid, pid, cid, date, val, unit) {
  DBI::dbExecute(con, sprintf(paste0(
    "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, ",
    "measurement_type_concept_id, value_as_number, unit_source_value) VALUES (%d, %d, %d, DATE '%s', 32817, %s, '%s')"),
    mid, pid, cid, date, format(val, scientific = FALSE), unit))
}
r2_cohort <- function(con) {
  r2_visit(con, 10, 1, 9201, "2020-06-01", "2020-06-05")
  r2_visit(con, 11, 2, 9201, "2020-06-01", "2020-06-05")
  r2_visit(con, 12, 3, 9201, "2020-06-01", "2020-06-05")
  r2_visit(con, 13, 1, 9203, "2020-07-01", "2020-07-01")
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf(paste0(
      "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, ",
      "observation_period_end_date, period_type_concept_id) VALUES (%d, %d, DATE '2010-01-01', DATE '2021-01-01', 32817)"),
      pid, pid))
  }
  DBI::dbExecute(con, paste0("CREATE TABLE hf_cohort (cohort_definition_id INT, subject_id BIGINT, ",
                             "cohort_start_date DATE, cohort_end_date DATE, readmitted BIGINT)"))
  DBI::dbExecute(con, paste0("INSERT INTO hf_cohort VALUES (1,1,DATE '2020-06-01',DATE '2020-06-05',1),",
                             "(1,2,DATE '2020-06-01',DATE '2020-06-05',0)"))
}

test_that("LOINC 48642-3 is eGFR, not anion gap", {
  expect_false("48642-3" %in% DEFAULT_LAB_LOINCS$anion_gap)
  expect_setequal(DEFAULT_LAB_LOINCS$anion_gap, c("1863-0", "33037-3", "41276-7"))
  expect_true("48642-3" %in% DEFAULT_LAB_LOINCS$egfr)
})

test_that("filter_bounds: wide, long, override, disable", {
  df <- data.frame(wbc = c(5, 226750000, 0.01, 160), sodium = c(140, 90, 150, 181))
  out <- filter_bounds(df)
  expect_equal(is.na(out$wbc), c(FALSE, TRUE, TRUE, FALSE))
  expect_equal(sum(is.na(out$sodium)), 2)
  expect_equal(max(df$wbc), 226750000)
  expect_equal(sum(is.na(filter_bounds(df, list(wbc = c(0.5, 150)))$wbc)), 3)
  expect_equal(filter_bounds(df, FALSE), df, ignore_attr = TRUE)
  expect_equal(nrow(filter_bounds(df, action = "drop")), 1)
  expect_equal(max(filter_bounds(df, action = "clip")$wbc), DEFAULT_PHYSIOLOGIC_BOUNDS$wbc[2])
  long <- data.frame(feature = c("wbc", "wbc", "bmi"), value_as_number = c(5e6, 7, 30))
  lo <- filter_bounds(long, feature_col = "feature")
  expect_equal(is.na(lo$value_as_number), c(TRUE, FALSE, FALSE))
  rep <- attr(lo, "bounds_report")
  expect_equal(rep$n_out_of_range[rep$feature == "wbc"], 1)
})

test_that("harmonize_measurement_units scales, flags and never duplicates rows", {
  con <- r2_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  r2_meas(con, 1, 1, 3001, "2020-01-02", 7.5, "10*3/uL")
  r2_meas(con, 2, 1, 3001, "2020-01-03", 7500, "/uL")
  r2_meas(con, 3, 1, 3002, "2020-01-02", 88.42, "umol/L")
  r2_meas(con, 4, 1, 3002, "2020-01-03", 2, "furlongs")
  rep <- harmonize_measurement_units(con, list(wbc = "6690-2", creatinine = "2160-0"))
  st <- function(f, u) rep$status[rep$feature == f & rep$unit == u]
  expect_equal(st("wbc", "/uL"), "scaled")
  expect_equal(st("wbc", "10*3/uL"), "canonical")
  expect_equal(st("creatinine", "furlongs"), "unrecognized")
  v <- DBI::dbGetQuery(con, "SELECT measurement_id, value_as_number FROM measurement_harmonized ORDER BY 1")
  expect_equal(round(v$value_as_number, 3), c(7.5, 7.5, 1, 2))
  expect_equal(DBI::dbGetQuery(con, "SELECT value_as_number FROM measurement WHERE measurement_id = 2")[[1]], 7500)
})

test_that("medication windows frame exposure around the stay", {
  con <- r2_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  r2_cohort(con)
  DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (500, 600, 1, 1)")
  for (d in list(c(1, "2020-03-01"), c(2, "2020-06-03"), c(3, "2020-06-05"), c(4, "2020-06-10"))) {
    DBI::dbExecute(con, sprintf(paste0(
      "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, ",
      "drug_exposure_end_date, drug_type_concept_id) VALUES (%s, 1, 600, DATE '%s', DATE '%s', 32817)"), d[1], d[2], d[2]))
  }
  me <- medication_extractor(con, list(bb = list(ancestor_concept_ids = 500)))
  f <- function(w) me$extract("hf_cohort", window = w)$bb[1]
  expect_equal(c(f("pre_admission"), f("inpatient"), f("discharge")), c(1, 1, 1))
  DBI::dbExecute(con, "DELETE FROM drug_exposure WHERE drug_exposure_id = 1")
  expect_equal(f("pre_admission"), 0)
  DBI::dbExecute(con, "DELETE FROM drug_exposure WHERE drug_exposure_id = 3")
  expect_equal(c(f("discharge"), f("inpatient")), c(0, 1))
  expect_error(me$extract("hf_cohort", window = "bogus"), "window must be one of")
  wide <- me$extract_all_windows("hf_cohort")
  expect_true(all(c("bb__pre_admission", "bb__inpatient", "bb__discharge") %in% names(wide)))
})

test_that("table1_generator applies profiles, harmonization and pre-aggregation bounds", {
  con <- r2_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  r2_cohort(con)
  r2_meas(con, 1, 1, 3002, "2020-06-02", 1.2, "mg/dL")
  r2_meas(con, 2, 2, 3002, "2020-06-02", 884.2, "umol/L")
  r2_meas(con, 3, 2, 3002, "2020-06-03", 900000, "mg/dL")
  DBI::dbExecute(con, paste0("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, ",
                             "condition_start_date, condition_type_concept_id, condition_source_value) VALUES ",
                             "(1, 1, 0, DATE '2019-01-01', 32817, 'I50.9')"))
  t1 <- table1_generator(con = con)
  t1$add_domain_profile("nephrology")
  t1$state$medications <- list()
  t1$add_custom_lab("wbc_x", "6690-2", bounds = c(0, 50))
  out <- t1$generate("hf_cohort", stratify_by = "readmitted")
  feats <- t1$state$features
  expect_equal(feats$creatinine[feats$subject_id == 1], 1.2)
  expect_equal(feats$creatinine[feats$subject_id == 2], 10, tolerance = 1e-3)
  expect_true("creatinine" %in% out$table1$Variable)
  hf <- table1_generator(con = con)$add_domain_profile("heart_failure")
  hf$state$medications <- list()
  invisible(hf$generate("hf_cohort"))
  expect_equal(hf$state$features$dx_heart_failure[hf$state$features$subject_id == 1], 1)
  expect_error(table1_generator(con = con)$add_domain_profile("nonexistent"), "Unknown profile")
})

test_that("cohort_flow: attrition, preflight and diagram outputs", {
  con <- r2_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  r2_cohort(con)
  f <- cohort_flow(con, anchor = "admission", follow_up_days = 30)
  f$add_step("Adults", "EXTRACT(year FROM v.visit_start_date) - p.year_of_birth >= 18")
  f$run()
  a <- f$state$attrition
  expect_equal(a$subjects_retained[a$step_name == "All visits"], 4)
  expect_equal(a$subjects_retained[a$step_name == "Adults"], 2)
  expect_equal(nrow(f$state$cohort), 2)
  expect_true(all(f$state$checks$status == "PASS"))
  expect_match(f$to_graphviz(), "digraph")
  expect_match(f$to_mermaid(), "flowchart")
  f2 <- cohort_flow(con, visit_concept_ids = c(9201L, 9203L))$run()
  ck <- f2$state$checks
  expect_equal(ck$status[ck$check == "visit types are inpatient (9201) or ER-to-inpatient (262)"], "FAIL")
})

test_that("generate_table2 flags sentinels and missingness", {
  df <- data.frame(a = c(1, 2, 3, 4), b = c(5, 5, 5, 5), c = c(NA, NA, NA, 1), d = rep(NA_real_, 4))
  r <- generate_table2(df)
  fl <- function(v) r$flags[r$variable == v]
  expect_equal(fl("b"), "ZERO_SD_SENTINEL")
  expect_equal(fl("c"), "HIGH_MISSINGNESS")
  expect_match(fl("d"), "ALL_MISSING")
  expect_equal(fl("a"), "")
  expect_equal(r$median[r$variable == "a"], 2.5)
})

test_that("as_plp_arrow keeps one rowId across tables", {
  skip_if_not_installed("arrow")
  con <- r2_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  df <- data.frame(subject_id = c(10, 11, 12), cohort_start_date = as.Date("2020-01-01"),
                   age = c(60, 0, 70), diabetes = c(1, 0, 1), y = c(1, 0, 0))
  r <- as_plp_arrow(con, df, outcome_col = "y")
  cov <- as.data.frame(r$covariates)
  expect_equal(nrow(cov), 4)
  ref <- as.data.frame(r$covariate_ref)
  coh <- as.data.frame(r$cohorts)
  row12 <- coh$rowId[coh$subjectId == 12]
  expect_equal(cov$covariateValue[cov$rowId == row12 & cov$covariateId == ref$covariateId[ref$covariateName == "age"]], 70)
  expect_setequal(names(as.data.frame(r$outcomes)), c("rowId", "outcomeCount"))
})

test_that("domain_profiles.yml matches the Python profiles' lab and ATC lists", {
  prof <- .load_domain_profiles()
  expect_setequal(names(prof), c("heart_failure", "nephrology", "sepsis_icu", "oncology", "pulmonary",
                                 "pharmacotherapy_safety"))
  expect_true("48642-3" %in% prof$nephrology$labs$egfr)
})

# ------------------------------------------------------------- vocabulary guards (opt-in)
r2_vocab <- function() {
  path <- Sys.getenv("OMOP_VOCAB_DB", "")
  if (!nzchar(path)) testthat::skip("set OMOP_VOCAB_DB to a DuckDB file containing the OMOP concept table")
  DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
}

test_that("every mapped LOINC / ATC code matches its feature name in the vocabulary", {
  v <- r2_vocab(); on.exit(DBI::dbDisconnect(v, shutdown = TRUE))
  fx <- file.path(testthat::test_path(), "..", "fixtures", "vocab_feature_keywords.csv")
  kw <- utils::read.csv(fx, stringsAsFactors = FALSE)
  keys <- setNames(strsplit(tolower(kw$keywords), "|", fixed = TRUE), paste(kw$kind, kw$name))
  wanted <- list()
  add <- function(kind, name, codes) {
    k <- paste(kind, name)
    wanted[[k]] <<- union(wanted[[k]], as.character(codes))
  }
  for (m in list(DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS, CORE_14_LAB_PANEL, ACUTE_LAB_PANEL)) {
    for (n in names(m)) add("lab", n, m[[n]])
  }
  for (p in .load_domain_profiles()) {
    for (n in names(p$labs)) add("lab", n, p$labs[[n]])
    for (n in names(p$medications)) add("med", n, p$medications[[n]]$atc_codes)
  }
  problems <- character(0)
  for (k in names(wanted)) {
    if (!k %in% names(keys)) {
      problems <- c(problems, sprintf("%s: add it to vocab_feature_keywords.csv", k))
      next
    }
    voc <- if (startsWith(k, "med ")) "ATC" else "LOINC"
    for (code in wanted[[k]]) {
      nm <- DBI::dbGetQuery(v, "SELECT concept_name FROM concept WHERE vocabulary_id = ? AND concept_code = ?",
                            params = list(voc, code))$concept_name
      if (length(nm) == 0) problems <- c(problems, sprintf("%s: %s not in vocabulary", k, code))
      else if (!any(vapply(keys[[k]], function(w) grepl(w, tolower(nm[1]), fixed = TRUE), TRUE))) {
        problems <- c(problems, sprintf("%s: %s is '%s'", k, code, nm[1]))
      }
    }
  }
  expect_equal(problems, character(0))
})

test_that("default medication concept ids are the classes they claim to be", {
  v <- r2_vocab(); on.exit(DBI::dbDisconnect(v, shutdown = TRUE))
  expect <- list(`21600713` = "insulin", `21600745` = "biguanide", `1503297` = "metformin", `21600749` = "sulfonylurea",
                 `1502855` = "tolbutamide", `1502809` = "tolazamide", `21601855` = "hmg coa",
                 `21601782` = "renin-angiotensin", `21600381` = "antihypertensive", `21601664` = "beta blocking",
                 `21601744` = "calcium channel", `1314002` = "atenolol", `21602722` = "corticosteroids",
                 `1506270` = "methylprednisolone", `1539403` = "simvastatin", `1545958` = "atorvastatin",
                 `1551860` = "pravastatin", `21601668` = "oxprenolol")
  for (id in names(expect)) {
    nm <- DBI::dbGetQuery(v, "SELECT concept_name FROM concept WHERE concept_id = ?", params = list(as.integer(id)))$concept_name
    expect_true(length(nm) == 1 && grepl(expect[[id]], tolower(nm), fixed = TRUE), info = sprintf("concept %s: %s", id, nm))
  }
  used <- unique(unlist(DEFAULT_MEDICATION_CONCEPTS))
  expect_true(all(used %in% as.integer(names(expect))), info = "a default concept id has no name guard")
})

test_that("no code is assigned to two features in the default maps", {
  for (m in list(DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS, CORE_14_LAB_PANEL, ACUTE_LAB_PANEL)) {
    codes <- unlist(lapply(names(m), function(n) stats::setNames(m[[n]], rep(n, length(m[[n]])))))
    dup <- codes[duplicated(codes)]
    expect_length(dup, 0)
  }
})
