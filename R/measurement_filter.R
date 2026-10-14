#' Pluggable physiologic bounding and unit harmonization for measurements
#'
#' Mirror of `omop_etl.measurement_filter` in Python.

#' Default physiologic plausibility bounds
#'
#' Named list `feature -> c(low, high)`. Override single features through the `bounds` argument of
#' [filter_bounds()].
#' @export
DEFAULT_PHYSIOLOGIC_BOUNDS <- list(
  wbc = c(0.1, 200),           # 10^3/uL
  creatinine = c(0.1, 30),     # mg/dL
  sodium = c(100, 180),        # mEq/L
  potassium = c(1, 10),        # mEq/L
  hemoglobin = c(2, 25),       # g/dL
  glucose = c(10, 2000),       # mg/dL
  systolic_bp = c(40, 300),    # mmHg
  diastolic_bp = c(20, 200),   # mmHg
  bmi = c(10, 120)             # kg/m^2
)

#' Default unit-conversion rules
#'
#' Named list `feature -> list(canonical =, rules = list(list(pattern, multiplier), ...))`. Units are matched
#' against lower-cased, whitespace-free text (micro sign read as `u`).
#' @export
DEFAULT_UNIT_RULES <- local({
  r <- function(pattern, mult) list(pattern, mult)
  list(
    wbc = list(canonical = "10*3/uL", rules = list(
      r("^(/ul|/mcl|/mm3|/mm\\^3|cells/ul|cells/mm3|#/ul|10\\*0/ul)$", 0.001), r("^(10\\*6/l)$", 0.001),
      r("^(/l|cells/l)$", 1e-6),
      r("^(10\\*3/ul|10\\^3/ul|k/ul|x10\\*3/ul|x10\\^3/ul|thou/ul|10\\*9/l|10e9/l|g/l|10\\*3/mm3)$", 1))),
    platelets = list(canonical = "10*3/uL", rules = list(
      r("^(/ul|/mcl|/mm3|cells/ul)$", 0.001), r("^(10\\*3/ul|10\\^3/ul|k/ul|thou/ul|10\\*9/l|g/l)$", 1))),
    creatinine = list(canonical = "mg/dL", rules = list(
      r("^(umol/l|micromol/l)$", 1 / 88.42), r("^(mmol/l)$", 1000 / 88.42), r("^(mg/dl)$", 1))),
    glucose = list(canonical = "mg/dL", rules = list(r("^(mmol/l)$", 18.016), r("^(mg/dl)$", 1), r("^(g/l)$", 100))),
    hemoglobin = list(canonical = "g/dL", rules = list(r("^(g/l)$", 0.1), r("^(mmol/l)$", 1.611), r("^(g/dl)$", 1))),
    albumin = list(canonical = "g/dL", rules = list(r("^(g/l)$", 0.1), r("^(g/dl)$", 1))),
    bun = list(canonical = "mg/dL", rules = list(r("^(mmol/l)$", 2.801), r("^(mg/dl)$", 1))),
    calcium = list(canonical = "mg/dL", rules = list(r("^(mmol/l)$", 4.008), r("^(mg/dl)$", 1))),
    ldl = list(canonical = "mg/dL", rules = list(r("^(mmol/l)$", 38.67), r("^(mg/dl)$", 1))),
    bilirubin_total = list(canonical = "mg/dL", rules = list(r("^(umol/l)$", 1 / 17.1), r("^(mg/dl)$", 1))),
    lactate = list(canonical = "mmol/L", rules = list(r("^(mg/dl)$", 1 / 9.008), r("^(mmol/l|meq/l)$", 1)))
  )
})

