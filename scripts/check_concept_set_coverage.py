#!/usr/bin/env python3
"""Which LOINC codes a site's in-stay rows use for each lab that its concept set does not hold (read-only).

Prints shares only (no counts). With --expand the bundled lab sets are first expanded from --vocab-db (nothing is written).

Example:
    python scripts/check_concept_set_coverage.py --db omop_site.duckdb --cohort cohort_site.parquet --expand --vocab-db omop_site.duckdb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from omop_etl import concept_sets as cs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="a site DuckDB (opened read-only)")
    ap.add_argument("--cohort", help="a cohort parquet (subject_id or person_id, cohort_start_date, cohort_end_date); "
                                     "without it every measurement row counts")
    ap.add_argument("--expand", action="store_true", help="expand the bundled lab sets from the vocabulary first")
    ap.add_argument("--vocab-db", help="DuckDB to expand from (default: --db)")
    ap.add_argument("--min-share", type=float, default=0.005)
    ap.add_argument("--labs", nargs="*", help="only these labs")
    a = ap.parse_args(argv)

    sets = cs.load_concept_sets("labs")
    if a.labs:
        sets = {k: v for k, v in sets.items() if k in a.labs}
    if a.expand:
        vcon = duckdb.connect(a.vocab_db or a.db, read_only=True)
        sets = {k: cs.expand_loinc_concept_set(vcon, v) for k, v in sets.items()}
        vcon.close()
    con = duckdb.connect(a.db, read_only=True)
    cohort = None
    if a.cohort:
        cohort = pd.read_parquet(a.cohort)
        if "subject_id" not in cohort and "person_id" in cohort:
            cohort = cohort.rename(columns={"person_id": "subject_id"})
    miss = cs.loinc_coverage(con, sets, cohort=cohort, min_share=a.min_share)
    if miss is None:
        print("the vocabulary has no LOINC part links: coverage cannot be checked")
        return 1
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 70)
    print("codes outside the sets (share of the analyte's rows):")
    print(miss.assign(share=miss["share"].map("{:.1%}".format)).to_string(index=False) if len(miss) else "    none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
