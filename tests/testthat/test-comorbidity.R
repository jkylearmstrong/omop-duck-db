# extract_elixhauser_comorbidities() and extract_charlson_index() (RFC 3.1). Mirrors tests/test_comorbidity.py case
# for case and asserts the SAME shared fixtures: tests/fixtures/comorbidity_{conditions,cohort,concept_ancestor}.csv
# and the golden files comorbidity_golden_{elixhauser,charlson}.csv (expected output of four configurations from an
# independent plain-Python model, see tests/fixtures/comorbidity_make_fixture.py). That is the cross-language parity
# check: the Python suite asserts the very same golden files.
#
# Hand-computed numbers (weights from PROVENANCE.md). Person 40 has heart failure, diabetes with and without
# complications, metastatic and solid cancer, hypertension with and without complications, acute and chronic liver
# disease and an acute myocardial infarction:
#   Elixhauser: chf 7 + liver_disease 11 + mets 12 = 30 after the hierarchy (solid_tumor 4 is dropped because mets is
#     present); 34 raw. Counted domains: chf, htn_comp, dm_comp, liver_disease, mets = 5; 8 raw.
#   Charlson: mi 1 + chf 1 + mod_severe_liver 3 + dm_comp 2 + mets 6 = 13 after the hierarchy; 17 raw (mild_liver 1,
#     dm_uncomp 1 and malignancy 2 are dropped). Counted categories: 5; 8 raw.

skip_if_not_installed("withr")

INDEXES <- list(
  elixhauser = list(fn = extract_elixhauser_comorbidities, prefix = "elix_", score = "elix_van_walraven_score",
                    total = "elix_total_conditions"),
  charlson = list(fn = extract_charlson_index, prefix = "cci_", score = "charlson_index", total = "cci_total_conditions")
)
CONFIGS <- list(
  default = list(),
  raw = list(hierarchy_adjusted = FALSE),
  source_only = list(source_and_standard = FALSE),
  all_history = list(lookback_days = NULL)
)
ELIX_ORDER <- c("chf", "arrhythmia", "valvular", "pulm_circ", "pvd", "htn_uncomp", "htn_comp", "paralysis",
                "neuro_other", "copd", "dm_uncomp", "dm_comp", "hypothyroid", "renal_failure", "liver_disease", "pud",
                "hiv", "lymphoma", "mets", "solid_tumor", "rheumatic", "coagulopathy", "obesity", "weight_loss",
                "fluid_electrolyte", "blood_loss_anemia", "deficiency_anemia", "alcohol_abuse", "drug_abuse",
                "psychoses", "depression")
CCI_ORDER <- c("mi", "chf", "pvd", "cevd", "dementia", "copd", "rheum", "pud", "mild_liver", "dm_uncomp", "dm_comp",
               "plegia", "renal", "malignancy", "mod_severe_liver", "mets", "hiv")
ORDER <- list(elixhauser = ELIX_ORDER, charlson = CCI_ORDER)
# van Walraven 2009 and Charlson 1987 weights as documented in PROVENANCE.md
WEIGHTS <- list(
  elixhauser = stats::setNames(c(7, 5, -1, 4, 2, 0, 0, 7, 6, 3, 0, 0, 0, 5, 11, 0, 0, 9, 12, 4, 0, 3, -4, 6, 5, -2, -2,
                                 0, -7, 0, -3), ELIX_ORDER),
  charlson = stats::setNames(c(1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 6, 6), CCI_ORDER)
)

# ----------------------------------------------------------------------------------------------- fixtures
cm_fixture_path <- function(name) normalizePath(test_path("..", "fixtures", name), winslash = "/", mustWork = TRUE)
cm_code_path <- function(index) {
  path <- system.file("extdata", "comorbidity", paste0(index, "_codes.csv"), package = "omopduckdb")
  stopifnot(nzchar(path))
  normalizePath(path, winslash = "/", mustWork = TRUE)
}
cm_cols <- function(index) paste0(INDEXES[[index]]$prefix, ORDER[[index]])
cm_names <- function(index) c("subject_id", "cohort_start_date", cm_cols(index), INDEXES[[index]]$score,
                              INDEXES[[index]]$total)

# The shared CDM fixture in a file database: full CDM schema, condition rows, cohort rows, concept_ancestor.
cm_load_shared <- function(db_path) {
  invisible(utils::capture.output(build_schema(db_path)))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  DBI::dbExecute(con, sprintf("
    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id,
                                      condition_start_date, condition_type_concept_id, condition_source_value)
    SELECT condition_occurrence_id, person_id, condition_concept_id, condition_start_date, 32020,
           condition_source_value
    FROM read_csv('%s', header = true, columns = {
      'condition_occurrence_id': 'INTEGER', 'person_id': 'INTEGER', 'condition_concept_id': 'INTEGER',
      'condition_start_date': 'DATE', 'condition_source_value': 'VARCHAR', 'scenario': 'VARCHAR'})",
                              cm_fixture_path("comorbidity_conditions.csv")))
  DBI::dbExecute(con, sprintf("
    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT cohort_definition_id, subject_id, cohort_start_date, cohort_start_date
    FROM read_csv('%s', header = true, columns = {
      'cohort_definition_id': 'INTEGER', 'subject_id': 'INTEGER', 'cohort_start_date': 'DATE',
      'scenario': 'VARCHAR'})", cm_fixture_path("comorbidity_cohort.csv")))
  DBI::dbExecute(con, sprintf("
    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation,
                                  max_levels_of_separation)
    SELECT ancestor_concept_id, descendant_concept_id, 0, 0 FROM read_csv('%s', header = true)",
                              cm_fixture_path("comorbidity_concept_ancestor.csv")))
  con
}

shared_path <- tempfile(fileext = ".duckdb")
shared <- cm_load_shared(shared_path)
withr::defer({
  if (DBI::dbIsValid(shared)) DBI::dbDisconnect(shared, shutdown = TRUE)
  unlink(shared_path)
}, teardown_env())

