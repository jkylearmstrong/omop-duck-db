test_that("labels work for any number of sites and are repeatable with a seed", {
  for (k in c(1L, 3L, 5L, 8L, 30L)) {
    sites <- paste0("hospital_", seq_len(k))
    lab <- assign_site_labels(sites, seed = 7)
    expect_equal(names(lab), sites)
    expect_equal(anyDuplicated(lab), 0L)
    expect_true(all(startsWith(lab, "Site ")))
    expect_identical(lab, assign_site_labels(sites, seed = 7))
  }
  expect_equal(unname(assign_site_labels(paste0("s", 1:28), mode = "ordered")[c(1, 26, 27, 28)]),
               c("Site A", "Site Z", "Site AA", "Site AB"))
})

test_that("a mapping file round-trips in the format Python reads", {
  f <- tempfile(fileext = ".json")
  lab <- c(north = "Site B", south = "Site A", east = "Site C")
  save_site_labels(lab, f)
  expect_identical(load_site_labels(f), lab)
  expect_identical(jsonlite::read_json(f), list(north = "Site B", south = "Site A", east = "Site C"))
})

test_that("validate_site_labels refuses a site without a label, shared labels and real names", {
  sites <- c("north", "south", "east", "west", "central", "lakeside")
  five <- assign_site_labels(sites[1:5], mode = "ordered")
  expect_error(validate_site_labels(sites, five), "1 of 6 site\\(s\\) have no label \\(site number\\(s\\) 6")
  expect_error(validate_site_labels(c("a1", "b1"), c(a1 = "Site A", b1 = "site a")), "share a label")
  expect_error(validate_site_labels(c("north", "south"), c(north = "south", south = "Site A")), "same as a site name")
  # a consortium-wide file with more sites than this run is fine
  all8 <- assign_site_labels(paste0("h", 1:8), seed = 1)
  expect_equal(names(validate_site_labels(c("h3", "h7"), all8)), c("h3", "h7"))
})

test_that("relabel_sites swaps labels at once and leaves everything else", {
  old <- c(north = "Site A", south = "Site B", east = "Site C")
  new <- c(north = "Site B", south = "Site A", east = "Site AA")
  expect_equal(relabel_sites(c("Site A", "Site B vs Site A", "Site C", "Site CA", NA), old, new),
               c("Site B", "Site A vs Site B", "Site AA", "Site CA", NA))
  df <- data.frame(site = c("Site A", "Site B", "Site C"), auc = c(0.7, 0.8, 0.9), stringsAsFactors = FALSE)
  df$f <- factor(df$site)
  out <- relabel_sites(df, old, new)
  expect_equal(out$site, c("Site B", "Site A", "Site AA"))
  expect_equal(as.character(out$f), c("Site B", "Site A", "Site AA"))
  expect_equal(out$auc, df$auc)
})

test_that("relabel_output_files rewrites text outputs and the privacy scan finds real names", {
  run <- tempfile("run_"); dir.create(run)
  writeLines(c("site,auc", "Site A,0.71", "Site B,0.65"), file.path(run, "Study_LOSO.csv"))
  writeLines('{"labels": ["Site A", "Site B"], "timings_seconds": {"site_Site A": 1}}', file.path(run, "Study_run.json"))
  old <- c(north = "Site A", south = "Site B")
  new <- c(north = "Site B", south = "Site A")
  out <- tempfile("run2_")
  res <- relabel_output_files(run, out, old, new)
  expect_setequal(res$written, c("Study_LOSO.csv", "Study_run.json"))
  expect_equal(readLines(file.path(out, "Study_LOSO.csv")), c("site,auc", "Site B,0.71", "Site A,0.65"))
  expect_true(grepl('"site_Site B"', paste(readLines(file.path(out, "Study_run.json")), collapse = "")))

  clean <- check_run_privacy(out, c("Springfield General", "north"))
  expect_equal(nrow(clean$hits), 0L)
  writeLines("loaded from SPRINGFIELD general", file.path(out, "log.txt"))
  dirty <- check_run_privacy(out, c("Springfield General", "north"))
  expect_equal(dirty$hits$file, "log.txt")
  expect_equal(dirty$hits$name_number, 1L)
})

test_that("image bytes inside a docx are not read as text (no false positive from compressed data)", {
  f <- testthat::test_path("testdata", "embedded_image_bytes.docx")
  d <- tempfile("scan_"); dir.create(d)
  file.copy(f, file.path(d, "Study_Manuscript.docx"))
  res <- check_run_privacy(d, c("Brockway", "Site Q"))
  expect_equal(nrow(res$hits), 0L)
  expect_equal(nrow(check_run_privacy(d, "Site A")$hits) > 0, TRUE)  # the document text is still scanned
})
