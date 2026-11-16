# v0.29.0: bundled OHDSI concept sets, the Circe-style resolver and the LOINC coverage check.
# Mirror of tests/test_concept_sets.py. Synthetic vocabulary only.

cs_vocab <- function(parts = TRUE) {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, vocabulary_id VARCHAR, concept_code VARCHAR,
                       concept_class_id VARCHAR, standard_concept VARCHAR, invalid_reason VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO concept VALUES
    (900, 'Bilirubin.total', 'LOINC', 'LP900', 'LOINC Component', NULL, NULL), (901, 'Mass concentration', 'LOINC', 'LP901', 'LOINC Property', NULL, NULL),
    (902, 'Moles/volume', 'LOINC', 'LP902', 'LOINC Property', NULL, NULL), (903, 'Qn', 'LOINC', 'LP903', 'LOINC Scale', NULL, NULL),
    (904, 'Point in time (spot)', 'LOINC', 'LP904', 'LOINC Time', NULL, NULL), (910, 'Serum or Plasma', 'LOINC', 'LP910', 'LOINC System', NULL, NULL),
    (911, 'Blood', 'LOINC', 'LP911', 'LOINC System', NULL, NULL), (912, 'Urine', 'LOINC', 'LP912', 'LOINC System', NULL, NULL),
    (3024128, 'Bilirubin.total [Mass/volume] in Serum or Plasma', 'LOINC', '1975-2', 'Lab Test', 'S', NULL),
    (3006140, 'Bilirubin.total [Mass/volume] in Blood', 'LOINC', '34543-9', 'Lab Test', 'S', NULL),
    (3013721, 'Bilirubin.total [Moles/volume] in Serum or Plasma', 'LOINC', '14631-6', 'Lab Test', 'S', NULL),
    (3002000, 'Bilirubin.total [Mass/volume] in Urine', 'LOINC', '5770-3', 'Lab Test', 'S', NULL),
    (21600712, 'A10', 'ATC', 'A10', 'ATC 2nd', 'C', NULL), (1503297, 'metformin', 'RxNorm', '6809', 'Ingredient', 'S', NULL),
    (44000001, 'metformin source', 'Local', 'MET', 'Drug', NULL, NULL)")
  DBI::dbExecute(con, "CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO concept_relationship VALUES (44000001, 1503297, 'Maps to')")
  if (parts) {
    tests <- list(c(3024128, 901, 910), c(3006140, 901, 911), c(3013721, 902, 910), c(3002000, 901, 912))
    for (t in tests) DBI::dbExecute(con, sprintf(paste("INSERT INTO concept_relationship VALUES (%1$.0f, 900, 'Has component'),",
                                                       "(%1$.0f, %2$.0f, 'Has property'), (%1$.0f, 903, 'Has scale type'),",
                                                       "(%1$.0f, 904, 'Has time aspect'), (%1$.0f, %3$.0f, 'Has system')"), t[1], t[2], t[3]))
  }
  DBI::dbExecute(con, "CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT,
                       min_levels_of_separation INT, max_levels_of_separation INT)")
  DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (21600712, 21600712, 0, 0), (21600712, 1503297, 2, 2), (1503297, 1503297, 0, 0)")
  con
}

test_that("bundled concept sets load and equal the exported constants", {
  m <- concept_set_manifest()
  expect_true(nzchar(m$version))
  expect_identical(names(BENCHMARK_LAB_LOINCS), m$domains$labs)
  expect_identical(BENCHMARK_LAB_LOINCS, lapply(load_concept_sets("labs"), concept_set_codes))
  expect_identical(EDERRI_MEDICATION_SPECS$biguanides$ancestor_concept_ids, 21600745)
  expect_identical(DEFAULT_MEDICATION_CONCEPTS$metformin, c(1503297L, 21600745L))
  for (n in setdiff(names(DEFAULT_LAB_LOINCS), "egfr")) expect_identical(DEFAULT_LAB_LOINCS[[n]], BENCHMARK_LAB_LOINCS[[n]])
  for (f in list_concept_sets()) expect_true(length(load_concept_set(f)$expression$items) > 0)
  expect_error(load_concept_set("labs/not_a_lab"), "no concept set")
})

test_that("the resolver follows Circe semantics", {
  con <- cs_vocab()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  ids <- function(e) resolve_concept_sets(con, list(s = e))$concept_id
  expect_equal(ids(concept_set_from_ids(21600712)), c(1503297, 21600712))
  expect_equal(ids(concept_set_from_ids(21600712, include_descendants = FALSE)), 21600712)
  mapped <- list(items = list(list(concept = list(CONCEPT_ID = 1503297), includeDescendants = FALSE, includeMapped = TRUE, isExcluded = FALSE)))
  expect_equal(ids(mapped), c(1503297, 44000001))
  minus <- concept_set_from_ids(21600712)
  minus$items[[2]] <- list(concept = list(CONCEPT_ID = 1503297), includeDescendants = FALSE, includeMapped = FALSE, isExcluded = TRUE)
  expect_equal(ids(minus), 21600712)
  expect_warning(got <- ids(concept_set_from_codes(c("1975-2", "0000-0"))), "not in this vocabulary.*0000-0")
  expect_equal(got, 3024128)
})

test_that("loinc_coverage names the codes a set misses, and the benchmark warns", {
  con <- cs_vocab()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id BIGINT,
                       measurement_date DATE, measurement_datetime TIMESTAMP, value_as_number DOUBLE)")
  DBI::dbExecute(con, "INSERT INTO measurement VALUES (1, 1, 3024128, DATE '2024-01-02', NULL, 1), (2, 2, 3006140, DATE '2024-01-02', NULL, 1),
                       (3, 2, 3006140, DATE '2024-01-02', NULL, 1), (4, 3, 3013721, DATE '2024-01-02', NULL, 1), (5, 4, 3024128, DATE '2024-01-02', NULL, 1)")
  cohort <- data.frame(subject_id = 1:4, cohort_start_date = as.Date("2024-01-01"), cohort_end_date = as.Date("2024-01-05"))
  sets <- list(bilirubin_total = load_concept_set("labs/bilirubin_total")$expression)
  miss <- loinc_coverage(con, sets, cohort = cohort)
  expect_identical(miss$concept_code, c("34543-9", "14631-6"))
  expect_equal(round(miss$share, 2), c(0.4, 0.2))
  expect_identical(miss$same_property, c(TRUE, FALSE))
  expect_warning(omopduckdb:::.cs_check_lab_coverage(con, sets, cohort, 0), "concept_set_coverage.*lab_bilirubin_total.*34543-9 40.0%")
  con2 <- cs_vocab(parts = FALSE)
  on.exit(DBI::dbDisconnect(con2, shutdown = TRUE), add = TRUE)
  expect_null(loinc_coverage(con2, sets))
})
