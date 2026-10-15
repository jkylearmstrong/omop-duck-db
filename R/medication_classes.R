#' @keywords internal
#' @noRd
.med_q <- function(x) paste0("'", gsub("'", "''", as.character(x), fixed = TRUE), "'")

#' @keywords internal
#' @noRd
.med_first_present <- function(columns, candidates, what, given = NULL) {
  lower <- tolower(columns)
  if (!is.null(given)) {
    if (!tolower(given) %in% lower) stop(sprintf("%s column '%s' not in %s.", what, given, paste(columns, collapse = ", ")))
    return(columns[match(tolower(given), lower)])
  }
  for (cand in candidates) {
    if (cand %in% lower) return(columns[match(cand, lower)])
  }
  stop(sprintf("Could not find a %s column among %s; pass it explicitly.", what, paste(columns, collapse = ", ")))
}

#' @keywords internal
#' @noRd
.med_resolve_table <- function(con, name, vocab_schema = NULL, vocab_db = NULL) {
  candidates <- c()
  if (!is.null(vocab_db) && nzchar(vocab_db) && !is.null(vocab_schema) && nzchar(vocab_schema)) {
    candidates <- c(candidates, paste0(vocab_db, ".", vocab_schema, ".", name))
  }
  if (!is.null(vocab_db) && nzchar(vocab_db)) {
    candidates <- c(candidates, paste0(vocab_db, ".main.", name), paste0(vocab_db, ".", name))
  }
  if (!is.null(vocab_schema) && nzchar(vocab_schema)) {
    candidates <- c(candidates, paste0(vocab_schema, ".", name))
  }
  candidates <- c(candidates, name, paste0("central_vocab.main.", name), paste0("central_vocab.", name))
  candidates <- unique(candidates)
  for (ref in candidates) {
    hit <- tryCatch(nrow(DBI::dbGetQuery(con, paste0("SELECT 1 FROM ", ref, " LIMIT 1"))) >= 0, error = function(e) FALSE)
    if (isTRUE(hit)) return(ref)
  }
  stop(sprintf("Vocabulary table '%s' is not reachable (or empty) on this connection.", name))
}

