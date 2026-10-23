#' Export targets generated from the single CDM DDL source
#'
#' Targets supported by [export_cdm()] beyond Parquet/PostgreSQL/CSV: `"oracle"`, `"sqlserver"`, `"snowflake"`,
#' `"bigquery"`. DDL, data files and loader scripts are all derived from the package's CDM DDL file, so they cannot
#' drift apart. Mirror of `omop_etl.export_targets`.
#'
#' @format A character vector.
#' @export
EXTRA_EXPORT_TARGETS <- c("oracle", "sqlserver", "snowflake", "bigquery")

.reserved <- list(
  oracle = c("ACCESS", "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUDIT", "BETWEEN", "BY", "CHAR", "CHECK", "CLUSTER",
             "COLUMN", "COMMENT", "COMPRESS", "CONNECT", "CREATE", "CURRENT", "DATE", "DECIMAL", "DEFAULT", "DELETE", "DESC",
             "DISTINCT", "DROP", "ELSE", "EXCLUSIVE", "EXISTS", "FILE", "FLOAT", "FOR", "FROM", "GRANT", "GROUP", "HAVING",
             "IDENTIFIED", "IMMEDIATE", "IN", "INCREMENT", "INDEX", "INITIAL", "INSERT", "INTEGER", "INTERSECT", "INTO", "IS",
             "LEVEL", "LIKE", "LOCK", "LONG", "MAXEXTENTS", "MINUS", "MLSLABEL", "MODE", "MODIFY", "NOAUDIT", "NOCOMPRESS",
             "NOT", "NOWAIT", "NULL", "NUMBER", "OF", "OFFLINE", "ON", "ONLINE", "OPTION", "OR", "ORDER", "PCTFREE", "PRIOR",
             "PUBLIC", "RAW", "RENAME", "RESOURCE", "REVOKE", "ROW", "ROWID", "ROWNUM", "ROWS", "SELECT", "SESSION", "SET",
             "SHARE", "SIZE", "SMALLINT", "START", "SUCCESSFUL", "SYNONYM", "SYSDATE", "TABLE", "THEN", "TO", "TRIGGER", "UID",
             "UNION", "UNIQUE", "UPDATE", "USER", "VALIDATE", "VALUES", "VARCHAR", "VARCHAR2", "VIEW", "WHENEVER", "WHERE", "WITH"),
  sqlserver = c("ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "BACKUP", "BEGIN", "BETWEEN", "BY", "CASE", "CHECK", "COLUMN",
                "COMMIT", "CONSTRAINT", "CREATE", "CURRENT", "DATABASE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE",
                "END", "EXEC", "EXISTS", "FILE", "FOR", "FOREIGN", "FROM", "FULL", "FUNCTION", "GROUP", "HAVING", "IN", "INDEX",
                "INNER", "INSERT", "INTO", "IS", "JOIN", "KEY", "LEFT", "LIKE", "NOT", "NULL", "OF", "ON", "OPEN", "OPTION", "OR",
                "ORDER", "OUTER", "PRIMARY", "PRINT", "PUBLIC", "REFERENCES", "RIGHT", "SCHEMA", "SELECT", "SET", "TABLE", "TO",
                "TOP", "TRAN", "UNION", "UNIQUE", "UPDATE", "USER", "VALUES", "VIEW", "WHEN", "WHERE", "WITH"),
  snowflake = c("ACCOUNT", "ALL", "ALTER", "AND", "ANY", "AS", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN", "CONNECT",
                "CONSTRAINT", "CREATE", "CURRENT", "DELETE", "DISTINCT", "DROP", "ELSE", "EXISTS", "FALSE", "FOLLOWING", "FOR",
                "FROM", "FULL", "GRANT", "GROUP", "HAVING", "ILIKE", "IN", "INCREMENT", "INNER", "INSERT", "INTERSECT", "INTO",
                "IS", "ISSUE", "JOIN", "LATERAL", "LEFT", "LIKE", "LOCALTIME", "LOCALTIMESTAMP", "MINUS", "NATURAL", "NOT", "NULL",
                "OF", "ON", "OR", "ORDER", "ORGANIZATION", "QUALIFY", "REGEXP", "REVOKE", "RIGHT", "RLIKE", "ROW", "ROWS", "SAMPLE",
                "SCHEMA", "SELECT", "SET", "SOME", "START", "TABLE", "TABLESAMPLE", "THEN", "TO", "TRIGGER", "TRUE", "TRY_CAST",
                "UNION", "UNIQUE", "UPDATE", "USING", "VALUES", "VIEW", "WHEN", "WHENEVER", "WHERE", "WITH"),
  bigquery = c("ALL", "AND", "ANY", "ARRAY", "AS", "ASC", "AT", "BETWEEN", "BY", "CASE", "CAST", "COLLATE", "CONTAINS", "CREATE",
               "CROSS", "CUBE", "CURRENT", "DEFAULT", "DEFINE", "DESC", "DISTINCT", "ELSE", "END", "ENUM", "ESCAPE", "EXCEPT",
               "EXCLUDE", "EXISTS", "EXTRACT", "FALSE", "FETCH", "FOLLOWING", "FOR", "FROM", "FULL", "GROUP", "GROUPING", "GROUPS",
               "HASH", "HAVING", "IF", "IGNORE", "IN", "INNER", "INTERSECT", "INTERVAL", "INTO", "IS", "JOIN", "LATERAL", "LEFT",
               "LIKE", "LIMIT", "LOOKUP", "MERGE", "NATURAL", "NEW", "NO", "NOT", "NULL", "NULLS", "OF", "ON", "OR", "ORDER",
               "OUTER", "OVER", "PARTITION", "PRECEDING", "PROTO", "RANGE", "RECURSIVE", "RESPECT", "RIGHT", "ROLLUP", "ROWS",
               "SELECT", "SET", "SOME", "STRUCT", "TABLESAMPLE", "THEN", "TO", "TREAT", "TRUE", "UNBOUNDED", "UNION", "UNNEST",
               "USING", "WHEN", "WHERE", "WINDOW", "WITH", "WITHIN")
)

