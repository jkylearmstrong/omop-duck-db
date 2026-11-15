# Tests for sanitize_measurements() (RFC 6.1). They mirror tests/test_sanitize_measurements.py and assert the
# SAME golden file, tests/fixtures/sanitize_expected.csv, for the shared hand-built fixture
# tests/fixtures/sanitize_measurements.csv (69 measurement rows, 68 with a value): systolic BP of 0, 999, -5
# and exactly on both bounds, NaN / +Inf / -Inf, a NULL value, a creatinine in mg/dL and umol/L next to NULL,
# 0 and unrelated units, a concept with no limit row, groups of 2 rows, zero IQR, zero variance and an
# unmapped concept 0. The golden values are for winsorize_iqr with iqr_multiplier = 1.5 and z_score_cutoff
# with z_threshold = 2.0; the headline rows were re-derived by hand (e.g. potassium sorted 0.5, 4.0, 4.0, 4.1,
# 4.2, 4.2, 4.3, 4.4, 4.5, 15: Q1 = 4.025, Q3 = 4.375, IQR = 0.35, fences 3.5 and 4.9).

skip_if_not_installed("withr")

METHODS <- c("dqd_biologic_limits", "winsorize_iqr", "z_score_cutoff")
ACTIONS <- c("nullify", "clamp", "drop_row")
PARAMS <- list(dqd_biologic_limits = list(), winsorize_iqr = list(iqr_multiplier = 1.5),
               z_score_cutoff = list(z_threshold = 2.0))
FLAGGED <- c("below_min", "above_max", "invalid_number")
CORE_COLUMNS <- c("measurement_id", "person_id", "measurement_concept_id", "measurement_date", "unit_concept_id",
                  "value_as_number", "value_as_number_raw", "sanitize_status")
ALL_COLUMNS <- c(CORE_COLUMNS, "measurement_datetime", "visit_occurrence_id")
REPORT_COLUMNS <- c("measurement_concept_id", "unit_concept_id", "n", "n_ok", "n_below", "n_above", "n_invalid",
                    "n_changed", "n_dropped", "n_no_limit", "n_unit_skipped", "n_insufficient_data")
SBP <- 3004249; CREAT <- 3016723; POTASSIUM <- 3023103; X_CONCEPT <- 2000000001

run_params <- function(con, method, action, ...) {
  do.call(sanitize_measurements, c(list(con, method = method, action = action), PARAMS[[method]], list(...)))
}

# ---------------------------------------------------------------------------------- fixture helpers
fixture_path <- function(name) normalizePath(test_path("..", "fixtures", name), winslash = "/", mustWork = TRUE)

fx_raw <- utils::read.csv(fixture_path("sanitize_measurements.csv"), stringsAsFactors = FALSE,
                          colClasses = "character")
FX <- data.frame(
  measurement_id = as.integer(fx_raw$measurement_id),
  person_id = as.integer(fx_raw$person_id),
  concept = as.numeric(fx_raw$measurement_concept_id),
  unit = suppressWarnings(as.numeric(fx_raw$unit_concept_id)),          # "" -> NA
  value = suppressWarnings(as.numeric(fx_raw$value_as_number)),         # "" -> NA, "NaN" -> NaN, "Infinity" -> Inf
  stringsAsFactors = FALSE
)
GOLDEN <- utils::read.csv(fixture_path("sanitize_expected.csv"), stringsAsFactors = FALSE, na.strings = "",
                          colClasses = c(method = "character", measurement_id = "integer",
                                         sanitize_status = "character", value_nullify = "numeric",
                                         value_clamp = "numeric"))

load_sanitize_fixture <- function(con) {
  csv <- fixture_path("sanitize_measurements.csv")
  DBI::dbExecute(con, sprintf("
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,
                             measurement_type_concept_id, value_as_number, unit_concept_id, visit_occurrence_id)
    SELECT measurement_id, person_id, measurement_concept_id, measurement_date, 32817,
           TRY_CAST(value_as_number AS DOUBLE), unit_concept_id, measurement_id * 10
    FROM read_csv('%s', header = true, columns = {
      'measurement_id': 'INTEGER', 'person_id': 'INTEGER', 'measurement_concept_id': 'INTEGER',
      'measurement_date': 'DATE', 'unit_concept_id': 'INTEGER', 'value_as_number': 'VARCHAR'})", csv))
  # persons 1 and 2, person 1 listed twice, and person 99 who has no measurements
  DBI::dbExecute(con, "CREATE TABLE sanitize_cohort AS SELECT * FROM (VALUES
    (1, DATE '2021-01-01'), (2, DATE '2021-01-01'), (1, DATE '2021-02-01'), (99, DATE '2021-01-01'))
    AS t(subject_id, cohort_start_date)")
  invisible(con)
}

new_fixture_db <- function() {
  path <- tempfile(fileext = ".duckdb")
  utils::capture.output(omopduckdb::build_schema(path))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  load_sanitize_fixture(con)
  DBI::dbDisconnect(con, shutdown = TRUE)
  path
}

# One read-only connection (any write would error, so every test using it also proves read-only safety) and one
# writable copy for the tests that create their own objects (use unique names).
RO_PATH <- new_fixture_db()
RW_PATH <- new_fixture_db()
ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = RO_PATH, read_only = TRUE)
rw <- DBI::dbConnect(duckdb::duckdb(), dbdir = RW_PATH)
withr::defer({
  for (cn in list(ro, rw)) if (DBI::dbIsValid(cn)) DBI::dbDisconnect(cn, shutdown = TRUE)
  unlink(c(RO_PATH, RW_PATH))
}, teardown_env())

# A fresh in-memory connection with a measurement table made from `rows` (a data.frame).
mem_measurements <- function(rows, env = parent.frame(), extra_cols = TRUE) {
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE), envir = env)
  df <- data.frame(measurement_id = as.integer(rows$id), person_id = as.integer(rows$person),
                   measurement_concept_id = as.integer(rows$concept), measurement_date = rep(as.Date("2021-01-01"), nrow(rows)),
                   unit_concept_id = as.integer(rows$unit), value_as_number = as.numeric(rows$value))
  if (extra_cols) {
    df$visit_occurrence_id <- rep(NA_integer_, nrow(df))
    df$measurement_datetime <- rep(as.POSIXct(NA), nrow(df))
  }
  DBI::dbWriteTable(con, "measurement", df)
  con
}

