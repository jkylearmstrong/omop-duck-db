#' Area-level geography features (RUCA, SDI) for model replication
#'
#' OMOP CDM v5.4 stores only raw address fields; it defines no RUCA or SDI. These functions derive both for each
#' index stay from the address in effect at admission (`person_address_history`, filled by [load_location()]; without
#' it `person.location_id` is used), a USDA ZIP -> RUCA crosswalk and a Robert Graham Center ZCTA -> SDI table, using
#' the **fixed SDI quintile cut points of the training cohort**. Cutting at quantiles of the local population would move
#' patients between quintiles whenever the local SDI distribution differs from training, so
#' [derive_geography_features()] errors without `sdi_cut_points` unless `fit_cut_points = TRUE` (training site only; the
#' fitted cuts are returned in `attr(result, "sdi_cut_points")`).
#'
#' Address rule (reference study): the address whose period contains the admission date; else the latest that started
#' before it; else the earliest that started after it. Addresses with a ZIP beat those without; ties go to
#' `preferred = 'Y'`, then the nearest start date. RUCA: primary code 1 = Metropolitan, 2 = Micropolitan, 3+ = Small Town
#' / Rural (USDA 99 "not coded" lands in Small Town / Rural as in the reference; `code_99_missing = TRUE` treats it as
#' unknown).
#'
#' @name geography
#' @param code Primary RUCA code (numeric or string such as `"4.2"`).
#' @param code_99_missing Logical; treat RUCA 99 as unknown.
#' @return `ruca_category()`: a character vector of categories.
#' @export
ruca_category <- function(code, code_99_missing = FALSE) {
  p <- suppressWarnings(as.integer(floor(as.numeric(as.character(code)))))
  out <- rep("RUCA: Small Town / Rural (3+)", length(p))
  out[!is.na(p) & p == 1L] <- "RUCA: Metropolitan (1.0)"
  out[!is.na(p) & p == 2L] <- "RUCA: Micropolitan (2.0)"
  out[is.na(p) | p < 1L | (isTRUE(code_99_missing) & p == 99L)] <- "RUCA: Missing / Unknown"
  out
}

.sdi_labels <- c("SDI Quintile 1 (Least Deprived)", "SDI Quintile 2", "SDI Quintile 3", "SDI Quintile 4",
                 "SDI Quintile 5 (Most Deprived)")

#' @rdname geography
#' @param cut_points Four finite, strictly ascending SDI scores.
#' @return `validate_sdi_cut_points()`: the cut points as numeric, or an error.
#' @export
validate_sdi_cut_points <- function(cut_points) {
  cuts <- suppressWarnings(as.numeric(cut_points))
  if (length(cuts) != 4L || any(!is.finite(cuts)) || any(diff(cuts) <= 0)) {
    stop("sdi_cut_points must be four finite, strictly ascending SDI scores; got ", paste(cut_points, collapse = ", "), ".", call. = FALSE)
  }
  cuts
}

#' @rdname geography
#' @param scores Numeric SDI scores.
#' @return `fit_sdi_cut_points()`: the 20/40/60/80th percentiles of the known scores (type 7, like NumPy).
#' @export
fit_sdi_cut_points <- function(scores) {
  s <- suppressWarnings(as.numeric(scores))
  s <- s[is.finite(s)]
  if (length(s) < 5L) stop("Need at least 5 non-missing SDI scores to fit quintile cut points.", call. = FALSE)
  validate_sdi_cut_points(unname(stats::quantile(s, c(0.2, 0.4, 0.6, 0.8), type = 7)))
}

#' @rdname geography
#' @return `assign_sdi_quintile()`: quintile labels; a score equal to a cut point goes to the higher quintile.
#' @export
assign_sdi_quintile <- function(scores, cut_points) {
  cuts <- validate_sdi_cut_points(cut_points)
  s <- suppressWarnings(as.numeric(scores))
  out <- rep("SDI: Missing / Unknown", length(s))
  known <- is.finite(s)
  out[known] <- .sdi_labels[findInterval(s[known], cuts) + 1L]
  out
}

