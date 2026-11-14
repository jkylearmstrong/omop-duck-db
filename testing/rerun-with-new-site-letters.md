# Rerun a study with new random site letters

For re-issuing results to reviewers without letting them line up the old and new site letters. Run it on the machine that holds the site databases. Uses `run_study` / `scripts/reproduce_study.py`. Nothing here should be committed, and the mapping must never be.

Placeholders: `<REPO>` (your clone), `<DB_DIR>` (folder with `omop_<site>.duckdb` and `central_vocabulary.duckdb`), `<site1> … <siteN>` (the `<site>` parts of those file names), `<PRIVATE>` (a folder outside any git repository, not synced, and apart from anything you share), plus the pinned-stays, RUCA and SDI files of the earlier run.

## 1. Make the mapping once

```powershell
cd <REPO>
python -c "import sys; sys.path.insert(0,'python'); from omop_etl import assign_site_labels, save_site_labels; m = assign_site_labels(['<site1>','<site2>','<site3>'], mode='random'); save_site_labels(m, r'<PRIVATE>\site_labels.json'); print('saved', len(m), 'labels')"
```

- No seed is used, so the mapping exists only in that JSON file and cannot be regenerated. Keep a second copy on a different disk.
- The command does not print the mapping. The command line holds the real names, so clear your shell history afterwards (PowerShell: `Remove-Item (Get-PSReadLineOption).HistorySavePath`).
- Open the JSON in an editor and compare it with the letters in the previous outputs. If more than one site keeps its old letter, delete the file and make a new one. State this rule before you look at any results, and use it only for this reason. With five sites about 27% of draws fail it.

## 2a. Relabel the existing run (no recompute; the numbers cannot move)

If the earlier run's output folder and its mapping file are still at hand, swap the letters instead of re-running:

```powershell
python scripts/relabel_study.py `
  --run-dir <PRIVATE>\run_old_labels --out <PRIVATE>\run_new_labels `
  --old-labels <PRIVATE>\site_labels_old.json --new-labels <PRIVATE>\site_labels.json `
  --names-file <PRIVATE>\site_names.txt
```

- It rewrites every label in the workbook (cells and sheet names), the CSV / JSON / Markdown / HTML outputs and the run record in one pass (Site A to B and B to A together is fine), rebuilds `<prefix>_Manuscript.docx` / `.pdf` from the relabelled workbook, and scans the new folder for the names. Exit code 1 on a hit.
- Instead of making the file in step 1 you can pass `--new-labels random --labels-out <PRIVATE>\site_labels.json`.
- Images are copied unchanged (the package's figures carry no site label) and listed, so check any figure you added yourself.
- R users: `relabel_sites()` relabels a data frame or text and `relabel_output_files()` the text outputs; the workbook and manuscript need the Python command.

## 2b. Or run the study with that mapping

```powershell
python scripts/reproduce_study.py `
  --db-dir <DB_DIR> --out <PRIVATE>\run_new_labels `
  --sites <site1> <site2> <site3> `
  --labels <PRIVATE>\site_labels.json `
  --pinned-stays <pinned_stays.csv> --ruca <ruca.csv> --sdi <sdi.csv> `
  --preset <preset> --age-method <method>
```

- Pass the JSON file to `--labels`, not `--labels random`, so every sheet and any later rerun uses the same letters.
- `python scripts/reproduce_study.py --list-decisions` lists the choices behind `--preset` and `--age-method`. Use the same ones as the run you are re-issuing.
- Leave out `--s1-s3-labels` unless the earlier workbook really used a different site order in Tables S1 and S3.
- Comparing with the earlier workbook (`--compare-to`) will show every site-labelled cell as different because the letters moved. That is expected and does not mean the numbers changed.
- Add `--names-file <PRIVATE>\site_names.txt`: the run then scans its own outputs, including the DOCX / PDF manuscript, and exits with code 1 (without writing the manuscript into the output folder) if a real name is found.
- Every site must have a label. A run with a site missing from the mapping, or with fewer `--labels` than `--sites`, stops with an error (in 0.24.0 and earlier that site silently appeared under its own name).

## 3. Before anything leaves the machine

Put the real site names, one per line, in a text file in `<PRIVATE>` and scan the output folder:

```powershell
python scripts/check_run_privacy.py --dir <PRIVATE>\run_new_labels --names-file <PRIVATE>\site_names.txt
```

- The workbook, tables, manuscript and report use the anonymous letters. The per-site intermediates do not: `features_<site>.parquet`, `cohort_<site>.parquet` and `attrition_<site>.csv` carry the real name in the file name, and in the `site_source` column and attrition `site` column. From the release after 0.24.0 they are written to a separate private folder (`--private-dir`, default `<out>_private`), so the output folder can be shared as a whole; never share the private folder. Older runs keep them in the output folder: share only the workbook, the `<prefix>_*.csv` / `.png` outputs and the summary.
- Releases before the one that added `check_run_privacy.py` also keyed `timings_seconds` in `<prefix>_run.json` by real site name (`site_<site>`), and those timings follow site size. From this release on they are keyed by the anonymous letter. With an older release, do not share the run record.
- The scan reports hits by the position of the name in your list, so its output can be pasted into an issue. It does not scan images.
- Exact per-site counts (Table S1, leave-one-site-out test N, CONSORT by site) can identify a site whatever its letter. Keep them in the private workbook, and use ranges in issues, NEWS and the repository.
- Tell reviewers that the letters changed so nobody lines up an old Site C with the new one. Send the old-to-new correspondence only to people who are already unblinded.
