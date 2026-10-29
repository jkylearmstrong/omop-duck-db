#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/run_bigquery_demo.R --billing-project <gcp-project-id> [--output-dir bigquery_demo_out] \
#   [--scratch-dataset project.dataset] [--vocabulary-dataset project.dataset] [--dry-run]
# Run from the repository root. First run: authenticate once in an interactive R session with bigrquery::bq_auth().
# SYNTHETIC CLAIMS DATA: a software check, not a clinical result (see ?BIGQUERY_DEMO_DISCLAIMER).
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}

summary <- run_bigquery_demo(
  billing_project = get_arg("--billing-project"),
  output_dir = get_arg("--output-dir", "bigquery_demo_out"),
  dataset = get_arg("--dataset"),
  scratch_dataset = get_arg("--scratch-dataset"),
  vocabulary_dataset = get_arg("--vocabulary-dataset"),
  cohort_json = get_arg("--cohort-json"),
  dry_run = "--dry-run" %in% args
)
cat("Wrote", file.path(get_arg("--output-dir", "bigquery_demo_out"), "demo_report.md"), "\n")
cat(BIGQUERY_DEMO_DISCLAIMER, "\n")
