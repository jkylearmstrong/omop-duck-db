#' Local risk-driver explanations for scored rows
#'
#' Swaps each feature for its reference value (the feature spec's `impute`, else 0) and reports the change in the final
#' calibrated probability as the feature's `impact` (positive = pushes risk up). Model-agnostic one-at-a-time occlusion, not
#' interaction-aware TreeSHAP. Mirror of Python `omop_etl.model_bundle.explain_rows`.
#'
#' @param bundle A `model_bundle` or bundle directory.
#' @param features data.frame holding the bundle's feature columns (already imputed, no `NA`).
#' @param top_k Number of drivers per row.
#' @return A list (one element per row) of lists with `feature`, `name`, `value`, `impact`, ordered by absolute impact.
#' @export
explain_rows <- function(bundle, features, top_k = 5L) {
  if (is.character(bundle)) bundle <- load_model_bundle(bundle)
  spec <- bundle$feature_spec$features
  nm <- vapply(spec, function(f) f$name, character(1))
  ref <- vapply(spec, function(f) if (is.null(f$impute)) 0 else as.numeric(f$impute), numeric(1))
  X <- as.matrix(features[, nm, drop = FALSE]); storage.mode(X) <- "double"
  base <- bundle_predict_proba(bundle, X)
  delta <- matrix(0, nrow(X), ncol(X))
  for (j in seq_along(nm)) {
    Xo <- X; Xo[, j] <- ref[[j]]
    delta[, j] <- base - bundle_predict_proba(bundle, Xo)
  }
  lapply(seq_len(nrow(X)), function(i) {
    ord <- order(-abs(delta[i, ]), seq_along(nm))
    ord <- utils::head(ord[delta[i, ord] != 0], top_k)
    lapply(ord, function(j) list(feature = if (is.null(spec[[j]]$label)) nm[[j]] else spec[[j]]$label, name = nm[[j]],
                                 value = unname(X[i, j]), impact = delta[i, j]))
  })
}

#' Pre-scoring unit sanity check
#'
#' Flags lab/vital features whose median is more than `max_ratio` times above or below the reference median, which is how an
#' unharmonized unit (mmol/L or umol/L where mg/dL is expected) shows up before physiologic bounds silently clip it.
#' Mirror of Python `omop_etl.unit_guard.check_unit_sanity`.
#'
#' @param features data.frame of features.
#' @param reference_medians Named numeric overriding/adding to [REFERENCE_MEDIANS].
#' @param max_ratio Flag when `site/reference` is above this or below its inverse.
#' @param min_n Skip features with fewer non-missing values.
#' @param warn Emit a warning naming flagged features.
#' @return data.frame: `feature`, `n`, `site_median`, `reference_median`, `ratio`, `flag`, `hint`, `suspect_factor`
#'   (the multiplier that brings a flagged column to the contract unit, `NA` when no named conversion matches).
#' @export
check_unit_sanity <- function(features, reference_medians = NULL, max_ratio = 3, min_n = 20L, warn = TRUE) {
  ref <- utils::modifyList(as.list(REFERENCE_MEDIANS), as.list(reference_medians))
  suspects <- list(lab_creatinine = list(c(88.4, "umol/L reported instead of mg/dL"), c(1 / 88.4, "inverse of the umol/L scale")),
                   lab_bun = list(c(0.357, "mmol/L urea reported instead of mg/dL BUN"), c(2.801, "inverse of the mmol/L urea scale")),
                   lab_glucose = list(c(0.0555, "mmol/L reported instead of mg/dL"), c(18, "inverse of the mmol/L scale")))
  rows <- list()
  for (n in names(ref)) {
    if (!n %in% names(features) || !isTRUE(ref[[n]] != 0)) next
    v <- suppressWarnings(as.numeric(features[[n]])); v <- v[is.finite(v)]
    if (length(v) < min_n) next
    med <- stats::median(v); ratio <- med / ref[[n]]
    flag <- is.finite(ratio) && (ratio > max_ratio || ratio < 1 / max_ratio)
    hint <- ""; factor <- NA_real_
    if (flag) {
      hint <- "unharmonized unit or scale suspected"
      for (s in suspects[[n]]) if (abs(log(max(ratio, 1e-9) / as.numeric(s[[1]]))) < log(1.6)) {
        hint <- s[[2]]; factor <- 1 / as.numeric(s[[1]]); break
      }
    }
    rows[[length(rows) + 1L]] <- data.frame(feature = n, n = length(v), site_median = med, reference_median = ref[[n]],
                                            ratio = ratio, flag = flag, hint = hint, suspect_factor = factor,
                                            stringsAsFactors = FALSE)
  }
  out <- if (length(rows)) do.call(rbind, rows) else
    data.frame(feature = character(), n = integer(), site_median = numeric(), reference_median = numeric(),
               ratio = numeric(), flag = logical(), hint = character(), suspect_factor = numeric(), stringsAsFactors = FALSE)
  if (warn && any(out$flag)) {
    b <- out[out$flag, ]
    warning("unit sanity: ", paste(sprintf("%s median %.3g vs reference %.3g%s", b$feature, b$site_median, b$reference_median,
                                           ifelse(nzchar(b$hint), paste0(" (", b$hint, ")"), "")), collapse = "; "), call. = FALSE)
  }
  out
}

