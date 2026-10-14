# Tests for the 3 modular features from tasks/request1.txt:
# 1. Automated Privacy-Preserving Cell Suppression Auditor (audit_cell_suppression)
# 2. Direct Quarto / Typst Publication Styler (table1_to_quarto, table1_to_typst)
# 3. Built-in Tree Model Explainability (consortium_explainability)

testthat::test_that("Feature A: audit_cell_suppression masks small cell counts in single data frame", {
  df <- data.frame(
    characteristic = c("Age mean (SD)", "Female n (%)", "Diabetes n (%)", "Rare Event n (%)", "P-Value"),
    Overall = c("65.2 (10.1)", "500 (50.0%)", "120 (12.0%)", "8 (0.8%)", "0.042"),
    Cohort_A = c("64.8 (9.8)", "260 (52.0%)", "65 (13.0%)", "5 (1.0%)", "0.150"),
    Cohort_B = c("65.6 (10.4)", "240 (48.0%)", "55 (11.0%)", "3 (0.6%)", "0.210"),
    raw_counts = c(1000L, 500L, 120L, 8L, 1L),
    stringsAsFactors = FALSE
  )

  res <- audit_cell_suppression(df, min_cell_count = 11L, mask_value = "<11")
  testthat::expect_s3_class(res, "data.frame")

  # Rare event cells (< 11) must be masked
  testthat::expect_equal(res$Overall[4], "<11")
  testthat::expect_equal(res$Cohort_A[4], "<11")
  testthat::expect_equal(res$Cohort_B[4], "<11")
  testthat::expect_equal(res$raw_counts[4], "<11")
  testthat::expect_equal(res$raw_counts[5], "<11")

  # Non-rare cells (>= 11) and continuous stats preserved
  testthat::expect_equal(res$Overall[1], "65.2 (10.1)")
  testthat::expect_equal(res$Overall[2], "500 (50.0%)")
  testthat::expect_equal(res$Cohort_A[3], "65 (13.0%)")
  testthat::expect_equal(res$Overall[5], "0.042")
})

testthat::test_that("Feature A: consortium_privacy_audit handles multi-table lists and S3 methods", {
  t1 <- data.frame(
    var = c("Hypertension", "UltraRare"),
    site_1 = c("45 (45.0%)", "4 (4.0%)"),
    site_2 = c("55 (55.0%)", "2 (2.0%)"),
    stringsAsFactors = FALSE
  )
  t2 <- data.frame(
    group = c("Arm A", "Arm B"),
    adverse_events = c(15L, 6L),
    stringsAsFactors = FALSE
  )

  tables <- list(table1 = t1, table2 = t2)
  audited <- consortium_privacy_audit(tables, min_cell_count = 10L, mask_value = "<10")

  testthat::expect_s3_class(audited, "consortium_privacy_audit")
  testthat::expect_false(audited$is_clean)
  testthat::expect_equal(audited$violation_count, 3L)

  # Check cleaned tables in list
  clean_t1 <- audited$tables$table1
  clean_t2 <- audited$tables$table2
  testthat::expect_equal(clean_t1$site_1[2], "<10")
  testthat::expect_equal(clean_t1$site_2[2], "<10")
  testthat::expect_equal(clean_t2$adverse_events[2], "<10")
  testthat::expect_equal(as.integer(clean_t2$adverse_events[1]), 15L)

  # S3 summary and as.data.frame
  sum_df <- summary(audited)
  testthat::expect_s3_class(sum_df, "data.frame")
  testthat::expect_equal(nrow(sum_df), 2L)
  testthat::expect_true("suppressed_cells" %in% colnames(sum_df))

  df_all <- as.data.frame(audited)
  testthat::expect_s3_class(df_all, "data.frame")
  testthat::expect_equal(nrow(df_all), 3L)

  # Print method smoke test
  testthat::expect_output(print(audited), "Consortium Privacy Audit")
})