#' Parse the CDM DDL file into a neutral schema model
#'
#' @param ddl_path Path to the DDL file (default: the one shipped with the package).
#' @return A named list `table -> data.frame(name, base, length, nullable)`, with `base` one of `integer`, `varchar`,
#'   `text`, `date`, `timestamp`, `numeric`.
#' @export
load_cdm_schema <- function(ddl_path = NULL) {
  if (is.null(ddl_path)) {
    ddl_path <- system.file("extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql", package = "omopduckdb", mustWork = TRUE)
  }
  lines <- readLines(ddl_path, warn = FALSE)
  schema <- list(); current <- NULL
  for (line in lines) {
    if (grepl("^\\s*--", line)) next
    m <- regmatches(line, regexec("(?i)CREATE TABLE\\s+@cdmDatabaseSchema\\.([a-z_0-9]+)\\s*\\(", line, perl = TRUE))[[1]]
    if (length(m)) { current <- tolower(m[2]); schema[[current]] <- list(); next }
    if (is.null(current)) next
    c_ <- regmatches(line, regexec("(?i)^\\s*\"?([a-z_0-9]+)\"?\\s+([A-Za-z]+)(?:\\(\\s*(\\d+)\\s*\\))?\\s+(NOT NULL|NULL)\\s*,?\\s*\\)?\\s*;?\\s*$",
                                   line, perl = TRUE))[[1]]
    if (!length(c_)) {
      if (nzchar(trimws(line))) stop("Unparsed DDL line: ", trimws(line), call. = FALSE)
      next
    }
    base <- c(integer = "integer", bigint = "bigint", varchar = "varchar", text = "text", date = "date", timestamp = "timestamp",
              numeric = "numeric")[tolower(c_[3])]
    if (is.na(base)) stop("Unknown type '", c_[3], "' in the DDL.", call. = FALSE)
    schema[[current]][[length(schema[[current]]) + 1L]] <- data.frame(
      name = tolower(c_[2]), base = unname(base), length = if (nzchar(c_[4])) as.integer(c_[4]) else NA_integer_,
      nullable = toupper(c_[5]) == "NULL", stringsAsFactors = FALSE)
    if (grepl(";\\s*$", line)) current <- NULL
  }
  lapply(schema, function(cols) do.call(rbind, cols))
}

.col_type <- function(dialect, base, len) {
  switch(dialect,
    oracle = switch(base, bigint = "NUMBER(19)", integer = "NUMBER(10)", date = "DATE", timestamp = "TIMESTAMP(0)", numeric = "NUMBER",
                    text = "CLOB", sprintf("VARCHAR2(%d CHAR)", len)),
    sqlserver = switch(base, bigint = "BIGINT", integer = "INT", date = "DATE", timestamp = "DATETIME2(0)", numeric = "FLOAT",
                       text = "VARCHAR(MAX)", sprintf("VARCHAR(%d)", len)),
    snowflake = switch(base, bigint = "NUMBER(19,0)", integer = "NUMBER(10,0)", date = "DATE", timestamp = "TIMESTAMP_NTZ(0)", numeric = "FLOAT",
                       text = "VARCHAR", sprintf("VARCHAR(%d)", len)),
    bigquery = switch(base, bigint = "INT64", integer = "INT64", date = "DATE", timestamp = "DATETIME", numeric = "FLOAT64", "STRING"),
    stop("Unsupported dialect '", dialect, "'; use one of ", paste(EXTRA_EXPORT_TARGETS, collapse = ", "), ".", call. = FALSE))
}

