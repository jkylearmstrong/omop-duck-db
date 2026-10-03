#' Sparse concept matrix as tidymodels-ready data
#'
#' Turns an [extract_sparse_concept_matrix()] result into a tibble that works with tidymodels
#' (rsample, recipes, parsnip, workflows, tune, yardstick):
#'
#' * `y`: the outcome as a factor with the **event as the first level**, which is what yardstick and
#'   tidymodels assume (`event_level = "first"`). Omitted if the cohort has no outcome (e.g. when preparing
#'   new patients for scoring).
#' * `person_id`, `index_date`: identifiers. Give them the `"id"` role in a recipe, and resample with
#'   `rsample::group_vfold_cv(data, group = person_id)` / `group_initial_split()`: a patient can have several
#'   index events, and ordinary row-wise resampling would put one patient in both training and assessment
#'   data (leakage).
#' * one numeric predictor per concept, named `<domain>_<concept_id>` (for example `condition_201826`):
#'   syntactically valid, stable across cohorts, and the same names [materialize_concept_features()] creates
#'   in DuckDB, so a model trained here can be scored there.
#'
#' With the sparsevctrs package installed (and `sparse = TRUE`) the predictors are sparse columns, so
#' high-dimensional concept matrices stay memory-light and tidymodels can hand them to sparse-aware engines
#' such as glmnet. Otherwise they are dense.
#'
#' @param sparse_result A result of [extract_sparse_concept_matrix()].
#' @param event,non_event Labels for the outcome factor (event first).
#' @param sparse Use sparse predictor columns when sparsevctrs is available (default `TRUE`).
#' @return A tibble. The attribute `"concepts"` maps each predictor name (`feature`) to its token, domain,
#'   concept id and concept name.
#' @examples
#' \dontrun{
#' res <- extract_sparse_concept_matrix(con, "cohort.parquet", min_patient_freq = 50, value = "binary")
#' dat <- as_tidymodels_data(res)
#' split <- rsample::group_initial_split(dat, group = person_id)
#' rec <- recipes::recipe(y ~ ., data = rsample::training(split)) |>
#'   recipes::update_role(person_id, index_date, new_role = "id")
#' spec <- parsnip::logistic_reg(penalty = 0.01, mixture = 1) |> parsnip::set_engine("glmnet")
#' fit <- workflows::workflow(rec, spec) |> parsnip::fit(rsample::training(split))
#' }
#' @export
as_tidymodels_data <- function(sparse_result, event = "yes", non_event = "no", sparse = TRUE) {
  if (!requireNamespace("tibble", quietly = TRUE)) stop("as_tidymodels_data() needs the 'tibble' package.", call. = FALSE)
  if (!requireNamespace("Matrix", quietly = TRUE)) stop("as_tidymodels_data() needs the 'Matrix' package.", call. = FALSE)
  X <- sparse_result$X
  concepts <- sparse_result$concepts
  feature <- .mlf_feature_name(concepts$domain, concepts$concept_id)
  if (anyDuplicated(feature)) stop("Duplicate predictor names: a concept appears twice in the vocabulary.", call. = FALSE)
  colnames(X) <- feature

  predictors <- if (isTRUE(sparse) && requireNamespace("sparsevctrs", quietly = TRUE)) {
    sparsevctrs::coerce_to_sparse_tibble(X)
  } else {
    if (isTRUE(sparse)) message("sparsevctrs is not installed; returning dense predictors.")
    tibble::as_tibble(as.matrix(X))
  }

  cohort <- sparse_result$cohort
  front <- tibble::tibble(person_id = cohort$person_id, index_date = cohort$index_date)
  if (!is.null(sparse_result$y)) {
    front$y <- factor(ifelse(as.numeric(sparse_result$y) == 1, event, non_event), levels = c(event, non_event))
  }
  out <- tibble::as_tibble(c(as.list(front), as.list(predictors)))
  attr(out, "concepts") <- data.frame(feature = feature, concepts[, c("token", "domain", "concept_id", "concept_name")],
                                      stringsAsFactors = FALSE)
  out
}

#' Concepts a fitted model actually uses
#'
#' Given a fitted parsnip model or workflow (for example a LASSO `glmnet` logistic regression), returns the
#' rows of `concepts` whose predictor has a non-zero coefficient. Use it to keep the in-database scoring table
#' narrow: pass the result to [materialize_concept_features()].
#'
#' @param fit A fitted parsnip model, or a fitted workflow.
#' @param concepts The `concepts` data.frame of an [extract_sparse_concept_matrix()] result (or the result
#'   itself).
#' @return The subset of `concepts` used by the model, with a `feature` column.
#' @export
model_concepts <- function(fit, concepts) {
  for (pkg in c("broom", "workflows")) {
    if (!requireNamespace(pkg, quietly = TRUE)) stop(sprintf("model_concepts() needs the '%s' package.", pkg), call. = FALSE)
  }
  if (!is.data.frame(concepts) && !is.null(concepts$concepts)) concepts <- concepts$concepts
  if (inherits(fit, "workflow")) fit <- workflows::extract_fit_parsnip(fit)
  coefs <- broom::tidy(fit)
  used <- coefs$term[!is.na(coefs$estimate) & coefs$estimate != 0]
  concepts$feature <- .mlf_feature_name(concepts$domain, concepts$concept_id)
  concepts[concepts$feature %in% used, , drop = FALSE]
}