#' Combination Product Disaggregation Audit
#'
#' Audits medication exposures for a specific OMOP class concept to determine the proportion
#' of patients receiving plain single-ingredient formulations vs fixed-dose combination products.
#'
#' @param con Active DuckDB connection.
#' @param class_concept_id Concept ID(s) of interest (e.g. RxNorm ingredient ID or ATC class concept ID).
#' @param cohort_table Optional cohort table name to restrict exposures to.
#' @param vocab_schema Optional schema containing vocabulary tables.
#' @param vocab_db Optional attached database containing vocabulary tables.
#' @return A named list of audit counts and shares.
#' @export
audit_combination_products <- function(con, class_concept_id, cohort_table = NULL, vocab_schema = NULL, vocab_db = NULL) {
  anc_tbl <- .med_resolve_table(con, "concept_ancestor", vocab_schema = vocab_schema, vocab_db = vocab_db)
  concept_tbl <- .med_resolve_table(con, "concept", vocab_schema = vocab_schema, vocab_db = vocab_db)

  ids <- as.integer(class_concept_id)
  ids_str <- paste(ids, collapse = ", ")

  cohort_join <- if (!is.null(cohort_table) && nzchar(cohort_table)) {
    sprintf("JOIN %s coh ON coh.person_id = de.person_id", cohort_table)
  } else {
    ""
  }

  summary_sql <- sprintf("
    WITH target_descendants AS (
        SELECT DISTINCT ca.descendant_concept_id AS drug_concept_id
        FROM %s ca
        WHERE ca.ancestor_concept_id IN (%s)
    ),
    ingredient_counts AS (
        SELECT td.drug_concept_id, count(DISTINCT ca2.ancestor_concept_id) AS n_ing
        FROM target_descendants td
        JOIN %s ca2 ON ca2.descendant_concept_id = td.drug_concept_id
        JOIN %s c ON c.concept_id = ca2.ancestor_concept_id
        WHERE c.concept_class_id = 'Ingredient'
        GROUP BY td.drug_concept_id
    ),
    classified_drugs AS (
        SELECT
            td.drug_concept_id,
            CASE WHEN COALESCE(ic.n_ing, 1) > 1 THEN 1 ELSE 0 END AS is_combination
        FROM target_descendants td
        LEFT JOIN ingredient_counts ic ON ic.drug_concept_id = td.drug_concept_id
    ),
    patient_flags AS (
        SELECT
            de.person_id,
            MAX(cd.is_combination) AS has_combo,
            MAX(CASE WHEN cd.is_combination = 0 THEN 1 ELSE 0 END) AS has_plain,
            SUM(cd.is_combination) AS combo_exp,
            SUM(CASE WHEN cd.is_combination = 0 THEN 1 ELSE 0 END) AS plain_exp
        FROM drug_exposure de
        JOIN classified_drugs cd ON cd.drug_concept_id = de.drug_concept_id
        %s
        GROUP BY de.person_id
    )
    SELECT
        COUNT(DISTINCT person_id) AS total_patients,
        COUNT(CASE WHEN has_plain = 1 AND has_combo = 0 THEN 1 END) AS plain_only_patients,
        COUNT(CASE WHEN has_combo = 1 AND has_plain = 0 THEN 1 END) AS combination_only_patients,
        COUNT(CASE WHEN has_plain = 1 AND has_combo = 1 THEN 1 END) AS overlapping_patients,
        COALESCE(SUM(plain_exp), 0) AS plain_only_exposures,
        COALESCE(SUM(combo_exp), 0) AS combination_only_exposures,
        COALESCE(SUM(plain_exp + combo_exp), 0) AS total_exposures
    FROM patient_flags;
  ", anc_tbl, ids_str, anc_tbl, concept_tbl, cohort_join)

  res <- DBI::dbGetQuery(con, summary_sql)
  tot_pts <- as.integer(res$total_patients[1] %||% 0)
  plain_pts <- as.integer(res$plain_only_patients[1] %||% 0)
  combo_pts <- as.integer(res$combination_only_patients[1] %||% 0)
  overlap_pts <- as.integer(res$overlapping_patients[1] %||% 0)
  plain_exp <- as.integer(res$plain_only_exposures[1] %||% 0)
  combo_exp <- as.integer(res$combination_only_exposures[1] %||% 0)
  tot_exp <- as.integer(res$total_exposures[1] %||% 0)

  combo_share <- if (tot_pts > 0) round((combo_pts + overlap_pts) / tot_pts, 4) else 0.0

  list(
    class_concept_ids = ids,
    total_patients = tot_pts,
    plain_only_patients = plain_pts,
    combination_only_patients = combo_pts,
    overlapping_patients = overlap_pts,
    plain_only_exposures = plain_exp,
    combination_only_exposures = combo_exp,
    total_exposures = tot_exp,
    combination_share_patients = combo_share,
    plain_only_share = if (tot_pts > 0) round(plain_pts / tot_pts, 4) else 0.0,
    overlapping_share = if (tot_pts > 0) round(overlap_pts / tot_pts, 4) else 0.0
  )
}

#' @keywords internal
#' @noRd
.med_normalize_specs <- function(medication_specs) {
  if (length(medication_specs) == 0) stop("medication_specs must define at least one drug class.")
  known <- c("ancestor_concept_ids", "atc_codes", "rxnorm_ingredient_ids", "source_values")
  out <- list()
  for (cls in names(medication_specs)) {
    spec <- medication_specs[[cls]]
    if (is.list(spec)) {
      extra <- setdiff(names(spec), known)
      if (length(extra)) stop(sprintf("Unknown keys %s in spec for '%s'; expected %s.", paste(extra, collapse = ", "), cls, paste(known, collapse = ", ")))
      out[[cls]] <- stats::setNames(lapply(known, function(k) spec[[k]] %||% vector("list", 0)), known)
      out[[cls]] <- lapply(out[[cls]], unlist)
    } else {
      out[[cls]] <- list(ancestor_concept_ids = spec, atc_codes = NULL, rxnorm_ingredient_ids = NULL, source_values = NULL)
    }
  }
  out
}

#' @keywords internal
#' @noRd
.med_load_crosswalk <- function(con, crosswalk) {
  empty <- data.frame(source_value = character(0), drug_class = character(0), stringsAsFactors = FALSE)
  if (is.null(crosswalk)) return(empty)
  df <- if (is.data.frame(crosswalk)) {
    crosswalk
  } else if (grepl("\\.(csv|tsv|txt|csv\\.gz)$", crosswalk, ignore.case = TRUE) || file.exists(crosswalk)) {
    DBI::dbGetQuery(con, paste0("SELECT * FROM ", read_csv_sql(crosswalk, all_varchar = TRUE)))
  } else {
    DBI::dbGetQuery(con, paste0("SELECT * FROM ", crosswalk))
  }
  src <- .med_first_present(names(df), c("source_value", "source_code", "source", "code", "cui", "ndc"), "crosswalk source-value")
  cls <- .med_first_present(names(df), c("drug_class", "medication_class", "class", "class_name"), "crosswalk drug-class")
  out <- data.frame(source_value = toupper(trimws(as.character(df[[src]]))),
                    drug_class = trimws(as.character(df[[cls]])), stringsAsFactors = FALSE)
  out <- out[nzchar(out$source_value) & out$source_value != "NA", , drop = FALSE]
  unique(out)
}

#' Dual-strategy medication-class classifier
#'
#' Classifies each cohort member's medication exposure two independent ways, so sites with very different
#' drug coding (legacy CUI / NDC source strings vs standardized RxNorm) can be compared:
#'
#' * **Strategy A, source / crosswalk** (`"source"`): a user-supplied crosswalk (CSV path, DuckDB table, or
#'   data frame) maps source strings to drug classes. Matched against `drug_source_value` and the
#'   `concept_code` of `drug_source_concept_id`.
#' * **Strategy B, OMOP standard hierarchy** (`"standard"`): `concept_ancestor` traversal from ATC class
#'   concepts (levels 3/4/5, by concept id or ATC code) or RxNorm ingredients down to `drug_concept_id`.
#' * `strategy = "both"` runs both; the combined flag is their OR.
#'
#' Mirror of `omop_etl.medication_classes.extract_medication_classes` in Python.
#'
#' @param db_con DuckDB DBI connection with `drug_exposure` (and `concept_ancestor` / `concept` for the
#'   standard strategy).
#' @param cohort_table Table / view with a person column (`subject_id` / `person_id`) and an index date
#'   (`cohort_start_date` / `index_date`).
#' @param medication_specs Named list `class_name -> spec`. `spec` is a list with any of
#'   `ancestor_concept_ids` (ATC class or RxNorm ingredient concept ids), `atc_codes`, `rxnorm_ingredient_ids`,
#'   `source_values` (inline strategy-A strings); or a bare vector, taken as `ancestor_concept_ids`.
#' @param strategy `"source"`, `"standard"` or `"both"`.
#' @param crosswalk Strategy-A mapping of source strings to classes (data frame, CSV path or table name with
#'   columns like `source_value` / `drug_class`). Combined with inline `source_values`.
#' @param cohort_definition_id Restrict a stacked OMOP cohort table to one definition.
#' @param person_col,index_date_col,site_col Override column auto-detection. A site column
#'   (`site_anon` / `site_id` / `site`) carries through for per-site auditing.
#' @param lookback_days,lookahead_days Exposure window around the index date. `NULL` lookback = unbounded.
#' @param window Optional named temporal frame overriding the lookback/lookahead days: `"pre_admission"` =
#'   `[index - lookback_days (default 365), index - 1]`; `"inpatient"` = `[index, stay end]`; `"discharge"` =
#'   `[stay end, stay end]` (orders written at discharge).
#' @param end_date_col Stay end column for `"inpatient"` / `"discharge"` (default `cohort_end_date` /
#'   `visit_end_date`).
#' @param on_unresolved Action when concepts or ATC codes fail to resolve (`"raise"`, `"warn"`, or `"ignore"`).
#' @param exclude_forms Character vector of dosage/form patterns to exclude (e.g. `"topical"`, `"ophthalmic"`).
#' @param exclude_dose_form_concept_ids Optional concept IDs of dose forms to exclude via 'RxNorm has dose form'.
#' @param apply_exclude_to_source Also apply form/dose-form exclusions to strategy A source values.
#' @param exclude_forms_source Alias for `apply_exclude_to_source`.
#' @param disaggregate_combinations If `TRUE`, adds `<class>_plain` and `<class>_combination` columns.
#' @param is_combination Alias for `disaggregate_combinations`.
#' @param vocab_schema Optional schema name containing vocabulary tables.
#' @param vocab_db Optional attached database containing vocabulary tables.
#' @param route_concept_ids Vector of standard OMOP route concept IDs to filter exposures by.
#' @param by_type Logical; if `TRUE`, also extracts flags separated by source concept type.
#' @param return_audit Also return [audit_medication_strategies()] output (needs `strategy = "both"`).
#' @param ... Forwarded to [audit_medication_strategies()].
#' @return A data frame with `person_id`, `index_date`, optional `site`, and per class `<class>_source`,
#'   `<class>_standard` (0/1, per the chosen strategy) and `<class>` (OR of the available flags). With
#'   `return_audit = TRUE`, a list `list(flags =, audit =)`.
#' @export
extract_medication_classes <- function(db_con, cohort_table, medication_specs, strategy = c("both", "source", "standard"),
                                       crosswalk = NULL, cohort_definition_id = NULL, person_col = NULL,
                                       index_date_col = NULL, site_col = NULL, lookback_days = NULL,
                                       lookahead_days = 0L, window = NULL, end_date_col = NULL,
                                       on_unresolved = c("raise", "warn", "ignore"),
                                       exclude_forms = NULL,
                                       exclude_dose_form_concept_ids = NULL,
                                       apply_exclude_to_source = FALSE,
                                       exclude_forms_source = FALSE,
                                       disaggregate_combinations = FALSE,
                                       is_combination = FALSE,
                                       vocab_schema = NULL,
                                       vocab_db = NULL,
                                       route_concept_ids = NULL, by_type = FALSE,
                                       return_audit = FALSE, ...) {
  strategy <- match.arg(strategy)
  on_unresolved <- match.arg(on_unresolved)
  windows <- c("pre_admission", "inpatient", "discharge")
  if (!is.null(window) && !window %in% windows) stop(sprintf("window must be one of %s, got '%s'.", paste(windows, collapse = ", "), window))
  if (isTRUE(return_audit) && strategy != "both") stop("return_audit = TRUE requires strategy = 'both'.")
  specs <- .med_normalize_specs(medication_specs)
  use_source <- strategy %in% c("source", "both")
  use_standard <- strategy %in% c("standard", "both")
  filter_combo <- isTRUE(disaggregate_combinations) || isTRUE(is_combination)
  apply_source_filter <- isTRUE(apply_exclude_to_source) || isTRUE(exclude_forms_source)
  v_schema <- if (!is.null(vocab_schema) && nzchar(vocab_schema)) vocab_schema else vocab_db

  cols <- DBI::dbGetQuery(db_con, paste0("DESCRIBE SELECT * FROM ", cohort_table))$column_name
  pcol <- .med_first_present(cols, c("subject_id", "person_id"), "person", person_col)
  icol <- .med_first_present(cols, c("cohort_start_date", "index_date"), "index date", index_date_col)
  scol <- NULL
  if (!is.null(site_col) || any(c("site_anon", "site_id", "site") %in% tolower(cols))) {
    scol <- .med_first_present(cols, c("site_anon", "site_id", "site"), "site", site_col)
  }
  where <- ""
  if (!is.null(cohort_definition_id)) {
    .med_first_present(cols, "cohort_definition_id", "cohort_definition_id")
    where <- sprintf("WHERE cohort_definition_id = %d", as.integer(cohort_definition_id))
  }

  ecol <- NULL
  if (!is.null(window) && window %in% c("inpatient", "discharge")) {
    ecol <- .med_first_present(cols, c("cohort_end_date", "visit_end_date", "discharge_date", "end_date"),
                               "stay end date", end_date_col)
  }
  end_sel <- if (!is.null(ecol)) sprintf(', CAST("%s" AS DATE) AS end_date', ecol) else ""
  site_sel <- if (!is.null(scol)) sprintf(', "%s" AS site', scol) else ""
  base <- DBI::dbGetQuery(db_con, sprintf(
    'SELECT ROW_NUMBER() OVER () AS row_id, "%s" AS person_id, CAST("%s" AS DATE) AS index_date%s%s FROM %s %s',
    pcol, icol, end_sel, site_sel, cohort_table, where))
  result <- base[, setdiff(names(base), c("row_id", "end_date")), drop = FALSE]

  d <- "de.drug_exposure_start_date"
  if (identical(window, "pre_admission")) {
    lb <- if (is.null(lookback_days)) 365L else as.integer(lookback_days)
    lo <- sprintf("AND %s >= v.index_date - %d", d, lb)
    hi <- sprintf("AND %s <= v.index_date - 1", d)
  } else if (identical(window, "inpatient")) {
    lo <- sprintf("AND %s >= v.index_date", d)
    hi <- sprintf("AND %s <= v.end_date", d)
  } else if (identical(window, "discharge")) {
    lo <- sprintf("AND %s >= v.end_date", d)
    hi <- sprintf("AND %s <= v.end_date", d)
  } else {
    lo <- if (is.null(lookback_days)) "" else sprintf("AND %s >= v.index_date - %d", d, as.integer(lookback_days))
    hi <- sprintf("AND %s <= v.index_date + %d", d, as.integer(lookahead_days))
  }

  route_clause <- ""
  if (!is.null(route_concept_ids) && length(route_concept_ids) > 0) {
    route_clause <- sprintf("AND de.route_concept_id IN (%s)", paste(as.integer(route_concept_ids), collapse = ", "))
  }

  flags <- list(source = list(), standard = list())
  flags_plain <- list(standard = list())
  flags_combo <- list(standard = list())
  flags_by_type <- list(source = list(), standard = list())
  tmp <- character(0)
  on.exit(for (t in tmp) try(DBI::dbExecute(db_con, paste0("DROP TABLE IF EXISTS ", t)), silent = TRUE), add = TRUE)
  DBI::dbWriteTable(db_con, "_med_cohort", base[, c("row_id", "person_id", "index_date", if (!is.null(ecol)) "end_date")], temporary = TRUE, overwrite = TRUE)
  tmp <- c(tmp, "_med_cohort")

  if (use_standard) {
    anc <- .med_resolve_table(db_con, "concept_ancestor", vocab_schema = v_schema, vocab_db = vocab_db)
    concept <- .med_resolve_table(db_con, "concept", vocab_schema = v_schema, vocab_db = vocab_db)
    cr_tbl <- tryCatch(.med_resolve_table(db_con, "concept_relationship", vocab_schema = v_schema, vocab_db = vocab_db), error = function(e) "concept_relationship")
    rows <- list()
    for (cls in names(specs)) {
      spec <- specs[[cls]]
      ids <- as.integer(c(spec$ancestor_concept_ids, spec$rxnorm_ingredient_ids))
      if (length(spec$atc_codes)) {
        codes <- paste(.med_q(spec$atc_codes), collapse = ", ")
        found_atc <- DBI::dbGetQuery(db_con, sprintf(
          "SELECT concept_id FROM %s WHERE vocabulary_id LIKE 'ATC%%' AND concept_code IN (%s)", concept, codes))$concept_id
        ids <- c(ids, found_atc)
        if (length(found_atc) == 0 && on_unresolved %in% c("raise", "warn")) {
          msg <- sprintf("Medication class '%s' ATC codes %s resolved to no concept in vocabulary.", cls, paste(spec$atc_codes, collapse = ", "))
          if (on_unresolved == "raise") stop(msg) else warning(msg)
        }
      }
      if (length(ids)) {
        rows[[length(rows) + 1L]] <- data.frame(drug_class = cls, ancestor_concept_id = sort(unique(as.integer(ids))),
                                                stringsAsFactors = FALSE)
      } else if (length(spec$atc_codes) == 0 && on_unresolved %in% c("raise", "warn")) {
        msg <- sprintf("Medication class '%s' has no ancestor concepts or ATC codes.", cls)
        if (on_unresolved == "raise") stop(msg) else warning(msg)
      }
    }
    anc_df <- if (length(rows)) do.call(rbind, rows) else data.frame(drug_class = character(0), ancestor_concept_id = integer(0))
    DBI::dbWriteTable(db_con, "_med_anc", anc_df, temporary = TRUE, overwrite = TRUE)
    tmp <- c(tmp, "_med_anc", "_med_class_concepts")

    ex_cond_parts <- character(0)
    if (!is.null(exclude_dose_form_concept_ids) && length(exclude_dose_form_concept_ids) > 0) {
      df_ids <- paste(as.integer(exclude_dose_form_concept_ids), collapse = ", ")
      ex_cond_parts <- c(ex_cond_parts, sprintf("(
        ca.descendant_concept_id IN (
          SELECT cr_sub.concept_id_1
          FROM %s cr_sub
          WHERE cr_sub.relationship_id = 'RxNorm has dose form'
            AND cr_sub.concept_id_2 IN (%s)
        )
        OR ca.descendant_concept_id IN (%s)
      )", cr_tbl, df_ids, df_ids))
    }
    if (!is.null(exclude_forms) && length(exclude_forms) > 0) {
      ex_form_strings <- tolower(exclude_forms[!grepl("^[0-9]+$", as.character(exclude_forms))])
      ex_form_ids <- as.integer(exclude_forms[grepl("^[0-9]+$", as.character(exclude_forms))])
      if (length(ex_form_ids) > 0) {
        df_ids <- paste(ex_form_ids, collapse = ", ")
        ex_cond_parts <- c(ex_cond_parts, sprintf("(
          ca.descendant_concept_id IN (
            SELECT cr_sub.concept_id_1
            FROM %s cr_sub
            WHERE cr_sub.relationship_id = 'RxNorm has dose form'
              AND cr_sub.concept_id_2 IN (%s)
          )
          OR ca.descendant_concept_id IN (%s)
        )", cr_tbl, df_ids, df_ids))
      }
      if (length(ex_form_strings) > 0) {
        c_matches <- paste(sprintf("LOWER(c.concept_name) LIKE '%%%s%%'", ex_form_strings), collapse = " OR ")
        df_matches <- paste(sprintf("LOWER(df_c.concept_name) LIKE '%%%s%%'", ex_form_strings), collapse = " OR ")
        ex_cond_parts <- c(ex_cond_parts, sprintf("(
          (%s)
          OR ca.descendant_concept_id IN (
            SELECT cr_sub.concept_id_1
            FROM %s cr_sub
            JOIN %s df_c ON df_c.concept_id = cr_sub.concept_id_2
            WHERE cr_sub.relationship_id = 'RxNorm has dose form'
              AND (%s)
          )
        )", c_matches, cr_tbl, concept, df_matches))
      }
    }

    exclude_clause <- ""
    if (length(ex_cond_parts) > 0) {
      combined_ex <- paste(ex_cond_parts, collapse = " OR ")
      exclude_clause <- sprintf("JOIN %s c ON c.concept_id = ca.descendant_concept_id WHERE NOT (%s)", concept, combined_ex)
    }

    combo_sel <- ""
    combo_join <- ""
    if (filter_combo) {
      combo_join <- sprintf("
        LEFT JOIN (
          SELECT ca_ing.descendant_concept_id, count(DISTINCT ca_ing.ancestor_concept_id) AS n_ing
          FROM %s ca_ing
          JOIN %s c_ing ON c_ing.concept_id = ca_ing.ancestor_concept_id
          WHERE c_ing.concept_class_id = 'Ingredient'
          GROUP BY ca_ing.descendant_concept_id
        ) ic ON ic.descendant_concept_id = ca.descendant_concept_id
      ", anc, concept)
      combo_sel <- ", CASE WHEN COALESCE(ic.n_ing, 1) > 1 THEN 1 ELSE 0 END AS is_combination"
    }

    DBI::dbExecute(db_con, sprintf(
      paste0("CREATE OR REPLACE TEMPORARY TABLE _med_class_concepts AS ",
             "SELECT DISTINCT a.drug_class, ca.descendant_concept_id AS concept_id%s FROM _med_anc a ",
             "JOIN %s ca ON ca.ancestor_concept_id = a.ancestor_concept_id ",
             "%s %s"), combo_sel, anc, combo_join, exclude_clause))

    if (on_unresolved %in% c("raise", "warn")) {
      desc_counts <- DBI::dbGetQuery(db_con, "SELECT drug_class, count(*) AS n FROM _med_class_concepts GROUP BY drug_class")
      for (cls in names(specs)) {
        if (!cls %in% anc_df$drug_class) next
        cnt <- if (cls %in% desc_counts$drug_class) desc_counts$n[desc_counts$drug_class == cls] else 0
        if (cnt == 0) {
          msg <- sprintf("Medication class '%s' resolved to no descendant concepts in concept_ancestor.", cls)
          if (on_unresolved == "raise") stop(msg) else warning(msg)
        }
      }
    }

    type_sel <- if (isTRUE(by_type)) ", de.drug_type_concept_id" else ""
    combo_hit_sel <- if (filter_combo) ", m.is_combination" else ""
    hits <- DBI::dbGetQuery(db_con, paste0(
      "SELECT DISTINCT v.row_id, m.drug_class", combo_hit_sel, type_sel, " FROM _med_cohort v ",
      "JOIN drug_exposure de ON de.person_id = v.person_id ",
      "JOIN _med_class_concepts m ON m.concept_id = de.drug_concept_id ",
      "WHERE TRUE ", lo, " ", hi, " ", route_clause))
    flags$standard <- split(hits$row_id, hits$drug_class)
    if (filter_combo && "is_combination" %in% names(hits)) {
      plain_hits <- hits[hits$is_combination == 0, , drop = FALSE]
      combo_hits <- hits[hits$is_combination == 1, , drop = FALSE]
      flags_plain$standard <- split(plain_hits$row_id, plain_hits$drug_class)
      flags_combo$standard <- split(combo_hits$row_id, combo_hits$drug_class)
    }
    if (isTRUE(by_type) && "drug_type_concept_id" %in% names(hits)) {
      non_na_hits <- hits[!is.na(hits$drug_type_concept_id), , drop = FALSE]
      if (nrow(non_na_hits) > 0) {
        keys <- paste0(non_na_hits$drug_class, "_type_", as.integer(non_na_hits$drug_type_concept_id))
        flags_by_type$standard <- split(non_na_hits$row_id, keys)
      }
    }
  }

  if (use_source) {
    xw <- .med_load_crosswalk(db_con, crosswalk)
    inline <- do.call(rbind, c(list(data.frame(source_value = character(0), drug_class = character(0), stringsAsFactors = FALSE)),
      lapply(names(specs), function(cls) {
        v <- specs[[cls]]$source_values
        if (length(v)) data.frame(source_value = toupper(trimws(as.character(v))), drug_class = cls, stringsAsFactors = FALSE)
      })))
    xw <- unique(rbind(xw, inline))
    xw <- xw[xw$drug_class %in% names(specs), , drop = FALSE]
    if (nrow(xw) == 0) {
      stop("Strategy 'source' needs a crosswalk or inline source_values for the classes in medication_specs.")
    }
    DBI::dbWriteTable(db_con, "_med_xw", xw, temporary = TRUE, overwrite = TRUE)
    tmp <- c(tmp, "_med_xw")
    code_join <- ""
    code_expr <- "NULL"
    de_cols <- tryCatch(tolower(DBI::dbGetQuery(db_con, "DESCRIBE SELECT * FROM drug_exposure LIMIT 0")$column_name), error = function(e) character(0))
    concept_ref <- tryCatch(.med_resolve_table(db_con, "concept", vocab_schema = v_schema, vocab_db = vocab_db), error = function(e) NULL)
    if (!is.null(concept_ref) && "drug_source_concept_id" %in% de_cols) {
      code_join <- sprintf("LEFT JOIN %s sc ON sc.concept_id = de.drug_source_concept_id", concept_ref)
      code_expr <- "UPPER(TRIM(sc.concept_code))"
    }

    src_exclude_clause <- ""
    if (apply_source_filter && ((!is.null(exclude_forms) && length(exclude_forms) > 0) ||
                                (!is.null(exclude_dose_form_concept_ids) && length(exclude_dose_form_concept_ids) > 0))) {
      cr_tbl <- tryCatch(.med_resolve_table(db_con, "concept_relationship", vocab_schema = v_schema, vocab_db = vocab_db), error = function(e) "concept_relationship")
      concept_tbl <- tryCatch(.med_resolve_table(db_con, "concept", vocab_schema = v_schema, vocab_db = vocab_db), error = function(e) "concept")
      src_ex_conds <- character(0)
      if (!is.null(exclude_dose_form_concept_ids) && length(exclude_dose_form_concept_ids) > 0) {
        df_ids <- paste(as.integer(exclude_dose_form_concept_ids), collapse = ", ")
        src_ex_conds <- c(src_ex_conds, sprintf("(
          de.drug_concept_id IN (
            SELECT concept_id_1 FROM %s
            WHERE relationship_id = 'RxNorm has dose form'
              AND concept_id_2 IN (%s)
          )
          OR de.drug_concept_id IN (%s)
        )", cr_tbl, df_ids, df_ids))
      }
      if (!is.null(exclude_forms) && length(exclude_forms) > 0) {
        s_ex_strings <- tolower(exclude_forms[!grepl("^[0-9]+$", as.character(exclude_forms))])
        s_ex_ids <- as.integer(exclude_forms[grepl("^[0-9]+$", as.character(exclude_forms))])
        if (length(s_ex_ids) > 0) {
          df_ids <- paste(s_ex_ids, collapse = ", ")
          src_ex_conds <- c(src_ex_conds, sprintf("(
            de.drug_concept_id IN (
              SELECT concept_id_1 FROM %s
              WHERE relationship_id = 'RxNorm has dose form'
                AND concept_id_2 IN (%s)
            )
            OR de.drug_concept_id IN (%s)
          )", cr_tbl, df_ids, df_ids))
        }
        if (length(s_ex_strings) > 0) {
          val_conds <- paste(sprintf("LOWER(de.drug_source_value) LIKE '%%%s%%'", s_ex_strings), collapse = " OR ")
          df_conds <- paste(sprintf("LOWER(df_c.concept_name) LIKE '%%%s%%'", s_ex_strings), collapse = " OR ")
          src_ex_conds <- c(src_ex_conds, sprintf("(
            (%s)
            OR de.drug_concept_id IN (
              SELECT cr_sub.concept_id_1
              FROM %s cr_sub
              JOIN %s df_c ON df_c.concept_id = cr_sub.concept_id_2
              WHERE cr_sub.relationship_id = 'RxNorm has dose form'
                AND (%s)
            )
          )", val_conds, cr_tbl, concept_tbl, df_conds))
        }
      }
      if (length(src_ex_conds) > 0) {
        src_exclude_clause <- sprintf("AND NOT (%s)", paste(src_ex_conds, collapse = " OR "))
      }
    }

    type_sel <- if (isTRUE(by_type)) ", de.drug_type_concept_id" else ""
    hits <- DBI::dbGetQuery(db_con, paste0(
      "SELECT DISTINCT v.row_id, x.drug_class", type_sel, " FROM _med_cohort v ",
      "JOIN drug_exposure de ON de.person_id = v.person_id ", code_join, " ",
      "JOIN _med_xw x ON x.source_value = UPPER(TRIM(de.drug_source_value)) OR x.source_value = ", code_expr, " ",
      "WHERE TRUE ", lo, " ", hi, " ", route_clause, " ", src_exclude_clause))
    flags$source <- split(hits$row_id, hits$drug_class)
    if (isTRUE(by_type) && "drug_type_concept_id" %in% names(hits)) {
      non_na_hits <- hits[!is.na(hits$drug_type_concept_id), , drop = FALSE]
      if (nrow(non_na_hits) > 0) {
        keys <- paste0(non_na_hits$drug_class, "_type_", as.integer(non_na_hits$drug_type_concept_id))
        flags_by_type$source <- split(non_na_hits$row_id, keys)
      }
    }
  }

  for (cls in names(specs)) {
    present <- character(0)
    if (use_source) {
      result[[paste0(cls, "_source")]] <- as.integer(base$row_id %in% (flags$source[[cls]] %||% integer(0)))
      present <- c(present, paste0(cls, "_source"))
    }
    if (use_standard) {
      result[[paste0(cls, "_standard")]] <- as.integer(base$row_id %in% (flags$standard[[cls]] %||% integer(0)))
      present <- c(present, paste0(cls, "_standard"))
      if (filter_combo) {
        result[[paste0(cls, "_plain")]] <- as.integer(base$row_id %in% (flags_plain$standard[[cls]] %||% integer(0)))
        result[[paste0(cls, "_combination")]] <- as.integer(base$row_id %in% (flags_combo$standard[[cls]] %||% integer(0)))
      }
    }
    result[[cls]] <- do.call(pmax, unname(as.list(result[present])))
  }

  if (isTRUE(by_type)) {
    all_type_keys <- sort(unique(c(names(flags_by_type$source), names(flags_by_type$standard))))
    for (tkey in all_type_keys) {
      rows_s <- flags_by_type$source[[tkey]] %||% integer(0)
      rows_t <- flags_by_type$standard[[tkey]] %||% integer(0)
      result[[tkey]] <- as.integer(base$row_id %in% union(rows_s, rows_t))
    }
  }

  if (isTRUE(return_audit)) {
    return(list(flags = result, audit = audit_medication_strategies(result, classes = names(specs),
                                                                   vocab_schema = vocab_schema, vocab_db = vocab_db, ...)))
  }
  result
}

#' @keywords internal
#' @noRd
.med_kappa <- function(a, b, c, d) {
  n <- a + b + c + d
  if (n == 0) return(NaN)
  po <- (a + d) / n
  pe <- ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
  if (pe >= 1) return(if (po == 1) 1 else NaN)
  (po - pe) / (1 - pe)
}

#' Per-site agreement audit of the source-crosswalk and OMOP-hierarchy strategies
#'
#' Computes, for each site and drug class (plus a pooled `ALL` row), the 2x2 agreement table of the two
#' strategies, Cohen's kappa, raw concordance and prevalences, and flags data-quality anomalies. Sites with
#' broken drug mappings stand out; nothing is hardcoded to particular sites.
#' Mirror of `omop_etl.medication_classes.audit_medication_strategies` in Python.
#'
#' @param flags Output of [extract_medication_classes()] with `strategy = "both"`.
#' @param classes Classes to audit (default: every class with both `_source` and `_standard` columns).
#' @param site_col Site column in `flags`; if absent all rows are audited as one site, `"all"`.
#' @param kappa_threshold Flag Cohen's kappa below this.
#' @param concordance_threshold Flag raw agreement below this.
#' @param prevalence_gap_threshold Flag `|prevalence_source - prevalence_standard|` above this.
#' @param peer_kappa_gap With at least 3 sites, flag a site whose kappa trails the median of the *other* sites
#'   by more than this.
#' @param min_n Minimum cohort size to audit (default 10).
#' @param min_prevalence Minimum expected prevalence threshold (default NULL).
#' @param explain If TRUE, joins disagreement explanation (requires \code{con} and \code{medication_specs}).
#' @param con Active DuckDB DBI connection (needed if \code{explain = TRUE}).
#' @param medication_specs Medication class specifications (needed if \code{explain = TRUE}).
#' @param cohort_table Cohort table name (default 'cohort').
#' @param vocab_schema Optional schema containing vocabulary tables.
#' @param vocab_db Optional attached database containing vocabulary tables.
#' @param ... Forwarded to [explain_medication_disagreement()] if \code{explain = TRUE}.
#' @return A data frame with one row per (site, class): `site`, `medication_class`, `n`, `both`,
#'   `source_only`, `standard_only`, `neither`, `source_prevalence`, `standard_prevalence`, `concordance`,
#'   `kappa`, `reason` (`both_blind`, `source_blind`, `standard_blind`, `below_min_prevalence`, `low_kappa`,
#'   `low_concordance`, `prevalence_gap`, `peer_outlier`) and `flagged`.
#'   If `explain = TRUE`, returns a list `list(audit =, explanation =)`.
#' @export
audit_medication_strategies <- function(flags, classes = NULL, site_col = "site", kappa_threshold = 0.6,
                                        concordance_threshold = 0.9, prevalence_gap_threshold = 0.10,
                                        peer_kappa_gap = 0.25, min_n = 10, min_prevalence = NULL,
                                        explain = FALSE, con = NULL, medication_specs = NULL,
                                        cohort_table = "cohort", vocab_schema = NULL, vocab_db = NULL, ...) {
  if (is.null(classes)) {
    src <- sub("_source$", "", grep("_source$", names(flags), value = TRUE))
    classes <- src[paste0(src, "_standard") %in% names(flags)]
  }
  if (length(classes) == 0) {
    stop("No class has both '<class>_source' and '<class>_standard' columns; run extract_medication_classes(strategy = 'both').")
  }
  frame <- flags
  if (!site_col %in% names(frame)) frame[[site_col]] <- "all"
  sites <- as.character(frame[[site_col]])

  records <- list()
  for (cls in classes) {
    s <- as.logical(frame[[paste0(cls, "_source")]])
    t <- as.logical(frame[[paste0(cls, "_standard")]])
    groups <- c(split(seq_along(s), sites), list(ALL = seq_along(s)))
    for (site in names(groups)) {
      idx <- groups[[site]]
      ss <- s[idx]
      tt <- t[idx]
      a <- sum(ss & tt)
      b <- sum(ss & !tt)
      c_ <- sum(!ss & tt)
      d <- sum(!ss & !tt)
      n <- a + b + c_ + d
      records[[length(records) + 1L]] <- data.frame(
        site = site, medication_class = cls, n = n, both = a, source_only = b, standard_only = c_, neither = d,
        source_prevalence = if (n) (a + b) / n else NaN, standard_prevalence = if (n) (a + c_) / n else NaN,
        concordance = if (n) (a + d) / n else NaN, kappa = .med_kappa(a, b, c_, d), stringsAsFactors = FALSE
      )
    }
  }
  audit <- do.call(rbind, records)
  rownames(audit) <- NULL

  reasons <- vector("list", nrow(audit))
  for (i in seq_len(nrow(audit))) {
    r <- audit[i, ]
    cls <- r$medication_class
    why <- character(0)
    if (r$n > 0) {
      if (r$source_prevalence == 0 && r$standard_prevalence > 0) {
        why <- c(why, "source_blind")
      } else if (r$standard_prevalence == 0 && r$source_prevalence > 0) {
        why <- c(why, "standard_blind")
      } else if (r$source_prevalence == 0 && r$standard_prevalence == 0) {
        if (min_n > 0 && r$n >= min_n) {
          why <- c(why, "both_blind")
        }
      }

      if (!is.null(min_prevalence) && (min_n == 0 || r$n >= min_n)) {
        floor <- if (is.list(min_prevalence) || (is.vector(min_prevalence) && !is.null(names(min_prevalence)))) {
          min_prevalence[[cls]] %||% 0.0
        } else {
          as.numeric(min_prevalence)
        }
        if (!is.na(floor) && floor > 0 && (r$source_prevalence < floor || r$standard_prevalence < floor)) {
          why <- c(why, "below_min_prevalence")
        }
      }

      if (!is.nan(r$kappa) && r$kappa < kappa_threshold && length(why) == 0) why <- c(why, "low_kappa")
      if (r$concordance < concordance_threshold) why <- c(why, "low_concordance")
      if (abs(r$source_prevalence - r$standard_prevalence) > prevalence_gap_threshold) why <- c(why, "prevalence_gap")
    }
    reasons[[i]] <- why
  }
  per_site <- audit$site != "ALL"
  for (cls in classes) {
    mask <- per_site & audit$medication_class == cls
    if (sum(mask) >= 3) {
      for (i in which(mask)) {
        k <- audit$kappa[[i]]
        peers <- audit$kappa[mask & seq_len(nrow(audit)) != i]
        peers <- peers[!is.nan(peers)]
        if (!is.nan(k) && length(peers) && k < stats::median(peers) - peer_kappa_gap) {
          reasons[[i]] <- c(reasons[[i]], "peer_outlier")
        }
      }
    }
  }
  audit$reason <- vapply(reasons, function(x) paste(unique(x), collapse = ","), character(1))
  audit$flagged <- nzchar(audit$reason)

  if (isTRUE(explain)) {
    if (is.null(con) || is.null(medication_specs)) {
      stop("explain = TRUE requires con and medication_specs to be provided.")
    }
    expl <- explain_medication_disagreement(
      con = con, flags = flags, medication_specs = medication_specs, cohort_table = cohort_table,
      classes = classes, site_col = site_col, vocab_schema = vocab_schema, vocab_db = vocab_db, ...
    )
    return(list(audit = audit, explanation = expl))
  }

  audit
}

#' Assert Non-Zero Medication Exposure Coverage Across Sites
#'
#' Verifies that medication classes have non-zero (or expected) exposure coverage across sites,
#' preventing silent-zero data drops.
#'
#' @param flags Data frame with exposure flags (from [extract_medication_classes()]).
#' @param classes Classes to check (default: all base class columns).
#' @param min_n Minimum site cohort size required to trigger assertion (default 1000).
#' @param site_col Site column name (default 'site').
#' @param min_prevalence Minimum expected prevalence threshold (default 0.0).
#' @return Invisibly returns NULL; throws an error if assertion fails.
#' @export
assert_exposure_coverage <- function(flags, classes = NULL, min_n = 1000, site_col = "site", min_prevalence = 0.0) {
  df <- flags
  actual_site <- if (site_col %in% names(df)) site_col else NULL
  if (is.null(actual_site)) {
    df[["_site"]] <- "ALL"
    actual_site <- "_site"
  }

  if (is.null(classes)) {
    base_candidates <- setdiff(names(df), c("person_id", "subject_id", "index_date", "site", actual_site))
    classes <- base_candidates[!grepl("(_source|_standard)$", base_candidates) & !grepl("^_", base_candidates) & !grepl("_type_\\d+$", base_candidates)]
  }

  sites <- unique(df[[actual_site]])
  for (site in sites) {
    grp <- df[df[[actual_site]] == site, , drop = FALSE]
    n <- nrow(grp)
    if (n < min_n) next
    for (cls in classes) {
      col <- if (cls %in% names(grp)) cls else if (paste0(cls, "_standard") %in% names(grp)) paste0(cls, "_standard") else if (paste0(cls, "_source") %in% names(grp)) paste0(cls, "_source") else NULL
      if (is.null(col)) next
      prev <- mean(grp[[col]], na.rm = TRUE)
      if (is.na(prev) || prev <= min_prevalence || prev == 0.0) {
        stop(sprintf(
          "assert_exposure_coverage failed for medication class '%s' at site '%s': observed prevalence %.4f <= threshold %f (N=%d). Check vocabulary mapping and input drug_exposure completeness.",
          cls, site, if (is.na(prev)) 0 else prev, min_prevalence, n
        ))
      }
    }
  }
  invisible(NULL)
}

#' Explain Strategy Disagreements for Medication Exposure
#'
#' Identifies top drug concepts and source values present in one strategy but missing in the other,
#' applying small-cell count suppression (<11) so output aggregates can safely leave the institution.
#'
#' @param con Active DuckDB DBI connection.
#' @param flags Output of [extract_medication_classes()] with `strategy = 'both'`.
#' @param medication_specs Medication class specifications list.
#' @param cohort_table Cohort table name (default 'cohort').
#' @param classes Classes to audit.
#' @param site_col Site column name (default 'site').
#' @param person_col Person identifier column name (default 'person_id' or auto-detected).
#' @param top_n Maximum top disagreement patterns to return per site and class (default 10).
#' @param min_cell_count Cell count threshold for privacy suppression (default 11).
#' @param mask_value Suppression mask string (default '<11').
#' @param vocab_schema Optional schema containing vocabulary tables.
#' @param vocab_db Optional attached database containing vocabulary tables.
#' @return A data frame with columns `site`, `medication_class`, `disagreement_type`,
#'   `drug_source_value`, `drug_concept_id`, `concept_name`, `concept_class_id`,
#'   `patient_count`, `row_count`, `is_suppressed`.
#' @export
explain_medication_disagreement <- function(con, flags, medication_specs, cohort_table = "cohort",
                                            classes = NULL, site_col = "site", person_col = NULL,
                                            top_n = 10, min_cell_count = 11, mask_value = "<11",
                                            vocab_schema = NULL, vocab_db = NULL) {
  if (is.null(classes)) {
    src <- sub("_source$", "", grep("_source$", names(flags), value = TRUE))
    classes <- src[paste0(src, "_standard") %in% names(flags)]
  }

  pcol <- person_col %||% if ("person_id" %in% names(flags)) "person_id" else "subject_id"
  df <- flags
  actual_site <- if (site_col %in% names(df)) site_col else NULL
  if (is.null(actual_site)) {
    df[["_site"]] <- "ALL"
    actual_site <- "_site"
  }

  concept_table <- tryCatch(.med_resolve_table(con, "concept", vocab_schema = vocab_schema, vocab_db = vocab_db), error = function(e) "concept")
  records <- list()

  for (cls in classes) {
    src_col <- paste0(cls, "_source")
    std_col <- paste0(cls, "_standard")
    if (!src_col %in% names(df) || !std_col %in% names(df)) next

    sites <- unique(df[[actual_site]])
    for (site in sites) {
      grp <- df[df[[actual_site]] == site, , drop = FALSE]

      # Source only
      src_only_pids <- unique(grp[[pcol]][grp[[src_col]] == 1 & grp[[std_col]] == 0])
      if (length(src_only_pids) > 0) {
        pid_df <- data.frame(pid = as.integer(src_only_pids))
        DBI::dbWriteTable(con, "_disagree_pids", pid_df, temporary = TRUE, overwrite = TRUE)
        res <- DBI::dbGetQuery(con, sprintf("
          SELECT 
            COALESCE(de.drug_source_value, 'None') AS drug_source_value,
            COALESCE(de.drug_concept_id, 0) AS drug_concept_id,
            COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
            COALESCE(c.concept_class_id, 'None') AS concept_class_id,
            COUNT(DISTINCT de.person_id) AS patient_count,
            COUNT(*) AS row_count
          FROM drug_exposure de
          JOIN _disagree_pids dp ON dp.pid = de.person_id
          LEFT JOIN %s c ON c.concept_id = de.drug_concept_id
          GROUP BY 1, 2, 3, 4
          ORDER BY patient_count DESC, row_count DESC
          LIMIT %d
        ", concept_table, as.integer(top_n)))
        try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _disagree_pids"), silent = TRUE)

        if (nrow(res) > 0) {
          for (r in seq_len(nrow(res))) {
            p_cnt <- as.integer(res$patient_count[r])
            r_cnt <- as.integer(res$row_count[r])
            suppressed <- p_cnt < min_cell_count
            records[[length(records) + 1L]] <- data.frame(
              site = site, medication_class = cls, disagreement_type = "source_only",
              drug_source_value = res$drug_source_value[r],
              drug_concept_id = as.integer(res$drug_concept_id[r]),
              concept_name = res$concept_name[r],
              concept_class_id = res$concept_class_id[r],
              patient_count = if (suppressed) mask_value else as.character(p_cnt),
              row_count = if (r_cnt < min_cell_count) mask_value else as.character(r_cnt),
              is_suppressed = suppressed, stringsAsFactors = FALSE
            )
          }
        }
      }

      # Standard only
      std_only_pids <- unique(grp[[pcol]][grp[[src_col]] == 0 & grp[[std_col]] == 1])
      if (length(std_only_pids) > 0) {
        pid_df <- data.frame(pid = as.integer(std_only_pids))
        DBI::dbWriteTable(con, "_disagree_pids", pid_df, temporary = TRUE, overwrite = TRUE)
        res <- DBI::dbGetQuery(con, sprintf("
          SELECT 
            COALESCE(de.drug_source_value, 'None') AS drug_source_value,
            COALESCE(de.drug_concept_id, 0) AS drug_concept_id,
            COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
            COALESCE(c.concept_class_id, 'None') AS concept_class_id,
            COUNT(DISTINCT de.person_id) AS patient_count,
            COUNT(*) AS row_count
          FROM drug_exposure de
          JOIN _disagree_pids dp ON dp.pid = de.person_id
          LEFT JOIN %s c ON c.concept_id = de.drug_concept_id
          GROUP BY 1, 2, 3, 4
          ORDER BY patient_count DESC, row_count DESC
          LIMIT %d
        ", concept_table, as.integer(top_n)))
        try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _disagree_pids"), silent = TRUE)

        if (nrow(res) > 0) {
          for (r in seq_len(nrow(res))) {
            p_cnt <- as.integer(res$patient_count[r])
            r_cnt <- as.integer(res$row_count[r])
            suppressed <- p_cnt < min_cell_count
            records[[length(records) + 1L]] <- data.frame(
              site = site, medication_class = cls, disagreement_type = "standard_only",
              drug_source_value = res$drug_source_value[r],
              drug_concept_id = as.integer(res$drug_concept_id[r]),
              concept_name = res$concept_name[r],
              concept_class_id = res$concept_class_id[r],
              patient_count = if (suppressed) mask_value else as.character(p_cnt),
              row_count = if (r_cnt < min_cell_count) mask_value else as.character(r_cnt),
              is_suppressed = suppressed, stringsAsFactors = FALSE
            )
          }
        }
      }
    }
  }

  out <- if (length(records)) do.call(rbind, records) else data.frame()
  if (nrow(out) == 0) {
    return(data.frame(
      site = character(0), medication_class = character(0), disagreement_type = character(0),
      drug_source_value = character(0), drug_concept_id = integer(0), concept_name = character(0),
      concept_class_id = character(0), patient_count = character(0), row_count = character(0),
      is_suppressed = logical(0), stringsAsFactors = FALSE
    ))
  }
  rownames(out) <- NULL
  out
}

#' Export Medication Class Crosswalk from OMOP Hierarchy
#'
#' Traverses ATC classes and RxNorm ingredient hierarchies to extract descendant RxNorm concept
#' codes, plus historical/retired concepts that map to them via OMOP 'Maps to' relationships.
#'
#' @param con Active DuckDB DBI connection.
#' @param medication_specs Medication class specifications list.
#' @param output_path Optional file path to write exported CSV.
#' @param include_retired Whether to include older retired RxNorm codes mapped via 'Maps to' (default TRUE).
#' @param exclude_forms Optional vector of dosage form strings to exclude (e.g. 'Ophthalmic', 'Topical').
#' @param must_contain Optional vector of required RxNorm codes or source strings; throws error if missing.
#' @param vocab_schema Optional schema containing vocabulary tables.
#' @param vocab_db Optional attached database containing vocabulary tables.
#' @return A data frame with columns `source_value`, `drug_class`, `concept_id`, `concept_name`,
#'   `concept_class_id`, `vocabulary_id`, `source`.
#' @export
export_medication_crosswalk <- function(con, medication_specs, output_path = NULL,
                                        include_retired = TRUE, exclude_forms = NULL,
                                        must_contain = NULL, vocab_schema = NULL, vocab_db = NULL) {
  specs <- .med_normalize_specs(medication_specs)
  anc <- .med_resolve_table(con, "concept_ancestor", vocab_schema = vocab_schema, vocab_db = vocab_db)
  concept <- .med_resolve_table(con, "concept", vocab_schema = vocab_schema, vocab_db = vocab_db)
  rel <- tryCatch(.med_resolve_table(con, "concept_relationship", vocab_schema = vocab_schema, vocab_db = vocab_db), error = function(e) "concept_relationship")

  rows <- list()
  for (cls in names(specs)) {
    spec <- specs[[cls]]
    ids <- as.integer(c(spec$ancestor_concept_ids, spec$rxnorm_ingredient_ids))
    if (length(spec$atc_codes)) {
      codes <- paste(.med_q(spec$atc_codes), collapse = ", ")
      ids <- c(ids, DBI::dbGetQuery(con, sprintf(
        "SELECT concept_id FROM %s WHERE vocabulary_id LIKE 'ATC%%' AND concept_code IN (%s)", concept, codes))$concept_id)
    }
    if (length(ids)) {
      rows[[length(rows) + 1L]] <- data.frame(drug_class = cls, ancestor_concept_id = sort(unique(as.integer(ids))),
                                              stringsAsFactors = FALSE)
    }
  }

  if (length(rows) == 0) {
    stop("No ancestor concept IDs or ATC codes could be resolved from medication_specs.")
  }

  anc_df <- do.call(rbind, rows)
  DBI::dbWriteTable(con, "_exp_anc_df", anc_df, temporary = TRUE, overwrite = TRUE)
  on.exit(try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _exp_anc_df"), silent = TRUE), add = TRUE)

  curr_sql <- sprintf("
    SELECT DISTINCT 
      c.concept_code AS source_value,
      a.drug_class,
      c.concept_id,
      c.concept_name,
      c.concept_class_id,
      c.vocabulary_id,
      'current' AS source
    FROM _exp_anc_df a
    JOIN %s ca ON ca.ancestor_concept_id = a.ancestor_concept_id
    JOIN %s c ON c.concept_id = ca.descendant_concept_id
  ", anc, concept)
  curr_df <- DBI::dbGetQuery(con, curr_sql)
  all_dfs <- list(curr_df)

  if (isTRUE(include_retired) && nrow(curr_df) > 0) {
    desc_ids <- paste(as.integer(unique(curr_df$concept_id)), collapse = ", ")
    ret_sql <- sprintf("
      SELECT DISTINCT 
        c_old.concept_code AS source_value,
        a.drug_class,
        c_old.concept_id,
        c_old.concept_name,
        c_old.concept_class_id,
        c_old.vocabulary_id,
        'maps_to' AS source
      FROM %s cr
      JOIN %s c_old ON c_old.concept_id = cr.concept_id_1
      JOIN _exp_anc_df a ON 1=1
      JOIN %s ca ON ca.ancestor_concept_id = a.ancestor_concept_id AND ca.descendant_concept_id = cr.concept_id_2
      WHERE cr.relationship_id = 'Maps to'
        AND cr.concept_id_2 IN (%s)
        AND c_old.concept_id NOT IN (%s)
    ", rel, concept, anc, desc_ids, desc_ids)
    ret_df <- tryCatch(DBI::dbGetQuery(con, ret_sql), error = function(e) data.frame())
    if (nrow(ret_df) > 0) {
      all_dfs[[length(all_dfs) + 1L]] <- ret_df
    }
  }

  out <- unique(do.call(rbind, all_dfs))

  if (!is.null(exclude_forms) && length(exclude_forms) > 0 && nrow(out) > 0) {
    for (f in exclude_forms) {
      out <- out[!grepl(tolower(f), tolower(out$concept_name), fixed = TRUE), , drop = FALSE]
    }
  }

  if (!is.null(must_contain) && length(must_contain) > 0) {
    present_codes <- toupper(trimws(as.character(out$source_value)))
    req_codes <- toupper(trimws(as.character(must_contain)))
    missing <- setdiff(req_codes, present_codes)
    if (length(missing) > 0) {
      stop(sprintf("Export crosswalk validation failed; missing required codes: %s", paste(missing, collapse = ", ")))
    }
  }

  if (!is.null(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    utils::write.csv(out, output_path, row.names = FALSE)
  }

  rownames(out) <- NULL
  out
}

#' Medication exposure engine bound to a connection and drug-class specs
#'
#' A thin stateful front-end over [extract_medication_classes()]: RxNorm / ATC `concept_ancestor` roll-up
#' (`strategy = "standard"`), a local NDC / CUI crosswalk (`"source"`), or both, framed in time with
#' `window = "pre_admission"`, `"inpatient"` or `"discharge"`. Mirror of `omop_etl.MedicationExtractor`.
#'
#' @param con DuckDB DBI connection.
#' @param medication_specs See [extract_medication_classes()].
#' @param strategy,crosswalk See [extract_medication_classes()]. Default strategy `"standard"`.
#' @return A list with functions `extract(cohort_table, window = NULL, ...)` and
#'   `extract_all_windows(cohort_table, ...)` (one data frame with `<class>__<window>` flag columns).
#' @export
medication_extractor <- function(con, medication_specs, strategy = "standard", crosswalk = NULL) {
  windows <- c("pre_admission", "inpatient", "discharge")
  extract <- function(cohort_table, window = NULL, ...) {
    extract_medication_classes(con, cohort_table, medication_specs, strategy = strategy, crosswalk = crosswalk,
                               window = window, ...)
  }
  extract_all_windows <- function(cohort_table, ...) {
    out <- NULL
    for (w in windows) {
      df <- extract(cohort_table, window = w, ...)
      flag <- df[, names(medication_specs), drop = FALSE]
      names(flag) <- paste0(names(flag), "__", w)
      keep <- df[, setdiff(names(df), c(names(medication_specs), grep("_(source|standard)$", names(df), value = TRUE))), drop = FALSE]
      out <- if (is.null(out)) cbind(keep, flag) else cbind(out, flag)
    }
    out
  }
  structure(list(extract = extract, extract_all_windows = extract_all_windows), class = "medication_extractor")
}

#' Transitions-of-care medication trajectories and trajectory summary statistics
#'
#' @param data Cohort data frame with transition columns.
#' @param classes Character vector of medication class names.
#' @return A list with S3 class `medication_transitions` exposing `$data`, `$classes`,
#'   `$summary()`, `$transition_matrix(drug_class)`, and `$to_dataframe()`.
#' @export
medication_transitions <- function(data, classes) {
  classes <- as.character(classes)
  summary_fn <- function() {
    n <- nrow(data)
    rows <- list()
    for (c in classes) {
      pre_col <- paste0(c, "_pre_admission")
      inp_col <- paste0(c, "_inpatient")
      dis_col <- paste0(c, "_discharge")
      init_col <- paste0(c, "_initiated_inpatient")
      disc_col <- paste0(c, "_discontinued_at_discharge")
      cont_col <- paste0(c, "_continued_across_stay")

      pre <- if (pre_col %in% names(data)) sum(data[[pre_col]], na.rm = TRUE) else 0L
      inp <- if (inp_col %in% names(data)) sum(data[[inp_col]], na.rm = TRUE) else 0L
      dis <- if (dis_col %in% names(data)) sum(data[[dis_col]], na.rm = TRUE) else 0L
      init <- if (init_col %in% names(data)) sum(data[[init_col]], na.rm = TRUE) else 0L
      disc <- if (disc_col %in% names(data)) sum(data[[disc_col]], na.rm = TRUE) else 0L
      cont <- if (cont_col %in% names(data)) sum(data[[cont_col]], na.rm = TRUE) else 0L

      rows[[length(rows) + 1]] <- data.frame(
        medication_class = c,
        total_patients = n,
        pre_admission_n = as.integer(pre),
        inpatient_n = as.integer(inp),
        discharge_n = as.integer(dis),
        initiated_inpatient_n = as.integer(init),
        discontinued_at_discharge_n = as.integer(disc),
        continued_across_stay_n = as.integer(cont),
        pct_initiated = round(100.0 * init / max(1, n), 1),
        pct_discontinued = round(100.0 * disc / max(1, if (pre > 0) pre else n), 1),
        pct_continued = round(100.0 * cont / max(1, if (pre > 0) pre else n), 1),
        stringsAsFactors = FALSE
      )
    }
    do.call(rbind, rows)
  }

  transition_matrix_fn <- function(drug_class) {
    if (!drug_class %in% classes) {
      stop(sprintf("drug_class '%s' not in classes: %s", drug_class, paste(classes, collapse = ", ")), call. = FALSE)
    }
    pre_col <- paste0(drug_class, "_pre_admission")
    dis_col <- paste0(drug_class, "_discharge")
    table(Pre_Admission = data[[pre_col]], Discharge = data[[dis_col]])
  }

  obj <- list(
    data = data,
    classes = classes,
    summary = summary_fn,
    transition_matrix = transition_matrix_fn,
    to_dataframe = function() data
  )
  class(obj) <- "medication_transitions"
  obj
}

#' Extract medication trajectory flags across care transitions
#'
#' Evaluates drug exposure across three canonical transitions (`pre_admission`, `inpatient`, `discharge`)
#' to classify trajectory flags:
#' - `{cls}_initiated_inpatient`: 1 if unexposed pre-admission and exposed during inpatient or discharge.
#' - `{cls}_discontinued_at_discharge`: 1 if exposed pre-admission/inpatient and not active at discharge.
#' - `{cls}_continued_across_stay`: 1 if exposed both pre-admission and at discharge.
#'
#' Also populates summary trajectory flags:
#' - `med_initiated_inpatient`: 1 if ANY evaluated drug class was newly initiated.
#' - `med_discontinued_at_discharge`: 1 if ANY evaluated drug class was discontinued.
#' - `med_continued_across_stay`: 1 if ANY evaluated drug class was continued across stay.
#'
#' @param con Active DuckDB connection.
#' @param cohort_table Cohort table name.
#' @param medication_specs Drug-class specs mapping (defaults to DEFAULT_MEDICATION_CONCEPTS).
#' @param strategy Extraction strategy (`"standard"`, `"source"`, or `"both"`).
#' @param crosswalk Optional source-value crosswalk.
#' @param lookback_days Pre-admission lookback window in days (default 365).
#' @param person_id_col,index_date_col,end_date_col Column name overrides.
#' @return A `medication_transitions` object.
#' @export
extract_medication_transitions <- function(con, cohort_table = "cohort",
                                           medication_specs = NULL,
                                           strategy = "standard",
                                           crosswalk = NULL,
                                           lookback_days = 365,
                                           person_id_col = NULL,
                                           index_date_col = NULL,
                                           end_date_col = NULL) {
  if (is.null(medication_specs)) {
    medication_specs <- DEFAULT_MEDICATION_CONCEPTS
  }
  specs <- .med_normalize_specs(medication_specs)
  classes <- names(specs)

  extractor <- medication_extractor(con, medication_specs, strategy = strategy, crosswalk = crosswalk)

  df_pre <- extractor$extract(cohort_table, window = "pre_admission", lookback_days = lookback_days,
                              person_id_col = person_id_col, index_date_col = index_date_col, end_date_col = end_date_col)
  df_inp <- extractor$extract(cohort_table, window = "inpatient",
                              person_id_col = person_id_col, index_date_col = index_date_col, end_date_col = end_date_col)
  df_dis <- extractor$extract(cohort_table, window = "discharge",
                              person_id_col = person_id_col, index_date_col = index_date_col, end_date_col = end_date_col)

  base_cols <- setdiff(names(df_pre), c(classes, grep("_(source|standard)$", names(df_pre), value = TRUE)))
  out <- df_pre[, base_cols, drop = FALSE]

  n <- nrow(out)
  any_init <- rep(0L, n)
  any_disc <- rep(0L, n)
  any_cont <- rep(0L, n)

  for (c in classes) {
    pre_s <- as.integer(df_pre[[c]])
    inp_s <- as.integer(df_inp[[c]])
    dis_s <- as.integer(df_dis[[c]])

    init_s <- as.integer(pre_s == 0L & (inp_s == 1L | dis_s == 1L))
    disc_s <- as.integer((pre_s == 1L | inp_s == 1L) & dis_s == 0L)
    cont_s <- as.integer(pre_s == 1L & dis_s == 1L)

    out[[paste0(c, "_pre_admission")]] <- pre_s
    out[[paste0(c, "_inpatient")]] <- inp_s
    out[[paste0(c, "_discharge")]] <- dis_s
    out[[paste0(c, "_initiated_inpatient")]] <- init_s
    out[[paste0(c, "_discontinued_at_discharge")]] <- disc_s
    out[[paste0(c, "_continued_across_stay")]] <- cont_s

    any_init <- as.integer(any_init == 1L | init_s == 1L)
    any_disc <- as.integer(any_disc == 1L | disc_s == 1L)
    any_cont <- as.integer(any_cont == 1L | cont_s == 1L)
  }

  out$med_initiated_inpatient <- any_init
  out$med_discontinued_at_discharge <- any_disc
  out$med_continued_across_stay <- any_cont

  medication_transitions(out, classes)
}

