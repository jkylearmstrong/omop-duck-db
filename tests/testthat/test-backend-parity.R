# BigQuery vs DuckDB and R vs Python parity helpers. The DuckDB run uses the same small CDM as the Python test
# (tests/test_backend_parity.py), including ids above 2^31 and 2^53, so nothing may truncate them.

beyond_double <- "9007199254740993"  # 2^53 + 1
beyond_int32 <- "2147483653"        # 2^31 + 5

parity_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  capture.output(build_schema(db))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  ex <- function(...) DBI::dbExecute(con, paste0(...))
  person <- function(pid, sex, yob, race) {
    ex("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (",
       pid, ", ", sex, ", ", yob, ", ", race, ", 0)")
    ex("INSERT INTO observation_period VALUES (", pid, ", ", pid, ", DATE '2016-01-01', DATE '2022-12-31', 0)")
  }
  n <- new.env(); n$c <- 0; n$d <- 0; n$m <- 0; n$v <- 0
  cond <- function(pid, cid, d) { n$c <- n$c + 1
    ex("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, ",
       "condition_type_concept_id) VALUES (", n$c, ", ", pid, ", ", cid, ", DATE '", d, "', 0)") }
  drug <- function(pid, cid, d) { n$d <- n$d + 1
    ex("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, ",
       "drug_type_concept_id) VALUES (", n$d, ", ", pid, ", ", cid, ", DATE '", d, "', DATE '", d, "', 0)") }
  meas <- function(pid, value, d) { n$m <- n$m + 1
    ex("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, ",
       "value_as_number) VALUES (", n$m, ", ", pid, ", 3004410, DATE '", d, "', 0, ", value, ")") }
  visit <- function(pid, cid, s, e) { n$v <- n$v + 1
    ex("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, ",
       "visit_type_concept_id) VALUES (", beyond_double, " + ", n$v, ", ", pid, ", ", cid, ", DATE '", s, "', DATE '", e, "', 0)") }
  ex("INSERT INTO concept_ancestor VALUES (201820, 201820, 0, 0), (201820, 443238, 1, 1), (201820, 4024659, 1, 1), ",
     "(201820, 4058243, 2, 2), (4024659, 4024659, 0, 0), (4024659, 4058243, 1, 1), (21600712, 21600712, 0, 0), (21600712, 1503297, 3, 3)")
  person(beyond_double, 8507, 1950, 8527)
  cond(beyond_double, 443238, "2019-03-01")
  visit(beyond_double, 9201, "2018-06-01", "2018-06-05")
  visit(beyond_double, 9201, "2018-06-20", "2018-06-25")
  visit(beyond_double, 9203, "2018-09-01", "2018-09-01")
  visit(beyond_double, 9202, "2018-12-01", "2018-12-01")
  visit(beyond_double, 9202, "2017-01-01", "2017-01-01")
  visit(beyond_double, 9201, "2019-03-01", "2019-03-04")
  person(beyond_int32, 8532, 1990, 8516)
  cond(beyond_int32, 4024659, "2019-04-01")
  cond(beyond_int32, 4058243, "2019-05-01")
  person(3, 8532, 1960, 8516)
  drug(3, 1503297, "2020-01-10")
  person(4, 8507, 1970, 8515)
  meas(4, 6.0, "2019-01-01")
  meas(4, 7.1, "2019-05-05")
  visit(4, 262, "2019-04-01", "2019-04-02")
  person(5, 8507, 2012, 8527)
  cond(5, 201820, "2022-01-01")
  person(6, 0, 1980, 0)
  cond(6, 201820, "2015-06-01")
  cond(6, 201820, "2018-02-02")
  cond(6, 201820, "2018-03-03")
  con
}

