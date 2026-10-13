# Feature request 3: staged wide loader, vectorized datetime normalizer, dual-strategy medication classifier +
# auditor, path sanitizer, RAM/disk governor, and the phenotype cohort DSL (R mirror of
# tests/test_request3_features.py).

winify <- function(p) if (.Platform$OS.type == "windows") gsub("/", "\\\\", p, fixed = TRUE) else p

new_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  list(con = con, db = db)
}

add_people <- function(con, n) {
  for (i in seq_len(n)) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (%d, 8507, 1960, 8527, 38003563, 'P%d')",
      i, i))
  }
}

# ---- path sanitizer ------------------------------------------------------------------------------------
test_that("sanitize_sql_path gives forward slashes, absolute paths, and keeps URIs", {
  out <- sanitize_sql_path("C:\\data\\site a\\vital.csv")
  expect_false(grepl("\\\\", out))
  expect_equal(sanitize_sql_path("s3://bucket/key/*.parquet"), "s3://bucket/key/*.parquet")
  expect_true(grepl("^(/|[A-Za-z]:)", sanitize_sql_path("relative/file.csv")))
  expect_match(sql_path_literal("o'brien.csv"), "''", fixed = TRUE)
})

test_that("read_csv_sql / attach_database / sql_copy_to work with awkward paths", {
  dir <- file.path(tempdir(), "dir with space")
  dir.create(dir, showWarnings = FALSE)
  csv <- file.path(dir, "o'brien.csv")
  writeLines(c("a,b", "1,2"), csv)
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expect_equal(DBI::dbGetQuery(con, paste("SELECT COUNT(*) AS n FROM", read_csv_sql(csv)))$n, 1)

  other <- file.path(dir, "other.duckdb")
  c2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = other)
  DBI::dbExecute(c2, "CREATE TABLE t AS SELECT 1 AS x")
  DBI::dbDisconnect(c2, shutdown = TRUE)
  attach_database(con, winify(other), "other", read_only = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT x FROM other.t")$x, 1)
  out <- file.path(dir, "out.parquet")
  sql_copy_to(con, "(SELECT * FROM other.t)", out)
  expect_true(file.exists(out))
})

# ---- resources -----------------------------------------------------------------------------------------
test_that("configure_resources sets limits and validates input", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  spill <- file.path(tempdir(), "spill")
  cfg <- configure_resources(con, memory_fraction = 0.5, temp_directory = spill, threads = 2)
  expect_equal(cfg$threads, 2L)
  expect_true(dir.exists(spill))
  expect_equal(as.integer(DBI::dbGetQuery(con, "SELECT current_setting('threads') AS v")$v), 2L)
  expect_match(DBI::dbGetQuery(con, "SELECT current_setting('temp_directory') AS v")$v, "spill$")
  expect_error(configure_resources(con, memory_fraction = 1.5), "memory_fraction")
  auto <- configure_resources(con)
  expect_gte(auto$threads, 1L)
  expect_true(dir.exists(auto$temp_directory))
})

# ---- vectorized datetime -------------------------------------------------------------------------------
messy_con <- function() {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE t AS SELECT * FROM (VALUES
    ('05JAN2020','4:15'),('2020-01-05','16:30:15'),('01/05/2020','4:15 PM'),('01/05/2020','12:05 AM'),
    ('2020-01-05',NULL),('2020-01-05','7'),('05JAN2020','0415'),('garbage','1:00'),(NULL,'1:00')) v(d, t)")
  con
}

