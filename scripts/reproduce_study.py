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
from omop_etl.study_run import run_study  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-dir", required=True, help="folder with omop_<site>.duckdb (and central_vocabulary.duckdb)")
    p.add_argument("--out", required=True)
    p.add_argument("--sites", nargs="+", required=True)
    p.add_argument("--labels", nargs="*", help="anonymous label per site, same order (default Site A, B, ...)")
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
    p.add_argument("--attrition", default="windowed", choices=["windowed", "full_history"], help="CONSORT layout (full_history = the manuscript's)")
    p.add_argument("--table5", default="manuscript", choices=["manuscript", "calibrated"])
    p.add_argument("--no-sdi-missing-term", action="store_true", help="leave the SDI-missing term out of Table 6 (as the manuscript did)")
    p.add_argument("--tie-break", default="deterministic", choices=["deterministic", "highest_zip", "ambiguous_missing", "arbitrary"])
    a = p.parse_args(argv)
    labels = dict(zip(a.sites, a.labels)) if a.labels else None
    r = run_study(a.db_dir, a.out, sites=a.sites, site_labels=labels, start=a.start, end=a.end, profile=None if a.profile == "none" else a.profile,
                  pinned_stays=a.pinned_stays, ruca_crosswalk=a.ruca, sdi_table=a.sdi, address_table=a.address_table, n_bootstraps=a.bootstraps,
                  loso=not a.no_loso, figures=not a.no_figures, reuse_features=a.reuse_features, compare_to=a.compare_to, prefix=a.prefix,
                  attrition_mode=a.attrition, table5=a.table5, sdi_missing_term=not a.no_sdi_missing_term, address_tie_break=a.tie_break)
    print(f"N = {len(r['data']):,}; workbook: {r['workbook']}; seconds: {r['timings']}")
    if r["comparison"] is not None:
        print(r["comparison"][0].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
