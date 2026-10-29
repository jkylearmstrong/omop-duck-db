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

.parse_op <- function(op_str) {
  op_clean <- tolower(trimws(as.character(op_str)))
  mapping <- list(
    "gte" = ">=", ">=" = ">=",
    "gt" = ">", ">" = ">",
    "lte" = "<=", "<=" = "<=",
    "lt" = "<", "<" = "<",
    "eq" = "=", "=" = "=", "==" = "=",
    "neq" = "!=", "!=" = "!="
  )
  res <- mapping[[op_clean]]
  if (is.null(res)) "=" else res
}

.compile_numeric_filter <- function(col_expr, filter_dict) {
  if (!is.list(filter_dict)) {
    val_num <- suppressWarnings(as.numeric(filter_dict))
    if (!is.na(val_num)) {
      return(sprintf("%s = %s", col_expr, val_num))
    }
    return(sprintf("%s = '%s'", col_expr, as.character(filter_dict)))
  }
  op <- .parse_op(filter_dict$Op %||% "eq")
  if (op == "between" || !is.null(filter_dict$Extent)) {
    val <- as.numeric(filter_dict$Value %||% 0)
    ext <- as.numeric(filter_dict$Extent %||% val)
    return(sprintf("%s BETWEEN %s AND %s", col_expr, val, ext))
  }
  if (!is.null(filter_dict$Value)) {
    val <- as.numeric(filter_dict$Value)
    return(sprintf("%s %s %s", col_expr, op, val))
  }
  "1=1"
}

.compile_window_bound <- function(bound_dict, index_date = "p.event_start_date") {
  if (is.null(bound_dict) || !is.list(bound_dict)) return(NULL)
  days <- bound_dict$Days
  if (is.null(days)) return(NULL)
  coeff <- as.integer(bound_dict$Coeff %||% 1L)
  offset <- as.integer(days) * coeff
  if (offset == 0L) {
    index_date
  } else if (offset > 0L) {
    sprintf("%s + INTERVAL '%d' DAY", index_date, offset)
  } else {
    sprintf("%s - INTERVAL '%d' DAY", index_date, abs(offset))
  }
}