#' Rescale flagged features to the contract units
#'
#' Multiplies every feature flagged by [check_unit_sanity()] whose median matches a named conversion (umol/L creatinine, mmol/L
#' urea, mmol/L glucose, ...) by its `suspect_factor`. Features flagged for an unexplained reason are left untouched and listed
#' with `rescaled = FALSE` for review. `features` is not modified. Mirror of Python `omop_etl.unit_guard.harmonize_contract_units`.
#'
#' @param features data.frame of features.
#' @param sanity Output of [check_unit_sanity()]; computed here (without a warning) when `NULL`.
#' @param reference_medians Passed to [check_unit_sanity()] when `sanity` is `NULL`.
#' @return A list: `features` (the rescaled data.frame) and `applied` (data.frame: `feature`, `factor`, `rescaled`, `hint`).
#' @export
harmonize_contract_units <- function(features, sanity = NULL, reference_medians = NULL) {
  if (is.null(sanity)) sanity <- check_unit_sanity(features, reference_medians = reference_medians, warn = FALSE)
  bad <- sanity[sanity$flag, , drop = FALSE]
  fixed <- features
  ok <- logical(nrow(bad))
  for (i in seq_len(nrow(bad))) {
    f <- bad$suspect_factor[[i]]; n <- bad$feature[[i]]
    ok[[i]] <- is.finite(f) && n %in% names(fixed)
    if (ok[[i]]) fixed[[n]] <- suppressWarnings(as.numeric(fixed[[n]])) * f
  }
  list(features = fixed,
       applied = data.frame(feature = as.character(bad$feature), factor = as.numeric(bad$suspect_factor), rescaled = ok,
                            hint = as.character(bad$hint), stringsAsFactors = FALSE))
}

#' Typical adult inpatient reference medians (contract units)
#' @format Named numeric. Override with the consortium's own medians via `check_unit_sanity(reference_medians =)`.
#' @export
REFERENCE_MEDIANS <- c(lab_creatinine = 1, lab_bun = 18, lab_sodium = 139, lab_potassium = 4.1, lab_chloride = 103,
                       lab_bicarbonate = 24, lab_anion_gap = 10, lab_egfr = 70, lab_glucose = 110,
                       heart_rate = 80, systolic_bp = 125, diastolic_bp = 70)