expect_numeric_match <- function(got, want) {
  expect_equal(is.na(got) & !is.nan(got), is.na(want) & !is.nan(want))   # NULLs line up
  expect_equal(is.nan(got), is.nan(want))
  fin <- is.finite(want)
  expect_equal(got[fin], want[fin], tolerance = 1e-9)
  inf <- is.infinite(want)
  expect_identical(got[inf], want[inf])
}

by_id <- function(df) {
  rownames(df) <- df$measurement_id
  df
}

# ---------------------------------------------------------------------------------- reference model
# Independent (base R) statement of the rules -> data.frame(measurement_id, status, lo, hi).
# rows: data.frame(measurement_id, concept, unit, value); limits: data.frame(concept, unit, lo, hi).
reference <- function(rows, method, limits = NULL, k_iqr = 3, z_thr = 4) {
  rows <- rows[!is.na(rows$value) | is.nan(rows$value), , drop = FALSE]
  n <- nrow(rows)
  status <- character(n); lo <- rep(NA_real_, n); hi <- rep(NA_real_, n)
  if (method == "dqd") {
    for (i in seq_len(n)) {
      v <- rows$value[i]; cid <- rows$concept[i]; u <- rows$unit[i]
      b <- NULL
      if (!is.na(u)) b <- limits[limits$concept == cid & !is.na(limits$unit) & limits$unit == u, , drop = FALSE]
      if (is.null(b) || nrow(b) == 0L) b <- limits[limits$concept == cid & is.na(limits$unit), , drop = FALSE]
      has <- nrow(b) > 0L
      if (has) { lo[i] <- b$lo[1]; hi[i] <- b$hi[1] }
      status[i] <- if (!is.finite(v)) "invalid_number"
        else if (!has) (if (cid %in% limits$concept) "unit_skipped" else "no_limit")
        else if (!is.na(lo[i]) && v < lo[i]) "below_min"
        else if (!is.na(hi[i]) && v > hi[i]) "above_max"
        else "ok"
    }
  } else {
    key <- paste(rows$concept, rows$unit)
    usable <- is.finite(rows$value) & rows$concept != 0
    for (g in unique(key[usable])) {
      vals <- rows$value[usable & key == g]
      if (length(vals) < 3L) next
      if (method == "iqr") {
        q <- unname(stats::quantile(vals, c(0.25, 0.75), type = 7))
        if (!(q[2] - q[1] > 0)) next
        f <- c(q[1] - k_iqr * (q[2] - q[1]), q[2] + k_iqr * (q[2] - q[1]))
      } else {
        s <- stats::sd(vals)
        if (!(s > 0)) next
        f <- c(mean(vals) - z_thr * s, mean(vals) + z_thr * s)
      }
      lo[key == g] <- f[1]; hi[key == g] <- f[2]
    }
    for (i in seq_len(n)) {
      v <- rows$value[i]
      status[i] <- if (!is.finite(v)) "invalid_number"
        else if (rows$concept[i] == 0) "no_limit"
        else if (is.na(lo[i])) "insufficient_data"
        else if (v < lo[i]) "below_min"
        else if (v > hi[i]) "above_max"
        else "ok"
    }
  }
  data.frame(measurement_id = rows$measurement_id, status = status, lo = lo, hi = hi, stringsAsFactors = FALSE)
}

reference_clean <- function(v, st, lo, hi, action) {
  out <- v
  below <- st == "below_min"; above <- st == "above_max"; inval <- st == "invalid_number"
  out[below | above | inval] <- NA_real_
  if (action == "clamp") {
    out[below] <- lo[below]
    out[above] <- hi[above]
    out[inval & is.infinite(v) & v > 0] <- hi[inval & is.infinite(v) & v > 0]
    out[inval & is.infinite(v) & v < 0] <- lo[inval & is.infinite(v) & v < 0]
  }
  out
}

bundled_ref_limits <- function() {
  b <- omopduckdb:::.sanitize_bundled_limits()
  data.frame(concept = b$concept_id, unit = b$unit_concept_id, lo = b$min_value, hi = b$max_value)
}

# ---------------------------------------------------------------------------------- golden matrix
test_that("the golden file matches the independent reference model", {
  # guards against a stale golden file (regenerate with tests/fixtures/sanitize_make_golden.py)
  expect_equal(nrow(GOLDEN), 3L * 68L)
  for (spec in list(c("dqd_biologic_limits", "dqd"), c("winsorize_iqr", "iqr"), c("z_score_cutoff", "z"))) {
    ref <- reference(FX, spec[2], limits = bundled_ref_limits(), k_iqr = 1.5, z_thr = 2)
    g <- GOLDEN[GOLDEN$method == spec[1], ]
    g <- g[match(ref$measurement_id, g$measurement_id), ]
    expect_identical(g$sanitize_status, ref$status)
    raw <- FX$value[match(ref$measurement_id, FX$measurement_id)]
    expect_numeric_match(g$value_nullify, reference_clean(raw, ref$status, ref$lo, ref$hi, "nullify"))
    expect_numeric_match(g$value_clamp, reference_clean(raw, ref$status, ref$lo, ref$hi, "clamp"))
  }
})

for (method in METHODS) for (action in ACTIONS) local({
  method <- method; action <- action
  test_that(sprintf("golden matrix: %s x %s", method, action), {
    exp <- GOLDEN[GOLDEN$method == method, ]
    exp <- exp[order(exp$measurement_id), ]
    df <- run_params(ro, method, action)
    flagged_ids <- exp$measurement_id[exp$sanitize_status %in% FLAGGED]
    kept <- if (action == "drop_row") setdiff(exp$measurement_id, flagged_ids) else exp$measurement_id
    expect_equal(df$measurement_id, sort(kept))          # right rows, ordered by measurement_id
    e <- exp[match(df$measurement_id, exp$measurement_id), ]
    expect_identical(df$sanitize_status, e$sanitize_status)
    want <- switch(action, nullify = e$value_nullify, clamp = e$value_clamp,
                   drop_row = FX$value[match(df$measurement_id, FX$measurement_id)])
    expect_numeric_match(df$value_as_number, want)
    expect_numeric_match(df$value_as_number_raw, FX$value[match(df$measurement_id, FX$measurement_id)])
    fx <- FX[match(df$measurement_id, FX$measurement_id), ]
    expect_equal(df$person_id, fx$person_id)
    expect_equal(df$measurement_concept_id, fx$concept)
    expect_equal(df$unit_concept_id, fx$unit)
    expect_equal(df$visit_occurrence_id, df$measurement_id * 10L)
  })
})