.ident <- function(dialect, name, oracle_max_identifier = 30L) {
  limit <- switch(dialect, oracle = oracle_max_identifier, sqlserver = 128L, snowflake = 255L, bigquery = 300L)
  if (nchar(name) > limit) {
    stop(sprintf("%s: identifier '%s' is %d characters; the limit is %d%s", dialect, name, nchar(name), limit,
                 if (dialect == "oracle") " (Oracle < 12.2; pass oracle_max_identifier = 128 for 12.2+)" else "."), call. = FALSE)
  }
  n <- if (dialect %in% c("oracle", "snowflake")) toupper(name) else name
  if (toupper(n) %in% .reserved[[dialect]]) {
    return(switch(dialect, oracle = , snowflake = paste0('"', n, '"'), sqlserver = paste0("[", n, "]"), bigquery = paste0("`", n, "`")))
  }
  n
}

#' CREATE TABLE statements for a target database
#'
#' @param dialect One of [EXTRA_EXPORT_TARGETS].
#' @param tables Table names (default all 39 CDM tables).
#' @param schema_name Optional schema / dataset qualifier.
#' @param schema Schema model from [load_cdm_schema()].
#' @param oracle_max_identifier 30 (Oracle < 12.2, the safe default) or 128.
#' @return One string. Identifiers over the limit raise an error; reserved words are quoted.
#' @export
generate_ddl <- function(dialect, tables = NULL, schema_name = NULL, schema = NULL, oracle_max_identifier = 30L) {
  model <- if (is.null(schema)) load_cdm_schema() else schema
  tabs <- tolower(if (is.null(tables)) names(model) else tables)
  unknown <- setdiff(tabs, names(model))
  if (length(unknown)) stop("Tables not in the CDM v5.4 DDL: ", paste(unknown, collapse = ", "), call. = FALSE)
  out <- vapply(tabs, function(t) {
    q <- if (dialect == "bigquery" && !is.null(schema_name)) sprintf("`%s.%s`", schema_name, t) else
      paste0(if (is.null(schema_name)) "" else paste0(schema_name, "."), .ident(dialect, t, oracle_max_identifier))
    cols <- model[[t]]
    lines <- vapply(seq_len(nrow(cols)), function(i) {
      sprintf("    %s %s%s", .ident(dialect, cols$name[i], oracle_max_identifier), .col_type(dialect, cols$base[i], cols$length[i]),
              if (cols$nullable[i]) "" else " NOT NULL")
    }, character(1))
    sprintf("CREATE TABLE %s (\n%s\n);", q, paste(lines, collapse = ",\n"))
  }, character(1))
  paste0(paste(out, collapse = "\n\n"), "\n")
}

.csv_select <- function(table, cols) {
  ex <- ifelse(cols$base == "timestamp", sprintf("strftime(\"%1$s\", '%%Y-%%m-%%d %%H:%%M:%%S') AS \"%1$s\"", cols$name),
               sprintf("\"%s\"", cols$name))
  sprintf("SELECT %s FROM %s", paste(ex, collapse = ", "), table)
}

.oracle_ctl <- function(table, cols, oracle_max_identifier = 30L) {
  fields <- vapply(seq_len(nrow(cols)), function(i) {
    nm <- .ident("oracle", cols$name[i], oracle_max_identifier)
    switch(cols$base[i],
      date = sprintf('    %s DATE "YYYY-MM-DD"', nm),
      timestamp = sprintf('    %s TIMESTAMP "YYYY-MM-DD HH24:MI:SS"', nm),
      text = sprintf("    %s CHAR(1000000)", nm),
      varchar = sprintf("    %s CHAR(%d)", nm, min(as.integer(cols$length[i]) * 4L, 4000L)),
      sprintf("    %s CHAR(64)", nm))
  }, character(1))
  paste0("-- NLS-independent: every DATE/TIMESTAMP field carries an explicit mask.\nOPTIONS (SKIP=1, DIRECT=TRUE)\nLOAD DATA\n",
         "CHARACTERSET UTF8\nINFILE '", table, ".csv'\nAPPEND INTO TABLE ", .ident("oracle", table, oracle_max_identifier),
         "\nFIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\nTRAILING NULLCOLS\n(\n", paste(fields, collapse = ",\n"), "\n)\n")
}

.sqlserver_load <- function(tables, data_dir = ".", schema_name = "dbo") {
  body <- vapply(tables, function(t) sprintf(
    paste0("BULK INSERT %s.%s\nFROM '%s/%s.csv'\nWITH (FORMAT = 'CSV', FIRSTROW = 2, FIELDQUOTE = '\"', FIELDTERMINATOR = ',', ",
           "ROWTERMINATOR = '0x0a', CODEPAGE = '65001', KEEPNULLS, TABLOCK);\nGO"),
    schema_name, .ident("sqlserver", t), sub("[/\\\\]+$", "", data_dir), t), character(1))
  paste0("-- Run after omop_sqlserver_ddl.sql. Requires SQL Server 2017+ (FORMAT = 'CSV'); paths are as seen by the server.\n\n",
         paste(body, collapse = "\n\n"), "\n")
}

