# Outstanding request 1 (R mirror of tests/test_outstanding_requests.py): BMI coalescing in the wide loader,
# inpatient labs, prior utilization, index-diagnosis guard and the scoring bundle.

or_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  for (i in 1:2) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (%d, 8507, 1960, 8527, 38003563, 'P%d')",
      i, i))
  }
  con
}

or_visit <- function(con, vid, pid, concept, start, end) {
  DBI::dbExecute(con, sprintf(
    "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, %d, DATE '%s', DATE '%s', 32817)",
    vid, pid, concept, start, end))
}

or_cohort <- function(con, rows) {
  DBI::dbExecute(con, "ALTER TABLE cohort ADD COLUMN visit_occurrence_id BIGINT")
  for (r in rows) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id) VALUES (1, %d, DATE '%s', DATE '%s', %s)",
      r[[1]], r[[2]], r[[3]], if (is.na(r[[4]])) "NULL" else r[[4]]))
  }
}

# ---- BMI coalescing ------------------------------------------------------------------------------------
test_that("load_wide_measurement coalesces columns sharing a concept (BMI / ORIGINAL_BMI)", {
  con <- or_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  p <- tempfile(fileext = ".csv")
  writeLines(c("PATID,MEASURE_DATE,BMI,ORIGINAL_BMI",
               "P1,2020-01-01,25,26",     # both: precedence wins
               "P1,2020-01-02,2,27",      # primary implausible -> falls back
               "P2,2020-01-03,,28",       # only fallback
               "P2,2020-01-04,,"), p)
  mapping <- DEFAULT_VITAL_CONCEPT_MAPPING[c("BMI", "ORIGINAL_BMI")]
  rep <- load_wide_measurement(con, p, mapping, value_plausibility_filters = DEFAULT_VITAL_PLAUSIBILITY_FILTERS,
                               coalesce_columns = list(c("ORIGINAL_BMI", "BMI")), time_col = NULL, visit_col = NULL)
  vals <- DBI::dbGetQuery(con, "SELECT value_as_number AS v FROM measurement ORDER BY 1")$v
  expect_equal(vals, c(26, 27, 28))
  expect_equal(rep$coalesced_into[rep$source_column == "BMI"], "ORIGINAL_BMI")

  DBI::dbExecute(con, "DELETE FROM measurement")
  auto <- load_wide_measurement(con, p, mapping, value_plausibility_filters = DEFAULT_VITAL_PLAUSIBILITY_FILTERS,
                                time_col = NULL, visit_col = NULL)
  expect_equal(sum(auto$rows_inserted), 3)   # BMI-first precedence: 25, 27 (BMI=2 implausible), 28
  expect_equal(auto$coalesced_into[auto$source_column == "ORIGINAL_BMI"], "BMI")

  DBI::dbExecute(con, "DELETE FROM measurement")
  off <- load_wide_measurement(con, p, mapping, coalesce_columns = NULL, time_col = NULL, visit_col = NULL)
  expect_equal(sum(off$rows_inserted), 5)    # no coalescing: BMI x2 + ORIGINAL_BMI x3
  expect_error(load_wide_measurement(con, p, list(BMI = 3038553L, ORIGINAL_BMI = 1L),
                                     coalesce_columns = list(c("BMI", "ORIGINAL_BMI")),
                                     time_col = NULL, visit_col = NULL), "more than one concept_id")
})

# ---- prior utilization ---------------------------------------------------------------------------------
test_that("extract_prior_utilization counts prior visits strictly before admission", {
  con <- or_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  or_visit(con, 10, 1, 9201, "2020-01-01", "2020-01-05")
  or_visit(con, 11, 1, 9201, "2020-01-20", "2020-01-23")
  or_visit(con, 12, 1, 9203, "2020-05-01", "2020-05-01")
  or_visit(con, 13, 1, 9202, "2018-01-01", "2018-01-01")
  or_visit(con, 20, 1, 9201, "2020-06-01", "2020-06-10")
  or_visit(con, 21, 1, 9201, "2020-06-20", "2020-06-22")
  or_cohort(con, list(list(1, "2020-06-01", "2020-06-10", 20), list(2, "2020-06-01", "2020-06-05", NA)))
  df <- extract_prior_utilization(con)
  p1 <- df[df$subject_id == 1, ]
  expect_equal(c(p1$prior_ip_count, p1$prior_ip_los_days, p1$prior_ed_count, p1$prior_os_count), c(2, 7, 1, 0))
  expect_equal(p1$prior_readmit_30d_count, 1)
  expect_equal(p1$days_since_last_encounter, 31)
  p2 <- df[df$subject_id == 2, ]
  expect_equal(p2$prior_ip_count, 0)
  expect_true(is.na(p2$days_since_last_encounter))
  expect_equal(nrow(df), 2)

  custom <- extract_prior_utilization(con, visit_concept_groups = list(acute = 9201), lookback_days = 30,
                                      readmission_group = "acute")
  expect_false("prior_ip_count" %in% names(custom))
  expect_error(extract_prior_utilization(con, anchor_date = "discharge"), "admission")
  expect_error(extract_prior_utilization(con, lookback_days = -1), "lookback_days")
  expect_error(extract_prior_utilization(con, visit_concept_groups = list("a b" = 1)), "identifier")
  expect_error(extract_prior_utilization(con, visit_concept_groups = list(x = 1)), "readmission_group")
})

