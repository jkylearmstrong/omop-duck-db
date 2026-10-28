# Windows CI (2 vCPU / 7 GB) crashed silently when GC finalised unclosed Andromeda (DuckDB-backed) objects.
# Keep each Andromeda small and single-threaded, and close them deterministically in tests.
options(andromedaThreads = 1, andromedaMemoryLimit = "512Mb")

# Close the Andromeda covariateData inside a plpData (or a bare Andromeda) and collect garbage.
close_plp <- function(x) {
  andr <- if (is.list(x) && !is.null(x$covariateData)) x$covariateData else x
  if (requireNamespace("Andromeda", quietly = TRUE) && !is.null(andr)) {
    try(Andromeda::close(andr), silent = TRUE)
  }
  invisible(gc())
}
