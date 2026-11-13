# Real-world testing plan for omop-duck-db

2026-11-13 · based on main at v0.22.0 (69c8532)

## Summary

Three paths have never been checked outside synthetic or offline tests. They differ a lot in how much CI can do:

| Gap | Can CI cover it? | What still needs a person |
|---|---|---|
| `to_sql` / `verify_transfer` on Oracle | **Mostly yes, and cheaply.** The live-db workflow already starts Oracle Free 23 and passes on every relevant PR; it just never calls `to_sql`. | One run against a real site Oracle (19c, real schema grants, quotas, wallet/TLS, real volume). |
| DGX Spark GPU image (`Dockerfile.dgx`) | **Partly.** CI can lint, build a CPU stand-in, and run the smoke test on CPU on x86 and arm64. It cannot reproduce GB10 / sm_121 CUDA. | Building and running `gpu-check` and `dgx-smoke` on the Spark itself, then pinning the verified NGC tag. |
| Post-0.11.1 features on non-synthetic data | **Partly.** CI can run everything on the MIMIC-IV demo in OMOP (100 real, de-identified patients, open access). | A field check at one or more real sites that returns only aggregate, small-cell-suppressed output. |

Suggested order: (1) Oracle `to_sql` in CI, about half a day; (2) MIMIC demo job plus a site field-check script, two to three days; (3) DGX CI stand-in plus a one-hour on-device session.

## 1. `to_sql` against a live Oracle server

### Where it stands

- `python/omop_etl/load_sql.py` (`to_sql`, `verify_transfer`, added in 0.18.0) says in its own docstring that it has never been run against a live Oracle.
- Offline coverage: `tests/test_deep_and_transfer.py` round-trips through SQLite; `tests/test_oracle_offline.py` compiles Oracle DDL and checks identifier rules without a server.
- `.github/workflows/live-db.yml` already runs PostgreSQL 16, SQL Server 2022 and **Oracle Free 23** service containers (last 5 runs green, about 2.5 minutes each). But `tests/test_live_databases.py` only tests `from_sql`, `import_omop`, the generated DDL and cohort SQL, and the workflow's path filter does not include `load_sql.py`, so a change to `to_sql` never triggers it.

### Oracle-specific risks the live test should target

These come from reading the code; none is confirmed as a bug yet.

