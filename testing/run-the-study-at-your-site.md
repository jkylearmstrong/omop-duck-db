# Run the multi-site study at your site, anonymously

For a site that runs the readmission study on its own data, alone or with any number of sister sites (one, three, five, eight, thirty: nothing assumes five). Python runs the study; R has the same label, relabel and scan helpers for working with the outputs. Nothing here should be committed.

Placeholders: `<DB_DIR>` (folder with `omop_<site>.duckdb` per site), `<site1> … <siteN>` (the `<site>` parts of those names), `<PRIVATE>` (a folder outside any git repository, not synced, apart from anything you share).

## 1. One mapping for the whole consortium

The coordinating site makes the site-to-letter mapping once and sends each site the same file, so "Site C" means the same health system in every site's tables.

```python
from omop_etl import assign_site_labels, save_site_labels
save_site_labels(assign_site_labels(["<site1>", "<site2>", "<site3>", "<site4>"]), r"<PRIVATE>/site_labels.json")
```

R: `save_site_labels(assign_site_labels(c("<site1>", "<site2>", "<site3>", "<site4>")), "<PRIVATE>/site_labels.json")`. Same JSON format, so either language reads the other's file. A site can run with only its own databases: entries for sites not in the run are ignored. Every site in the run must have its own label, or the run stops (`validate_site_labels()` in R and Python).

## 2. Run

```bash
python scripts/reproduce_study.py --db-dir <DB_DIR> --out <PRIVATE>/share/run1 --sites <site1> <site2> \
    --labels <PRIVATE>/site_labels.json --names-file <PRIVATE>/site_names.txt
```

The output folder then holds only anonymous, shareable files:

- `<prefix>_Manuscript_Tables.xlsx`, `<prefix>_Manuscript.docx`, `<prefix>_Manuscript.pdf` (and the `.qmd` they are built from), all from the same tables and the same letters;
- `<prefix>_*.csv`, `<prefix>_*.png`, `<prefix>_summary.json`, `<prefix>_run.json` (no site names, no mapping).

The per-site intermediates that carry real names go to `<out>_private` (`--private-dir` to choose another place). Do not share that folder.

`--names-file` (one real site name per line) makes the run scan its own outputs, including the text and metadata of the DOCX and PDF; any hit stops the run with exit code 1 and the manuscript is not written into the output folder. The site keys are always added to the scan when they are at least three characters long. `--no-document` skips the DOCX / PDF. The DOCX / PDF need [Quarto](https://quarto.org) (it brings Typst, so no LaTeX); without it the run writes the `.qmd`, makes the DOCX with Pandoc if that is installed, and says in `<prefix>_run.json` what it could not render.

## 3. New letters later

`scripts/relabel_study.py` re-issues a finished run under a new mapping without recomputing: see `testing/rerun-with-new-site-letters.md`.

## 4. Before sharing anything else

`python scripts/check_run_privacy.py --dir <folder> --names-file <PRIVATE>/site_names.txt` (R: `check_run_privacy("<folder>", "<PRIVATE>/site_names.txt")`). Exact per-site counts can identify a site whatever its letter; this scan does not look for them.
