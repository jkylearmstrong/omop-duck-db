# omop_connect() (RFC 1.1), the concept-ancestry macros (RFC 1.2) and clamp_physiologic (RFC 6.1).
# Mirrors tests/test_omop_connect.py case for case. Everything runs on tiny synthetic fixtures (a site CDM
# plus a stand-in central vocabulary database); the last test uses a real Athena vocabulary only when the
# OMOP_VOCAB_DB environment variable points at one.

skip_if_not_installed("withr")

# --------------------------------------------------------------------------------------------- fixtures
# Synthetic stand-ins, not real Athena ids.
OC_CONCEPTS <- data.frame(
  concept_id = c(1000, 1001, 1002, 1003, 2000, 9001),
  concept_name = c(
    "Diabetes mellitus", "Type 2 diabetes mellitus", "Type 2 diabetes mellitus with complication",
    "Type 1 diabetes mellitus", "Hypertensive disorder", "Type 2 diabetes mellitus without complications"
  ),
  vocabulary_id = c(rep("SNOMED", 5), "ICD10CM"),
  concept_code = c("DM", "T2DM", "T2DMC", "T1DM", "HTN", "E11.9"),
  stringsAsFactors = FALSE
)
# Athena's concept_ancestor carries the self row (0, 0) for every concept.
OC_ANCESTORS <- data.frame(
  a = c(1000, 1000, 1000, 1000, 1001, 1001, 1002, 1003, 2000),
  d = c(1000, 1001, 1002, 1003, 1001, 1002, 1002, 1003, 2000),
  lo = c(0, 1, 2, 1, 0, 1, 0, 0, 0),
  hi = c(0, 1, 2, 1, 0, 1, 0, 0, 0)
)
OC_VOCAB_TABLES <- c("concept", "concept_ancestor", "concept_relationship")

oc_make_vocab <- function(path, tables = OC_VOCAB_TABLES) {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  if ("concept" %in% tables) {
    DBI::dbExecute(con, paste(
      "CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR, domain_id VARCHAR, vocabulary_id VARCHAR,",
      "concept_class_id VARCHAR, standard_concept VARCHAR, concept_code VARCHAR,",
      "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)"
    ))
    for (i in seq_len(nrow(OC_CONCEPTS))) {
      r <- OC_CONCEPTS[i, ]
      std <- if (r$vocabulary_id == "SNOMED") "'S'" else "NULL"
      DBI::dbExecute(con, sprintf(
        "INSERT INTO concept VALUES (%d, '%s', 'Condition', '%s', 'Disorder', %s, '%s', DATE '1970-01-01', DATE '2099-12-31', NULL)",
        r$concept_id, r$concept_name, r$vocabulary_id, std, r$concept_code
      ))
    }
  }
  if ("concept_ancestor" %in% tables) {
    DBI::dbExecute(con, paste(
      "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER,",
      "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)"
    ))
    for (i in seq_len(nrow(OC_ANCESTORS))) {
      DBI::dbExecute(con, do.call(sprintf, c(list("INSERT INTO concept_ancestor VALUES (%d, %d, %d, %d)"), as.list(OC_ANCESTORS[i, ]))))
    }
  }
  if ("concept_relationship" %in% tables) {
    DBI::dbExecute(con, paste(
      "CREATE TABLE concept_relationship (concept_id_1 INTEGER, concept_id_2 INTEGER, relationship_id VARCHAR,",
      "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)"
    ))
    DBI::dbExecute(con, paste(
      "INSERT INTO concept_relationship VALUES",
      "(9001, 1001, 'Maps to', DATE '1970-01-01', DATE '2099-12-31', NULL),",
      "(1001, 9001, 'Mapped from', DATE '1970-01-01', DATE '2099-12-31', NULL)"
    ))
  }
  invisible(path)
}

# A CDM built by build_schema() (so every vocabulary table exists, empty) with a few patients.
oc_template <- local({
  path <- tempfile(fileext = ".duckdb")
  utils::capture.output(omopduckdb::build_schema(db_path = path))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  for (pid in 1:4) {
    DBI::dbExecute(con, sprintf(
      "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1970, 8527, 38003564)",
      pid
    ))
  }
  DBI::dbExecute(con, paste(
    "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id,",
    "condition_start_date, condition_type_concept_id) VALUES",
    "(1, 1, 1001, DATE '2020-01-01', 32020), (2, 2, 1002, DATE '2020-01-01', 32020),",
    "(3, 3, 2000, DATE '2020-01-01', 32020), (4, 4, 1003, DATE '2020-01-01', 32020)"
  ))
  DBI::dbDisconnect(con, shutdown = TRUE)
  gc()
  path
})
withr::defer(unlink(oc_template), testthat::teardown_env())

# A site directory holding cdm.duckdb (empty vocabulary tables) and a sibling central_vocabulary.duckdb.
oc_site <- function(env = parent.frame(), vocab_name = "central_vocabulary.duckdb") {
  dir <- withr::local_tempdir(.local_envir = env)
  cdm <- file.path(dir, "cdm.duckdb")
  file.copy(oc_template, cdm)
  vocab <- NULL
  if (!is.null(vocab_name)) {
    vocab <- file.path(dir, vocab_name)
    oc_make_vocab(vocab)
  }
  list(dir = dir, cdm = cdm, vocab = vocab)
}

oc_close <- function(con) {
  if (DBI::dbIsValid(con)) DBI::dbDisconnect(con, shutdown = TRUE)
  invisible(gc())
}

# omop_connect() with its warnings captured (and muffled): list(con, warnings).
oc_connect <- function(..., env = parent.frame()) {
  warns <- character()
  con <- withCallingHandlers(
    omop_connect(...),
    warning = function(w) {
      warns <<- c(warns, conditionMessage(w))
      invokeRestart("muffleWarning")
    }
  )
  withr::defer(oc_close(con), envir = env)
  list(con = con, warnings = warns[startsWith(warns, "omop_connect")])
}

oc_q <- function(con, sql) DBI::dbGetQuery(con, sql)
oc_n <- function(con, table) oc_q(con, paste("SELECT COUNT(*) AS n FROM", table))$n
oc_ids <- function(con, sql) sort(oc_q(con, sql)[[1]])
oc_dbs <- function(con) {
  res <- oc_q(con, "SELECT database_name, path FROM duckdb_databases() WHERE NOT internal")
  stats::setNames(res$path, res$database_name)
}
oc_setting <- function(con, name) oc_q(con, sprintf("SELECT value FROM duckdb_settings() WHERE name = '%s'", name))$value
oc_macros <- function(con, database) {
  oc_q(con, sprintf(
    "SELECT function_name FROM duckdb_functions() WHERE function_type IN ('macro', 'table_macro') AND NOT internal AND database_name = '%s'",
    database
  ))$function_name
}
oc_temp_views <- function(con) {
  sort(oc_q(con, "SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal")$view_name)
}
oc_snapshot <- function(path) list(md5 = unname(tools::md5sum(path)), size = file.size(path), mtime = file.mtime(path))
oc_samefile <- function(a, b) same_file(a, b)

