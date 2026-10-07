#' Native CIRCE / Atlas JSON-to-DuckDB Vectorized SQL Compiler
#'
#' Parses OHDSI CIRCE cohort definition JSON directly into vectorized DuckDB SQL
#' CTEs without requiring Java, rJava, CirceR, or SqlRender.
#'
#' @docType class
#' @name circe
NULL

.DOMAIN_TABLE_MAP <- list(
  ConditionOccurrence = list(
    table = "condition_occurrence",
    concept_col = "condition_concept_id",
    date_col = "condition_start_date",
    end_date_col = "COALESCE(condition_end_date, condition_start_date)"
  ),
  DrugExposure = list(
    table = "drug_exposure",
    concept_col = "drug_concept_id",
    date_col = "drug_exposure_start_date",
    end_date_col = "COALESCE(drug_exposure_end_date, drug_exposure_start_date)"
  ),
  ProcedureOccurrence = list(
    table = "procedure_occurrence",
    concept_col = "procedure_concept_id",
    date_col = "procedure_date",
    end_date_col = "procedure_date"
  ),
  VisitOccurrence = list(
    table = "visit_occurrence",
    concept_col = "visit_concept_id",
    date_col = "visit_start_date",
    end_date_col = "visit_end_date"
  ),
  Measurement = list(
    table = "measurement",
    concept_col = "measurement_concept_id",
    date_col = "measurement_date",
    end_date_col = "measurement_date"
  )
)

