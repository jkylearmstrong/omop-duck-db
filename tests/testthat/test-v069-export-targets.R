test_that("schema model has 39 tables and matches the DuckDB schema", {
  model <- load_cdm_schema()
  expect_length(model, 39)
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db, read_only = TRUE); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  for (t in names(model)) {
    live <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", t))$column_name)
    expect_equal(model[[t]]$name, live, info = t)
  }
})

test_that("generated DDL lints clean for every dialect", {
  for (d in EXTRA_EXPORT_TARGETS) {
    sql <- generate_ddl(d)
    expect_equal(lengths(regmatches(sql, gregexpr("CREATE TABLE", sql))), 39)
    expect_equal(lint_ddl(sql, d), character(), info = d)
  }
  expect_true(any(grepl("unquoted reserved", lint_ddl("CREATE TABLE T (\n    DATE DATE\n);", "oracle"))))
  expect_true(any(grepl("unexpected type", lint_ddl("CREATE TABLE T (\n    A TEXT\n);", "sqlserver"))))
})

test_that("Oracle identifier limit and reserved words", {
  long <- list(t = data.frame(name = "a_very_long_column_name_that_exceeds_thirty", base = "integer", length = NA_integer_, nullable = FALSE))
  expect_error(generate_ddl("oracle", schema = long), "30")
  expect_match(generate_ddl("oracle", schema = long, oracle_max_identifier = 128), "NUMBER\\(10\\)")
  odd <- list(t = data.frame(name = c("date", "comment", "level"), base = c("date", "varchar", "integer"),
                             length = c(NA, 10L, NA), nullable = TRUE))
  ddl <- generate_ddl("oracle", schema = odd)
  expect_match(ddl, '"DATE" DATE', fixed = TRUE); expect_match(ddl, '"COMMENT" VARCHAR2(10 CHAR)', fixed = TRUE)
  expect_match(ddl, '"LEVEL" NUMBER(10)', fixed = TRUE)
})

test_that("export_cdm writes files for each new target", {
  db <- tempfile(fileext = ".duckdb"); build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), db); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, birth_datetime, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1970, TIMESTAMP '1970-03-04 05:06:07.891', 8527, 38003564, 'a,b \"quoted\"')")
  DBI::dbExecute(con, "INSERT INTO observation_period VALUES (1, 1, DATE '2020-01-01', DATE '2020-12-31', 32817)")
  want <- list(oracle = c("person.csv", "person.ctl", "omop_oracle_ddl.sql", "load_oracle.sh"),
               sqlserver = c("person.csv", "omop_sqlserver_ddl.sql", "load_sqlserver.sql"),
               snowflake = c("person.parquet", "omop_snowflake_ddl.sql", "load_snowflake.sql"),
               bigquery = c("person.parquet", "omop_bigquery_ddl.sql", "load_bigquery.sh"))
  for (tgt in names(want)) {
    out <- tempfile(tgt)
    res <- export_cdm(con, tgt, out, tables = c("person", "observation_period"))
    for (f in want[[tgt]]) expect_true(file.exists(file.path(out, f)), info = paste(tgt, f))
    ddl <- paste(readLines(file.path(out, sprintf("omop_%s_ddl.sql", tgt))), collapse = "\n")
    expect_equal(lint_ddl(ddl, tgt), character(), info = tgt)
    expect_setequal(names(res$exported_tables), c("person", "observation_period"))
  }
  ora <- tempfile("ora"); export_cdm(con, "oracle", ora, tables = c("person", "observation_period"))
  ctl <- paste(readLines(file.path(ora, "person.ctl")), collapse = "\n")
  expect_match(ctl, "SKIP=1", fixed = TRUE); expect_match(ctl, 'BIRTH_DATETIME TIMESTAMP "YYYY-MM-DD HH24:MI:SS"', fixed = TRUE)
  dd <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(dd, shutdown = TRUE), add = TRUE)
  row <- DBI::dbGetQuery(dd, sprintf("SELECT birth_datetime, person_source_value FROM read_csv_auto('%s', all_varchar=true)",
                                     gsub("\\\\", "/", file.path(ora, "person.csv"))))
  expect_equal(row$birth_datetime, "1970-03-04 05:06:07")
  expect_equal(row$person_source_value, 'a,b "quoted"')
  expect_error(export_cdm(con, "mysql", tempfile()), "Unsupported")
})