oc_shipped_macro_names <- function() {
  names <- character()
  for (mf in c("mapping_macros.sql", "cohort_readmission.sql", "table1_aggregations.sql", "cohort_mortality.sql")) {
    code <- gsub("--[^\n]*", "", paste(readLines(macro_file_path(mf)), collapse = "\n"))
    m <- regmatches(code, gregexpr("CREATE OR REPLACE MACRO (\\w+)", code, perl = TRUE))[[1]]
    names <- c(names, sub("CREATE OR REPLACE MACRO ", "", m, fixed = TRUE))
  }
  names
}

# ------------------------------------------------------------------------------- path / connection input
test_that("a path attaches the sibling vocabulary and queries need no schema prefix", {
  s <- oc_site()
  for (path in list(s$cdm, normalizePath(s$cdm, winslash = "\\", mustWork = FALSE))) {
    r <- oc_connect(path, read_only = TRUE)
    con <- r$con
    expect_s4_class(con, "duckdb_connection")
    expect_equal(r$warnings, character())
    expect_true(oc_samefile(oc_dbs(con)[["central_vocab"]], s$vocab))
    expect_equal(oc_setting(con, "search_path"), "main,central_vocab.main")
    rows <- oc_q(con, paste(
      "SELECT c.concept_name, COUNT(*) AS n FROM condition_occurrence co",
      "JOIN concept c ON co.condition_concept_id = c.concept_id GROUP BY 1 ORDER BY 1"
    ))
    expect_equal(rows$concept_name, c(
      "Hypertensive disorder", "Type 1 diabetes mellitus", "Type 2 diabetes mellitus",
      "Type 2 diabetes mellitus with complication"
    ))
    expect_equal(rows$n, rep(1, 4))
    oc_close(con)
  }
})

test_that("an existing connection is reused and never closed", {
  s <- oc_site()
  con0 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  withr::defer(oc_close(con0))
  expect_identical(omop_connect(con0, vocab_db = s$vocab), con0)
  expect_equal(oc_n(con0, "concept"), nrow(OC_CONCEPTS))
  # a failing call must not close a connection it does not own either
  expect_error(omop_connect(con0, vocab_db = file.path(s$dir, "nope.duckdb")), "Vocabulary database not found")
  expect_true(DBI::dbIsValid(con0))
  expect_equal(oc_q(con0, "SELECT 1 AS x")$x, 1)
  expect_error(omop_connect(con0, vocab_db = s$cdm), "primary database itself")
  expect_true(DBI::dbIsValid(con0))
  oc_close(con0)
  # connecting never wrote anything into the (writable) database: macros live in the session only
  check <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(check))
  expect_equal(oc_macros(check, "cdm"), character())
})

test_that("an existing read-only connection works, and read_only = TRUE on a writable one warns", {
  s <- oc_site()
  con0 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  expect_identical(omop_connect(con0, vocab_db = s$vocab), con0)
  expect_equal(oc_q(con0, "SELECT clamp_physiologic(500, 0, 300) AS x")$x, 300)
  oc_close(con0)

  con1 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  withr::defer(oc_close(con1))
  expect_warning(omop_connect(con1, vocab_db = s$vocab, read_only = TRUE), "read_only = TRUE has no effect")
})

test_that("read_only = TRUE on a missing database is an error and creates nothing", {
  missing <- file.path(withr::local_tempdir(), "does_not_exist.duckdb")
  expect_error(omop_connect(missing, read_only = TRUE), "Database file not found")
  expect_false(file.exists(missing))
})

test_that("read_only = TRUE with an in-memory database is a clear error", {
  # an in-memory database has no file to open read-only; R and Python both refuse it up front
  for (target in c(":memory:", "", ":memory:named")) {
    expect_error(omop_connect(target, read_only = TRUE), "in-memory database")
  }
})

test_that("a leading tilde is expanded in the database and vocabulary paths", {
  skip_if(identical(path.expand("~"), "~"), "no home directory to expand '~' to")
  # Missing paths only: nothing is created in the home directory. The messages show the expanded path.
  db <- "~/omop_connect_test_missing.duckdb"
  expect_error(
    omop_connect(db, read_only = TRUE),
    paste0("Database file not found: ", path.expand(db)),
    fixed = TRUE
  )
  expect_error(
    omop_connect(":memory:", vocab_db = "~/omop_connect_test_missing_vocab.duckdb"),
    paste0("Vocabulary database not found: ", normalizePath(
      "~/omop_connect_test_missing_vocab.duckdb", winslash = "/", mustWork = FALSE
    )),
    fixed = TRUE
  )
  expect_false(file.exists(path.expand(db)))
})

test_that("the read-only primary and the attached vocabulary really are read-only", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_error(DBI::dbExecute(con, paste(
    "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)",
    "VALUES (99, 8507, 1970, 8527, 38003564)"
  )), "read-only")
  expect_error(DBI::dbExecute(con, "INSERT INTO central_vocab.main.concept_ancestor VALUES (1, 1, 0, 0)"), "read-only")
})

# ------------------------------------------------------------------------------------------- discovery
test_that("auto-discovery finds each sibling name", {
  for (name in c("central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb")) {
    s <- oc_site(vocab_name = name)
    con <- oc_connect(s$cdm, read_only = TRUE)$con
    expect_true(oc_samefile(oc_dbs(con)[["central_vocab"]], file.path(s$dir, name)), info = name)
    oc_close(con)
  }
})

test_that("auto-discovery priority is central_vocabulary, vocabulary, vocab", {
  s <- oc_site(vocab_name = "vocab.duckdb")
  oc_make_vocab(file.path(s$dir, "vocabulary.duckdb"))
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_true(oc_samefile(oc_dbs(con)[["central_vocab"]], file.path(s$dir, "vocabulary.duckdb")))
})

test_that("auto-discovery only looks next to the database, never in the working directory", {
  elsewhere <- withr::local_tempdir()
  dir.create(file.path(elsewhere, "derived", "omop_duckdb"), recursive = TRUE)
  oc_make_vocab(file.path(elsewhere, "central_vocabulary.duckdb"))
  oc_make_vocab(file.path(elsewhere, "derived", "omop_duckdb", "central_vocabulary.duckdb"))
  s <- oc_site(vocab_name = NULL)
  withr::local_dir(elsewhere)
  r <- oc_connect(s$cdm, read_only = TRUE)
  expect_false("central_vocab" %in% names(oc_dbs(r$con)))
  expect_match(r$warnings, "No vocabulary available", fixed = TRUE)
})