test_that("output shape, columns and types", {
  df <- sanitize_measurements(ro)
  expect_s3_class(df, "data.frame")
  expect_identical(names(df), ALL_COLUMNS)
  expect_false(10L %in% df$measurement_id)          # the NULL-valued row is out of scope
  expect_equal(nrow(df), 68L)
  expect_s3_class(df$measurement_date, "Date")
  expect_identical(names(attr(df, "sanitization_report")), REPORT_COLUMNS)
})

# ---------------------------------------------------------------------------------- report
for (method in METHODS) for (action in ACTIONS) local({
  method <- method; action <- action
  test_that(sprintf("report reconciles with the rows and the golden file: %s x %s", method, action), {
    df <- run_params(ro, method, action)
    rep <- attr(df, "sanitization_report")
    expect_identical(names(rep), REPORT_COLUMNS)
    status_cols <- c("n_ok", "n_below", "n_above", "n_invalid", "n_no_limit", "n_unit_skipped", "n_insufficient_data")
    expect_equal(rep$n, rowSums(rep[status_cols]))
    expect_equal(rep$n_changed + rep$n_dropped, rep$n_below + rep$n_above + rep$n_invalid)
    if (action == "drop_row") {
      expect_true(all(rep$n_changed == 0))
      expect_equal(sum(rep$n_dropped), 68L - nrow(df))
    } else {
      expect_true(all(rep$n_dropped == 0))
      expect_equal(nrow(df), 68L)
    }
    expect_equal(sum(rep$n), 68L)        # every in-scope row, including dropped ones
    if (method != "dqd_biologic_limits") expect_true(all(rep$n_unit_skipped == 0)) else expect_true(all(rep$n_insufficient_data == 0))
    expect_type(rep$n, "integer")

    # independent tally straight from the golden statuses
    g <- GOLDEN[GOLDEN$method == method, ]
    g$concept <- FX$concept[match(g$measurement_id, FX$measurement_id)]
    g$unit <- FX$unit[match(g$measurement_id, FX$measurement_id)]
    key_g <- paste(g$concept, g$unit)
    key_r <- paste(rep$measurement_concept_id, rep$unit_concept_id)
    expect_setequal(key_r, unique(key_g))
    for (i in seq_len(nrow(rep))) {
      st <- g$sanitize_status[key_g == key_r[i]]
      expect_equal(rep$n[i], length(st))
      expect_equal(c(rep$n_ok[i], rep$n_below[i], rep$n_above[i], rep$n_invalid[i], rep$n_no_limit[i],
                     rep$n_unit_skipped[i], rep$n_insufficient_data[i]),
                   c(sum(st == "ok"), sum(st == "below_min"), sum(st == "above_max"), sum(st == "invalid_number"),
                     sum(st == "no_limit"), sum(st == "unit_skipped"), sum(st == "insufficient_data")))
    }
    # ordered by concept, then unit with NULL last
    ord <- order(rep$measurement_concept_id, rep$unit_concept_id, na.last = TRUE)
    expect_equal(ord, seq_len(nrow(rep)))
  })
})

# ---------------------------------------------------------------------------------- dqd_biologic_limits
test_that("bounds are inclusive", {
  df <- by_id(sanitize_measurements(ro))
  for (i in c(4, 5, 16, 17)) {      # SBP 40 and 300, creatinine mg/dL 0.1 and 30 sit exactly on the bounds
    expect_identical(df[as.character(i), "sanitize_status"], "ok")
    expect_equal(df[as.character(i), "value_as_number"], FX$value[FX$measurement_id == i])
  }
  expect_identical(df["12", "sanitize_status"], "below_min")    # 39
  expect_identical(df["13", "sanitize_status"], "above_max")    # 301
  lim <- data.frame(concept_id = SBP, min_value = 120, max_value = 125)
  own <- by_id(sanitize_measurements(ro, limits = lim, measurement_concept_ids = SBP))
  expect_identical(own["1", "sanitize_status"], "ok")
  expect_identical(own["14", "sanitize_status"], "ok")
})

test_that("unit awareness never misapplies bounds", {
  df <- by_id(sanitize_measurements(ro, action = "nullify"))
  # umol/L creatinine is judged against the umol/L row, not the mg/dL one (88 and 100 would be far above 30)
  expect_equal(df["20", "value_as_number"], 88); expect_identical(df["20", "sanitize_status"], "ok")
  expect_equal(df["23", "value_as_number"], 100); expect_identical(df["23", "sanitize_status"], "ok")
  expect_identical(df["22", "sanitize_status"], "above_max")      # 3000 umol/L > 2650
  # NULL unit, unit 0 and an unlisted unit: untouched and unit_skipped
  for (i in c(24, 25, 26, 62)) {
    expect_identical(df[as.character(i), "sanitize_status"], "unit_skipped")
    expect_equal(df[as.character(i), "value_as_number"], df[as.character(i), "value_as_number_raw"])
  }
  expect_true(all(df[as.character(37:49), "sanitize_status"] == "no_limit"))      # concept without a limit row
  rep <- attr(sanitize_measurements(ro), "sanitization_report")
  expect_equal(sum(rep$n_unit_skipped[rep$measurement_concept_id == CREAT]), 3L)
  expect_equal(sum(rep$n_no_limit[rep$measurement_concept_id == CREAT]), 0L)
  # 13 rows of X have a finite value (id 65 is +Inf, i.e. invalid_number, not no_limit)
  expect_equal(sum(rep$n_no_limit[rep$measurement_concept_id == X_CONCEPT]), 13L)
})

test_that("concept 0 is never pooled by the statistical methods", {
  for (method in c("winsorize_iqr", "z_score_cutoff")) {
    df <- run_params(ro, method, "drop_row")
    zero <- df[df$measurement_concept_id == 0, ]
    expect_identical(zero$sanitize_status, rep("no_limit", 4))
    expect_equal(zero$value_as_number, c(12345, 1, 2, -1000))
  }
})

