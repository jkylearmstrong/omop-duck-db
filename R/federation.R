#' Create a Zero-Copy Federated Consortium Connection
#'
#' Attaches multiple site DuckDB databases in read-only mode and generates unified
#' `v_*` views (e.g. `v_person`, `v_visit_occurrence`) with `site_id` and `site_anon`
#' provenance columns. Optionally attaches a central Athena vocabulary.
#'
#' @param site_dbs Named character vector or list mapping site labels/pseudonyms
#'   (e.g. `c("Site A" = "site1.duckdb", "Site B" = "site2.duckdb")`) to DuckDB database paths.
#' @param central_vocab_db Optional path to an Athena vocabulary DuckDB database.
#' @param output_con Optional DuckDB connection. If `NULL`, creates a new in-memory connection.
#' @return DuckDB connection with attached site databases and unified `v_*` views.
#' @export
create_federated_consortium <- function(site_dbs, central_vocab_db = NULL, output_con = NULL) {
  if (length(site_dbs) == 0) {
    stop("site_dbs cannot be empty.")
  }

  con <- if (!is.null(output_con)) output_con else DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")

  federated_tables <- c(
    "person", "visit_occurrence", "condition_occurrence", "procedure_occurrence",
    "drug_exposure", "measurement", "observation", "death", "observation_period",
    "drug_era", "condition_era", "provider", "care_site", "cdm_source"
  )

  site_names <- names(site_dbs)
  if (is.null(site_names)) {
    site_names <- as.character(seq_along(site_dbs))
  }

  attached_sites <- list()

  for (i in seq_along(site_dbs)) {
    db_path <- as.character(site_dbs[[i]])
    if (!file.exists(db_path)) {
      stop("Site database not found: ", db_path)
    }

    norm_path <- gsub("\\\\", "/", normalizePath(db_path, mustWork = TRUE))
    schema_alias <- sprintf("site_%d", i)
    DBI::dbExecute(con, sprintf("ATTACH '%s' AS %s (READ_ONLY);", norm_path, schema_alias))

    site_anon <- site_names[i]
    if (is.na(site_anon) || site_anon == "") {
      site_anon <- sprintf("Site %d", i)
    }

    # Resolve site_id
    site_id <- suppressWarnings(as.integer(site_anon))
    if (is.na(site_id)) {
      # Try care_site or person
      care_site_res <- tryCatch(
        DBI::dbGetQuery(con, sprintf("SELECT MIN(care_site_id) AS id FROM %s.care_site WHERE care_site_id IS NOT NULL", schema_alias)),
        error = function(e) NULL
      )
      if (!is.null(care_site_res) && nrow(care_site_res) > 0 && !is.na(care_site_res$id[1])) {
        site_id <- as.integer(care_site_res$id[1])
      } else {
        person_res <- tryCatch(
          DBI::dbGetQuery(con, sprintf("SELECT MIN(care_site_id) AS id FROM %s.person WHERE care_site_id IS NOT NULL", schema_alias)),
          error = function(e) NULL
        )
        if (!is.null(person_res) && nrow(person_res) > 0 && !is.na(person_res$id[1])) {
          site_id <- as.integer(person_res$id[1])
        } else {
          site_id <- i
        }
      }
    }

    attached_sites[[length(attached_sites) + 1]] <- list(
      schema = schema_alias,
      site_id = site_id,
      site_anon = site_anon
    )
  }

  # Attach central vocabulary if provided
  if (!is.null(central_vocab_db)) {
    attach_central_vocabulary(con, central_vocab_db, temporary = FALSE)
  }

  # Build federated views for each CDM table
  for (tbl in federated_tables) {
    first_schema <- NULL
    for (s in attached_sites) {
      has_tbl <- DBI::dbGetQuery(
        con,
        sprintf("SELECT 1 FROM information_schema.tables WHERE table_catalog = '%s' AND table_name = '%s'", s$schema, tbl)
      )
      if (nrow(has_tbl) > 0) {
        if (is.null(first_schema)) {
          first_schema <- s$schema
        }
      }
    }

    if (is.null(first_schema)) {
      next
    }

    cols_df <- DBI::dbGetQuery(
      con,
      sprintf("SELECT column_name FROM information_schema.columns WHERE table_catalog = '%s' AND table_name = '%s' ORDER BY ordinal_position", first_schema, tbl)
    )
    cols_sql <- paste(sprintf('t."%s"', cols_df$column_name), collapse = ", ")

    site_selects <- c()
    for (s in attached_sites) {
      has_tbl <- DBI::dbGetQuery(
        con,
        sprintf("SELECT 1 FROM information_schema.tables WHERE table_catalog = '%s' AND table_name = '%s'", s$schema, tbl)
      )
      if (nrow(has_tbl) > 0) {
        s_anon_esc <- gsub("'", "''", s$site_anon)
        site_selects <- c(
          site_selects,
          sprintf("SELECT %d AS site_id, '%s' AS site_anon, %s FROM %s.%s t", s$site_id, s_anon_esc, cols_sql, s$schema, tbl)
        )
      }
    }

    if (length(site_selects) > 0) {
      union_sql <- paste(site_selects, collapse = " UNION ALL\n")
      view_sql <- sprintf("CREATE OR REPLACE VIEW v_%s AS\n%s;", tbl, union_sql)
      DBI::dbExecute(con, view_sql)
    }
  }

  invisible(con)
}