test_that("discovery never attaches the primary to itself", {
  for (name in c("central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb")) {
    dir <- withr::local_tempdir()
    cdm <- file.path(dir, name) # the primary database itself carries a discoverable name
    file.copy(oc_template, cdm)
    r <- oc_connect(cdm, read_only = TRUE)
    expect_false("central_vocab" %in% names(oc_dbs(r$con)), info = name)
    expect_match(r$warnings, "No vocabulary available", fixed = TRUE)
    expect_equal(oc_n(r$con, "person"), 4)
    oc_close(r$con)
  }
})

test_that("auto_attach_vocab = FALSE skips discovery, but an explicit vocabulary is still attached", {
  s <- oc_site()
  r <- oc_connect(s$cdm, read_only = TRUE, auto_attach_vocab = FALSE)
  expect_false("central_vocab" %in% names(oc_dbs(r$con)))
  expect_equal(r$warnings, character()) # the caller opted out, so no "no vocabulary" nag
  oc_close(r$con)
  r2 <- oc_connect(s$cdm, vocab_db = s$vocab, read_only = TRUE, auto_attach_vocab = FALSE)
  expect_true("central_vocab" %in% names(oc_dbs(r2$con)))
})

test_that("an explicit vocabulary wins over a sibling", {
  s <- oc_site()
  other <- file.path(withr::local_tempdir(), "other_vocab.duckdb")
  oc_make_vocab(other, tables = "concept")
  con <- oc_connect(s$cdm, vocab_db = other, read_only = TRUE)$con
  expect_true(oc_samefile(oc_dbs(con)[["central_vocab"]], other))
})

# ---------------------------------------------------------------------------------------------- errors
test_that("an explicit missing vocabulary is an error and opens nothing", {
  s <- oc_site()
  expect_error(omop_connect(s$cdm, vocab_db = file.path(s$dir, "typo.duckdb")), "Vocabulary database not found")
  expect_error(omop_connect(s$cdm, vocab_db = s$dir), "Vocabulary database not found") # a directory
  # no silent fall-back to the sibling that auto-discovery would have found
  expect_error(
    omop_connect(s$cdm, vocab_db = file.path(s$dir, "typo.duckdb"), auto_attach_vocab = TRUE),
    "Vocabulary database not found"
  )
  # the primary database was never opened, so it can still be opened in any mode
  check <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  withr::defer(oc_close(check))
  expect_equal(oc_n(check, "person"), 4)
})

test_that("a vocabulary equal to the primary is an error and the opened database is released", {
  s <- oc_site()
  expect_error(omop_connect(s$cdm, vocab_db = s$cdm), "primary database itself")
  # DuckDB refuses a read-only open of a file that still has a read-write instance in this process
  check <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(check))
  expect_equal(oc_n(check, "person"), 4)
})

test_that("a failure during setup closes the connection omop_connect opened, but not one it was given", {
  skip_if(utils::packageVersion("testthat") < "3.2.0", "local_mocked_bindings() needs testthat >= 3.2.0")
  s <- oc_site()
  opened <- NULL
  testthat::local_mocked_bindings(configure_omop_connection = function(con, ...) {
    opened <<- con
    stop("boom")
  })
  expect_error(omop_connect(s$cdm), "boom")
  expect_false(DBI::dbIsValid(opened))
  con0 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  withr::defer(oc_close(con0))
  expect_error(omop_connect(con0), "boom")
  expect_true(DBI::dbIsValid(con0))
})

test_that("a different database already attached as central_vocab is an error", {
  s <- oc_site()
  other <- file.path(withr::local_tempdir(), "other.duckdb")
  oc_make_vocab(other)
  con0 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(con0))
  DBI::dbExecute(con0, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", other)))
  expect_error(omop_connect(con0, vocab_db = s$vocab), "different database is already attached")
})

test_that("a closed connection and malformed arguments are rejected", {
  s <- oc_site()
  con0 <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  DBI::dbDisconnect(con0, shutdown = TRUE)
  expect_error(omop_connect(con0), "closed or invalid")
  expect_error(omop_connect(c("a", "b")), "single file path")
  expect_error(omop_connect(s$cdm, vocab_db = c("a", "b")), "single file path")
})

test_that("paths with quotes and spaces are escaped", {
  dir <- file.path(withr::local_tempdir(), "O'Brien's site")
  dir.create(dir)
  file.copy(oc_template, file.path(dir, "cdm.duckdb"))
  oc_make_vocab(file.path(dir, "central_vocabulary.duckdb"))
  for (vocab_db in list(NULL, file.path(dir, "central_vocabulary.duckdb"))) {
    con <- oc_connect(file.path(dir, "cdm.duckdb"), vocab_db = vocab_db, read_only = TRUE)$con
    expect_true(oc_samefile(oc_dbs(con)[["central_vocab"]], file.path(dir, "central_vocabulary.duckdb")))
    expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
    oc_close(con)
  }
})

# ------------------------------------------------------------------------------------------ idempotency
test_that("attaching is idempotent", {
  s <- oc_site()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(con))
  for (i in 1:3) omop_connect(con, vocab_db = s$vocab)
  expect_equal(sum(names(oc_dbs(con)) == "central_vocab"), 1)
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
  # the same file spelled differently (relative / backslashes) is the same attachment
  withr::local_dir(s$dir)
  omop_connect(con, vocab_db = "central_vocabulary.duckdb")
  omop_connect(con, vocab_db = gsub("/", "\\\\", s$vocab))
  omop_connect(con) # auto-discovery on an already-attached connection is a no-op too
  expect_equal(sum(names(oc_dbs(con)) == "central_vocab"), 1)
})

test_that("a pre-attached vocabulary is adopted", {
  s <- oc_site()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(con))
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS central_vocab (READ_ONLY)", gsub("\\\\", "/", s$vocab)))
  omop_connect(con)
  expect_equal(oc_setting(con, "search_path"), "main,central_vocab.main")
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
})

test_that("two connections to the same files both work", {
  s <- oc_site()
  con_a <- oc_connect(s$cdm, read_only = TRUE)$con
  con_b <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(oc_n(con_a, "descendants_of(1000)"), 4)
  expect_equal(oc_n(con_b, "descendants_of(1000)"), 4)
})

