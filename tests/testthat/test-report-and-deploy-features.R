# Per-variable Table 1 decimals + section headers, Table 3 odds ratios, ONNX export, FHIR inpatient medications.
# Mirror of tests/test_report_and_deploy_features.py.

t1_df <- function(n = 200) {
  set.seed(0)
  data.frame(outcome_flag = sample(0:1, n, TRUE), lab_creatinine = round(rnorm(n, 1.4, 1.4), 3),
             age = rnorm(n, 65, 12), female = sample(0:1, n, TRUE))
}
n_decimals <- function(cell) nchar(strsplit(strsplit(cell, " ")[[1]][1], ".", fixed = TRUE)[[1]][2])

test_that("generate_table1 takes per-variable decimals with a default", {
  t <- generate_table1(t1_df(), continuous_vars = c("lab_creatinine", "age"), categorical_vars = "female",
                       decimal_places = c(lab_creatinine = 2))$table1
  cr <- t[t$Variable == "lab_creatinine" & t$Category == "Mean (SD)", "Overall (N=200)"]
  ag <- t[t$Variable == "age" & t$Category == "Mean (SD)", "Overall (N=200)"]
  expect_equal(n_decimals(cr), 2)
  expect_equal(n_decimals(ag), 1)
  t3 <- generate_table1(t1_df(), continuous_vars = "age", decimal_places = c(default = 3))$table1
  expect_equal(n_decimals(t3[t3$Category == "Mean (SD)", "Overall (N=200)"]), 3)
})

test_that("generate_table1 groups order variables under header rows", {
  base <- generate_table1(t1_df(), continuous_vars = c("lab_creatinine", "age"), categorical_vars = "female")$table1
  expect_equal(base$Variable[2:3], c("lab_creatinine", "lab_creatinine"))
  g <- generate_table1(t1_df(), continuous_vars = c("lab_creatinine", "age"), categorical_vars = "female",
                       groups = list(Demographics = c("age", "female"), `Laboratory Values` = c("lab_creatinine", "nope")))$table1
  heads <- which(g$Variable %in% c("Demographics", "Laboratory Values"))
  expect_equal(g$Variable[heads], c("Demographics", "Laboratory Values"))
  expect_true(all(unlist(g[heads[1], setdiff(names(g), "Variable")]) == ""))
  expect_equal(sum(g$Variable[(heads[1] + 1):(heads[2] - 1)] == "age"), 2)
  rest <- generate_table1(t1_df(), continuous_vars = c("age", "lab_creatinine"), groups = list(Demographics = "age"))$table1
  expect_true("Other" %in% rest$Variable)
})

test_that("generate_table3_odds_ratios scales increments and exports", {
  set.seed(1); n <- 4000
  X <- data.frame(systolic_bp = rnorm(n, 130, 20), diabetes = sample(0:1, n, TRUE))
  p <- 1 / (1 + exp(-(-3 + 0.03 * (X$systolic_bp - 130) + 0.7 * X$diabetes)))
  y <- as.integer(runif(n) < p)
  per_unit <- generate_table3_odds_ratios(NULL, X, y, continuous_scale = NULL)
  sd1 <- generate_table3_odds_ratios(NULL, X, y)
  ten <- generate_table3_odds_ratios(NULL, X, y, continuous_scale = c(systolic_bp = 10))
  expect_equal(ten$Increment[1], "per 10-unit")
  expect_match(sd1$Increment[1], "^per 1 SD")
  expect_equal(ten$or[1], per_unit$or[1]^10, tolerance = 1e-8)
  expect_equal(sd1$Increment[2], "Yes vs No")
  expect_true(sd1$or[2] > 1.5 && sd1$or[2] < 3)
  expect_equal(sd1$p_value[1], "<0.001")
  fit <- stats::glm(y ~ ., data = cbind(X, y = y), family = stats::binomial())
  expect_equal(generate_table3_odds_ratios(fit, X, y, continuous_scale = NULL)$or, per_unit$or, tolerance = 1e-8)
  expect_match(export_table3(sd1, "markdown"), "Variable", fixed = TRUE)
  expect_match(export_table3(sd1, "quarto"), "tbl-table3", fixed = TRUE)
  expect_match(export_table3(sd1, "typst"), "#figure", fixed = TRUE)
})

test_that("export_to_onnx writes a graph and refuses what it cannot reproduce", {
  spec <- list(features = list(list(name = "a", dtype = "float", impute = 0), list(name = "b", dtype = "float", impute = 0)))
  mk <- function(cal = NULL, kind = "logistic") write_model_bundle(
    file.path(tempfile(), "b"), name = "m", version = "1", feature_spec = spec, calibration = cal,
    components = list(list(kind = kind, filename = "g1.json", weight = 2,
                           content = list(intercept = -1, coef = list(0.5, -0.2), mean = list(1, 2), scale = list(2, 4))),
                      list(kind = "logistic", filename = "g2.json", content = list(intercept = 0.3, coef = list(0.1, 0.4)))))
  out <- file.path(tempfile(), "m.onnx")
  export_to_onnx(mk(list(method = "platt", slope = 0.9, intercept = -0.1)), out)
  expect_true(file.exists(out)); expect_gt(file.size(out), 200)
  expect_true(file.exists(paste0(out, ".features.json")))
  bytes <- readBin(out, "raw", file.size(out))
  expect_equal(as.integer(bytes[1:2]), c(0x08, 0x08))  # ir_version = 8
  expect_error(export_to_onnx(mk(list(method = "isotonic", x = list(0, 1), y = list(0, 1))), out), "not supported")
})

test_that("score_fhir_bundle reads inpatient MedicationAdministration / MedicationStatement into drug_exposure", {
  rx <- "http://www.nlm.nih.gov/research/umls/rxnorm"
  code <- function(c) list(coding = list(list(system = rx, code = c)))
  res <- function(...) list(resource = list(...))
  b <- list(resourceType = "Bundle", entry = list(
    res(resourceType = "Patient", id = "p1", gender = "male", birthDate = "1950-03-02"),
    res(resourceType = "Encounter", id = "e1", class = list(code = "IMP"), period = list(start = "2024-01-01", end = "2024-01-05")),
    res(resourceType = "MedicationRequest", medicationCodeableConcept = code("1"), authoredOn = "2024-01-02"),
    res(resourceType = "MedicationAdministration", status = "completed", medicationCodeableConcept = code("2"),
        effectivePeriod = list(start = "2024-01-03T08:00:00Z", end = "2024-01-04T08:00:00Z")),
    res(resourceType = "MedicationAdministration", status = "not-done", medicationCodeableConcept = code("3"), effectiveDateTime = "2024-01-03"),
    res(resourceType = "MedicationStatement", status = "active", medicationCodeableConcept = code("4"), dateAsserted = "2024-01-04"),
    res(resourceType = "MedicationStatement", status = "active", medicationCodeableConcept = code("99"))))
  seen <- NULL
  testthat::local_mocked_bindings(score_encounter = function(con, ...) {
    seen <<- DBI::dbGetQuery(con, "SELECT drug_concept_id, CAST(drug_exposure_end_date AS VARCHAR) AS e FROM drug_exposure ORDER BY drug_exposure_id")
    list()
  })
  cm <- stats::setNames(as.list(1001:1004), paste0(rx, "|", 1:4))
  r <- score_fhir_bundle(b, NULL, concept_map = cm)
  expect_equal(seen$drug_concept_id, c(1001L, 1002L, 1004L))
  expect_equal(seen$e[2], "2024-01-04")
  expect_equal(r$fhir$unmapped_codes, paste0(rx, "|99"))
})
