SPEC_POC <- list(features = list(
  list(name = "lab_creatinine", dtype = "float", bounds = c(0.1, 30), impute = 1),
  list(name = "heart_rate", dtype = "float", bounds = c(20, 250), impute = 80),
  list(name = "prior_ip_count", dtype = "int", impute = 0)))

poc_bundle <- function() {
  write_model_bundle(file.path(tempfile(), "b"), name = "demo", version = "1", feature_spec = SPEC_POC,
                     components = list(list(kind = "logistic", filename = "glm.json",
                                            content = list(intercept = -3.5, coef = as.list(c(0.8, 0.02, 0.5))))))
}

poc_cdm <- function() {
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1950, 8527, 38003564)")
  for (r in list(c(10, "2020-01-01", "2020-01-04"), c(11, "2020-06-01", "2020-06-04"))) {
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%s, 1, 9201, DATE '%s', DATE '%s', 32827)", r[1], r[2], r[3]))
  }
  m <- list(c(3016723, "2020-06-01", 1), c(3016723, "2020-06-03", 3), c(3027018, "2020-06-01", 90), c(3027018, "2020-06-04", 70))
  for (i in seq_along(m)) {
    DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, measurement_type_concept_id) VALUES (%d, 1, %s, DATE '%s', %s, 44818702)", i, m[[i]][1], m[[i]][2], m[[i]][3]))
  }
  con
}

poc_fhir <- function() {
  obs <- function(code, value, unit) list(resource = list(resourceType = "Observation",
    code = list(coding = list(list(system = "http://loinc.org", code = code))), effectiveDateTime = "2024-01-04",
    valueQuantity = list(value = value, unit = unit)))
  list(resourceType = "Bundle", entry = list(
    list(resource = list(resourceType = "Patient", id = "p1", gender = "male", birthDate = "1950-03-02")),
    list(resource = list(resourceType = "Encounter", id = "e1", class = list(code = "IMP"), period = list(start = "2024-01-01", end = "2024-01-05"))),
    obs("2160-0", 265.2, "umol/L"), obs("8867-4", 90, "/min"),
    list(resource = list(resourceType = "Condition", code = list(coding = list(list(system = "http://snomed.info/sct", code = "44054006")))))))
}

test_that("score_fhir_bundle scores in memory, converts units and explains", {
  r <- score_fhir_bundle(poc_fhir(), poc_bundle(), explain = TRUE)
  expect_equal(r$features$lab_creatinine, 3, tolerance = 0.01)
  expect_equal(r$fhir$unmapped_codes, "http://snomed.info/sct|44054006")
  expect_equal(r$top_drivers[[1]]$name, "lab_creatinine")
  expect_gt(r$top_drivers[[1]]$impact, 0)
  imp <- vapply(r$top_drivers, function(d) abs(d$impact), numeric(1))
  expect_false(is.unsorted(rev(imp)))
  expect_error(score_fhir_bundle(list(resourceType = "Patient"), poc_bundle()), "Bundle")
})

test_that("score_encounter(explain = TRUE) adds top_drivers", {
  con <- poc_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  b <- poc_bundle()
  expect_null(score_encounter(con, 11, b)$top_drivers)
  r <- score_encounter(con, 11, b, explain = TRUE, top_k = 2)
  expect_lte(length(r$top_drivers), 2)
  expect_equal(unname(r$top_drivers[[1]]$value), 3)
})

test_that("check_unit_sanity flags a umol/L creatinine feed", {
  df <- data.frame(lab_creatinine = rep(88, 50), heart_rate = rep(82, 50))
  expect_warning(res <- check_unit_sanity(df), "lab_creatinine")
  expect_true(res$flag[res$feature == "lab_creatinine"])
  expect_match(res$hint[res$feature == "lab_creatinine"], "umol")
  expect_false(res$flag[res$feature == "heart_rate"])
  expect_silent(check_unit_sanity(data.frame(lab_creatinine = rep(1.1, 50))))
})

test_that("write_drift_report writes a self-contained page", {
  set.seed(1)
  n <- 600
  sc <- data.frame(score_date = as.Date("2023-01-01") + sample(0:359, n, TRUE), prob = rbeta(n, 2, 8))
  sc$outcome_flag <- as.integer(runif(n) < sc$prob)
  lg <- monitor_scores(sc, rbeta(1000, 2, 8), as_of = "2024-06-01", min_events = 5)
  lg$model_version <- "1"
  out <- write_drift_report(lg, tempfile(fileext = ".html"), scores = sc)
  page <- paste(readLines(out, warn = FALSE), collapse = "\n")
  expect_match(page, "<svg"); expect_match(page, "Population stability index"); expect_match(page, "Calibration")
  expect_false(grepl("<script", page))
})

test_that("etl_pcornet(mode = 'append') adds only new rows and is idempotent", {
  src <- tempfile(); dir.create(src)
  writeLines(c("PATID,SEX,BIRTH_DATE,RACE,HISPANIC", "P1,F,1960-01-01,05,N"), file.path(src, "demographic.csv"))
  writeLines(c("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE", "E1,P1,IP,2022-08-01,2022-08-05"), file.path(src, "encounter.csv"))
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  capture.output(etl_pcornet(src, db, key_check = "ignore"))
  writeLines(c("PATID,SEX,BIRTH_DATE,RACE,HISPANIC", "P1,F,1960-01-01,05,N", "P2,M,1970-01-01,03,N"), file.path(src, "demographic.csv"))
  writeLines(c("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE", "E1,P1,IP,2022-08-01,2022-08-05",
               "E2,P2,IP,2022-11-01,2022-11-04", "E3,P1,ED,2022-10-02,2022-10-02"), file.path(src, "encounter.csv"))
  capture.output(etl_pcornet(src, db, mode = "append", since = "2022-10-01", key_check = "ignore"))
  capture.output(etl_pcornet(src, db, mode = "append", since = "2022-10-01", key_check = "ignore"))
  con <- DBI::dbConnect(duckdb::duckdb(), db, read_only = TRUE); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) n FROM person")$n, 2)
  v <- DBI::dbGetQuery(con, "SELECT visit_occurrence_id, visit_source_value FROM visit_occurrence ORDER BY 1")
  expect_equal(v$visit_source_value, c("E1", "E2", "E3")); expect_equal(as.integer(v$visit_occurrence_id), 1:3)
  expect_error(etl_pcornet(src, db, mode = "append"), "since")
})

test_that("score_fhir_bundle reads BP panel components and prior Encounters", {
  b <- poc_fhir()
  comp <- function(code, v) list(code = list(coding = list(list(system = "http://loinc.org", code = code))), valueQuantity = list(value = v, unit = "mmHg"))
  b$entry <- c(b$entry, list(
    list(resource = list(resourceType = "Observation", effectiveDateTime = "2024-01-04",
                         code = list(coding = list(list(system = "http://loinc.org", code = "85354-9"))),
                         component = list(comp("8480-6", 130), comp("8462-4", 80)))),
    list(resource = list(resourceType = "Encounter", id = "h1", class = list(code = "IMP"), period = list(start = "2023-12-01", end = "2023-12-04"))),
    list(resource = list(resourceType = "Encounter", id = "h2", class = list(code = "IMP"), period = list(start = "2023-10-01", end = "2023-10-03")))))
  r <- score_fhir_bundle(b, poc_bundle())
  expect_equal(r$fhir$observations_skipped, 0)
  expect_equal(r$fhir$encounter_id, "e1")
  expect_equal(r$features$prior_ip_count, 2)
})