# ---------------------------------------------------------------------------------- search_path precedence
test_that("a second connection to the database needs omop_connect itself", {
  # search_path, the TEMP views and the TEMP macros are per connection (documented in the roxygen block)
  s <- oc_site()
  con <- oc_connect(s$cdm, vocab_db = s$vocab, read_only = TRUE)$con
  bare <- DBI::dbConnect(con@driver)
  # the pitfall: the empty local table shadows the vocabulary, silently, and the macros are gone
  expect_equal(oc_n(bare, "concept"), 0)
  expect_false(identical(oc_setting(bare, "search_path"), oc_setting(con, "search_path")))
  expect_error(oc_q(bare, "SELECT * FROM descendants_of(1000)"), "Catalog Error")
  # the documented remedy: pass the connection through omop_connect itself (same attachment, no conflict)
  fixed <- oc_connect(bare)$con
  expect_equal(oc_n(fixed, "concept"), nrow(OC_CONCEPTS))
  expect_equal(oc_ids(fixed, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
})

test_that("the documentation warns about second connections", {
  src <- file.path(getwd(), "..", "..", "R", "load_vocabulary.R")
  skip_if_not(file.exists(src), "package source not available")
  text <- paste(readLines(src), collapse = " ")
  expect_match(text, "Per-connection state", fixed = TRUE)
  expect_match(text, "DBI::dbConnect(con@driver)", fixed = TRUE)
})

test_that("empty local vocabulary tables fall through to the attached vocabulary", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  # build_schema() leaves these empty in the site database; they must not shadow the attached vocabulary
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
  expect_equal(oc_n(con, "concept_ancestor"), nrow(OC_ANCESTORS))
  expect_equal(oc_n(con, "concept_relationship"), 2)
  expect_equal(oc_n(con, "person"), 4) # tables outside the vocabulary are untouched
})

test_that("a populated local table wins over the attached vocabulary", {
  s <- oc_site()
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  DBI::dbExecute(w, paste(
    "INSERT INTO concept VALUES (1001, 'LOCAL type 2 diabetes', 'Condition', 'SNOMED', 'Disorder', 'S', 'T2DM',",
    "DATE '1970-01-01', DATE '2099-12-31', NULL)"
  ))
  oc_close(w)
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(oc_q(con, "SELECT concept_name FROM concept WHERE concept_id = 1001")$concept_name, "LOCAL type 2 diabetes")
  expect_equal(oc_n(con, "concept"), 1)
  # precedence is per table: concept_ancestor is still empty locally, so it resolves to the vocabulary
  expect_equal(oc_n(con, "concept_ancestor"), nrow(OC_ANCESTORS))
  # and the attached vocabulary is still reachable explicitly
  expect_equal(
    oc_q(con, "SELECT concept_name FROM central_vocab.concept WHERE concept_id = 1001")$concept_name,
    "Type 2 diabetes mellitus"
  )
})

test_that("tables without a local copy resolve through the search path", {
  s <- oc_site()
  bare <- file.path(s$dir, "bare.duckdb") # no CDM schema at all: only a person table
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = bare)
  DBI::dbExecute(w, "CREATE TABLE person (person_id INTEGER)")
  oc_close(w)
  con <- oc_connect(bare, vocab_db = s$vocab, read_only = TRUE)$con
  expect_equal(oc_n(con, "concept_ancestor"), nrow(OC_ANCESTORS))
  expect_equal(oc_temp_views(con), character()) # resolved by search_path alone: no shadow views needed
  expect_equal(oc_n(con, "descendants_of(1000)"), 4)
})

test_that("new objects are created in the primary database, not in the vocabulary", {
  s <- oc_site()
  con <- oc_connect(s$cdm, vocab_db = s$vocab)$con
  DBI::dbExecute(con, "CREATE TABLE my_cohort AS SELECT person_id FROM person")
  where <- oc_q(con, "SELECT database_name FROM duckdb_tables() WHERE table_name = 'my_cohort'")$database_name
  expect_equal(where, "cdm")
})

test_that("a search_path set on a passed-in connection is kept with central_vocab appended", {
  s <- oc_site()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  withr::defer(oc_close(con))
  DBI::dbExecute(con, "CREATE SCHEMA analysis")
  DBI::dbExecute(con, "CREATE TABLE analysis.cohort_tbl AS SELECT 7 AS person_id")
  DBI::dbExecute(con, "SET search_path = 'analysis,main'")
  omop_connect(con, vocab_db = s$vocab)
  expect_equal(oc_setting(con, "search_path"), "analysis,main,central_vocab.main")
  expect_equal(oc_q(con, "SELECT person_id FROM cohort_tbl")$person_id, 7) # caller's schema still resolves
  expect_equal(oc_n(con, "person"), 4)
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
  omop_connect(con, vocab_db = s$vocab) # connecting again does not stack another entry
  expect_equal(oc_setting(con, "search_path"), "analysis,main,central_vocab.main")
})

test_that("the search path is extended, not replaced", {
  cases <- list(
    list("", "main,central_vocab.main"),
    list(NULL, "main,central_vocab.main"),
    list(NA_character_, "main,central_vocab.main"),
    list("main", "main,central_vocab.main"),
    list("analysis,main", "analysis,main,central_vocab.main"),
    list("main,central_vocab.main", "main,central_vocab.main"),
    list("central_vocab.main,main", "central_vocab.main,main"),
    list("CENTRAL_VOCAB.main,main", "CENTRAL_VOCAB.main,main"),
    list("\"central_vocab\".main", "\"central_vocab\".main"),
    list("central_vocab", "central_vocab"),
    list("cdm.central_vocab", "cdm.central_vocab,central_vocab.main") # a schema that merely has that name
  )
  for (case in cases) {
    expect_identical(search_path_with_central_vocab(case[[1]]), case[[2]])
  }
})

# ----------------------------------------------------------------------- read-only + macros + untouched
test_that("a read-only connect loads macros as TEMP macros and leaves both files untouched", {
  s <- oc_site()
  before <- list(cdm = oc_snapshot(s$cdm), vocab = oc_snapshot(s$vocab))
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_true(all(oc_shipped_macro_names() %in% oc_macros(con, "temp"))) # every shipped macro, as TEMP
  expect_equal(oc_macros(con, "cdm"), character())
  expect_equal(oc_macros(con, "central_vocab"), character())
  in_vocab <- oc_q(con, "SELECT table_name FROM duckdb_tables() WHERE database_name = 'central_vocab'")$table_name
  expect_setequal(in_vocab, OC_VOCAB_TABLES)
  # exercise macros and vocabulary reads
  expect_equal(oc_n(con, "descendants_of(1000)"), 4)
  expect_equal(oc_q(con, "SELECT map_to_standard_concept_id('ICD10CM', 'E11.9') AS x")$x, 1001)
  expect_equal(oc_q(con, "SELECT clamp_physiologic(500, 0, 300) AS x")$x, 300)
  oc_close(con)
  expect_equal(list(cdm = oc_snapshot(s$cdm), vocab = oc_snapshot(s$vocab)), before)
  expect_length(list.files(s$dir, pattern = "\\.wal$"), 0)
})