# ---- inpatient labs ------------------------------------------------------------------------------------
test_that("extract_inpatient_labs applies selection rules, outlier bounds and the acute window", {
  con <- or_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES
    (7001,'Creatinine','Measurement','LOINC','Lab Test','S','2160-0',DATE '2000-01-01',DATE '2099-12-31'),
    (7002,'Lactate','Measurement','LOINC','Lab Test','S','2524-7',DATE '2000-01-01',DATE '2099-12-31')")
  or_cohort(con, list(list(1, "2020-06-01", "2020-06-10", NA)))
  meas <- function(id, concept, ts, val) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, value_as_number, measurement_type_concept_id) VALUES (%d, 1, %d, CAST(TIMESTAMP '%s' AS DATE), TIMESTAMP '%s', %s, 32817)",
      id, concept, ts, ts, val))
  }
  meas(1, 7001, "2020-05-20 08:00:00", 1.0)
  meas(2, 7001, "2020-06-02 08:00:00", 3.0)
  meas(3, 7001, "2020-06-09 08:00:00", 2.0)
  meas(4, 7001, "2020-06-10 08:00:00", 999.0)
  meas(5, 7002, "2020-05-25 08:00:00", 9.0)
  specs <- list(creatinine = "2160-0", lactate = "2524-7")
  last <- extract_inpatient_labs(con, lab_specs = specs, routine_window = "all")
  expect_equal(last$creatinine, 2)
  expect_true(is.na(last$lactate))
  expect_equal(extract_inpatient_labs(con, lab_specs = specs, selection_rule = "worst", routine_window = "stay")$creatinine, 3)
  expect_equal(extract_inpatient_labs(con, lab_specs = specs, routine_window = "lookback")$creatinine, 1)
  expect_equal(extract_inpatient_labs(con, lab_specs = specs, routine_window = "all", outlier_bounds = list())$creatinine, 999)
  expect_error(extract_inpatient_labs(con, lab_specs = specs, selection_rule = "nope"), "selection_rule")
  expect_error(extract_inpatient_labs(con, lab_specs = specs, acute_lab_subset = "troponin"), "acute_lab_subset")
})

# ---- index-diagnosis guard -----------------------------------------------------------------------------
test_that("exclude_index_diagnoses drops index-visit diagnoses and rejects discharge anchors", {
  con <- or_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  or_cohort(con, list(list(1, "2020-06-01", "2020-06-10", 20)))
  dx <- function(id, src, date, vid) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value, visit_occurrence_id) VALUES (%d, 1, 0, DATE '%s', 32020, '%s', %d)",
      id, date, src, vid))
  }
  dx(1, "I50.9", "2020-05-31", 20)
  dx(2, "E11.9", "2020-01-01", 5)
  guarded <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE)
  expect_equal(c(guarded$elix_chf, guarded$elix_dm_uncomp), c(0, 1))
  open <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE, exclude_index_diagnoses = FALSE)
  expect_equal(open$elix_chf, 1)
  expect_error(extract_elixhauser_comorbidities(con, source_and_standard = FALSE, index_date_col = "cohort_end_date"),
               "discharge")
  ok <- extract_charlson_index(con, source_and_standard = FALSE, index_date_col = "cohort_end_date",
                               exclude_index_diagnoses = FALSE)
  expect_equal(nrow(ok), 1)
})

# ---- scoring bundle ------------------------------------------------------------------------------------
test_that("omop_scoring_pipeline enforces the feature schema and round-trips", {
  x <- data.frame(a = c(1, 2, NA, 4), b = c(0, 1, 1, 0))
  fit_model <- function(m, y) {
    w <- colMeans(m)
    function(z) 1 / (1 + exp(-rowSums(sweep(z, 2, w, "-"))))
  }
  pipe <- fit_scoring_pipeline(x, c(0, 1, 1, 0), fit_model, impute = TRUE, scale = TRUE, metadata = list(cohort = "x"))
  new <- data.frame(b = 1, a = NA_real_)
  expect_length(predict_scoring_pipeline(pipe, new), 1)
  expect_error(predict_scoring_pipeline(pipe, new["a"]), "missing column")
  expect_error(predict_scoring_pipeline(pipe, cbind(new, c = 1)), "unexpected column")
  expect_error(predict_scoring_pipeline(pipe, data.frame(b = 1, a = "x")), "non-numeric")
  err <- tryCatch(predict_scoring_pipeline(pipe, new["a"]), error = function(e) e)
  expect_s3_class(err, "omop_feature_schema_error")
  expect_equal(unname(scoring_pipeline_out_of_range(pipe, data.frame(a = 100, b = 0))), 1)
  path <- save_scoring_pipeline(pipe, file.path(tempdir(), "bundle.rds"))
  again <- load_scoring_pipeline(path)
  expect_equal(again$feature_names, c("a", "b"))
  expect_equal(again$metadata$cohort, "x")
  expect_equal(predict_scoring_pipeline(again, new), predict_scoring_pipeline(pipe, new))
  expect_equal(predict_scoring_class(pipe, new, threshold = 0), 1L)
  bare <- omop_scoring_pipeline(function(m) rep(0.5, nrow(m)), c("a", "b"))
  expect_error(predict_scoring_pipeline(bare, new), "no imputer")
})