.geo_table <- function(source, aliases, what) {
  df <- if (is.data.frame(source)) source else utils::read.csv(source, colClasses = "character", check.names = FALSE)
  lower <- tolower(names(df))
  out <- list()
  for (target in names(aliases)) {
    hit <- match(TRUE, lower %in% aliases[[target]])
    if (is.na(hit)) stop(what, " needs a column named one of ", paste(aliases[[target]], collapse = ", "), "; found ", paste(names(df), collapse = ", "), ".", call. = FALSE)
    out[[target]] <- df[[hit]]
  }
  as.data.frame(out, stringsAsFactors = FALSE)
}

.zip5 <- function(x) {
  x <- trimws(as.character(x))
  x <- ifelse(grepl("^[0-9]{1,4}$", x), formatC(as.integer(x), width = 5, flag = "0"), x)
  m <- regmatches(x, regexpr("[0-9]{5}", x))
  out <- rep(NA_character_, length(x))
  out[grepl("[0-9]{5}", x)] <- m
  out
}

#' @rdname geography
#' @param source A data frame or CSV path.
#' @return `read_ruca_crosswalk()`: data frame `zip5, ruca_code`. `read_sdi_table()`: data frame `zip5, sdi_score`.
#' @export
read_ruca_crosswalk <- function(source) {
  d <- .geo_table(source, list(zip5 = c("zipcode", "zip", "zip5", "zip_code"),
                               ruca_code = c("ruca_code", "ruca", "ruca1", "primary_ruca")), "RUCA crosswalk")
  d$zip5 <- .zip5(d$zip5)
  d$ruca_code <- suppressWarnings(as.numeric(d$ruca_code))
  d <- d[!is.na(d$zip5) & !duplicated(d$zip5), , drop = FALSE]
  rownames(d) <- NULL
  d
}

#' @rdname geography
#' @export
read_sdi_table <- function(source) {
  d <- .geo_table(source, list(zip5 = c("zcta5_fips", "zcta5", "zcta", "zip", "zip5"), sdi_score = c("sdi_score", "sdi")), "SDI table")
  d$zip5 <- .zip5(d$zip5)
  d$sdi_score <- suppressWarnings(as.numeric(d$sdi_score))
  d <- d[!is.na(d$zip5) & !duplicated(d$zip5), , drop = FALSE]
  rownames(d) <- NULL
  d
}

.geo_has_table <- function(con, name) {
  DBI::dbGetQuery(con, "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ?",
                  params = list(name))$n[[1]] > 0
}