#' LOINC code to model feature, used by [score_fhir_bundle()]
#' @format Named character.
#' @export
LOINC_TO_FEATURE <- c("8867-4" = "heart_rate", "8480-6" = "systolic_bp", "8462-4" = "diastolic_bp", "3094-0" = "lab_bun",
                      "2160-0" = "lab_creatinine", "38483-4" = "lab_creatinine", "2951-2" = "lab_sodium", "2823-3" = "lab_potassium",
                      "2075-0" = "lab_chloride", "1963-8" = "lab_bicarbonate", "2028-9" = "lab_bicarbonate",
                      "33037-3" = "lab_anion_gap", "62238-1" = "lab_egfr", "33914-3" = "lab_egfr",
                      "2345-7" = "lab_glucose", "2339-0" = "lab_glucose")

.fhir_codings <- function(res, field = "code") {
  blk <- res[[field]]
  if (is.null(blk)) return(list())
  if (is.null(names(blk)) && length(blk)) blk <- blk[[1]]
  lapply(blk$coding, function(c) list(system = if (is.null(c$system)) "" else c$system, code = if (is.null(c$code)) "" else c$code))
}
.fhir_date <- function(x) if (is.null(x) || !nzchar(x)) NULL else as.Date(substr(x, 1, 10))

#' FHIR system URIs and the OMOP vocabulary each one selects
#' @format Named character.
#' @export
VOCABULARY_BY_SYSTEM <- c("http://snomed.info/sct" = "SNOMED", "http://www.nlm.nih.gov/research/umls/rxnorm" = "RxNorm",
                          "http://hl7.org/fhir/sid/icd-10-cm" = "ICD10CM", "http://hl7.org/fhir/sid/icd-10" = "ICD10",
                          "http://hl7.org/fhir/sid/icd-9-cm" = "ICD9CM", "http://hl7.org/fhir/sid/ndc" = "NDC",
                          "http://www.ama-assn.org/go/cpt" = "CPT4")

#' Resolve FHIR codes to standard OMOP concept ids
#'
#' Looks `"system|code"` strings up in the `concept` table of a vocabulary connection. The system URI selects the vocabulary
#' ([VOCABULARY_BY_SYSTEM]); a standard concept is used as is, otherwise the valid `Maps to` relationship is followed (when
#' `concept_relationship` exists). Unknown systems and codes with no standard target are omitted. Mirror of Python
#' `omop_etl.fhir.resolve_codes`.
#'
#' @param vocab_con A DBI connection holding the OMOP vocabulary tables.
#' @param codes Character vector of `"system|code"`.
#' @return A named list `"system|code" = concept_id`.
#' @export
resolve_fhir_codes <- function(vocab_con, codes) {
  codes <- unique(as.character(codes))
  sys <- sub("\\|.*$", "", codes); code <- sub("^[^|]*\\|", "", codes)
  keep <- sys %in% names(VOCABULARY_BY_SYSTEM) & nzchar(code) & grepl("|", codes, fixed = TRUE)
  if (!any(keep)) return(list())
  codes <- codes[keep]; sys <- sys[keep]; code <- code[keep]
  tabs <- tolower(DBI::dbListTables(vocab_con))
  if (!"concept" %in% tabs) stop("The vocabulary connection has no 'concept' table.", call. = FALSE)
  qs <- function(x) as.character(DBI::dbQuoteString(vocab_con, x))
  vals <- paste(sprintf("(%s, %s, %s)", qs(codes), qs(unname(VOCABULARY_BY_SYSTEM[sys])), qs(code)), collapse = ", ")
  if ("concept_relationship" %in% tabs) {
    join <- paste("LEFT JOIN concept_relationship r ON r.concept_id_1 = c.concept_id AND r.relationship_id = 'Maps to'",
                  "AND r.invalid_reason IS NULL")
    target <- "CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE r.concept_id_2 END"
  } else {
    join <- ""; target <- "CASE WHEN c.standard_concept = 'S' THEN c.concept_id END"
  }
  r <- DBI::dbGetQuery(vocab_con, sprintf(
    "SELECT q.k, MIN(%s) AS cid FROM (VALUES %s) AS q(k, vocabulary_id, concept_code) JOIN concept c ON c.vocabulary_id = q.vocabulary_id AND c.concept_code = q.concept_code AND c.invalid_reason IS NULL %s GROUP BY q.k",
    target, vals, join))
  r <- r[!is.na(r$cid), , drop = FALSE]
  stats::setNames(as.list(as.integer(r$cid)), r$k)
}

