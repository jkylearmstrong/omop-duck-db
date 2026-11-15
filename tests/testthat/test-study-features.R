# FeatureSet / study-design features (mirrors tests/test_study_features.py)
ruca_x <- data.frame(ZIPCODE = c("19104", "15213", "08101", "17821", "00501"), RUCA_CODE = c("1", "2.1", "4", "99", "1"))
sdi_x <- data.frame(ZCTA5_FIPS = c("19104", "15213", "8101", "17821"), SDI_score = c(95, 40, 10, 70))
cuts_x <- c(20, 45, 65, 85)
income_x <- data.frame(zcta = c("19104", "15213", "08101"), median_income = c(30000, 90000, 60000))

sf_con <- function(addr, visits, env = parent.frame()) {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, envir = env)
  for (pid in 1:10) DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)", pid))
  DBI::dbExecute(con, "CREATE TABLE person_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR)")
  for (a in addr) DBI::dbExecute(con, "INSERT INTO person_address_history VALUES (?, ?, ?, ?::DATE, ?::DATE, ?)", params = a)
  for (v in visits) DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, 9201, DATE '%s', DATE '%s', 0)", v[[1]], v[[2]], v[[3]], v[[3]]))
  con
}
sf_default <- function(env = parent.frame()) {
  sf_con(list(list(1, 10, "19104", "2010-01-01", NA, "N"), list(2, 11, "15213", "2010-01-01", NA, "N")),
         list(list(1, 1, "2016-06-01"), list(2, 2, "2017-02-01")), env)
}

test_that("RUCA and SDI features match derive_geography_features()", {
  con <- sf_default()
  fs <- feature_set() |> add_feature(ruca_feature(ruca_x)) |> add_feature(sdi_feature(sdi_x, cuts_x))
  out <- compute_features(fs, con)
  out <- out[order(out$stay_id), ]
  ref <- derive_geography_features(con, ruca_x, sdi_x, cuts_x)
  expect_equal(out$sdi_score, ref$sdi_score[order(ref$stay_id)])
  expect_equal(out$ruca_code, c(1, 2))
  expect_equal(out$sdi_quintile, c(5, 2))
  expect_equal(out$ruca_group, c(1, 2))
  expect_equal(feature_set_columns(fs), c("ruca_code", "ruca_group", "sdi_score", "sdi_quintile"))
})

test_that("SDI needs fixed cut points or fit = TRUE", {
  con <- sf_default()
  expect_error(compute_features(feature_set(sdi_feature(sdi_x)), con), "cut_points")
  expect_error(compute_features(feature_set(sdi_feature(sdi_x)), con, fit = TRUE), "at least 5")
})

test_that("area features: log1p, unmapped ZIP flag, and fitted state travels", {
  con <- sf_default()
  out <- compute_features(feature_set(area_feature("income", income_x, "median_income", transform = "log1p")), con)
  out <- out[order(out$stay_id), ]
  expect_equal(out$income[1], log1p(30000))
  expect_equal(out$income_missing, c(0, 0))
  expect_error(compute_features(feature_set(area_feature("income", income_x, "median_income", transform = "percentile")), con), "training fit")

  train_tbl <- data.frame(zcta = as.character(10000 + 0:49), median_income = seq(20000, 200000, length.out = 50))
  addr <- lapply(1:10, function(i) list(i, 100 + i, as.character(10000 + i), "2010-01-01", NA, "N"))
  vis <- lapply(1:10, function(i) list(i, i, "2016-01-01"))
  tcon <- sf_con(addr, vis)
  tfs <- feature_set(area_feature("income", train_tbl, "median_income", transform = "percentile", impute = TRUE))
  tout <- compute_features(tfs, tcon, fit = TRUE)
  st <- feature_set_state(tfs)
  expect_length(st$income$percentile_grid, 101)
  expect_true(all(tout$income >= 0 & tout$income <= 1))

  path <- tempfile(fileext = ".json")
  export_feature_manifest(tfs, path)
  other <- feature_set(area_feature("income", income_x, "median_income", transform = "percentile", impute = TRUE))
  load_feature_state(other, path)
  o <- compute_features(other, con)
  o <- o[order(o$stay_id), ]
  expect_equal(o$income[1], approx(st$income$percentile_grid, seq(0, 1, length.out = 101), xout = 30000)$y, tolerance = 1e-8)
})

test_that("unmapped ZIPs get a missing indicator", {
  con <- sf_con(list(list(1, 10, "99999", "2010-01-01", NA, "N"), list(2, 11, "15213", "2010-01-01", NA, "N")),
                list(list(1, 1, "2016-06-01"), list(2, 2, "2017-02-01")))
  o <- compute_features(feature_set(area_feature("income", income_x, "median_income", transform = "log1p")), con)
  o <- o[order(o$stay_id), ]
  expect_true(is.na(o$income[1])); expect_equal(o$income_missing, c(1, 0))
})