#' @rdname geography
#' @param con A DBI connection to an OMOP DuckDB.
#' @param ruca_crosswalk,sdi_table Data frame or CSV path (see `read_ruca_crosswalk()`, `read_sdi_table()`).
#' @param sdi_cut_points Fixed SDI quintile cut points from the training cohort; required unless `fit_cut_points`.
#' @param stays Optional data frame with `person_id`, `admit_date` and optionally a unique `stay_id`; defaults to every
#'   inpatient visit (concepts 9201, 262) with `stay_id = visit_occurrence_id`.
#' @param address_table `"auto"` (use `person_address_history` when present and non-empty, else `person.location_id`),
#'   a table name, or `NULL` for the person's location.
#' @param fit_cut_points Logical; fit cut points from these stays (training reference only).
#' @return `derive_geography_features()`: data frame `person_id, stay_id, zip5, rule, ruca_code, ruca_category,
#'   sdi_score, sdi_quintile`, with the cut points used in `attr(, "sdi_cut_points")`.
#' @export
derive_geography_features <- function(con, ruca_crosswalk, sdi_table, sdi_cut_points = NULL, stays = NULL,
                                      address_table = "auto", fit_cut_points = FALSE, code_99_missing = FALSE) {
  if (is.null(sdi_cut_points) && !isTRUE(fit_cut_points)) {
    stop("sdi_cut_points is required: SDI quintiles must use the training cohort's fixed cut points, not quantiles of the ",
         "local population. Pass fit_cut_points = TRUE only when building the training reference.", call. = FALSE)
  }
  cuts <- if (!is.null(sdi_cut_points)) validate_sdi_cut_points(sdi_cut_points) else NULL
  ruca <- read_ruca_crosswalk(ruca_crosswalk)
  sdi <- read_sdi_table(sdi_table)

  if (is.null(stays)) {
    stays <- DBI::dbGetQuery(con, "SELECT person_id, visit_occurrence_id AS stay_id, visit_start_date AS admit_date
                                   FROM visit_occurrence WHERE visit_concept_id IN (9201, 262)")
  } else {
    if (!"stay_id" %in% names(stays)) stays$stay_id <- seq_len(nrow(stays))
    miss <- setdiff(c("person_id", "admit_date"), names(stays))
    if (length(miss)) stop("stays is missing column(s) ", paste(miss, collapse = ", "), ".", call. = FALSE)
    if (anyDuplicated(stays$stay_id)) stop("stays.stay_id must be unique.", call. = FALSE)
    stays <- stays[, c("person_id", "stay_id", "admit_date")]
  }
  stays$admit_date <- as.Date(stays$admit_date)
  tbl <- address_table
  if (identical(tbl, "auto")) {
    tbl <- if (.geo_has_table(con, "person_address_history") &&
               DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person_address_history")$n[[1]] > 0) "person_address_history" else NULL
  }
  if (!is.null(tbl) && !grepl("^[A-Za-z0-9_]+$", tbl)) stop("invalid address table name ", shQuote(tbl), ".", call. = FALSE)

  duckdb::duckdb_register(con, "_geo_stays", stays)
  on.exit(duckdb::duckdb_unregister(con, "_geo_stays"), add = TRUE)
  picked <- if (is.null(tbl)) {
    DBI::dbGetQuery(con, "
      SELECT person_id, stay_id, zip5, CASE WHEN zip5 IS NULL THEN 'no_zip' ELSE 'person_location' END AS rule
      FROM (SELECT s.person_id, s.stay_id, nullif(regexp_extract(CAST(l.zip AS VARCHAR), '(\\d{5})', 1), '') AS zip5
            FROM _geo_stays s LEFT JOIN person p ON p.person_id = s.person_id
            LEFT JOIN location l ON l.location_id = p.location_id)")
  } else {
    DBI::dbGetQuery(con, sprintf("
      WITH m AS (
        SELECT s.person_id, s.stay_id, s.admit_date, a.zip5, a.address_start AS st, a.preferred, (a.zip5 IS NULL) AS nozip,
               (a.address_start IS NULL OR a.address_start <= s.admit_date) AS started,
               (a.address_end IS NULL OR a.address_end >= s.admit_date) AS ongoing
        FROM _geo_stays s LEFT JOIN %s a ON a.person_id = s.person_id
      ), r AS (
        SELECT *, CASE WHEN started AND ongoing THEN 'in_effect' WHEN started THEN 'latest_before' ELSE 'earliest_after' END AS rule0 FROM m
      )
      SELECT person_id, stay_id, zip5, CASE WHEN zip5 IS NULL THEN 'no_zip' ELSE rule0 END AS rule
      FROM r
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY stay_id
        ORDER BY nozip,
                 CASE rule0 WHEN 'in_effect' THEN 0 WHEN 'latest_before' THEN 1 ELSE 2 END,
                 CASE WHEN upper(coalesce(preferred, '')) = 'Y' THEN 0 ELSE 1 END,
                 CASE WHEN rule0 = 'earliest_after' THEN date_diff('day', admit_date, st)
                      ELSE coalesce(date_diff('day', st, admit_date), 1000000) END,
                 st DESC NULLS LAST) = 1", tbl))
  }
  g <- merge(merge(picked, ruca, by = "zip5", all.x = TRUE, sort = FALSE), sdi, by = "zip5", all.x = TRUE, sort = FALSE)
  g <- g[order(g$stay_id), , drop = FALSE]
  g$ruca_category <- ruca_category(g$ruca_code, code_99_missing = code_99_missing)
  if (is.null(cuts)) cuts <- fit_sdi_cut_points(g$sdi_score)
  g$sdi_quintile <- assign_sdi_quintile(g$sdi_score, cuts)
  g <- g[, c("person_id", "stay_id", "zip5", "rule", "ruca_code", "ruca_category", "sdi_score", "sdi_quintile")]
  rownames(g) <- NULL
  attr(g, "sdi_cut_points") <- cuts
  g
}