#' @rdname resolve_fhir_codes
#' @export
resolve_codes <- resolve_fhir_codes

#' Score a FHIR Bundle without a pre-built database
#'
#' Loads the Patient, Encounter, Observation, Condition and MedicationRequest resources of a FHIR R4 Bundle into a throw-away
#' temporary DuckDB CDM (deleted on exit) and runs [score_encounter()]. Observations are matched on LOINC and creatinine / BUN
#' are converted from SI units. Condition and medication codes are loaded only when `concept_map` (`c("system|code" = concept_id)`)
#' maps them or, when `vocab_con` / `vocab_db_path` is given, [resolve_fhir_codes()] finds them in the OMOP vocabulary; the rest are
#' returned in `fhir$unmapped_codes`. Mirror of Python `omop_etl.fhir.score_fhir_bundle`.
#'
#' @param fhir_json A FHIR Bundle as a JSON string, a file path, or a parsed list.
#' @param model_bundle A `model_bundle` or bundle directory.
#' @param encounter_id Encounter `id` to score (default: the latest by start).
#' @param as_of,concept_map,explain,top_k See [score_encounter()].
#' @param vocab_con A DBI connection with the OMOP vocabulary, used for codes not in `concept_map`.
#' @param vocab_db_path Path of a DuckDB vocabulary file (opened read-only) used when `vocab_con` is `NULL`.
#' @param ... Further arguments to [score_encounter()].
#' @return As [score_encounter()] plus a `fhir` element.
#' @export
score_fhir_bundle <- function(fhir_json, model_bundle, encounter_id = NULL, as_of = NULL, concept_map = NULL,
                              explain = FALSE, top_k = 5L, vocab_con = NULL, vocab_db_path = NULL, ...) {
  b <- if (is.list(fhir_json)) fhir_json else if (file.exists(fhir_json)) jsonlite::fromJSON(fhir_json, simplifyVector = FALSE) else
    jsonlite::fromJSON(fhir_json, simplifyVector = FALSE)
  if (!identical(b$resourceType, "Bundle")) stop("score_fhir_bundle expects a FHIR Bundle (resourceType 'Bundle').", call. = FALSE)
  res <- split(lapply(b$entry, `[[`, "resource"), vapply(b$entry, function(e) as.character(e$resource$resourceType), character(1)))
  if (is.null(res$Patient)) stop("Bundle has no Patient resource.", call. = FALSE)
  if (is.null(res$Encounter)) stop("Bundle has no Encounter resource.", call. = FALSE)
  patient <- res$Patient[[1]]
  encs <- res$Encounter
  if (!is.null(encounter_id)) {
    encs <- Filter(function(x) identical(x$id, encounter_id), encs)
    if (!length(encs)) stop("Encounter '", encounter_id, "' not in the bundle.", call. = FALSE)
  }
  starts <- vapply(encs, function(x) if (is.null(x$period$start)) "" else as.character(x$period$start), character(1))
  enc <- encs[[order(starts, decreasing = TRUE)[[1]]]]
  start <- .fhir_date(enc$period$start)
  if (is.null(start)) stop("Encounter has no period.start.", call. = FALSE)
  today <- Sys.Date()
  end <- .fhir_date(enc$period$end)
  if (is.null(end)) end <- if (!is.null(as_of)) as.Date(as_of) else today
  birth <- .fhir_date(patient$birthDate)
  gender <- c(female = 8532L, male = 8507L)[tolower(if (is.null(patient$gender)) "" else patient$gender)]
  cmap <- as.list(concept_map)
  if (!is.null(vocab_con) || !is.null(vocab_db_path)) {
    codes_of <- function(rs, field) unlist(lapply(rs, function(r) vapply(.fhir_codings(r, field), function(c) paste0(c$system, "|", c$code), character(1))))
    wanted <- setdiff(c(codes_of(res$Condition, "code"), codes_of(res$MedicationRequest, "medicationCodeableConcept")), names(cmap))
    vc <- vocab_con
    if (is.null(vc)) {
      vc <- DBI::dbConnect(duckdb::duckdb(), vocab_db_path, read_only = TRUE)
      on.exit(try(DBI::dbDisconnect(vc, shutdown = TRUE), silent = TRUE), add = TRUE)
    }
    cmap <- utils::modifyList(resolve_fhir_codes(vc, wanted), cmap)
  }
  unmapped <- character(); skipped <- 0L
  factors <- list(lab_creatinine = c("umol/l" = 1 / 88.4, "\u00b5mol/l" = 1 / 88.4), lab_bun = c("mmol/l" = 2.801),
                  lab_glucose = c("mmol/l" = 18.016))
  enc_class <- c(IMP = 9201L, ACUTE = 9201L, NONAC = 9201L, EMER = 9203L, AMB = 9202L, OBSENC = 581385L)

  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db)
  on.exit({ try(DBI::dbDisconnect(con, shutdown = TRUE), silent = TRUE); unlink(db) }, add = TRUE)
  ex <- function(sql) DBI::dbExecute(con, sql)
  q <- function(x) if (is.null(x) || length(x) == 0 || is.na(x)) "NULL" else paste0("DATE '", format(x), "'")
  ex(sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, %d, %s, 0, 0)",
             if (is.na(gender[[1]])) 0L else gender[[1]], if (is.null(birth)) "NULL" else format(birth, "%Y")))
  cls <- if (is.null(enc$class$code)) "IMP" else enc$class$code
  ex(sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (1, 1, %d, %s, %s, 0)",
             if (is.na(enc_class[cls])) 9201L else enc_class[[cls]], q(start), q(end)))
  # Other Encounters are the longitudinal history behind prior_ip_count, prior_ed_count, days_since_prior_encounter.
  first <- start; vid <- 1L
  for (other in res$Encounter) {
    if (identical(other, enc)) next
    s_o <- .fhir_date(other$period$start)
    if (is.null(s_o)) next
    vid <- vid + 1L; first <- min(first, s_o)
    e_o <- .fhir_date(other$period$end); if (is.null(e_o)) e_o <- s_o
    cls_o <- if (is.null(other$class$code)) "IMP" else other$class$code
    ex(sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, 1, %d, %s, %s, 0)",
               vid, if (is.na(enc_class[cls_o])) 9201L else enc_class[[cls_o]], q(s_o), q(e_o)))
  }
  ex(sprintf("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) VALUES (1, 1, %s, %s, 0)",
             q(min(first, if (is.null(birth)) first else birth)), q(max(end, today))))
  mid <- 0L
  for (o in res$Observation) {
    when <- .fhir_date(if (!is.null(o$effectiveDateTime)) o$effectiveDateTime else if (!is.null(o$effectivePeriod$start)) o$effectivePeriod$start else o$issued)
    # A panel (e.g. blood pressure 85354-9 / 55284-4) has no valueQuantity; its values sit in component[].
    parts <- if (!is.null(o$valueQuantity)) list(o) else if (length(o$component)) o$component else list(o)
    for (part in parts) {
      cod <- Filter(function(c) grepl("loinc.org$", c$system) && c$code %in% names(LOINC_TO_FEATURE), .fhir_codings(part))
      qty <- part$valueQuantity
      if (!length(cod) || is.null(qty$value) || is.null(when)) { skipped <- skipped + 1L; next }
      feat <- LOINC_TO_FEATURE[[cod[[1]]$code]]
      unit <- tolower(if (!is.null(qty$unit)) qty$unit else if (!is.null(qty$code)) qty$code else "")
      f <- factors[[feat]][unit]
      value <- as.numeric(qty$value) * (if (length(f) && !is.na(f)) f[[1]] else 1)
      mid <- mid + 1L
      ex(sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES (%d, 1, %d, %s, 0, %.10g, 1)",
                 mid, as.integer(VELOCITY_CONCEPTS[[feat]][[1]]), q(when), value))
    }
  }
  lookup <- function(codings) { for (c in codings) { k <- paste0(c$system, "|", c$code); if (!is.null(cmap[[k]])) return(cmap[[k]]) }; NULL }
  note <- function(codings) unique(vapply(codings, function(c) paste0(c$system, "|", c$code), character(1)))
  for (i in seq_along(res$Condition)) {
    cd <- res$Condition[[i]]; cid <- lookup(.fhir_codings(cd))
    if (is.null(cid)) { unmapped <- c(unmapped, note(.fhir_codings(cd))); next }
    when <- .fhir_date(if (!is.null(cd$onsetDateTime)) cd$onsetDateTime else cd$recordedDate)
    ex(sprintf("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, visit_occurrence_id) VALUES (%d, 1, %d, %s, 0, 1)",
               i, as.integer(cid), q(if (is.null(when)) start else when)))
  }
  for (i in seq_along(res$MedicationRequest)) {
    m <- res$MedicationRequest[[i]]; cod <- .fhir_codings(m, "medicationCodeableConcept"); cid <- lookup(cod)
    if (is.null(cid)) { unmapped <- c(unmapped, note(cod)); next }
    when <- .fhir_date(m$authoredOn); when <- if (is.null(when)) start else when
    ex(sprintf("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, visit_occurrence_id) VALUES (%d, 1, %d, %s, %s, 0, 1)",
               i, as.integer(cid), q(when), q(when)))
  }
  out <- score_encounter(con, 1, model_bundle, as_of = as_of, explain = explain, top_k = top_k, ...)
  out$fhir <- list(encounter_id = enc$id, patient_id = patient$id, observations_skipped = skipped, unmapped_codes = sort(unique(unmapped)))
  out
}

