# Anonymous site labels for multi-site studies: assign, save, load, check, relabel, and scan outputs for real site names.
# The mapping file is the same JSON object the Python package reads and writes (omop_etl.save_site_labels / load_site_labels),
# so a consortium can assign letters once and hand the same file to sites working in R or Python.

.re_escape <- function(s) gsub("([.|()\\^{}+$*?]|\\[|\\]|\\\\)", "\\\\\\1", s)

.file_ext <- function(f) {
  b <- basename(f)
  ifelse(grepl("\\.[^.]+$", b), sub(".*\\.([^.]+)$", "\\1", b), "")
}

.letters_for <- function(i) {
  vapply(i, function(k) {
    out <- ""
    while (k > 0) {
      r <- (k - 1) %% 26
      out <- paste0(LETTERS[r + 1], out)
      k <- (k - 1) %/% 26
    }
    out
  }, character(1))
}

#' Assign anonymous site labels
#'
#' Gives each site a label such as `"Site A"`, so that tables and figures do not identify a health system. Works for any number of
#' sites (after `Site Z` the labels continue `Site AA`, `Site AB`, ...).
#'
#' Keep the mapping private and outside the repository and the shared outputs ([save_site_labels()]). Assign it once for the whole
#' consortium and give every site the same file, or each site's "Site A" would be a different health system. A seed makes the
#' assignment repeatable in R; R and Python draw different shuffles from the same seed, so share the mapping file, not the seed.
#'
#' @param sites Character vector of site keys (duplicates are dropped).
#' @param mode `"random"` (a random letter per site) or `"ordered"` (letters follow the order of `sites`).
#' @param seed Optional integer seed for a repeatable random assignment.
#' @param prefix Text before the letter.
#' @return A named character vector: names are the site keys, values the labels.
#' @examples
#' assign_site_labels(c("north", "south", "east"), mode = "ordered")
#' @export
assign_site_labels <- function(sites, mode = c("random", "ordered"), seed = NULL, prefix = "Site ") {
  mode <- match.arg(mode)
  sites <- unique(as.character(sites))
  lab <- .letters_for(seq_along(sites))
  if (mode == "random" && length(lab) > 1) {
    if (!is.null(seed)) set.seed(seed)
    lab <- lab[sample.int(length(lab))]
  }
  stats::setNames(paste0(prefix, lab), sites)
}

#' Save or load a site-to-label mapping
#'
#' Writes / reads the mapping as a JSON object (`{"site": "Site A", ...}`), the same file format as the Python package.
#' Store it somewhere that is not committed or shared with the results.
#'
#' @param labels Named character vector or named list (site key -> label).
#' @param path File path.
#' @return `save_site_labels()` returns `path` invisibly; `load_site_labels()` a named character vector.
#' @export
save_site_labels <- function(labels, path) {
  labels <- unlist(labels)
  if (is.null(names(labels)) || any(names(labels) == "")) stop("`labels` must be named by site.", call. = FALSE)
  jsonlite::write_json(as.list(labels), path, auto_unbox = TRUE, pretty = TRUE)
  invisible(path)
}

#' @rdname save_site_labels
#' @export
load_site_labels <- function(path) {
  x <- jsonlite::read_json(path, simplifyVector = FALSE)
  stats::setNames(vapply(x, function(v) as.character(v), character(1)), names(x))
}

#' Check a site-to-label mapping before a run
#'
#' Stops when a site has no label (it would appear in the tables under its own name), when two sites share a label, when a label is
#' empty, or when a label equals a site key. Extra entries are allowed, so one consortium-wide file can be given to every site.
#'
#' @param sites Site keys used in this run.
#' @param labels Named character vector or list (site key -> label).
#' @return The labels of `sites`, as a named character vector in the order of `sites`.
#' @export
validate_site_labels <- function(sites, labels) {
  sites <- unique(as.character(sites))
  labels <- unlist(labels)
  missing <- setdiff(sites, names(labels))
  if (length(missing)) {
    stop(sprintf("%d of %d site(s) have no label (site number(s) %s in the order given); every site needs one, or it would appear under its own name.",
                 length(missing), length(sites), paste(match(missing, sites), collapse = ", ")), call. = FALSE)
  }
  used <- trimws(as.character(labels[sites]))
  names(used) <- sites
  if (any(is.na(used) | used == "")) stop("A site label is empty.", call. = FALSE)
  if (anyDuplicated(tolower(used))) stop("Two sites share a label; each site needs its own.", call. = FALSE)
  if (any(tolower(used) %in% tolower(sites))) stop("A label is the same as a site name; labels must be anonymous.", call. = FALSE)
  used
}

