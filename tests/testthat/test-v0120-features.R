# v0.12.0 R side: disclosure control, CONSORT attrition from counts, parity audit. Synthetic counts only.

reference <- data.frame(
  step_number = 1:5,
  step_name = c("Source population", "Washout", "Age < 18", "Died in hospital", "Transferred out"),
  subjects_retained = c(1000, 900, 850, 820, 800),
  subjects_dropped = c(0, 100, 50, 30, 20),
  percent_retained = c(100, 90, 85, 82, 80),
  stringsAsFactors = FALSE
)
excl <- c(Washout = 100, `Age < 18` = 50, `Died in hospital` = 30, `Transferred out` = 20)

exact_unknowns <- function(df) {
  r <- df$subjects_retained; d <- df$subjects_dropped
  for (i in seq_len(nrow(df))[-1]) {
    if (is.na(r[i - 1]) + is.na(d[i]) + is.na(r[i]) == 1L) return(TRUE)
  }
  shown <- which(!is.na(r))
  for (k in seq_len(max(length(shown) - 1L, 0L))) {
    a <- shown[k]; b <- shown[k + 1L]
    if (b - a > 1L && r[a] - r[b] > 0 && r[a] - r[b] < 11) return(TRUE)
  }
  FALSE
}

test_that("small_cell_mask and format_count follow the rule: 1..threshold-1, zero is safe", {
  expect_equal(small_cell_mask(c(0, 1, 10, 11, 500, NA)), c(FALSE, TRUE, TRUE, FALSE, FALSE, FALSE))
  expect_equal(format_count(c(1234, 5, NA, 0)), c("1,234", "<11", "<11", "0"))
  expect_error(small_cell_mask(1, 0), "positive whole number")
})

test_that("suppress_small_counts blanks counts, dependent columns and does complementary suppression", {
  df <- data.frame(g = c("a", "b", "c"), n = c(5, 0, 400), pct = c(1.2, 0, 98.8))
  out <- suppress_small_counts(df, "n", also = "pct")
  expect_true(is.na(out$n[1]) && is.na(out$pct[1]))
  expect_equal(out$n[2:3], c(0, 400))
  expect_equal(out$suppressed, c(TRUE, FALSE, FALSE))
  expect_equal(df$n[1], 5)

  d2 <- data.frame(grp = c("x", "x", "x", "y", "y"), n = c(4, 60, 300, 80, 90))
  o2 <- suppress_small_counts(d2, "n", group_by = "grp")
  expect_true(is.na(o2$n[1]) && is.na(o2$n[2]))
  expect_equal(o2$n[3:5], c(300, 80, 90))
  expect_equal(suppress_small_counts(d2, "n", group_by = "grp", complementary = FALSE)$n[2], 60)
})

test_that("assert_disclosure_safe names the cells, not the values", {
  expect_error(assert_disclosure_safe(data.frame(n = c(3, 50)), "n"), "n\\[row 1\\]")
  expect_true(assert_disclosure_safe(suppress_small_counts(data.frame(n = c(3, 50)), "n"), "n"))
})

test_that("attrition suppression leaves no number recoverable by subtraction", {
  set.seed(7)
  for (trial in 1:300) {
    n <- sample(2:7, 1)
    ret <- sample(c(0:15, 100:4000), 1)
    for (k in seq_len(n - 1L)) ret <- c(ret, max(tail(ret, 1) - sample(c(0, 1:12, 20:500), 1), 0))
    df <- data.frame(subjects_retained = ret, subjects_dropped = c(0, head(ret, -1) - tail(ret, -1)),
                     percent_retained = 100 * ret / max(ret[1], 1))
    out <- suppress_attrition_table(df, 11)
    expect_false(exact_unknowns(out), info = paste(ret, collapse = ","))
    shown <- out$subjects_retained[!is.na(out$subjects_retained)]
    expect_false(any(shown > 0 & shown < 11))
  }
})

test_that("summarize_attrition_consort matches the hand-built reference table", {
  att <- summarize_attrition_consort(1000, excl)
  expect_s3_class(att, "consort_attrition")
  expect_equal(as.data.frame(unclass(att))[, names(reference)], reference, ignore_attr = TRUE)
  expect_equal(att$exclusion_reason[-1], names(excl))
  att2 <- summarize_attrition_consort(1000, list(list(label = "Washout", remaining = 900, reason = "Prior use"),
                                                 list(label = "Age", excluded = 50)))
  expect_equal(att2$subjects_retained, c(1000, 900, 850))
  expect_equal(att2$exclusion_reason[2], "Prior use")
  expect_error(summarize_attrition_consort(10, c(a = 11)), "cannot exclude")
  expect_error(summarize_attrition_consort(10, c(a = -1)), "cannot exclude")
})

