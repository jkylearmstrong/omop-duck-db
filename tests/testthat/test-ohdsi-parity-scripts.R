# Runs the OHDSI parity scripts under scripts/ as subprocesses. Each script exits non-zero on any mismatch with the official tool.
# Optional: skipped unless OMOP_RUN_HADES_TESTS=true and the official packages (and, for CohortConstructor, Java) are installed.

run_parity_script <- function(script) {
  root <- normalizePath(file.path(testthat::test_path(), "..", ".."), winslash = "/")
  path <- file.path(root, "scripts", script)
  skip_if_not(file.exists(path), "script not available in this build")
  rscript <- file.path(R.home("bin"), if (.Platform$OS.type == "windows") "Rscript.exe" else "Rscript")
  out <- suppressWarnings(system2(rscript, c("--no-init-file", "-e",
    shQuote(sprintf("if (nzchar(Sys.getenv('RETICULATE_PYTHON'))) invisible(reticulate::import('torch')); suppressMessages(pkgload::load_all('%s', quiet = TRUE)); source('%s')", root, path))),
    stdout = TRUE, stderr = TRUE))
  list(status = attr(out, "status") %||% 0L, output = paste(out, collapse = "\n"))
}

skip_unless_hades <- function(...) {
  skip_on_cran()
  if (!identical(Sys.getenv("OMOP_RUN_HADES_TESTS"), "true")) skip("set OMOP_RUN_HADES_TESTS=true")
  # check installation without loading: loading DeepPatientLevelPrediction here would import Python torch after duckdb (DLL conflict)
  for (p in c("pkgload", "DatabaseConnector", ...)) skip_if(!nzchar(system.file(package = p)), paste("package", p, "is not installed"))
}

test_that("CohortMethod agrees with direct counts and the 2x2 odds ratio on a DuckDB CDM", {
  skip_unless_hades("CohortMethod", "FeatureExtraction")
  res <- run_parity_script("ohdsi_parity_cohortmethod.R")
  expect_equal(res$status, 0L, info = res$output)
  expect_match(res$output, "All CohortMethod parity checks passed")
})

test_that("CohortConstructor agrees with Circe and direct SQL on a DuckDB CDM", {
  skip_unless_hades("CohortConstructor", "CDMConnector", "CirceR")
  res <- run_parity_script("ohdsi_parity_cohortconstructor.R")
  expect_equal(res$status, 0L, info = res$output)
  expect_match(res$output, "All CohortConstructor parity checks passed")
})

test_that("DeepPatientLevelPrediction trains on as_plp_data() output", {
  skip_unless_hades("DeepPatientLevelPrediction", "PatientLevelPrediction", "reticulate", "Andromeda")
  if (!nzchar(Sys.getenv("RETICULATE_PYTHON"))) skip("set RETICULATE_PYTHON to a Python with torch and polars")
  res <- run_parity_script("ohdsi_parity_deepplp.R")
  expect_equal(res$status, 0L, info = res$output)
  expect_match(res$output, "All DeepPatientLevelPrediction parity checks passed")
})