test_that("load_mapping_macros keeps persisting macros on writable databases (ETL path)", {
  path <- withr::local_tempfile(fileext = ".duckdb")
  utils::capture.output(omopduckdb::build_schema(db_path = path))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  load_mapping_macros(con)
  oc_close(con)
  ro <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
  db <- sub("\\.duckdb$", "", basename(path))
  expect_true(all(oc_shipped_macro_names() %in% oc_macros(ro, db))) # persisted in the file
  expect_match(format(oc_q(ro, "SELECT parse_omop_date('2020-01-02') AS d")$d), "2020-01-02", fixed = TRUE)
  expect_equal(load_mapping_macros(ro), character()) # read-only: falls back to TEMP instead of failing
  expect_true(all(oc_shipped_macro_names() %in% oc_macros(ro, "temp")))
  oc_close(ro)

  path2 <- withr::local_tempfile(fileext = ".duckdb")
  utils::capture.output(omopduckdb::build_schema(db_path = path2))
  con2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = path2)
  load_mapping_macros(con2, temporary = TRUE)
  oc_close(con2)
  ro2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = path2, read_only = TRUE)
  withr::defer(oc_close(ro2))
  expect_equal(oc_macros(ro2, sub("\\.duckdb$", "", basename(path2))), character())
})

test_that("the shipped macro files split and rewrite cleanly", {
  for (mf in c("mapping_macros.sql", "cohort_readmission.sql", "table1_aggregations.sql", "cohort_mortality.sql")) {
    sql <- paste(readLines(macro_file_path(mf)), collapse = "\n")
    code <- gsub("--[^\n]*", "", sql)
    stmts <- split_macro_statements(sql)
    expect_gt(length(stmts), 0)
    expect_true(all(startsWith(stmts, "CREATE OR REPLACE MACRO ")), info = mf)
    expect_equal(length(stmts), lengths(regmatches(code, gregexpr("\\bMACRO\\b", code, perl = TRUE))), info = mf)
    literals <- regmatches(code, gregexpr("'(?:[^']|'')*'", code, perl = TRUE))[[1]]
    expect_false(any(grepl(";", literals, fixed = TRUE) | grepl("--", literals, fixed = TRUE)), info = mf)
  }
})

test_that("a missing macro file is reported, not skipped silently", {
  skip_if(utils::packageVersion("testthat") < "3.2.0", "local_mocked_bindings() needs testthat >= 3.2.0")
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(oc_close(con))
  testthat::local_mocked_bindings(macro_file_path = function(mf) file.path(tempdir(), "no_such_dir", mf))
  warns <- character()
  withCallingHandlers(
    load_mapping_macros(con),
    warning = function(w) {
      warns <<- c(warns, conditionMessage(w))
      invokeRestart("muffleWarning")
    }
  )
  expect_length(warns, 4)
  expect_match(warns[[1]], "'mapping_macros.sql' was not found", fixed = TRUE)
})

# ------------------------------------------------------------------------------------- macro semantics
test_that("descendants_of includes self and all levels", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(names(oc_q(con, "SELECT * FROM descendants_of(1000)")), "concept_id")
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1000)"), c(1000, 1001, 1002, 1003))
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1002)"), 1002) # a leaf: just itself
  # Athena's concept_ancestor carries the self row; the macro relies on it rather than adding one
  expect_equal(oc_n(con, "concept_ancestor WHERE ancestor_concept_id = 1000 AND descendant_concept_id = 1000"), 1)
})

test_that("ancestors_of includes self and all levels", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(names(oc_q(con, "SELECT * FROM ancestors_of(1002)")), "concept_id")
  expect_equal(oc_ids(con, "SELECT concept_id FROM ancestors_of(1002)"), c(1000, 1001, 1002))
  expect_equal(oc_ids(con, "SELECT concept_id FROM ancestors_of(1000)"), 1000)
  expect_equal(oc_ids(con, "SELECT concept_id FROM ancestors_of(1003)"), c(1000, 1003))
})

test_that("ancestry macros return nothing for unknown or NULL ids", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  for (macro in c("descendants_of", "ancestors_of")) {
    expect_equal(nrow(oc_q(con, sprintf("SELECT * FROM %s(999999)", macro))), 0)
    expect_equal(nrow(oc_q(con, sprintf("SELECT * FROM %s(NULL)", macro))), 0)
  }
})

test_that("ancestry macros work in subqueries, joins and correlated use", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  persons <- oc_q(con, paste(
    "SELECT person_id FROM condition_occurrence",
    "WHERE condition_concept_id IN (SELECT concept_id FROM descendants_of(1001)) ORDER BY 1"
  ))$person_id
  expect_equal(persons, c(1, 2)) # type 2 diabetes and its descendant, not type 1 and not hypertension
  joined <- oc_q(con, paste(
    "SELECT co.person_id, c.concept_name FROM condition_occurrence co",
    "JOIN descendants_of(1000) d ON d.concept_id = co.condition_concept_id",
    "JOIN concept c ON c.concept_id = d.concept_id ORDER BY 1"
  ))
  expect_equal(joined$person_id, c(1, 2, 4))
  expect_equal(oc_n(con, paste(
    "concept c WHERE c.concept_id IN (SELECT concept_id FROM ancestors_of(1002)) AND c.standard_concept = 'S'"
  )), 3)
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1000 + 1)"), c(1001, 1002)) # expression argument
  DBI::dbExecute(con, "CREATE TEMP TABLE probe (person_id INTEGER, ancestor_id INTEGER, descendant_id INTEGER)")
  DBI::dbExecute(con, "INSERT INTO probe VALUES (1, 1000, 1002), (2, 1001, 1003), (3, 1003, 1003)")
  # the caller's columns are literally called ancestor_id / descendant_id, like the macro parameters
  expect_equal(oc_q(con, paste(
    "SELECT person_id FROM probe WHERE descendant_id IN (SELECT concept_id FROM descendants_of(ancestor_id)) ORDER BY 1"
  ))$person_id, c(1, 3))
  expect_equal(oc_q(con, paste(
    "SELECT person_id FROM probe WHERE ancestor_id IN (SELECT concept_id FROM ancestors_of(descendant_id)) ORDER BY 1"
  ))$person_id, c(1, 3))
})

test_that("macro parameters cannot collide with the columns they filter", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  columns <- oc_q(con, "DESCRIBE concept_ancestor")$column_name
  expect_setequal(columns, c(
    "ancestor_concept_id", "descendant_concept_id", "min_levels_of_separation", "max_levels_of_separation"
  ))
  for (macro in c("descendants_of", "ancestors_of")) {
    params <- oc_q(con, sprintf("SELECT unnest(parameters) AS p FROM duckdb_functions() WHERE function_name = '%s'", macro))$p
    expect_gt(length(params), 0)
    expect_length(intersect(params, columns), 0)
  }
})