test_that("vectorized_strptime normalizes times and honors on_error", {
  con <- messy_con()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  got <- DBI::dbGetQuery(con, paste("SELECT", vectorized_strptime("d", "t"), "AS ts FROM t"))$ts
  fmt <- format(got, "%Y-%m-%d %H:%M:%S", tz = "UTC")
  expect_equal(fmt[1:7], c("2020-01-05 04:15:00", "2020-01-05 16:30:15", "2020-01-05 16:15:00",
                           "2020-01-05 00:05:00", "2020-01-05 00:00:00", "2020-01-05 07:00:00", "2020-01-05 04:15:00"))
  expect_true(is.na(got[8]) && is.na(got[9]))

  dflt <- DBI::dbGetQuery(con, paste("SELECT", vectorized_strptime("d", "t", on_error = "default_date"), "AS ts FROM t"))$ts
  expect_equal(format(dflt[8], "%Y-%m-%d", tz = "UTC"), "1900-01-01")
  expect_true(is.na(dflt[9]))
  expect_error(DBI::dbGetQuery(con, paste("SELECT", vectorized_strptime("d", "t", on_error = "raise"), "FROM t")), "garbage")
  expect_error(vectorized_strptime("d", "t", on_error = "explode"))
})

test_that("custom formats and the installed macro work", {
  con <- messy_con()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  iso <- DBI::dbGetQuery(con, paste("SELECT", vectorized_strptime("d", NULL, formats = "%Y-%m-%d"), "AS ts FROM t"))$ts
  expect_true(is.na(iso[1]))
  expect_equal(format(iso[2], "%Y-%m-%d", tz = "UTC"), "2020-01-05")
  install_vectorized_strptime(con)
  expect_equal(format(DBI::dbGetQuery(con, "SELECT vectorized_strptime(d, t) AS ts FROM t LIMIT 1")$ts, "%H:%M", tz = "UTC"), "04:15")
  expect_equal(DBI::dbGetQuery(con, "SELECT normalize_time('4:15') AS v")$v, "04:15")
})

# ---- wide loader ---------------------------------------------------------------------------------------
vital_csv <- function() {
  p <- tempfile(fileext = ".csv")
  writeLines(c(
    "PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,SYSTOLIC,DIASTOLIC,BMI,ORIGINAL_BMI",
    "P1,E1,05JAN2020,4:15,120,80,25.5,",
    "P1,E1,06JAN2020,,999,85,2,",
    "P2,E2,2020-02-01,13:00,130,,31.0,31.0",
    "NOPE,E3,2020-02-01,13:00,140,90,22,"), p)
  p
}

test_that("load_wide_measurement stages and inserts one concept per committed pass", {
  cdm <- new_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  add_people(con, 2)
  report <- load_wide_measurement(con, vital_csv(), DEFAULT_VITAL_CONCEPT_MAPPING,
                                  value_plausibility_filters = DEFAULT_VITAL_PLAUSIBILITY_FILTERS,
                                  missing_columns = "skip")
  by <- split(report, report$source_column)
  sbp <- by$SYSTOLIC
  expect_equal(c(sbp$rows_non_null, sbp$rows_out_of_range, sbp$rows_unmatched_person, sbp$rows_inserted), c(4, 1, 1, 2))
  expect_equal(by$DIASTOLIC$rows_inserted, 2)
  expect_equal(by$BMI$rows_out_of_range, 1)
  expect_equal(by$ORIGINAL_BMI$rows_inserted, 1)
  total <- DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM measurement")$n
  expect_equal(total, sum(report$rows_inserted))
  ids <- DBI::dbGetQuery(con, "SELECT COUNT(DISTINCT measurement_id) AS d, COUNT(*) AS n FROM measurement")
  expect_equal(ids$d, ids$n)
  row <- DBI::dbGetQuery(con, "SELECT measurement_concept_id, value_as_number, unit_concept_id, measurement_source_value, strftime(measurement_datetime, '%Y-%m-%d %H:%M') AS ts FROM measurement WHERE value_as_number = 120")
  expect_equal(row$measurement_concept_id, 3004249)
  expect_equal(row$unit_concept_id, 8876)
  expect_equal(row$measurement_source_value, "8480-6")
  expect_equal(row$ts, "2020-01-05 04:15")
  expect_equal(nrow(DBI::dbGetQuery(con, "SELECT 1 FROM duckdb_tables() WHERE table_name = '_staged_wide_measurement'")), 0)
})