#' Calibration curve by equal-count bins
#' @param y 0/1 outcomes.
#' @param p Predicted probabilities.
#' @param bins Number of bins.
#' @return data.frame: `predicted`, `observed`, `n`.
#' @export
calibration_curve <- function(y, p, bins = 10L) {
  ok <- is.finite(y) & is.finite(p); y <- y[ok]; p <- p[ok]
  if (!length(p)) return(data.frame(predicted = numeric(), observed = numeric(), n = integer()))
  g <- split(order(p), cut(seq_along(p), breaks = min(bins, length(p)), labels = FALSE))
  do.call(rbind, lapply(g[lengths(g) > 0], function(i) data.frame(predicted = mean(p[i]), observed = mean(y[i]), n = length(i))))
}

.svg_lines <- function(title, labels, series, hlines = NULL, w = 640, h = 240) {
  vals <- c(unlist(series), unlist(hlines)); vals <- vals[is.finite(vals)]
  if (!length(vals) || !length(labels)) return(sprintf("<h3>%s</h3><p class='muted'>No data yet.</p>", title))
  L <- 50; R <- 16; T <- 14; B <- 44
  lo <- min(0, vals); hi <- max(vals) * 1.1; if (hi == lo) hi <- lo + 1
  n <- length(labels); xs <- L + (w - L - R) * (seq_len(n) - 1) / max(n - 1, 1)
  y <- function(v) T + (h - T - B) * (1 - (v - lo) / (hi - lo))
  p <- sprintf("<svg viewBox='0 0 %d %d' role='img' aria-label='%s'>", w, h, title)
  for (k in 0:4) { v <- lo + (hi - lo) * k / 4
    p <- c(p, sprintf("<line x1='%d' x2='%d' y1='%.1f' y2='%.1f' class='grid'/><text x='%d' y='%.1f' class='tick' text-anchor='end'>%s</text>", L, w - R, y(v), y(v), L - 6, y(v) + 4, format(v, digits = 3))) }
  for (nm in names(hlines)) p <- c(p, sprintf("<line x1='%d' x2='%d' y1='%.1f' y2='%.1f' class='ref'/><text x='%d' y='%.1f' class='tick' text-anchor='end'>%s</text>", L, w - R, y(hlines[[nm]]), y(hlines[[nm]]), w - R, y(hlines[[nm]]) - 3, nm))
  step <- max(1, n %/% 8)
  for (i in seq_len(n)) if ((i - 1) %% step == 0) p <- c(p, sprintf("<text x='%.1f' y='%d' class='tick' text-anchor='middle'>%s</text>", xs[i], h - 22, labels[i]))
  for (si in seq_along(series)) {
    s <- series[[si]]; ok <- is.finite(s)
    if (any(ok)) p <- c(p, sprintf("<polyline fill='none' class='s%d' points='%s'/>", si - 1, paste(sprintf("%.1f,%.1f", xs[ok], y(s[ok])), collapse = " ")),
                        sprintf("<circle cx='%.1f' cy='%.1f' r='3' class='d%d'/>", xs[ok], y(s[ok]), si - 1))
  }
  paste0(sprintf("<h3>%s</h3>", title), paste(c(p, "</svg>"), collapse = ""))
}

