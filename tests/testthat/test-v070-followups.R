test_that("R-written bundle, manifest and packet files use null, never NaN/NA tokens", {
  spec <- list(features = list(
    list(name = "creatinine", dtype = "float", bounds = list(0.1, NaN), impute = NaN),
    list(name = "heart_rate", dtype = "float", bounds = list(20, 250), impute = 80)))
  dir <- tempfile("b")
  write_model_bundle(dir, "m", "1", spec, list(list(kind = "logistic", filename = "g.json",
                                                    content = list(intercept = NA_real_, coef = list(0.1, 0.2)))))
  for (f in list.files(dir, pattern = "[.]json$", full.names = TRUE)) {
    txt <- paste(readLines(f, warn = FALSE), collapse = "\n")
    expect_false(grepl("NaN|\"NA\"|\bNA\b|Inf", txt), info = basename(f))
    expect_silent(jsonlite::fromJSON(txt))
  }
  m <- run_manifest("unit", extra = list(rate = NaN, x = NA_real_))
  p <- write_run_manifest(m, tempfile(fileext = ".json"))
  expect_false(grepl("NaN|\"NA\"", paste(readLines(p, warn = FALSE), collapse = "")))
})