testthat::test_that("Feature B: table1_to_quarto and table1_to_typst output valid markup", {
  df <- data.frame(
    Characteristic = c("Age, median [IQR]", "Serum Creatinine (mg/dL)", "Female, n (%)"),
    Overall = c("64.0 [52.0, 75.0]", "1.10 [0.85, 1.45]", "245 (49.0%)"),
    Cohort_1 = c("63.0 [51.0, 74.0]", "1.08 [0.82, 1.40]", "120 (48.0%)"),
    p_value = c("0.420", "0.185", "0.724"),
    stringsAsFactors = FALSE
  )

  # Quarto export
  qmd <- table1_to_quarto(
    df,
    caption = "Study Baseline Characteristics",
    label = "tbl-baseline",
    loinc_footnotes = list("Serum Creatinine" = "2160-0")
  )
  testthat::expect_type(qmd, "character")
  testthat::expect_true(grepl("#\\| label: tbl-baseline", qmd))
  testthat::expect_true(grepl("Study Baseline Characteristics", qmd))
  testthat::expect_true(grepl("Characteristic", qmd))

  # Typst export
  typ <- table1_to_typst(
    df,
    caption = "Study Baseline Characteristics",
    label = "tbl-baseline",
    loinc_footnotes = list("Serum Creatinine" = "2160-0")
  )
  testthat::expect_type(typ, "character")
  testthat::expect_true(grepl("#figure\\(", typ))
  testthat::expect_true(grepl("#table\\(", typ))
  testthat::expect_true(grepl("<tbl-baseline>", typ))
  testthat::expect_true(grepl("2160-0", typ))

  # export_table1 wrapper
  qmd_exp <- export_table1(df, format = "quarto", caption = "Exported Cohort")
  testthat::expect_true(grepl("#\\| tbl-cap:", qmd_exp))

  typ_exp <- export_table1(df, format = "typst", caption = "Exported Cohort")
  testthat::expect_true(grepl("#table\\(", typ_exp))
})

testthat::test_that("Feature B: table1_generator has to_quarto and to_typst methods", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE))

  gen <- table1_generator(con = con)
  testthat::expect_true(is.function(gen$to_quarto))
  testthat::expect_true(is.function(gen$to_typst))

  df <- data.frame(
    Variable = c("Age", "Sex"),
    Value = c("55 [45, 65]", "1 (50.0%)"),
    stringsAsFactors = FALSE
  )

  qmd <- gen$to_quarto(table1_data = df, caption = "Generator Quarto", label = "tbl-gen")
  testthat::expect_true(grepl("#\\| label: tbl-gen", qmd))

  typ <- gen$to_typst(table1_data = df, caption = "Generator Typst", label = "tbl-gen")
  testthat::expect_true(grepl("#table\\(", typ))
})

testthat::test_that("Feature C: consortium_explainability computes importance, TRIPOD table, and drift", {
  set.seed(42)
  n <- 60L
  X <- data.frame(
    age = stats::rnorm(n, 65, 10),
    bmi = stats::rnorm(n, 28, 5),
    creatinine = stats::rnorm(n, 1.2, 0.4),
    glucose = stats::rnorm(n, 110, 25)
  )
  # Fit a simple glm model
  y <- as.numeric(X$age * 0.05 + X$creatinine * 0.8 + stats::rnorm(n) > 4.0)
  m <- stats::glm(y ~ age + bmi + creatinine + glucose, data = X, family = stats::binomial())

  expl <- consortium_explainability(
    model = m,
    X = X,
    y = y,
    method = "auto"
  )

  testthat::expect_s3_class(expl, "consortium_explainability")
  testthat::expect_equal(length(expl$feature_names), 4L)

  # Importance data frame
  imp <- expl$importance
  testthat::expect_s3_class(imp, "data.frame")
  testthat::expect_equal(nrow(imp), 4L)
  testthat::expect_true(all(c("rank", "feature", "importance", "direction", "cohort_median", "cohort_iqr") %in% colnames(imp)))

  # TRIPOD Item 13b summary table
  tripod <- expl$tripod_summary
  testthat::expect_s3_class(tripod, "data.frame")
  testthat::expect_true(all(c("TRIPOD_Rank", "Candidate_Predictor", "Univariable_Direction", "Reporting_Standard") %in% colnames(tripod)))
  testthat::expect_equal(tripod$Reporting_Standard[1], "TRIPOD Item 13b / STROBE Item 15a")

  # Multi-site importance drift auditing
  sites <- rep(c("SiteA", "SiteB"), each = 30L)
  expl_drift <- consortium_explainability(
    model = m,
    X = X,
    site_labels = sites
  )
  testthat::expect_false(is.null(expl_drift$site_drift))
  testthat::expect_true("rank_SiteA" %in% colnames(expl_drift$site_drift))
  testthat::expect_true("rank_SiteB" %in% colnames(expl_drift$site_drift))

  # S3 methods
  testthat::expect_equal(nrow(as.data.frame(expl)), 4L)
  testthat::expect_equal(nrow(summary(expl)), 4L)
  testthat::expect_output(print(expl), "Consortium Tree Model Explainability")

  # Plotting smoke test
  pdf_null <- tempfile(fileext = ".pdf")
  grDevices::pdf(pdf_null)
  testthat::expect_silent(plot(expl, top_n = 4L))
  grDevices::dev.off()
  unlink(pdf_null)
})
