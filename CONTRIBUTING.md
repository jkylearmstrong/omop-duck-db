# Contributing

## Avoid duplicate work

Several people (and AI sessions) often work from the same review report or issue list. Before starting anything that will take more than a few minutes:

1. **Look for overlap.** Run `gh pr list --state all`, `git log origin/main -15` and `git worktree list`. A report from the last few days is likely already being worked on.
2. **Claim the work.** Open a draft PR (or an issue) in the first minutes. Put the report or finding IDs in the title, for example `[review-0.20.0 C-01..C-10] ...`. If a report is shared, split it by finding ID so no two people hold the same items.
3. **Merge `main` often.** `git fetch origin main && git merge FETCH_HEAD` at least once an hour on a long branch, so a conflicting change shows up early rather than as 29 conflicted files at the end.
4. **Keep PRs small.** One concern per PR (fixes, a breaking default change, docs, new checks). Small PRs conflict less and are easy to drop or reorder.

## Before you open a PR

- Run both suites: Python `PYTHONPATH=python python -m pytest tests -q`, R `devtools::test()`.
- Add a `NEWS.md` entry; flag breaking changes as **Breaking**.
- Changes to shared SQL (`inst/sql/`) or the DDL must be checked in both languages.

## Releases

A release is a version bump (`DESCRIPTION`, `pyproject.toml`, `python/omop_etl/__init__.py`, `python/omop_duck_db/__init__.py`), a merged PR, then a `v*` tag pushed from `main`. The tag triggers the Docker workflow, which runs both test suites and publishes the image.
