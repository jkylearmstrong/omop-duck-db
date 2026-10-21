test_that("velocity features: values, sentinel and zero-division guards", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id INT, person_id INT, visit_concept_id INT, visit_start_date DATE, visit_end_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id INT, person_id INT, measurement_concept_id INT, measurement_date DATE, value_as_number DOUBLE)")
  v <- function(id, p, c, s, e) DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence VALUES (%d,%d,%d,DATE '%s',DATE '%s')", id, p, c, s, e))
  v(1, 1, 9201, "2019-09-01", "2019-09-05"); v(2, 1, 9201, "2019-09-20", "2019-09-22")
  v(3, 1, 9201, "2020-02-01", "2020-02-03"); v(4, 1, 9201, "2020-03-20", "2020-03-25")
  v(5, 1, 9203, "2020-02-10", "2020-02-10"); v(6, 1, 9203, "2020-04-10", "2020-04-12")
  v(7, 2, 9201, "2020-04-10", "2020-04-12")
  v(8, 3, 9201, "2020-04-10", "2020-04-20"); v(9, 3, 9201, "2020-05-01", "2020-05-02")
  DBI::dbExecute(con, "CREATE TABLE coh AS SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date FROM visit_occurrence WHERE visit_occurrence_id IN (6,7,8)")
  m <- function(id, p, c, val) DBI::dbExecute(con, sprintf("INSERT INTO measurement VALUES (%d,%d,%d,DATE '2020-04-10',%s)", id, p, c, val))
  m(1, 1, 3027018, 100); m(2, 1, 3004249, 80); m(3, 1, 3013682, 40); m(4, 1, 3016723, 2)
  m(5, 2, 3027018, 90); m(6, 2, 3004249, 0); m(7, 2, 3013682, 10); m(8, 2, 3016723, 0)
  f <- extract_velocity_features(con, "coh")
  p1 <- f[f$subject_id == 1, ]; p2 <- f[f$subject_id == 2, ]; p3 <- f[f$subject_id == 3, ]
  expect_equal(p1$ip_velocity, 4 * 30 / 365)
  expect_equal(p1$ed_velocity, 30 / 365)
  expect_equal(p1$recent_ip_ratio, 2 / 4)
  expect_equal(p1$readmission_intensity, 1 / 4)
  expect_equal(p1$days_since_last_ip, as.numeric(as.Date("2020-04-10") - as.Date("2020-03-25")))
  expect_equal(p1$shock_index, 1.25); expect_equal(p1$bun_to_creatinine, 20)
  expect_equal(p1$lace_proxy, 6)
  expect_equal(p2$days_since_last_ip, NO_PRIOR_VISIT_DAYS)
  expect_equal(c(p2$ip_velocity, p2$recent_ip_ratio, p2$readmission_intensity), c(0, 0, 0))
  expect_true(is.na(p2$shock_index) && is.na(p2$bun_to_creatinine))
  expect_equal(p3$days_since_last_ip, 365); expect_equal(p3$lace_proxy, 5)
})

spec <- list(features = list(
  list(name = "creatinine", dtype = "float", unit = "mg/dL", bounds = list(0.1, 30), impute = 1),
  list(name = "heart_rate", dtype = "float", unit = "bpm", bounds = list(20, 250), impute = 80),
  list(name = "ip_velocity", dtype = "int", impute = 0)))
mk_frame <- function(n = 200, seed = 0) {
  set.seed(seed)
  data.frame(person_id = seq_len(n), creatinine = rlnorm(n, 0.1, 0.4), heart_rate = pmin(pmax(rnorm(n, 85, 15), 30), 200),
             ip_velocity = sample(0:3, n, TRUE))
}