# ---------------------------------------------------------------------------------- override limits
test_that("limits without a unit replace the whole concept", {
  lim <- data.frame(concept_id = SBP, min_value = 100, max_value = 200)
  df <- by_id(sanitize_measurements(ro, limits = lim, action = "clamp"))
  expected <- list(`1` = c("ok", 120), `2` = c("below_min", 100), `3` = c("above_max", 200), `4` = c("below_min", 100),
                   `5` = c("above_max", 200), `6` = c("below_min", 100), `11` = c("ok", 135),
                   `12` = c("below_min", 100), `13` = c("above_max", 200), `14` = c("ok", 125))
  for (i in names(expected)) {
    expect_identical(df[i, "sanitize_status"], expected[[i]][1])
    expect_equal(df[i, "value_as_number"], as.numeric(expected[[i]][2]))
  }
  base <- by_id(sanitize_measurements(ro, action = "clamp"))
  other <- as.character(FX$measurement_id[FX$concept != SBP & !is.na(FX$value)])
  expect_identical(df[other, "sanitize_status"], base[other, "sanitize_status"])
})

test_that("limits with a unit replace only that unit", {
  lim <- data.frame(concept_id = CREAT, unit_concept_id = 8749, min_value = 90, max_value = 2000)
  df <- by_id(sanitize_measurements(ro, limits = lim))
  expect_identical(df["20", "sanitize_status"], "below_min")     # 88 < 90
  expect_identical(df["23", "sanitize_status"], "ok")            # 100
  expect_identical(df["21", "sanitize_status"], "above_max")     # 2650 > 2000
  expect_identical(df["18", "sanitize_status"], "above_max")     # mg/dL still the bundled 0.1-30
  expect_identical(df["19", "sanitize_status"], "below_min")
})

test_that("limits for a new concept, open bounds and any-unit rows", {
  lim <- data.frame(concept_id = X_CONCEPT, unit_concept_id = 8840, min_value = 0, max_value = 20)
  df <- by_id(sanitize_measurements(ro, limits = lim))
  expect_identical(df["37", "sanitize_status"], "below_min"); expect_identical(df["47", "sanitize_status"], "above_max")
  expect_identical(df["38", "sanitize_status"], "ok")
  expect_identical(df["48", "sanitize_status"], "unit_skipped"); expect_identical(df["49", "sanitize_status"], "unit_skipped")

  lim <- data.frame(concept_id = X_CONCEPT, min_value = NA_real_, max_value = 20)   # NA = open lower bound
  df <- by_id(sanitize_measurements(ro, limits = lim))
  expect_identical(df["37", "sanitize_status"], "ok"); expect_identical(df["47", "sanitize_status"], "above_max")
  expect_identical(df["48", "sanitize_status"], "ok"); expect_identical(df["49", "sanitize_status"], "above_max")

  lim <- data.frame(concept_id = X_CONCEPT, min_value = 0, max_value = NA_real_)
  df <- by_id(sanitize_measurements(ro, limits = lim, action = "clamp"))
  expect_equal(df["37", "value_as_number"], 0); expect_equal(df["47", "value_as_number"], 100)
  expect_true(is.na(df["65", "value_as_number"]))      # +Inf with an open upper bound falls back to NULL
})

test_that("a unit-specific row wins over an any-unit row", {
  lim <- data.frame(concept_id = c(X_CONCEPT, X_CONCEPT), unit_concept_id = c(NA, 8840),
                    min_value = c(0, 11), max_value = c(1000, 17))
  df <- by_id(sanitize_measurements(ro, limits = lim))
  expect_identical(df["38", "sanitize_status"], "below_min")     # 10 < 11 (unit-specific row)
  expect_identical(df["46", "sanitize_status"], "above_max")     # 18 > 17
  expect_identical(df["49", "sanitize_status"], "ok")            # 8749 falls back to the any-unit row
})

test_that("limits column names are case-insensitive and extra columns are ignored", {
  lim <- data.frame(Concept_ID = SBP, MIN_VALUE = 100, Max_Value = 200, name = "SBP", x = 1)
  expect_identical(by_id(sanitize_measurements(ro, limits = lim))["2", "sanitize_status"], "below_min")
  expect_identical(by_id(sanitize_measurements(
    ro, limits = list(concept_id = SBP, min_value = 100, max_value = 200)))["2", "sanitize_status"], "below_min")
})

test_that("limits is ignored with a warning for the statistical methods", {
  lim <- data.frame(concept_id = SBP, min_value = 100, max_value = 200)
  expect_warning(df <- sanitize_measurements(ro, method = "winsorize_iqr", limits = lim, iqr_multiplier = 1.5),
                 "limits is only used")
  expect_identical(by_id(df)["2", "sanitize_status"], "ok")
})

test_that("overrides do not leak between calls", {
  sanitize_measurements(ro, limits = data.frame(concept_id = SBP, min_value = 100, max_value = 200))
  df <- by_id(sanitize_measurements(ro))
  expect_identical(df["2", "sanitize_status"], "below_min"); expect_identical(df["1", "sanitize_status"], "ok")
  expect_identical(df["12", "sanitize_status"], "below_min")     # 39 < 40 (bundled bound, not 100)
  expect_identical(df["4", "sanitize_status"], "ok")
})

# ---------------------------------------------------------------------------------- scope
test_that("cohort restriction (row-wise method)", {
  df <- sanitize_measurements(ro, cohort_table = "sanitize_cohort", action = "nullify")
  want <- sort(FX$measurement_id[FX$person_id %in% c(1, 2) & !is.na(FX$value)])
  expect_equal(df$measurement_id, want)       # persons 1 and 2 only, each measurement once
  expect_setequal(df$person_id, c(1, 2))
  g <- GOLDEN[GOLDEN$method == "dqd_biologic_limits", ]
  expect_identical(df$sanitize_status, g$sanitize_status[match(df$measurement_id, g$measurement_id)])
  expect_equal(sum(attr(df, "sanitization_report")$n), length(want))
})

test_that("cohort restriction with drop_row and the report", {
  df <- sanitize_measurements(ro, cohort_table = "sanitize_cohort", action = "drop_row")
  scope <- FX[FX$person_id %in% c(1, 2) & !is.na(FX$value), ]
  g <- GOLDEN[GOLDEN$method == "dqd_biologic_limits", ]
  flagged <- g$measurement_id[g$sanitize_status %in% FLAGGED]
  kept <- sort(setdiff(scope$measurement_id, flagged))
  expect_equal(df$measurement_id, kept)
  rep <- attr(df, "sanitization_report")
  expect_equal(sum(rep$n), nrow(scope))
  expect_equal(sum(rep$n_dropped), nrow(scope) - length(kept))
  expect_gt(sum(rep$n_dropped), 0)
  expect_true(all(rep$n_changed == 0))
})