1. **Empty string is NULL in Oracle.** A DuckDB `''` lands as NULL, so `verify_transfer`'s `non_null` count would report a mismatch on an otherwise lossless copy. Either compare differently on Oracle or document it.
2. **VARCHAR2 byte vs character length.** `VARCHAR2(4000)` uses byte semantics on most databases, so a long value with accented or non-Latin text overflows even though it is under 4000 characters.
3. **HUGEINT / UBIGINT into `NUMBER(19)`** overflows for values above 2^63.
4. **`TIMESTAMP WITH TIME ZONE`** maps to plain `TIMESTAMP`, so the zone is dropped silently.
5. **`BINARY_DOUBLE` with NaN / Infinity**, and `sum()` precision against `rtol=1e-9` on large float columns.
6. **`if_exists="replace"` / `"append"` and `has_table`** with an upper-cased table name and a non-default `schema=` (writing into another user's schema needs grants).
7. **Throughput.** Inserts are SQLAlchemy `executemany` with dict rows, 50,000 per chunk; nobody has measured rows per second on Oracle, which decides whether pushing a full CDM is practical.
8. **Credential scrubbing** in errors raised by the real `oracledb` driver (the offline test only uses a fake URL).

### What can be automated in CI

- Add `python/omop_etl/load_sql.py` to both `paths` filters in `live-db.yml`.
- Add tests to `tests/test_live_databases.py`, reusing its `engine` fixture so they run on all four backends:
  - round trip `person` and a query source through `to_sql` then `verify_transfer`, asserting every row matches (or that only the documented empty-string rows differ on Oracle);
  - an edge-case table: NULLs, `''`, 4000-character ASCII and multibyte strings, dates before 1970, timestamps with fractional seconds, `DECIMAL(38,9)`, NaN/Inf doubles, BOOLEAN, values near the BIGINT limits;
  - `if_exists` fail / replace / append, the `columns=` subset, `chunksize=1` and a large chunk;
  - a non-default `schema=` (create a second user in the Oracle container's init);
  - a credential-scrub check with a wrong password against the real driver;
  - a timed load of about 1 million rows that writes rows per second to the job summary (report it, do not fail on it).
- Optionally add an **Oracle XE 21c** service (`gvenzl/oracle-xe:21-slim`) to get closer to sites on 19c. There is no free 19c image on a public registry, so 19c itself stays manual.

### What needs a person

- One run against a real site Oracle (ideally 19c) with a non-admin account: `to_sql` a feature table, `verify_transfer`, then read it back with `import_omop`. Record the Oracle version, `NLS_CHARACTERSET`, `NLS_LENGTH_SEMANTICS`, whether a wallet or TLS was needed, and the measured throughput.
- Needs: a test schema with CREATE TABLE and a tablespace quota, network access from wherever the package runs, and a non-PHI table to load (the synthetic twin or an aggregate feature table is fine).

### Done when

The live-db job runs the new tests green on Oracle Free (and XE 21c if added), the empty-string and byte-length behaviour is fixed or documented, the docstring's "never run against a live Oracle" line is updated, and one site run is written up in the `status_and_roadmap` vignette.

## 2. DGX Spark GPU image

### Where it stands

- `Dockerfile.dgx` builds on `nvcr.io/nvidia/pytorch:25.01-py3` for the DGX Spark (GB10, aarch64, sm_121). The file itself says to pin "the tag you have verified on the device"; nobody has.
- No workflow builds it (`docker-release.yml` builds only the R image).
- The build context is the **parent eDerri project**: it copies `omop-learn-repo/` and `pipeline/`, which are not in this repo, so it cannot build from a plain checkout.
- It has a build-time import check, plus `gpu-check` and `dgx-smoke` (`scripts/dgx_smoke_test.py --require-gpu`, which trains the study MLP on random data and requires leave-one-site-out AUROC above 0.6).

### Risks

1. **The NGC tag may not support GB10.** Whether 25.01 ships kernels for sm_121 needs checking against NVIDIA's release notes (I have not confirmed it); a later tag may be needed. Check on the device first.
2. **Silent `omop-learn` failure.** It installs with `|| true`, so a broken install only shows up when training starts.
3. **aarch64 wheels** for `xgboost`, `shap`, `zarr`, `oracledb` and `duckdb` must exist for the NGC Python version.
4. **Memory.** GB10 has unified memory; `--ipc=host` and the `memlock` settings matter for DataLoader workers on real tensor files.

### What can be automated in CI

- **Static checks** on every PR touching the file: `hadolint`, and a check that every `COPY` source exists (with the parent-project folders stubbed).
- **A CPU stand-in build:** swap the NGC base for a slim Python image with CPU torch (make the base an ARG), stub `omop-learn-repo/` and `pipeline/` so the context resolves, run the build-time import check, then `python scripts/dgx_smoke_test.py` without `--require-gpu`. This catches broken extras, missing imports and smoke-test regressions.
- **The same stand-in on arm64**, using GitHub's `ubuntu-24.04-arm` runner, to catch missing aarch64 wheels.
- **Do not pull the full NGC image in hosted CI.** It is well over 10 GB, close to a hosted runner's free disk.
- Optional: register the Spark as a **self-hosted runner** labelled `dgx`, with a `workflow_dispatch` job that builds the real image and runs `gpu-check` and `dgx-smoke --rows 200000`. This is the only way to automate the GPU part.

### What needs a person (on the Spark)

1. Build from the parent folder: `docker build -f omop-duck-db/Dockerfile.dgx -t omop-duck-db:dgx .`
2. `docker run --rm --gpus all omop-duck-db:dgx gpu-check`: expect the GB10 name and capability (12, 1).
3. `docker run --rm --gpus all --ipc=host omop-duck-db:dgx dgx-smoke --rows 200000`: record the time, device and AUROCs.
4. Run a few minutes of one real training job from `pipeline/` on a real tensors file, watching `nvidia-smi`.
5. Pin the NGC tag that worked in the Dockerfile and record the outputs in `status_and_roadmap`.

About one hour, with the parent project checked out on the device.

### Done when

The stand-in build and CPU smoke test run in CI on x86 and arm64, the NGC tag is pinned to one verified on the device, and the on-device outputs are recorded.

## 3. Post-0.11.1 features on non-synthetic data

### Where it stands

The five-production-database field test was at v0.11.0. Everything since was tested on synthetic data or golden fixtures (0.12.0's NEWS says "all written fresh with synthetic tests"). The features most at risk on real data:

| Release | Feature | Why real data could break it |
|---|---|---|
| 0.11.2 | `data_cutoff="auto"`, observation-period end checks, `cap_observation_period` | Real end-date pile-ups and future-dated records |
| 0.12.0 | Disclosure control, CONSORT closing, cross-site duplicates | Real small cells and skewed site sizes |
| 0.13.0 | Admission date bounds, `sample_stay_rule`, benchmark features, deep datasets, frozen vocabulary, leave-one-site-out | Concept 0, unmapped codes, very long stays, sparse labs |
| 0.14.0 | `FeatureSet`: RUCA, SDI, area features | Missing or 9-digit ZIPs, PO boxes, out-of-state addresses |
| 0.15.0 | `study_report` tables and figures, `[MASK]` token layout | Rare categories, all-missing columns |
| 0.17.0 to 0.20.0 | `run_study`, manuscript tables, decision presets, any number of sites | End-to-end on real attrition |
| 0.21.0 | Age in completed years, 12-hour time parsing, `etl_pcornet` full re-run, `load_vital` | Real PCORnet time strings and vitals |

### What can be automated in CI

- **MIMIC-IV demo in OMOP** (PhysioNet, open access, 100 real de-identified ICU patients). It is small, and its dates are shifted into the future, which is itself a good test of `data_cutoff="auto"`. Add a weekly scheduled job (plus `workflow_dispatch`) that downloads and caches it, builds the DuckDB, and runs: the observation-period checks, `define_study_cohort` with each `sample_stay_rule`, the benchmark and patient features, `extract_multimodal_deep_dataset`, `FeatureSet` with whatever location data exists, `run_study` on a split into pseudo-sites, and the DQD catalog. Assert structure and invariants (no duplicated stays, attrition closes, no suppressed cell recoverable, no all-NaN feature columns), not exact numbers. Download at run time rather than committing the data, and check the licence terms first.
- **PCORnet edge cases** for the 0.21.0 ETL changes: if no open real PCORnet extract exists, add a fixture of real-looking cases taken from field reports (12-hour times, `%Y%m%d` dates, re-runs), with no patient data.
- **Invariant checks** that work on any data, run in the same job: `assert_disclosure_safe` on every exported table, every cohort `person_id` present in `person`, age consistent with birth and admission dates.

### What needs a person

- A **field-check script** (new; suggested name `scripts/field_check.py`) that a site runs against its own CDM. It runs the same steps as the MIMIC job and writes only aggregate, small-cell-suppressed output: row counts, attrition, timings, warnings raised, feature completeness, and exceptions with data values scrubbed. Each site sends that file back; no patient rows leave the site.
- Needs: at least one, ideally two or more, of the 0.11.0 field-test sites; someone there to run it; and the site's approval to share aggregates.
- Compare each site's output with its 0.11.0 numbers where the feature existed then, so changes like completed-years age show up as expected differences rather than surprises.

### Done when

The MIMIC demo job runs weekly and green, at least one site has returned a field-check report, and each failure has an issue.

## Open questions for Kyle

- Is there a site Oracle you can get a test schema on, and which version is it?
- Is the Spark available for a one-hour session, and would you want it as a self-hosted runner?
- Which of the 0.11.0 field-test sites would run a field-check script?