test_that("the exporters render counts and suppressed counts", {
  att <- generate_consort_attrition(initial_n = 1000, exclusions = excl)
  expect_match(to_mermaid(att), "N = 800, 80.0%", fixed = TRUE)
  expect_match(to_mermaid(att), "Excluded: 100", fixed = TRUE)
  expect_true(nzchar(to_typst(att)) && nzchar(to_tikz(att)) && nzchar(to_latex(att)))

  small <- summarize_attrition_consort(sites = list(
    s1 = list(initial_n = 40, exclusions = c(Washout = 5, Age = 3)),
    s2 = list(initial_n = 40, exclusions = c(Washout = 2, Age = 1))))
  expect_true(any(small$suppressed))
  expect_false(exact_unknowns(small))
  expect_match(to_mermaid(small), "<11", fixed = TRUE)
  expect_false(grepl("NA", to_mermaid(small), fixed = TRUE))
  expect_match(to_typst(small), "<11", fixed = TRUE)
  expect_match(to_tikz(small), "<11", fixed = TRUE)
})

test_that("pooling sums sites, checks steps and writes <11 in the CSV", {
  b <- list(initial_n = 500, exclusions = c(Washout = 50, `Age < 18` = 20, `Died in hospital` = 4, `Transferred out` = 3))
  pooled <- summarize_attrition_consort(sites = list(a = list(initial_n = 1000, exclusions = excl), b = b))
  expect_equal(pooled$subjects_retained[1:3], c(1500, 1350, 1280))
  expect_equal(pooled$subjects_dropped, c(0, 150, 70, 34, 23))
  expect_true(all(pooled$n_sites == 2))

  bad <- list(initial_n = 100, exclusions = c(Different = 10))
  expect_error(summarize_attrition_consort(sites = list(a = list(initial_n = 100, exclusions = c(Washout = 10)), b = bad)), "different steps")

  small <- summarize_attrition_consort(initial_n = 30, exclusions = c(Washout = 5, Age = 3), min_cell = 11)
  path <- tempfile(fileext = ".csv")
  write_consort_csv(small, path)
  txt <- paste(readLines(path), collapse = "\n")
  expect_match(txt, "<11", fixed = TRUE)
  expect_false(grepl("NA", txt, fixed = TRUE))
})

test_that("generate_consort_attrition still accepts steps and attrition tables", {
  df <- data.frame(step_number = 1:2, step_name = c("All", "Some"), subjects_retained = c(40, 20),
                   subjects_dropped = c(0, 20), percent_retained = c(100, 50))
  att <- generate_consort_attrition(cohort_data = df)
  expect_equal(att$subjects_retained, c(40, 20))
  expect_error(generate_consort_attrition(), "Could not determine")
})

test_that("audit_matrix_parity flags a shift, new missingness and flipped categories", {
  set.seed(1)
  n <- 400
  legacy <- data.frame(key = seq_len(n), age = rnorm(n, 60, 10), sbp = rnorm(n, 125, 15), sex = sample(c("F", "M"), n, TRUE),
                       stringsAsFactors = FALSE)
  omop <- legacy
  omop$sbp <- omop$sbp + 8
  omop$age[1:60] <- NA
  omop$sex[1:60] <- ifelse(legacy$sex[1:60] == "F", "M", "F")
  a <- audit_matrix_parity(legacy, omop, "key", columns = c("age", "sbp", "sex"))
  led <- a$ledger[match(c("age", "sbp", "sex"), a$ledger$column), ]
  rownames(led) <- led$column
  expect_true(led["sbp", "flagged"]); expect_gt(abs(led["sbp", "smd"]), 0.4)
  expect_gt(led["sbp", "correlation"], 0.999); expect_lt(led["sbp", "ccc"], 0.95)
  expect_true(led["age", "flagged"]); expect_equal(led["age", "missingness_gap"], 0.15)
  expect_equal(led["sex", "kind"], "categorical")
  expect_equal(led["sex", "pct_agreement"], 85)
  expect_setequal(a$flagged, c("age", "sbp", "sex"))
  expect_equal(a$keys$n_matched, 400)
  expect_false("key" %in% names(a$ledger))

  clean <- audit_matrix_parity(legacy, legacy, "key", columns = c("sbp", "sex"))
  expect_length(clean$flagged, 0)
  expect_error(audit_matrix_parity(rbind(legacy, legacy[1, ]), omop, "key"), "duplicate")
  tiny <- audit_matrix_parity(legacy[1:8, ], omop[1:8, ], "key", columns = "sbp")
  expect_true(all(tiny$ledger$suppressed)); expect_true(all(is.na(tiny$ledger$smd)))
})
