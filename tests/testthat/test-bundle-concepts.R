# The R bundle concept check behaves like omop_etl.bundle_concepts (tests/test_bundle_concepts.py).
bc_spec <- list(features = list(list(name = "age", dtype = "float", bounds = c(0, 120), impute = 60),
                                list(name = "charlson_site", dtype = "float", bounds = c(0, 30), impute = 0)))

bc_site <- function(first = NULL, env = parent.frame()) {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db) }, envir = env)
  d <- as.Date("2020-01-01")
  if (!is.null(first)) { f <- data.frame(person_id = 1, date = d); f[[first]] <- 1; write_custom_features(con, f) }
  write_custom_features(con, data.frame(person_id = 1, date = d, charlson_site = 3))
  con
}

bc_bundle <- function(spec, env = parent.frame()) {
  d <- tempfile("bundle_")
  withr::defer(unlink(d, recursive = TRUE), envir = env)
  write_model_bundle(d, name = "demo", version = "1", feature_spec = spec,
                     components = list(list(name = "glm", kind = "logistic", filename = "glm.json", weight = 1,
                                            content = list(intercept = -1, coef = c(0.01, 0.3)))))
  d
}

test_that("stamping records the ids and the same site validates", {
  a <- bc_site()
  spec <- stamp_feature_spec_concepts(bc_spec, a)
  expect_null(spec$features[[1]]$concept_id)
  expect_equal(spec$features[[2]]$concept_id, 2001000000)
  expect_null(bc_spec$features[[2]]$concept_id)
  out <- validate_bundle_concepts(spec, a)
  expect_equal(out$status, "ok"); expect_equal(out$local_concept_id, 2001000000)
})

test_that("a site with a different id is refused with the reason", {
  spec <- stamp_feature_spec_concepts(bc_spec, bc_site())
  b <- bc_site(first = "elixhauser")
  expect_error(validate_bundle_concepts(spec, b), "different_id")
  out <- validate_bundle_concepts(spec, b, error = FALSE)
  expect_equal(out$status, "different_id"); expect_equal(out$local_concept_id, 2001000001)
})

test_that("a missing feature, an id used by another feature and the concept_key are handled", {
  spec <- list(features = list(list(name = "frailty", concept_id = 2001000000), list(name = "age")))
  out <- validate_bundle_concepts(spec, c(charlson_site = 2001000000), error = FALSE)
  expect_equal(out$status, "id_used_by_other_feature"); expect_match(out$detail, "charlson_site")
  expect_equal(validate_bundle_concepts(spec, numeric(0), error = FALSE)$status, "not_registered_here")
  expect_equal(nrow(validate_bundle_concepts(list(features = list(list(name = "age"))), numeric(0))), 0)
  keyed <- list(features = list(list(name = "charlson_v2", concept_key = "charlson_site", concept_id = 2001000000)))
  expect_equal(validate_bundle_concepts(keyed, c(charlson_site = 2001000000))$status, "ok")
})

test_that("the source can be a registry file and the bundle a directory; score_cohort checks before scoring", {
  skip_if_not_installed("jsonlite")
  a <- bc_site()
  path <- tempfile(fileext = ".json")
  export_custom_concept_registry(a, path)
  d <- bc_bundle(stamp_feature_spec_concepts(bc_spec, a))
  expect_equal(validate_bundle_concepts(d, path)$status, "ok")
  df <- data.frame(person_id = c(1, 2), age = c(50, 70), charlson_site = c(2, 5))
  ok <- score_cohort(d, df, check_concepts = a)
  expect_equal(ok$prob, 1 / (1 + exp(-(-1 + 0.01 * df$age + 0.3 * df$charlson_site))), tolerance = 1e-6)
  expect_equal(nrow(score_cohort(d, df)), 2)
  expect_error(score_cohort(d, df, check_concepts = bc_site(first = "elixhauser")), "do not match this site")
})