#' Write concept features to a DuckDB table, for in-database scoring
#'
#' Builds the same per-row lookback-window features as [extract_sparse_concept_matrix()], but as a **wide
#' table inside DuckDB**: `row_id` (1-based parquet position), `person_id`, `index_date`, the outcome `y` if
#' the parquet has one, and one numeric column per requested concept, named `<domain>_<concept_id>` exactly as
#' in [as_tidymodels_data()]. That is the table to point a scoring query at: a model trained in tidymodels can
#' be converted to SQL with `orbital::orbital()` (workflows) or `tidypredict::tidypredict_sql()` (glm and
#' friends) and evaluated inside DuckDB on this table, on the training cohort or on any new cohort parquet,
#' with exactly the same window rules, so training and scoring features cannot drift apart.
#'
#' The SQL has one expression per concept, so request only what the model needs ([model_concepts()]).
#'
#' @inheritParams extract_sparse_concept_matrix
#' @param concepts A data.frame with `domain` and `concept_id` columns: typically `res$concepts`, or the
#'   subset returned by [model_concepts()].
#' @param table Name of the table to create (replaced if it exists).
#' @param temporary Create a temporary table (default `FALSE`).
#' @return Invisibly, a list with `table`, `features` (column names created) and `n_rows`.
#' @examples
#' \dontrun{
#' used <- model_concepts(fit, res$concepts)
#' orb <- orbital::orbital(fit)
#' scores <- orbital::augment(orb, dplyr::tbl(con, "to_score")) |> dplyr::collect()
#' }
#' @export
materialize_concept_features <- function(con,
                                         features_parquet,
                                         concepts,
                                         table = "ml_concept_features",
                                         lookback_days = 365,
                                         value = "count",
                                         include_index_date = FALSE,
                                         person_col = NULL,
                                         index_date_col = NULL,
                                         outcome_col = NULL,
                                         temporary = FALSE) {
  if (!is.data.frame(concepts) && !is.null(concepts$concepts)) concepts <- concepts$concepts
  if (!all(c("domain", "concept_id") %in% names(concepts))) {
    stop("`concepts` needs `domain` and `concept_id` columns (e.g. res$concepts).", call. = FALSE)
  }
  if (!value %in% c("count", "binary")) stop("value must be 'count' or 'binary'", call. = FALSE)
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){0,2}$", table)) {
    stop(sprintf("Invalid table identifier: %s", table), call. = FALSE)
  }
  if (nrow(concepts) == 0) stop("`concepts` is empty: nothing to materialise.", call. = FALSE)
  domains <- .mlf_validate_options(unique(concepts$domain), lookback_days, 1)
  concepts <- concepts[!duplicated(.mlf_pair_key(concepts$domain, concepts$concept_id)), c("domain", "concept_id")]

  cohort <- .mlf_load_cohort(con, features_parquet, person_col, index_date_col, outcome_col)
  feature <- .mlf_feature_name(concepts$domain, concepts$concept_id)
  agg <- if (value == "count") "SUM(a.n)" else "MAX(1)"
  cells <- sprintf("CAST(COALESCE(%s FILTER (WHERE a.domain = '%s' AND a.concept_id = %.0f), 0) AS DOUBLE) AS %s",
                   agg, concepts$domain, as.numeric(concepts$concept_id), vapply(feature, .mlf_ident, character(1)))
  has_y <- "y" %in% names(cohort$df)

  sql <- sprintf(paste0(
    "CREATE OR REPLACE %s TABLE %s AS %s, agg AS (SELECT row_idx, domain, concept_id, COUNT(*) AS n FROM ev ",
    "GROUP BY row_idx, domain, concept_id) ",
    "SELECT CAST(c.row_idx + 1 AS BIGINT) AS row_id, c.person_id, c.index_date%s, %s ",
    "FROM cohort c LEFT JOIN agg a ON a.row_idx = c.row_idx ",
    "GROUP BY c.row_idx, c.person_id, c.index_date%s ORDER BY c.row_idx"),
    if (isTRUE(temporary)) "TEMPORARY" else "", table,
    .mlf_events_ctes(cohort$sql, domains, lookback_days, include_index_date, 1, concepts),
    if (has_y) ", c.y" else "", paste(cells, collapse = ", "), if (has_y) ", c.y" else "")
  DBI::dbExecute(con, sql)
  invisible(list(table = table, features = feature, n_rows = nrow(cohort$df)))
}
