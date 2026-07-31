#!/usr/bin/env Rscript
# Usage: Rscript inst/scripts/gdrive_sync.R <upload|download>
library(omopduckdb)

args <- commandArgs(trailingOnly = TRUE)
action <- if (length(args)) args[[1]] else NULL
if (is.null(action) || !(action %in% c("upload", "download"))) {
  stop("Usage: Rscript inst/scripts/gdrive_sync.R <upload|download>")
}

if (action == "upload") {
  gdrive_upload_snapshot()
} else {
  gdrive_download_latest()
}