probes <- local({
  fx <- utils::read.csv(cm_fixture_path("comorbidity_cohort.csv"), stringsAsFactors = FALSE)
  fx <- fx[startsWith(fx$scenario, "probe "), ]
  stats::setNames(fx$subject_id, fx$scenario)
})

# A minimal CDM in memory. conds: data.frame(person, concept, date, source); cohort: data.frame(id, person, date).
cm_mini <- function(conds = NULL, cohort = NULL, ancestors = NULL, env = parent.frame()) {
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE), envir = env)
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER,
                         condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
  DBI::dbExecute(con, "CREATE TABLE cohort (cohort_definition_id INTEGER, subject_id INTEGER,
                         cohort_start_date DATE, cohort_end_date DATE)")
  if (!is.null(conds) && nrow(conds) > 0L) {
    DBI::dbAppendTable(con, "condition_occurrence", data.frame(
      condition_occurrence_id = seq_len(nrow(conds)), person_id = as.integer(conds$person),
      condition_concept_id = as.integer(conds$concept), condition_start_date = as.Date(conds$date),
      condition_source_value = conds$source, stringsAsFactors = FALSE))
  }
  if (!is.null(cohort) && nrow(cohort) > 0L) {
    DBI::dbAppendTable(con, "cohort", data.frame(
      cohort_definition_id = as.integer(cohort$id), subject_id = as.integer(cohort$person),
      cohort_start_date = as.Date(cohort$date), cohort_end_date = as.Date(cohort$date)))
  }
  if (!is.null(ancestors)) {
    DBI::dbExecute(con, "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER,
                           min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)")
    if (nrow(ancestors) > 0L) {
      DBI::dbAppendTable(con, "concept_ancestor", data.frame(
        ancestor_concept_id = as.integer(ancestors$a), descendant_concept_id = as.integer(ancestors$d),
        min_levels_of_separation = 0L, max_levels_of_separation = 0L))
    }
  }
  con
}
cm_conds <- function(...) {
  rows <- list(...)
  data.frame(person = vapply(rows, function(r) r[[1]], numeric(1)), concept = vapply(rows, function(r) r[[2]], numeric(1)),
             date = as.Date(vapply(rows, function(r) r[[3]], character(1))),
             source = vapply(rows, function(r) if (is.na(r[[4]])) NA_character_ else r[[4]], character(1)),
             stringsAsFactors = FALSE)
}
cm_cohort <- function(persons, date = "2021-06-01", id = 1) {
  data.frame(id = id, person = persons, date = as.Date(date))
}

# Raw flags of one row as domain names.
flagged <- function(df, index, person, index_date = NULL) {
  sub <- df[df$subject_id == person, , drop = FALSE]
  if (!is.null(index_date)) sub <- sub[format(sub$cohort_start_date) == index_date, , drop = FALSE]
  expect_equal(nrow(sub), 1L, info = paste("rows for person", person))
  ORDER[[index]][vapply(ORDER[[index]], function(d) sub[[paste0(INDEXES[[index]]$prefix, d)]][1] == 1, logical(1))]
}
summary_of <- function(df, index, person, index_date = NULL) {
  sub <- df[df$subject_id == person, , drop = FALSE]
  if (!is.null(index_date)) sub <- sub[format(sub$cohort_start_date) == index_date, , drop = FALSE]
  c(sub[[INDEXES[[index]]$score]][1], sub[[INDEXES[[index]]$total]][1])
}
same_set <- function(a, b) expect_setequal(a, b)

# ----------------------------------------------------------------------------------------- parity with golden
for (index in names(INDEXES)) {
  for (config in names(CONFIGS)) {
    local({
      index <- index
      config <- config
      test_that(sprintf("%s (%s) matches the shared golden file", index, config), {
        got <- do.call(INDEXES[[index]]$fn, c(list(shared), CONFIGS[[config]]))
        gold <- utils::read.csv(cm_fixture_path(sprintf("comorbidity_golden_%s.csv", index)),
                                stringsAsFactors = FALSE, colClasses = c(config = "character", cohort_start_date = "character"))
        exp <- gold[gold$config == config, setdiff(names(gold), "config"), drop = FALSE]
        rownames(exp) <- NULL
        expect_identical(names(got), names(exp))
        got$cohort_start_date <- format(got$cohort_start_date)
        expect_equal(got, exp, ignore_attr = TRUE)
      })
    })
  }
}