test_that("feature contract: report, units, clip/nullify", {
  df <- mk_frame(100); df$creatinine <- df$creatinine * 88.4; df$ip_velocity <- NULL
  err <- tryCatch(validate_feature_frame(df, spec), error = function(e) e)
  expect_s3_class(err, "feature_contract_error")
  expect_equal(err$report$missing, "ip_velocity")
  expect_match(err$report$unit_suspicion$creatinine, "umol/L")
  df <- mk_frame(100); df$heart_rate[1:5] <- 999
  expect_equal(validate_feature_frame(df, spec, "clip")$n_clipped, 5L)
  nul <- validate_feature_frame(df, spec, "nullify")
  expect_equal(nul$n_nullified, 5L); expect_equal(sum(is.na(nul$frame$heart_rate)), 5L)
  expect_equal(feature_validation_telemetry(nul)$n_values_nullified, 5L)
  expect_true(validate_feature_frame(mk_frame(10), spec)$ok)
  bad <- mk_frame(10); bad$creatinine <- "x"
  expect_error(validate_feature_frame(bad, spec, "clip"), "dtype")
})

test_that("model bundle round-trip, tamper detection, pickle refusal", {
  coef <- c(0.9, 0.02, 0.4)
  dir <- file.path(tempfile("bundle"))
  write_model_bundle(dir, "demo", "1.0.0", spec, list(list(kind = "logistic", filename = "glm.json",
                                                      content = list(intercept = -3, coef = as.list(coef)))))
  df <- mk_frame()
  out <- score_cohort(load_model_bundle(dir), df)
  expect_equal(names(out), c("person_id", "prob", "risk_tier"))
  expect_equal(out$prob, as.numeric(1 / (1 + exp(-(-3 + as.matrix(df[, 2:4]) %*% coef)))), tolerance = 1e-5)
  dir2 <- tempfile("b2")
  write_model_bundle(dir2, "m", "2", spec, list(
    list(kind = "logistic", filename = "a.json", weight = 3, content = list(intercept = -3, coef = as.list(coef))),
    list(kind = "logistic", filename = "b.json", weight = 1, content = list(intercept = 0, coef = list(0, 0, 0)))),
    calibration = list(method = "platt", slope = 1, intercept = 0))
  p <- score_cohort(dir2, df)$prob
  expect_equal(p, (3 * out$prob + 0.5) / 4, tolerance = 1e-5)
  writeLines('{"intercept": 0, "coef": [1,1,1]}', file.path(dir, "glm.json"))
  expect_error(load_model_bundle(dir), "SHA-256", class = "bundle_integrity_error")
  writeBin(as.raw(1:3), file.path(dir2, "model.pkl"))
  expect_error(load_model_bundle(dir2), "pickle", class = "bundle_integrity_error")
})

test_that("model bundle rejects Python-only components and imputes NA", {
  dir <- tempfile("b3")
  write_model_bundle(dir, "m", "1", spec, list(list(kind = "logistic", filename = "g.json",
                                                    content = list(intercept = 0, coef = list(0.1, 0.01, 0.1)))))
  df <- mk_frame(20); df$creatinine[1] <- NA
  expect_false(anyNA(score_cohort(dir, df)$prob))
  bl <- jsonlite::fromJSON(file.path(dir, "blend.json"), simplifyVector = FALSE)
  bl$components[[1]]$kind <- "onnx"
  jsonlite::write_json(bl, file.path(dir, "blend.json"), auto_unbox = TRUE, null = "null")
  man <- jsonlite::fromJSON(file.path(dir, "manifest.json"), simplifyVector = FALSE)
  man$model_sha256[["blend.json"]] <- sha256_file(file.path(dir, "blend.json"))
  jsonlite::write_json(man, file.path(dir, "manifest.json"), auto_unbox = TRUE)
  expect_error(load_model_bundle(dir), "Python-only", class = "bundle_integrity_error")
})

site <- function(n, seed, prev = 0.15, signal = 1.5) {
  set.seed(seed); y <- as.integer(runif(n) < prev)
  list(p = 1 / (1 + exp(-rnorm(n, -2 + signal * y, 1))), y = y)
}

