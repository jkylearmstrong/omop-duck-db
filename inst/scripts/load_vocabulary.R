#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/load_vocabulary.R --vocab-dir "path/to/athena_download" [--db-path omop_cdm.duckdb]
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}
vocab_dir <- get_arg("--vocab-dir")
if (is.null(vocab_dir)) {
  stop("Usage: Rscript inst/scripts/load_vocabulary.R --vocab-dir <path-to-athena-download> [--db-path omop_cdm.duckdb]")
}

ok <- load_vocabulary(
  vocab_dir = vocab_dir,
  db_path = get_arg("--db-path", "omop_cdm.duckdb")
)
if (!ok) quit(status = 1)