for (index in names(INDEXES)) {
  local({
    index <- index
    meta <- INDEXES[[index]]
    test_that(paste(index, "columns, order and types"), {
      df <- meta$fn(shared)
      expect_s3_class(df, "data.frame")
      expect_identical(names(df), cm_names(index))
      expect_length(cm_cols(index), if (index == "elixhauser") 31L else 17L)
      for (col in c(cm_cols(index), meta$score, meta$total)) {
        expect_type(df[[col]], "integer")
        expect_false(anyNA(df[[col]]), info = col)
      }
      expect_true(all(unlist(df[cm_cols(index)]) %in% c(0L, 1L)))
    })

    test_that(paste(index, "code table has the documented weights and shape"), {
      rows <- utils::read.csv(cm_code_path(index), stringsAsFactors = FALSE, colClasses = "character")
      expect_identical(unique(rows$domain), ORDER[[index]])
      w <- tapply(as.integer(rows$weight), rows$domain, unique)
      expect_equal(unlist(w[ORDER[[index]]]), WEIGHTS[[index]], ignore_attr = TRUE)
      for (d in ORDER[[index]]) {
        expect_true(all(c("ICD10CM", "ICD9CM") %in% rows$code_system[rows$domain == d]), info = d)
      }
      expect_true(nzchar(system.file("extdata", "comorbidity", paste0(index, "_codes.csv"), package = "omopduckdb")))
    })

    test_that(paste(index, "every domain triggers from ICD-10, ICD-9 and a standard descendant"), {
      full <- meta$fn(shared)
      src <- meta$fn(shared, source_and_standard = FALSE)
      rows <- utils::read.csv(cm_code_path(index), stringsAsFactors = FALSE, colClasses = "character")
      with_ancestors <- unique(rows$domain[rows$code_system == "SNOMED_ANCESTOR"])
      no_standard_side <- setdiff(ORDER[[index]], with_ancestors)
      # Metastatic solid tumour is detected from ICD source values only in the Charlson table (PROVENANCE.md).
      expect_identical(no_standard_side, if (index == "charlson") "mets" else character(0))
      at <- function(df, person, col) df[[col]][match(person, df$subject_id)]
      for (domain in ORDER[[index]]) {
        col <- paste0(meta$prefix, domain)
        p10 <- probes[[sprintf("probe %s %s icd10", index, domain)]]
        p9 <- probes[[sprintf("probe %s %s icd9", index, domain)]]
        expect_equal(at(full, p10, col), 1L, info = paste(index, domain, "ICD-10"))
        expect_equal(at(src, p10, col), 1L, info = paste(index, domain, "ICD-10 source only"))
        expect_equal(at(full, p9, col), 1L, info = paste(index, domain, "ICD-9"))
        expect_equal(at(src, p9, col), 1L, info = paste(index, domain, "ICD-9 source only"))
        label <- sprintf("probe %s %s standard", index, domain)
        if (domain %in% no_standard_side) {
          expect_false(label %in% names(probes))
          next
        }
        ps <- probes[[label]]
        expect_equal(at(full, ps, col), 1L, info = paste(index, domain, "standard descendant"))
        expect_equal(at(src, ps, col), 0L, info = paste(index, domain, "standard probe must not match by source"))
      }
    })

    test_that(paste(index, "window boundary days"), {
      df <- meta$fn(shared)
      # persons 10-15: heart failure code on index-1, the index day, index-365, index-366, index+1 and in 2015
      same_set(flagged(df, index, 10), "chf")
      same_set(flagged(df, index, 11), character(0))
      same_set(flagged(df, index, 12), "chf")
      same_set(flagged(df, index, 13), character(0))
      same_set(flagged(df, index, 14), character(0))
      same_set(flagged(df, index, 15), character(0))
    })

    test_that(paste(index, "lookback_days = NULL means all history before the index"), {
      df <- meta$fn(shared, lookback_days = NULL)
      same_set(flagged(df, index, 13), "chf")
      same_set(flagged(df, index, 15), "chf")
      same_set(flagged(df, index, 11), character(0))
      same_set(flagged(df, index, 14), character(0))
    })

    test_that(paste(index, "short and zero look-back"), {
      one <- meta$fn(shared, lookback_days = 1)
      same_set(flagged(one, index, 10), "chf")
      same_set(flagged(one, index, 12), character(0))
      zero <- meta$fn(shared, lookback_days = 0)
      expect_equal(sum(as.matrix(zero[cm_cols(index)])), 0)
      expect_equal(nrow(zero), nrow(meta$fn(shared)))
    })

    test_that(paste(index, "source value normalisation"), {
      df <- meta$fn(shared)
      same_set(flagged(df, index, 20), "chf")  # lower case
      same_set(flagged(df, index, 21), "chf")  # spaces and dots anywhere
      same_set(flagged(df, index, 22), "chf")  # ICD-9 with and without the dot
      same_set(flagged(df, index, 23), character(0))  # NULL source value
      same_set(flagged(df, index, 24), character(0))  # empty source value
    })

    test_that(paste(index, "negative controls"), {
      df <- meta$fn(shared)
      for (person in c(30, 31, 32, 33)) {  # unrelated, shorter than the prefix, contains-not-starts, neighbours
        same_set(flagged(df, index, person), character(0))
        expect_equal(summary_of(df, index, person), c(0L, 0L))
      }
    })

    test_that(paste(index, "persons without conditions are retained, one row per distinct cohort row"), {
      df <- meta$fn(shared)
      for (person in c(70, 71)) {  # no condition rows at all / only a record after the index date
        same_set(flagged(df, index, person), character(0))
        expect_equal(summary_of(df, index, person), c(0L, 0L))
      }
      distinct <- DBI::dbGetQuery(shared, "SELECT count(*) AS n FROM (SELECT DISTINCT subject_id, cohort_start_date FROM cohort)")$n
      expect_equal(nrow(df), distinct)
    })

    test_that(paste(index, "several index dates per person, duplicated cohort rows, ordering"), {
      df <- meta$fn(shared)
      expect_identical(format(df$cohort_start_date[df$subject_id == 50]), c("2021-02-01", "2021-09-01", "2022-09-20"))
      same_set(flagged(df, index, 50, "2021-02-01"), "chf")  # the copd record is after this index date
      same_set(flagged(df, index, 50, "2021-09-01"), c("chf", "copd"))
      same_set(flagged(df, index, 50, "2022-09-20"), character(0))  # both records older than 365 days
      expect_equal(sum(df$subject_id == 51), 1L)
      expect_false(anyDuplicated(df[c("subject_id", "cohort_start_date")]) > 0)
      expect_identical(order(df$subject_id, df$cohort_start_date), seq_len(nrow(df)))
    })

    test_that(paste(index, "cohort_id filter"), {
      everyone <- unique(meta$fn(shared)$subject_id)
      one <- unique(meta$fn(shared, cohort_id = 1)$subject_id)
      two <- meta$fn(shared, cohort_id = 2)
      expect_setequal(two$subject_id, c(60, 61, 62))
      expect_equal(nrow(two), 3L)
      same_set(flagged(two, index, 60), "chf")
      same_set(flagged(two, index, 61), character(0))
      same_set(flagged(two, index, 62), "copd")
      expect_false(any(c(60, 61) %in% one))
      expect_true(62 %in% one)
      expect_setequal(everyone, c(one, 60, 61))
      none <- meta$fn(shared, cohort_id = 999)
      expect_equal(nrow(none), 0L)
      expect_identical(names(none), names(meta$fn(shared)))
    })

    test_that(paste(index, "standard concept side"), {
      full <- meta$fn(shared)
      src <- meta$fn(shared, source_and_standard = FALSE)
      # 316139 = chf ancestor, 255573 = copd ancestor; 9000001/9000002 are its child and grandchild; 9000003 sits under both
      expected_full <- list(`80` = "chf", `81` = "chf", `82` = "chf", `83` = c("chf", "copd"), `84` = character(0),
                            `85` = "chf", `86` = c("chf", "copd"), `87` = character(0), `88` = "copd")
      expected_src <- list(`80` = character(0), `81` = character(0), `82` = character(0), `83` = character(0),
                           `84` = character(0), `85` = "chf", `86` = "copd", `87` = character(0), `88` = "copd")
      for (person in names(expected_full)) {
        same_set(flagged(full, index, as.numeric(person)), expected_full[[person]])
        same_set(flagged(src, index, as.numeric(person)), expected_src[[person]])
      }
    })
  })
}

