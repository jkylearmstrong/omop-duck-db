# Tests for define_study_cohort(), the unified study-cohort template engine. They mirror
# tests/test_define_study_cohort.py and assert the SAME golden files:
#
# * tests/fixtures/study_cohort_golden.csv: results of the legacy builders (build_readmission_cohort /
#   build_end_of_life_cohort) for 110 parameter combinations on the randomized adversarial CDM in
#   tests/fixtures/study_cohort_cdm/. The golden was produced BEFORE the builders were refactored onto the shared
#   engine, so these tests prove that the refactored builders and define_study_cohort() with equivalent
#   parameters return row-for-row the same cohorts and outcome flags.
# * tests/fixtures/study_cohort_define_golden.csv: the capabilities the legacy builders do not have (emergency /
#   outpatient / custom visit types, study windows, exact-age rule, cohort-only outcome, other death and follow-up
#   conventions). Python checks each of these 48 scenarios against an independent plain-Python implementation of
#   the documented rules; here R is held to the same file.
#
# followed by hand-built micro-CDMs, one property per test (boundaries, death ascertainment, sampling, visit types,
# materialisation incl. read-only connections, idempotence, attrition, validation, SQL safety).

skip_if_not_installed("withr")

sc_path <- function(...) normalizePath(test_path("..", "fixtures", ...), winslash = "/", mustWork = TRUE)
read_rows <- function(name) {
  utils::read.csv(sc_path(name), stringsAsFactors = FALSE, colClasses = "character", na.strings = character(0))
}

CDM_TABLES <- c("person", "visit_occurrence", "death", "observation_period", "measurement",
                "condition_occurrence", "drug_exposure")
EXPECTED_COLUMNS <- c(
  "cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date", "visit_occurrence_id",
  "visit_concept_id", "outcome_flag", "outcome_date", "followup_verified", "age_at_index", "los_days",
  "discharged_to_concept_id", "target_outcome"
)
# CONSORT subject counts of the RFC example on the randomized fixture; the Python suite asserts the same numbers
RFC_ATTRITION_SUBJECTS <- c(216, 204, 189, 180, 107, 96, 90)

# ---------------------------------------------------------------------------------------------- fixtures

# An empty CDM schema is built once per file and copied for every hand-built CDM.
.schema_template <- new.env()
schema_template <- function() {
  if (is.null(.schema_template$path)) {
    path <- tempfile(fileext = ".duckdb")
    suppressMessages(capture.output(build_schema(path)))
    .schema_template$path <- path
    withr::defer(unlink(path), envir = testthat::teardown_env())
  }
  .schema_template$path
}

# The shared randomized CDM (260 persons). Tests only write cohort ids they own.
.fixture <- new.env()
fixture_cdm <- function() {
  if (is.null(.fixture$con) || !DBI::dbIsValid(.fixture$con)) {
    db <- tempfile(fileext = ".duckdb")
    file.copy(schema_template(), db)
    con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
    for (t in CDM_TABLES) {
      DBI::dbExecute(con, sprintf("INSERT INTO %s BY NAME SELECT * FROM read_csv('%s', header = true)", t,
                                  sc_path("study_cohort_cdm", paste0(t, ".csv"))))
    }
    .fixture$con <- con
    withr::defer({
      if (DBI::dbIsValid(con)) DBI::dbDisconnect(con, shutdown = TRUE)
      unlink(db)
    }, envir = testthat::teardown_env())
  }
  .fixture$con
}

day <- function(base, n = 0) format(as.Date(base) + n)

# A micro CDM for hand-checkable scenarios. Methods return the object invisibly so they chain.
micro_cdm <- function(path = NULL, env = parent.frame()) {
  db <- if (is.null(path)) tempfile(fileext = ".duckdb") else path
  file.copy(schema_template(), db, overwrite = TRUE)
  m <- new.env()
  m$db <- db
  m$con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  m$seq <- 0
  withr::defer({
    if (DBI::dbIsValid(m$con)) DBI::dbDisconnect(m$con, shutdown = TRUE)
    if (is.null(path)) unlink(db)
  }, envir = env)
  nxt <- function() { m$seq <- m$seq + 1; m$seq }
  run <- function(sql) { DBI::dbExecute(m$con, sql); invisible(m) }
  d <- function(x) sprintf("DATE '%s'", x)

  m$person <- function(pid, born = "1960-01-01") {
    y <- as.integer(strsplit(born, "-")[[1]])
    run(sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth,
                 race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, %d, %d, %d, 8527, 38003564)",
                pid, y[1], y[2], y[3]))
  }
  m$visit <- function(vid, pid, start, end = start, concept = 9201, disch = NA) {
    run(sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,
                 visit_end_date, visit_type_concept_id, discharged_to_concept_id)
                 VALUES (%d, %d, %d, %s, %s, 32827, %s)", vid, pid, concept, d(start), d(end),
                if (is.na(disch)) "NULL" else as.character(disch)))
  }
  m$death <- function(pid, date) run(sprintf("INSERT INTO death (person_id, death_date) VALUES (%d, %s)", pid, d(date)))
  m$obs <- function(pid, start, end) {
    run(sprintf("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,
                 observation_period_end_date, period_type_concept_id) VALUES (%d, %d, %s, %s, 32817)",
                nxt(), pid, d(start), d(end)))
  }
  m$measurement <- function(pid, date) {
    run(sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,
                 measurement_type_concept_id) VALUES (%d, %d, 3004249, %s, 32817)", nxt(), pid, d(date)))
  }
  m$condition <- function(pid, date) {
    run(sprintf("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id,
                 condition_start_date, condition_type_concept_id) VALUES (%d, %d, 201826, %s, 32020)",
                nxt(), pid, d(date)))
  }
  m$drug <- function(pid, date) {
    run(sprintf("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date,
                 drug_exposure_end_date, drug_type_concept_id) VALUES (%d, %d, 1503297, %s, %s, 32838)",
                nxt(), pid, d(date), d(date)))
  }
  m$define <- function(...) {
    args <- list(...)
    if (is.null(args$materialise)) args$materialise <- FALSE
    if (is.null(args$attrition)) args$attrition <- FALSE
    do.call(define_study_cohort, c(list(con = m$con), args))
  }
  m$ids <- function(...) as.numeric(m$define(...)$subject_id)
  m
}

two_patient_cdm <- function(path = NULL, env = parent.frame()) {
  m <- micro_cdm(path, env)
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-05")$visit(11, 1, "2021-01-20", "2021-01-23")   # readmitted
  m$person(2)$visit(20, 2, "2021-01-01", "2021-01-04")$visit(21, 2, "2021-03-01", "2021-03-01", concept = 9202)
  m
}

# Settings that isolate one rule at a time: no wash-in, no follow-up filter, readmission outcome
SIMPLE <- list(washin_days = 0, require_verified_followup = FALSE)
with_simple <- function(...) c(list(...), SIMPLE)

