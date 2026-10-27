#' Build a CohortGenerator-ready `cohortDefinitionSet` from the shipped eDERRI cohorts (FR-D3)
#'
#' Reads `target_cohort.sql`, `outcome_cohort.sql` and their Circe JSON from the `cohorts/` directory of the
#' omop-duck-db repository and returns the data frame that `CohortGenerator::generateCohortSet()` takes:
#' one row per cohort with `cohortId`, `cohortName`, `sql` and `json`. The study parameters `@followup_days` and
#' `@min_age` are rendered into the SQL here, so only the four parameters CohortGenerator itself fills in
#' (`@cdm_database_schema`, `@target_database_schema`, `@target_cohort_table`, `@target_cohort_id`) remain.
#'
#' The SQL is the authoritative definition. The Circe JSON is hand-written, has not been imported into ATLAS and
#' cannot express the discharge-disposition exclusion; CohortGenerator uses the `sql` column to generate cohorts.
#'
#' @param cohorts_dir Directory holding `target_cohort.sql`, `outcome_cohort.sql` and the `*.circe.json` files.
#' @param target_cohort_id,outcome_cohort_id Cohort ids to assign (default 1 and 2).
#' @param followup_days Verified follow-up after discharge in days (default 30).
#' @param min_age Minimum age in years at discharge (default 18).
#' @return A data frame with columns `cohortId`, `cohortName`, `sql`, `json`.
#' @export
cohort_definition_set <- function(cohorts_dir = "cohorts",
                                  target_cohort_id = 1L,
                                  outcome_cohort_id = 2L,
                                  followup_days = 30L,
                                  min_age = 18L) {
  whole <- function(x, nm) {
    if (length(x) != 1L || is.na(x) || x != round(x) || x < 0) stop(sprintf("`%s` must be a non-negative whole number.", nm), call. = FALSE)
    as.integer(x)
  }
  target_cohort_id <- whole(target_cohort_id, "target_cohort_id")
  outcome_cohort_id <- whole(outcome_cohort_id, "outcome_cohort_id")
  followup_days <- whole(followup_days, "followup_days")
  min_age <- whole(min_age, "min_age")
  if (target_cohort_id == outcome_cohort_id) stop("`target_cohort_id` and `outcome_cohort_id` must differ.", call. = FALSE)

  read_file <- function(name) {
    path <- file.path(cohorts_dir, name)
    if (!file.exists(path)) stop(sprintf("Cohort file not found: %s", path), call. = FALSE)
    paste(readLines(path, warn = FALSE, encoding = "UTF-8"), collapse = "\n")
  }
  render <- function(sql) {
    sql <- gsub("@followup_days", as.character(followup_days), sql, fixed = TRUE)
    gsub("@min_age", as.character(min_age), sql, fixed = TRUE)
  }

  data.frame(
    cohortId = c(target_cohort_id, outcome_cohort_id),
    cohortName = c("eDERRI target: adult IP / ED-to-IP index stay", "eDERRI outcome: inpatient admission"),
    sql = c(render(read_file("target_cohort.sql")), render(read_file("outcome_cohort.sql"))),
    json = c(read_file("target_cohort.circe.json"), read_file("outcome_cohort.circe.json")),
    stringsAsFactors = FALSE
  )
}
