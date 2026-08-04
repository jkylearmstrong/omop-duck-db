#' Run Full Package Checks, Coverage, Documentation & Build Pipeline
#'
#' Usage from R:
#'   source("scripts/release.R")
#'   release(bump_version = "0.2.0")

release <- function(bump_version = NULL, check_only = TRUE) {
  message("=== Starting omopduckdb Dual-Language Check & Release ===")

  if (!is.null(bump_version)) {
    message("Updating DESCRIPTION and pyproject.toml version to: ", bump_version)
    desc <- readLines("DESCRIPTION")
    desc <- gsub("^Version: .+$", paste0("Version: ", bump_version), desc)
    writeLines(desc, "DESCRIPTION")

    if (file.exists("pyproject.toml")) {
      py <- readLines("pyproject.toml")
      py <- gsub('^version = ".+?"', paste0('version = "', bump_version, '"'), py)
      writeLines(py, "pyproject.toml")
    }
  }

  message("\n--- Step 1: Render README.Rmd ---")
  if (file.exists("README.Rmd") && system("quarto --version", ignore.stdout = TRUE, ignore.stderr = TRUE) == 0) {
    system("quarto render README.Rmd --to gfm")
  }

  message("\n--- Step 2: R Document & Test ---")
  if (requireNamespace("devtools", quietly = TRUE)) {
    devtools::document()
    devtools::test()
  }

  message("\n--- Step 3: R Coverage ---")
  if (requireNamespace("covr", quietly = TRUE)) {
    cov <- covr::package_coverage()
    print(cov)
  }

  message("\n--- Step 4: Python Pytest & Coverage ---")
  system("python -m pytest tests/")

  if (!check_only) {
    message("\n--- Step 5: Build Python Dist & Check ---")
    system("python -m build")
    system("python -m twine check dist/*")

    if (requireNamespace("pkgdown", quietly = TRUE)) {
      options(pkgdown.internet = FALSE)
      pkgdown::build_site(new_process = FALSE, install = FALSE, preview = FALSE)
    }
  }

  message("\n=== All checks & builds completed! ===")
}

if (sys.nframe() == 0) {
  release()
}