# What the callers' columns are called must not matter: these are the names a caller's table can plausibly
# hold, including the macros' own output column (concept_id) and the concept_ancestor columns.
test_that("ancestry macros bind the caller's column whatever it is called", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  values <- c(1000, 1001, 1002, 1003, 2000, 999999, NA)
  # size of descendants_of / ancestors_of per value; unknown and NULL ids give nothing
  counts <- list(descendants_of = c(4, 2, 1, 1, 1, 0, 0), ancestors_of = c(1, 2, 3, 2, 1, 0, 0))
  for (column in c("concept_id", "ancestor_concept_id", "descendant_concept_id", "ancestor_id", "descendant_id")) {
    DBI::dbExecute(con, "DROP TABLE IF EXISTS probe")
    DBI::dbExecute(con, sprintf('CREATE TEMP TABLE probe ("%s" INTEGER)', column))
    DBI::dbExecute(con, paste("INSERT INTO probe VALUES", paste0("(", ifelse(is.na(values), "NULL", values), ")", collapse = ", ")))
    for (qualified in c(FALSE, TRUE)) {
      arg <- if (qualified) sprintf('probe."%s"', column) else sprintf('"%s"', column)
      for (macro in names(counts)) {
        label <- paste(macro, column, if (qualified) "qualified" else "unqualified")
        # correlated scalar subquery: the argument is the caller's column, row by row
        rows <- oc_q(con, sprintf(
          'SELECT "%s" AS v, (SELECT COUNT(*) FROM %s(%s)) AS n FROM probe ORDER BY "%s" NULLS LAST',
          column, macro, arg, column
        ))
        expect_equal(rows$v, values, info = label)
        expect_equal(rows$n, counts[[macro]], info = label)
        # a lateral join over the same column returns the very same concepts
        expect_equal(
          oc_n(con, sprintf('probe, %s(probe."%s")', macro, column)), sum(counts[[macro]]),
          info = label
        )
        # and so does an IN subquery that is correlated on it: which rows reach concept 1001?
        reaching <- sum(vapply(
          values[!is.na(values)],
          function(v) 1001 %in% oc_ids(con, sprintf("SELECT concept_id FROM %s(%d)", macro, v)),
          logical(1)
        ))
        expect_equal(
          oc_n(con, sprintf("probe WHERE 1001 IN (SELECT concept_id FROM %s(%s))", macro, arg)), reaching,
          info = label
        )
      }
    }
  }
})

test_that("existing mapping macros resolve through the attached vocabulary", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(oc_q(con, "SELECT map_to_standard_concept_id('ICD10CM', 'E11.9') AS x")$x, 1001)
  expect_equal(oc_q(con, "SELECT map_to_standard_concept_id('ICD10CM', 'nope') AS x")$x, 0)
  expect_equal(oc_q(con, "SELECT source_concept_id('ICD10CM', 'E11.9') AS x")$x, 9001)
})

test_that("clamp_physiologic semantics", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_equal(
    oc_q(con, "SELECT clamp_physiologic(x, 0, 100) AS y FROM (VALUES (-5), (0), (50), (100), (150)) t(x)")$y,
    c(0, 0, 50, 100, 100) # bounds are inclusive
  )
  expect_true(is.na(oc_q(con, "SELECT clamp_physiologic(NULL, 0, 100) AS y")$y)) # NULL stays NULL
  expect_equal(
    oc_q(con, "SELECT clamp_physiologic(x, 0, 100) AS y FROM (VALUES (-5), (NULL), (150)) t(x)")$y,
    c(0, NA, 100)
  )
  expect_equal(unlist(oc_q(con, "SELECT clamp_physiologic(36.6::DOUBLE, 30, 45) AS a, clamp_physiologic(99.5::DOUBLE, 30, 45) AS b")),
               c(a = 36.6, b = 45))
  # a NULL bound leaves that side open
  expect_equal(
    unlist(oc_q(con, paste(
      "SELECT clamp_physiologic(-5, 0, NULL) AS a, clamp_physiologic(500, 0, NULL) AS b,",
      "clamp_physiologic(-5, NULL, 10) AS c, clamp_physiologic(50, NULL, 10) AS d, clamp_physiologic(7, NULL, NULL) AS e"
    ))),
    c(a = 0, b = 500, c = -5, d = 10, e = 7)
  )
  # bounds may come from columns, row by row
  expect_equal(
    oc_q(con, "SELECT clamp_physiologic(v, lo, hi) AS y FROM (VALUES (5, 0, 10), (5, 6, 10), (5, 0, 4)) t(v, lo, hi)")$y,
    c(5, 6, 4)
  )
})

test_that("clamp_physiologic rejects an inverted range", {
  s <- oc_site()
  con <- oc_connect(s$cdm, read_only = TRUE)$con
  expect_error(oc_q(con, "SELECT clamp_physiologic(5, 10, 0)"), "min_val \\(10\\) is greater than max_val \\(0\\)")
  # a NULL value does not hide a bad range
  expect_error(oc_q(con, "SELECT clamp_physiologic(NULL, 10, 0)"), "greater than max_val")
  expect_error(
    oc_q(con, "SELECT clamp_physiologic(v, lo, hi) FROM (VALUES (5, 0, 10), (5, 10, 0)) t(v, lo, hi)"),
    "greater than max_val"
  )
  # no rows, no evaluation, no error
  expect_equal(nrow(oc_q(con, "SELECT clamp_physiologic(v, 10, 0) FROM range(0) t(v)")), 0)
})

# ----------------------------------------------------------------------- relation to attach_central_vocabulary
test_that("omop_connect after attach_central_vocabulary does not conflict", {
  s <- oc_site()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm, read_only = TRUE)
  withr::defer(oc_close(con))
  utils::capture.output(attach_central_vocabulary(con, s$vocab, temporary = TRUE)) # ATTACH + temporary views
  views_before <- oc_temp_views(con)
  warns <- character()
  res <- withCallingHandlers(omop_connect(con), warning = function(w) {
    warns <<- c(warns, conditionMessage(w))
    invokeRestart("muffleWarning")
  })
  expect_identical(res, con) # same attachment recognised: no second ATTACH, no error
  expect_equal(warns, character())
  expect_equal(sum(names(oc_dbs(con)) == "central_vocab"), 1)
  expect_equal(oc_temp_views(con), views_before) # its views were reused, none duplicated or replaced
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1000)"), c(1000, 1001, 1002, 1003))
})

