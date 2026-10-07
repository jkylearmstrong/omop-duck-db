#' Load or refresh the OHDSI Athena vocabulary into the OMOP CDM database
#'
#' Each table is truncated and reloaded inside its own transaction, with a
#' row-count sanity check before commit -- so a bad/partial download can only
#' fail that one table (rolled back, left untouched), not half-clobber the
#' rest of the vocabulary.
#'
#' Supports CPT4 sanitization: Athena downloads without a UMLS license
#' output `CONCEPT_CPT4.csv` (or CPT4 concept entries) with NULL concept_names,
#' which violates the NOT NULL constraint on `concept.concept_name`.
#' When `sanitize_cpt4 = TRUE`, CPT4 concept names are automatically
#' sanitized to `'CPT4 ' || concept_code`.
#'
#' @param vocab_dir Path to an extracted Athena vocabulary download (a
#'   directory of tab-delimited `*.csv` files).
#' @param db_path Path to the DuckDB database file (schema must already exist,
#'   see [build_schema()]).
#' @param sanitize_cpt4 Automatically sanitize NULL concept_names for CPT4 concepts.
#' @param sanitize_all_null_names Automatically sanitize any NULL concept_names.
#' @return `TRUE` if every matched table loaded successfully, `FALSE` if any
#'   table failed and was rolled back.
#' @export
load_vocabulary <- function(vocab_dir, db_path = "omop_cdm.duckdb", sanitize_cpt4 = TRUE, sanitize_all_null_names = FALSE) {
  if (!dir.exists(vocab_dir)) {
    stop("Vocabulary directory not found: ", vocab_dir)
  }

  vocab_files <- list.files(path = vocab_dir, full.names = TRUE, recursive = TRUE, pattern = "\\.csv$")
  if (length(vocab_files) == 0) {
    stop("No CSV files found under: ", vocab_dir)
  }
  names(vocab_files) <- tolower(stringr::str_remove_all(basename(vocab_files), "(?i)\\.csv$"))

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    try(DBI::dbDisconnect(con, shutdown = TRUE), silent = TRUE)
    gc()
  })
  existing_tables <- tolower(DBI::dbListTables(con))

  all_ok <- TRUE
  for (table_name in names(vocab_files)) {
    if (table_name == "concept_cpt4") next # handled separately below
    if (!(table_name %in% existing_tables)) {
      cat("Skipping", table_name, "- no matching table in schema (run build_schema() first)\n")
      next
    }
    file_path <- vocab_files[[table_name]]
    cat("Reloading", toupper(table_name), "from", basename(file_path), "...\n")

    DBI::dbExecute(con, "BEGIN TRANSACTION")
    ok <- tryCatch(
      {
        DBI::dbExecute(con, paste0("DELETE FROM ", table_name))

        if (table_name == "concept") {
          name_expr <- if (isTRUE(sanitize_all_null_names)) {
            "COALESCE(concept_name, 'CPT4 ' || concept_code)"
          } else if (isTRUE(sanitize_cpt4)) {
            "COALESCE(concept_name, CASE WHEN vocabulary_id = 'CPT4' THEN 'CPT4 ' || concept_code ELSE NULL END)"
          } else {
            "concept_name"
          }

          DBI::dbExecute(con, sprintf("
            CREATE OR REPLACE TEMPORARY VIEW _temp_concept_raw AS
            SELECT * FROM read_csv('%s', delim = '\t', header = true, all_varchar = true);
          ", file_path))

          DBI::dbExecute(con, sprintf("
            INSERT INTO concept
            SELECT
                TRY_CAST(concept_id AS INTEGER),
                %s AS concept_name,
                domain_id,
                vocabulary_id,
                concept_class_id,
                standard_concept,
                concept_code,
                COALESCE(TRY_STRPTIME(valid_start_date, '%%Y%%m%%d')::DATE, TRY_CAST(valid_start_date AS DATE)),
                COALESCE(TRY_STRPTIME(valid_end_date, '%%Y%%m%%d')::DATE, TRY_CAST(valid_end_date AS DATE)),
                invalid_reason
            FROM _temp_concept_raw;
          ", name_expr))

          DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_concept_raw;")
        } else {
          DBI::dbExecute(con, paste0(
            "COPY ", table_name, " FROM '", file_path, "' (DELIMITER '\t', HEADER, DATEFORMAT '%Y%m%d');"
          ))
        }

        row_count <- DBI::dbGetQuery(con, paste0("SELECT COUNT(*) AS n FROM ", table_name))$n
        if (row_count == 0) stop("table loaded with zero rows")
        DBI::dbExecute(con, "COMMIT")
        cat("  ->", row_count, "rows (committed)\n")
        TRUE
      },
      error = function(e) {
        DBI::dbExecute(con, "ROLLBACK")
        cat("  Error loading", table_name, ":", conditionMessage(e), "- rolled back, table unchanged\n")
        FALSE
      }
    )
    all_ok <- all_ok && ok
  }

  # Ingest separate CONCEPT_CPT4.csv if present
  if ("concept_cpt4" %in% names(vocab_files) && "concept" %in% existing_tables) {
    cpt4_path <- vocab_files[["concept_cpt4"]]
    cat("Ingesting separate CPT4 concepts from", basename(cpt4_path), "...\n")
    tryCatch({
      DBI::dbExecute(con, sprintf("
        CREATE OR REPLACE TEMPORARY VIEW _temp_cpt4_raw AS
        SELECT * FROM read_csv('%s', delim = '\t', header = true, all_varchar = true);
      ", cpt4_path))
      DBI::dbExecute(con, "
        INSERT OR REPLACE INTO concept
        SELECT
            TRY_CAST(concept_id AS INTEGER),
            COALESCE(concept_name, 'CPT4 ' || concept_code) AS concept_name,
            domain_id,
            vocabulary_id,
            concept_class_id,
            standard_concept,
            concept_code,
            COALESCE(TRY_STRPTIME(valid_start_date, '%Y%m%d')::DATE, TRY_CAST(valid_start_date AS DATE)),
            COALESCE(TRY_STRPTIME(valid_end_date, '%Y%m%d')::DATE, TRY_CAST(valid_end_date AS DATE)),
            invalid_reason
        FROM _temp_cpt4_raw;
      ")
      DBI::dbExecute(con, "DROP VIEW IF EXISTS _temp_cpt4_raw;")
      cat("  -> CPT4 concepts sanitized and merged\n")
    }, error = function(e) {
      cat("  Warning: failed to load CONCEPT_CPT4.csv:", conditionMessage(e), "\n")
    })
  }

  invisible(all_ok)
}

#' Query vocabulary version and summary metrics from DuckDB OMOP CDM
#' @param con Active DuckDB connection
#' @return List with vocabulary_version, total_concepts, total_relationships, and counts by vocabulary.
#' @export
check_vocabulary_version <- function(con) {
  version_df <- tryCatch(
    DBI::dbGetQuery(con, "SELECT vocabulary_version FROM vocabulary WHERE vocabulary_id = 'None' LIMIT 1"),
    error = function(e) data.frame(vocabulary_version = "Unknown")
  )
  version <- if (nrow(version_df) > 0) version_df$vocabulary_version[[1]] else "Unknown"

  counts <- DBI::dbGetQuery(con, "
    SELECT vocabulary_id, COUNT(*) AS count 
    FROM concept 
    GROUP BY vocabulary_id 
    ORDER BY count DESC;
  ")
  total_concepts <- sum(counts$count)
  total_rel <- tryCatch(
    DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM concept_relationship")$n,
    error = function(e) 0
  )

  unmapped_discharge <- tryCatch(
    {
      tables <- tolower(DBI::dbListTables(con))
      if ("visit_occurrence" %in% tables) {
        DBI::dbGetQuery(con, "
          SELECT discharged_to_source_value, COUNT(*) AS count
          FROM visit_occurrence
          WHERE discharged_to_concept_id = 0
            AND discharged_to_source_value IS NOT NULL
            AND TRIM(discharged_to_source_value) != ''
          GROUP BY discharged_to_source_value
          ORDER BY count DESC
          LIMIT 10;
        ")
      } else {
        data.frame(discharged_to_source_value = character(), count = integer())
      }
    },
    error = function(e) data.frame(discharged_to_source_value = character(), count = integer())
  )

  list(
    vocabulary_version = version,
    total_concepts = total_concepts,
    total_relationships = total_rel,
    vocabularies = stats::setNames(as.list(counts$count), counts$vocabulary_id),
    unmapped_discharge_statuses = unmapped_discharge
  )
}

# --- omop_connect: session helper that makes an attached central vocabulary transparent ---------------

.CENTRAL_VOCAB_ALIAS <- "central_vocab"
# Looked up, in this order, in the directory that holds the primary database (nowhere else).
.VOCAB_DB_FILENAMES <- c("central_vocabulary.duckdb", "vocabulary.duckdb", "vocab.duckdb")
# Same tables attach_central_vocabulary() exposes.
.CENTRAL_VOCAB_TABLES <- c(
  "concept", "concept_relationship", "concept_ancestor", "concept_synonym",
  "vocabulary", "relationship", "concept_class", "domain", "drug_strength"
)
.CENTRAL_VOCAB_SEARCH_PATH <- paste0("main,", .CENTRAL_VOCAB_ALIAS, ".main")

#' @keywords internal
#' @noRd
vocab_abs_path <- function(path) normalizePath(path, winslash = "/", mustWork = FALSE)

#' @keywords internal
#' @noRd
same_file <- function(a, b) {
  if (is.null(a) || is.null(b) || !nzchar(a) || !nzchar(b)) {
    return(FALSE)
  }
  a <- vocab_abs_path(a)
  b <- vocab_abs_path(b)
  if (.Platform$OS.type == "windows") {
    a <- tolower(a)
    b <- tolower(b)
  }
  identical(a, b)
}

# Absolute path of the connection's current database file, or NULL for an in-memory database.
#' @keywords internal
#' @noRd
primary_database_file <- function(con) {
  res <- DBI::dbGetQuery(con, "SELECT path FROM duckdb_databases() WHERE database_name = current_database()")
  if (nrow(res) == 0 || is.na(res$path[[1]]) || !nzchar(res$path[[1]])) {
    return(NULL)
  }
  vocab_abs_path(res$path[[1]])
}

# NULL if no `central_vocab` database is attached, else its path ("" if it has none).
#' @keywords internal
#' @noRd
attached_central_vocab <- function(con) {
  res <- DBI::dbGetQuery(con, sprintf(
    "SELECT path FROM duckdb_databases() WHERE database_name = %s", DBI::dbQuoteString(con, .CENTRAL_VOCAB_ALIAS)
  ))
  if (nrow(res) == 0) {
    return(NULL)
  }
  if (is.na(res$path[[1]])) "" else res$path[[1]]
}

# First of .VOCAB_DB_FILENAMES that sits next to `db_file` (never `db_file` itself).
#' @keywords internal
#' @noRd
discover_sibling_vocab <- function(db_file) {
  if (is.null(db_file)) {
    return(NULL)
  }
  for (name in .VOCAB_DB_FILENAMES) {
    candidate <- file.path(dirname(db_file), name)
    if (file.exists(candidate) && !dir.exists(candidate) && !same_file(candidate, db_file)) {
      return(vocab_abs_path(candidate))
    }
  }
  NULL
}

# Lower-case names of the vocabulary tables the attached `central_vocab` holds in `main`.
#' @keywords internal
#' @noRd
central_vocab_table_names <- function(con) {
  names_sql <- paste(DBI::dbQuoteString(con, .CENTRAL_VOCAB_TABLES), collapse = ", ")
  tolower(DBI::dbGetQuery(con, sprintf(
    paste0(
      "SELECT table_name FROM information_schema.tables ",
      "WHERE table_catalog = %s AND table_schema = 'main' AND table_name IN (%s)"
    ),
    DBI::dbQuoteString(con, .CENTRAL_VOCAB_ALIAS), names_sql
  ))$table_name)
}

# Let an *empty* local vocabulary table fall through to the attached vocabulary.
#
# A database built with build_schema() has empty local `concept`, `concept_ancestor`, ... tables.
# `search_path` puts `main` first, so they would shadow the attached vocabulary and every vocabulary
# query would silently return nothing. For each such empty table (and only those), a session-scoped
# TEMP view over `central_vocab` is created; temp objects are resolved first, and nothing is written to
# either database file. A populated local table, or any temp object of the same name (e.g. a view left
# by attach_central_vocabulary()), is left alone.
#' @keywords internal
#' @noRd
expose_central_vocab_tables <- function(con) {
  primary <- DBI::dbGetQuery(con, "SELECT current_database() AS db")$db[[1]]
  if (identical(primary, .CENTRAL_VOCAB_ALIAS)) {
    return(invisible(NULL))
  }
  names_sql <- paste(DBI::dbQuoteString(con, .CENTRAL_VOCAB_TABLES), collapse = ", ")
  central <- central_vocab_table_names(con)
  local_tables <- tolower(DBI::dbGetQuery(con, sprintf(
    paste0(
      "SELECT table_name FROM duckdb_tables() ",
      "WHERE database_name = %s AND schema_name = 'main' AND table_name IN (%s)"
    ),
    DBI::dbQuoteString(con, primary), names_sql
  ))$table_name)
  session <- tolower(DBI::dbGetQuery(con, sprintf(
    paste0(
      "SELECT table_name FROM duckdb_tables() WHERE database_name = 'temp' AND table_name IN (%s) ",
      "UNION ALL SELECT view_name FROM duckdb_views() WHERE database_name = 'temp' AND NOT internal ",
      "AND view_name IN (%s)"
    ),
    names_sql, names_sql
  ))$table_name)
  for (tbl in .CENTRAL_VOCAB_TABLES) {
    if (!(tbl %in% central) || !(tbl %in% local_tables) || tbl %in% session) next
    ident <- DBI::dbQuoteIdentifier(con, tbl)
    has_rows <- DBI::dbGetQuery(con, sprintf(
      "SELECT EXISTS (SELECT 1 FROM %s.main.%s) AS has_rows", DBI::dbQuoteIdentifier(con, primary), ident
    ))$has_rows[[1]]
    if (!isTRUE(has_rows)) {
      DBI::dbExecute(con, sprintf(
        "CREATE OR REPLACE TEMP VIEW %s AS SELECT * FROM %s.main.%s", ident, .CENTRAL_VOCAB_ALIAS, ident
      ))
    }
  }
  invisible(NULL)
}

# Whether `concept` resolves (locally or through the search path) and holds at least one row.
#' @keywords internal
#' @noRd
has_vocabulary_rows <- function(con) {
  tryCatch(
    isTRUE(DBI::dbGetQuery(con, "SELECT EXISTS (SELECT 1 FROM concept) AS has_rows")$has_rows[[1]]),
    error = function(e) {
      if (!is_unresolved_name_error(e)) stop(e)
      FALSE
    }
  )
}

# Whether the primary or temp catalog holds any vocabulary table or view (even an empty or dangling one).
#' @keywords internal
#' @noRd
has_vocabulary_tables <- function(con) {
  names_sql <- paste(DBI::dbQuoteString(con, .CENTRAL_VOCAB_TABLES), collapse = ", ")
  res <- DBI::dbGetQuery(con, sprintf(
    paste0(
      "SELECT EXISTS (SELECT 1 FROM (",
      "SELECT database_name, table_name AS name FROM duckdb_tables() ",
      "UNION ALL SELECT database_name, view_name FROM duckdb_views() WHERE NOT internal",
      ") WHERE lower(name) IN (%s) AND database_name IN (current_database(), 'temp')) AS present"
    ),
    names_sql
  ))
  isTRUE(res$present[[1]])
}

# `current` search_path extended so the attached vocabulary is reachable, nothing of it dropped.
#
# The caller's entries are kept, in order, and come first. Then, unless `current` already lists
# `central_vocab` (it is then returned as is -- the caller placed it), the result gets
#  * `main` appended when no entry already means the primary database's `main` schema (`main`, or
#    `<primary>.main`). DuckDB resolves names through the search path in order and only falls back to `main`
#    *after* it, so without this a path such as 'analysis' extended by `central_vocab.main` alone would let
#    the central vocabulary win over a populated local vocabulary table, contradicting the documented
#    precedence (primary `main` first); and
#  * `central_vocab.main` appended last.
# An empty path (the default of a fresh connection) becomes 'main,central_vocab.main'.
#' @keywords internal
#' @noRd
search_path_with_central_vocab <- function(current, primary = NULL) {
  if (is.null(current) || length(current) != 1L || is.na(current)) current <- ""
  entries <- trimws(strsplit(current, ",", fixed = TRUE)[[1]])
  entries <- entries[nzchar(entries)]
  if (length(entries) == 0L) {
    return(.CENTRAL_VOCAB_SEARCH_PATH)
  }
  names_lc <- tolower(gsub("\"", "", entries, fixed = TRUE))
  if (.CENTRAL_VOCAB_ALIAS %in% sub("\\..*$", "", names_lc)) {
    return(paste(entries, collapse = ","))
  }
  main_entries <- "main"
  if (!is.null(primary) && length(primary) == 1L && !is.na(primary) && nzchar(primary)) {
    main_entries <- c(main_entries, tolower(paste0(primary, ".main")))
  }
  if (!any(names_lc %in% main_entries)) {
    entries <- c(entries, "main")
  }
  paste(c(entries, paste0(.CENTRAL_VOCAB_ALIAS, ".main")), collapse = ",")
}

#' @keywords internal
#' @noRd
configure_omop_connection <- function(con, explicit_vocab, auto_attach_vocab, load_macros) {
  db_file <- primary_database_file(con)
  if (!is.null(explicit_vocab) && same_file(explicit_vocab, db_file)) {
    stop(
      "`vocab_db` is the primary database itself (", explicit_vocab, "). A database that already ",
      "holds its own vocabulary needs no separate vocabulary database.",
      call. = FALSE
    )
  }

  attach_sql <- function(path) {
    sprintf("ATTACH %s AS %s (READ_ONLY)", DBI::dbQuoteString(con, path), .CENTRAL_VOCAB_ALIAS)
  }
  attached <- attached_central_vocab(con)
  if (!is.null(explicit_vocab)) {
    if (is.null(attached)) {
      DBI::dbExecute(con, attach_sql(explicit_vocab))
    } else if (!same_file(attached, explicit_vocab)) {
      stop(
        "A different database is already attached as '", .CENTRAL_VOCAB_ALIAS, "' (",
        if (nzchar(attached)) attached else "no file path", "); cannot attach ", explicit_vocab,
        " under the same name.",
        call. = FALSE
      )
    }
  } else if (is.null(attached) && isTRUE(auto_attach_vocab)) {
    discovered <- discover_sibling_vocab(db_file)
    if (!is.null(discovered)) DBI::dbExecute(con, attach_sql(discovered))
  }

  attached <- attached_central_vocab(con)
  vocab_attached <- !is.null(attached)
  empty_vocab <- FALSE
  if (vocab_attached) {
    # A search_path the caller set on a connection passed in is kept, extended by main (when absent)
    # and central_vocab.main.
    current <- DBI::dbGetQuery(con, "SELECT current_setting('search_path') AS search_path")$search_path[[1]]
    primary <- DBI::dbGetQuery(con, "SELECT current_database() AS db")$db[[1]]
    wanted <- search_path_with_central_vocab(current, primary)
    if (!identical(wanted, current)) {
      DBI::dbExecute(con, sprintf("SET search_path = %s", DBI::dbQuoteString(con, wanted)))
    }
    expose_central_vocab_tables(con)
    empty_vocab <- length(central_vocab_table_names(con)) == 0L
  }

  # Macros whose body DuckDB validates when it creates them (map_to_standard_concept_id(), ...) can only
  # be created while the tables and columns they read are visible; the rest are still created, and the
  # gap is reported below.
  skipped <- if (isTRUE(load_macros)) load_mapping_macros(con, temporary = TRUE, skip_unresolved = TRUE) else character()

  notes <- character()
  if (empty_vocab) {
    # Attached on purpose or found next to the database, but a file that holds no vocabulary tables
    # (an empty database, or some other database) would otherwise connect without a word.
    notes <- c(notes, paste0(
      "The vocabulary database attached as '", .CENTRAL_VOCAB_ALIAS, "' (",
      if (nzchar(attached)) attached else "no file path", ") contains none of the vocabulary tables (",
      paste(.CENTRAL_VOCAB_TABLES, collapse = ", "), ") in its main schema, so it provides no concepts. ",
      "Check that vocab_db points at an Athena vocabulary database."
    ))
  }
  if (!vocab_attached && isTRUE(auto_attach_vocab) && !has_vocabulary_rows(con)) {
    where <- if (!is.null(db_file)) {
      paste("next to", db_file)
    } else {
      "(an in-memory database has no directory to look in)"
    }
    notes <- c(notes, paste0(
      "No vocabulary available: no central vocabulary database (", paste(.VOCAB_DB_FILENAMES, collapse = ", "),
      ") was found ", where, ", and the database has no local vocabulary (`concept` is missing or empty). ",
      "Pass vocab_db= to attach one, or auto_attach_vocab=FALSE if none is needed."
    ))
  }
  if (length(skipped) > 0) {
    # Opting out of a vocabulary (auto_attach_vocab = FALSE, none given) on a database with no vocabulary
    # tables at all makes the skipped vocabulary macros expected, so they are not worth a warning.
    opted_out <- !vocab_attached && !isTRUE(auto_attach_vocab) && !has_vocabulary_tables(con)
    if (!opted_out) {
      notes <- c(notes, paste0(
        "These SQL macros were not created because the vocabulary tables or columns they read are not ",
        "available on this connection: ", paste(skipped, collapse = ", "),
        ". Reconnect once a complete vocabulary is available."
      ))
    }
  }
  if (length(notes) > 0) {
    warning("omop_connect: ", paste(notes, collapse = " "), call. = FALSE)
  }
  invisible(NULL)
}

#' Connect to an OMOP DuckDB Database with the Central Vocabulary Attached Transparently
#'
#' Attaches the central Athena vocabulary database read-only as `central_vocab` and sets the
#' connection's `search_path` to `'main,central_vocab.main'`, so `concept`, `concept_ancestor`,
#' `concept_relationship`, ... can be queried with no schema prefix while a 10M-concept vocabulary is
#' stored once instead of inside every site database. (A `search_path` you already set on a connection
#' you pass in is kept and extended, never replaced; see Details.) The shared SQL macros
#' (`descendants_of()`, `ancestors_of()`, `clamp_physiologic()`, `map_to_standard_concept_id()`, the
#' cohort macros, ...) are loaded as session-scoped `TEMP` macros, so connecting never writes to a
#' database file and also works on read-only databases.
#'
#' @details
#' **Closing.** A connection opened by `omop_connect()` must be closed by the caller with
#' `DBI::dbDisconnect(con, shutdown = TRUE)`. A connection passed in as `db_path` is used as is and is
#' never closed by this function, nor are its read-only/read-write mode or existing contents changed
#' (`read_only = TRUE` is ignored, with a warning, for a connection that is open read-write). If setup
#' fails after `omop_connect()` opened the database, it closes it again before raising.
#'
#' **Vocabulary discovery.** An explicit `vocab_db` must exist (otherwise an error is raised) and is
#' always attached. Without one, and with `auto_attach_vocab = TRUE`, `central_vocabulary.duckdb`,
#' `vocabulary.duckdb` or `vocab.duckdb` is looked up (in that order) in the directory that holds the
#' primary database -- nowhere else, and never the primary database itself. If nothing is found and
#' the database has no local vocabulary (`concept` missing or empty), a warning explains how to
#' supply one; the connection is still returned. Calling `omop_connect()` again, or on a connection
#' that already has the same vocabulary attached as `central_vocab`, does not attach it twice; a
#' *different* database already attached under that name is an error.
#'
#' **Search path.** On a fresh connection (empty `search_path`) it becomes `'main,central_vocab.main'`.
#' A `search_path` already set on a connection passed in as `db_path` is not replaced; its entries are kept,
#' in order, and extended: `main` is appended unless an entry already means the primary database's `main`
#' schema (`main` or `<database>.main`), then `central_vocab.main` is appended. So `'analysis,main'` becomes
#' `'analysis,main,central_vocab.main'` and `'analysis'` becomes `'analysis,main,central_vocab.main'` too:
#' the schemas you put first keep taking precedence, and the primary database's own tables always rank ahead
#' of the central vocabulary (DuckDB would otherwise consult `main` only *after* `central_vocab.main`,
#' letting the central vocabulary win over a populated local vocabulary table). A path that already lists
#' `central_vocab` is left exactly as the caller wrote it. Calling `omop_connect()` again changes nothing.
#'
#' **Precedence.** It is per table: a vocabulary table that holds rows in the primary database wins
#' over the attached one. A local table that is merely *empty* -- as every vocabulary table is after
#' [build_schema()] -- does not shadow the attached vocabulary: `omop_connect()` adds a session-only
#' `TEMP` view over the central table for it (so `INSERT` into that table through this connection is
#' refused; use a separate connection to load a local vocabulary).
#'
#' **Macros.** DuckDB validates the body of some macros when it creates them, so
#' `map_to_standard_concept_id()` and `source_concept_id()` are created only if the vocabulary tables
#' (with the columns they read) are visible at connect time; otherwise they are skipped and named in
#' the warning. `descendants_of()` and `ancestors_of()` are always created and look `concept_ancestor`
#' up when they are used, raising a catalog error if it is not visible then.
#'
#' **Per-connection state.** `search_path`, the session-only `TEMP` views and the `TEMP` macros belong
#' to the connection `omop_connect()` configured; the attachment itself belongs to the database
#' instance. A new connection to the same database (for example `DBI::dbConnect(con@driver)`, a
#' connection from a pool, or a connection a parallel worker opens) starts without any of them. On a
#' database built with [build_schema()] such a connection sees the *empty* local `concept` table
#' rather than the central vocabulary and has no `descendants_of()`, so a count of concepts there is
#' silently zero. Pass that connection through `omop_connect()` itself (it accepts an existing
#' connection), or give every worker its own `omop_connect()` connection.
#'
#' **Relation to [attach_central_vocabulary()].** That function is the ETL-time helper: it attaches
#' the vocabulary and creates a zero-copy view per vocabulary table (temporary, or persistent in the
#' database). Both functions recognise an attachment the other (or an earlier call) made: when
#' `central_vocab` is already attached to the *same* file the `ATTACH` is skipped, so they can be used in
#' either order on one connection; a *different* file under that name is an error in both.
#'
#' @param db_path Path of the primary OMOP DuckDB database (`":memory:"` for an in-memory one; a leading
#'   `~` is expanded), or an existing DBI connection. A path that does not exist is created as an empty
#'   database unless `read_only = TRUE` (then it is an error).
#' @param vocab_db Path of the vocabulary database to attach (a leading `~` is expanded). Must exist. If
#'   `NULL` and `auto_attach_vocab` is `TRUE`, a sibling vocabulary database is auto-discovered (see
#'   Details).
#' @param read_only Logical; open the primary database read-only (default `FALSE`). An in-memory
#'   database cannot be read-only (it has no file to open and starts out empty), so `read_only = TRUE`
#'   with `":memory:"` is an error. Ignored, with a warning if `TRUE`, for an existing connection.
#' @param auto_attach_vocab Logical; auto-discover a sibling vocabulary database when `vocab_db` is
#'   `NULL` (default `TRUE`). An explicit `vocab_db` is always attached; one that holds none of the
#'   vocabulary tables (`concept`, `concept_ancestor`, ...) draws a warning. With `FALSE` and no vocabulary
#'   tables in the database at all, the vocabulary macros are skipped without a warning, since you opted
#'   out (`load_macros = FALSE` skips all macros).
#' @param load_macros Logical; load the shared SQL macros as `TEMP` macros (default `TRUE`).
#' @return A DBI connection to the primary database (the same object when `db_path` was a
#'   connection), with `central_vocab` attached and `search_path` configured when a vocabulary was
#'   found. Close it with `DBI::dbDisconnect(con, shutdown = TRUE)` when `omop_connect()` opened it.
#' @seealso [attach_central_vocabulary()], [load_vocabulary()], [build_schema()]
#' @examples
#' db <- tempfile(fileext = ".duckdb")
#' build_schema(db_path = db)
#' con <- omop_connect(db, auto_attach_vocab = FALSE)
#' DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (1, 1, 0, 0), (1, 2, 1, 1), (2, 2, 0, 0)")
#' # Every concept at or below concept 1, concept 1 included
#' DBI::dbGetQuery(con, "SELECT concept_id FROM descendants_of(1) ORDER BY concept_id")
#' DBI::dbGetQuery(con, "SELECT clamp_physiologic(250, 0, 100) AS clamped")
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' unlink(db)
#'
#' \dontrun{
#' # A site database plus a shared Athena vocabulary database (the vocabulary file is
#' # auto-discovered when it sits next to the site database)
#' con <- omop_connect(
#'   "site_cdm.duckdb",
#'   vocab_db = "central_vocabulary.duckdb",
#'   read_only = TRUE
#' )
#' # 201826 = SNOMED 'Type 2 diabetes mellitus'
#' DBI::dbGetQuery(con, "
#'   SELECT COUNT(*) AS n FROM condition_occurrence
#'   WHERE condition_concept_id IN (SELECT concept_id FROM descendants_of(201826))
#' ")
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' }
#' @export
omop_connect <- function(db_path,
                         vocab_db = NULL,
                         read_only = FALSE,
                         auto_attach_vocab = TRUE,
                         load_macros = TRUE) {
  explicit_vocab <- NULL
  if (!is.null(vocab_db)) {
    if (!is.character(vocab_db) || length(vocab_db) != 1L || is.na(vocab_db)) {
      stop("`vocab_db` must be a single file path.", call. = FALSE)
    }
    explicit_vocab <- vocab_abs_path(vocab_db)
    if (!file.exists(explicit_vocab) || dir.exists(explicit_vocab)) {
      stop("Vocabulary database not found: ", explicit_vocab, call. = FALSE)
    }
  }

  owns_connection <- !inherits(db_path, "DBIConnection")
  if (owns_connection) {
    if (!is.character(db_path) || length(db_path) != 1L || is.na(db_path)) {
      stop("`db_path` must be a single file path or an existing DBI connection.", call. = FALSE)
    }
    db_path <- path.expand(db_path)
    if (isTRUE(read_only) && (identical(db_path, "") || startsWith(db_path, ":memory:"))) {
      stop(
        "`read_only = TRUE` cannot be combined with an in-memory database: it has no file to open ",
        "read-only and starts out empty. Pass the path of a database file.",
        call. = FALSE
      )
    }
    if (isTRUE(read_only) && !file.exists(db_path)) {
      stop("Database file not found: ", db_path, call. = FALSE)
    }
    con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = isTRUE(read_only))
    ok <- FALSE
    on.exit(if (!ok) DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  } else {
    con <- db_path
    if (!DBI::dbIsValid(con)) {
      stop("`db_path` is a closed or invalid connection.", call. = FALSE)
    }
    if (isTRUE(read_only) && !con_is_read_only(con)) {
      warning(
        "omop_connect: read_only = TRUE has no effect on an existing connection that is open ",
        "read-write; open it with DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE) instead.",
        call. = FALSE
      )
    }
  }

  configure_omop_connection(con, explicit_vocab, auto_attach_vocab, load_macros)
  if (owns_connection) ok <- TRUE
  con
}
