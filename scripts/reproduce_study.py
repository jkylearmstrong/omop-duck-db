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
from omop_etl.study_run import STUDY_DECISIONS, STUDY_PRESETS, run_study  # noqa: E402


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
    p.add_argument("--ruca"); p.add_argument("--sdi"); p.add_argument("--address-table", default="auto")
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
    p.add_argument("--tie-break", choices=["deterministic", "highest_zip", "ambiguous_missing", "arbitrary", "first_listed"])
    p.add_argument("--address-undated", choices=["last", "first"], help="rank of an address with no start date")
    p.add_argument("--age-method", choices=["completed_years", "year_difference"], help="age calculation method in cohort definition")
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
    else:
        labels = dict(zip(a.sites, a.labels)) if a.labels else None
    r = run_study(a.db_dir, a.out, sites=a.sites, site_labels=labels, label_seed=a.label_seed, labels_out=a.labels_out, start=a.start, end=a.end, profile=None if a.profile == "none" else a.profile,
                  pinned_stays=a.pinned_stays, ruca_crosswalk=a.ruca, sdi_table=a.sdi, address_table=a.address_table, n_bootstraps=a.bootstraps,
                  loso=not a.no_loso, figures=not a.no_figures, reuse_features=a.reuse_features, compare_to=a.compare_to, prefix=a.prefix,
                  attrition_mode=a.attrition, table5=a.table5, sdi_missing_term=a.sdi_missing_term, address_tie_break=a.tie_break, address_undated=a.address_undated,
                  table6_increments=a.table6_increments, consort_final_steps=a.consort_final_steps, age_method=a.age_method,
                  sheet_site_labels=({"Table S1": dict(zip(labels.values(), a.s1_s3_labels)), "Table S3": dict(zip(labels.values(), a.s1_s3_labels))}
                                     if a.s1_s3_labels and isinstance(labels, dict) else None), preset=a.preset)
    print(f"N = {len(r['data']):,}; workbook: {r['workbook']}; seconds: {r['timings']}")
    if r["comparison"] is not None:
        print(r["comparison"][0].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