test_that("load_wide_measurement single mode, keep_staged, table source and errors", {
  cdm <- new_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  add_people(con, 2)
  rep <- load_wide_measurement(con, vital_csv(), list(SYSTOLIC = 3004249L), batch_execution_mode = "single",
                               staged_table_name = "stg", keep_staged = TRUE)
  expect_equal(rep$rows_inserted, 3)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM stg")$n, 4)

  DBI::dbExecute(con, "CREATE TABLE wide AS SELECT 'P1' AS patid, '2021-03-04' AS dt, 70.0 AS hr")
  rep2 <- load_wide_measurement(con, "wide", list(hr = list(concept_id = 3027018L, min = 20, max = 250)),
                                person_col = "patid", date_col = "dt", time_col = NULL, visit_col = NULL)
  expect_equal(rep2$rows_inserted, 1)
  expect_error(load_wide_measurement(con, "wide", list(hr = 1L), person_col = "missing", date_col = "dt"), "not found")
  expect_error(load_wide_measurement(con, "wide", list(hr = 1L), batch_execution_mode = "parallel",
                                     person_col = "patid", date_col = "dt"))
})

# ---- medication classes --------------------------------------------------------------------------------
med_cdm <- function() {
  cdm <- new_cdm()
  con <- cdm$con
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES
    (1000,'Statins','Drug','ATC','ATC 4th','C','C10AA','1970-01-01','2099-12-31'),
    (9001,'Legacy statin code','Drug','NDC','11-digit NDC',NULL,'00093-1234','1970-01-01','2099-12-31')")
  for (pair in list(c(1000, 1000), c(1000, 2000), c(1000, 2001), c(2000, 2000), c(2000, 2001), c(2001, 2001))) {
    DBI::dbExecute(con, sprintf("INSERT INTO concept_ancestor VALUES (%d, %d, 0, 0)", pair[[1]], pair[[2]]))
  }
  add_people(con, 12)
  DBI::dbExecute(con, "CREATE TABLE med_cohort (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE, site_anon VARCHAR)")
  for (i in 1:12) {
    site <- if (i <= 4) "A" else if (i <= 8) "B" else "C"
    DBI::dbExecute(con, sprintf("INSERT INTO med_cohort VALUES (1, %d, '2020-06-01', '2020-06-01', '%s')", i, site))
  }
  n <- 0L
  rx <- function(pid, concept, src) {
    n <<- n + 1L
    DBI::dbExecute(con, sprintf(
      "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_start_datetime, drug_exposure_end_date, drug_type_concept_id, drug_source_value, drug_source_concept_id) VALUES (%d, %d, %d, '2020-05-01', '2020-05-01', '2020-05-01', 32817, '%s', 0)",
      n, pid, concept, src))
  }
  for (p in 1:2) rx(p, 2001, "LIPITOR")
  for (p in 5:6) rx(p, 2001, "unmapped-local-string")
  rx(9, 2001, "LIPITOR")
  rx(10, 0, "LIPITOR")
  rx(11, 2001, "other")
  cdm
}

SPECS <- list(statin = list(ancestor_concept_ids = 1000))
XWALK <- data.frame(source_value = c("lipitor", "00093-1234"), drug_class = c("statin", "statin"))

test_that("extract_medication_classes runs both strategies", {
  cdm <- med_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  out <- extract_medication_classes(con, "med_cohort", SPECS, strategy = "both", crosswalk = XWALK)
  row <- function(p) out[out$person_id == p, ]
  expect_equal(c(row(1)$statin_source, row(1)$statin_standard, row(1)$statin), c(1, 1, 1))
  expect_equal(c(row(5)$statin_source, row(5)$statin_standard, row(5)$statin), c(0, 1, 1))
  expect_equal(c(row(10)$statin_source, row(10)$statin_standard, row(10)$statin), c(1, 0, 1))
  expect_equal(row(3)$statin, 0)
  expect_setequal(out$site, c("A", "B", "C"))

  std <- extract_medication_classes(con, "med_cohort", SPECS, strategy = "standard")
  expect_true("statin_standard" %in% names(std) && !"statin_source" %in% names(std))
  expect_equal(sum(std$statin), 6)
  src <- extract_medication_classes(con, "med_cohort", SPECS, strategy = "source", crosswalk = XWALK)
  expect_equal(sum(src$statin), 4)
  none <- extract_medication_classes(con, "med_cohort", SPECS, strategy = "standard", lookback_days = 10)
  expect_equal(sum(none$statin), 0)
})

