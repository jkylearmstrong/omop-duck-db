#' Detect physical system RAM
#'
#' Best-effort, dependency-free detection (PowerShell on Windows, `/proc/meminfo` on Linux, `sysctl` /
#' `vm_stat` on macOS). Mirror of `omop_etl.resources.detect_system_ram` in Python.
#'
#' @return A named numeric vector `c(total = <bytes>, available = <bytes>)`; both `NA` when undetectable.
#' @examples
#' detect_system_ram()
#' @export
detect_system_ram <- function() {
  none <- c(total = NA_real_, available = NA_real_)
  tryCatch({
    sys <- Sys.info()[["sysname"]]
    if (identical(sys, "Windows")) {
      out <- suppressWarnings(system2(
        "powershell",
        c("-NoProfile", "-Command",
          shQuote("$o = Get-CimInstance Win32_OperatingSystem; $o.TotalVisibleMemorySize; $o.FreePhysicalMemory")),
        stdout = TRUE, stderr = FALSE
      ))
      v <- suppressWarnings(as.numeric(trimws(out)))
      v <- v[!is.na(v)]
      if (length(v) >= 2) return(c(total = v[[1]] * 1024, available = v[[2]] * 1024))
    } else if (identical(sys, "Linux")) {
      lines <- readLines("/proc/meminfo", warn = FALSE)
      kb <- function(key) {
        hit <- grep(paste0("^", key, ":"), lines, value = TRUE)
        if (length(hit)) as.numeric(gsub("[^0-9]", "", hit[[1]])) * 1024 else NA_real_
      }
      total <- kb("MemTotal")
      avail <- kb("MemAvailable")
      if (is.na(avail)) avail <- kb("MemFree")
      for (path in c("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")) {
        if (file.exists(path)) {
          raw <- trimws(readLines(path, n = 1, warn = FALSE))
          if (grepl("^[0-9]+$", raw) && !is.na(total) && as.numeric(raw) < total) {
            total <- as.numeric(raw)
            avail <- min(avail, total, na.rm = TRUE)
          }
        }
      }
      if (!is.na(total) && !is.na(avail)) return(c(total = total, available = avail))
    } else if (identical(sys, "Darwin")) {
      total <- as.numeric(system2("sysctl", c("-n", "hw.memsize"), stdout = TRUE))
      avail <- total
      vm <- system2("vm_stat", stdout = TRUE)
      page <- as.numeric(sub(".*page size of ([0-9]+).*", "\\1", vm[grepl("page size of", vm)][[1]]))
      pages <- function(key) {
        hit <- vm[startsWith(vm, key)]
        if (length(hit)) as.numeric(gsub("[^0-9]", "", sub("^[^:]*:", "", hit[[1]]))) else 0
      }
      free <- (pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")) * page
      if (is.finite(free) && free > 0) avail <- free
      return(c(total = total, available = avail))
    }
    none
  }, error = function(e) none)
}

#' @keywords internal
#' @noRd
.disk_free_bytes <- function(path) {
  tryCatch({
    if (identical(Sys.info()[["sysname"]], "Windows")) {
      drive <- toupper(substr(normalizePath(path, winslash = "/", mustWork = FALSE), 1, 1))
      out <- suppressWarnings(system2(
        "powershell", c("-NoProfile", "-Command", shQuote(sprintf("(Get-PSDrive -Name %s).Free", drive))),
        stdout = TRUE, stderr = FALSE
      ))
      as.numeric(trimws(out[[1]]))
    } else {
      out <- suppressWarnings(system2("df", c("-Pk", shQuote(path)), stdout = TRUE, stderr = FALSE))
      as.numeric(strsplit(trimws(out[[length(out)]]), "\\s+")[[1]][[4]]) * 1024
    }
  }, error = function(e) NA_real_)
}

#' @keywords internal
#' @noRd
.pick_spill_directory <- function() {
  # Nothing is created until a winner is chosen. The system drive and POSIX "/" are never written at their
  # root: they spill under the temp directory.
  if (identical(Sys.info()[["sysname"]], "Windows")) {
    sys_drive <- toupper(Sys.getenv("SystemDrive", "C:"))
    targets <- character(0)
    for (letter in LETTERS[3:26]) {
      drive <- paste0(letter, ":/")
      if (!dir.exists(drive)) next
      targets <- c(targets, if (identical(paste0(letter, ":"), sys_drive)) {
        file.path(tempdir(), "omop_duckdb_spill")
      } else if (file.access(drive, 2) == 0) {
        paste0(drive, "omop_duckdb_spill")
      })
    }
  } else {
    targets <- c("/tmp/omop_duckdb_spill", "/var/tmp/omop_duckdb_spill",
                 file.path(path.expand("~"), "omop_duckdb_spill"), file.path(getwd(), "omop_duckdb_spill"))
  }
  targets <- unique(c(targets, file.path(tempdir(), "omop_duckdb_spill")))
  nearest <- function(p) {
    while (!dir.exists(p) && dirname(p) != p) p <- dirname(p)
    p
  }
  free <- vapply(targets, function(t) .disk_free_bytes(nearest(t)), numeric(1))
  for (i in order(-ifelse(is.na(free), -1, free))) {
    ok <- tryCatch({
      dir.create(targets[[i]], recursive = TRUE, showWarnings = FALSE)
      probe <- file.path(targets[[i]], ".write_probe")
      writeLines("ok", probe)
      unlink(probe)
      TRUE
    }, error = function(e) FALSE, warning = function(w) FALSE)
    if (ok) return(list(path = targets[[i]], free = if (is.na(free[[i]])) NA_real_ else free[[i]]))
  }
  fallback <- file.path(tempdir(), "omop_duckdb_spill")
  dir.create(fallback, recursive = TRUE, showWarnings = FALSE)
  list(path = fallback, free = NA_real_)
}

#' Size a DuckDB connection to the host: RAM limit, disk-spill location and threads
#'
#' Sets `memory_limit` to a fraction of the host's RAM, points `temp_directory` at the writable volume with
#' the most free space so large joins spill gracefully, and sets `threads` from the usable cores.
#' Mirror of `omop_etl.resources.configure_resources` in Python.
#'
#' @param con DuckDB DBI connection (modified in place).
#' @param memory_fraction Fraction of RAM DuckDB may use (default 0.75), in `(0, 1]`.
#' @param temp_directory Spill directory. Default: auto-pick the volume with the most free space.
#' @param threads Thread count. Default: usable cores minus `reserve_threads` (at least 1).
#' @param use_available_ram Base the limit on currently *available* RAM (default) instead of total RAM.
#' @param reserve_threads Cores left to the OS when `threads` is not given.
#' @param max_temp_directory_size Optional cap on spill size, e.g. `"200GB"`.
#' @return A list of class `"ResourceConfig"`: `total_ram_bytes`, `available_ram_bytes`, `memory_limit`,
#'   `temp_directory`, `temp_free_bytes`, `threads`, `notes`. If RAM cannot be detected the DuckDB default
#'   `memory_limit` is left in place and a note says so.
#' @examples
#' con <- DBI::dbConnect(duckdb::duckdb())
#' cfg <- configure_resources(con, memory_fraction = 0.5, threads = 2)
#' cfg$threads
#' DBI::dbDisconnect(con, shutdown = TRUE)
#' @export
configure_resources <- function(con, memory_fraction = 0.75, temp_directory = NULL, threads = NULL,
                                use_available_ram = TRUE, reserve_threads = 1L, max_temp_directory_size = NULL) {
  if (!is.numeric(memory_fraction) || memory_fraction <= 0 || memory_fraction > 1) {
    stop("memory_fraction must be in (0, 1].")
  }
  notes <- character(0)

  ram <- detect_system_ram()
  basis <- if (isTRUE(use_available_ram) && !is.na(ram[["available"]])) ram[["available"]] else ram[["total"]]
  if (!is.na(basis)) {
    mb <- max(256L, as.integer(floor(basis * memory_fraction / (1024 * 1024))))
    DBI::dbExecute(con, sprintf("SET memory_limit = '%dMB'", mb))
    limit <- sprintf("%dMB", mb)
  } else {
    limit <- as.character(DBI::dbGetQuery(con, "SELECT current_setting('memory_limit') AS v")$v[[1]])
    notes <- c(notes, "Physical RAM could not be detected; left DuckDB's default memory_limit in place.")
  }

  if (is.null(temp_directory)) {
    spill <- .pick_spill_directory()
    spill_dir <- spill$path
    free <- spill$free
  } else {
    spill_dir <- temp_directory
    dir.create(spill_dir, recursive = TRUE, showWarnings = FALSE)
    free <- .disk_free_bytes(spill_dir)
  }
  DBI::dbExecute(con, sprintf("SET temp_directory = '%s'", sanitize_sql_path(spill_dir)))
  if (!is.null(max_temp_directory_size)) {
    DBI::dbExecute(con, sprintf("SET max_temp_directory_size = '%s'", max_temp_directory_size))
  }

  n_threads <- if (!is.null(threads)) as.integer(threads) else max(1L, parallel::detectCores(logical = TRUE) - as.integer(reserve_threads))
  if (is.na(n_threads) || n_threads < 1L) stop("threads must be >= 1.")
  DBI::dbExecute(con, sprintf("SET threads = %d", n_threads))

  structure(
    list(
      total_ram_bytes = unname(ram[["total"]]), available_ram_bytes = unname(ram[["available"]]),
      memory_limit = limit, temp_directory = sanitize_sql_path(spill_dir), temp_free_bytes = free,
      threads = n_threads, notes = notes
    ),
    class = "ResourceConfig"
  )
}
