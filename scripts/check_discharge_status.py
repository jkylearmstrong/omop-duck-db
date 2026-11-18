#!/usr/bin/env python3
"""Where the discharge status of inpatient stays comes from, and how much of it is missing: a quick check of each site database.

For the inpatient and emergency-to-inpatient stays admitted in the study window, it counts the PCORnet DISCHARGE_STATUS source
values (trimmed, upper case), the OMOP ``discharged_to_concept_id`` values the ETL wrote, and the discharge-status class each stay
gets under the 0.33.0 rule (:func:`omop_etl.benchmark_features.ederri_disposition_level`: "other" and "missing" separate) and under
the rule of 0.31.0 / 0.32.0 (the manuscript's "Missing / Unknown" held every class outside home, home health, SNF/rehab and AMA, and
the source value was not trimmed). It reads one GROUP BY per site, so it takes seconds to a minute per site.

Two outputs, as in ``compare_with_delivered.py``:

* ``--answers``: JSON with pooled counts only (all sites together; counts under 11 shown as "<11"), which can be shared.
* ``--detail``: Markdown with the same tables per site, under the site's anonymous label; keep it with the data.

Example:
    python scripts/check_discharge_status.py --db-dir D:/omop --sites s1 s2 s3 --labels <PRIVATE>/labels.json \\
        --answers <PRIVATE>/discharge_answers.json --detail <PRIVATE>/discharge_detail.md --max-seconds 600
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.benchmark_features import _EDERRI_DISPOSITION_CONCEPTS, ederri_disposition_level  # noqa: E402

INPATIENT = (9201, 262)  # inpatient visit, emergency room and inpatient visit
MIN_CELL = 11
_OLD_SETS = {"home": {8536, 4161979}, "home_health": {8844, 4038234}, "snf": {8863, 8676, 8827, 4163901}, "ama": {4155309, 44814650}}


def old_rule_level(concept_id, raw_source) -> str:
    """The class before 0.33.0: the protocol's concept sets without the ETL's HH / RH / AM concepts, an untrimmed source value, and
    everything else "other" (printed as "Missing / Unknown" in the manuscript)."""
    c = -1 if concept_id is None or pd.isna(concept_id) else int(concept_id)
    sv = None if raw_source is None or (not isinstance(raw_source, str) and pd.isna(raw_source)) else str(raw_source).upper()
    if c in _OLD_SETS["home"] or sv in ("HO", "HOME"):
        return "home"
    if c in _OLD_SETS["home_health"] or sv in ("HH", "HOME HEALTH"):
        return "home_health"
    if c in _OLD_SETS["snf"] or sv in ("SN", "RH", "SNF", "REHAB"):
        return "snf"
    if c in _OLD_SETS["ama"] or sv in ("AM", "AMA"):
        return "ama"
    return "other (shown as Missing / Unknown)"


def site_counts(db: Path, start: str, end: str) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(
            "SELECT discharged_to_source_value AS raw_source, discharged_to_concept_id AS concept_id, count(*) AS n "
            "FROM visit_occurrence WHERE visit_concept_id IN (?, ?) AND visit_start_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) "
            "GROUP BY ALL", [*INPATIENT, start, end]).df()
    finally:
        con.close()


def summarise(c: pd.DataFrame) -> dict[str, pd.DataFrame]:
    c = c.copy()
    c["source"] = c["raw_source"].map(lambda v: "(null)" if v is None or (not isinstance(v, str) and pd.isna(v))
                                      else (v.strip().upper() or "(blank)"))
    c["padded"] = c["raw_source"].map(lambda v: isinstance(v, str) and v != v.strip())
    c["class_0330"] = [ederri_disposition_level(k, s) for k, s in zip(c["concept_id"], c["raw_source"])]
    c["class_before"] = [old_rule_level(k, s) for k, s in zip(c["concept_id"], c["raw_source"])]
    total = int(c["n"].sum())

    def tab(col: str) -> pd.DataFrame:
        t = c.groupby(col, dropna=False)["n"].sum().sort_values(ascending=False).reset_index()
        t["pct"] = (100 * t["n"] / total).round(2) if total else 0.0
        return t

    by = {"source value": tab("source"), "concept id": tab("concept_id"), "class (0.33.0)": tab("class_0330"),
          "class (0.31.0 / 0.32.0)": tab("class_before")}
    by["padded source values"] = pd.DataFrame({"n": [int(c.loc[c["padded"], "n"].sum())], "pct": [round(100 * c.loc[c["padded"], "n"].sum() / total, 2) if total else 0.0]})
    by["stays"] = pd.DataFrame({"n": [total]})
    return by


def _suppress(t: pd.DataFrame) -> list[dict]:
    rows = []
    for r in t.to_dict("records"):
        n = int(r["n"])
        rows.append({**{k: (None if pd.isna(v) else v) for k, v in r.items() if k not in ("n", "pct")},
                     "n": f"<{MIN_CELL}" if 0 < n < MIN_CELL else n, "pct": None if 0 < n < MIN_CELL else r.get("pct")})
    return rows


def _md(t: pd.DataFrame) -> str:
    cols = list(t.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for r in t.itertuples(index=False):
        lines.append("| " + " | ".join("" if pd.isna(v) else (f"<{MIN_CELL}" if c == "n" and 0 < int(v) < MIN_CELL else str(v))
                                       for c, v in zip(cols, r)) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db-dir", required=True, type=Path, help="folder with omop_<site>.duckdb")
    p.add_argument("--sites", nargs="+", required=True)
    p.add_argument("--labels", help="JSON file of site -> anonymous label (default Site A, B, ... in --sites order)")
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2022-09-30")
    p.add_argument("--answers", required=True, type=Path, help="pooled counts only (shareable)")
    p.add_argument("--detail", required=True, type=Path, help="per-site tables under anonymous labels (keep it private)")
    p.add_argument("--max-seconds", type=float, default=900, help="stop before the next site once this much time has passed")
    a = p.parse_args(argv)
    labels = json.loads(Path(a.labels).read_text(encoding="utf-8")) if a.labels else {s: f"Site {chr(65 + i)}" for i, s in enumerate(a.sites)}
    t0 = time.monotonic()
    parts, detail = [], [f"# Discharge status by site ({a.start} to {a.end}, inpatient and emergency-to-inpatient stays)", ""]
    for site in a.sites:
        if time.monotonic() - t0 > a.max_seconds:
            print(f"stopped: over {a.max_seconds:.0f} s before the next site", file=sys.stderr)
            return 2
        c = site_counts(a.db_dir / f"omop_{site}.duckdb", a.start, a.end)
        parts.append(c)
        detail += [f"## {labels.get(site, '(unlabelled site)')}", ""]
        for name, t in summarise(c).items():
            detail += [f"### {name}", "", _md(t), ""]
    pooled = summarise(pd.concat(parts, ignore_index=True).groupby(["raw_source", "concept_id"], dropna=False)["n"].sum().reset_index())
    answers = {"window": [a.start, a.end], "n_sites": len(a.sites), "seconds": round(time.monotonic() - t0, 1),
               "ederri_concepts_0330": {k: sorted(v) for k, v in _EDERRI_DISPOSITION_CONCEPTS.items()},
               **{k: _suppress(v) for k, v in pooled.items()}}
    a.answers.parent.mkdir(parents=True, exist_ok=True)
    a.answers.write_text(json.dumps(answers, indent=1, default=str), encoding="utf-8")
    a.detail.parent.mkdir(parents=True, exist_ok=True)
    a.detail.write_text("\n".join(detail), encoding="utf-8")
    print(f"done in {answers['seconds']} s: {a.answers} (shareable), {a.detail} (private)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
