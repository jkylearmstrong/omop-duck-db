#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/build_schema.R [--db-path omop_cdm.duckdb] [--regenerate-ddl]
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}

build_schema(
  db_path = get_arg("--db-path", "omop_cdm.duckdb"),
  regenerate_ddl = "--regenerate-ddl" %in% args
)