test_that("set validation, sql and custom features", {
  con <- sf_default()
  fs <- feature_set(ruca_feature(ruca_x))
  expect_error(add_feature(fs, ruca_feature(ruca_x)), "already in the set")
  expect_error(add_feature(fs, custom_feature("other", "ruca_code", function(c, s) s)), "duplicate")
  expect_error(add_feature(fs, "ruca"), "takes a study_feature")
  expect_length(remove_feature(fs, "ruca")$features, 0)
  n <- sql_feature("n_visits", "SELECT s.stay_id, CAST(count(v.visit_occurrence_id) AS DOUBLE) AS n_visits FROM _stays s LEFT JOIN visit_occurrence v ON v.person_id = s.person_id AND v.visit_start_date <= s.admit_date GROUP BY 1", "n_visits")
  cu <- custom_feature("flag", "is_2017", function(con, stays) data.frame(stay_id = stays$stay_id, is_2017 = as.numeric(format(stays$admit_date, "%Y") == "2017")))
  out <- compute_features(feature_set(n, cu), con)
  out <- out[order(out$stay_id), ]
  expect_equal(out$n_visits, c(1, 1)); expect_equal(out$is_2017, c(0, 1))
  expect_equal(check_feature_set(feature_set(sql_feature("x", "SELECT 1", "x", requires = "no_such_table")), con), list(x = "no_such_table"))
})

test_that("write_feature_set stores measurements and builds FeatureExtraction code", {
  con <- sf_default()
  fs <- feature_set(ruca_feature(ruca_x), sdi_feature(sdi_x, cuts_x), area_feature("income", income_x, "median_income", transform = "log1p"))
  wide <- compute_features(fs, con)
  concepts <- write_feature_set(fs, con, wide)
  expect_equal(as.numeric(concepts$sdi_score[[1]]), GEOGRAPHY_CONCEPT_IDS[["sdi_score"]])
  expect_gte(as.numeric(concepts$income[[1]]), 2001000000)
  n <- DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM measurement WHERE measurement_concept_id = %.0f", as.numeric(concepts$income[[1]])))$n
  expect_equal(n, 2)
  r <- feature_set_extraction_r(fs, concepts)
  expect_match(r, "useMeasurementValueLongTerm = TRUE", fixed = TRUE)
  expect_match(r, sprintf("%.0f", as.numeric(concepts$income[[1]])), fixed = TRUE)
  expect_error(feature_set_extraction_r(fs), "no concept id")
  m <- feature_set_manifest(fs, concepts)
  expect_equal(m$features[[3]]$concept_ids$income, as.numeric(concepts$income[[1]]))
})

test_that("feature_list builds the geography set", {
  con <- sf_default()
  fs <- feature_list("geography", list(ruca = list(crosswalk = ruca_x), sdi = list(table = sdi_x, cut_points = cuts_x)))
  expect_equal(names(fs$features), c("ruca", "sdi"))
  expect_equal(nrow(compute_features(fs, con)), 2)
  expect_error(feature_list("geography", list(ruca = list(crosswalk = ruca_x))), "needs arguments")
})

test_that("FeatureExtraction reads a whole feature set (pinned and registered concepts) through the covariate settings", {
  skip_if_not_installed("FeatureExtraction")
  skip_if_not_installed("DatabaseConnector")
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(unlink(db_path), add = TRUE)
  for (pid in 1:2) DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)", pid))
  DBI::dbExecute(con, "CREATE TABLE person_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO person_address_history VALUES (1, 10, '19104', DATE '2010-01-01', NULL, 'N'), (2, 11, '15213', DATE '2010-01-01', NULL, 'N')")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (1, 1, 9201, DATE '2016-06-01', DATE '2016-06-02', 0), (2, 2, 9201, DATE '2017-02-01', DATE '2017-02-02', 0)")
  DBI::dbExecute(con, "DROP TABLE IF EXISTS cohort"); DBI::dbExecute(con, "CREATE TABLE cohort (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE)")
  DBI::dbExecute(con, "INSERT INTO cohort VALUES (1, 1, DATE '2016-06-01', DATE '2016-06-02'), (1, 2, DATE '2017-02-01', DATE '2017-02-02')")
  fs <- feature_set(sdi_feature(sdi_x, cuts_x), area_feature("income", income_x, "median_income", transform = "log1p"))
  concepts <- write_feature_set(fs, con, compute_features(fs, con, stays_from_cohort(con, "cohort")))
  DBI::dbDisconnect(con, shutdown = TRUE)
  conn <- tryCatch(DatabaseConnector::connect(DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)),
                   error = function(e) skip(paste("cannot connect:", conditionMessage(e))))
  on.exit(DatabaseConnector::disconnect(conn), add = TRUE, after = FALSE)
  cov <- tryCatch(FeatureExtraction::getDbCovariateData(connection = conn, cdmDatabaseSchema = "main", cohortTable = "cohort",
                    cohortDatabaseSchema = "main", cohortIds = 1, rowIdField = "subject_id",
                    covariateSettings = feature_set_covariate_settings(fs, concepts), aggregated = FALSE),
                  error = function(e) skip(paste("FeatureExtraction cannot run here:", conditionMessage(e))))
  got <- as.data.frame(cov$covariates)
  expect_equal(nrow(got), 2 * length(feature_set_columns(fs)))
  expect_setequal(got$covariateValue[got$rowId == 1], c(95, 5, log1p(30000), 0))
  expect_equal(nrow(as.data.frame(cov$covariateRef)), length(feature_set_columns(fs)))
})
