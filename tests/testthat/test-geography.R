library(omopduckdb)

ruca <- data.frame(ZIPCODE = c("19104", "15213", "08101", "17821"), RUCA_CODE = c("1", "2.1", "4", "99"))
sdi <- data.frame(ZCTA5_FIPS = c("19104", "15213", "8101", "17821"), SDI_score = c(95, 40, 10, 70))
cuts <- c(20, 45, 65, 85)

geo_con <- function(env = parent.frame()) {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, envir = env)
  for (pid in 1:2) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)", pid))
  }
  DBI::dbExecute(con, "CREATE TABLE person_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR)")
  con
}
add_addr <- function(con, ...) {
  a <- list(...)
  DBI::dbExecute(con, "INSERT INTO person_address_history VALUES (?, ?, ?, ?::DATE, ?::DATE, ?)", params = a)
}
add_visit <- function(con, vid, pid, d) {
  DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, 9201, DATE '%s', DATE '%s', 0)", vid, pid, d, d))
}

test_that("ruca_category and SDI binning follow the reference coding with fixed cut points", {
  expect_equal(ruca_category(c(1, "2.1", 4.2, 99, NA, 0, "x")),
               c("RUCA: Metropolitan (1.0)", "RUCA: Micropolitan (2.0)", "RUCA: Small Town / Rural (3+)",
                 "RUCA: Small Town / Rural (3+)", rep("RUCA: Missing / Unknown", 3)))
  expect_equal(ruca_category(99, code_99_missing = TRUE), "RUCA: Missing / Unknown")
  expect_equal(fit_sdi_cut_points(1:100), c(20.8, 40.6, 60.4, 80.2))
  q <- assign_sdi_quintile(c(5, 20, 44.9, 45, 99, NA), cuts)
  expect_equal(q[1:5], c("SDI Quintile 1 (Least Deprived)", "SDI Quintile 2", "SDI Quintile 2", "SDI Quintile 3", "SDI Quintile 5 (Most Deprived)"))
  expect_equal(q[6], "SDI: Missing / Unknown")
  for (bad in list(1:3, c(1, 1, 2, 3), c(4, 3, 2, 1), c(1, 2, 3, NA))) {
    expect_error(validate_sdi_cut_points(bad), "sdi_cut_points must be four")
  }
  expect_error(fit_sdi_cut_points(1:2), "at least 5")
})

test_that("readers normalise ZIPs and headers", {
  expect_setequal(read_sdi_table(sdi)$zip5, c("19104", "15213", "08101", "17821"))
  expect_equal(read_ruca_crosswalk(ruca)$ruca_code[2], 2.1)
  expect_error(read_ruca_crosswalk(data.frame(a = 1)), "RUCA crosswalk needs")
})

test_that("the address in effect at admission wins, with the reference fallbacks", {
  con <- geo_con()
  add_addr(con, 1, 10, "19104", "2010-01-01", "2014-12-31", "N")
  add_addr(con, 1, 11, "15213", "2015-01-01", NA, "N")
  add_addr(con, 1, 12, "08101", "2020-01-01", NA, "Y")
  add_addr(con, 2, 13, NA, "2000-01-01", NA, "Y")
  add_addr(con, 2, 14, "17821", "2030-01-01", NA, "N")
  add_visit(con, 1, 1, "2016-06-01"); add_visit(con, 2, 1, "2012-06-01")
  add_visit(con, 3, 1, "2008-01-01"); add_visit(con, 4, 2, "2021-03-03")
  g <- derive_geography_features(con, ruca, sdi, cuts)
  rownames(g) <- g$stay_id
  expect_equal(g[c("1", "2", "3", "4"), "zip5"], c("15213", "19104", "08101", "17821"))
  expect_equal(g[c("1", "2", "3", "4"), "rule"], c("in_effect", "in_effect", "earliest_after", "earliest_after"))
  expect_equal(g["1", "ruca_category"], "RUCA: Micropolitan (2.0)")
  expect_equal(g["1", "sdi_quintile"], "SDI Quintile 2")
  expect_equal(g["4", "ruca_category"], "RUCA: Small Town / Rural (3+)")
  expect_equal(attr(g, "sdi_cut_points"), cuts)
})

test_that("missing and unmatched ZIPs are unknown; cut points are mandatory unless fitting", {
  con <- geo_con()
  add_addr(con, 1, 1, NA, "2000-01-01", NA, "Y")
  add_addr(con, 2, 2, "99999", "2000-01-01", NA, "Y")
  add_visit(con, 1, 1, "2012-01-01"); add_visit(con, 2, 2, "2012-01-01")
  g <- derive_geography_features(con, ruca, sdi, cuts)
  expect_equal(g$rule[g$stay_id == 1], "no_zip")
  expect_equal(g$ruca_category, rep("RUCA: Missing / Unknown", 2))
  expect_equal(g$sdi_quintile, rep("SDI: Missing / Unknown", 2))
  expect_error(derive_geography_features(con, ruca, sdi), "sdi_cut_points is required")
  expect_error(derive_geography_features(con, ruca, sdi, cuts, address_table = "x; DROP"), "invalid address table")
})

