#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/etl_pcornet.R --source-dir "path/to/pcornet_extract" [--db-path omop_cdm.duckdb] [--central-vocab vocab.duckdb] [--site-id 1] [--site-anon "Site A"] [--site-name "Hospital System A"] [--disambiguate-patids] [--auto-resources] [--memory-limit 16GB] [--threads N] [--temp-dir DIR] [--key-check warn|raise|ignore] [--use-enrollment auto|true|false] [--enrollment-type-concept-id N]
if (requireNamespace("pkgload", quietly = TRUE) && file.exists("DESCRIPTION")) {
  pkgload::load_all(".", quiet = TRUE)
} else {
  library(omopduckdb)
}

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- which(args == flag)
  if (length(i) && i < length(args)) args[i + 1] else default
}
has_flag <- function(flag) {
  any(args == flag)
}

source_dir <- get_arg("--source-dir")
if (is.null(source_dir)) {
  stop("Usage: Rscript inst/scripts/etl_pcornet.R --source-dir <path-to-pcornet-extract> [--db-path omop_cdm.duckdb] [--central-vocab vocab.duckdb] [--site-id <int>] [--site-anon <str>] [--site-name <str>] [--disambiguate-patids]")
}

site_id_str <- get_arg("--site-id")
site_id <- if (!is.null(site_id_str)) as.integer(site_id_str) else NULL

use_enrollment <- switch(tolower(get_arg("--use-enrollment", "auto")),
  auto = "auto", `true` = TRUE, `false` = FALSE,
  stop("--use-enrollment must be auto, true or false.")
)

etl_pcornet(
  source_dir = source_dir,
  db_path = get_arg("--db-path", "omop_cdm.duckdb"),
  central_vocab = get_arg("--central-vocab"),
  site_id = site_id,
  site_anon = get_arg("--site-anon"),
  site_name = get_arg("--site-name"),
  disambiguate_patids = has_flag("--disambiguate-patids"),
  auto_resources = has_flag("--auto-resources"),
  memory_limit = get_arg("--memory-limit"),
  threads = if (!is.null(get_arg("--threads"))) as.integer(get_arg("--threads")) else NULL,
  temp_dir = get_arg("--temp-dir"),
  key_check = get_arg("--key-check", "warn"),
  use_enrollment = use_enrollment,
  enrollment_type_concept_id = as.integer(get_arg("--enrollment-type-concept-id", "0"))
)