#' HTML drift and calibration report from the monitoring log
#'
#' Writes one self-contained HTML file (inline SVG, no scripts or external requests, aggregates only) with the PSI trend, Brier
#' trajectory, O/E ratio, a predicted-vs-observed calibration curve (when `scores` has outcomes) and the monthly table. Mirror of
#' Python `omop_etl.drift_report.write_drift_report`.
#'
#' @param log Monitoring log: data.frame or a `.csv` / `.jsonl` path (see [update_monitoring_log()]).
#' @param out_path HTML file to write.
#' @param scores Optional data.frame with `prob_col` and `outcome_col`.
#' @param prob_col,outcome_col Column names in `scores`.
#' @param model_version Version to report (default: the latest in the log).
#' @param months Number of most recent months.
#' @param title Page title.
#' @return `out_path`, invisibly.
#' @export
write_drift_report <- function(log, out_path, scores = NULL, prob_col = "prob", outcome_col = "outcome_flag", model_version = NULL,
                               months = 12L, title = "Model monitoring report") {
  df <- if (is.character(log)) {
    if (grepl("\\.(jsonl|json|ndjson)$", log, ignore.case = TRUE)) jsonlite::stream_in(file(log), verbose = FALSE) else
      utils::read.csv(log, stringsAsFactors = FALSE, colClasses = c(model_version = "character"))
  } else as.data.frame(log)
  if (!"month" %in% names(df)) stop("monitoring log needs a 'month' column.", call. = FALSE)
  df$month <- as.character(df$month)
  mv <- ""
  if ("model_version" %in% names(df)) {
    df$model_version[is.na(df$model_version)] <- ""
    mv <- if (!is.null(model_version)) as.character(model_version) else df$model_version[order(df$month)][nrow(df)]
    df <- df[df$model_version == mv, , drop = FALSE]
  }
  df <- utils::tail(df[order(df$month), , drop = FALSE], months)
  num <- function(c) if (c %in% names(df)) suppressWarnings(as.numeric(df[[c]])) else rep(NA_real_, nrow(df))
  charts <- c(.svg_lines("Population stability index (PSI)", df$month, list(PSI = num("psi")), c(watch = 0.10, drift = 0.25)),
              .svg_lines("Brier score", df$month, list(Brier = num("brier"))),
              .svg_lines("Observed / expected ratio", df$month, list(`O/E` = num("oe_ratio")), c(`1.0` = 1)))
  cal <- "<p class='muted'>No outcome data supplied (pass scores with an outcome column).</p>"
  if (!is.null(scores) && all(c(prob_col, outcome_col) %in% names(scores))) {
    cv <- calibration_curve(as.numeric(scores[[outcome_col]]), as.numeric(scores[[prob_col]]))
    if (nrow(cv)) {
      sz <- 280; hi <- max(cv$predicted, cv$observed, 0.05) * 1.1
      px <- function(v) 44 + (sz - 54) * v / hi; py <- function(v) 12 + (sz - 48) * (1 - v / hi)
      cal <- sprintf("<svg viewBox='0 0 %d %d' role='img' aria-label='Calibration curve'><line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' class='ref'/><polyline fill='none' class='s0' points='%s'/>%s<text x='%d' y='%d' class='tick' text-anchor='middle'>Predicted</text></svg>",
                     sz, sz, px(0), py(0), px(hi), py(hi), paste(sprintf("%.1f,%.1f", px(cv$predicted), py(cv$observed)), collapse = " "),
                     paste(sprintf("<circle cx='%.1f' cy='%.1f' r='4' class='d0'><title>n=%d</title></circle>", px(cv$predicted), py(cv$observed), cv$n), collapse = ""),
                     sz %/% 2, sz - 8)
    }
  }
  st <- if ("status" %in% names(df)) as.character(df$status) else character()
  latest <- if (length(st)) st[[length(st)]] else "n/a"
  flagged <- df$month[st %in% c("drift", "miscalibrated")]
  cols <- intersect(c("month", "n", "mean_prob", "psi", "brier", "oe_ratio", "slope", "status"), names(df))
  esc <- function(x) gsub(">", "&gt;", gsub("<", "&lt;", gsub("&", "&amp;", x, fixed = TRUE), fixed = TRUE), fixed = TRUE)
  rows <- paste(vapply(seq_len(nrow(df)), function(i) paste0("<tr>", paste(sprintf("<td>%s</td>", vapply(cols, function(c) {
    v <- df[[c]][[i]]; if (is.na(v)) "" else if (is.numeric(v) && !is.integer(v)) format(v, digits = 4) else esc(as.character(v)) }, character(1))), collapse = ""), "</tr>"), character(1)), collapse = "")
  page <- sprintf("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>%s</title><style>:root{--bg:#fff;--fg:#1b1f23;--mut:#667;--grid:#dde1e6;--c0:#1565c0}@media(prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e6e8ea;--mut:#9aa3ab;--grid:#2d343b;--c0:#64b5f6}}body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}main{max-width:900px;margin:auto}h3{margin:24px 0 4px}.muted{color:var(--mut)}svg{width:100%%;height:auto}.grid{stroke:var(--grid)}.ref{stroke:var(--mut);stroke-dasharray:4 4}.tick{fill:var(--mut);font-size:10px}.s0{stroke:var(--c0);stroke-width:2}.d0{fill:var(--c0)}table{border-collapse:collapse;width:100%%;font-size:13px}th,td{border-bottom:1px solid var(--grid);padding:4px 8px;text-align:right}th:first-child,td:first-child{text-align:left}.tw{overflow-x:auto}</style></head><body><main><h1>%s</h1><p class='muted'>Model version: %s &middot; %d month(s) &middot; latest status <b>%s</b> &middot; flagged months: %s</p>%s<h3>Predicted vs observed (calibration)</h3>%s<h3>Monthly detail</h3><div class='tw'><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div><p class='muted'>Aggregates only. PSI &ge; 0.10 = watch, &ge; 0.25 = drift.</p></main></body></html>",
                  esc(title), esc(title), esc(if (nzchar(mv)) mv else "n/a"), nrow(df), esc(latest),
                  if (length(flagged)) paste(flagged, collapse = ", ") else "none", paste(charts, collapse = ""), cal,
                  paste(sprintf("<th>%s</th>", cols), collapse = ""), rows)
  dir.create(dirname(out_path), showWarnings = FALSE, recursive = TRUE)
  writeLines(page, out_path, useBytes = TRUE)
  invisible(out_path)
}
