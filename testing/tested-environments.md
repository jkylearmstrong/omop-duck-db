# Tested environments

What has been run for real, with the evidence, and what has not. Update this file when a new run is recorded; do not add a claim without a record.

## Oracle: `to_sql` / `verify_transfer`

| | |
|---|---|
| Date | 2026-11-13 |
| Check | `scripts/verify_server_roundtrip.py` (synthetic table, 2,000 rows) |
| Server | Oracle Database 19c Enterprise Edition, Extreme Performance (19.0.0.0.0) |
| Driver | `python-oracledb`, SQLAlchemy `oracle+oracledb`, connection by TNS alias |
| Settings | `NLS_CHARACTERSET = WE8MSWIN1252`, `NLS_NCHAR_CHARACTERSET = AL16UTF16`, `NLS_LENGTH_SEMANTICS = BYTE` |
| Result | **PASS**. Record: [`records/oracle-19c-roundtrip-2026-11-13.md`](records/oracle-19c-roundtrip-2026-11-13.md) |
| Load speed | 2,000 rows in 0.26 s (a rough figure; one small run) |

**What the pass shows.** `to_sql` created the table and loaded the rows on a live 19c server. For every column, the non-null count matched. For numeric columns (BIGINT near 2^62, DOUBLE, DECIMAL(18,4)) the sum, minimum and maximum matched. The scratch table was dropped afterwards. The known Oracle difference was seen as expected: empty strings are stored as NULL (667 local, 0 remote in the `txt_empty` column).

**What it does not show.**

- **Text, dates and timestamps are checked by non-null count only**, not by value. The server's character set is `WE8MSWIN1252`, which cannot hold the Japanese test strings, so those characters were very likely replaced on the way in. The check could not see that. Do not read this record as proof that non-Latin text survives. A value-level comparison is a follow-up.
- Only the account's default schema was used (`schema=` untested), with `if_exists="fail"` only. `replace` and `append` were not run.
- NaN / Infinity doubles, `HUGEINT`, time-zone timestamps and a failed-login error message (credential scrubbing with the real driver) were not exercised.
- One small run on one server with one account. Throughput at scale is unmeasured.
- Oracle Free 23 in CI and other Oracle versions were not run.

## DGX Spark

**Not verified.** `scripts/dgx_smoke_test.py` and `Dockerfile.dgx` have not been run on a DGX Spark, and no record exists. Earlier DGX work on a study pipeline does not test this repository's image. When it is run, record the container image or tag, the PyTorch and CUDA versions, the output of `dgx_smoke_test.py` and the date, and add a section here.

## Everything else

The offline test suite runs on every change (1,643 tests passed, 29 skipped, on 2026-11-13 on a Windows workstation with Python 3.13). Gaps still open are listed in [`real-world-testing-plan.md`](real-world-testing-plan.md).