test_that("crosswalk CSV, inline source values, ATC codes, and validation", {
  cdm <- med_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  csv <- tempfile(fileext = ".csv")
  writeLines(c("cui,medication_class", "LIPITOR,statin"), csv)
  specs <- list(statin = list(atc_codes = "C10AA", source_values = "other"))
  out <- extract_medication_classes(con, "med_cohort", specs, strategy = "both", crosswalk = csv)
  expect_equal(out[out$person_id == 11, ]$statin_source, 1)
  expect_equal(out[out$person_id == 5, ]$statin_standard, 1)
  expect_error(extract_medication_classes(con, "med_cohort", SPECS, strategy = "nope"))
  expect_error(extract_medication_classes(con, "med_cohort", SPECS, strategy = "source"), "crosswalk")
  expect_error(extract_medication_classes(con, "med_cohort", SPECS, strategy = "standard", return_audit = TRUE))
})

test_that("the auditor flags discordant sites and computes kappa", {
  cdm <- med_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  res <- extract_medication_classes(con, "med_cohort", SPECS, strategy = "both", crosswalk = XWALK, return_audit = TRUE)
  a <- res$audit
  site <- function(s) a[a$site == s & a$medication_class == "statin", ]
  expect_equal(site("A")$kappa, 1)
  expect_false(site("A")$flagged)
  expect_match(site("B")$reason, "source_blind")
  expect_equal(site("B")$concordance, 0.5)
  expect_true(site("C")$flagged)
  expect_equal(site("ALL")$n, 12)

  frame <- data.frame(site = "X", m_source = c(rep(1, 25), rep(0, 75)),
                      m_standard = c(rep(1, 20), rep(0, 5), rep(1, 10), rep(0, 65)))
  k <- audit_medication_strategies(frame)
  row <- k[k$site == "X", ]
  expect_equal(row$concordance, 0.85)
  expect_equal(row$kappa, (0.85 - 0.60) / (1 - 0.60))

  rows <- do.call(rbind, lapply(c(S1 = TRUE, S2 = TRUE, S3 = TRUE, S4 = FALSE), function(agree) {
    s <- as.integer(seq_len(40) %% 2 == 1)
    data.frame(x_source = s, x_standard = if (agree) s else 1L - s)
  }))
  rows$site <- rep(c("S1", "S2", "S3", "S4"), each = 40)
  peers <- audit_medication_strategies(rows)
  expect_match(peers[peers$site == "S4", ]$reason, "peer_outlier")
  expect_false(peers[peers$site == "S1", ]$flagged)
  expect_error(audit_medication_strategies(data.frame(a = 1)))
})

