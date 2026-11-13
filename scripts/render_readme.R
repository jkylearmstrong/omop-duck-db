# Regenerates README.md from README.Rmd (README.md is generated; edit README.Rmd, then run this).
#   Rscript scripts/render_readme.R            # render
#   Rscript scripts/render_readme.R --check    # exit 1 if README.md is out of date (for CI / pre-commit)
# Needs: knitr, rmarkdown and Quarto (https://quarto.org) on the PATH.
check <- "--check" %in% commandArgs(TRUE)
root <- normalizePath(if (file.exists("README.Rmd")) "." else "..", winslash = "/")
rmd <- file.path(root, "README.Rmd")
out <- file.path(root, "README.md")
norm <- function(x) gsub("\r\n", "\n", x, fixed = TRUE)
old <- if (file.exists(out)) norm(paste(readLines(out, warn = FALSE), collapse = "\n")) else ""
status <- system2("quarto", c("render", shQuote(rmd), "--to", "gfm"), stdout = FALSE, stderr = FALSE)
if (status != 0) stop("quarto render failed", call. = FALSE)
new <- norm(paste(readLines(out, warn = FALSE), collapse = "\n"))
if (check) {
  writeLines(strsplit(old, "\n", fixed = TRUE)[[1]], out)   # leave the working tree as it was
  if (!identical(old, new)) { message("README.md is out of date: run Rscript scripts/render_readme.R"); quit(status = 1) }
  message("README.md is up to date.")
}