.label_translation <- function(old_labels, new_labels, sites = NULL) {
  old_labels <- unlist(old_labels)
  new_labels <- unlist(new_labels)
  if (is.null(sites)) sites <- names(old_labels)
  o <- validate_site_labels(sites, old_labels)
  n <- validate_site_labels(sites, new_labels)
  if (any(grepl("^[A-Za-z0-9]{1,2}$", o))) {
    stop("Old labels as short as a bare letter cannot be swapped safely in text; use labels like 'Site A'.", call. = FALSE)
  }
  stats::setNames(unname(n), unname(o))
}

.relabel_chr <- function(x, map) {
  if (!length(map) || !length(x)) return(x)
  alts <- names(map)[order(-nchar(names(map)))]
  pat <- paste0("(?<![A-Za-z0-9])(", paste(.re_escape(alts), collapse = "|"), ")(?![A-Za-z0-9])")
  ok <- !is.na(x)
  y <- x[ok]
  m <- gregexpr(pat, y, perl = TRUE)
  regmatches(y, m) <- lapply(regmatches(y, m), function(v) unname(map[v]))
  x[ok] <- y
  x
}

#' Swap site labels in data or text
#'
#' Replaces every whole-word old label with the new label of the same site, all at once (so `Site A -> Site B` together with
#' `Site B -> Site A` is fine). For a data frame, column names and every character or factor column are relabelled; numbers are not
#' touched and rows keep their order.
#'
#' @param x A character vector or a data frame.
#' @param old_labels,new_labels Site key -> label mappings (named vectors or lists, or paths to mapping files) covering the same sites.
#' @return `x` with the labels swapped.
#' @examples
#' old <- c(north = "Site A", south = "Site B")
#' new <- c(north = "Site B", south = "Site A")
#' relabel_sites(c("Site A", "Site B: 0.71"), old, new)
#' @export
relabel_sites <- function(x, old_labels, new_labels) {
  if (is.character(old_labels) && length(old_labels) == 1 && is.null(names(old_labels))) old_labels <- load_site_labels(old_labels)
  if (is.character(new_labels) && length(new_labels) == 1 && is.null(names(new_labels))) new_labels <- load_site_labels(new_labels)
  map <- .label_translation(old_labels, new_labels)
  if (is.data.frame(x)) {
    names(x) <- .relabel_chr(names(x), map)
    for (j in seq_along(x)) {
      if (is.factor(x[[j]])) {
        levels(x[[j]]) <- .relabel_chr(levels(x[[j]]), map)
      } else if (is.character(x[[j]])) {
        x[[j]] <- .relabel_chr(x[[j]], map)
      }
    }
    return(x)
  }
  .relabel_chr(as.character(x), map)
}

.text_ext <- c("csv", "tsv", "json", "md", "txt", "html", "htm")

#' Re-issue study outputs under a new site mapping (text files)
#'
#' Copies `run_dir` to `out_dir`, swapping every old label for the new one in CSV, TSV, JSON, Markdown, text and HTML files (and in
#' file names). Numbers do not change. Images are copied unchanged. The Excel workbook and the DOCX / PDF manuscript are not
#' relabelled in R: use the Python command `scripts/relabel_study.py`, which relabels the workbook and rebuilds the manuscript from it.
#'
#' @param run_dir Output folder of the run (left unchanged).
#' @param out_dir New folder.
#' @param old_labels,new_labels Mappings or mapping files, as in [relabel_sites()].
#' @return A list with `written`, `unchanged` (copied as they are) and `not_relabelled` (left out) file names, invisibly.
#' @export
relabel_output_files <- function(run_dir, out_dir, old_labels, new_labels) {
  if (normalizePath(run_dir, mustWork = TRUE) == normalizePath(out_dir, mustWork = FALSE)) stop("`out_dir` must differ from `run_dir`.", call. = FALSE)
  if (is.character(old_labels) && length(old_labels) == 1 && is.null(names(old_labels))) old_labels <- load_site_labels(old_labels)
  if (is.character(new_labels) && length(new_labels) == 1 && is.null(names(new_labels))) new_labels <- load_site_labels(new_labels)
  map <- .label_translation(old_labels, new_labels)
  dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
  files <- list.files(run_dir, recursive = TRUE, all.files = FALSE)
  written <- character(); unchanged <- character(); left <- character()
  for (f in files) {
    ext <- tolower(.file_ext(f))
    dst <- file.path(out_dir, .relabel_chr(f, map))
    dir.create(dirname(dst), recursive = TRUE, showWarnings = FALSE)
    if (ext %in% .text_ext) {
      txt <- readLines(file.path(run_dir, f), warn = FALSE, encoding = "UTF-8")
      writeLines(.relabel_chr(txt, map), dst, useBytes = TRUE)
      written <- c(written, basename(dst))
    } else if (ext %in% c("png", "jpg", "jpeg", "svg")) {
      file.copy(file.path(run_dir, f), dst, overwrite = TRUE)
      unchanged <- c(unchanged, basename(dst))
    } else {
      left <- c(left, f)
    }
  }
  if (length(left)) {
    message("Not relabelled in R (use scripts/relabel_study.py for the workbook and manuscript): ", paste(left, collapse = ", "))
  }
  invisible(list(written = written, unchanged = unchanged, not_relabelled = left))
}

