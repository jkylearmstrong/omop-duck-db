# A whole small study on a synthetic OMOP DuckDB, using the package's tools in the order a downstream study meets them.
#
#   source(system.file("examples", "study_walkthrough.R", package = "omopduckdb")); study_walkthrough()
#
# 1. Cohort       Run the index-stay cohort (a Circe definition) with the native compiler.
# 2. Reconcile    Compare it with a legacy patient list and say why patients are missing.
# 3. Own feature  Add a site-specific score to the CDM so FeatureExtraction / PLP can read it, and register its concept id.
# 4. Ablation     Judge the new feature against noise; compare two model runs.
# 5. Bundle       Package the model with the feature's concept id, score with the check, and see the check refuse a site where
#                 the same feature has another id.
#
# Everything is synthetic and seeded; nothing leaves the process. The same walkthrough in Python is inst/examples/study_walkthrough.py.

walkthrough_cdm <- function(n = 400, seed = 1) {
  set.seed(seed)
  db <- tempfile(fileext = ".duckdb")
  build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  person <- data.frame(person_id = seq_len(n), gender_concept_id = 8507L, year_of_birth = sample(1930:2011, n, TRUE),
                       race_concept_id = 0L, ethnicity_concept_id = 0L, person_source_value = sprintf("MRN%05d", seq_len(n)))
  duckdb::duckdb_register(con, "_p", person)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) SELECT * FROM _p")
  obs_end <- ifelse(runif(n) < 0.08, as.Date("2019-06-30"), as.Date("2022-12-31"))
  duckdb::duckdb_register(con, "_o", data.frame(person_id = person$person_id, s = as.Date("2015-01-01"), e = as.Date(obs_end, origin = "1970-01-01")))
  DBI::dbExecute(con, "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) SELECT person_id, person_id, s, e, 0 FROM _o")
  k <- sample(1:3, n, TRUE)
  v <- data.frame(person_id = rep(person$person_id, k))
  v$visit_occurrence_id <- seq_len(nrow(v))
  v$visit_concept_id <- sample(c(9201L, 262L), nrow(v), TRUE, prob = c(0.85, 0.15))
  v$s <- as.Date("2018-01-01") + sample(0:899, nrow(v), TRUE)
  v$e <- v$s + sample(2:9, nrow(v), TRUE)
  duckdb::duckdb_register(con, "_v", v)
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) SELECT visit_occurrence_id, person_id, visit_concept_id, s, e, 0 FROM _v")
  dead <- sample(person$person_id, round(0.06 * n))
  last <- tapply(v$e, v$person_id, max)
  d <- data.frame(person_id = dead, d = as.Date(last[as.character(dead)], origin = "1970-01-01") + sample(-3:40, length(dead), TRUE))
  duckdb::duckdb_register(con, "_d", d)
  DBI::dbExecute(con, "INSERT INTO death (person_id, death_date, death_type_concept_id) SELECT person_id, d, 0 FROM _d")
  list(con = con, db = db, person = person)
}

