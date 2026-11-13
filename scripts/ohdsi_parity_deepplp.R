# Hands the package's sparse concept matrix to OHDSI DeepPatientLevelPrediction through as_plp_data() and checks, on a
# synthetic DuckDB CDM with a known signal:
#   * as_plp_data() output is accepted by PatientLevelPrediction::createStudyPopulation() (outcome count equals the labels)
#   * covariate ids are concept_id * 1000 + analysis_id (conforms to the OHDSI convention)
#   * DeepPatientLevelPrediction::setMultiLayerPerceptron() trains through runPlp() on the bridged data (torch, CPU)
#   * the trained model ranks the held-out test set better than chance (AUC > 0.6) because the fixture has a real signal
#
# No Athena vocabulary is needed. Needs: DeepPatientLevelPrediction, PatientLevelPrediction, FeatureExtraction, Andromeda,
# reticulate with a Python environment that has torch and polars, omopduckdb.
#   Rscript scripts/ohdsi_parity_deepplp.R        (exit status 1 on any mismatch)
# Python torch must be imported before duckdb / HADES load their native libraries; on Windows the other order fails with
# "WinError 1114: a DLL initialization routine failed" (c10.dll).
invisible(reticulate::import("torch"))
suppressMessages({library(omopduckdb); library(PatientLevelPrediction); library(DeepPatientLevelPrediction); library(Andromeda)})
set.seed(20261113)
# DeepPLP >= 2.0 trains with Python torch through reticulate (set RETICULATE_PYTHON to an environment with torch and polars).

db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
cond_ids <- 100001:100030                       # 30 condition concepts; the first 5 raise the outcome risk
DBI::dbExecute(con, sprintf("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
  SELECT id, 'Condition ' || id, 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'C' || id, DATE '1970-01-01', DATE '2099-12-31' FROM (SELECT unnest([%s]) AS id)",
  paste(cond_ids, collapse = ",")))

n <- 4000L
duckdb::duckdb_register(con, "_p", data.frame(person_id = seq_len(n), gender_concept_id = sample(c(8507L, 8532L), n, TRUE),
                                              year_of_birth = sample(1940:1990, n, TRUE), race_concept_id = 0L, ethnicity_concept_id = 0L))
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) SELECT * FROM _p")
DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, person_id, DATE '2015-01-01', DATE '2023-12-31', 32817 FROM _p")

index <- as.Date("2021-06-01")
has <- matrix(rbinom(n * length(cond_ids), 1, 0.12), n, length(cond_ids))           # who has which condition before index
has[rowSums(has) == 0, 30] <- 1L                                                # every patient has a covariate (note 1)
lp <- -1.6 + has[, 1:5] %*% rep(1.1, 5)
y <- rbinom(n, 1, plogis(lp))
ij <- which(has == 1, arr.ind = TRUE)
cond <- data.frame(person_id = ij[, 1], concept_id = cond_ids[ij[, 2]], d = index - sample(10:300, nrow(ij), TRUE))
duckdb::duckdb_register(con, "_c", cond)
DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_start_datetime, condition_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817 FROM _c")

parquet <- tempfile(fileext = ".parquet")
duckdb::duckdb_register(con, "_cohort", data.frame(subject_id = seq_len(n), cohort_start_date = index, outcome_flag = y))
DBI::dbExecute(con, sprintf("COPY (SELECT * FROM _cohort) TO '%s' (FORMAT PARQUET)", gsub("\\\\", "/", parquet)))

res <- extract_sparse_concept_matrix(con, parquet, domains = "condition", value = "binary")
plp <- as_plp_data(res, con, target_id = 1, outcome_id = 2)
DBI::dbDisconnect(con, shutdown = TRUE)

pop_settings <- createStudyPopulationSettings(riskWindowStart = 1, riskWindowEnd = 365, requireTimeAtRisk = FALSE,
                                              firstExposureOnly = FALSE, washoutPeriod = 0, removeSubjectsWithPriorOutcome = FALSE)
pop <- createStudyPopulation(plpData = plp, outcomeId = 2, populationSettings = pop_settings)
ref <- as.data.frame(plp$covariateData$covariateRef)

out_dir <- tempfile("deepplp_"); dir.create(out_dir)
mlp <- setMultiLayerPerceptron(numLayers = 2, sizeHidden = 32, dropout = 0.2, sizeEmbedding = 16, estimatorSettings =
                                 setEstimator(learningRate = 1e-3, batchSize = 256, epochs = 12, device = "cpu", seed = 1),
                               hyperParamSearch = "random", randomSample = 1, randomSampleSeed = 1)
fit <- suppressMessages(runPlp(plpData = plp, outcomeId = 2, analysisId = "deepplp_parity", analysisName = "bridge",
                               populationSettings = pop_settings, splitSettings = createDefaultSplitSetting(testFraction = 0.25, nfold = 3, splitSeed = 1),
                               sampleSettings = createSampleSettings(), featureEngineeringSettings = createFeatureEngineeringSettings(),
                               preprocessSettings = createPreprocessSettings(minFraction = 0, normalize = FALSE, removeRedundancy = FALSE),
                               modelSettings = mlp, logSettings = createLogSettings(verbosity = "ERROR"), saveDirectory = out_dir))
stats_df <- fit$performanceEvaluation$evaluationStatistics
auc_test <- stats_df
auc_test <- as.numeric(auc_test$value[auc_test$evaluation == "Test" & auc_test$metric == "AUROC"])

checks <- c(
  `study population outcomes == labels` = sum(pop$outcomeCount) == sum(y),
  `covariate ids follow concept_id * 1000 + analysis_id` = all(ref$covariateId %in% (cond_ids * 1000 + 102)) && nrow(ref) == length(cond_ids),
  `DeepPLP MLP trained through runPlp on the bridged plpData` = !is.null(fit$model) && length(auc_test) == 1 && is.finite(auc_test),
  `test AUROC > 0.6 on a fixture with a real signal` = isTRUE(auc_test > 0.6)
)
cat(sprintf("DeepPLP MLP test AUROC %.3f (outcome prevalence %.3f)\n", auc_test, mean(y)))
print(data.frame(check = names(checks), ok = unname(checks)), row.names = FALSE)
if (!all(checks)) quit(status = 1)
cat("All DeepPatientLevelPrediction parity checks passed.\n")

# Notes
# 1. With ~2% of patients having no covariate at all, DeepPLP 2.4.0 stopped with "index 3000 is out of bounds for dimension 0 with
#    size 2924" on this bridged data. Whether native FeatureExtraction data behaves the same was not checked, so the fixture gives
#    every patient at least one covariate and this limitation is recorded in OHDSI_INTEGRATION.md rather than hidden.
# 2. Python torch must be importable by reticulate (RETICULATE_PYTHON); a Windows venv with torch 2.14 (CPU) and polars was used.