test_that("run_duckdb_demo builds the cohort and features with exact 64-bit ids", {
  con <- parity_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  res <- run_duckdb_demo(con)
  expect_setequal(res$cohort$subject_id, c(beyond_double, "3", "4", "6"))
  starts <- stats::setNames(as.character(res$cohort$cohort_start_date), res$cohort$subject_id)
  expect_equal(unname(starts[c("4", "6")]), c("2019-05-05", "2018-02-02"))
  p1 <- res$features[res$features$subject_id == beyond_double, ]
  expect_equal(nrow(p1), 1L)
  expect_equal(as.numeric(p1[c("age_at_index", "prior_ip_count", "prior_ed_count", "prior_os_count", "prior_ip_os_ed_count",
                               "readmission_count_twelve_months")]), c(69, 2, 1, 1, 4, 1))
  expect_equal(c(p1$sex, p1$race), c("Male", "White"))
  expect_equal(nrow(check_id_precision(res$features)), 0L)
  types <- DBI::dbGetQuery(con, "SELECT data_type FROM information_schema.columns WHERE column_name = 'subject_id'")
  expect_true(all(types$data_type == "BIGINT"))
  expect_true(compare_cohorts(res$cohort, res$cohort)$match)
  expect_true(compare_features(res$features, res$features)$match)
})

test_that("the R and Python DuckDB paths share one features template", {
  expect_true(file.exists(test_path("..", "..", "inst", "sql", "duckdb_demo_features.sql")) ||
                nzchar(system.file("sql", "duckdb_demo_features.sql", package = "omopduckdb")))
})

test_that("compare_cohorts and compare_features find differences", {
  a <- data.frame(subject_id = c("1", beyond_double), cohort_start_date = as.Date(c("2020-01-01", "2020-02-01")),
                  cohort_end_date = as.Date(c("2020-01-01", "2020-02-01")))
  b <- a; b$cohort_end_date[2] <- as.Date("2030-01-01")
  r <- compare_cohorts(a, b)
  expect_true(r$same_subjects); expect_false(r$match); expect_equal(r$n_rows_only_left, 1L)
  r <- compare_cohorts(a, a[1, ], examples = 3L)
  expect_false(r$same_subjects); expect_equal(r$examples_only_left, beyond_double)
  expect_true(compare_cohorts(data.frame(subject_id = 3, cohort_start_date = "2020-01-01", cohort_end_date = "2020-01-01"),
                              data.frame(subject_id = 3L, cohort_start_date = as.Date("2020-01-01"), cohort_end_date = as.Date("2020-01-01")))$match)
  expect_error(compare_cohorts(data.frame(subject_id = 2^53 + 2, cohort_start_date = "x", cohort_end_date = "x"), a), "2\\^53")

  f <- data.frame(subject_id = c("1", "2"), age_at_index = c(50, 60), sex = c("Male", "Female"),
                  index_date = as.Date(c("2020-01-01", "2020-01-02")))
  g <- f; g$age_at_index[1] <- 51; g$sex[2] <- "Other/Unknown"
  r <- compare_features(f, g)
  expect_false(r$match)
  expect_equal(r$columns$age_at_index$n_mismatch, 1L); expect_equal(r$columns$sex$n_mismatch, 1L)
  expect_equal(r$columns$index_date$n_mismatch, 0L)
  expect_true(compare_features(f, transform(f, age_at_index = age_at_index + 0.4), columns = "age_at_index", tolerance = 0.5)$match)
  expect_false(compare_features(f, f[1, ])$match)
  expect_error(compare_features(rbind(f, f), f), "not unique")
})