#' Resolve bounds: defaults merged with user overrides
#'
#' @param bounds `NULL` / `TRUE` = defaults; `FALSE` = none; a named list overrides / extends per feature
#'   (`NULL` entry removes a feature's bound).
#' @return Named list `feature -> c(low, high)` (`NA` end = open).
#' @export
resolve_bounds <- function(bounds = NULL) {
  if (identical(bounds, FALSE)) return(list())
  merged <- DEFAULT_PHYSIOLOGIC_BOUNDS
  if (is.null(bounds) || identical(bounds, TRUE)) return(merged)
  for (k in names(bounds)) {
    v <- bounds[[k]]
    if (is.null(v)) {
      merged[[k]] <- NULL
      next
    }
    if (length(v) != 2) stop(sprintf("bounds[['%s']] must be c(low, high) (either may be NA).", k))
    merged[[k]] <- as.numeric(v)
  }
  merged
}

#' Apply physiologic bounds to a data frame
#'
#' @param data Wide data frame (one column per feature) when `feature_col` is `NULL`; otherwise a long frame
#'   with `feature_col` naming the feature and `value_col` the value.
#' @param bounds `NULL` = [DEFAULT_PHYSIOLOGIC_BOUNDS]; a named list overrides per feature
#'   (`list(wbc = c(0.5, 150))`); `FALSE` disables filtering. Inclusive; `NA` end = open.
#' @param action `"nullify"` (out-of-range -> `NA`; default), `"clip"` or `"drop"`.
#' @param feature_col,value_col See `data`.
#' @return The data frame with attribute `bounds_report` (`feature, low, high, n_checked, n_out_of_range`).
#' @export
filter_bounds <- function(data, bounds = NULL, action = c("nullify", "clip", "drop"), feature_col = NULL,
                          value_col = "value_as_number") {
  action <- match.arg(action)
  cfg <- resolve_bounds(bounds)
  df <- data
  report <- list()
  drop_mask <- rep(FALSE, nrow(df))
  oor <- function(x, lo, hi) {
    m <- rep(FALSE, length(x))
    if (!is.na(lo)) m <- m | (!is.na(x) & x < lo)
    if (!is.na(hi)) m <- m | (!is.na(x) & x > hi)
    m
  }
  clip <- function(x, lo, hi) {
    if (!is.na(lo)) x <- pmax(x, lo)
    if (!is.na(hi)) x <- pmin(x, hi)
    x
  }
  add <- function(feat, lo, hi, n, bad) {
    report[[length(report) + 1L]] <<- data.frame(feature = feat, low = lo, high = hi, n_checked = n,
                                                 n_out_of_range = bad, stringsAsFactors = FALSE)
  }
  if (is.null(feature_col)) {
    for (feat in names(cfg)) {
      if (!feat %in% names(df)) next
      x <- suppressWarnings(as.numeric(df[[feat]]))
      lo <- cfg[[feat]][1]
      hi <- cfg[[feat]][2]
      bad <- oor(x, lo, hi)
      add(feat, lo, hi, sum(!is.na(x)), sum(bad))
      if (action == "nullify") df[[feat]] <- ifelse(bad, NA_real_, x)
      else if (action == "clip") df[[feat]] <- clip(x, lo, hi)
      else drop_mask <- drop_mask | bad
    }
  } else {
    if (!all(c(feature_col, value_col) %in% names(df))) {
      stop(sprintf("Long frame needs columns '%s' and '%s'.", feature_col, value_col))
    }
    vals <- suppressWarnings(as.numeric(df[[value_col]]))
    for (feat in names(cfg)) {
      sel <- df[[feature_col]] == feat & !is.na(df[[feature_col]])
      if (!any(sel)) next
      lo <- cfg[[feat]][1]
      hi <- cfg[[feat]][2]
      bad <- sel & oor(vals, lo, hi)
      add(feat, lo, hi, sum(sel & !is.na(vals)), sum(bad))
      if (action == "nullify") vals[bad] <- NA_real_
      else if (action == "clip") vals[sel] <- clip(vals[sel], lo, hi)
      else drop_mask <- drop_mask | bad
    }
    df[[value_col]] <- vals
  }
  if (action == "drop") df <- df[!drop_mask, , drop = FALSE]
  rep_df <- if (length(report)) do.call(rbind, report) else
    data.frame(feature = character(0), low = numeric(0), high = numeric(0), n_checked = integer(0), n_out_of_range = integer(0))
  attr(df, "bounds_report") <- rep_df
  df
}