test_that("lookback_days accepts whole numbers of any numeric type", {
  base <- extract_charlson_index(shared)
  expect_identical(extract_charlson_index(shared, lookback_days = 365L), base)
  expect_identical(extract_charlson_index(shared, lookback_days = 365), base)
})

test_that("a timestamp index column uses the calendar day", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-06-01", "I50.9"), list(2, 0, "2021-05-31", "J44.9")))
  DBI::dbExecute(con, "CREATE TABLE stays (pid INTEGER, admit_ts TIMESTAMP)")
  DBI::dbExecute(con, "INSERT INTO stays VALUES (1, TIMESTAMP '2021-06-01 15:00:00'), (2, TIMESTAMP '2021-06-01 00:00:01')")
  df <- extract_elixhauser_comorbidities(con, "stays", person_col = "pid", index_date_col = "admit_ts",
                                         source_and_standard = FALSE)
  expect_identical(names(df)[1:2], c("pid", "admit_ts"))
  # a same-day record is not strictly before the index date; the day before is
  expect_equal(df$elix_chf[df$pid == 1], 0L)
  expect_equal(df$elix_copd[df$pid == 2], 1L)
})

test_that("whitespace of every kind is removed", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "i\t50 .\n9"), list(2, 0, "2021-05-01", "  428 . 0  "),
                          list(3, 0, "2021-05-01", "J 44\r.9")), cm_cohort(1:3))
  df <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE)
  expect_equal(df$elix_chf, c(1L, 1L, 0L))
  expect_equal(df$elix_copd, c(0L, 0L, 1L))
})

test_that("prefix matching is a prefix, not a substring or an exact match", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50"), list(2, 0, "2021-05-01", "I509XYZ"),
                          list(3, 0, "2021-05-01", "I5"), list(4, 0, "2021-05-01", "CI50.9"),
                          list(5, 0, "2021-05-01", "50.9")), cm_cohort(1:5))
  expect_equal(extract_charlson_index(con, source_and_standard = FALSE)$cci_chf, c(1L, 1L, 0L, 0L, 0L))
})

test_that("an empty cohort returns an empty data.frame with all columns", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")))
  for (index in names(INDEXES)) {
    df <- INDEXES[[index]]$fn(con, source_and_standard = FALSE)
    expect_equal(nrow(df), 0L)
    expect_identical(names(df), cm_names(index))
  }
})

test_that("cohort_id on a table without cohort_definition_id warns and profiles everything", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")))
  DBI::dbExecute(con, "CREATE TABLE plain AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
  expect_warning(df <- extract_charlson_index(con, "plain", cohort_id = 7, source_and_standard = FALSE),
                 "cohort_id=7 was ignored")
  expect_equal(df$cci_chf, 1L)
})

# ----------------------------------------------------------------------------------------------- hierarchy
# person: index -> list(raw flags, adjusted c(score, total), raw c(score, total)); see the header for person 40
HAND <- list(
  `40` = list(
    elixhauser = list(c("chf", "htn_uncomp", "htn_comp", "dm_uncomp", "dm_comp", "liver_disease", "mets", "solid_tumor"),
                      c(30, 5), c(34, 8)),
    charlson = list(c("mi", "chf", "mild_liver", "dm_uncomp", "dm_comp", "malignancy", "mod_severe_liver", "mets"),
                    c(13, 5), c(17, 8))),
  # E11.22 + E11.9: both diabetes flags. Elixhauser weights are 0 (count 1 vs 2); Charlson dm_comp 2 vs 2 + 1.
  `41` = list(elixhauser = list(c("dm_uncomp", "dm_comp"), c(0, 1), c(0, 2)),
              charlson = list(c("dm_uncomp", "dm_comp"), c(2, 1), c(3, 2))),
  # E66.9 obesity -4, F11.20 drug abuse -7, F32.9 depression -3, E03.9 hypothyroid 0: the score can be negative
  `42` = list(elixhauser = list(c("hypothyroid", "obesity", "drug_abuse", "depression"), c(-14, 4), c(-14, 4)),
              charlson = list(character(0), c(0, 0), c(0, 0))),
  # C78.0 mets 12 + C50.9 solid tumour 4: 12 after the hierarchy, 16 raw. Charlson mets 6 + malignancy 2: 6 vs 8
  `43` = list(elixhauser = list(c("mets", "solid_tumor"), c(12, 1), c(16, 2)),
              charlson = list(c("malignancy", "mets"), c(6, 1), c(8, 2))),
  # K73.9 (mild) + K72.9 (severe): Elixhauser one liver_disease 11; Charlson severe 3 vs 1 + 3
  `44` = list(elixhauser = list("liver_disease", c(11, 1), c(11, 1)),
              charlson = list(c("mild_liver", "mod_severe_liver"), c(3, 1), c(4, 2))),
  # I10 + I12.9: both hypertension flags, weights 0, so only the count differs
  `45` = list(elixhauser = list(c("htn_uncomp", "htn_comp"), c(0, 1), c(0, 2)),
              charlson = list(character(0), c(0, 0), c(0, 0)))
)

