#!/usr/bin/env python3
"""Expand the bundled lab (and vital) concept sets from a vocabulary, and compare them with a legacy code list.

Reads only the vocabulary tables (concept, concept_relationship) of a DuckDB opened read-only; no patient table is read,
so the output holds public LOINC codes and names only. For each set, every LOINC test sharing a listed code's component,
property, scale and time aspect with a blood, serum or plasma system is added (omop_etl.concept_sets.expand_loinc_concept_set).

Examples:
    # what would change (nothing is written)
    python scripts/build_concept_sets.py --db path/to/omop_site.duckdb
    # write the expanded files and set the manifest version
    python scripts/build_concept_sets.py --db path/to/omop_site.duckdb --write --version 2026.11.2
    # which of a legacy roll-up's LOINC codes each set holds (a CSV with a code column, and optionally a test-name column)
    python scripts/build_concept_sets.py --db path/to/omop_site.duckdb --legacy legacy_labs.csv --legacy-code-col LAB_LOINC
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import duckdb  # noqa: E402

from omop_etl import concept_sets as cs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="a DuckDB holding concept and concept_relationship (opened read-only)")
    ap.add_argument("--domain", action="append", choices=["labs", "vitals"], help="domain(s) to expand (default: labs)")
    ap.add_argument("--write", action="store_true", help="write the expanded files into inst/extdata/concept_sets")
    ap.add_argument("--version", help="with --write: the new manifest version (required when a file changes)")
    ap.add_argument("--legacy", help="a CSV of a legacy lab roll-up; reports which set holds each of its LOINC codes")
    ap.add_argument("--legacy-code-col", default="LAB_LOINC")
    ap.add_argument("--legacy-name-col", help="optional test-name column of the legacy CSV")
    a = ap.parse_args(argv)

    con = duckdb.connect(a.db, read_only=True)
    domains = a.domain or ["labs"]
    root = Path(__file__).resolve().parents[1] / "inst" / "extdata" / "concept_sets"
    changed = []
    sets_now: dict[str, dict] = {}
    for d in domains:
        names = cs.concept_set_manifest()["domains"][d] + [o.split("/")[1] for o in cs.concept_set_manifest().get("other", [])
                                                           if o.startswith(d + "/")]
        for n in names:
            data = cs.load_concept_set(f"{d}/{n}")
            new = cs.expand_loinc_concept_set(con, data["expression"], mode=data.get("expansion", "blood"),
                                              expand_from=data.get("expand_from"))
            sets_now[n] = new
            before, after = cs.concept_set_codes(data["expression"]), cs.concept_set_codes(new)
            added = [it["concept"] for it in new["items"] if it["concept"].get("CONCEPT_CODE") not in before]
            print(f"{d}/{n}: {len(before)} -> {len(after)} codes" + (f" (expansion: {data['expansion']})" if data.get("expansion") else ""))
            for c in added:
                print(f"    + {c['CONCEPT_CODE']}  {c.get('CONCEPT_NAME', '')}")
            if new != data["expression"]:
                changed.append((d, n, data, new))
    if a.legacy:
        legacy = con.execute(f"SELECT DISTINCT CAST({json.dumps(a.legacy_code_col)} AS VARCHAR) AS code"
                             + (f", CAST({json.dumps(a.legacy_name_col)} AS VARCHAR) AS name" if a.legacy_name_col else ", NULL AS name")
                             + " FROM read_csv(?, all_varchar = true) ORDER BY 1", [a.legacy]).fetchall()
        holder = {code: n for n, e in sets_now.items() for code in cs.concept_set_codes(e)}
        print("\nlegacy code -> concept set")
        for code, name in legacy:
            if code:
                print(f"    {code:<10} {holder.get(code, 'NOT IN ANY SET'):<22} {name or ''}")
    if a.write and changed:
        if not a.version:
            print("--write changes files: give --version", file=sys.stderr)
            return 2
        for d, n, data, new in changed:
            cs.write_concept_set(root / d / f"{n}.json", n, d, new, **{k: v for k, v in data.items() if k not in ("name", "domain", "expression")})
        m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        m["version"] = a.version
        (root / "manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {len(changed)} file(s); manifest version {a.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