test_that("compare_column_types flags narrowed ids and mismatches", {
  bq <- data.frame(table_name = c(rep("person", 5), "visit_occurrence"),
                   column_name = c("person_id", "gender_concept_id", "year_of_birth", "person_source_value", "birth_datetime",
                                   "visit_occurrence_id"),
                   data_type = c("INT64", "INT64", "INT64", "STRING", "DATETIME", "INT64"))
  dk <- data.frame(table_name = c(rep("PERSON", 5), "visit_occurrence"),
                   column_name = c("person_id", "gender_concept_id", "year_of_birth", "person_source_value", "birth_datetime",
                                   "admitted_from_concept_id"),
                   data_type = c("INTEGER", "INTEGER", "INTEGER", "DOUBLE", "TIMESTAMP", "INTEGER"))
  r <- compare_column_types(bq, dk)
  st <- stats::setNames(r$status, paste(r$table_name, r$column_name, sep = "."))
  expect_equal(unname(st[c("person.person_id", "person.gender_concept_id", "person.person_source_value", "person.birth_datetime",
                           "visit_occurrence.visit_occurrence_id", "visit_occurrence.admitted_from_concept_id")]),
               c("id_narrowed", "ok", "type_mismatch", "ok", "only_bigquery", "only_duckdb"))
  dk$data_type[1] <- "BIGINT"
  expect_equal(compare_column_types(bq[1, ], dk[1, ])$status, "ok")
})

test_that("the shipped DDL keeps every key id of the demo tables 64-bit", {
  db <- tempfile(fileext = ".duckdb")
  capture.output(build_schema(db))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  dk <- DBI::dbGetQuery(con, paste("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_name IN",
                                   "('person', 'observation_period', 'visit_occurrence', 'condition_occurrence', 'drug_exposure',",
                                   "'measurement', 'concept_ancestor')"))
  to_bq <- c(BIGINT = "INT64", INTEGER = "INT64", VARCHAR = "STRING", DATE = "DATE", TIMESTAMP = "DATETIME", DOUBLE = "FLOAT64")
  bq <- transform(dk, data_type = unname(to_bq[sub("\\(.*$", "", data_type)]))
  r <- compare_column_types(bq, dk)
  expect_true(all(r$status == "ok"), info = paste(r$column_name[r$status != "ok"], collapse = ", "))
})

test_that("check_id_precision accepts integer64 and text, flags doubles and R integers", {
  ok <- data.frame(person_id = beyond_double, gender_concept_id = 8507L, stringsAsFactors = FALSE)
  if (requireNamespace("bit64", quietly = TRUE)) ok$visit_occurrence_id <- bit64::as.integer64(beyond_double)
  expect_equal(nrow(check_id_precision(ok)), 0L)
  bad <- data.frame(person_id = 1, visit_occurrence_id = 1L, x = 1.5)
  expect_equal(check_id_precision(bad)$column, c("person_id", "visit_occurrence_id"))
})

test_that("BigQuery INT64 columns keep key ids as integer64 and turn counts into integers", {
  skip_if_not_installed("bit64")
  df <- data.frame(subject_id = bit64::as.integer64(beyond_double), prior_ip_count = bit64::as.integer64(2),
                   gender_concept_id = bit64::as.integer64(8507))
  out <- omopduckdb:::.bq_int64_columns(df)
  expect_true(inherits(out$subject_id, "integer64"))
  expect_identical(as.character(out$subject_id), beyond_double)
  expect_identical(out$prior_ip_count, 2L)
  expect_identical(out$gender_concept_id, 8507L)
})