study_walkthrough <- function(out_dir = NULL, verbose = TRUE) {
  say <- function(...) if (verbose) cat(..., sep = "")
  out <- out_dir %||% tempfile("omop_walkthrough_")
  dir.create(out, showWarnings = FALSE, recursive = TRUE)
  cdm <- walkthrough_cdm()
  con <- cdm$con; person <- cdm$person
  on.exit({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(cdm$db) }, add = TRUE)
  set.seed(7)
  result <- list()

  # 1. Cohort: a Circe definition, evaluated by the native compiler (no Java, no WebAPI).
  definition <- jsonlite::fromJSON(file.path(cohort_definitions_dir(), "target_cohort.circe.json"), simplifyVector = FALSE)
  cohort <- execute_circe_cohort(con, definition)
  say(sprintf("1. cohort: %d index stays from %d people\n", length(unique(cohort$subject_id)), nrow(person)))
  result$cohort_n <- length(unique(cohort$subject_id))

  # 2. Reconcile with a legacy list: most of our cohort, some people the cohort rightly excludes, some ids that do not exist.
  in_cohort <- person$person_id %in% cohort$subject_id
  members <- person$person_source_value[in_cohort]; others <- person$person_source_value[!in_cohort]
  legacy <- data.frame(person_source_value = c(members[seq_len(floor(0.9 * length(members)))], head(others, 25), "MRN99999", "MRN99998"))
  rec <- reconcile_cohort(con, legacy, definition = definition, min_cell = 3)
  say(sprintf("2. reconcile: %d of %d legacy patients found; why the rest are missing:\n", rec$counts$both, rec$counts$n_reference))
  if (verbose) print(rec$gaps[, c("reason", "detail", "n", "pct")], row.names = FALSE)
  result$reconcile <- rec$counts; result$gaps <- rec$gaps

  # 3. Your own feature: write it to the CDM so FeatureExtraction / PLP / this package read it like any other covariate.
  ref <- merge(cohort, person[, c("person_id", "year_of_birth")], by.x = "subject_id", by.y = "person_id")
  age <- as.integer(format(as.Date(ref$cohort_start_date), "%Y")) - ref$year_of_birth
  comorbidity <- rpois(nrow(ref), pmin(pmax((age - 30) / 25, 0.2), 3))            # the burden the site score measures, not just age
  charlson_site <- pmin(pmax(comorbidity + round(rnorm(nrow(ref), 0, 0.5)), 0), 10)
  feats <- data.frame(person_id = ref$subject_id, date = as.Date(ref$cohort_start_date), charlson_site = charlson_site)
  concepts <- write_custom_features(con, feats, labels = c(charlson_site = "Charlson index, site definition"))
  registry_file <- file.path(out, "custom_concepts.json")
  export_custom_concept_registry(con, registry_file)
  say(sprintf("3. own feature: charlson_site is concept %.0f; registry written to %s\n", as.numeric(concepts$charlson_site[[1]]), registry_file))
  result$concept_id <- as.numeric(concepts$charlson_site[[1]])

  # 4. Is the feature worth keeping, and are two model runs really different?
  x <- data.frame(age = as.numeric(age), charlson_site = charlson_site, noise = rnorm(nrow(ref)))
  z <- 0.02 * (x$age - 60) + 0.6 * comorbidity - 1.2
  y <- as.integer(runif(nrow(x)) < 1 / (1 + exp(-z)))
  abl <- ablate_feature_groups(x, y, list(charlson_site = "charlson_site", noise = "noise"), n_boot = 100, seed = 2)
  drop <- abl[abl$mode == "drop", ]
  say("4. ablation (change in AUROC when the group is removed; 95% interval):\n")
  for (i in seq_len(nrow(drop))) say(sprintf("   %-14s %+.3f  [%+.3f, %+.3f]\n", drop$group[i], drop$auroc_delta[i], drop$auroc_delta_lo[i], drop$auroc_delta_hi[i]))
  result$ablation <- drop
  cut <- floor(0.7 * nrow(x)); tr <- seq_len(cut); te <- setdiff(seq_len(nrow(x)), tr)
  p_full <- ridge_logistic_fit_predict(x[tr, ], y[tr], x[te, ])
  p_small <- ridge_logistic_fit_predict(x[tr, "age", drop = FALSE], y[tr], x[te, "age", drop = FALSE])
  runs <- lapply(list(p_small, p_full), function(p) data.frame(person_id = ref$subject_id[te], pred = p, outcome = y[te]))
  cmp <- compare_model_runs(runs[[1]], runs[[2]], run_names = c("age only", "with charlson_site"), n_boot = 200)
  d <- cmp$difference[cmp$difference$metric == "auroc", ]
  say(sprintf("   AUROC %.3f -> %.3f  (difference %+.3f, interval [%+.3f, %+.3f])\n", d[["age only"]], d[["with charlson_site"]], d$delta, d$delta_lo, d$delta_hi))
  result$model_difference <- d

  # 5. Package the model with the concept id it was trained with; the check refuses a site where the id means something else.
  spec <- stamp_feature_spec_concepts(list(features = list(list(name = "age", dtype = "float", bounds = c(0, 120), impute = 60),
                                                           list(name = "charlson_site", dtype = "float", bounds = c(0, 30), impute = 0))), con)
  bundle_dir <- file.path(out, "bundle")
  write_model_bundle(bundle_dir, name = "walkthrough", version = "1", feature_spec = spec,
                     components = list(list(name = "glm", kind = "logistic", filename = "glm.json", weight = 1,
                                            content = list(intercept = -1.2, coef = c(0.02, 0.6)))))
  new <- data.frame(person_id = ref$subject_id, age = x$age, charlson_site = x$charlson_site)
  scores <- score_cohort(bundle_dir, new, check_concepts = con)
  say(sprintf("5. bundle: scored %d patients at the training site (concept check passed)\n", nrow(scores)))
  other <- walkthrough_cdm(seed = 2)
  on.exit({ DBI::dbDisconnect(other$con, shutdown = TRUE); unlink(other$db) }, add = TRUE)
  write_custom_features(other$con, data.frame(person_id = feats$person_id[1:5], date = feats$date[1:5], elixhauser_site = 1))   # another feature got 2001000000 first
  write_custom_features(other$con, feats[1:5, ])
  refused <- tryCatch({ score_cohort(bundle_dir, new, check_concepts = other$con); "" }, error = function(e) conditionMessage(e))
  say(sprintf("   at a site where charlson_site has another id: %s...\n", substr(refused, 1, 110)))
  result$scored <- nrow(scores); result$refused <- refused
  invisible(result)
}