test_that("hierarchy_adjusted on and off give the hand-computed scores and counts", {
  for (index in names(INDEXES)) {
    fn <- INDEXES[[index]]$fn
    on <- fn(shared, hierarchy_adjusted = TRUE)
    off <- fn(shared, hierarchy_adjusted = FALSE)
    for (person in names(HAND)) {
      h <- HAND[[person]][[index]]
      p <- as.numeric(person)
      # the 0/1 columns are the raw flags either way
      same_set(flagged(on, index, p), h[[1]])
      same_set(flagged(off, index, p), h[[1]])
      expect_equal(summary_of(on, index, p), as.integer(h[[2]]), info = paste(index, person, "adjusted"))
      expect_equal(summary_of(off, index, p), as.integer(h[[3]]), info = paste(index, person, "raw"))
      # the plain weighted sum is recomputable from the flags and the documented weights
      expect_equal(h[[3]][1], sum(WEIGHTS[[index]][h[[1]]]))
      expect_equal(h[[3]][2], length(h[[1]]))
    }
  }
})

test_that("each hierarchy rule works in isolation", {
  con <- cm_mini(cm_conds(
    list(1, 0, "2021-05-01", "E11.22"), list(1, 0, "2021-05-01", "E11.9"),   # dm_comp + dm_uncomp
    list(2, 0, "2021-05-01", "C78.0"), list(2, 0, "2021-05-01", "C50.9"),    # mets + solid tumour
    list(3, 0, "2021-05-01", "I12.9"), list(3, 0, "2021-05-01", "I10"),      # htn_comp + htn_uncomp
    list(4, 0, "2021-05-01", "K72.9"), list(4, 0, "2021-05-01", "K73.9")),   # severe + mild liver
    cm_cohort(1:4))
  on <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE)
  off <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE, hierarchy_adjusted = FALSE)
  expect_equal(on$elix_total_conditions, c(1L, 1L, 1L, 1L))
  expect_equal(off$elix_total_conditions, c(2L, 2L, 2L, 1L))
  expect_equal(on$elix_van_walraven_score, c(0L, 12L, 0L, 11L))
  expect_equal(off$elix_van_walraven_score, c(0L, 16L, 0L, 11L))
  cci_on <- extract_charlson_index(con, source_and_standard = FALSE)
  cci_off <- extract_charlson_index(con, source_and_standard = FALSE, hierarchy_adjusted = FALSE)
  expect_equal(cci_on$charlson_index, c(2L, 6L, 0L, 3L))
  expect_equal(cci_on$cci_total_conditions, c(1L, 1L, 0L, 1L))
  expect_equal(cci_off$charlson_index, c(3L, 8L, 0L, 4L))
  expect_equal(cci_off$cci_total_conditions, c(2L, 2L, 0L, 2L))
})

test_that("weights come from the CSV, not from code", {
  lines <- readLines(cm_code_path("elixhauser"))
  edited <- c(lines[1], sub(",chf,elix_chf,Congestive heart failure,7,", ",chf,elix_chf,Congestive heart failure,70,",
                            lines[-1], fixed = TRUE))
  expect_false(identical(edited, lines))
  dir <- withr::local_tempdir()
  writeLines(edited, file.path(dir, "elixhauser_codes.csv"))
  testthat::local_mocked_bindings(.comorb_code_path = function(file) file.path(dir, file))
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")), cm_cohort(1))
  expect_equal(extract_elixhauser_comorbidities(con, source_and_standard = FALSE)$elix_van_walraven_score, 70L)
})

# ------------------------------------------------------------------------------------- standard concepts
test_that("the standard side respects the window", {
  con <- cm_mini(cm_conds(list(1, 9001, "2021-06-01", NA), list(2, 9001, "2021-05-31", NA),
                          list(3, 9001, "2020-05-31", NA)), cm_cohort(1:3),
                 ancestors = data.frame(a = c(316139, 316139, 9001), d = c(316139, 9001, 9001)))
  expect_equal(extract_elixhauser_comorbidities(con)$elix_chf, c(0L, 1L, 0L))
})

test_that("an ancestor matches even without its self row", {
  con <- cm_mini(cm_conds(list(1, 316139, "2021-05-01", NA)), cm_cohort(1), ancestors = data.frame(a = 316139, d = 9001))
  expect_equal(extract_elixhauser_comorbidities(con)$elix_chf, 1L)
})

test_that("a missing concept_ancestor warns and falls back to source-only matching", {
  con <- cm_mini(cm_conds(list(1, 9001, "2021-05-01", NA), list(2, 0, "2021-05-01", "I50.9")), cm_cohort(1:2))
  for (index in names(INDEXES)) {
    meta <- INDEXES[[index]]
    expect_warning(df <- meta$fn(con),
                   "concept_ancestor could not be resolved .*source-value \\(ICD prefix\\) matching only")
    expect_identical(df, meta$fn(con, source_and_standard = FALSE))
    expect_equal(df[[paste0(meta$prefix, "chf")]], c(0L, 1L))
  }
  expect_no_warning(extract_elixhauser_comorbidities(con, source_and_standard = FALSE))
})

test_that("an empty concept_ancestor warns too", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")), cm_cohort(1), ancestors = data.frame(a = numeric(0), d = numeric(0)))
  expect_warning(df <- extract_charlson_index(con), "concept_ancestor exists on this connection but is empty")
  expect_equal(df$cci_chf, 1L)
})

cm_vocab_file <- function(env = parent.frame()) {
  path <- withr::local_tempfile(fileext = ".duckdb", .local_envir = env)
  v <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  DBI::dbExecute(v, "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER,
                       min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)")
  DBI::dbExecute(v, "INSERT INTO concept_ancestor VALUES (316139, 316139, 0, 0), (316139, 9001, 1, 1), (9001, 9001, 0, 0)")
  DBI::dbDisconnect(v, shutdown = TRUE)
  path
}

