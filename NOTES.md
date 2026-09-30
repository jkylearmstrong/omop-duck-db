# Notes

Internal working notes on where this package came from and where it's headed. See `README.Rmd`/`README.md` for the technical package overview.

## Where it came from

`omop-duck-db` (R package `omopduckdb`) builds and populates an OMOP CDM v5.4 database in DuckDB, loads the OHDSI Athena vocabulary, and maps PCORnet-format source data into the CDM — with parallel R and Python ETL implementations sharing the same schema and concept-mapping SQL, so either language gets the same result. It's deliberately the public/generic piece of a bigger initiative: no institution names, no site specifics, no protected data, ever — real-world use against production data happens entirely outside this repo, against your own infrastructure (see `kyle_out_zip/omop_bridge/` for that private, site-aware layer).

## Key decisions (and why)

- **License: dual `GPL-3 | MIT`**, changed from plain MIT on 2026-08-05 to match the rest of the public track (`TempleCBE`, `pslongSim`, `ML-PScore`, `quarto_temple_brand`). Still local-only, not pushed.
- **Schema is generated, never hand-declared.** `inst/extdata/5.4/duckdb/*.sql` (from OHDSI's `CommonDataModel` R package) is the single source of truth for both ETL paths — this is why `kyle_out_zip`'s bridging script calls this package's real entrypoint instead of writing new mapping SQL; a hand-rolled alternative (`kyle_out_zip/omop_duckdb_gpu_server_integration_plan.md`) diverged from this and would have produced a broken database (wrong ID scheme, wrong glob paths, invented columns).

## Current status (2026-08-05)

Verified directly against source this session, not assumed:
- The Python ETL (`build_omop_cdm.py`) has **no `VITAL` → `MEASUREMENT` mapping** yet — only `demographic`, `provider`, `encounter`, `diagnosis`, `procedures`, `lab_result_cm`, `prescribing` are handled.
- `load_vocabulary()` is **R-only** — no Python equivalent exists yet.
- PCORnet source file/column expectations were cross-checked against `tests/fixtures/pcornet_sample/` and confirmed to match what `kyle_out_zip`'s real per-site extracts actually look like (modulo one `ssid`→`PATID` header rename).

## Where it's heading

- Close the two gaps above (`VITAL` mapping, Python-side vocabulary loader) — needed before `kyle_out_zip`'s eDerri pipeline can capture vitals data or run entirely in Python without an R fallback.
- Commit + push the license change.
- Once `kyle_out_zip/omop_bridge/` runs successfully against real site data, OHDSI's DataQualityDashboard is the next milestone (tracked in `tasks/Big_Picture.txt`), still not wired up here.