test_that("demo outputs compare across R-style and Python-style files", {
  feats <- data.frame(subject_id = c(beyond_double, "3"), index_date = c("2019-03-01", "2020-01-10"), age_at_index = c(69, 60),
                      sex = c("Male", "Female"), race = c("White", "Black or African American"),
                      prior_ip_os_ed_count = c(4, 0), readmission_count_twelve_months = c(1, 0), stringsAsFactors = FALSE)
  s <- summarize_bigquery_demo(feats, NULL)
  r_dir <- tempfile(); py_dir <- tempfile(); dir.create(r_dir); dir.create(py_dir)
  jsonlite::write_json(c(s, list(run = list(billing_project = "a"))), file.path(r_dir, "demo_summary.json"),
                       auto_unbox = TRUE, null = "null", digits = NA)
  s2 <- s; s2$age_at_index$mean <- s2$age_at_index$mean + 0.01
  jsonlite::write_json(c(s2, list(run = list(billing_project = "b"))), file.path(py_dir, "demo_summary.json"),
                       auto_unbox = TRUE, null = "null", digits = NA)
  utils::write.csv(feats, file.path(r_dir, "demo_features.csv"), row.names = FALSE)
  writeLines(c("subject_id,index_date,age_at_index,sex,race,prior_ip_os_ed_count,readmission_count_twelve_months",
               paste0(beyond_double, ",2019-03-01,69,Male,White,4,1"), "3,2020-01-10,60,Female,Black or African American,0,0"),
             file.path(py_dir, "demo_features.csv"))
  res <- compare_demo_outputs(r_dir, py_dir)
  expect_true(res$match, info = paste(res$summary_differences, collapse = "; "))
  s2$n_cohort <- 3L; s2$sex$Male$n <- 2L
  jsonlite::write_json(s2, file.path(py_dir, "demo_summary.json"), auto_unbox = TRUE, null = "null", digits = NA)
  d <- compare_demo_summaries(file.path(r_dir, "demo_summary.json"), file.path(py_dir, "demo_summary.json"))
  expect_true("n_cohort: 2 vs 3" %in% d)
  expect_true(any(startsWith(d, "sex.Male.n")))
})

test_that("the BigQuery sample copy reads one person sample and keeps ids exact in DuckDB", {
  skip_if_not_installed("bit64")
  sql <- omopduckdb:::.demo_sample_sql("bigquery-public-data.cms_synthetic_patient_data_omop", 1000L, 7L)
  expect_setequal(names(sql), c("person", "observation_period", "visit_occurrence", "condition_occurrence", "drug_exposure",
                                "measurement", "concept_ancestor"))
  expect_true(all(grepl("MOD(person_id, 1000) = 7", sql, fixed = TRUE)))
  expect_true(all(grepl("`bigquery-public-data.cms_synthetic_patient_data_omop.", sql, fixed = TRUE)))
  expect_error(omopduckdb:::.demo_sample_sql("x.y", 10L, 10L), "sample_remainder")
  # the public SynPUF copy has neither visit_occurrence nor measurement
  synpuf <- c("person", "observation_period", "condition_occurrence", "drug_exposure", "concept_ancestor", "concept")
  seen <- character()
  runner <- list(fetch = function(q) {
    seen <<- c(seen, q)
    if (grepl("INFORMATION_SCHEMA", q)) return(data.frame(table_name = synpuf))
    data.frame(person_id = bit64::as.integer64(beyond_double), year_of_birth = 1950L, d = as.Date("2020-01-01"))
  })
  db <- tempfile(fileext = ".duckdb")
  n <- copy_demo_sample_from_bigquery("my-proj", db_path = db, runner = runner)
  expect_equal(length(seen), 6L)  # the table listing, then five copies
  expect_setequal(n$table, c("person", "observation_period", "condition_occurrence", "drug_exposure", "concept_ancestor"))
  expect_false(any(grepl("measurement|visit_occurrence", seen)))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expect_equal(DBI::dbGetQuery(con, "SELECT CAST(person_id AS VARCHAR) AS id FROM person")$id, beyond_double)
  expect_equal(DBI::dbGetQuery(con, "SELECT data_type FROM information_schema.columns WHERE table_name = 'person' AND column_name = 'person_id'")$data_type, "BIGINT")
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM visit_occurrence")$n, 0)  # empty, from the CDM schema
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM measurement")$n, 0)
})

test_that("the sample SQL keeps measurement when the source has it", {
  sql <- omopduckdb:::.demo_sample_sql("p.d", 10L, 0L)
  expect_true(all(c("visit_occurrence", "measurement") %in% names(sql)))
  expect_true(grepl("measurement_concept_id", sql[["concept_ancestor"]], fixed = TRUE))
})
