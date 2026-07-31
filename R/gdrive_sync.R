#' Upload the large generated artifacts to Google Drive
#'
#' Syncs `omop_cdm.duckdb` and the Athena vocabulary archive to/from Google
#' Drive instead of committing them to git. Configure the target Drive folder
#' in `config/gdrive.yml` (copy from `config/gdrive.yml.example`). First run
#' opens a browser OAuth prompt via the `googledrive` package.
#'
#' @return Invisibly, `NULL`.
#' @export
gdrive_upload_snapshot <- function() {
  drive_folder <- .gdrive_folder()
  for (f in .synced_files()) {
    if (!file.exists(f)) {
      cat("Skipping", f, "- not found locally\n")
      next
    }
    cat("Uploading", f, "...\n")
    existing <- googledrive::drive_ls(path = drive_folder, pattern = paste0("^", basename(f), "$"))
    if (nrow(existing) > 0) {
      googledrive::drive_update(existing$id[[1]], media = f)
    } else {
      googledrive::drive_upload(f, path = drive_folder, name = basename(f))
    }
  }
  cat("Upload complete.\n")
  invisible(NULL)
}

#' Download the latest snapshot from Google Drive
#'
#' @inherit gdrive_upload_snapshot description
#' @return Invisibly, `NULL`.
#' @export
gdrive_download_latest <- function() {
  drive_folder <- .gdrive_folder()
  remote_files <- googledrive::drive_ls(path = drive_folder)
  if (nrow(remote_files) == 0) {
    stop("No files found in the configured Google Drive folder.")
  }
  for (i in seq_len(nrow(remote_files))) {
    name <- remote_files$name[[i]]
    cat("Downloading", name, "...\n")
    googledrive::drive_download(remote_files[i, ], path = name, overwrite = TRUE)
  }
  cat("Download complete.\n")
  invisible(NULL)
}

.gdrive_folder <- function() {
  cfg <- read_local_config("gdrive.yml")
  if (is.null(cfg$folder_id) || cfg$folder_id == "REPLACE_WITH_YOUR_DRIVE_FOLDER_ID") {
    stop(
      "Missing/unset config/gdrive.yml. Copy config/gdrive.yml.example to config/gdrive.yml ",
      "and set folder_id to your Google Drive folder's ID."
    )
  }
  googledrive::as_id(cfg$folder_id)
}

.synced_files <- function() {
  candidates <- c("omop_cdm.duckdb", Sys.glob("vocabulary_download_v5_*.zip"))
  candidates[file.exists(candidates)]
}