#' Scan study outputs for real site names
#'
#' Looks for each name (case-insensitive, whole words) in file and folder names, in the text of CSV / JSON / Markdown / text / HTML /
#' YAML / SQL / R / Python / Quarto files, and in every part of xlsx, docx and other zip-based files (in the XML and, for a docx, in
#' the paragraph text with the tags removed; embedded images and fonts are skipped). PDFs, images and other binary files are listed as not scanned (the Python scanner,
#' `scripts/check_run_privacy.py`, also reads PDF text). Hits are reported by the position of the name in `names`, never by the name, so
#' the result can be shared.
#'
#' @param dir Output folder to scan.
#' @param names Real site names, or the path of a text file with one per line (lines starting with `#` are skipped).
#' @return A list: `hits` (data frame with `file`, `name_number`, `where`) and `not_scanned` (file names).
#' @export
check_run_privacy <- function(dir, names) {
  if (length(names) == 1 && file.exists(names)) {
    names <- trimws(readLines(names, warn = FALSE, encoding = "UTF-8"))
    names <- names[names != "" & !startsWith(names, "#")]
  }
  names <- names[nzchar(trimws(names))]
  if (!length(names)) stop("Give at least one site name.", call. = FALSE)
  pats <- paste0("(?i)(?<![A-Za-z0-9])", .re_escape(trimws(names)), "(?![A-Za-z0-9])")
  found <- function(text) which(vapply(pats, function(p) any(grepl(p, text, perl = TRUE, useBytes = TRUE)), logical(1)))
  hits <- data.frame(file = character(), name_number = integer(), where = character(), stringsAsFactors = FALSE)
  add <- function(f, i, w) if (length(i)) hits <<- rbind(hits, data.frame(file = f, name_number = as.integer(i), where = w, stringsAsFactors = FALSE))
  not_scanned <- character()
  text_ext <- c(.text_ext, "log", "yaml", "yml", "sql", "py", "r", "qmd", "rmd", "xml", "typ")
  zip_ext <- c("xlsx", "xlsm", "docx", "pptx", "zip")
  entries <- list.files(dir, recursive = TRUE, include.dirs = TRUE, all.files = FALSE)
  for (f in entries) {
    add(f, found(f), "file or folder name")
    p <- file.path(dir, f)
    if (dir.exists(p)) next
    ext <- tolower(.file_ext(f))
    if (ext %in% text_ext) {
      add(f, found(readLines(p, warn = FALSE, encoding = "UTF-8")), "content")
    } else if (ext %in% zip_ext) {
      tmp <- tempfile("scan_")
      ok <- tryCatch({ utils::unzip(p, exdir = tmp); TRUE }, error = function(e) FALSE, warning = function(w) FALSE)
      if (!ok) { not_scanned <- c(not_scanned, f); next }
      for (part in list.files(tmp, recursive = TRUE)) {
        # images, fonts and other binary parts are not read as text: compressed bytes can match a short name by chance
        head_bytes <- readBin(file.path(tmp, part), "raw", n = 4096L)
        if (grepl("\\.(png|jpe?g|gif|bmp|tiff?|emf|wmf|ico|webp|bin|odttf|ttf|otf|wdp)$", tolower(part)) || any(head_bytes == as.raw(0L))) {
          add(f, found(part), "part name")
          next
        }
        x <- paste(suppressWarnings(readLines(file.path(tmp, part), warn = FALSE, encoding = "UTF-8")), collapse = "\n")
        add(f, found(x), paste("part", part))
        if (ext == "docx" && grepl("\\.xml$", part)) {
          plain <- gsub("<[^>]+>", "", gsub("</w:p>", "\n", x, fixed = TRUE))
          add(f, found(plain), "document text")
        }
      }
      unlink(tmp, recursive = TRUE)
    } else {
      not_scanned <- c(not_scanned, f)
    }
  }
  list(hits = unique(hits), not_scanned = not_scanned)
}