test_that("method and action are trimmed and case-insensitive", {
  want <- sanitize_measurements(ro, method = "winsorize_iqr", action = "clamp")
  got <- sanitize_measurements(ro, method = "  Winsorize_IQR ", action = "CLAMP")
  expect_identical(got, want)
})

test_that("cohort restriction scopes the statistics too", {
  rows <- FX[FX$person_id %in% c(1, 2), ]
  for (spec in list(c("winsorize_iqr", "iqr"), c("z_score_cutoff", "z"))) {
    df <- run_params(ro, spec[1], "clamp", cohort_table = "sanitize_cohort")
    ref <- reference(rows, spec[2], limits = bundled_ref_limits(), k_iqr = 1.5, z_thr = 2)
    expect_equal(df$measurement_id, sort(ref$measurement_id))
    r <- ref[match(df$measurement_id, ref$measurement_id), ]
    expect_identical(df$sanitize_status, r$status)
    raw <- rows$value[match(df$measurement_id, rows$measurement_id)]
    expect_numeric_match(df$value_as_number, reference_clean(raw, r$status, r$lo, r$hi, "clamp"))
  }
  # restricting really changes the answer (otherwise this test proves nothing)
  full <- by_id(sanitize_measurements(ro, method = "winsorize_iqr", iqr_multiplier = 1.5))
  sub <- by_id(sanitize_measurements(ro, cohort_table = "sanitize_cohort", method = "winsorize_iqr", iqr_multiplier = 1.5))
  expect_true(any(full[rownames(sub), "sanitize_status"] != sub$sanitize_status))
})

test_that("the cohort person column is validated with no fallback", {
  DBI::dbExecute(rw, "CREATE TABLE sanitize_cohort_pid AS SELECT person_id FROM (VALUES (1), (3)) t(person_id)")
  expect_error(sanitize_measurements(rw, cohort_table = "sanitize_cohort_pid"),
               "person_col 'subject_id' not found.*Available columns: \\['person_id'\\]")
  df <- sanitize_measurements(rw, cohort_table = "sanitize_cohort_pid", person_col = "PERSON_ID")   # case-insensitive
  expect_setequal(df$person_id, c(1, 3))
  df <- sanitize_measurements(rw, cohort_table = "main.sanitize_cohort")        # schema-qualified
  expect_setequal(df$person_id, c(1, 2))
})

test_that("concept restriction", {
  df <- sanitize_measurements(ro, measurement_concept_ids = c(SBP, POTASSIUM, SBP))
  expect_setequal(df$measurement_concept_id, c(SBP, POTASSIUM))
  expect_equal(nrow(df), 13L + 10L)
  expect_equal(nrow(sanitize_measurements(ro, measurement_concept_ids = POTASSIUM)), 10L)
  both <- sanitize_measurements(ro, cohort_table = "sanitize_cohort", measurement_concept_ids = SBP)
  expect_setequal(both$person_id, c(1, 2)); expect_setequal(both$measurement_concept_id, SBP)
  none <- sanitize_measurements(ro, measurement_concept_ids = 123456789)       # absent ids simply match nothing
  expect_equal(nrow(none), 0L)
  expect_identical(names(none), ALL_COLUMNS)
  rep <- attr(none, "sanitization_report")
  expect_equal(nrow(rep), 0L); expect_identical(names(rep), REPORT_COLUMNS)
})

# ---------------------------------------------------------------------------------- statistical edge cases
test_that("the group size threshold is three", {
  rows <- data.frame(id = 1:5, person = 1L, concept = c(7, 7, 7, 8, 8), unit = 8840L,
                     value = c(1, 2, 100, 1, 1000))        # concept 7: exactly 3 -> screened; concept 8: 2 rows
  con <- mem_measurements(rows)
  for (method in c("winsorize_iqr", "z_score_cutoff")) {
    df <- sanitize_measurements(con, method = method)
    expect_identical(df$sanitize_status, c("ok", "ok", "ok", "insufficient_data", "insufficient_data"))
    expect_equal(df$value_as_number, c(1, 2, 100, 1, 1000))
  }
  df <- sanitize_measurements(con, method = "winsorize_iqr", iqr_multiplier = 0.01)   # tiny k: 100 is above the fence
  expect_identical(df$sanitize_status[3], "above_max")
})

test_that("zero IQR and zero variance are insufficient_data", {
  iqr <- by_id(sanitize_measurements(ro, method = "winsorize_iqr", iqr_multiplier = 1.5))
  expect_true(all(iqr[as.character(50:54), "sanitize_status"] == "insufficient_data"))   # 7, 7, 7, 7, 9: IQR is 0
  z <- by_id(sanitize_measurements(ro, method = "z_score_cutoff", z_threshold = 2))
  expect_true(all(z[as.character(50:54), "sanitize_status"] == "ok"))                    # sd > 0, 9 has z = 1.79
  for (df in list(iqr, z)) {
    expect_true(all(df[as.character(55:58), "sanitize_status"] == "insufficient_data"))  # 5, 5, 5, 5
    expect_identical(df["59", "sanitize_status"], "invalid_number")                      # NaN in a degenerate group
    expect_equal(df[as.character(55:58), "value_as_number"], rep(5, 4))
  }
  tight <- by_id(sanitize_measurements(ro, method = "z_score_cutoff", z_threshold = 1.5))
  expect_identical(tight["54", "sanitize_status"], "above_max")                          # z = 1.79 > 1.5
})

test_that("default thresholds and the two statistical methods disagree as designed", {
  iqr <- by_id(sanitize_measurements(ro, method = "winsorize_iqr"))
  z <- by_id(sanitize_measurements(ro, method = "z_score_cutoff"))
  expect_identical(iqr["3", "sanitize_status"], "above_max")      # 999 is far outside the k = 3 fences
  expect_identical(z["3", "sanitize_status"], "ok")               # but |z| < 4 with only 10 values
  expect_false(any(z$sanitize_status %in% c("above_max", "below_min")))
})