#' @keywords internal
#' @noRd
.norm_unit <- function(u) {
  if (is.null(u) || length(u) == 0 || is.na(u)) return("")
  u <- gsub("[[:space:]]+", "", tolower(as.character(u)))
  u <- gsub(intToUtf8(181), "u", u, fixed = TRUE)
  gsub(intToUtf8(956), "u", u, fixed = TRUE)
}

#' Unit-normalization preflight for the measurement table
#'
#' Inspects `unit_source_value` (else the `concept_name` of `unit_concept_id`) for every feature with a rule
#' and classifies the distinct units as scaled / canonical / no_unit / unrecognized. With `create_view`, a
#' temporary view `view_name` is created with `value_as_number` (and `range_low` / `range_high`) multiplied to
#' the canonical unit plus `harmonization_factor` and `unit_harmonized`. The source table is never modified,
#' and unrecognized units are left unscaled (factor 1) and flagged, never guessed. Feed the view to
#' [extract_measurements()] through `measurement_table`. Mirror of
#' `omop_etl.measurement_filter.harmonize_measurement_units`.
#'
#' @param con DuckDB DBI connection.
#' @param features Named list `feature -> LOINC codes / concept ids`; default `DEFAULT_LAB_LOINCS` +
#'   `DEFAULT_VITAL_LOINCS`.
#' @param unit_rules Named list overriding / extending [DEFAULT_UNIT_RULES].
#' @param measurement_table,view_name,create_view Source table, view name and whether to create the view.
#' @return Data frame `feature, unit, canonical_unit, factor, status, n`.
#' @export
harmonize_measurement_units <- function(con, features = NULL, unit_rules = NULL, measurement_table = "measurement",
                                        view_name = "measurement_harmonized", create_view = TRUE) {
  feats <- if (is.null(features)) c(DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS) else features
  rules <- DEFAULT_UNIT_RULES
  for (k in names(unit_rules)) rules[[k]] <- unit_rules[[k]]
  feats <- feats[names(feats) %in% names(rules)]
  empty_report <- data.frame(feature = character(0), unit = character(0), canonical_unit = character(0),
                             factor = numeric(0), status = character(0), n = integer(0), stringsAsFactors = FALSE)
  mcols <- tolower(DBI::dbGetQuery(con, paste0("DESCRIBE ", measurement_table))$column_name)
  if (length(feats) == 0) {
    if (create_view) {
      DBI::dbExecute(con, sprintf(paste0("CREATE OR REPLACE TEMPORARY VIEW %s AS SELECT *, 1.0 AS harmonization_factor, ",
                                         "CAST(NULL AS VARCHAR) AS unit_harmonized FROM %s"), view_name, measurement_table))
    }
    return(empty_report)
  }
  has_concept <- "concept" %in% DBI::dbGetQuery(con, "SELECT table_name FROM information_schema.tables")$table_name
  use_uc <- has_concept && "unit_concept_id" %in% mcols

  seeds <- do.call(rbind, lapply(names(feats), function(f) {
    data.frame(feature = f, code = as.character(unlist(feats[[f]])), stringsAsFactors = FALSE)
  }))
  DBI::dbWriteTable(con, "_hu_seeds", seeds, temporary = TRUE, overwrite = TRUE)
  on.exit(try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _hu_seeds"), silent = TRUE), add = TRUE)
  parts <- c(sprintf("SELECT s.feature, m.measurement_concept_id AS cid FROM _hu_seeds s JOIN %s m ON m.measurement_source_value = s.code",
                     measurement_table))
  if (has_concept) {
    parts <- c(parts, paste0("SELECT s.feature, c.concept_id FROM _hu_seeds s JOIN concept c ",
                             "ON c.vocabulary_id = 'LOINC' AND c.concept_code = s.code"))
  }
  parts <- c(parts, "SELECT s.feature, TRY_CAST(s.code AS BIGINT) FROM _hu_seeds s WHERE regexp_matches(s.code, '^[0-9]+$')")
  DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY TABLE _hu_map AS SELECT CAST(MIN(feature) AS VARCHAR) AS feature, ",
                             "CAST(cid AS BIGINT) AS cid FROM (", paste(parts, collapse = " UNION "),
                             ") WHERE cid IS NOT NULL GROUP BY cid"))
  unit_sql <- paste0("COALESCE(NULLIF(TRIM(m.unit_source_value), ''), ", if (use_uc) "uc.concept_name" else "NULL", ")")
  uj <- if (use_uc) "LEFT JOIN concept uc ON uc.concept_id = m.unit_concept_id" else ""
  seen <- DBI::dbGetQuery(con, sprintf(paste0("SELECT mp.feature, %s AS unit, COUNT(*) AS n FROM %s m ",
                                              "JOIN _hu_map mp ON mp.cid = m.measurement_concept_id %s ",
                                              "WHERE m.value_as_number IS NOT NULL GROUP BY 1, 2"),
                                       unit_sql, measurement_table, uj))
  rows <- lapply(seq_len(nrow(seen)), function(i) {
    spec <- rules[[seen$feature[i]]]
    nu <- .norm_unit(seen$unit[i])
    factor <- NA_real_
    status <- "unrecognized"
    if (nu == "") {
      factor <- 1
      status <- "no_unit"
    } else {
      for (rl in spec$rules) {
        if (grepl(rl[[1]], nu, perl = TRUE)) {
          factor <- rl[[2]]
          status <- if (rl[[2]] == 1) "canonical" else "scaled"
          break
        }
      }
    }
    data.frame(feature = seen$feature[i], unit = seen$unit[i], canonical_unit = spec$canonical,
               factor = if (is.na(factor)) 1 else factor, status = status, n = as.integer(seen$n[i]),
               stringsAsFactors = FALSE)
  })
  report <- if (length(rows)) do.call(rbind, rows) else empty_report

  if (create_view) {
    fac <- report[report$status == "scaled", c("feature", "unit", "factor"), drop = FALSE]
    fac$unit <- as.character(fac$unit)
    DBI::dbWriteTable(con, "_hu_fac_df", fac, temporary = TRUE, overwrite = TRUE)
    DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY TABLE _hu_fac AS SELECT CAST(feature AS VARCHAR) feature, ",
                               "CAST(unit AS VARCHAR) unit, CAST(factor AS DOUBLE) factor FROM _hu_fac_df"))
    canon <- data.frame(feature = names(feats), canonical_unit = vapply(names(feats), function(f) rules[[f]]$canonical, ""),
                        stringsAsFactors = FALSE)
    DBI::dbWriteTable(con, "_hu_canon_df", canon, temporary = TRUE, overwrite = TRUE)
    DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY TABLE _hu_canon AS SELECT CAST(feature AS VARCHAR) feature, ",
                               "CAST(canonical_unit AS VARCHAR) canonical_unit FROM _hu_canon_df"))
    scaled <- c("value_as_number", "range_low", "range_high")
    ord <- DBI::dbGetQuery(con, paste0("DESCRIBE ", measurement_table))$column_name
    sel <- paste(ifelse(tolower(ord) %in% scaled, sprintf("m.%s * COALESCE(f.factor, 1.0) AS %s", ord, ord),
                        paste0("m.", ord)), collapse = ", ")
    DBI::dbExecute(con, sprintf(paste0(
      "CREATE OR REPLACE TEMPORARY VIEW %s AS SELECT %s, COALESCE(f.factor, 1.0) AS harmonization_factor, ",
      "ca.canonical_unit AS unit_harmonized FROM %s m ",
      "LEFT JOIN _hu_map mp ON mp.cid = m.measurement_concept_id %s ",
      "LEFT JOIN _hu_fac f ON f.feature = mp.feature AND f.unit = %s ",
      "LEFT JOIN _hu_canon ca ON ca.feature = f.feature"), view_name, sel, measurement_table, uj, unit_sql))
  }
  report
}