test_that("concept_ancestor is found in an attached central_vocab (with or without search_path)", {
  for (set_search_path in c(FALSE, TRUE)) {
    con <- cm_mini(cm_conds(list(1, 9001, "2021-05-01", NA)), cm_cohort(1))  # no concept_ancestor in main
    DBI::dbExecute(con, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", cm_vocab_file())))
    if (set_search_path) DBI::dbExecute(con, "SET search_path = 'main,central_vocab.main'")
    expect_no_warning(elix <- extract_elixhauser_comorbidities(con))
    expect_no_warning(cci <- extract_charlson_index(con))
    expect_equal(elix$elix_chf, 1L)
    expect_equal(cci$cci_chf, 1L)
  }
})

test_that("an empty concept_ancestor in main does not shadow an attached vocabulary", {
  con <- cm_mini(cm_conds(list(1, 9001, "2021-05-01", NA)), cm_cohort(1),
                 ancestors = data.frame(a = numeric(0), d = numeric(0)))
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", cm_vocab_file())))
  expect_no_warning(df <- extract_elixhauser_comorbidities(con))
  expect_equal(df$elix_chf, 1L)
})

# ------------------------------------------------------------------------- connections and cleanliness
test_that("it works on a read-only connection", {
  path <- tempfile(fileext = ".duckdb")
  rw <- cm_load_shared(path)
  expected <- lapply(INDEXES, function(m) m$fn(rw))
  DBI::dbDisconnect(rw, shutdown = TRUE)
  ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
  withr::defer({
    DBI::dbDisconnect(ro, shutdown = TRUE)
    unlink(path)
  })
  expect_true(isTRUE(as.logical(DBI::dbGetQuery(
    ro, "SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()")$readonly[[1]])))
  for (index in names(INDEXES)) {
    for (config in names(CONFIGS)) {
      expect_equal(nrow(do.call(INDEXES[[index]]$fn, c(list(ro), CONFIGS[[config]]))), nrow(expected[[index]]))
    }
    expect_identical(INDEXES[[index]]$fn(ro), expected[[index]])
  }
})

cm_catalog <- function(con) {
  q <- function(sql) DBI::dbGetQuery(con, sql)
  list(
    tables = q("SELECT database_name, schema_name, table_name, temporary FROM duckdb_tables() ORDER BY ALL"),
    views = q("SELECT database_name, schema_name, view_name, temporary FROM duckdb_views() WHERE NOT internal ORDER BY ALL"),
    functions = q("SELECT database_name, schema_name, function_name FROM duckdb_functions() WHERE NOT internal ORDER BY ALL"),
    sequences = q("SELECT database_name, sequence_name FROM duckdb_sequences() ORDER BY ALL"),
    schemas = q("SELECT database_name, schema_name FROM duckdb_schemas() ORDER BY ALL")
  )
}

test_that("it leaves no objects behind", {
  before <- cm_catalog(shared)
  for (meta in INDEXES) {
    for (kwargs in CONFIGS) do.call(meta$fn, c(list(shared), kwargs))
    meta$fn(shared, cohort_id = 2)
  }
  expect_identical(cm_catalog(shared), before)
  expect_false(any(before$tables$temporary))
})

test_that("a read-only CDM with an attached read-only vocabulary on the search path works and creates nothing", {
  # The omop_connect(read_only = TRUE) layout: read-only CDM file, read-only vocabulary file attached as
  # central_vocab, SET search_path = 'main,central_vocab.main'.
  cdm <- withr::local_tempfile(fileext = ".duckdb")
  rw <- DBI::dbConnect(duckdb::duckdb(), dbdir = cdm)
  DBI::dbExecute(rw, "CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER,
                        condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
  DBI::dbExecute(rw, "INSERT INTO condition_occurrence VALUES (1, 1, 9001, DATE '2021-05-01', NULL),
                        (2, 2, 0, DATE '2021-05-01', 'I50.9')")
  DBI::dbExecute(rw, "CREATE TABLE cohort AS SELECT i AS subject_id, DATE '2021-06-01' AS cohort_start_date
                        FROM range(1, 4) AS t(i)")
  DBI::dbDisconnect(rw, shutdown = TRUE)
  ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = cdm, read_only = TRUE)
  withr::defer(DBI::dbDisconnect(ro, shutdown = TRUE))
  DBI::dbExecute(ro, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", cm_vocab_file())))
  DBI::dbExecute(ro, "SET search_path = 'main,central_vocab.main'")
  before <- cm_catalog(ro)
  expect_no_warning({
    elix <- extract_elixhauser_comorbidities(ro)
    cci <- extract_charlson_index(ro)
  })
  expect_equal(elix$elix_chf, c(1L, 1L, 0L))
  expect_equal(cci$cci_chf, c(1L, 1L, 0L))
  expect_identical(cm_catalog(ro), before)
})

# ----------------------------------------------------------------------------------------------- arguments
BAD_ARGUMENTS <- list(
  list(list(cohort_table = "no_such_table"), "does not exist"),
  list(list(cohort_table = "cohort; DROP TABLE condition_occurrence"), "cohort_table must be"),
  list(list(cohort_table = "cohort --"), "cohort_table must be"),
  list(list(cohort_table = 5), "cohort_table must be"),
  list(list(person_col = "nope"), "person_col 'nope' is not a column"),
  list(list(index_date_col = "nope"), "index_date_col 'nope' is not a column"),
  list(list(person_col = 'subject_id" FROM x; --'), "is not a column"),
  list(list(person_col = NULL), "person_col must be a column name"),
  list(list(person_col = "cohort_start_date", index_date_col = "cohort_start_date"), "different columns"),
  list(list(lookback_days = -1), "lookback_days must be a non-negative whole number or NULL"),
  list(list(lookback_days = 1.5), "lookback_days must be"),
  list(list(lookback_days = "365"), "lookback_days must be"),
  list(list(lookback_days = TRUE), "lookback_days must be"),
  list(list(lookback_days = NA_real_), "lookback_days must be"),
  list(list(lookback_days = c(30, 365)), "lookback_days must be"),
  list(list(cohort_id = "a"), "cohort_id must be a whole number"),
  list(list(cohort_id = TRUE), "cohort_id must be a whole number"),
  list(list(cohort_id = 1.5), "cohort_id must be a whole number"),
  list(list(source_and_standard = 1), "source_and_standard must be TRUE or FALSE"),
  list(list(source_and_standard = NA), "source_and_standard must be TRUE or FALSE"),
  list(list(hierarchy_adjusted = "yes"), "hierarchy_adjusted must be TRUE or FALSE")
)

test_that("bad arguments raise clear errors", {
  for (index in names(INDEXES)) {
    for (case in BAD_ARGUMENTS) {
      expect_error(do.call(INDEXES[[index]]$fn, c(list(shared), case[[1]])), case[[2]],
                   info = paste(index, deparse(case[[1]])))
    }
  }
  # nothing was dropped by the injection attempts
  expect_gt(DBI::dbGetQuery(shared, "SELECT count(*) AS n FROM condition_occurrence")$n, 0)
})

test_that("columns are never guessed", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")))
  DBI::dbExecute(con, "CREATE TABLE stays AS SELECT 1 AS person_id, DATE '2021-06-01' AS visit_start_date")
  expect_error(extract_elixhauser_comorbidities(con, "stays"),
               "person_col 'subject_id' is not a column of 'stays'.*never guessed")
  expect_error(extract_charlson_index(con, "stays", person_col = "person_id"),
               "index_date_col 'cohort_start_date' is not a column of 'stays'")
})

test_that("custom columns, schema-qualified tables and case-insensitive names work", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")))
  DBI::dbExecute(con, "CREATE SCHEMA study")
  DBI::dbExecute(con, "CREATE TABLE study.stays AS SELECT 1 AS \"Person_ID\", DATE '2021-06-01' AS visit_start_date")
  df <- extract_elixhauser_comorbidities(con, "study.stays", person_col = "person_id", index_date_col = "VISIT_START_DATE",
                                         source_and_standard = FALSE)
  expect_identical(names(df)[1:2], c("person_id", "VISIT_START_DATE"))  # output columns are named as passed
  expect_equal(df$elix_chf, 1L)
})

test_that("a missing or incomplete condition_occurrence is an error", {
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
  expect_error(extract_elixhauser_comorbidities(con), "condition_occurrence does not exist")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id INTEGER, condition_concept_id INTEGER, condition_start_date DATE)")
  expect_error(extract_charlson_index(con), "condition_occurrence is missing column\\(s\\) condition_source_value")
})

test_that("a NULL person or index date is an error, not a silent zero row", {
  con <- cm_mini(cm_conds(list(1, 0, "2021-05-01", "I50.9")))
  DBI::dbExecute(con, "INSERT INTO cohort VALUES (1, 1, DATE '2021-06-01', DATE '2021-06-01'), (1, 2, NULL, NULL)")
  expect_error(extract_elixhauser_comorbidities(con), "1 row.*NULL subject_id or cohort_start_date")
  DBI::dbExecute(con, "UPDATE cohort SET cohort_definition_id = 2 WHERE subject_id = 2")  # now excluded by the filter
  expect_equal(extract_elixhauser_comorbidities(con, cohort_id = 1, source_and_standard = FALSE)$elix_chf, 1L)
})

# ------------------------------------------------------------------------------------------ code-table loader
test_that("the code-table loader rejects malformed files and a missing file", {
  lines <- readLines(cm_code_path("charlson"))
  expect_true(startsWith(lines[2], "charlson,mi,cci_mi,") && grepl(",1,ICD10CM,I21", lines[2], fixed = TRUE))
  edits <- list(
    list(function(l) c(sub("code_system", "system", l[1], fixed = TRUE), l[-1]), "expected header"),
    list(function(l) c(l[1], sub(",ICD10CM,", ",ICD11,", l[2], fixed = TRUE), l[-(1:2)]), "unknown code_system"),
    list(function(l) c(l[1], sub(",1,ICD10CM,", ",x,ICD10CM,", l[2], fixed = TRUE), l[-(1:2)]), "weight must be an integer"),
    list(function(l) c(l[1], sub(",1,ICD10CM,", ",5,ICD10CM,", l[2], fixed = TRUE), l[-(1:2)]), "more than one column or weight"),
    list(function(l) c(l[1], sub(",I21", ",i21", l[2], fixed = TRUE), l[-(1:2)]), "upper-case"),
    list(function(l) c(l[1], sub("charlson,", "elixhauser,", l[2], fixed = TRUE), l[-(1:2)]), "index must be 'charlson'"),
    list(function(l) c(l[1], sub(",mi,cci_mi", ",MI,cci_mi", l[2], fixed = TRUE), l[-(1:2)]), "lower-case identifiers")
  )
  for (e in edits) {
    dir <- withr::local_tempdir()
    writeLines(e[[1]](lines), file.path(dir, "charlson_codes.csv"))
    testthat::local_mocked_bindings(.comorb_code_path = function(file) file.path(dir, file))
    con <- cm_mini()
    expect_error(extract_charlson_index(con), e[[2]], info = e[[2]])
  }
  empty <- withr::local_tempdir()
  testthat::local_mocked_bindings(.comorb_code_path = function(file) file.path(empty, file))
  expect_error(extract_elixhauser_comorbidities(cm_mini()), "inst/extdata/comorbidity/elixhauser_codes.csv")
})

# ------------------------------------------------------------------------------------------------- scale
test_that("20,000 index rows and 400,000 condition rows finish in seconds", {
  codes <- c("I50.9", "I10", "E11.9", "J44.9", "C50.9", "C78.0", "K72.9", "F32.9", "E66.9", "428.0", "250.00", "401.9",
             "Z00.00", "J01.90", "S72.001A", "N18.3", "I48.91", "D64.9", "M54.5", "R07.9", "K21.9", "E78.5", "G47.33",
             "F41.1", "I25.10", "N39.0", "L03.90", "H25.9", "M17.9", "Z79.4")
  rows <- utils::read.csv(cm_code_path("elixhauser"), stringsAsFactors = FALSE, colClasses = "character")
  ancestors <- sort(unique(as.numeric(rows$code[rows$code_system == "SNOMED_ANCESTOR"])))
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE))
  anc_list <- paste(sprintf("%.0f", ancestors), collapse = ", ")
  DBI::dbExecute(con, sprintf("CREATE TABLE concept_ancestor AS
    SELECT CAST(a AS BIGINT) AS ancestor_concept_id, CAST(a AS BIGINT) * 1000 + k AS descendant_concept_id,
           1 AS min_levels_of_separation, 1 AS max_levels_of_separation
    FROM (SELECT unnest([%s]::BIGINT[]) AS a) CROSS JOIN range(1, 51) AS t(k)", anc_list))
  DBI::dbExecute(con, sprintf("INSERT INTO concept_ancestor SELECT a, a, 0, 0 FROM (SELECT unnest([%s]::BIGINT[]) AS a)",
                              anc_list))
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS cohort_definition_id, i + 1 AS subject_id,
    DATE '2021-01-01' + CAST(i % 500 AS INTEGER) AS cohort_start_date,
    DATE '2021-01-01' + CAST(i % 500 AS INTEGER) AS cohort_end_date FROM range(20000) AS t(i)")
  DBI::dbExecute(con, sprintf("CREATE TABLE condition_occurrence AS SELECT i AS condition_occurrence_id,
    (i %% 20000) + 1 AS person_id,
    CASE WHEN i %% 7 = 0 THEN CAST(CAST(list_element([%s]::BIGINT[], 1 + CAST(hash(i) %% 40 AS INTEGER)) AS BIGINT) * 1000
         + 1 + CAST(hash(i + 1) %% 50 AS INTEGER) AS BIGINT) ELSE 0 END AS condition_concept_id,
    DATE '2020-01-01' + CAST(hash(i * 7) %% 900 AS INTEGER) AS condition_start_date,
    list_element([%s], 1 + CAST(hash(i * 3) %% 30 AS INTEGER)) AS condition_source_value
    FROM range(400000) AS t(i)", paste(sprintf("%.0f", ancestors[1:40]), collapse = ", "),
                              paste(sprintf("'%s'", codes), collapse = ", ")))
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM condition_occurrence")$n, 400000)

  elapsed <- system.time({
    elix <- extract_elixhauser_comorbidities(con)
    cci <- extract_charlson_index(con)
  })[["elapsed"]]
  expect_equal(nrow(elix), 20000L)
  expect_equal(nrow(cci), 20000L)
  expect_lt(elapsed, 30)

  # independent plain-SQL check of one domain (chf): ICD prefixes by LIKE, standard side by IN (descendants)
  chf <- rows[rows$domain == "chf", ]
  like <- paste(sprintf("x.src LIKE '%s%%'", chf$code[chf$code_system != "SNOMED_ANCESTOR"]), collapse = " OR ")
  anc <- paste(chf$code[chf$code_system == "SNOMED_ANCESTOR"], collapse = ", ")
  expected <- DBI::dbGetQuery(con, sprintf("
    SELECT DISTINCT c.subject_id FROM cohort c JOIN (
        SELECT person_id, condition_concept_id, condition_start_date,
               upper(replace(replace(condition_source_value, '.', ''), ' ', '')) AS src
        FROM condition_occurrence) x ON x.person_id = c.subject_id
    WHERE x.condition_start_date < c.cohort_start_date AND x.condition_start_date >= c.cohort_start_date - 365
      AND ((%s) OR x.condition_concept_id IN (
           SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id IN (%s)))
    ORDER BY 1", like, anc))$subject_id
  expect_gt(length(expected), 0)
  expect_equal(elix$subject_id[elix$elix_chf == 1], expected)
  expect_gt(sum(as.matrix(elix[cm_cols("elixhauser")])), 0)
  expect_gt(sum(as.matrix(cci[cm_cols("charlson")])), 0)
})

# ------------------------------------------------------------------------------ optional real vocabulary
test_that("real vocabulary: standard concepts resolve through central_vocab", {
  vocab <- Sys.getenv("OMOP_VOCAB_DB", "")
  skip_if(!nzchar(vocab) || !file.exists(vocab), "set OMOP_VOCAB_DB to an Athena vocabulary DuckDB file")
  # 316139 heart failure, 312327 acute myocardial infarction (not a pulmonary-circulation disorder), 201826 type 2
  # diabetes mellitus, 320128 essential hypertension, 316866 hypertensive disorder (the parent, so not uncomplicated
  # hypertension by itself)
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", vocab)))
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER,
                         condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 1, 316139, DATE '2021-05-01', NULL),
    (2, 2, 312327, DATE '2021-05-01', NULL), (3, 3, 201826, DATE '2021-05-01', NULL),
    (4, 4, 320128, DATE '2021-05-01', NULL), (5, 5, 316866, DATE '2021-05-01', NULL)")
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT i AS subject_id, DATE '2021-06-01' AS cohort_start_date FROM range(1, 7) AS t(i)")
  DBI::dbExecute(con, "SET search_path = 'main,central_vocab.main'")
  expect_no_warning({
    elix <- extract_elixhauser_comorbidities(con)
    cci <- extract_charlson_index(con)
  })
  same_set(flagged(elix, "elixhauser", 1), "chf")
  same_set(flagged(cci, "charlson", 1), "chf")
  same_set(flagged(elix, "elixhauser", 2), character(0))
  same_set(flagged(cci, "charlson", 2), "mi")
  same_set(flagged(elix, "elixhauser", 3), "dm_uncomp")
  same_set(flagged(cci, "charlson", 3), "dm_uncomp")
  same_set(flagged(elix, "elixhauser", 4), "htn_uncomp")
  same_set(flagged(elix, "elixhauser", 5), character(0))
  same_set(flagged(elix, "elixhauser", 6), character(0))
})
