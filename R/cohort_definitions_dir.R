#' The folder with the shipped cohort definitions
#'
#' The package ships its Circe definitions (`*.circe.json`) and the portable OHDSI SQL (`target_cohort.sql`, `outcome_cohort.sql`) in
#' `inst/cohorts`, so they are available in an installed package as well as in a source checkout.
#'
#' @return The path of the folder (`system.file("cohorts", package = "omopduckdb")`, or `inst/cohorts` of a source checkout).
#' @examples
#' list.files(cohort_definitions_dir(), pattern = "circe[.]json$")
#' @export
cohort_definitions_dir <- function() {
  p <- system.file("cohorts", package = "omopduckdb")
  if (nzchar(p) && dir.exists(p)) return(p)
  file.path(getwd(), "inst", "cohorts")
}