# ---- phenotype DSL -------------------------------------------------------------------------------------
pheno_cdm <- function() {
  cdm <- new_cdm()
  con <- cdm$con
  add_people(con, 8)
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES (7001,'E11.9','Condition','ICD10CM','5-char billing code',NULL,'E11.9','1970-01-01','2099-12-31')")
  for (pair in list(c(201826, 201826), c(201826, 4000), c(4000, 4000), c(21600, 21600), c(21600, 21601), c(21601, 21601))) {
    DBI::dbExecute(con, sprintf("INSERT INTO concept_ancestor VALUES (%d, %d, 0, 0)", pair[[1]], pair[[2]]))
  }
  for (i in 1:8) DBI::dbExecute(con, sprintf("INSERT INTO observation_period VALUES (%d, %d, '2015-01-01', '2024-12-31', 32817)", i, i))
  id <- 0L
  cond <- function(p, concept, src, date, status = 0) {
    id <<- id + 1L
    DBI::dbExecute(con, sprintf("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_status_concept_id, condition_source_value, condition_source_concept_id) VALUES (%d, %d, %d, '%s', 32817, %d, '%s', 0)", id, p, concept, date, status, src))
  }
  lab <- function(p, value, date) {
    id <<- id + 1L
    DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, measurement_source_value) VALUES (%d, %d, 3004410, '%s', 32817, %s, '4548-4')", id, p, date, value))
  }
  rx <- function(p, concept, date, days) {
    id <<- id + 1L
    DBI::dbExecute(con, sprintf("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, days_supply) VALUES (%d, %d, %d, '%s', '%s', 32817, %d)", id, p, concept, date, date, days))
  }
  cond(1, 4000, "x", "2020-03-01", 32902)
  cond(2, 0, "E11.9", "2020-04-01", 32908)
  lab(3, 7.0, "2020-05-01")
  lab(4, 5.5, "2020-05-01")
  rx(5, 21601, "2020-06-01", 60)
  rx(6, 21601, "2020-06-01", 5)
  cond(7, 4000, "x", "2020-02-01")
  lab(7, 7.2, "2020-01-01")
  lab(7, 6.8, "2020-09-01")
  cond(8, 4000, "x", "2020-02-01")
  cond(8, 0, "E10.1", "2020-02-05")
  cdm
}

T2DM_COND <- function() condition_criteria(concept_ids = 201826, icd_codes = "E11")
HBA1C <- function() measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5)
DRUG <- function() drug_criteria(concept_ids = 21600, min_days_supply = 30)
ids <- function(df) sort(df$subject_id)

test_that("union, intersect and index dates", {
  cdm <- pheno_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  u <- execute_phenotype(con, T2DM_COND() | HBA1C() | DRUG(), "cohort", 1L)
  expect_equal(ids(u), c(1, 2, 3, 5, 7, 8))
  expect_equal(as.character(u[u$subject_id == 7, ]$cohort_start_date), "2020-01-01")
  i <- execute_phenotype(con, T2DM_COND() & HBA1C(), "cohort", 2L)
  expect_equal(ids(i), 7)
  expect_equal(as.character(i$cohort_start_date), "2020-02-01")
})

test_that("NOT, top-level NOT, diagnosis position, occurrences, thresholds", {
  cdm <- pheno_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expect_equal(ids(execute_phenotype(con, T2DM_COND() & !condition_criteria(icd_codes = "E10"), "cohort", 3L)), c(1, 2, 7))
  expect_equal(ids(execute_phenotype(con, !T2DM_COND(), "cohort", 4L)), c(3, 4, 5, 6))
  expect_equal(ids(execute_phenotype(con, condition_criteria(concept_ids = 201826, icd_codes = "E11", diagnosis = "primary"), "cohort", 5L)), 1)
  expect_equal(ids(execute_phenotype(con, condition_criteria(icd_codes = "E11", diagnosis = "secondary"), "cohort", 6L)), 2)
  twice <- execute_phenotype(con, measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5, min_occurrences = 2L), "cohort", 7L)
  expect_equal(ids(twice), 7)
  expect_equal(as.character(twice$cohort_start_date), "2020-09-01")
  expect_equal(nrow(execute_phenotype(con, condition_criteria(concept_ids = 201826, include_descendants = FALSE), "cohort", 8L)), 0)
  expect_equal(ids(execute_phenotype(con, measurement_criteria(loinc_codes = "4548-4", value_gt = 7.0), "cohort", 9L)), 7)
  expect_equal(ids(execute_phenotype(con, measurement_criteria(loinc_codes = "4548-4", value_lt = 6.0), "cohort", 10L)), 4)
})

test_that("lookback / lookahead are relative to the index table", {
  cdm <- pheno_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE admits (subject_id BIGINT, cohort_start_date DATE)")
  DBI::dbExecute(con, "INSERT INTO admits VALUES (7, '2020-03-01'), (3, '2020-05-20'), (4, '2020-05-20'), (1, '2020-03-02')")
  crit <- measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5, lookback_days = 90L)
  df <- execute_phenotype(con, crit, "cohort", 11L, index_table = "admits")
  expect_equal(ids(df), c(3, 7))
  expect_equal(as.character(df[df$subject_id == 7, ]$cohort_start_date), "2020-03-01")
  ahead <- measurement_criteria(loinc_codes = "4548-4", value_gte = 6.5, lookahead_days = 365L)
  expect_equal(ids(execute_phenotype(con, ahead, "cohort", 12L, index_table = "admits")), 7)
  expect_equal(ids(execute_phenotype(con, everyone() & !crit, "cohort", 13L, index_table = "admits")), c(1, 4))
})

