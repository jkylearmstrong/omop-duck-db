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

## 2. Run the study with that mapping

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

## 3. Before anything leaves the machine

Put the real site names, one per line, in a text file in `<PRIVATE>` and scan the output folder:

```powershell
python scripts/check_run_privacy.py --dir <PRIVATE>\run_new_labels --names-file <PRIVATE>\site_names.txt
```

- The workbook, tables and report use the anonymous letters. The per-site intermediates do not: `features_<site>.parquet`, `cohort_<site>.parquet` and `attrition_<site>.csv` carry the real name in the file name, and in the `site_source` column and attrition `site` column. Share only the workbook, the `<prefix>_*.csv` / `.png` outputs and the summary, not the per-site files.
- Releases before the one that added `check_run_privacy.py` also keyed `timings_seconds` in `<prefix>_run.json` by real site name (`site_<site>`), and those timings follow site size. From this release on they are keyed by the anonymous letter. With an older release, do not share the run record.
- The scan reports hits by the position of the name in your list, so its output can be pasted into an issue. It does not scan images.
- Exact per-site counts (Table S1, leave-one-site-out test N, CONSORT by site) can identify a site whatever its letter. Keep them in the private workbook, and use ranges in issues, NEWS and the repository.
- Tell reviewers that the letters changed so nobody lines up an old Site C with the new one. Send the old-to-new correspondence only to people who are already unblinded.
