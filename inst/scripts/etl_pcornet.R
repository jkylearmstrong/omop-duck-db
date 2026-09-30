#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/etl_pcornet.R --source-dir "path/to/pcornet_extract" [--db-path omop_cdm.duckdb] [--central-vocab vocab.duckdb]
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}
source_dir <- get_arg("--source-dir")
if (is.null(source_dir)) {
  stop("Usage: Rscript inst/scripts/etl_pcornet.R --source-dir <path-to-pcornet-extract> [--db-path omop_cdm.duckdb] [--central-vocab vocab.duckdb]")
}

etl_pcornet(
  source_dir = source_dir,
  db_path = get_arg("--db-path", "omop_cdm.duckdb"),
  central_vocab = get_arg("--central-vocab")
)