test_that("random data matches the reference model", {
  set.seed(11)
  n <- 600
  concept <- sample(c(SBP, SBP, X_CONCEPT, 0), n, replace = TRUE)
  unit <- ifelse(concept == SBP, 8876, sample(c(8876, NA, 0), n, replace = TRUE))
  roll <- runif(n)
  value <- round(rnorm(n, 120, 18), 3)
  value[roll < 0.12] <- sample(c(-20, 0, 350, 999, 5000), sum(roll < 0.12), replace = TRUE)
  value[roll < 0.06] <- NA
  value[roll < 0.04] <- sample(c(Inf, -Inf), sum(roll < 0.04), replace = TRUE)
  value[roll < 0.02] <- NaN
  rows <- data.frame(id = seq_len(n), person = seq_len(n) %% 7 + 1, concept = concept, unit = unit, value = value)
  con <- mem_measurements(rows)
  model_rows <- data.frame(measurement_id = rows$id, concept = concept, unit = unit, value = value)
  lims <- bundled_ref_limits()
  for (spec in list(c("dqd_biologic_limits", "dqd"), c("winsorize_iqr", "iqr"), c("z_score_cutoff", "z"))) {
    ref <- reference(model_rows, spec[2], limits = lims, k_iqr = 1.5, z_thr = 2)
    raw_of <- function(ids) model_rows$value[match(ids, model_rows$measurement_id)]
    for (action in c("nullify", "clamp")) {
      df <- sanitize_measurements(con, method = spec[1], action = action, iqr_multiplier = 1.5, z_threshold = 2)
      expect_equal(df$measurement_id, sort(ref$measurement_id))
      r <- ref[match(df$measurement_id, ref$measurement_id), ]
      expect_identical(df$sanitize_status, r$status)
      expect_numeric_match(df$value_as_number, reference_clean(raw_of(df$measurement_id), r$status, r$lo, r$hi, action))
    }
    drop <- sanitize_measurements(con, method = spec[1], action = "drop_row", iqr_multiplier = 1.5, z_threshold = 2)
    expect_setequal(drop$measurement_id, ref$measurement_id[!ref$status %in% FLAGGED])
  }
})

# ---------------------------------------------------------------------------------- invalid numbers
for (method in METHODS) local({
  method <- method
  test_that(sprintf("NaN and infinity are invalid under %s", method), {
    ids <- c(7, 8, 9, 59, 65)
    nullified <- by_id(run_params(ro, method, "nullify"))
    expect_true(all(nullified[as.character(ids), "sanitize_status"] == "invalid_number"))
    expect_true(all(is.na(nullified[as.character(ids), "value_as_number"])))
    dropped <- run_params(ro, method, "drop_row")
    expect_length(intersect(ids, dropped$measurement_id), 0L)
    clamped <- by_id(run_params(ro, method, "clamp"))
    expect_true(is.na(clamped["7", "value_as_number"]) && is.na(clamped["59", "value_as_number"]))   # NaN -> NULL
    expect_true(is.nan(clamped["7", "value_as_number_raw"]))
    expect_identical(clamped["8", "value_as_number_raw"], Inf)
  })
})

test_that("clamp sends infinity to the nearest finite bound", {
  dqd <- by_id(sanitize_measurements(ro, action = "clamp"))
  expect_equal(dqd["8", "value_as_number"], 300); expect_equal(dqd["9", "value_as_number"], 40)
  expect_true(is.na(dqd["65", "value_as_number"]))      # no limit row -> no bound -> NULL
  iqr <- by_id(sanitize_measurements(ro, method = "winsorize_iqr", action = "clamp", iqr_multiplier = 1.5))
  expect_equal(iqr["8", "value_as_number"], 588); expect_equal(iqr["9", "value_as_number"], -290)
  expect_equal(iqr["65", "value_as_number"], 24)
})

test_that("clamp moves values to the violated bound", {
  df <- by_id(sanitize_measurements(ro, action = "clamp"))
  expect_equal(unname(unlist(df[c("2", "3", "6"), "value_as_number"])), c(40, 300, 40))
  expect_equal(df["19", "value_as_number"], 0.1); expect_equal(df["22", "value_as_number"], 2650)
  expect_equal(df["2", "value_as_number_raw"], 0); expect_equal(df["3", "value_as_number_raw"], 999)
})

# ---------------------------------------------------------------------------------- safety
catalog_snapshot <- function(con) {
  list(
    tables = DBI::dbGetQuery(con, "SELECT database_name, schema_name, table_name, temporary FROM duckdb_tables() ORDER BY ALL"),
    views = DBI::dbGetQuery(con, "SELECT database_name, schema_name, view_name, temporary FROM duckdb_views() WHERE NOT internal ORDER BY ALL"),
    functions = DBI::dbGetQuery(con, "SELECT function_name, function_type FROM duckdb_functions() WHERE NOT internal ORDER BY ALL"),
    sequences = DBI::dbGetQuery(con, "SELECT sequence_name FROM duckdb_sequences() ORDER BY ALL"),
    data = DBI::dbGetQuery(con, "SELECT md5(string_agg(CAST(t AS VARCHAR), '|' ORDER BY measurement_id)) AS h FROM measurement AS t")
  )
}

test_that("a read-only connection works and the database file is untouched", {
  before <- unname(tools::md5sum(RO_PATH))
  listing <- sort(list.files(dirname(RO_PATH)))
  for (method in METHODS) for (action in ACTIONS) {
    lim <- if (method == "dqd_biologic_limits") data.frame(concept_id = SBP, min_value = 1, max_value = 2) else NULL
    sanitize_measurements(ro, cohort_table = "sanitize_cohort", method = method, action = action, limits = lim)
  }
  expect_identical(unname(tools::md5sum(RO_PATH)), before)
  expect_identical(sort(list.files(dirname(RO_PATH))), listing)       # no WAL / temp file next to the database
  expect_error(DBI::dbExecute(ro, "CREATE TABLE should_fail AS SELECT 1"))     # the connection really is read-only
})

test_that("it creates no objects and changes no data", {
  before <- catalog_snapshot(rw)
  for (method in METHODS) for (action in ACTIONS) {
    sanitize_measurements(rw, cohort_table = "sanitize_cohort", method = method, action = action)
  }
  expect_identical(catalog_snapshot(rw), before)
})