test_that("person.location_id is the fallback and explicit stays are validated", {
  con <- geo_con()
  DBI::dbExecute(con, "INSERT INTO location (location_id, zip) VALUES (1, '19104-1234')")
  DBI::dbExecute(con, "UPDATE person SET location_id = 1 WHERE person_id = 2")
  stays <- data.frame(person_id = c(2, 1), admit_date = c("2016-01-01", "2016-01-01"))
  g <- derive_geography_features(con, ruca, sdi, cuts, stays = stays)
  expect_equal(g$rule, c("person_location", "no_zip"))
  expect_equal(g$sdi_quintile[1], "SDI Quintile 5 (Most Deprived)")
  expect_error(derive_geography_features(con, ruca, sdi, cuts, stays = data.frame(person_id = 1)), "missing column")
  expect_error(derive_geography_features(con, ruca, sdi, cuts, stays = cbind(stays, stay_id = 1)), "unique")
  all <- data.frame(zip5 = sprintf("%05d", 1:10), sdi = 1:10)
  for (i in 1:10) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)", 100 + i))
    add_addr(con, 100 + i, 1, sprintf("%05d", i), NA, NA, "Y")
  }
  g <- derive_geography_features(con, data.frame(zip = all$zip5, ruca = 1), all, fit_cut_points = TRUE,
                                 stays = data.frame(person_id = 101:110, admit_date = "2012-01-01"))
  expect_equal(attr(g, "sdi_cut_points"), c(2.8, 4.6, 6.4, 8.2))
  expect_equal(length(unique(g$sdi_quintile)), 5)
})

test_that("load_location writes a dated, idempotent person_address_history", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, add = TRUE)
  omopduckdb:::load_mapping_macros(con)
  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
  omopduckdb:::.load_person(con, fixture_dir)
  load_location(con, fixture_dir)
  n <- DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person_address_history")$n
  expect_gte(n, 3)
  load_location(con, fixture_dir)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person_address_history")$n, n)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person_address_history WHERE person_id NOT IN (SELECT person_id FROM person)")$n, 0)
})

test_that("load_location repairs an unquoted comma in ADDRESS_CITY instead of shifting columns", {
  src <- tempfile(); dir.create(src)
  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
  file.copy(list.files(fixture_dir, full.names = TRUE), src)
  pat <- utils::read.csv(file.path(src, "demographic.csv"), colClasses = "character")[[1]][1]
  writeLines(c(
    "ADDRESSID,PATID,ADDRESS_USE,ADDRESS_TYPE,ADDRESS_PREFERRED,ADDRESS_CITY,ADDRESS_STATE,ADDRESS_ZIP5,ADDRESS_ZIP9,ADDRESS_PERIOD_START,ADDRESS_PERIOD_END",
    sprintf("1,%s,HO,PO,Y,Philadelphia,PA,19104,,01JAN2010,31DEC2012", pat),
    sprintf("2,%s,HO,PO,N,St. Louis, MO,MO,63101,,01JAN2013,", pat)), file.path(src, "lds_address_history.csv"))
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, add = TRUE)
  omopduckdb:::load_mapping_macros(con)
  omopduckdb:::.load_person(con, src)
  load_location(con, src)
  loc <- DBI::dbGetQuery(con, "SELECT city, state, zip FROM location WHERE state = 'MO'")
  expect_equal(unlist(loc[1, ]), c(city = "St. Louis, MO", state = "MO", zip = "63101"))
  h <- DBI::dbGetQuery(con, "SELECT address_start, address_end FROM person_address_history WHERE zip5 = '63101'")
  expect_equal(nrow(h), 1)
  expect_equal(as.character(h$address_start), "2013-01-01")
  expect_true(is.na(h$address_end))
})

test_that("transfer exclusion is opt-in, keeps AMA, and the diabetes cohort turns it on", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, add = TRUE)
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1960, 0, 0)", pid))
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) VALUES (%d, %d, DATE '2015-01-01', DATE '2022-01-01', 0)", pid, pid))
  }
  dis <- c(8536, 8717, 4155309)
  for (i in 1:3) {
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, discharged_to_concept_id) VALUES (%d, %d, 9201, DATE '2020-03-01', DATE '2020-03-05', 0, %d)", i, i, dis[i]))
  }
  run <- function(...) define_study_cohort(con, visit_type = "inpatient", target_outcome = "none", washin_days = 0,
                                           require_verified_followup = FALSE, materialise = FALSE, ...)
  expect_equal(sort(run()$subject_id), 1:3)
  expect_equal(sort(run(transfer_discharge_concept_ids = 8717)$subject_id), c(1L, 3L))
  expect_equal(sort(run(transfer_discharge_concept_ids = integer(0))$subject_id), 1:3)
  expect_equal(formals(build_ederri_diabetes_cohort)$transfer_discharge_concept_ids, 8717)
})