#' Compile CIRCE JSON to Vectorized DuckDB SQL
#'
#' @param circe_json JSON character string, parsed list, or file path.
#' @param target_cohort_id Target cohort definition id (default 1).
#' @param cdm_schema CDM schema name (default `"main"`).
#' @return SQL character string.
#' @export
compile_circe_to_duckdb <- function(circe_json,
                                    target_cohort_id = 1L,
                                    cdm_schema = "main") {
  data <- if (is.character(circe_json) && file.exists(circe_json)) {
    jsonlite::fromJSON(circe_json, simplifyVector = FALSE)
  } else if (is.character(circe_json)) {
    jsonlite::fromJSON(circe_json, simplifyVector = FALSE)
  } else if (is.list(circe_json)) {
    circe_json
  } else {
    stop("`circe_json` must be a JSON string, list, or file path.", call. = FALSE)
  }

  # Concept Sets
  cs_list <- data$ConceptSets %||% list()
  cs_unions <- character(0)
  for (cs in cs_list) {
    cs_id <- cs$id %||% 0L
    items <- cs$expression$items %||% list()
    for (item in items) {
      cid <- item$concept$CONCEPT_ID %||% item$concept$concept_id %||% 0L
      desc <- isTRUE(item$includeDescendants)
      excl <- isTRUE(item$isExcluded)
      if (excl) next

      if (desc) {
        cs_unions <- c(cs_unions, sprintf("SELECT %d AS codeset_id, descendant_concept_id AS concept_id FROM %s.concept_ancestor WHERE ancestor_concept_id = %d",
                                          as.integer(cs_id), cdm_schema, as.integer(cid)))
      } else {
        cs_unions <- c(cs_unions, sprintf("SELECT %d AS codeset_id, %d AS concept_id", as.integer(cs_id), as.integer(cid)))
      }
    }
  }

  cs_sql <- if (length(cs_unions) > 0) paste(cs_unions, collapse = " UNION ALL\n    ") else "SELECT 0 AS codeset_id, 0 AS concept_id WHERE 1=0"

  # Primary Criteria
  primary_crit <- data$PrimaryCriteria %||% list()
  crit_list <- primary_crit$CriteriaList %||% list()
  obs_win <- primary_crit$ObservationWindow %||% list()
  prior_days <- as.integer(obs_win$PriorDays %||% 0L)
  post_days <- as.integer(obs_win$PostDays %||% 0L)

  primary_selects <- character(0)
  for (crit in crit_list) {
    for (dom_key in names(.DOMAIN_TABLE_MAP)) {
      if (!is.null(crit[[dom_key]])) {
        c_item <- crit[[dom_key]]
        codeset_id <- c_item$CodesetId
        cfg <- .DOMAIN_TABLE_MAP[[dom_key]]

        cs_join <- if (!is.null(codeset_id)) {
          sprintf("JOIN _cs_resolved cs ON e.%s = cs.concept_id AND cs.codeset_id = %d", cfg$concept_col, as.integer(codeset_id))
        } else ""

        primary_selects <- c(primary_selects, sprintf("
          SELECT
            e.person_id AS subject_id,
            CAST(e.%s AS DATE) AS event_start_date,
            CAST(%s AS DATE) AS event_end_date,
            '%s' AS criteria_type
          FROM %s.%s e
          %s
        ", cfg$date_col, cfg$end_date_col, dom_key, cdm_schema, cfg$table, cs_join))
      }
    }
  }

  if (length(primary_selects) == 0) {
    primary_selects <- sprintf("
      SELECT
        person_id AS subject_id,
        visit_start_date AS event_start_date,
        visit_end_date AS event_end_date,
        'VisitOccurrence' AS criteria_type
      FROM %s.visit_occurrence
    ", cdm_schema)
  }

  primary_union <- paste(primary_selects, collapse = " UNION ALL\n")

  obs_filter <- if (prior_days > 0 || post_days > 0) {
    sprintf("
      JOIN %s.observation_period op
        ON op.person_id = p.subject_id
       AND op.observation_period_start_date <= p.event_start_date - INTERVAL '%d' DAY
       AND op.observation_period_end_date >= p.event_start_date + INTERVAL '%d' DAY
    ", cdm_schema, prior_days, post_days)
  } else ""

  limit_rule <- tolower(primary_crit$PrimaryCriteriaLimit$Type %||% "all")
  order_clause <- if (limit_rule == "first") {
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY p.subject_id ORDER BY p.event_start_date ASC) = 1"
  } else ""

  sql <- sprintf("
    WITH _cs_resolved AS (
        %s
    ),
    _primary_events_raw AS (
        %s
    ),
    _primary_qualified AS (
        SELECT
            p.subject_id,
            p.event_start_date,
            p.event_end_date
        FROM _primary_events_raw p
        %s
        %s
    )
    SELECT
        %d AS cohort_definition_id,
        subject_id,
        event_start_date AS cohort_start_date,
        event_end_date AS cohort_end_date
    FROM _primary_qualified
    ORDER BY subject_id, cohort_start_date;
  ", cs_sql, primary_union, obs_filter, order_clause, as.integer(target_cohort_id))

  trimws(sql)
}

#' Execute CIRCE JSON Cohort Directly in DuckDB
#'
#' @param con Active DuckDB connection (DBI::dbConnect).
#' @param circe_json CIRCE JSON string, list, or file path.
#' @param target_cohort_id Target cohort definition id (default 1).
#' @param cohort_table Target cohort table name (default `"cohort"`).
#' @param cdm_schema Schema holding CDM tables (default `"main"`).
#' @return A `data.frame` of resulting cohort records.
#' @export
execute_circe_cohort <- function(con,
                                 circe_json,
                                 target_cohort_id = 1L,
                                 cohort_table = "cohort",
                                 cdm_schema = "main") {
  if (!inherits(con, "duckdb_connection")) stop("`con` must be a DuckDB connection.", call. = FALSE)
  sql <- compile_circe_to_duckdb(circe_json, target_cohort_id = target_cohort_id, cdm_schema = cdm_schema)
  DBI::dbExecute(con, sprintf("CREATE TABLE IF NOT EXISTS %s (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE);", cohort_table))
  DBI::dbExecute(con, sprintf("DELETE FROM %s WHERE cohort_definition_id = %d;", cohort_table, as.integer(target_cohort_id)))
  DBI::dbExecute(con, sprintf("INSERT INTO %s %s;", cohort_table, sql))
  DBI::dbGetQuery(con, sprintf("SELECT * FROM %s WHERE cohort_definition_id = %d;", cohort_table, as.integer(target_cohort_id)))
}
