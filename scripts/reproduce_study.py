"""Command line for omop_etl.run_study: per-site OMOP DuckDB files -> cohort, features, tables, figures, manuscript workbook.

Example (the multi-site eDERRI study, pinned to an earlier draw and compared with the previous deliverable):

    python scripts/reproduce_study.py --db-dir derived/omop_duckdb --out out --sites SiteA SiteB SiteC SiteD SiteE \
        --ruca aws_data/supplementaryData/ruca_mapping.csv --sdi aws_data/supplementaryData/SDI_Data/rgcsdi-2015-2019-zcta.csv \
        --address-table ederri_address_history --pinned-stays derived/grant_scoped_cohort_random/selected_stays_seed20261104.csv \
        --compare-to deliverables/eDERRI_Traditional_ML_Models_Manuscript_Tables.xlsx
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.data_gaps import DataGapError, format_findings  # noqa: E402
from omop_etl.study_run import STUDY_DECISIONS, STUDY_PRESETS, RunPrivacyError, run_study  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-dir", required=False, help="folder with omop_<site>.duckdb (and central_vocabulary.duckdb)")
    p.add_argument("--out", required=False)
    p.add_argument("--sites", nargs="+", required=False)
    p.add_argument("--labels", nargs="*", help="anonymous label per site, same order (default Site A, B, ...); or one word: random, ordered, or a JSON file of site -> label")
    p.add_argument("--label-seed", type=int, help="seed for --labels random (repeatable assignment)")
    p.add_argument("--labels-out", help="save the site -> label mapping here (keep it out of the repository and the shared results)")
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2022-09-30")
    p.add_argument("--profile", default="ederri", choices=["ederri", "both", "none"])
    p.add_argument("--pinned-stays", help="CSV with site_source and visit_occurrence_id of the stays to use")
    p.add_argument("--ruca"); p.add_argument("--sdi")
    p.add_argument("--address-table", default="auto",
                   help="dated address table for SDI / RUCA. 'auto' (default) uses the one non-empty table whose name contains 'address' and "
                        "that has person_id, zip5, address_start, address_end, preferred, and stops with the candidates if there are none or several; "
                        "'person_location' uses person.location_id")
    p.add_argument("--allow-data-gaps", nargs="+", default=[], metavar="CHECK",
                   help="accept these data gaps instead of failing (a check such as sdoh, address_table, vocabulary, pinned, empty_site, "
                        "group_missing, or a feature group such as labs; 'all' accepts every one). Accepted gaps are still written to <prefix>_run.json")
    p.add_argument("--check-data", action="store_true",
                   help="build every site, report every data gap of every site in one pass, and stop before modelling (exit 0 when clean, 3 otherwise)")
    p.add_argument("--data-gap-threshold", type=float, default=0.95,
                   help="a feature group missing for at least this share of a site's stays is a data gap (default 0.95)")
    p.add_argument("--bootstraps", type=int, default=100)
    p.add_argument("--no-loso", action="store_true"); p.add_argument("--no-figures", action="store_true")
    p.add_argument("--reuse-features", action="store_true")
    p.add_argument("--compare-to", help="previous manuscript workbook to compare with")
    p.add_argument("--prefix", default="Study")
    p.add_argument("--preset", default="package", choices=sorted(STUDY_PRESETS),
                   help="named set of decisions; the flags below override it. delivered_2026_11_11 recreates the workbook sent to Dr. Rubin")
    p.add_argument("--attrition", choices=["windowed", "full_history"], help="CONSORT layout (full_history = the manuscript layout)")
    p.add_argument("--table5", choices=["manuscript", "calibrated"])
    p.add_argument("--sdi-missing-term", dest="sdi_missing_term", action="store_true", default=None, help="keep the SDI-missing term in Table 6")
    p.add_argument("--no-sdi-missing-term", dest="sdi_missing_term", action="store_false", help="leave it out (as the delivered workbook did)")
    p.add_argument("--consort-final-steps", dest="consort_final_steps", action="store_true", default=None, help="Table S3 ends with the window / one-stay and pinned-list steps")
    p.add_argument("--no-consort-final-steps", dest="consort_final_steps", action="store_false", help="Table S3 ends with the last eligibility rule (as delivered)")
    p.add_argument("--s1-s3-labels", nargs="*", help="labels for Tables S1 and S3 only, same order as --sites (the delivered workbook uses a different order than Table 5)")
    p.add_argument("--table6-increments", choices=["sd", "clinical"], help="continuous Table 6 rows per 1 SD or per clinical unit")
    p.add_argument("--table6-missing", choices=["indicator", "median"],
                   help="continuous Table 6 measures with missing values: a 'not measured' term next to each (indicator, the package preset) or median imputation only (median, as delivered)")
    p.add_argument("--last-lab-values", choices=["drop", "keep"],
                   help="latest in-stay lab values as model features: drop (both presets; index-stay means only) or keep")
    p.add_argument("--tie-break", choices=["deterministic", "highest_zip", "ambiguous_missing", "arbitrary", "first_listed"])
    p.add_argument("--address-undated", choices=["last", "first"], help="rank of an address with no start date")
    p.add_argument("--concept-sets", choices=list(STUDY_DECISIONS["concept_sets"]["options"]),
                   help="lab and vital definitions: bundled (this release), legacy_2026_11_11 (the pre-0.29.0 LOINC lists; the delivered_2026_11_11 preset pins them) or an exact bundled version")
    p.add_argument("--age-method", choices=["completed_years", "year_difference"], help="age calculation method in cohort definition")
    p.add_argument("--private-dir", help="folder for the per-site intermediates that carry site names (default <out>_private; never share it)")
    p.add_argument("--no-document", action="store_true", help="skip <prefix>_Manuscript.docx / .pdf (the workbook is always written)")
    p.add_argument("--document-formats", nargs="+", default=["docx", "pdf"], choices=["docx", "pdf"])
    p.add_argument("--quarto-cache-dir", help="writable folder for Quarto's cache, for sandboxes where the user's cache folder is read only "
                   "(default: OMOP_QUARTO_CACHE_DIR, else Quarto's own; a permission error is retried once with a temporary cache)")
    p.add_argument("--doc-title", help="manuscript title (default '<prefix> manuscript')")
    p.add_argument("--doc-author", default="", help="author shown in the manuscript and its file properties (default none)")
    p.add_argument("--names-file", help="text file, one real site name per line (kept outside the repository): the outputs are scanned for them")
    p.add_argument("--names", nargs="*", default=[], help="real site names to scan for (they end up in shell history; prefer --names-file)")
    p.add_argument("--no-privacy-check", action="store_true", help="skip the scan of the outputs for site names")
    p.add_argument("--manuscript-profile", choices=["full", "tables_only"], default=None,
                   help="full (default): the manuscript text from the package's versioned template; tables_only: every table under its sheet name")
    p.add_argument("--manuscript-params", type=Path, default=None,
                   help="JSON or YAML file with the study team's title, authors and review-board statement (kept outside the package)")
    p.add_argument("--list-decisions", action="store_true", help="print every decision, its options and the presets, then exit")
    a = p.parse_args(argv)
    if a.list_decisions:
        for k, d in STUDY_DECISIONS.items():
            print(f"{k}: {d['options']}\n    {d['about']}\n    " + ", ".join(f"{n}={v[k]!r}" for n, v in STUDY_PRESETS.items()))
        return 0
    if not (a.db_dir and a.out and a.sites):
        p.error("--db-dir, --out and --sites are required")
    if a.labels and len(a.labels) == 1 and len(a.sites) != 1:
        labels = a.labels[0]
    elif a.labels and len(a.labels) != len(a.sites):
        p.error(f"--labels has {len(a.labels)} label(s) for {len(a.sites)} site(s); give one per site in the same order, or a JSON mapping file, or 'random'")
    else:
        labels = dict(zip(a.sites, a.labels)) if a.labels else None
    names = list(a.names)
    if a.names_file:
        from omop_etl.run_privacy import read_names_file

        names += read_names_file(a.names_file)
    try:
        r = run_study(a.db_dir, a.out, sites=a.sites, site_labels=labels, label_seed=a.label_seed, labels_out=a.labels_out, start=a.start, end=a.end, profile=None if a.profile == "none" else a.profile,
                      pinned_stays=a.pinned_stays, ruca_crosswalk=a.ruca, sdi_table=a.sdi,
                      address_table=None if a.address_table == "person_location" else a.address_table, n_bootstraps=a.bootstraps,
                      loso=not a.no_loso, figures=not a.no_figures, reuse_features=a.reuse_features, compare_to=a.compare_to, prefix=a.prefix,
                      attrition_mode=a.attrition, table5=a.table5, sdi_missing_term=a.sdi_missing_term, address_tie_break=a.tie_break, address_undated=a.address_undated,
                      table6_increments=a.table6_increments, consort_final_steps=a.consort_final_steps, age_method=a.age_method, concept_sets=a.concept_sets, table6_missing=a.table6_missing, last_lab_values=a.last_lab_values,
                      sheet_site_labels=({"Table S1": dict(zip(labels.values(), a.s1_s3_labels)), "Table S3": dict(zip(labels.values(), a.s1_s3_labels))}
                                         if a.s1_s3_labels and isinstance(labels, dict) else None), preset=a.preset,
                      manuscript_profile=a.manuscript_profile, manuscript_params=a.manuscript_params,
                      private_dir=a.private_dir, document=not a.no_document, document_formats=a.document_formats, document_title=a.doc_title,
                      document_author=a.doc_author, site_names=names, privacy_check=not a.no_privacy_check,
                      document_cache_dir=a.quarto_cache_dir, allow_data_gaps=a.allow_data_gaps, data_gap_threshold=a.data_gap_threshold, check_only=a.check_data,
                      run_args={k: v for k, v in vars(a).items() if k != "names"})
    except RunPrivacyError as e:
        print(f"PRIVACY CHECK FAILED: {e}", file=sys.stderr)
        return 1
    except DataGapError as e:
        print(f"DATA GAP CHECK FAILED (nothing was modelled): {e}", file=sys.stderr)
        return 3
    if r.get("checked_only"):
        print("data check passed: no data gap" + (" beyond those accepted" if any(f.get("allowed") for f in r["data_checks"]) else ""))
        print("address table used: " + (", ".join(f"{k}: {v}" for k, v in sorted(r["address_table"].items())) or "none (SDI / RUCA off)"))
        if r["data_checks"]:
            print(format_findings(r["data_checks"]), file=sys.stderr)
        return 0
    print(f"N = {len(r['data']):,}; workbook: {r['workbook']}; seconds: {r['timings']}")
    doc = r["document"]
    if doc.get("requested"):
        print(f"manuscript: {', '.join(doc.get('files', []))}" + (f"; not rendered: {', '.join(doc['missing'])} ({doc['note']})" if doc.get("missing") else ""))
    pc = r["privacy_check"]
    print(f"privacy check: {pc['hits']} hit(s) for {pc['n_names']} name(s)" if pc.get("checked") else "privacy check: no names to scan for (give --names-file)")
    print(f"per-site intermediates (private, do not share): {r['private_dir']}")
    print("address table used: " + (", ".join(f"{k}: {v}" for k, v in sorted(r["address_table"].items())) or "none (SDI / RUCA off)"))
    gaps = [f for f in r["data_checks"] if f.get("severity") == "error"]
    warns = [f for f in r["data_checks"] if f.get("severity") == "warning"]
    if gaps:
        print(f"DATA GAPS ACCEPTED with --allow-data-gaps ({len(gaps)}):\n" + format_findings(gaps), file=sys.stderr)
    if warns:
        print(f"data warnings ({len(warns)}), also in the run record:\n" + format_findings(warns), file=sys.stderr)
    if r["comparison"] is not None:
        print(r["comparison"][0].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
