#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/load_from_bigquery.R --billing-project <gcp-project-id> \
#   --dataset bigquery-public-data.cms_synthetic_patient_data_omop \
#   --tables person,visit_occurrence,condition_occurrence \
#   [--db-path omop_cdm.duckdb] [--mode overwrite|append]
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}
tables_arg <- get_arg("--tables")
if (is.null(tables_arg)) {
  stop("Provide --tables as a comma-separated list, e.g. --tables concept,concept_ancestor,concept_relationship")
}

load_from_bigquery(
  billing_project = get_arg("--billing-project"),
  dataset = get_arg("--dataset"),
  tables = trimws(strsplit(tables_arg, ",")[[1]]),
  db_path = get_arg("--db-path", "omop_cdm.duckdb"),
  mode = get_arg("--mode", "overwrite")
)
