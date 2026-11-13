# Tested environments

What has been run for real, with the evidence, and what has not. Update this file when a new run is recorded; do not add a claim without a record.

## Oracle: `to_sql` / `verify_transfer`

| | |
|---|---|
| Date | 2026-11-13 (two runs the same day) |
| Check | `scripts/verify_server_roundtrip.py` (synthetic table, 2,000 rows) |
| Server | Oracle Database 19c Enterprise Edition, Extreme Performance (19.0.0.0.0) |
| Driver | `python-oracledb`, SQLAlchemy `oracle+oracledb`, connection by TNS alias |
| Settings | `NLS_CHARACTERSET = WE8MSWIN1252`, `NLS_NCHAR_CHARACTERSET = AL16UTF16`, `NLS_LENGTH_SEMANTICS = BYTE` |
| Run 1 (counts and sums) | PASS. Record: [`records/oracle-19c-roundtrip-2026-11-13.md`](records/oracle-19c-roundtrip-2026-11-13.md) |
| Run 2 (adds value-level comparison, `content=True`) | PASS with two known differences. Record: [`records/oracle-19c-roundtrip-content-2026-11-13.md`](records/oracle-19c-roundtrip-content-2026-11-13.md) |
| Load speed | 2,000 rows in 0.22 to 0.26 s (a rough figure; one small run) |

**What the runs show.** `to_sql` created the table and loaded the rows on a live 19c server, and the scratch table was dropped afterwards. For every column the non-null count matched, and for numeric columns (BIGINT near 2^62, DOUBLE, DECIMAL(18,4)) the sum, minimum and maximum matched. With the value-level comparison, these columns matched **by value**: Latin text including accented characters (`café naïve résumé`) and 900-character strings, DATE (including dates before 1970), TIMESTAMP with fractional seconds, and BOOLEAN.

**Two differences, both expected on this server.**

1. **Empty strings become NULL** (667 sent, 0 stored). This is Oracle's normal behaviour. The value comparison treats an empty string as NULL on the local side for Oracle, so that column's content matches and only its non-null count differs.
2. **Non-Latin text is not preserved.** The database character set is `WE8MSWIN1252`, which cannot store Japanese characters. Every value of the Japanese test column came back with each character replaced by `¿` (each eight-character Japanese string came back as eight `¿` followed by the row number, for example `¿¿¿¿¿¿¿¿ 56`). `to_sql` raised no error and no warning, and counts and sums alone could not see it; only the value comparison did. **Do not push text that may contain non-Latin characters (for example patient names, addresses or free-text notes in other languages) to a database like this without checking.** A database with `AL32UTF8` should keep it; that was not tested. A warning from `to_sql` for non-Unicode targets is a sensible follow-up.

**What the runs do not show.**

- Only the account's default schema was used (`schema=` untested), with `if_exists="fail"` only. `replace` and `append` were not run.
- NaN / Infinity doubles, `HUGEINT`, time-zone timestamps (not compared by value) and a failed-login error message (credential scrubbing with the real driver) were not exercised.
- One small run on one server with one account. Throughput at scale is unmeasured, and the value comparison reads every value, so it is slower on large tables.
- Oracle Free 23 in CI and other Oracle versions were not run.

## DGX Spark: `Dockerfile.dgx` and `scripts/dgx_smoke_test.py`

| | |
|---|---|
| Date | 2026-11-13 |
| Host | aarch64 DGX Spark, NVIDIA GB10 (compute capability 12.1), driver 580.178.04, Docker 29.6.2 |
| Image | built from this repository's `Dockerfile.dgx` (base `nvcr.io/nvidia/pytorch:25.01-py3`, torch 2.6.0a0 nv25.01, CUDA 12.8) |
| `gpu-check` | passed (device visible: NVIDIA GB10, (12, 1)) |
| `dgx-smoke` | **passed** after the NumPy pin below. Record: [`records/dgx-spark-smoke-2026-11-13.md`](records/dgx-spark-smoke-2026-11-13.md) |

**A bug found by this run, and fixed here.** The image as it stood failed `dgx-smoke` with `RuntimeError: Numpy is not available`. The base image ships NumPy 1.26.4 and its torch is built against it; installing the package extras upgraded NumPy to 2.2.6, which that torch cannot use. `Dockerfile.dgx` now pins NumPy to the base image's version in the same constraint file that pins torch. With the pin, NumPy stays at 1.26.4 and the smoke test trains and scores on the GPU in about 10 s.

**What it shows.** The image builds on aarch64, sees the GB10, and the package's MLP trains and is scored with the study tables on CUDA.

**What it does not show.** The smoke test uses synthetic data (AUCs of about 0.66 to 0.72 say nothing about the study). It does not run the study pipeline, the Transformer or other trainers, multi-GPU, or long jobs. The base image prints a warning that GB10 "may not yet be supported" in this container version; the run worked regardless. Only one DGX Spark was tested.

## Everything else

The offline test suite runs on every change (1,643 tests passed, 29 skipped, on 2026-11-13 on a Windows workstation with Python 3.13). Gaps still open are listed in [`real-world-testing-plan.md`](real-world-testing-plan.md).
