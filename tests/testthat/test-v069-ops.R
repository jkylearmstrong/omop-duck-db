new_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  con
}
add_person <- function(con, pid) {
  DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1960, 8527, 38003564)", pid))
}
add_visit <- function(con, vid, pid, concept, s, e) {
  DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, %d, DATE '%s', DATE '%s', 32827)", vid, pid, concept, s, e))
}

SAMPLE <- function() normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"), mustWork = FALSE)

test_that("check_source reports tables, names the missing column, and strict ETL fails fast", {
  skip_if_not(dir.exists(SAMPLE()), "sample fixtures not available")
  ok <- check_source(SAMPLE())
  expect_true(ok$ok); expect_true(ok$tables$encounter$present); expect_true("death" %in% ok$skipped)
  bad <- tempfile("bad"); dir.create(bad)
  file.copy(list.files(SAMPLE(), full.names = TRUE), bad)
  enc <- utils::read.csv(file.path(bad, "encounter.csv"), colClasses = "character")
  enc$ENC_TYPE <- NULL
  utils::write.csv(enc, file.path(bad, "encounter.csv"), row.names = FALSE)
  rep <- check_source(bad)
  expect_false(rep$ok); expect_equal(rep$tables$encounter$missing_required, "ENC_TYPE")
  expect_error(check_source(bad, raise_on_error = TRUE), "encounter.*ENC_TYPE", class = "source_contract_error")
  expect_error(etl_pcornet(bad, tempfile(fileext = ".duckdb"), strict_source = TRUE), "ENC_TYPE")
})

test_that("a Parquet source directory gives the same CDM as CSV", {
  skip_if_not(dir.exists(SAMPLE()), "sample fixtures not available")
  pq <- tempfile("pq"); dir.create(pq)
  con <- DBI::dbConnect(duckdb::duckdb())
  for (f in list.files(SAMPLE(), pattern = "\\.csv$", full.names = TRUE)) {
    DBI::dbExecute(con, sprintf("COPY (SELECT * FROM read_csv_auto('%s', all_varchar=true)) TO '%s' (FORMAT PARQUET)",
                                gsub("\\\\", "/", f), gsub("\\\\", "/", file.path(pq, sub("\\.csv$", ".parquet", basename(f))))))
  }
  DBI::dbDisconnect(con, shutdown = TRUE)
  expect_true(check_source(pq, "parquet")$ok)
  a <- tempfile(fileext = ".duckdb"); b <- tempfile(fileext = ".duckdb")
  build_schema(db_path = a); build_schema(db_path = b)
  suppressMessages(utils::capture.output({ etl_pcornet(SAMPLE(), a); etl_pcornet(pq, b) }))
  ca <- DBI::dbConnect(duckdb::duckdb(), a, read_only = TRUE); cb <- DBI::dbConnect(duckdb::duckdb(), b, read_only = TRUE)
  on.exit({ DBI::dbDisconnect(ca, shutdown = TRUE); DBI::dbDisconnect(cb, shutdown = TRUE) }, add = TRUE)
  for (t in c("person", "visit_occurrence", "condition_occurrence", "measurement", "drug_exposure")) {
    q <- sprintf("SELECT * FROM %s ORDER BY ALL", t)
    expect_equal(DBI::dbGetQuery(ca, q), DBI::dbGetQuery(cb, q), info = t)
  }
})

test_that("run_dqd plausibleDateRange flags 1913 and 2100 visit dates", {
  con <- new_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  for (p in 1:3) add_person(con, p)
  add_visit(con, 1, 1, 9201, "2020-01-01", "2020-01-03")
  add_visit(con, 2, 2, 9201, "1913-05-01", "1913-05-03")
  add_visit(con, 3, 3, 9201, "2100-01-01", "2100-01-02")
  res <- run_dqd(con)
  chk <- Filter(function(r) identical(r$check_name, "plausibleDateRange") && r$cdm_table_name == "visit_occurrence" &&
                  r$cdm_field_name == "visit_start_date", res$CheckResults)[[1]]
  expect_true(chk$failed); expect_equal(chk$num_violated_rows, 2L)
  expect_equal(c(chk$num_before_lower_bound, chk$num_after_upper_bound, chk$num_denominator_rows), c(1L, 1L, 3L))
  ok <- run_dqd(con, plausible_date_range = c("1900-01-01", "2200-01-01"))
  expect_false(any(vapply(ok$CheckResults, function(r) identical(r$check_name, "plausibleDateRange") && isTRUE(r$failed), logical(1))))
})