.snowflake_load <- function(tables, data_dir = ".", stage = "OMOP_STAGE") {
  b <- sub("[/\\\\]+$", "", data_dir)
  paste0(paste(c("-- Run in SnowSQL (PUT is a client command). Run omop_snowflake_ddl.sql first.",
                 sprintf("CREATE STAGE IF NOT EXISTS %s FILE_FORMAT = (TYPE = PARQUET);", stage),
                 sprintf("PUT file://%s/%s.parquet @%s AUTO_COMPRESS = FALSE OVERWRITE = TRUE;", b, tables, stage),
                 sprintf("COPY INTO %s FROM @%s/%s.parquet\n  FILE_FORMAT = (TYPE = PARQUET) MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE ON_ERROR = ABORT_STATEMENT;",
                         vapply(tables, .ident, character(1), dialect = "snowflake"), stage, tables)), collapse = "\n"), "\n")
}

.bigquery_load <- function(tables, data_dir = ".", dataset = "omop") {
  b <- sub("[/\\\\]+$", "", data_dir)
  paste0(paste(c("#!/usr/bin/env bash", "set -euo pipefail",
                 sprintf("# Run omop_bigquery_ddl.sql first (bq query --use_legacy_sql=false < omop_bigquery_ddl.sql); dataset: %s", dataset),
                 sprintf("bq load --source_format=PARQUET --replace %s.%s %s/%s.parquet", dataset, tables, b, tables)),
               collapse = "\n"), "\n")
}

#' Static lint of generated DDL
#'
#' Checks that every statement is a `CREATE TABLE`, parentheses balance, identifiers are within the limit, reserved
#' words are quoted and every column type is one the generator emits.
#'
#' @param sql DDL text.
#' @param dialect One of [EXTRA_EXPORT_TARGETS].
#' @param oracle_max_identifier See [generate_ddl()].
#' @return A character vector of problems (empty when clean).
#' @export
lint_ddl <- function(sql, dialect, oracle_max_identifier = 30L) {
  limit <- switch(dialect, oracle = oracle_max_identifier, sqlserver = 128L, snowflake = 255L, bigquery = 300L)
  allowed <- switch(dialect,
    oracle = "^(NUMBER(\\(\\d+(,\\d+)?\\))?|DATE|TIMESTAMP\\(0\\)|CLOB|VARCHAR2\\(\\d+ CHAR\\))$",
    sqlserver = "^(INT|BIGINT|DATE|DATETIME2\\(0\\)|FLOAT|VARCHAR\\((MAX|\\d+)\\))$",
    snowflake = "^(NUMBER\\(\\d+,0\\)|DATE|TIMESTAMP_NTZ\\(0\\)|FLOAT|VARCHAR(\\(\\d+\\))?)$",
    bigquery = "^(INT64|DATE|DATETIME|FLOAT64|STRING)$")
  stmts <- trimws(Filter(function(s) nzchar(trimws(s)), strsplit(sql, ";", fixed = TRUE)[[1]]))
  if (!length(stmts)) return("no statements")
  problems <- character()
  for (s in stmts) {
    head <- regmatches(s, regexec("CREATE TABLE\\s+(\\S+)\\s*\\(", s))[[1]]
    if (!length(head)) { problems <- c(problems, paste0("not a CREATE TABLE: ", substr(s, 1, 40))); next }
    for (line in strsplit(s, "\n", fixed = TRUE)[[1]][-1]) {
      line <- sub(",$", "", trimws(line))
      if (!nzchar(line) || line == ")") next
      m <- regmatches(line, regexec("^(\"[^\"]+\"|\\[[^]]+\\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\\s+(.+?)(\\s+NOT NULL)?$", line))[[1]]
      if (!length(m)) { problems <- c(problems, sprintf("%s: cannot parse column line '%s'", head[2], line)); next }
      id <- gsub("[\"`\\[\\]]", "", m[2])
      if (nchar(id) > limit) problems <- c(problems, sprintf("%s.%s: identifier longer than %d", head[2], id, limit))
      if (toupper(id) %in% .reserved[[dialect]] && m[2] == id) problems <- c(problems, sprintf("%s.%s: unquoted reserved word", head[2], id))
      if (!grepl(allowed, trimws(m[3]))) problems <- c(problems, sprintf("%s.%s: unexpected type '%s'", head[2], id, m[3]))
    }
  }
  problems
}
