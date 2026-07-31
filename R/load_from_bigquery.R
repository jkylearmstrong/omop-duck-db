#' Copy OMOP-shaped table(s) from BigQuery into the local DuckDB CDM database
#'
#' Two uses:
#' 1. Vocab refresh: point `dataset` at a live/public OMOP vocab source and
#'    `tables` at `concept`, `concept_ancestor`, `concept_relationship`, etc.
#'    as an alternative to [load_vocabulary()]'s Athena-zip path.
#' 2. Demo/test data: point `dataset` at
#'    `bigquery-public-data.cms_synthetic_patient_data_omop` and `tables` at
#'    any CDM clinical tables to populate a working demo database without
#'    waiting on real PCORnet extracts.
#'
#' First run needs an interactive R session (not `Rscript`) to complete the
#' `bigrquery`/`gargle` browser OAuth flow once via `bigrquery::bq_auth()` --
#' subsequent non-interactive runs reuse the cached token. `billing_project`
#' is required even for public datasets -- BigQuery needs a project to
#' attribute query cost/quota to.
#'
#' @param billing_project Your own GCP project ID, used for query billing/quota.
#'   Defaults to `billing_project` in `config/bigquery.yml`.
#' @param dataset `"source_project.dataset_id"`, e.g.
#'   `"bigquery-public-data.cms_synthetic_patient_data_omop"`. Defaults to
#'   `dataset` in `config/bigquery.yml`.
#' @param tables Character vector of table names to copy.
#' @param db_path Path to the DuckDB database file.
#' @param mode `"overwrite"` deletes existing rows in the target table before
#'   loading; `"append"` adds to them.
#' @return Invisibly, `db_path`.
#' @export
load_from_bigquery <- function(billing_project = NULL,
                                dataset = NULL,
                                tables,
                                db_path = "omop_cdm.duckdb",
                                mode = c("overwrite", "append")) {
  mode <- match.arg(mode)
  cfg <- read_local_config("bigquery.yml")

  billing_project <- billing_project %||% cfg$billing_project
  dataset <- dataset %||% cfg$dataset

  if (is.null(billing_project) || billing_project == "REPLACE_WITH_YOUR_GCP_PROJECT_ID") {
    stop("Provide billing_project or set it in config/bigquery.yml")
  }
  if (is.null(dataset)) {
    stop("Provide dataset (e.g. bigquery-public-data.cms_synthetic_patient_data_omop) or set it in config/bigquery.yml")
  }

  dataset_parts <- strsplit(dataset, "\\.")[[1]]
  if (length(dataset_parts) != 2) {
    stop("dataset must be 'source_project.dataset_id', e.g. bigquery-public-data.cms_synthetic_patient_data_omop")
  }
  source_project <- dataset_parts[[1]]
  dataset_id <- dataset_parts[[2]]

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  existing_tables <- DBI::dbListTables(con)

  for (tbl in tables) {
    cat("Downloading", paste(source_project, dataset_id, tbl, sep = "."), "(billed to", billing_project, ") ...\n")
    bq_tbl <- bigrquery::bq_table(project = source_project, dataset = dataset_id, table = tbl)
    df <- bigrquery::bq_table_download(bq_tbl, billing_project = billing_project)
    cat("  ->", nrow(df), "rows fetched from BigQuery\n")

    if (tbl %in% existing_tables) {
      if (mode == "overwrite") {
        DBI::dbExecute(con, paste0("DELETE FROM ", tbl))
      }
      DBI::dbAppendTable(con, tbl, df)
    } else {
      DBI::dbWriteTable(con, tbl, df, overwrite = TRUE)
    }
    cat("  -> loaded into local table", tbl, "\n")
  }

  cat("BigQuery load complete.\n")
  invisible(db_path)
}