.compile_criteria <- function(crit_wrapper, cdm_schema = "main") {
  crit <- crit_wrapper$Criteria %||% crit_wrapper
  start_win <- crit_wrapper$StartWindow %||% list()
  occ <- crit_wrapper$Occurrence %||% list()
  occ_type <- as.integer(occ$Type %||% 2L)
  occ_count <- as.integer(occ$Count %||% 1L)

  # 1. DemographicCriteria
  if (!is.null(crit$DemographicCriteria)) {
    demo <- crit$DemographicCriteria
    demo_conds <- "per.person_id = p.subject_id"
    if (!is.null(demo$Age)) {
      age_expr <- sprintf("date_diff('year', make_date(per.year_of_birth, COALESCE(per.month_of_birth, 1), COALESCE(per.day_of_birth, 1)), p.event_start_date)")
      demo_conds <- c(demo_conds, .compile_numeric_filter(age_expr, demo$Age))
    }
    if (!is.null(demo$Gender)) {
      g_items <- demo$Gender
      g_cids <- integer(0)
      if (is.list(g_items)) {
        for (g in g_items) {
          if (is.list(g)) {
            cid <- g$CONCEPT_ID %||% g$concept_id
            if (!is.null(cid)) g_cids <- c(g_cids, as.integer(cid))
          } else {
            g_cids <- c(g_cids, as.integer(g))
          }
        }
      } else {
        g_cids <- as.integer(g_items)
      }
      if (length(g_cids) > 0) {
        demo_conds <- c(demo_conds, sprintf("per.gender_concept_id IN (%s)", paste(g_cids, collapse = ", ")))
      }
    }
    subquery <- sprintf("SELECT 1 FROM %s.person per WHERE %s", cdm_schema, paste(demo_conds, collapse = " AND "))
    if ((occ_type == 0L && occ_count == 0L) || (occ_type == 1L && occ_count == 0L)) {
      return(sprintf("NOT EXISTS (%s)", subquery))
    }
    return(sprintf("EXISTS (%s)", subquery))
  }

  # 2. Clinical Domain Criteria
  for (domain_key in names(.DOMAIN_TABLE_MAP)) {
    if (!is.null(crit[[domain_key]])) {
      c_item <- crit[[domain_key]]
      cfg <- .DOMAIN_TABLE_MAP[[domain_key]]
      tbl <- cfg$table
      c_col <- cfg$concept_col
      d_col <- cfg$date_col

      conds <- "e.person_id = p.subject_id"
      codeset_id <- c_item$CodesetId
      if (!is.null(codeset_id)) {
        conds <- c(conds, sprintf("e.%s IN (SELECT concept_id FROM _cs_resolved WHERE codeset_id = %d)", c_col, as.integer(codeset_id)))
      }

      start_bound <- .compile_window_bound(start_win$Start)
      end_bound <- .compile_window_bound(start_win$End)
      if (!is.null(start_bound)) {
        conds <- c(conds, sprintf("CAST(e.%s AS DATE) >= %s", d_col, start_bound))
      }
      if (!is.null(end_bound)) {
        conds <- c(conds, sprintf("CAST(e.%s AS DATE) <= %s", d_col, end_bound))
      }

      if (!is.null(c_item$ValueAsNumber)) {
        conds <- c(conds, .compile_numeric_filter("e.value_as_number", c_item$ValueAsNumber))
      }

      where_clause <- paste(conds, collapse = " AND ")
      subquery <- sprintf("SELECT 1 FROM %s.%s e WHERE %s", cdm_schema, tbl, where_clause)

      if ((occ_type == 0L && occ_count == 0L) || (occ_type == 1L && occ_count == 0L)) {
        return(sprintf("NOT EXISTS (%s)", subquery))
      } else if (occ_type == 2L && occ_count <= 1L) {
        return(sprintf("EXISTS (%s)", subquery))
      } else if (occ_type == 2L) {
        return(sprintf("(SELECT COUNT(*) FROM %s.%s e WHERE %s) >= %d", cdm_schema, tbl, where_clause, occ_count))
      } else if (occ_type == 0L) {
        return(sprintf("(SELECT COUNT(*) FROM %s.%s e WHERE %s) = %d", cdm_schema, tbl, where_clause, occ_count))
      } else if (occ_type == 1L) {
        return(sprintf("(SELECT COUNT(*) FROM %s.%s e WHERE %s) <= %d", cdm_schema, tbl, where_clause, occ_count))
      }
    }
  }
  NULL
}

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
    cs_id <- as.integer(cs$id %||% 0L)
    items <- cs$expression$items %||% list()
    included <- character(0)
    excluded <- character(0)
    for (item in items) {
      cid <- as.integer(item$concept$CONCEPT_ID %||% item$concept$concept_id %||% 0L)
      # the concept itself, plus its descendants when includeDescendants (includeMapped is not supported)
      sel <- sprintf("SELECT %d AS concept_id", cid)
      if (isTRUE(item$includeDescendants)) {
        sel <- paste0(sel, sprintf(" UNION SELECT descendant_concept_id FROM %s.concept_ancestor WHERE ancestor_concept_id = %d",
                                   cdm_schema, cid))
      }
      if (isTRUE(item$isExcluded)) excluded <- c(excluded, sel) else included <- c(included, sel)
    }
    if (!length(included)) next
    body <- paste(included, collapse = " UNION ")
    if (length(excluded)) {
      # ATLAS semantics: excluded items (and their descendants) are removed from the set
      body <- sprintf("SELECT concept_id FROM (%s) WHERE concept_id NOT IN (%s)", body, paste(excluded, collapse = " UNION "))
    }
    cs_unions <- c(cs_unions, sprintf("SELECT %d AS codeset_id, concept_id FROM (%s)", cs_id, body))
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

        # a measurement entry event can carry a value filter (e.g. HbA1c >= 6.5)
        value_where <- if (!is.null(c_item$ValueAsNumber)) {
          paste("WHERE", .compile_numeric_filter("e.value_as_number", c_item$ValueAsNumber))
        } else ""

        primary_selects <- c(primary_selects, sprintf("
          SELECT
            e.person_id AS subject_id,
            CAST(e.%s AS DATE) AS event_start_date,
            CAST(%s AS DATE) AS event_end_date,
            '%s' AS criteria_type
          FROM %s.%s e
          %s
          %s
        ", cfg$date_col, cfg$end_date_col, dom_key, cdm_schema, cfg$table, cs_join, value_where))
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

  # 5. Inclusion Rules
  inclusion_rules <- data$InclusionRules %||% list()
  rule_clauses <- character(0)
  for (rule in inclusion_rules) {
    expr <- rule$expression %||% list()
    rule_type <- toupper(expr$Type %||% "ALL")
    crit_list_sub <- expr$CriteriaList %||% list()
    crit_clauses <- character(0)
    for (crit_wrapper in crit_list_sub) {
      c_sql <- .compile_criteria(crit_wrapper, cdm_schema = cdm_schema)
      if (!is.null(c_sql) && nzchar(c_sql)) {
        crit_clauses <- c(crit_clauses, c_sql)
      }
    }
    if (length(crit_clauses) > 0) {
      join_op <- if (rule_type == "ALL") " AND\n                " else " OR\n                "
      rule_clauses <- c(rule_clauses, sprintf("(\n                %s\n            )", paste(crit_clauses, collapse = join_op)))
    }
  }

  inc_filter <- if (length(rule_clauses) > 0) {
    paste0("WHERE ", paste(rule_clauses, collapse = " AND\n          "))
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
        %s
    )
    SELECT
        %d AS cohort_definition_id,
        subject_id,
        event_start_date AS cohort_start_date,
        event_end_date AS cohort_end_date
    FROM _primary_qualified
    ORDER BY subject_id, cohort_start_date;
  ", cs_sql, primary_union, obs_filter, inc_filter, order_clause, as.integer(target_cohort_id))

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

#' Export CohortFlow to OHDSI Circe JSON
#'
#' @description
#' Translates a `CohortFlow` computable phenotype definition into compliant
#' OHDSI CIRCE JSON specification for round-trip compatibility with OHDSI Atlas
#' and the OHDSI Phenotype Library.
#'
#' @param cohort_flow A `cohort_flow` object or list containing visit concepts and steps.
#' @param cohort_name Display name of the cohort (default `"Consortium Cohort"`).
#' @param cohort_description Optional narrative description.
#' @param output_path Optional file path to write JSON.
#'
#' @return A named list representing the CIRCE JSON structure.
#' @export
export_to_circe_json <- function(cohort_flow,
                                 cohort_name = "Consortium Cohort",
                                 cohort_description = NULL,
                                 output_path = NULL) {
  v_ids <- if (!is.null(cohort_flow$visit_concept_ids)) {
    as.integer(cohort_flow$visit_concept_ids)
  } else {
    c(9201L, 262L)
  }

  visit_items <- lapply(v_ids, function(cid) {
    list(
      concept = list(
        CONCEPT_ID = cid,
        CONCEPT_NAME = sprintf("Encounter Visit Concept %d", cid),
        STANDARD_CONCEPT = "S",
        STANDARD_CONCEPT_CAPTION = "Standard",
        INVALID_REASON = "V",
        INVALID_REASON_CAPTION = "Valid",
        CONCEPT_CODE = as.character(cid),
        DOMAIN_ID = "Visit",
        VOCABULARY_ID = "Visit",
        CONCEPT_CLASS_ID = "Visit"
      ),
      isExcluded = FALSE,
      includeDescendants = TRUE,
      includeMapped = FALSE
    )
  })

  concept_sets <- list(
    list(
      id = 0L,
      name = sprintf("%s - Index Visit Types", cohort_name),
      expression = list(items = visit_items)
    )
  )

  post_d <- if (!is.null(cohort_flow$follow_up_days)) as.integer(cohort_flow$follow_up_days) else 0L
  one_v <- if (!is.null(cohort_flow$one_visit_per_person)) isTRUE(cohort_flow$one_visit_per_person) else FALSE

  primary_criteria <- list(
    CriteriaList = list(
      list(
        VisitOccurrence = list(
          CodesetId = 0L
        )
      )
    ),
    ObservationWindow = list(
      PriorDays = 0L,
      PostDays = post_d
    ),
    PrimaryCriteriaLimit = list(
      Type = if (one_v) "First" else "All"
    )
  )

  steps <- if (!is.null(cohort_flow$steps)) cohort_flow$steps else list()
  inclusion_rules <- list()

  for (idx in seq_along(steps)) {
    st <- steps[[idx]]
    st_name <- if (is.list(st) || is.vector(st)) st[[1]] else sprintf("Step %d", idx)
    st_pred <- if ((is.list(st) || is.vector(st)) && length(st) >= 2) st[[2]] else ""
    pred_l <- tolower(as.character(st_pred))

    rule_crit <- list(
      list(
        VisitOccurrence = list(
          CodesetId = 0L
        ),
        Occurrence = list(Type = 2L, Count = 1L),
        StartWindow = list(
          Start = list(Days = 0L, Coeff = -1L),
          End = list(Days = 0L, Coeff = 1L)
        )
      )
    )

    inclusion_rules[[idx]] <- list(
      name = as.character(st_name),
      description = sprintf("SQL Predicate: %s", as.character(st_pred)),
      expression = list(
        Type = "ALL",
        CriteriaList = rule_crit
      )
    )
  }

  circe_data <- list(
    ConceptSets = concept_sets,
    PrimaryCriteria = primary_criteria,
    InclusionRules = inclusion_rules,
    QualifiedLimit = list(
      Type = if (one_v) "First" else "All"
    ),
    ExpressionType = "SIMPLE_EXPRESSION",
    Title = cohort_name,
    Description = if (!is.null(cohort_description)) cohort_description else sprintf("Exported from omopduckdb cohort_flow (%d inclusion rule(s)).", length(steps))
  )

  if (!is.null(output_path)) {
    json_txt <- jsonlite::toJSON(circe_data, auto_unbox = TRUE, pretty = TRUE)
    writeLines(json_txt, con = output_path)
  }

  circe_data
}