test_that("identifiers are quoted, not spliced", {
  for (bad in c("sanitize_cohort; DROP TABLE measurement", "sanitize_cohort\" --", "sanitize_cohort) UNION SELECT")) {
    expect_error(sanitize_measurements(rw, cohort_table = bad), "could not be read")
  }
  expect_error(sanitize_measurements(rw, cohort_table = "sanitize_cohort", person_col = "subject_id\") FROM x --"),
               "not found")
  expect_equal(DBI::dbGetQuery(rw, "SELECT count(*) AS n FROM measurement")$n, 69)
})

test_that("a cohort table named like an internal CTE still resolves", {
  # the generated WITH chain uses short CTE names; a user table of the same name must not be shadowed
  for (name in c("base", "lim", "stats", "joined", "cls", "fin", "zscale", "c", "m")) {
    DBI::dbExecute(rw, sprintf('CREATE OR REPLACE TABLE "%s" AS SELECT * FROM (VALUES (1), (2)) t(subject_id)', name))
    for (method in METHODS) {
      df <- run_params(rw, method, "nullify", cohort_table = name)
      expect_setequal(df$person_id, c(1, 2))
    }
    DBI::dbExecute(rw, sprintf('DROP TABLE IF EXISTS "%s"', name))
  }
})

# ---------------------------------------------------------------------------------- argument validation
test_that("argument validation", {
  bad <- function(pattern, ...) expect_error(sanitize_measurements(rw, ...), pattern)
  bad("method must be one of", method = "bogus")
  bad("method must be one of", method = NULL)
  bad("method must be one of", method = c("winsorize_iqr", "z_score_cutoff"))
  bad("action must be one of", action = "delete")
  for (v in list(0, -1.5, NaN, Inf, "3", TRUE, NULL, NA_real_, c(1, 2))) bad("iqr_multiplier", iqr_multiplier = v)
  for (v in list(0, -2, NaN, Inf, "4", FALSE, NULL, NA_real_, c(1, 2))) bad("z_threshold", z_threshold = v)
  bad("is empty", measurement_concept_ids = numeric(0))
  bad("whole number", measurement_concept_ids = 1.5)
  bad("whole number", measurement_concept_ids = c(1, NA))
  bad("whole number", measurement_concept_ids = c(1, Inf))
  bad("vector of integer", measurement_concept_ids = "abc")
  bad("vector of integer", measurement_concept_ids = TRUE)
  bad("non-empty table", cohort_table = "")
  bad("non-empty table", cohort_table = 5)
  bad("non-empty table", cohort_table = NA_character_)
  bad("schema.table", cohort_table = "a.b.c.d")
  bad("schema.table", cohort_table = "a..b")
  bad("schema.table", cohort_table = "a.")
  bad("could not be read", cohort_table = "no_such_table")
  bad("person_col", cohort_table = "sanitize_cohort", person_col = "")
  bad("person_col", cohort_table = "sanitize_cohort", person_col = NA_character_)
  bad("person_col 'person_id' not found", cohort_table = "sanitize_cohort", person_col = "person_id")
  bad("missing required column", limits = data.frame(concept_id = 1, min_value = 0))
  bad("limits must be a data.frame", limits = 5)
  bad("greater than max_value", limits = data.frame(concept_id = 1, min_value = 5, max_value = 1))
  bad("both missing", limits = data.frame(concept_id = 1, min_value = NA_real_, max_value = NA_real_))
  bad("must be finite", limits = data.frame(concept_id = 1, min_value = 0, max_value = Inf))
  bad("numeric or missing", limits = data.frame(concept_id = 1, min_value = "abc", max_value = 3))
  bad("whole number", limits = data.frame(concept_id = 1.5, min_value = 0, max_value = 3))
  bad("whole number", limits = data.frame(concept_id = "abc", min_value = 0, max_value = 3))
  bad("concept_id is missing", limits = data.frame(concept_id = NA_real_, min_value = 0, max_value = 3))
  bad("duplicate", limits = data.frame(concept_id = c(1, 1), min_value = c(0, 1), max_value = c(3, 4)))
  bad("duplicate", limits = data.frame(concept_id = c(1, 1), unit_concept_id = c(8876, 8876),
                                       min_value = c(0, 1), max_value = c(3, 4)))
})

test_that("a missing measurement table or column is reported", {
  empty <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(empty, shutdown = TRUE))
  expect_error(sanitize_measurements(empty), "needs a CDM 'measurement' table")
  DBI::dbExecute(empty, "CREATE TABLE measurement (measurement_id INTEGER, person_id INTEGER, value_as_number DOUBLE)")
  expect_error(sanitize_measurements(empty),
               "missing required column\\(s\\) \\['measurement_concept_id', 'measurement_date', 'unit_concept_id'\\]")
})

