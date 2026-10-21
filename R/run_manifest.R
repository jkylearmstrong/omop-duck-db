#' SHA-256 of a file (alias used by run manifests)
#' @param path File path.
#' @return Lower-case hex digest.
#' @keywords internal
#' @noRd
.file_sha256 <- function(path) digest::digest(file = path, algo = "sha256", serialize = FALSE)

#' Start a reproducibility manifest for a pipeline run
#'
#' Records versions, seeds, the SHA-256 of input files and of the feature spec, the git commit, wall time and the
#' cohort size at each attrition step. Mirror of `omop_etl.run_manifest.RunManifest`; the JSON files are interchangeable.
#' Add steps with [run_manifest_add_step()] and finish with [write_run_manifest()].
#'
#' @param entry_point Name of the pipeline step (e.g. `"cohort define"`).
#' @param seeds Named list of random seeds.
#' @param inputs Character vector of input files to hash.
#' @param feature_spec Optional path to the feature spec.
#' @param extra Named list of extra provenance.
#' @param git_dir Directory in which to run `git rev-parse HEAD`.
#' @return An environment of class `run_manifest` (mutable, so steps can be added in place).
#' @export
run_manifest <- function(entry_point, seeds = list(), inputs = character(), feature_spec = NULL, extra = list(),
                         git_dir = NULL) {
  m <- new.env(parent = emptyenv())
  m$entry_point <- entry_point; m$seeds <- as.list(seeds); m$inputs <- as.character(inputs)
  m$feature_spec <- feature_spec; m$extra <- as.list(extra); m$attrition <- list(); m$git_dir <- git_dir
  m$t0 <- Sys.time(); m$started <- format(Sys.time(), "%Y-%m-%dT%H:%M:%S+00:00", tz = "UTC")
  class(m) <- "run_manifest"
  m
}

#' Add an input file or an attrition step to a run manifest
#' @param m A `run_manifest`.
#' @param step Step name.
#' @param n Cohort size (rows) after the step.
#' @param ... Extra named values stored with the step.
#' @return `m`, invisibly.
#' @export
run_manifest_add_step <- function(m, step, n, ...) {
  m$attrition[[length(m$attrition) + 1L]] <- c(list(step = as.character(step), n = as.integer(n)), list(...))
  invisible(m)
}

#' @rdname run_manifest_add_step
#' @param path Input file path to record (hashed when the manifest is written).
#' @export
run_manifest_add_input <- function(m, path) {
  m$inputs <- c(m$inputs, as.character(path))
  invisible(m)
}

.git_commit <- function(dir) {
  git <- Sys.which("git")
  if (!nzchar(git)) return(NULL)
  out <- tryCatch(suppressWarnings(system2(git, c(if (!is.null(dir)) c("-C", shQuote(dir)), "rev-parse", "HEAD"),
                                           stdout = TRUE, stderr = FALSE)), error = function(e) character())
  if (length(out) == 1L && grepl("^[0-9a-f]{40}$", out)) out else NULL
}

#' Write `run_manifest.json`
#' @param m A `run_manifest`.
#' @param path Output path.
#' @return The path, invisibly.
#' @export
write_run_manifest <- function(m, path = "run_manifest.json") {
  pk <- vapply(c("omopduckdb", "duckdb", "DBI", "jsonlite"), function(p) {
    v <- tryCatch(as.character(utils::packageVersion(p)), error = function(e) NA_character_); v
  }, character(1))
  d <- list(manifest_version = 1L, entry_point = m$entry_point, started = m$started,
            wall_time_seconds = round(as.numeric(difftime(Sys.time(), m$t0, units = "secs")), 3),
            r_version = R.version.string, platform = R.version$platform,
            packages = as.list(pk), seeds = m$seeds, git_commit = .git_commit(m$git_dir),
            inputs = stats::setNames(lapply(m$inputs, .file_sha256), m$inputs),
            feature_spec = if (is.null(m$feature_spec)) NULL else list(path = m$feature_spec, sha256 = .file_sha256(m$feature_spec)),
            attrition = m$attrition, extra = m$extra)
  dir.create(dirname(path), showWarnings = FALSE, recursive = TRUE)
  jsonlite::write_json(d, path, auto_unbox = TRUE, null = "null", na = "null", pretty = TRUE, digits = NA)
  invisible(path)
}

#' Verify the inputs recorded in a run manifest
#'
#' Re-hashes every recorded input (and the feature spec) and optionally compares attrition counts, so a changed input
#' file fails before scoring. Mirror of `omop_etl.run_manifest.verify_manifest`.
#'
#' @param path Path to `run_manifest.json`.
#' @param base_dir Directory against which relative input paths resolve (default: working directory).
#' @param attrition Optional list of `list(step =, n =)` from a re-run, compared with the manifest.
#' @return The manifest (a list), invisibly, when everything matches; otherwise a `manifest_verification_error`.
#' @export
verify_manifest <- function(path, base_dir = getwd(), attrition = NULL) {
  m <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  problems <- character()
  entries <- m$inputs
  if (!is.null(m$feature_spec)) entries[[m$feature_spec$path]] <- m$feature_spec$sha256
  for (nm in names(entries)) {
    f <- if (grepl("^([A-Za-z]:|/|\\\\)", nm)) nm else file.path(base_dir, nm)
    if (!file.exists(f)) problems <- c(problems, sprintf("%s: file not found", nm))
    else if (!identical(.file_sha256(f), entries[[nm]])) problems <- c(problems, sprintf("%s: SHA-256 changed since the manifest was written", nm))
  }
  if (!is.null(attrition)) {
    old <- vapply(m$attrition, function(a) paste0(a$step, "=", a$n), character(1))
    new <- vapply(attrition, function(a) paste0(a$step, "=", as.integer(a$n)), character(1))
    if (!identical(old, new)) problems <- c(problems, sprintf("attrition counts differ: manifest [%s] vs rerun [%s]",
                                                              paste(old, collapse = ", "), paste(new, collapse = ", ")))
  }
  if (length(problems)) {
    stop(structure(class = c("manifest_verification_error", "error", "condition"),
                   list(message = paste0("Manifest verification failed:\n  ", paste(problems, collapse = "\n  ")), call = NULL)))
  }
  invisible(m)
}