test_that("compile_phenotype is single-pass and validates input", {
  sql <- compile_phenotype(T2DM_COND() | (HBA1C() & !DRUG()), cohort_definition_id = 3L)
  expect_equal(lengths(regmatches(sql, gregexpr("GROUP BY person_id", sql, fixed = TRUE))), 1)
  expect_equal(lengths(regmatches(sql, gregexpr("UNION ALL", sql, fixed = TRUE))), 2)
  expect_equal(lengths(regmatches(sql, gregexpr("FROM measurement", sql, fixed = TRUE))), 1)
  expect_error(compile_phenotype(measurement_criteria(concept_ids = 1, lookback_days = 5L)), "index_table")
  expect_error(compile_phenotype(condition_criteria()))
  expect_error(compile_phenotype(drug_criteria()))
  expect_error(compile_phenotype(condition_criteria(concept_ids = 1, min_occurrences = 0L)))
  expect_error(condition_criteria(concept_ids = 1, diagnosis = "tertiary"))
  expect_error(phenotype_union(T2DM_COND()))
})

test_that("operators flatten and lists round-trip", {
  cdm <- pheno_cdm()
  con <- cdm$con
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expr <- T2DM_COND() | HBA1C() | DRUG()
  expect_length(expr$children, 3)
  spec <- phenotype_to_list(T2DM_COND() & !HBA1C())
  rebuilt <- phenotype_from_list(spec)
  expect_s3_class(rebuilt, "phenotype_intersect")
  expect_s3_class(rebuilt$children[[2]], "phenotype_not")
  a <- execute_phenotype(con, T2DM_COND() & !HBA1C(), "cohort", 20L)
  b <- execute_phenotype(con, rebuilt, "cohort", 21L)
  expect_equal(ids(a), ids(b))
  expect_error(phenotype_from_list(list(type = "wat")))
  again <- execute_phenotype(con, T2DM_COND(), "cohort", 30L)
  again2 <- execute_phenotype(con, T2DM_COND(), "cohort", 30L)
  expect_equal(nrow(again), nrow(again2))
})

# ---- ETL logging + vocabulary tables filter ---------------------------------------------------------
test_that("prepare_source_view / build_safe_source_view log aliases and padding", {
  p <- file.path(tempdir(), "demographic.csv")
  writeLines(c("ssid,BIRTH_DATE", "P1,2000-01-01"), p)
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  out <- paste(utils::capture.output(
    build_safe_source_view(con, "demographic", winify(p), c("PATID", "BIRTH_DATE", "PROVIDERID"))
  ), collapse = "\n")
  expect_match(out, "ssid -> PATID", fixed = TRUE)
  expect_match(out, "PROVIDERID", fixed = TRUE)
  res <- DBI::dbGetQuery(con, "SELECT PATID, PROVIDERID FROM src_demographic")
  expect_equal(res$PATID, "P1")
  expect_true(is.na(res$PROVIDERID))
})

test_that("load_vocabulary can load a subset of tables", {
  dir <- file.path(tempdir(), "vocab_subset")
  dir.create(dir, showWarnings = FALSE)
  file.copy(list.files(test_path("testdata", "tiny_vocab"), full.names = TRUE), dir)
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  expect_true(load_vocabulary(dir, db_path = db, tables = "concept"))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  expect_gt(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM concept")$n, 0)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM concept_relationship")$n, 0)
  DBI::dbDisconnect(con, shutdown = TRUE)
  expect_error(load_vocabulary(dir, db_path = db, tables = "nope"), "Unknown vocabulary tables")
})