test_that("split_by_date: union equals original, source modified only on request", {
  con <- new_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  for (p in 1:2) add_person(con, p)
  dts <- c("2019-06-01", "2021-12-31", "2022-01-01", "2023-05-05")
  for (i in 1:4) {
    add_visit(con, i, 1 + i %% 2, 9201, dts[i], dts[i])
    DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) VALUES (%d, %d, 3027018, DATE '%s', 44818702, %d)", i, 1 + i %% 2, dts[i], 70 + i))
  }
  man <- run_manifest("split")
  out <- tempfile(fileext = ".duckdb")
  rep <- split_by_date(con, "2022-01-01", out, manifest = man)
  v <- rep$tables$visit_occurrence
  expect_equal(c(v$kept, v$moved, v$source_before), c(2, 2, 4))
  expect_true(any(vapply(man$attrition, function(s) identical(s$step, "split:visit_occurrence") && s$n == 2L, logical(1))))
  expect_error(split_by_date(con, "2022-01-01", out), "already exists")
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) n FROM visit_occurrence")$n, 4)
  split_by_date(con, "2022-01-01", tempfile(fileext = ".duckdb"), remove_from_source = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) n FROM visit_occurrence")$n, 2)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) n FROM measurement")$n, 2)
  DBI::dbExecute(con, "DETACH \"_heldout_split\"") |> try(silent = TRUE)
  held <- DBI::dbConnect(duckdb::duckdb(), out, read_only = TRUE)
  on.exit(DBI::dbDisconnect(held, shutdown = TRUE), add = TRUE)
  expect_equal(format(DBI::dbGetQuery(held, "SELECT min(visit_start_date) d FROM visit_occurrence")$d), "2022-01-01")
  expect_equal(DBI::dbGetQuery(held, "SELECT count(*) n FROM person")$n, 2)
})

multi_stay <- function(con, n = 12, history = FALSE) {
  vid <- 1
  for (p in seq_len(n)) {
    add_person(con, p)
    if (history) { add_visit(con, 1000 + p, p, 9202, "2018-01-01", "2018-01-01"); add_visit(con, 2000 + p, p, 9202, "2021-06-01", "2021-06-01") }
    for (k in 1:4) { add_visit(con, vid, p, 9201, sprintf("2020-0%d-10", k), sprintf("2020-0%d-14", k)); vid <- vid + 1 }
  }
}
SIMPLE <- list(washin_days = 0, require_verified_followup = FALSE, min_age = NULL, target_outcome = "none",
               attrition = FALSE, materialise = FALSE)
define <- function(con, ...) do.call(define_study_cohort, c(list(con), list(...), SIMPLE[setdiff(names(SIMPLE), names(list(...)))]))

test_that("persist_keys: byte-identical key files and reuse instead of resampling", {
  con <- new_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  multi_stay(con)
  keys <- tempfile(fileext = ".csv")
  a <- define(con, persist_keys = keys, random_state = 1)
  first <- readBin(keys, "raw", file.info(keys)$size)
  expect_equal(nrow(a), 12); expect_equal(readLines(keys)[1], "subject_id,visit_occurrence_id")
  define(con, persist_keys = keys, random_state = 1)
  expect_identical(readBin(keys, "raw", file.info(keys)$size), first)
  cc <- define(con, persist_keys = keys, random_state = 999)
  expect_identical(readBin(keys, "raw", file.info(keys)$size), first)
  expect_equal(cc[, c("subject_id", "visit_occurrence_id")], a[, c("subject_id", "visit_occurrence_id")])
  other <- define(con, random_state = 999)
  expect_false(identical(other$visit_occurrence_id, a$visit_occurrence_id))
  expect_error(define(con, one_per_patient = FALSE), "one_per_patient")
})

test_that("deleting a key row changes only that patient", {
  con <- new_cdm(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  multi_stay(con)
  keys <- tempfile(fileext = ".csv")
  base <- define(con, persist_keys = keys, random_state = 7)
  txt <- readLines(keys); writeLines(txt[!startsWith(txt, "5,")], keys)
  changed <- define(con, persist_keys = keys, random_state = 123)
  diff_ids <- changed$subject_id[changed$visit_occurrence_id != base$visit_occurrence_id[match(changed$subject_id, base$subject_id)]]
  expect_true(all(diff_ids == 5))
  expect_equal(length(readLines(keys)), nrow(base) + 1)
})

test_that("run manifest records, verifies, and detects a changed input", {
  f <- tempfile(fileext = ".csv"); writeLines(c("a,b", "1,2"), f)
  spec <- tempfile(fileext = ".json"); writeLines("{}", spec)
  m <- run_manifest("unit", seeds = list(random_state = 42), inputs = f, feature_spec = spec)
  run_manifest_add_step(m, "start", 100); run_manifest_add_step(m, "kept", 80)
  path <- write_run_manifest(m, tempfile(fileext = ".json"))
  d <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  expect_equal(d$seeds$random_state, 42); expect_true(nzchar(d$inputs[[f]])); expect_equal(d$attrition[[2]]$n, 80)
  expect_silent(verify_manifest(path, attrition = list(list(step = "start", n = 100), list(step = "kept", n = 80))))
  expect_error(verify_manifest(path, attrition = list(list(step = "start", n = 100), list(step = "kept", n = 79))),
               "attrition", class = "manifest_verification_error")
  writeLines(c("a,b", "1,3"), f)
  expect_error(verify_manifest(path), "SHA-256 changed", class = "manifest_verification_error")
})
