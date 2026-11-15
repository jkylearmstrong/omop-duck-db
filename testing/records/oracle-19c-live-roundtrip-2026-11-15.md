# Live Oracle round trip (scripts/oracle_roundtrip_check.py), 2026-11-15

First run on a site's live Oracle data, read only (one `SET TRANSACTION READ ONLY` transaction; no `--write-back`). Site named by its
anonymous label only; no row counts.

- Oracle: Oracle Database 19c EE Extreme Perf Release 19.0.0.0.0; NLS_CHARACTERSET WE8MSWIN1252 (so the CONVERT-to-UTF-8 text hash path ran)
- Tables: PCORnet CDM tables plus site analysis tables, not OMOP; `--tables all` of the owner
- Privacy scan of the records (`scripts/check_run_privacy.py`): 0 hits

| run | site | tables | exit | time | Oracle -> DuckDB | Oracle vs PostgreSQL | Oracle vs DuckDB read back from PostgreSQL |
|---|---|---|---|---|---|---|---|
| smoke (`--read-back --row-level`) | Site A | 1 | 0 | 25 s | PASS | PASS | PASS |
| full (`--tables all --read-back`) | Site A | 22 | 0 | 5271 s | PASS (1176 column statistics, 1088 s) | PASS (3320 s) | PASS (862 s) |

No differing statistic in any table. The row-level re-read ran on the smoke table only. Most of the PostgreSQL time is `store_site`'s own
DuckDB-vs-PostgreSQL `verify_transfer`, which reads each column separately.