# ---------------------------------------------------------------------------------- schemas
test_that("a minimal measurement table with DECIMAL values works and optional columns are only passed through", {
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id INTEGER, person_id INTEGER,
    measurement_concept_id INTEGER, measurement_date DATE, unit_concept_id INTEGER, value_as_number DECIMAL(18, 3))")
  DBI::dbExecute(con, sprintf("INSERT INTO measurement VALUES (1, 1, %d, DATE '2021-01-01', 8876, 120.5),
    (2, 1, %d, DATE '2021-01-02', 8876, 999.0), (3, 1, %d, DATE '2021-01-03', 8876, NULL)", SBP, SBP, SBP))
  df <- sanitize_measurements(con)
  expect_identical(names(df), CORE_COLUMNS)
  expect_identical(df$sanitize_status, c("ok", "above_max"))
  expect_equal(df$value_as_number, c(120.5, NA))
})

test_that("an empty measurement table returns empty frames", {
  con <- mem_measurements(data.frame(id = integer(0), person = integer(0), concept = integer(0),
                                     unit = integer(0), value = numeric(0)))
  for (method in METHODS) {
    df <- sanitize_measurements(con, method = method)
    expect_equal(nrow(df), 0L)
    expect_identical(names(df)[1:8], CORE_COLUMNS)
    rep <- attr(df, "sanitization_report")
    expect_identical(names(rep), REPORT_COLUMNS)
    expect_equal(nrow(rep), 0L)
  }
})

# ---------------------------------------------------------------------------------- warnings, extremes
sbp_rows <- function(units) {
  data.frame(id = seq_along(units), person = 1L, concept = SBP, unit = as.integer(units), value = 120 + seq_along(units))
}

test_that("it warns when most rows of a limit-bearing concept have no unit", {
  # 3 of 4 blood pressures have unit 0 / NULL: they are unit_skipped and the user must hear about it
  rows <- rbind(sbp_rows(c(8876, 0, NA, 0)),
                data.frame(id = 9L, person = 1L, concept = POTASSIUM, unit = 8753L, value = 4))
  con <- mem_measurements(rows)
  expect_warning(df <- sanitize_measurements(con),
                 "more than half of the measurements of 1 concept\\(s\\).*concept_id 3004249")
  expect_equal(sum(df$sanitize_status == "unit_skipped"), 3L)
  # more than five offending concepts: the message lists five and the total
  concepts <- sort(unique(omopduckdb:::.sanitize_bundled_limits()$concept_id))[1:7]
  many <- mem_measurements(data.frame(id = 1:7, person = 1L, concept = concepts, unit = 0L, value = 1))
  expect_warning(sanitize_measurements(many), "7 concept\\(s\\).*\\.\\.\\. \\(7 in all\\)")
})

test_that("there is no unit warning for half or fewer unit-less rows, user limits or the statistical methods", {
  quiet <- function(expr) expect_warning(expr, NA)
  quiet(sanitize_measurements(mem_measurements(sbp_rows(c(8876, 0, 8876, 0)))))          # exactly half
  quiet(sanitize_measurements(mem_measurements(sbp_rows(c(8876, 8876, 8876, 0)))))
  allnull <- mem_measurements(sbp_rows(c(NA, NA, 0, 0)))
  # an any-unit limit screens unit-less rows, so nothing is unscreened
  quiet(sanitize_measurements(allnull, limits = data.frame(concept_id = SBP, min_value = 40, max_value = 300)))
  quiet(sanitize_measurements(allnull, method = "winsorize_iqr"))        # the statistical methods never need units
  quiet(sanitize_measurements(allnull, method = "z_score_cutoff"))
  # a concept without any limit row is no_limit, not unit_skipped
  quiet(sanitize_measurements(mem_measurements(
    data.frame(id = 1:2, person = 1L, concept = X_CONCEPT, unit = 0L, value = c(1, 2)))))
})

for (big in c(1e160, 1e300, 1.7e308, -1.7e308)) for (method in c("winsorize_iqr", "z_score_cutoff")) local({
  big <- big; method <- method
  test_that(sprintf("an extreme magnitude (%g) neither overflows nor aborts: %s", big, method), {
    # One absurd artifact must not make DuckDB raise (STDDEV_SAMP overflows on squares above ~1e154).
    rows <- data.frame(id = 1:11, person = 1L, concept = 7L, unit = 8840L, value = c(1:10, big))
    con <- mem_measurements(rows)
    run <- function(action, args = PARAMS[[method]]) {
      by_id(do.call(sanitize_measurements, c(list(con, method = method, action = action), args)))
    }
    nullified <- run("nullify")
    expect_identical(nullified["11", "sanitize_status"], if (big > 0) "above_max" else "below_min")
    expect_true(is.na(nullified["11", "value_as_number"]))
    expect_identical(nullified["11", "value_as_number_raw"], big)
    expect_true(all(nullified[as.character(1:10), "sanitize_status"] == "ok"))
    clamped <- run("clamp")
    expect_true(is.finite(clamped["11", "value_as_number"]) && abs(clamped["11", "value_as_number"]) < abs(big))
    # a threshold so wide that the fences overflow flags nothing instead of failing
    wide <- run("clamp", if (method == "winsorize_iqr") list(iqr_multiplier = 1e308) else list(z_threshold = 1e308))
    expect_true(all(wide$sanitize_status == "ok"))
  })
})

# ---------------------------------------------------------------------------------- bundled limits table
test_that("the bundled limits table is well formed and found through system.file", {
  path <- system.file("extdata", "physiologic_limits.csv", package = "omopduckdb")
  expect_true(nzchar(path) && file.exists(path))
  raw <- utils::read.csv(path, stringsAsFactors = FALSE, comment.char = "", colClasses = "character")
  expect_identical(names(raw), c("concept_id", "name", "category", "loinc_code", "unit", "unit_concept_id",
                                 "min_value", "max_value", "source", "note"))
  expect_false(any(duplicated(raw[c("concept_id", "unit_concept_id")])))
  expect_true(all(as.numeric(raw$min_value) < as.numeric(raw$max_value)))
  expect_true(all(raw$category %in% c("vital", "lab")))
  loaded <- omopduckdb:::.sanitize_bundled_limits()
  expect_equal(nrow(loaded), nrow(raw))
  expect_equal(nrow(loaded), 136L)
  expect_true(all(c(SBP, CREAT, POTASSIUM, 3012888, 3027018) %in% loaded$concept_id))
  expect_named(loaded, c("concept_id", "unit_concept_id", "min_value", "max_value"))
})

test_that("every concept id in the limits table exists in the vocabulary (needs OMOP_VOCAB_DB)", {
  vocab <- Sys.getenv("OMOP_VOCAB_DB", unset = "")
  skip_if(!nzchar(vocab) || !file.exists(vocab), "set OMOP_VOCAB_DB to an Athena-loaded DuckDB to run this check")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = vocab, read_only = TRUE)
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE))
  lim <- omopduckdb:::.sanitize_bundled_limits()
  ids <- unique(lim$concept_id)
  found <- DBI::dbGetQuery(con, sprintf(
    "SELECT concept_id, domain_id, standard_concept, invalid_reason FROM concept WHERE concept_id IN (%s)",
    paste(sprintf("%.0f", ids), collapse = ", ")))
  expect_setequal(found$concept_id, ids)
  expect_true(all(found$domain_id == "Measurement") && all(found$standard_concept == "S"))
  expect_true(all(is.na(found$invalid_reason)))
  units <- unique(lim$unit_concept_id)
  found <- DBI::dbGetQuery(con, sprintf("SELECT concept_id, domain_id FROM concept WHERE concept_id IN (%s)",
                                        paste(sprintf("%.0f", units), collapse = ", ")))
  expect_setequal(found$concept_id, units)
  expect_true(all(found$domain_id == "Unit"))
})