test_that("omop_connect reads a database with persistent central-vocabulary views", {
  # attach_central_vocabulary(temporary = FALSE) leaves persistent views over central_vocab in the file
  s <- oc_site()
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  utils::capture.output(attach_central_vocabulary(w, s$vocab, temporary = FALSE))
  oc_close(w)
  con <- oc_connect(s$cdm, read_only = TRUE)$con # the views only resolve once the vocabulary is attached again
  expect_equal(oc_n(con, "concept"), nrow(OC_CONCEPTS))
  expect_equal(oc_ids(con, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
  expect_equal(oc_temp_views(con), character()) # the persistent views already point at the vocabulary
})

# --------------------------------------------------------------------------------- no vocabulary found
test_that("no vocabulary found still returns a connection with macros and a clear message", {
  dir <- file.path(withr::local_tempdir(), "lonely")
  dir.create(dir)
  cdm <- file.path(dir, "cdm.duckdb")
  file.copy(oc_template, cdm)
  r <- oc_connect(cdm, read_only = TRUE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "No vocabulary available", fixed = TRUE)
  expect_match(r$warnings, "central_vocabulary.duckdb", fixed = TRUE)
  expect_match(r$warnings, "vocab_db", fixed = TRUE)
  expect_false("central_vocab" %in% names(oc_dbs(r$con)))
  # build_schema tables exist, so every macro loads
  expect_true(all(oc_shipped_macro_names() %in% oc_macros(r$con, "temp")))
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(500, 0, 300) AS x")$x, 300)
  expect_equal(oc_n(r$con, "person"), 4)
})

test_that("no warning when the database carries its own vocabulary", {
  s <- oc_site(vocab_name = NULL)
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  DBI::dbExecute(w, paste(
    "INSERT INTO concept VALUES (1001, 'T2DM', 'Condition', 'SNOMED', 'Disorder', 'S', 'T2DM',",
    "DATE '1970-01-01', DATE '2099-12-31', NULL)"
  ))
  oc_close(w)
  r <- oc_connect(s$cdm, read_only = TRUE)
  expect_equal(r$warnings, character())
  expect_false("central_vocab" %in% names(oc_dbs(r$con)))
  expect_equal(oc_q(r$con, "SELECT concept_name FROM concept")$concept_name, "T2DM")
})

test_that("a database without any vocabulary tables skips only the macros that need them", {
  s <- oc_site(vocab_name = NULL)
  bare <- file.path(s$dir, "bare.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = bare)
  DBI::dbExecute(w, "CREATE TABLE person (person_id INTEGER)")
  oc_close(w)
  r <- oc_connect(bare, read_only = TRUE)
  skipped <- c("map_to_standard_concept_id", "source_concept_id")
  expect_length(r$warnings, 1)
  for (name in skipped) expect_match(r$warnings, name, fixed = TRUE)
  expect_false(grepl("descendants_of", r$warnings, fixed = TRUE))
  expect_false(grepl("ancestors_of", r$warnings, fixed = TRUE))
  expect_true(all(setdiff(oc_shipped_macro_names(), skipped) %in% oc_macros(r$con, "temp")))
  expect_length(intersect(skipped, oc_macros(r$con, "temp")), 0)
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(-4, 0, 10) AS x")$x, 0)
  # the ancestry macros are created anyway and report the missing table when they are used
  for (macro in c("descendants_of", "ancestors_of")) {
    expect_error(oc_q(r$con, sprintf("SELECT * FROM %s(1)", macro)), "concept_ancestor")
  }
})

test_that("opting out of a vocabulary on a database without one does not warn", {
  s <- oc_site(vocab_name = NULL)
  bare <- file.path(s$dir, "bare.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = bare)
  DBI::dbExecute(w, "CREATE TABLE person (person_id INTEGER)")
  oc_close(w)
  for (target in list(":memory:", bare)) {
    r <- oc_connect(target, auto_attach_vocab = FALSE)
    expect_equal(r$warnings, character())
    expect_true("clamp_physiologic" %in% oc_macros(r$con, "temp")) # everything that needs no vocabulary loads
    expect_length(intersect(c("map_to_standard_concept_id", "source_concept_id"), oc_macros(r$con, "temp")), 0)
  }
})

test_that("opting out still warns about a vocabulary that is present but incomplete", {
  s <- oc_site()
  partial <- file.path(s$dir, "partial.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = partial)
  DBI::dbExecute(w, "CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR)") # no concept_ancestor
  oc_close(w)
  r <- oc_connect(partial, read_only = TRUE, auto_attach_vocab = FALSE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "map_to_standard_concept_id", fixed = TRUE)
  expect_false(grepl("No vocabulary available", r$warnings, fixed = TRUE)) # the user opted out of the search

  # persistent views over a vocabulary this connection cannot find: present but dangling, so still named
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  utils::capture.output(attach_central_vocabulary(w, s$vocab, temporary = FALSE))
  oc_close(w)
  lonely <- file.path(withr::local_tempdir(), "lonely", "cdm.duckdb")
  dir.create(dirname(lonely))
  file.copy(s$cdm, lonely)
  r <- oc_connect(lonely, read_only = TRUE, auto_attach_vocab = FALSE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "map_to_standard_concept_id", fixed = TRUE)
})

test_that("persistent vocabulary views that dangle give the no-vocabulary message", {
  # attach_central_vocabulary(temporary = FALSE) views point at a vocabulary this connection cannot find
  s <- oc_site()
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = s$cdm)
  utils::capture.output(attach_central_vocabulary(w, s$vocab, temporary = FALSE))
  oc_close(w)
  lonely <- file.path(withr::local_tempdir(), "lonely", "cdm.duckdb")
  dir.create(dirname(lonely))
  file.copy(s$cdm, lonely) # no sibling vocabulary here, so the views cannot resolve
  r <- oc_connect(lonely, read_only = TRUE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "No vocabulary available", fixed = TRUE)
  expect_match(r$warnings, "map_to_standard_concept_id", fixed = TRUE) # its table is a dangling view: skipped and named
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(500, 0, 300) AS x")$x, 300)
  # naming the vocabulary afterwards repairs the connection
  expect_identical(omop_connect(r$con, vocab_db = s$vocab), r$con)
  expect_equal(oc_ids(r$con, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
})

test_that("load_macros = FALSE creates no macros", {
  s <- oc_site()
  r <- oc_connect(s$cdm, read_only = TRUE, load_macros = FALSE)
  expect_equal(r$warnings, character())
  expect_equal(oc_macros(r$con, "temp"), character())
  expect_error(oc_q(r$con, "SELECT * FROM descendants_of(1000)"), "Catalog Error")
  expect_equal(oc_n(r$con, "concept"), nrow(OC_CONCEPTS)) # vocabulary still attached
})

test_that("a new path is created as an empty database", {
  path <- file.path(withr::local_tempdir(), "fresh.duckdb")
  r <- oc_connect(path)
  expect_match(r$warnings, "No vocabulary available", fixed = TRUE)
  expect_true(file.exists(path))
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(-4, 0, 10) AS x")$x, 0)
})

test_that("an in-memory database works", {
  s <- oc_site()
  r <- oc_connect(":memory:")
  expect_match(r$warnings, "in-memory database has no directory", fixed = TRUE)
  oc_close(r$con)
  # an explicit vocabulary works for an in-memory database (macros need no local tables then)
  r2 <- oc_connect(":memory:", vocab_db = s$vocab)
  expect_equal(r2$warnings, character())
  expect_equal(oc_ids(r2$con, "SELECT concept_id FROM descendants_of(1001)"), c(1001, 1002))
  expect_equal(oc_q(r2$con, "SELECT concept_name FROM concept WHERE concept_id = 2000")$concept_name, "Hypertensive disorder")
})

test_that("a vocabulary without concept_ancestor reports it when the ancestry macros are used", {
  s <- oc_site(vocab_name = NULL)
  thin <- file.path(s$dir, "thin_vocab.duckdb")
  oc_make_vocab(thin, tables = c("concept", "concept_relationship"))
  bare <- file.path(s$dir, "bare.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = bare)
  DBI::dbExecute(w, "CREATE TABLE person (person_id INTEGER)")
  oc_close(w)
  r <- oc_connect(bare, vocab_db = thin, read_only = TRUE)
  expect_equal(r$warnings, character()) # nothing the macros need at creation time is missing
  expect_true(all(c("map_to_standard_concept_id", "descendants_of", "ancestors_of") %in% oc_macros(r$con, "temp")))
  expect_equal(oc_q(r$con, "SELECT map_to_standard_concept_id('ICD10CM', 'E11.9') AS x")$x, 1001)
  for (macro in c("descendants_of", "ancestors_of")) {
    expect_error(oc_q(r$con, sprintf("SELECT * FROM %s(1000)", macro)), "concept_ancestor")
  }
})

oc_empty_database <- function(path) {
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = path)
  DBI::dbExecute(w, "CREATE TABLE unrelated (x INTEGER)")
  oc_close(w)
  invisible(path)
}

test_that("an explicit vocabulary without vocabulary tables warns", {
  s <- oc_site(vocab_name = NULL)
  empty <- oc_empty_database(file.path(s$dir, "not_a_vocabulary.duckdb"))
  r <- oc_connect(s$cdm, vocab_db = empty, read_only = TRUE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "contains none of the vocabulary tables", fixed = TRUE)
  expect_match(r$warnings, "not_a_vocabulary.duckdb", fixed = TRUE)
  expect_match(r$warnings, "vocab_db", fixed = TRUE)
  # something was attached, it just holds nothing
  expect_false(grepl("No vocabulary available", r$warnings, fixed = TRUE))
  expect_true("central_vocab" %in% names(oc_dbs(r$con))) # the connection is still returned and usable
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(500, 0, 300) AS x")$x, 300)
  expect_equal(oc_n(r$con, "person"), 4)
})

test_that("a discovered sibling without vocabulary tables warns too", {
  s <- oc_site(vocab_name = NULL)
  oc_empty_database(file.path(s$dir, "vocab.duckdb"))
  r <- oc_connect(s$cdm, read_only = TRUE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "contains none of the vocabulary tables", fixed = TRUE)
})

test_that("a real vocabulary gives no empty-vocabulary warning", {
  s <- oc_site()
  r <- oc_connect(s$cdm, vocab_db = s$vocab, read_only = TRUE)
  expect_false(any(grepl("none of the vocabulary tables", r$warnings, fixed = TRUE)))
  oc_close(r$con) # a different vocabulary cannot share the open database instance
  # a single vocabulary table is enough for it not to be an empty vocabulary
  thin <- file.path(s$dir, "thin.duckdb")
  oc_make_vocab(thin, tables = "concept")
  r <- oc_connect(s$cdm, vocab_db = thin, read_only = TRUE)
  expect_false(any(grepl("none of the vocabulary tables", r$warnings, fixed = TRUE)))
})

test_that("a stub vocabulary missing columns skips only the macros that read them", {
  # a hand-made vocabulary without the usual columns still connects; the gap is named, not raised
  s <- oc_site(vocab_name = NULL)
  stub <- file.path(s$dir, "stub_vocab.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = stub)
  DBI::dbExecute(w, "CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR)")
  DBI::dbExecute(w, "INSERT INTO concept VALUES (1, 'stub')")
  DBI::dbExecute(w, paste(
    "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER,",
    "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)"
  ))
  DBI::dbExecute(w, "INSERT INTO concept_ancestor VALUES (1, 1, 0, 0)")
  oc_close(w)
  bare <- file.path(s$dir, "bare.duckdb")
  w <- DBI::dbConnect(duckdb::duckdb(), dbdir = bare)
  DBI::dbExecute(w, "CREATE TABLE person (person_id INTEGER)")
  oc_close(w)
  r <- oc_connect(bare, vocab_db = stub, read_only = TRUE)
  expect_length(r$warnings, 1)
  expect_match(r$warnings, "tables or columns", fixed = TRUE)
  expect_match(r$warnings, "source_concept_id", fixed = TRUE)
  expect_match(r$warnings, "map_to_standard_concept_id", fixed = TRUE)
  expect_false(grepl("descendants_of", r$warnings, fixed = TRUE))
  expect_equal(oc_ids(r$con, "SELECT concept_id FROM descendants_of(1)"), 1)
  expect_equal(oc_q(r$con, "SELECT clamp_physiologic(-4, 0, 10) AS x")$x, 0)
})

# --------------------------------------------------------------------------- optional real-vocabulary check
test_that("descendants_of works on a real vocabulary (OMOP_VOCAB_DB)", {
  vocab_file <- Sys.getenv("OMOP_VOCAB_DB")
  skip_if(!nzchar(vocab_file) || !file.exists(vocab_file), "set OMOP_VOCAB_DB to an Athena vocabulary DuckDB file")
  stat <- function() c(file.size(vocab_file), as.numeric(file.mtime(vocab_file)))
  before <- stat()
  con <- oc_connect(":memory:", vocab_db = vocab_file)$con # attached READ_ONLY; nothing is written to it
  # 201826 = SNOMED 'Type 2 diabetes mellitus' (the RFC text's 316866 is 'Hypertensive disorder')
  expect_equal(oc_q(con, "SELECT concept_name FROM concept WHERE concept_id = 201826")$concept_name, "Type 2 diabetes mellitus")
  descendants <- oc_ids(con, "SELECT concept_id FROM descendants_of(201826)")
  expect_true(201826 %in% descendants && length(descendants) > 1) # self row plus more specific concepts
  expect_gte(oc_n(con, "concept c JOIN descendants_of(201826) d USING (concept_id) WHERE c.standard_concept = 'S'"), 1)
  ancestors <- oc_ids(con, "SELECT concept_id FROM ancestors_of(201826)")
  expect_true(201826 %in% ancestors && 201820 %in% ancestors) # 201820 = 'Diabetes mellitus'
  oc_close(con)
  expect_equal(stat(), before)
})