test_that("telemetry metrics match brute force and packets are aggregate-only", {
  s <- site(2000, 1)
  d <- auroc_delong(s$y, s$p)
  pos <- s$p[s$y == 1]; neg <- s$p[s$y == 0]
  brute <- mean(outer(pos, neg, ">")) + 0.5 * mean(outer(pos, neg, "=="))
  expect_equal(unname(d["auc"]), brute, tolerance = 1e-12)
  dir <- tempfile("tel"); dir.create(dir)
  pk <- site_telemetry(s$p, s$y, "S01", output = file.path(dir, "S01.json"))
  expect_length(pk$deciles, 10)
  expect_gt(pk$metrics$auroc, 0.7)
  expect_error(site_telemetry(s$p, s$y, "Temple University Hospital"), "site_id")
  bad <- c(pk, list(patient_ids = list(1, 2)))
  expect_error(validate_packet(bad), "patient_ids")
  t60 <- site(60, 4, 0.05)
  tiny <- site_telemetry(t60$p, t60$y, "S03")
  expect_true(tiny$suppressed); expect_null(tiny$metrics)
})

test_that("small deciles are suppressed with secondary suppression", {
  s <- site(400, 3, prev = 0.03)
  pk <- site_telemetry(s$p, s$y, "S02", min_cell = 5)
  hidden <- sum(vapply(pk$deciles, function(d) is.null(d$events), logical(1)))
  expect_false(hidden == 1)
  shown <- Filter(function(d) !is.null(d$events), pk$deciles)
  expect_true(all(vapply(shown, function(d) d$events == 0 || d$events >= 5, logical(1))))
})

test_that("aggregator pools sites and flags heterogeneity", {
  dir <- tempfile("agg"); dir.create(dir)
  ps <- list(); ys <- list()
  for (i in 1:4) {
    s <- site(c(3000, 4000, 2500, 5000)[i], 10 + i); ps[[i]] <- s$p; ys[[i]] <- s$y
    site_telemetry(s$p, s$y, sprintf("S%02d", i), output = file.path(dir, sprintf("S%02d.json", i)))
  }
  agg <- aggregate_telemetry(dir)
  pooled <- auroc_delong(unlist(ys), unlist(ps))["auc"]
  expect_lt(abs(agg$auroc$pooled - pooled), 0.01)
  expect_equal(agg$n, 14500)
  e <- site(4000, 99, signal = 0.3)
  site_telemetry(e$p, e$y, "S09", output = file.path(dir, "S09.json"))
  het <- aggregate_telemetry(dir)
  expect_gt(het$auroc$tau2, 0); expect_lt(het$auroc$q_p, 0.05)
  d <- site(3000, 5)
  site_telemetry(d$p, d$y, "S01", output = file.path(dir, "dup.json"))
  expect_error(aggregate_telemetry(dir), "Duplicate")
})

test_that("from_sql / import_omop stream a DBI source to Parquet and DuckDB", {
  skip_if_not_installed("RSQLite")
  src <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
  on.exit(DBI::dbDisconnect(src), add = TRUE)
  DBI::dbWriteTable(src, "ENCOUNTER", data.frame(ENCOUNTERID = 1:10, PATID = paste0("P", 1:10), COST = (1:10) * 1.5))
  out <- tempfile("pq")
  rep <- from_sql(src, "encounter", out, chunksize = 3)
  expect_equal(rep$rows, 10); expect_equal(rep$source_table, "ENCOUNTER")
  ddb <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(ddb, shutdown = TRUE), add = TRUE)
  r <- DBI::dbGetQuery(ddb, sprintf("SELECT count(*) n, sum(cost) s FROM read_parquet('%s')", gsub("\\\\", "/", file.path(out, "encounter.parquet"))))
  expect_equal(c(r$n, r$s), c(10, 82.5))
  expect_equal(from_sql(src, "enc", out, table_map = list(enc = "encounter"))$table, "enc")
  expect_error(from_sql(src, "nope", out), "not found")
  DBI::dbWriteTable(src, "PERSON", data.frame(PERSON_ID = 1:2, GENDER_CONCEPT_ID = c(8507L, 8532L), YEAR_OF_BIRTH = c(1970L, 1980L),
                                              RACE_CONCEPT_ID = 8527L, ETHNICITY_CONCEPT_ID = 38003564L))
  db <- tempfile(fileext = ".duckdb")
  res <- import_omop(src, db, tables = c("person", "measurement"))
  expect_equal(res$rows[res$table == "person"], 2)
  expect_true(is.na(res$rows[res$table == "measurement"]))
})