cohort_rows <- function(con, cohort_id) {
  r <- DBI::dbGetQuery(con, sprintf(
    "SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = %d
     ORDER BY subject_id, cohort_start_date", cohort_id))
  paste(r$subject_id, as.character(r$cohort_start_date), as.character(r$cohort_end_date))
}
n_temp_tables <- function(con) DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM duckdb_tables() WHERE temporary")$n

# ======================================================================================================
# 1. golden equivalence with the legacy builders
# ======================================================================================================

id_list <- function(spec) {
  if (spec == "ANY") NULL else as.integer(strsplit(spec, "|", fixed = TRUE)[[1]])
}

run_legacy <- function(con, s) {
  if (s$builder == "readmission") {
    build_readmission_cohort(
      con, cohort_id = 1, outcome_cohort_id = NULL,
      target_visit_concept_ids = id_list(s$visit_ids), outcome_visit_concept_ids = id_list(s$outcome_visit_ids),
      followup_window_days = as.integer(s$window_days), grace_days = as.integer(s$gap_days),
      washin_days = as.integer(s$washin_days), index_selection_rule = s$rule, random_state = as.integer(s$seed),
      require_verified_followup = s$verified == "TRUE")
  } else {
    build_end_of_life_cohort(
      con, cohort_id = 1, outcome_cohort_id = NULL, mortality_type = s$mortality_type,
      target_visit_concept_ids = id_list(s$visit_ids), outcome_visit_concept_ids = id_list(s$outcome_visit_ids),
      mortality_window_days = as.integer(s$window_days), gap_days = as.integer(s$gap_days),
      min_age = as.integer(s$min_age), washin_days = as.integer(s$washin_days),
      require_verified_followup = s$verified == "TRUE", index_selection_rule = s$rule,
      random_state = as.integer(s$seed))
  }
}

# define_study_cohort() with the parameters equivalent to a legacy scenario (see its documentation)
define_equivalent <- function(con, s) {
  args <- list(
    con = con, visit_type = id_list(s$visit_ids), outcome_visit_type = id_list(s$outcome_visit_ids),
    followup_days = as.integer(s$window_days), gap_days = as.integer(s$gap_days),
    washin_days = as.integer(s$washin_days), sampling_rule = s$rule, random_state = as.integer(s$seed),
    require_verified_followup = s$verified == "TRUE", materialise = FALSE, attrition = FALSE)
  if (is.null(args$visit_type)) args["visit_type"] <- list(NULL)
  if (s$builder == "readmission") {
    args$target_outcome <- "all_cause_readmission"
    args$min_age <- 18
  } else {
    args$min_age <- as.integer(s$min_age)
    if (s$mortality_type == "composite_readmit_or_death") {
      args$target_outcome <- "readmission_or_death"
    } else {
      args$target_outcome <- "mortality"
      args$mortality_type <- s$mortality_type
    }
    if (s$mortality_type == "in_hospital") args$gap_days <- 0   # no window in-hospital; the legacy builder ignores it
  }
  do.call(define_study_cohort, args)
}

# one string per patient: subject|visit|flag[|outcome date]
legacy_signature <- function(df, with_date) {
  df <- df[order(df$subject_id), ]
  paste(sprintf("%.0f", df$subject_id), sprintf("%.0f", df$visit_occurrence_id), sprintf("%.0f", df$outcome_flag),
        if (with_date) ifelse(is.na(df$outcome_date), "", as.character(df$outcome_date)) else "", sep = "|")
}

LEGACY_SCENARIOS <- read_rows("study_cohort_scenarios.csv")
LEGACY_GOLDEN <- read_rows("study_cohort_golden.csv")
golden_signature <- function(scenario_id, with_date) {
  g <- LEGACY_GOLDEN[LEGACY_GOLDEN$scenario_id == scenario_id, ]
  g <- g[order(as.numeric(g$subject_id)), ]
  paste(g$subject_id, g$visit_occurrence_id, g$outcome_flag, if (with_date) g$outcome_date else "", sep = "|")
}

test_that("the golden file covers every legacy parameter combination and is not degenerate", {
  expect_equal(nrow(LEGACY_SCENARIOS), 110)
  expect_setequal(unique(LEGACY_SCENARIOS$builder), c("readmission", "eol"))
  expect_setequal(unique(LEGACY_SCENARIOS$mortality_type), c("", "in_hospital", "post_discharge", "fixed_window",
                                                             "composite_readmit_or_death"))
  sizes <- table(LEGACY_GOLDEN$scenario_id)
  expect_setequal(names(sizes), LEGACY_SCENARIOS$scenario_id)
  expect_true(all(sizes > 5))
})

test_that("build_readmission_cohort / build_end_of_life_cohort reproduce their pre-refactor results", {
  con <- fixture_cdm()
  for (i in seq_len(nrow(LEGACY_SCENARIOS))) {
    s <- LEGACY_SCENARIOS[i, ]
    with_date <- s$builder == "eol"
    expect_identical(legacy_signature(run_legacy(con, s), with_date), golden_signature(s$scenario_id, with_date),
                     info = s$scenario_id)
  }
})

test_that("define_study_cohort with equivalent parameters equals the legacy builders row for row", {
  con <- fixture_cdm()
  vis <- DBI::dbGetQuery(con, "SELECT visit_occurrence_id, visit_start_date, visit_end_date FROM visit_occurrence")
  for (i in seq_len(nrow(LEGACY_SCENARIOS))) {
    s <- LEGACY_SCENARIOS[i, ]
    with_date <- s$builder == "eol"
    df <- define_equivalent(con, s)
    expect_identical(legacy_signature(df, with_date), golden_signature(s$scenario_id, with_date), info = s$scenario_id)
    # the dates / LOS columns are the index stay's own values
    v <- vis[match(df$visit_occurrence_id, vis$visit_occurrence_id), ]
    expect_equal(df$cohort_start_date, v$visit_start_date, info = s$scenario_id)
    expect_equal(df$cohort_end_date, v$visit_end_date, info = s$scenario_id)
    expect_equal(df$los_days, as.numeric(v$visit_end_date - v$visit_start_date), info = s$scenario_id)
  }
})

test_that("the legacy builders keep their documented columns", {
  con <- fixture_cdm()
  r <- build_readmission_cohort(con, cohort_id = 1, washin_days = 0)
  expect_equal(names(r), c("cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date",
                           "visit_occurrence_id", "outcome_flag", "los_days", "age_at_admission",
                           "discharged_to_concept_id"))
  e <- build_end_of_life_cohort(con, cohort_id = 1, outcome_cohort_id = NULL, washin_days = 0)
  expect_equal(names(e), c("cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date",
                           "visit_occurrence_id", "outcome_flag", "outcome_date", "mortality_type", "age_at_index",
                           "los_days", "discharged_to_concept_id"))
  expect_setequal(e$mortality_type, "post_discharge")
})

# ======================================================================================================
# 2. the define-only capabilities, asserted against the shared golden
# ======================================================================================================

DEFINE_SCENARIOS <- read_rows("study_cohort_define_scenarios.csv")
DEFINE_GOLDEN <- read_rows("study_cohort_define_golden.csv")

split_bar <- function(v) {
  lapply(strsplit(v, "|", fixed = TRUE)[[1]], function(x) if (grepl("^[0-9]+$", x)) as.numeric(x) else x)
}

# Scenario row -> define_study_cohort() arguments (same mapping as the Python suite)
define_args <- function(row) {
  args <- list(
    visit_type = if (row$visit_type == "ANY") NULL else split_bar(row$visit_type),
    outcome_visit_type = split_bar(row$outcome_visit_type),
    study_window = list(if (nzchar(row$study_start)) row$study_start else NULL,
                        if (nzchar(row$study_end)) row$study_end else NULL),
    min_age = if (row$min_age == "none") NULL else as.numeric(row$min_age),
    age_method = row$age_method,
    min_los_days = if (row$min_los_days == "auto") "auto" else if (row$min_los_days == "none") NULL else as.numeric(row$min_los_days),
    washin_days = as.numeric(row$washin_days),
    followup_days = if (nzchar(row$followup_days)) as.numeric(row$followup_days) else NULL,
    gap_days = as.numeric(row$gap_days),
    exclude_in_hospital_death = if (row$exclude_death == "auto") "auto" else row$exclude_death == "TRUE",
    target_outcome = row$target_outcome,
    mortality_type = row$mortality_type,
    sampling_rule = row$rule,
    random_state = as.numeric(row$seed),
    require_verified_followup = row$verified == "TRUE",
    materialise = FALSE,
    attrition = FALSE
  )
  # NULL is a meaningful value for these arguments, so keep the element (`args$x <- NULL` would drop it)
  for (nm in c("visit_type", "min_age", "min_los_days", "followup_days")) {
    if (is.null(args[[nm]])) args[nm] <- list(NULL)
  }
  if (nzchar(row$death_ids)) args$death_discharge_concept_ids <- as.numeric(strsplit(row$death_ids, "|", fixed = TRUE)[[1]])
  if (nzchar(row$death_sources)) args$death_sources <- strsplit(row$death_sources, "|", fixed = TRUE)[[1]]
  if (nzchar(row$evidence)) args$followup_evidence <- strsplit(row$evidence, "|", fixed = TRUE)[[1]]
  args
}

blank_na <- function(x, fmt = NULL) {
  out <- if (is.null(fmt)) as.character(x) else sprintf(fmt, x)
  ifelse(is.na(x), "", out)
}

define_signature <- function(df) {
  df <- df[order(df$subject_id), ]
  paste(sprintf("%.0f", df$subject_id), sprintf("%.0f", df$visit_occurrence_id),
        blank_na(df$outcome_flag, "%.0f"), blank_na(df$outcome_date), blank_na(df$followup_verified, "%.0f"),
        sep = "|")
}

test_that("define_study_cohort reproduces the committed golden for every define scenario", {
  con <- fixture_cdm()
  seen_labels <- character(0)
  informative <- 0
  for (i in seq_len(nrow(DEFINE_SCENARIOS))) {
    s <- DEFINE_SCENARIOS[i, ]
    df <- do.call(define_study_cohort, c(list(con = con), define_args(s)))
    g <- DEFINE_GOLDEN[DEFINE_GOLDEN$scenario_id == s$scenario_id, ]
    g <- g[order(as.numeric(g$subject_id)), ]
    expected <- paste(g$subject_id, g$visit_occurrence_id, g$outcome_flag, g$outcome_date, g$followup_verified, sep = "|")
    expect_identical(define_signature(df), expected, info = s$scenario_id)
    # the cohort is one stay per patient, ordered by patient, with the documented columns
    expect_equal(names(df), EXPECTED_COLUMNS, info = s$scenario_id)
    expect_false(anyDuplicated(df$subject_id) > 0, info = s$scenario_id)
    expect_gt(nrow(df), 5)
    seen_labels <- union(seen_labels, unique(df$target_outcome))
    if (any(!is.na(df$outcome_flag)) && length(unique(df$outcome_flag)) == 2L) informative <- informative + 1
  }
  # the scenario set must not degenerate into empty or constant cohorts
  expect_true(all(c("all_cause_readmission", "none", "mortality_post_discharge", "mortality_in_hospital",
                    "mortality_fixed_window", "readmission_or_death") %in% seen_labels))
  expect_gte(informative, 30)
})

# ======================================================================================================
# 3. the RFC example
# ======================================================================================================

test_that("the RFC example runs as written on the randomized CDM", {
  con <- fixture_cdm()
  cohort <- define_study_cohort(
    con,
    visit_type = "inpatient",
    study_window = c("2017-01-01", "2020-12-31"),
    min_age = 18,
    min_los_days = 1,
    washin_days = 365,
    followup_days = 30,
    exclude_in_hospital_death = TRUE,
    target_outcome = "all_cause_readmission_30d"
  )
  expect_s3_class(cohort, "data.frame")
  expect_equal(names(cohort), EXPECTED_COLUMNS)
  expect_false(is.unsorted(cohort$subject_id)); expect_false(anyDuplicated(cohort$subject_id) > 0)
  expect_true(all(cohort$target_outcome == "all_cause_readmission"))
  expect_true(all(cohort$cohort_start_date >= as.Date("2017-01-01") & cohort$cohort_start_date <= as.Date("2020-12-31")))
  expect_true(all(cohort$age_at_index >= 18) && all(cohort$los_days >= 1))
  expect_true(all(cohort$outcome_flag %in% c(0, 1)) && all(cohort$followup_verified == 1))

  att <- attr(cohort, "attrition")
  expect_equal(names(att), c("step_number", "step_name", "subjects_retained", "subjects_dropped", "percent_retained"))
  expect_equal(att$subjects_retained, RFC_ATTRITION_SUBJECTS)
  expect_equal(att$subjects_retained[nrow(att)], nrow(cohort))
  expect_equal(att$step_name, c(
    "Index visit type in (9201)",
    "Index visit start between 2017-01-01 and 2020-12-31 (inclusive)",
    "Age >= 18 years at index",
    "Length of stay >= 1 day(s)",
    "Prior observation >= 365 days (wash-in)",
    "No in-hospital death at index stay",
    "Verified follow-up (evidence >= 30 days after discharge)"
  ))
  expect_equal(attr(cohort, "summary")$total_subjects, nrow(cohort))
  # materialised exactly as the legacy builders do (the default writes on a writable connection)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM cohort WHERE cohort_definition_id = 1")$n, nrow(cohort))
  expect_equal(attr(cohort, "definition")$target_outcome, "all_cause_readmission")
})

test_that("the RFC example equals its explicit equivalent parameters", {
  con <- fixture_cdm()
  rfc <- define_study_cohort(
    con, visit_type = "inpatient", study_window = c("2017-01-01", "2020-12-31"), min_age = 18, min_los_days = 1,
    washin_days = 365, followup_days = 30, exclude_in_hospital_death = TRUE,
    target_outcome = "all_cause_readmission_30d", materialise = FALSE, attrition = FALSE)
  explicit <- define_study_cohort(
    con, visit_type = 9201, study_window = list("2017-01-01", "2020-12-31"), min_age = 18, min_los_days = 1,
    washin_days = 365, followup_days = 30, exclude_in_hospital_death = TRUE,
    target_outcome = "all_cause_readmission", outcome_visit_type = 9201, gap_days = 0, sampling_rule = "random",
    random_state = 42, require_verified_followup = TRUE, materialise = FALSE, attrition = FALSE)
  expect_equal(rfc, explicit, ignore_attr = "definition")
})

test_that("a study window only restricts the index date, not outcome ascertainment", {
  con <- fixture_cdm()
  base <- list(con = con, visit_type = "inpatient", min_age = 18, min_los_days = 1, washin_days = 0,
               followup_days = 30, exclude_in_hospital_death = TRUE, target_outcome = "all_cause_readmission_30d",
               materialise = FALSE, attrition = FALSE)
  windowed <- do.call(define_study_cohort, c(base, list(study_window = c("2017-01-01", "2020-12-31"))))
  late <- windowed[windowed$cohort_end_date + 30 > as.Date("2020-12-31"), ]
  expect_gt(nrow(late), 0)
  unrestricted <- do.call(define_study_cohort, c(base, list(study_window = NULL)))
  both <- merge(late, unrestricted, by = "subject_id", suffixes = c("", "_u"))
  same_stay <- both[both$visit_occurrence_id == both$visit_occurrence_id_u, ]
  expect_gt(nrow(same_stay), 0)
  expect_equal(same_stay$outcome_flag, same_stay$outcome_flag_u)
})

# ======================================================================================================
# 4. boundaries, one property per test
# ======================================================================================================

test_that("study_window is inclusive at both ends and either end may be open", {
  m <- micro_cdm()
  for (r in list(c(1, "2016-12-31"), c(2, "2017-01-01"), c(3, "2020-12-31"), c(4, "2021-01-01"))) {
    pid <- as.integer(r[1])
    m$person(pid)$visit(pid * 10, pid, r[2], day(r[2], 2))
  }
  ids <- function(...) m$ids(target_outcome = "none", washin_days = 0, require_verified_followup = FALSE, ...)
  expect_equal(ids(study_window = c("2017-01-01", "2020-12-31")), c(2, 3))
  expect_equal(ids(study_window = c("2017-01-01", NA)), c(2, 3, 4))
  expect_equal(ids(study_window = c(NA, "2020-12-31")), c(1, 2, 3))
  expect_equal(ids(study_window = list("2017-01-01", NULL)), c(2, 3, 4))
  expect_equal(ids(study_window = list(NULL, "2020-12-31")), c(1, 2, 3))
  expect_equal(ids(study_window = c(NA, NA)), c(1, 2, 3, 4))
  expect_equal(ids(study_window = NULL), c(1, 2, 3, 4))
  expect_equal(ids(), c(1, 2, 3, 4))
  expect_equal(ids(study_window = c("2017-01-01", "2017-01-01")), 2)            # a single-day window
  expect_equal(ids(study_window = c(as.Date("2017-01-01"), as.Date("2020-12-31"))), c(2, 3))
  expect_equal(ids(study_window = list(as.POSIXct("2017-01-01 08:00:00", tz = "UTC"),
                                       as.POSIXct("2020-12-31 23:59:00", tz = "UTC"))), c(2, 3))
})

test_that("min_age is applied on the boundary day, with both age methods", {
  m <- micro_cdm()
  m$person(1, "2003-06-15")$visit(11, 1, "2021-06-15", "2021-06-17")   # turns 18 on the index day
  m$person(2, "2003-06-16")$visit(21, 2, "2021-06-15", "2021-06-17")   # one day short of 18
  m$person(3, "2003-12-31")$visit(31, 3, "2021-01-01", "2021-01-03")   # 17 years and 1 day
  m$person(4, "2004-02-29")$visit(41, 4, "2022-02-28", "2022-03-02")   # leap-day birthday: anniversary on 28 Feb
  m$person(5, "2004-02-29")$visit(51, 5, "2022-02-27", "2022-03-02")   # the day before it
  kw <- list(target_outcome = "none", washin_days = 0, require_verified_followup = FALSE)
  expect_equal(do.call(m$ids, c(kw, list(age_method = "completed_years", min_age = 18))), c(1, 4))
  exact <- do.call(m$define, c(kw[names(kw) != "min_age"], list(age_method = "completed_years", min_age = 17)))
  expect_equal(setNames(exact$age_at_index, exact$subject_id), c("1" = 18, "2" = 17, "3" = 17, "4" = 18, "5" = 17))
  # the legacy default (year_difference) counts calendar-year boundaries, so everyone above passes
  legacy <- do.call(m$define, kw)
  expect_equal(legacy$subject_id, 1:5)
  expect_true(all(legacy$age_at_index == 18))
  # min_age = NULL disables the rule; min_age = 0 keeps everyone with a birth year
  m$person(6, "2020-01-01")$visit(61, 6, "2021-01-01", "2021-01-03")
  expect_equal(tail(do.call(m$ids, c(kw, list(min_age = NULL))), 1), 6)
  expect_equal(tail(do.call(m$ids, c(kw, list(min_age = 0))), 1), 6)
  expect_false(6 %in% do.call(m$ids, c(kw, list(min_age = 18))))
})

test_that("a missing birth month and day count as the first", {
  m <- micro_cdm()
  DBI::dbExecute(m$con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id,
                         ethnicity_concept_id) VALUES (1, 8507, 2000, 8527, 38003564)")
  m$visit(11, 1, "2018-01-01", "2018-01-03")$visit(12, 1, "2017-12-31", "2018-01-01")
  expect_equal(m$ids(age_method = "completed_years", min_age = 18, target_outcome = "none", washin_days = 0,
                     require_verified_followup = FALSE), 1)       # born 2000-01-01
  expect_equal(m$ids(age_method = "completed_years", min_age = 19, target_outcome = "none", washin_days = 0,
                     require_verified_followup = FALSE), numeric(0))
})

test_that("length of stay is applied at exactly min_los_days and one less", {
  m <- micro_cdm()
  for (i in 1:4) m$person(i)$visit(i * 10, i, "2021-03-01", day("2021-03-01", c(0, 1, 2, 3)[i]))
  m$person(5)$visit(50, 5, "2021-03-01", "2021-02-28")      # end before start: a data error
  ids <- function(...) m$ids(target_outcome = "none", washin_days = 0, require_verified_followup = FALSE, ...)
  expect_equal(ids(min_los_days = 1), c(2, 3, 4))
  expect_equal(ids(min_los_days = 2), c(3, 4))
  expect_equal(ids(min_los_days = 3), 4)
  expect_equal(ids(min_los_days = 0), c(1, 2, 3, 4))
  expect_equal(ids(min_los_days = NULL), c(1, 2, 3, 4, 5))
  expect_equal(ids(), c(2, 3, 4))                              # "auto" is 1 day for a cohort-only template
  expect_equal(m$define(min_los_days = 0, target_outcome = "none", washin_days = 0,
                        require_verified_followup = FALSE)$los_days, c(0, 1, 2, 3))
})

test_that("wash-in boundary: observation period and prior-visit fallback", {
  index <- "2021-06-01"
  m <- micro_cdm()
  for (pid in 1:7) m$person(pid)$visit(pid * 10, pid, index, day(index, 3))
  m$obs(1, day(index, -365), day(index, 30))   # starts exactly washin_days before the index: qualifies
  m$obs(2, day(index, -364), day(index, 30))   # one day too late
  m$obs(3, day(index, -900), day(index, -1))   # long enough but ends the day before the index
  m$obs(4, day(index, -900), index)            # ends on the index day: covering
  m$visit(51, 5, day(index, -365), day(index, -365), concept = 9202)   # no observation period; prior visit 365 d before
  m$visit(61, 6, day(index, -364), day(index, -364), concept = 9202)   # 364 d before: too late
  # person 7: no observation period and no other visit
  ids <- function(...) m$ids(target_outcome = "none", require_verified_followup = FALSE, ...)
  expect_equal(ids(washin_days = 365), c(1, 4, 5))
  expect_equal(ids(washin_days = 364), c(1, 2, 4, 5, 6))
  expect_equal(ids(washin_days = 0), 1:7)
  expect_equal(ids(washin_days = NULL), 1:7)
})

test_that("in-hospital death via the death table only", {
  m <- micro_cdm()
  for (pid in 1:5) m$person(pid)$visit(pid * 10, pid, "2021-01-01", "2021-01-05")
  m$death(1, "2021-01-03")   # during the stay
  m$death(2, "2021-01-05")   # on the discharge day
  m$death(3, "2021-01-06")   # the day after discharge: alive at discharge
  m$death(4, "2020-12-20")   # before the admission (inconsistent source data)
  ids <- function(...) do.call(m$ids, with_simple(target_outcome = "all_cause_readmission", ...))
  expect_equal(ids(exclude_in_hospital_death = TRUE), c(3, 5))
  expect_equal(ids(), c(3, 5))                 # "auto" excludes for readmission
  kept <- do.call(m$define, with_simple(target_outcome = "all_cause_readmission", exclude_in_hospital_death = FALSE))
  expect_equal(kept$subject_id, c(1, 2, 3, 5))  # except a stay that starts after the death
  expect_true(all(kept$outcome_flag == 0))      # a patient who died cannot be readmitted
})

test_that("in-hospital death via the discharge disposition only", {
  m <- micro_cdm()
  stay <- c("2021-01-01", "2021-01-05")
  m$person(1)$visit(10, 1, stay[1], stay[2], disch = 4216643)   # 'Patient died'
  m$person(2)$visit(20, 2, stay[1], stay[2], disch = 4155309)   # legacy end-of-life code (see the documentation)
  m$person(3)$visit(30, 3, stay[1], stay[2], disch = 8536)
  ids <- function(...) do.call(m$ids, with_simple(...))
  expect_equal(ids(target_outcome = "all_cause_readmission"), c(2, 3))   # readmission convention: 4216643 only
  expect_equal(ids(target_outcome = "all_cause_readmission", death_discharge_concept_ids = c(4216643, 4155309)), 3)
  expect_equal(ids(target_outcome = "all_cause_readmission", death_discharge_concept_ids = 4216643), c(2, 3))
  expect_equal(ids(target_outcome = "all_cause_readmission", death_discharge_concept_ids = 8536), c(1, 2))
  # the mortality convention is the legacy end-of-life one unless overridden
  expect_equal(ids(target_outcome = "mortality", mortality_type = "post_discharge"), 3)
  expect_equal(ids(target_outcome = "mortality", mortality_type = "post_discharge",
                   death_discharge_concept_ids = 4216643), c(2, 3))
})

test_that("a death in both sources excludes the stay once", {
  m <- micro_cdm()
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-05", disch = 4216643)$death(1, "2021-01-05")
  m$person(2)$visit(20, 2, "2021-01-01", "2021-01-05", disch = 8536)
  ids <- function(...) do.call(m$ids, with_simple(target_outcome = "all_cause_readmission", ...))
  expect_equal(ids(), 2)
  expect_equal(ids(death_sources = c("death_table", "discharge_disposition")), 2)
})

test_that("death_sources decide where a patient's death date comes from", {
  m <- micro_cdm()
  # person 1: table death in the stay; person 2: a prior visit discharged dead, then a later stay (post-death activity)
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-05")$death(1, "2021-01-03")
  m$person(2)$visit(19, 2, "2020-12-01", "2020-12-05", concept = 9202, disch = 4216643)
  m$visit(20, 2, "2021-01-01", "2021-01-05")
  ids <- function(...) do.call(m$ids, with_simple(target_outcome = "all_cause_readmission", ...))
  expect_equal(ids(), 2)                                                          # table only: person 2's stay is kept
  expect_equal(ids(death_sources = "death_table"), 2)
  expect_equal(ids(death_sources = "discharge_disposition"), 1)                   # table ignored, disposition propagated
  expect_equal(ids(death_sources = c("death_table", "discharge_disposition")), numeric(0))
})

test_that("verified follow-up: boundaries and evidence types", {
  f <- "2021-02-04"   # discharge 2021-01-05 + 30 days
  m <- micro_cdm()
  for (pid in 1:11) m$person(pid)$visit(pid * 10, pid, "2021-01-01", "2021-01-05")
  m$visit(101, 1, f, f, concept = 9202)                              # 1: later visit exactly at discharge + 30
  m$visit(201, 2, day(f, -1), day(f, -1), concept = 9202)            # 2: one day too early
  m$visit(301, 3, "2021-01-20", "2021-01-22")                        # 3: readmitted inside the window, nothing later
  m$measurement(4, f)                                                # 4: measurement at the boundary
  m$condition(5, f)                                                  # 5: condition at the boundary
  m$drug(6, f)                                                       # 6: drug exposure at the boundary
  m$obs(7, "2020-01-01", f)                                          # 7: only an observation period reaching the boundary
  m$death(8, day(f, 6))                                              # 8: died after the window, no other record
  m$measurement(9, day(f, -1))                                       # 9: measurement one day early
  m$obs(11, "2020-01-01", day(f, -1))                                # 10: no records at all; 11: period one day short
  kw <- list(washin_days = 0, target_outcome = "all_cause_readmission", sampling_rule = "first", min_los_days = 1)
  kept <- do.call(m$define, kw)
  expect_equal(kept$subject_id, c(1, 3, 4, 5, 6))
  expect_equal(setNames(kept$outcome_flag, kept$subject_id), c("1" = 0, "3" = 1, "4" = 0, "5" = 0, "6" = 0))
  expect_true(all(kept$followup_verified == 1))
  # switched off: everyone stays, followup_verified tells who would have been dropped
  everyone <- do.call(m$define, c(kw, list(require_verified_followup = FALSE)))
  expect_equal(everyone$subject_id[everyone$followup_verified == 1], c(1, 3, 4, 5, 6))
  expect_equal(everyone$subject_id[everyone$followup_verified == 0], c(2, 7, 8, 9, 10, 11))
  # evidence sources are selectable: an observation period and a death both verify when allowed
  expect_equal(do.call(m$ids, c(kw, list(followup_evidence = "observation_period"))), c(3, 7))
  expect_equal(do.call(m$ids, c(kw, list(followup_evidence = "death", death_sources = "death_table"))), c(3, 8))
  expect_equal(do.call(m$ids, c(kw, list(followup_evidence = c("observation_period", "visit", "measurement",
                                                               "condition", "drug", "death")))),
               c(1, 3, 4, 5, 6, 7, 8))
})

test_that("verified follow-up is anchored on the index start for fixed-window mortality", {
  m <- micro_cdm()
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-20")
  m$person(2)$visit(20, 2, "2021-01-01", "2021-01-20")
  m$measurement(1, "2021-06-30")   # 180 days after the index start
  m$measurement(2, "2021-06-29")
  kw <- list(target_outcome = "mortality", mortality_type = "fixed_window", followup_days = 180, washin_days = 0)
  expect_equal(do.call(m$ids, kw), 1)
  expect_equal(do.call(m$ids, c(kw, list(require_verified_followup = FALSE))), c(1, 2))
})

test_that("sampling: first, last and random with a fixed seed", {
  m <- micro_cdm()
  m$person(1)
  starts <- c("2021-01-10", "2021-04-10", "2021-08-10", "2021-11-10")
  for (i in seq_along(starts)) m$visit(99 + i, 1, starts[i], day(starts[i], 4))
  m$visit(900, 1, "2022-06-01", "2022-06-01", concept = 9202)   # later evidence so that all stays are verified
  kw <- list(target_outcome = "none", washin_days = 0, require_verified_followup = TRUE)
  expect_equal(do.call(m$define, c(kw, list(sampling_rule = "first")))$visit_occurrence_id, 100)
  expect_equal(do.call(m$define, c(kw, list(sampling_rule = "last")))$visit_occurrence_id, 103)

  expected_random <- function(seed) {
    h <- DBI::dbGetQuery(m$con, sprintf("SELECT v, hash(CAST(v AS INTEGER), %d) AS h FROM (VALUES (100), (101), (102), (103)) t(v)", seed))
    h <- h[order(h$h, h$v), ]
    h$v[1]
  }
  picks <- numeric(0)
  for (seed in 1:24) {
    a <- do.call(m$define, c(kw, list(sampling_rule = "random", random_state = seed)))$visit_occurrence_id
    b <- do.call(m$define, c(kw, list(sampling_rule = "random", random_state = seed)))$visit_occurrence_id
    expect_identical(a, b)                                  # reproducible
    expect_equal(a, expected_random(seed))                  # and exactly the documented argmin of the seeded hash
    picks <- c(picks, a)
  }
  expect_gt(length(unique(picks)), 1)                       # the seed matters
  expect_equal(do.call(m$define, c(kw, list(sampling_rule = " Random ")))$visit_occurrence_id, expected_random(42))
})

test_that("visit types: inpatient, emergency, outpatient, custom ids and any", {
  m <- micro_cdm()
  for (r in list(c(1, 9201), c(2, 9203), c(3, 9202), c(4, 9999), c(5, 262))) {
    pid <- as.integer(r[1])
    m$person(pid)$visit(pid * 10, pid, "2021-01-01", "2021-01-03", concept = as.integer(r[2]))
  }
  ids <- function(...) m$ids(target_outcome = "none", washin_days = 0, require_verified_followup = FALSE, ...)
  expect_equal(ids(visit_type = "inpatient"), 1)
  expect_equal(ids(visit_type = "emergency"), 2)
  expect_equal(ids(visit_type = "outpatient"), 3)
  expect_equal(ids(visit_type = c("inpatient", "emergency")), c(1, 2))
  expect_equal(ids(visit_type = c(9201, 9202)), c(1, 3))
  expect_equal(ids(visit_type = 9999), 4)
  expect_equal(ids(visit_type = list("emergency", 262, "9999")), c(2, 4, 5))
  expect_equal(ids(visit_type = " Inpatient "), 1)
  expect_equal(ids(visit_type = NULL), 1:5)
  expect_equal(m$define(visit_type = "emergency", target_outcome = "none", washin_days = 0,
                        require_verified_followup = FALSE)$visit_concept_id, 9203)
  expect_equal(ids(), 1)                                    # inpatient is the default
})

test_that("the standard visit-type concept ids are the documented ones", {
  expect_equal(omopduckdb:::.STUDY_VISIT_TYPES, c(inpatient = 9201, emergency = 9203, outpatient = 9202))
})

test_that("visit-type concepts exist in a real vocabulary (optional: set OMOP_VOCAB_DB)", {
  vocab <- Sys.getenv("OMOP_VOCAB_DB")
  skip_if(!nzchar(vocab), "set OMOP_VOCAB_DB to an Athena-loaded DuckDB file")
  vcon <- DBI::dbConnect(duckdb::duckdb(), dbdir = vocab, read_only = TRUE)
  withr::defer(DBI::dbDisconnect(vcon, shutdown = TRUE))
  got <- DBI::dbGetQuery(vcon, "SELECT concept_id, concept_name, domain_id, standard_concept FROM concept
                                WHERE concept_id IN (9201, 9202, 9203) ORDER BY concept_id")
  expect_equal(got$concept_name, c("Inpatient Visit", "Outpatient Visit", "Emergency Room Visit"))
  expect_true(all(got$domain_id == "Visit") && all(got$standard_concept == "S"))
  expect_equal(DBI::dbGetQuery(vcon, "SELECT COUNT(*) AS n FROM concept WHERE concept_id = 4216643")$n, 1)
})

test_that("the cohort-only template leaves the outcome columns NA", {
  m <- micro_cdm()
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-05")$visit(11, 1, "2021-01-20", "2021-01-22")
  m$person(2)$visit(20, 2, "2021-01-01", "2021-01-05")$visit(21, 2, "2021-03-01", "2021-03-01", concept = 9202)
  df <- m$define(target_outcome = "none", washin_days = 0)
  expect_true(all(is.na(df$outcome_flag)) && all(is.na(df$outcome_date)))
  expect_true(all(df$target_outcome == "none"))
  expect_equal(df$subject_id, 2)           # verification stays on by default: only person 2 has later evidence
  expect_equal(df$followup_verified, 1)
  expect_equal(m$ids(target_outcome = "none", washin_days = 0, require_verified_followup = FALSE), c(1, 2))
  expect_equal(m$ids(target_outcome = "none", washin_days = 0, followup_days = NULL,
                     require_verified_followup = FALSE), c(1, 2))
  expect_error(m$define(target_outcome = "none", outcome_cohort_id = 2), "outcome_cohort_id cannot be used")
})

# ---- mortality paradigms and the composite -------------------------------------------------------------

test_that("post-discharge mortality window boundaries", {
  e <- "2021-01-05"
  m <- micro_cdm()
  for (pid in 1:7) m$person(pid)$visit(pid * 10, pid, "2021-01-01", e)
  m$death(1, day(e, 30))   # last day of the window: inside
  m$death(2, day(e, 31))   # one day later: outside, but verified by the death itself
  m$death(3, day(e, 1))    # first day: inside
  m$death(4, e)            # on the discharge day: an in-hospital death, excluded from the index stays
  m$death(5, day(e, 10))
  m$visit(61, 6, day(e, 40), day(e, 40), concept = 9202)   # survivor with verified follow-up
  kw <- list(target_outcome = "mortality", mortality_type = "post_discharge", washin_days = 0)
  df <- do.call(m$define, kw)
  expect_equal(df$subject_id, c(1, 2, 3, 5, 6))
  expect_equal(df$outcome_flag, c(1, 0, 1, 1, 0))
  expect_equal(as.character(df$outcome_date), c(day(e, 30), NA, day(e, 1), day(e, 10), NA))
  gap <- do.call(m$define, c(kw, list(gap_days = 5, require_verified_followup = FALSE)))   # window (e+5, e+30]
  expect_equal(setNames(gap$outcome_flag, gap$subject_id), c("1" = 1, "2" = 0, "3" = 0, "5" = 1, "6" = 0, "7" = 0))
  expect_true(all(do.call(m$define, c(kw, list(gap_days = 5)))$followup_verified == 1))
  expect_error(do.call(m$define, c(kw, list(exclude_in_hospital_death = FALSE))), "exclude_in_hospital_death = TRUE")
})

test_that("in-hospital mortality: both sources and the disposition override", {
  m <- micro_cdm()
  m$person(1)$visit(10, 1, "2021-05-01", "2021-05-05", disch = 4216643)
  m$person(2)$visit(20, 2, "2021-05-01", "2021-05-06")$death(2, "2021-05-06")
  m$person(3)$visit(30, 3, "2021-05-01", "2021-05-04", disch = 8536)
  m$person(4)$visit(40, 4, "2021-05-01", "2021-05-01", disch = 4216643)    # same-day death: min LOS auto is 0
  m$person(5)$visit(50, 5, "2021-05-01", "2021-05-03", disch = 4155309)    # legacy code, see the documentation
  kw <- list(target_outcome = "mortality", mortality_type = "in_hospital", washin_days = 0)
  df <- do.call(m$define, kw)
  expect_equal(df$outcome_flag, c(1, 1, 0, 1, 1))
  expect_equal(as.character(df$outcome_date), c("2021-05-05", "2021-05-06", NA, "2021-05-01", "2021-05-03"))
  expect_true(all(is.na(df$followup_verified)))
  narrow <- do.call(m$define, c(kw, list(death_discharge_concept_ids = 4216643)))
  expect_equal(narrow$outcome_flag, c(1, 1, 0, 1, 0))
  expect_equal(do.call(m$ids, c(kw, list(min_los_days = 1))), c(1, 2, 3, 5))
  expect_error(do.call(m$define, c(kw, list(exclude_in_hospital_death = TRUE))), "in-hospital mortality")
  expect_error(do.call(m$define, c(kw, list(gap_days = 3))), "gap_days has no effect")
})

test_that("fixed-window mortality is measured from the index start", {
  s <- "2021-01-01"
  m <- micro_cdm()
  for (pid in 1:6) m$person(pid)$visit(pid * 10, pid, s, day(s, 3))
  m$death(1, day(s, 30))    # on the gap boundary: outside (s + 30, s + 180]
  m$death(2, day(s, 31))    # first day inside
  m$death(3, day(s, 180))   # last day inside
  m$death(4, day(s, 181))   # first day beyond
  m$death(5, day(s, 2))     # during the index stay: allowed here, and inside the gap
  kw <- list(target_outcome = "mortality", mortality_type = "fixed_window", followup_days = 180, gap_days = 30,
             washin_days = 0, require_verified_followup = FALSE)
  df <- do.call(m$define, kw)
  expect_equal(df$outcome_flag, c(0, 1, 1, 0, 0, 0))
  expect_true(all(df$los_days == 3))
  expect_equal(do.call(m$ids, c(kw, list(exclude_in_hospital_death = TRUE))), c(1, 2, 3, 4, 6))
})

test_that("readmission_or_death reports the first event", {
  e <- "2021-05-05"
  m <- micro_cdm()
  for (pid in 1:4) m$person(pid)$visit(pid * 10, pid, "2021-05-01", e)
  m$visit(11, 1, day(e, 10), day(e, 13))                                  # 1: readmission only
  m$death(2, day(e, 12))                                                  # 2: death only
  m$visit(31, 3, day(e, 10), day(e, 13))$death(3, day(e, 20))             # 3: readmission first
  m$visit(41, 4, day(e, 45), day(e, 45), concept = 9202)                  # 4: verified survivor
  df <- m$define(target_outcome = "readmission_or_death", washin_days = 0, sampling_rule = "first")
  expect_equal(df$outcome_flag, c(1, 1, 1, 0))
  expect_equal(as.character(df$outcome_date), c(day(e, 10), day(e, 12), day(e, 10), NA))
  expect_true(all(df$target_outcome == "readmission_or_death"))
  alias <- m$define(target_outcome = "composite_readmit_or_death", washin_days = 0, sampling_rule = "first")
  expect_equal(df, alias, ignore_attr = "definition")
  via_mortality <- m$define(target_outcome = "mortality", mortality_type = "composite_readmit_or_death",
                            washin_days = 0, sampling_rule = "first")
  expect_equal(df, via_mortality, ignore_attr = "definition")
})

test_that("overlapping, adjacent and same-day-transfer stays", {
  m <- micro_cdm()
  m$person(1)
  m$visit(1, 1, "2021-01-01", "2021-01-10")   # A
  m$visit(2, 1, "2021-01-10", "2021-01-15")   # B: same-day transfer, starts on A's discharge day
  m$visit(3, 1, "2021-01-16", "2021-01-20")   # C: adjacent, starts the day after B's discharge
  m$visit(4, 1, "2021-01-18", "2021-01-25")   # D: overlaps C
  m$person(2)$visit(21, 2, "2021-03-01", "2021-03-05")$visit(22, 2, "2021-03-06", "2021-03-07")   # adjacent pair
  kw <- list(target_outcome = "all_cause_readmission", require_verified_followup = FALSE, washin_days = 0)
  define <- function(...) do.call(m$define, c(kw, list(...)))

  first <- define(sampling_rule = "first")
  # A's readmission window (Jan 10, Feb 9] excludes B (it starts ON the discharge day) but holds C and D
  expect_equal(first$visit_occurrence_id[1], 1)
  expect_equal(first$outcome_flag[1], 1)
  expect_equal(as.character(first$outcome_date[1]), "2021-01-16")
  expect_equal(as.character(define(sampling_rule = "first", gap_days = 6)$outcome_date[1]), "2021-01-18")
  expect_equal(define(sampling_rule = "first", gap_days = 6)$outcome_flag[1], 1)
  expect_equal(define(sampling_rule = "first", gap_days = 6, followup_days = 7)$outcome_flag[1], 0)   # (Jan 16, Jan 17]
  # a same-day transfer is never a readmission, even with no gap
  only_transfer <- micro_cdm()
  only_transfer$person(1)$visit(1, 1, "2021-01-01", "2021-01-05")$visit(2, 1, "2021-01-05", "2021-01-09")
  expect_equal(do.call(only_transfer$define, c(kw, list(sampling_rule = "first")))$outcome_flag, 0)
  expect_equal(do.call(only_transfer$define, c(kw, list(sampling_rule = "last")))$outcome_flag, 0)
  # an overlapping stay that starts inside the index stay is not a readmission either
  overlap <- micro_cdm()
  overlap$person(1)$visit(1, 1, "2021-01-01", "2021-01-10")$visit(2, 1, "2021-01-05", "2021-01-12")
  expect_equal(do.call(overlap$define, c(kw, list(sampling_rule = "first")))$outcome_flag, 0)
  # adjacent stays: the day after discharge counts with no gap, not with a one-day gap
  expect_equal(first$outcome_flag[first$subject_id == 2], 1)
  one_day_gap <- define(sampling_rule = "first", gap_days = 1)
  expect_equal(one_day_gap$outcome_flag[one_day_gap$subject_id == 2], 0)
  # sampling among overlapping stays: last picks the latest start (D)
  expect_equal(define(sampling_rule = "last")$visit_occurrence_id[1], 4)
})

# ======================================================================================================
# 5. materialisation, read-only connections, idempotence
# ======================================================================================================

test_that("materialise = TRUE writes cohort_definition and the outcome cohort", {
  m <- two_patient_cdm()
  df <- m$define(materialise = TRUE, washin_days = 0, cohort_definition_id = 7, outcome_cohort_id = 8,
                 sampling_rule = "first", cohort_name = "It's a study", attrition = TRUE)
  expect_equal(df$subject_id, c(1, 2))
  expect_equal(df$outcome_flag, c(1, 0))
  expect_equal(cohort_rows(m$con, 7), c("1 2021-01-01 2021-01-05", "2 2021-01-01 2021-01-04"))
  expect_equal(cohort_rows(m$con, 8), "1 2021-01-20 2021-01-23")      # the readmission visit itself
  defs <- DBI::dbGetQuery(m$con, "SELECT cohort_definition_id, cohort_definition_name, cohort_definition_description
                                  FROM cohort_definition ORDER BY 1")
  expect_equal(defs$cohort_definition_id, c(7, 8))
  expect_equal(defs$cohort_definition_name, c("It's a study", "It's a study - Outcome (all_cause_readmission)"))
  expect_match(defs$cohort_definition_description[1], "outcome all_cause_readmission", fixed = TRUE)
  expect_match(defs$cohort_definition_description[1], "sampling first", fixed = TRUE)
  expect_equal(attr(df, "summary")$total_subjects, 2)
  expect_equal(tail(attr(df, "attrition")$subjects_retained, 1), 2)
})

test_that("a mortality outcome cohort is a point event on the outcome date", {
  m <- micro_cdm()
  m$person(1)$visit(10, 1, "2021-01-01", "2021-01-05")$death(1, "2021-01-20")
  m$person(2)$visit(20, 2, "2021-01-01", "2021-01-05")$measurement(2, "2021-03-01")
  m$define(materialise = TRUE, target_outcome = "mortality", washin_days = 0, cohort_definition_id = 1,
           outcome_cohort_id = 2)
  expect_equal(cohort_rows(m$con, 2), "1 2021-01-20 2021-01-20")
})

test_that("materialise = FALSE does not touch the cohort tables", {
  m <- two_patient_cdm()
  m$define(materialise = FALSE, washin_days = 0, cohort_definition_id = 7, outcome_cohort_id = 8)
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM cohort")$n, 0)
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM cohort_definition")$n, 0)
  expect_null(attr(m$define(materialise = FALSE, washin_days = 0), "summary"))   # no cohort rows to summarise
  # no temp tables left behind, including by the attrition pass
  m$define(materialise = FALSE, washin_days = 0, attrition = TRUE)
  expect_equal(n_temp_tables(m$con), 0)
})

test_that("re-running the same cohort id replaces it cleanly", {
  m <- two_patient_cdm()
  kw <- list(materialise = TRUE, washin_days = 0, cohort_definition_id = 7, outcome_cohort_id = 8,
             sampling_rule = "first")
  do.call(m$define, kw)
  do.call(m$define, kw)
  expect_length(cohort_rows(m$con, 7), 2)
  expect_length(cohort_rows(m$con, 8), 1)
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM cohort_definition WHERE cohort_definition_id IN (7, 8)")$n, 2)
  # a different definition under the same id fully replaces the old rows
  m$define(materialise = TRUE, washin_days = 0, cohort_definition_id = 7, outcome_cohort_id = 8,
           sampling_rule = "first", visit_type = "outpatient", target_outcome = "mortality", min_los_days = 0,
           require_verified_followup = FALSE)
  expect_equal(cohort_rows(m$con, 7), "2 2021-03-01 2021-03-01")
  expect_length(cohort_rows(m$con, 8), 0)    # no deaths: the old readmission outcome rows are gone
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM cohort_definition WHERE cohort_definition_id = 7")$n, 1)
  # other cohort ids are untouched
  m$define(materialise = TRUE, washin_days = 0, cohort_definition_id = 3)
  m$define(materialise = TRUE, washin_days = 0, cohort_definition_id = 7)
  expect_length(cohort_rows(m$con, 3), 2)
})

test_that("table_name persists the full result", {
  m <- two_patient_cdm()
  df <- m$define(materialise = FALSE, washin_days = 0, table_name = "main.my_index_cohort")
  saved <- DBI::dbGetQuery(m$con, "SELECT * FROM my_index_cohort ORDER BY subject_id")
  expect_equal(names(saved), EXPECTED_COLUMNS)
  expect_equal(saved$subject_id, df$subject_id)
  m$define(materialise = FALSE, washin_days = 0, table_name = "my_index_cohort", sampling_rule = "last")   # replaces
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM my_index_cohort")$n, 2)
})

test_that("read-only connections: materialise = FALSE works, writing is refused with a clear error", {
  path <- tempfile(fileext = ".duckdb")
  withr::defer(unlink(path))
  m <- two_patient_cdm(path)
  DBI::dbDisconnect(m$con, shutdown = TRUE)
  ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
  withr::defer(if (DBI::dbIsValid(ro)) DBI::dbDisconnect(ro, shutdown = TRUE))

  df <- define_study_cohort(ro, materialise = FALSE, washin_days = 0, sampling_rule = "first")
  expect_equal(df$subject_id, c(1, 2))
  expect_equal(df$outcome_flag, c(1, 0))
  expect_equal(tail(attr(df, "attrition")$subjects_retained, 1), 2)       # attrition also works read-only
  expect_error(define_study_cohort(ro, materialise = TRUE, washin_days = 0), "read-only")
  expect_error(define_study_cohort(ro, materialise = FALSE, washin_days = 0, table_name = "persisted"), "read-only")
  expect_equal(n_temp_tables(ro), 0)                                      # the failed attempts left nothing behind
  DBI::dbDisconnect(ro, shutdown = TRUE)
  # nothing was written (re-checked on a fresh read-only handle: after a failed write on a read-only connection the
  # duckdb R driver keeps the file locked against a read-write reopen until garbage collection)
  ro2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
  withr::defer(if (DBI::dbIsValid(ro2)) DBI::dbDisconnect(ro2, shutdown = TRUE))
  expect_equal(DBI::dbGetQuery(ro2, "SELECT COUNT(*) AS n FROM cohort")$n, 0)
  expect_equal(DBI::dbGetQuery(ro2, "SELECT COUNT(*) AS n FROM cohort_definition")$n, 0)
})

test_that("the default materialise = 'auto' writes when writable and warns when read-only", {
  path <- tempfile(fileext = ".duckdb")
  withr::defer(unlink(path))
  m <- two_patient_cdm(path)
  expect_no_warning(df <- define_study_cohort(m$con, washin_days = 0, sampling_rule = "first", cohort_definition_id = 4))
  expect_length(cohort_rows(m$con, 4), 2)               # writable: materialised like the legacy builders
  expect_false(is.null(attr(df, "summary")))
  DBI::dbDisconnect(m$con, shutdown = TRUE)
  ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
  withr::defer(if (DBI::dbIsValid(ro)) DBI::dbDisconnect(ro, shutdown = TRUE))
  expect_warning(res <- define_study_cohort(ro, washin_days = 0, sampling_rule = "first", cohort_definition_id = 4),
                 "read-only")
  expect_equal(res$subject_id, c(1, 2))
  expect_null(attr(res, "summary"))
  expect_warning(                                        # the RFC example needs no change on a read-only connection
    define_study_cohort(ro, visit_type = "inpatient", study_window = c("2017-01-01", "2020-12-31"), min_age = 18,
                        min_los_days = 1, washin_days = 365, followup_days = 30, exclude_in_hospital_death = TRUE,
                        target_outcome = "all_cause_readmission_30d"),
    "read-only")
  expect_error(define_study_cohort(ro, materialise = "maybe"), "materialise must be")
})

test_that("schema qualifies the CDM tables", {
  m <- two_patient_cdm()
  a <- m$define(washin_days = 0, schema = "main", sampling_rule = "first")
  b <- m$define(washin_days = 0, sampling_rule = "first")
  expect_equal(a, b, ignore_attr = "definition")
  expect_error(m$define(washin_days = 0, schema = "no_such_schema"))
})

# ======================================================================================================
# 6. attrition and metadata
# ======================================================================================================

test_that("attrition steps follow the active criteria and end at the cohort size", {
  con <- fixture_cdm()
  df <- define_study_cohort(con, materialise = FALSE, visit_type = c("inpatient", "emergency"),
                            study_window = c(NA, "2019-12-31"), min_age = NULL, min_los_days = NULL, washin_days = 0,
                            target_outcome = "mortality", mortality_type = "post_discharge", cohort_definition_id = 5)
  steps <- attr(df, "attrition")
  expect_equal(steps$step_name, c(
    "Index visit type in (9201, 9203)",
    "Index visit start on or before 2019-12-31",
    "No in-hospital death at index stay",
    "Verified follow-up (evidence >= 30 days after discharge)"
  ))
  expect_equal(steps$subjects_retained[nrow(steps)], nrow(df))
  expect_false(is.unsorted(rev(steps$subjects_retained)))
  none <- define_study_cohort(con, materialise = FALSE, target_outcome = "mortality", mortality_type = "in_hospital",
                              attrition = FALSE)
  expect_null(attr(none, "attrition"))
})

test_that("the definition attribute records the resolved settings", {
  m <- two_patient_cdm()
  d <- attr(m$define(target_outcome = "mortality", mortality_type = "fixed_window", washin_days = 0), "definition")
  expect_equal(d$target_outcome, "mortality_fixed_window")
  expect_null(d$min_los_days)                                                     # "auto" resolved: no restriction
  expect_false(d$exclude_in_hospital_death)
  expect_equal(d$death_discharge_concept_ids, c(4216643, 4155309))                # end-of-life convention
  expect_equal(d$death_sources, c("death_table", "discharge_disposition"))
  expect_equal(d$followup_evidence, c("observation_period", "visit", "measurement", "condition", "drug", "death"))
  r <- attr(m$define(washin_days = 0), "definition")
  expect_equal(r$death_discharge_concept_ids, 4216643)
  expect_equal(r$death_sources, "death_table")
  expect_equal(r$followup_evidence, c("visit", "measurement", "condition", "drug"))
  expect_equal(r$min_los_days, 1)
  expect_true(r$exclude_in_hospital_death)
  expect_equal(r$visit_concept_ids, 9201)
  expect_equal(r$outcome_visit_concept_ids, 9201)
})

test_that("the all_cause_readmission_<N>d alias must agree with followup_days", {
  m <- two_patient_cdm()
  a <- m$define(target_outcome = "all_cause_readmission_30d", washin_days = 0)
  b <- m$define(target_outcome = "all_cause_readmission", washin_days = 0)
  expect_equal(a, b, ignore_attr = "definition")
  m$define(target_outcome = "all_cause_readmission_45d", followup_days = 45, washin_days = 0)
  expect_error(m$define(target_outcome = "all_cause_readmission_45d", followup_days = 30), "implies a 45-day window")
})

# ======================================================================================================
# 7. input validation and SQL safety
# ======================================================================================================

VALIDATION_CASES <- list(
  list(list(visit_type = "surgery"), "Unknown visit_type 'surgery'"),
  list(list(visit_type = c("inpatient", "daycase")), "Unknown visit_type 'daycase'"),
  list(list(visit_type = list()), "must not be empty"),
  list(list(visit_type = 9201.5), "whole number"),
  list(list(visit_type = TRUE), "whole number"),
  list(list(outcome_visit_type = "clinic"), "Unknown outcome_visit_type 'clinic'"),
  list(list(study_window = "2017-01-01"), "study_window must be NULL or a"),
  list(list(study_window = c("2017-01-01", "2018-01-01", "2019-01-01")), "study_window must be NULL or a"),
  list(list(study_window = c("2021-01-01", "2020-12-31")), "must not be after"),
  list(list(study_window = c("2017-13-01", NA)), "valid date"),
  list(list(study_window = c("2017-02-30", NA)), "valid date"),
  list(list(study_window = c("01/02/2017", NA)), "valid date"),
  list(list(study_window = c(20170101, NA)), "valid date"),
  list(list(min_age = -1), "min_age must be >= 0"),
  list(list(min_age = 17.5), "min_age must be a whole number"),
  list(list(min_age = "18"), "min_age must be a whole number"),
  list(list(min_los_days = -1), "min_los_days must be >= 0"),
  list(list(min_los_days = "two"), "min_los_days must be a whole number"),
  list(list(washin_days = -365), "washin_days must be >= 0"),
  list(list(followup_days = 0), "followup_days must be >= 1"),
  list(list(followup_days = -30), "followup_days must be >= 1"),
  list(list(followup_days = NULL), "followup_days is required"),
  list(list(gap_days = -1), "gap_days must be >= 0"),
  list(list(gap_days = 30), "Inconsistent windows"),
  list(list(gap_days = 45), "Inconsistent windows"),
  list(list(sampling_rule = "median"), "Unsupported sampling_rule 'median'"),
  list(list(target_outcome = "readmit"), "Unsupported target_outcome 'readmit'"),
  list(list(target_outcome = NULL), "target_outcome must be a string"),
  list(list(target_outcome = "mortality", mortality_type = "eventual"), "Unsupported mortality_type 'eventual'"),
  list(list(age_method = "exact"), "Unsupported age_method 'exact'"),
  list(list(exclude_in_hospital_death = "yes"), "exclude_in_hospital_death must be TRUE or FALSE"),
  list(list(require_verified_followup = "yes"), "require_verified_followup must be TRUE or FALSE"),
  list(list(materialise = 1), "materialise must be TRUE, FALSE or 'auto'"),
  list(list(materialise = "yes"), "materialise must be TRUE, FALSE or 'auto'"),
  list(list(random_state = 1.5), "random_state must be a whole number"),
  list(list(cohort_definition_id = NULL), "must not be NULL"),
  list(list(cohort_definition_id = 3, outcome_cohort_id = 3), "must differ from cohort_definition_id"),
  list(list(death_sources = "graveyard"), "death_sources must be a non-empty subset"),
  list(list(death_sources = character(0)), "death_sources must be a non-empty subset"),
  list(list(followup_evidence = c("visit", "rumour")), "followup_evidence must be a non-empty subset"),
  list(list(death_discharge_concept_ids = numeric(0)), "must not be empty"),
  list(list(death_discharge_concept_ids = "x"), "whole number"),
  list(list(schema = "main; DROP TABLE person"), "Invalid schema"),
  list(list(schema = "a.b.c"), "Invalid schema"),
  list(list(schema = ""), "schema must be a non-empty string"),
  list(list(table_name = "t; DROP TABLE person"), "Invalid table_name"),
  list(list(table_name = "x y"), "Invalid table_name"),
  list(list(target_outcome = "mortality", mortality_type = "post_discharge", exclude_in_hospital_death = FALSE),
       "exclude_in_hospital_death = TRUE"),
  list(list(target_outcome = "readmission_or_death", exclude_in_hospital_death = FALSE),
       "exclude_in_hospital_death = TRUE"),
  list(list(target_outcome = "mortality", mortality_type = "in_hospital", exclude_in_hospital_death = TRUE),
       "in-hospital mortality"),
  list(list(target_outcome = "none", gap_days = 2), "gap_days has no effect"),
  list(list(target_outcome = "none", followup_days = NULL), "needs followup_days"),
  list(list(target_outcome = "none", outcome_cohort_id = 2), "outcome_cohort_id cannot be used")
)

test_that("invalid inputs raise clear errors and change nothing", {
  m <- two_patient_cdm()
  for (i in seq_along(VALIDATION_CASES)) {
    args <- VALIDATION_CASES[[i]][[1]]
    message <- VALIDATION_CASES[[i]][[2]]
    # keep an explicit NULL (visit_type = NULL etc. are meaningful values), unlike `args$x <- NULL`
    call_args <- args
    if (!"materialise" %in% names(call_args)) call_args$materialise <- TRUE
    expect_error(do.call(m$define, call_args), message, info = sprintf("case %d: %s", i, message))
  }
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM cohort")$n, 0)
  expect_equal(n_temp_tables(m$con), 0)
  # outcome_visit_type = NULL falls back to inpatient
  expect_s3_class(m$define(outcome_visit_type = NULL, target_outcome = "none", followup_days = NULL,
                           require_verified_followup = FALSE), "data.frame")
})

test_that("hostile strings are never executed as SQL", {
  m <- two_patient_cdm()
  evil <- "x'); DROP TABLE person; --"
  m$define(materialise = TRUE, washin_days = 0, cohort_name = evil, cohort_description = evil, outcome_cohort_id = 2)
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM person")$n, 2)
  expect_equal(DBI::dbGetQuery(m$con, "SELECT cohort_definition_name AS n FROM cohort_definition WHERE cohort_definition_id = 1")$n, evil)
  for (bad in c("1; DROP TABLE person", "9201) OR (1=1")) {
    expect_error(m$define(visit_type = bad))
  }
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM person")$n, 2)
})

test_that("the legacy builders validate before running", {
  m <- two_patient_cdm()
  expect_error(build_readmission_cohort(m$con, index_selection_rule = "sometimes"),
               "Unsupported index_selection_rule 'sometimes'")
  expect_error(build_end_of_life_cohort(m$con, mortality_type = "forever"), "Unsupported mortality_type 'forever'")
  expect_error(build_readmission_cohort(m$con, table_name = "a b"), "Invalid table_name")
  expect_error(build_end_of_life_cohort(m$con, schema = "main; DROP TABLE person"), "Invalid schema")
  expect_equal(DBI::dbGetQuery(m$con, "SELECT COUNT(*) AS n FROM person")$n, 2)
})

test_that("the legacy builders store apostrophes in cohort names verbatim", {
  # the pre-refactor end-of-life builder escaped the quotes of the outcome definition's name twice ("O''Brien Outcome")
  m <- two_patient_cdm()
  build_end_of_life_cohort(m$con, cohort_id = 1, outcome_cohort_id = 2, cohort_name = "O'Brien", washin_days = 0)
  build_readmission_cohort(m$con, cohort_id = 3, outcome_cohort_id = 4, cohort_name = "Alice's", washin_days = 0)
  defs <- DBI::dbGetQuery(m$con, "SELECT cohort_definition_id, cohort_definition_name FROM cohort_definition ORDER BY 1")
  expect_equal(defs$cohort_definition_name, c("O'Brien", "O'Brien Outcome", "Alice's", "Alice's - Readmission Outcome"))
})

test_that("compute_attrition is reused, not reimplemented", {
  con <- fixture_cdm()
  df <- define_study_cohort(con, materialise = FALSE, washin_days = 0)
  direct <- compute_attrition(con, 1, list(list("everyone", "SELECT person_id AS subject_id FROM person")))
  expect_equal(names(attr(df, "attrition")), names(direct))
})
