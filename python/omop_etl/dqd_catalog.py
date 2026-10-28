"""Schema-driven DataQualityDashboard check catalog, executed in DuckDB SQL.

Where ``omop_etl.dqd.run_dqd`` runs a hand-picked set of checks, ``run_dqd_catalog`` generates the checks from the CDM
DDL (``load_cdm_schema``) so every table and field of the CDM is covered, one batched scan per table:

* ``cdmTable`` / ``cdmField``      - the table / column exists (conformance)
* ``isRequired``                   - NOT NULL columns hold no NULL (conformance)
* ``isPrimaryKey``                 - ``<table>_id`` is unique (conformance)
* ``isForeignKey``                 - ids resolve in person / visit / provider / care_site / location / concept (conformance)
* ``measureValueCompleteness``     - share of NULLs per field (completeness)
* ``standardConceptRecordCompleteness`` - share of ``*_concept_id = 0`` per field (completeness)
* ``plausibleStartBeforeEnd``, ``plausibleAfterBirth``, ``plausibleBeforeDeath``, ``plausibleFutureDate`` (plausibility)

Results use the OHDSI DQD JSON shape (Metadata / Overview / CheckResults) with ``category`` and ``context`` (Kahn
framework) added. This is a generated structural/temporal catalog, not the full ~4000-check OHDSI list: concept-level
checks that need a vocabulary (``plausibleGender``, ``sourceConceptRecordCompleteness`` by concept set, ``fkDomain``/``fkClass``,
``plausibleValueLow/High`` by concept) are out of scope here; ``sanitize_measurements`` covers physiologic limits.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Sequence

import duckdb

from omop_etl.dqd import run_dqd
from omop_etl.export_targets import load_cdm_schema
from omop_etl.jsonio import strict_dumps

__all__ = ["run_dqd_catalog", "run_dqd_full", "DQD_CATALOG_CHECKS"]

DQD_CATALOG_CHECKS = ("cdmTable", "cdmField", "isRequired", "isPrimaryKey", "isForeignKey", "measureValueCompleteness",
                      "standardConceptRecordCompleteness", "plausibleStartBeforeEnd", "plausibleAfterBirth",
                      "plausibleBeforeDeath", "plausibleFutureDate")
_KAHN = {"cdmTable": ("Conformance", "Verification"), "cdmField": ("Conformance", "Verification"),
         "isRequired": ("Conformance", "Verification"), "isPrimaryKey": ("Conformance", "Verification"),
         "isForeignKey": ("Conformance", "Verification"), "measureValueCompleteness": ("Completeness", "Verification"),
         "standardConceptRecordCompleteness": ("Completeness", "Verification"),
         "plausibleStartBeforeEnd": ("Plausibility", "Validation"), "plausibleAfterBirth": ("Plausibility", "Validation"),
         "plausibleBeforeDeath": ("Plausibility", "Validation"), "plausibleFutureDate": ("Plausibility", "Validation")}
_FK = {"person_id": "person", "visit_occurrence_id": "visit_occurrence", "preceding_visit_occurrence_id": "visit_occurrence",
       "visit_detail_id": "visit_detail", "provider_id": "provider", "care_site_id": "care_site", "location_id": "location"}
_VOCAB = {"concept", "vocabulary", "domain", "concept_class", "relationship", "concept_relationship", "concept_ancestor",
          "concept_synonym", "drug_strength", "source_to_concept_map", "cdm_source", "metadata", "cohort_definition"}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def run_dqd_catalog(con: duckdb.DuckDBPyConnection, output_json: str | Path | None = None, *, cdm_version: str = "5.4",
                    tables: Sequence[str] | None = None, threshold_pct: float = 0.0,
                    value_completeness_threshold_pct: float = 100.0, zero_concept_threshold_pct: float = 10.0,
                    death_grace_days: int = 60, today: datetime.date | None = None) -> dict[str, Any]:
    """Generates and runs the catalog checks. A check fails when its violating share exceeds its threshold
    (``threshold_pct`` for conformance and plausibility; ``value_completeness_threshold_pct`` fails a field at or above
    that NULL share, so the default flags fully-empty fields; ``zero_concept_threshold_pct`` for unmapped concepts)."""
    schema = load_cdm_schema()
    want = {t.lower() for t in tables} if tables else None
    today = today or datetime.date.today()
    info = con.execute("SELECT lower(table_name), lower(column_name), upper(data_type) FROM information_schema.columns "
                       "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')").fetchall()
    present: dict[str, dict[str, str]] = {}
    for t, c, d in info:
        present.setdefault(t, {})[c] = d
    results: list[dict[str, Any]] = []

    def add(name, tbl, fld, desc, viol, denom, thr, level="FIELD", cmp="gt"):
        if denom == 0:
            status, failed, pct = "NOT_APPLICABLE", False, 0.0
        else:
            pct = 100.0 * viol / denom
            failed = pct >= thr if cmp == "ge" else pct > thr
            status = "FAILED" if failed else "PASSED"
        cat, ctx = _KAHN[name]
        results.append({"check_id": f"CAT_{name}_{tbl}_{fld or 'TABLE'}".upper(), "check_name": name, "check_level": level,
                        "category": cat, "context": ctx, "cdm_table_name": tbl, "cdm_field_name": fld,
                        "check_description": desc, "num_violated_rows": int(viol), "pct_violated_rows": round(pct, 4),
                        "num_denominator_rows": int(denom), "threshold_value": thr, "execution_status": status, "failed": failed})

    concept_rows = con.execute("SELECT count(*) FROM concept").fetchone()[0] if "concept" in present else 0
    death_ok = "death" in present and {"person_id", "death_date"} <= set(present["death"])
    person_ok = "person" in present and "year_of_birth" in present["person"]
    for tbl, cols in schema.items():
        if want and tbl not in want:
            continue
        exists = tbl in present
        add("cdmTable", tbl, None, f"Table {tbl} exists in the database.", 0 if exists else 1, 1, 0.0, level="TABLE")
        if not exists:
            continue
        have = present[tbl]
        for c in cols:
            add("cdmField", tbl, c.name, f"Column {tbl}.{c.name} exists.", 0 if c.name in have else 1, 1, 0.0)
        usable = [c for c in cols if c.name in have]
        n = con.execute(f"SELECT count(*) FROM {_q(tbl)}").fetchone()[0]
        aggs = []
        for c in usable:
            aggs.append(f"count(*) FILTER (WHERE {_q(c.name)} IS NULL)")
            aggs.append(f"count(*) FILTER (WHERE {_q(c.name)} = 0)" if c.name.endswith("_concept_id") else "0")
        row = con.execute(f"SELECT {', '.join(aggs)} FROM {_q(tbl)}").fetchone() if usable else ()
        pk = f"{tbl}_id"
        for i, c in enumerate(usable):
            nulls, zeros = row[2 * i], row[2 * i + 1]
            if not c.nullable:
                add("isRequired", tbl, c.name, f"{tbl}.{c.name} is NOT NULL in the CDM.", nulls, n, threshold_pct)
            add("measureValueCompleteness", tbl, c.name, f"Share of NULL values in {tbl}.{c.name}.", nulls, n,
                value_completeness_threshold_pct, cmp="ge")
            if c.name.endswith("_concept_id") and not c.name.endswith("source_concept_id"):
                add("standardConceptRecordCompleteness", tbl, c.name, f"Share of {tbl}.{c.name} = 0 (unmapped).", zeros, n,
                    zero_concept_threshold_pct)
            if c.name == pk and n:
                dup = con.execute(f"SELECT count(*) - count(DISTINCT {_q(pk)}) FROM {_q(tbl)}").fetchone()[0]
                add("isPrimaryKey", tbl, pk, f"{tbl}.{pk} is unique.", dup, n, threshold_pct)
            ref = _FK.get(c.name)
            if ref and ref != tbl and ref in present and n:
                rid = f"{ref}_id" if c.name != "preceding_visit_occurrence_id" else "visit_occurrence_id"
                if rid in present[ref]:
                    v = con.execute(f"SELECT count(*) FROM {_q(tbl)} t WHERE t.{_q(c.name)} IS NOT NULL AND NOT EXISTS "
                                    f"(SELECT 1 FROM {_q(ref)} r WHERE r.{_q(rid)} = t.{_q(c.name)})").fetchone()[0]
                    add("isForeignKey", tbl, c.name, f"{tbl}.{c.name} resolves in {ref}.{rid}.", v, n, threshold_pct)
            elif c.name.endswith("_concept_id") and concept_rows and tbl != "concept" and n:
                v = con.execute(f"SELECT count(*) FROM {_q(tbl)} t WHERE t.{_q(c.name)} IS NOT NULL AND t.{_q(c.name)} <> 0 AND NOT EXISTS "
                                f"(SELECT 1 FROM concept r WHERE r.concept_id = t.{_q(c.name)})").fetchone()[0]
                add("isForeignKey", tbl, c.name, f"{tbl}.{c.name} resolves in concept.concept_id.", v, n, threshold_pct)

        if tbl in _VOCAB or tbl in ("cohort", "person") or "person_id" not in have or not n:
            continue
        dates = [c.name for c in usable if have[c.name] == "DATE"]
        for sd in [d for d in dates if d.endswith("_start_date")]:
            ed = sd.replace("_start_date", "_end_date")
            if ed in dates:
                v = con.execute(f"SELECT count(*) FROM {_q(tbl)} WHERE {_q(ed)} < {_q(sd)}").fetchone()[0]
                nn = con.execute(f"SELECT count(*) FROM {_q(tbl)} WHERE {_q(ed)} IS NOT NULL AND {_q(sd)} IS NOT NULL").fetchone()[0]
                add("plausibleStartBeforeEnd", tbl, sd, f"{tbl}.{ed} is not before {sd}.", v, nn, threshold_pct)
        for d in [d for d in dates if not d.endswith("_end_date")]:
            nn = con.execute(f"SELECT count(*) FROM {_q(tbl)} WHERE {_q(d)} IS NOT NULL").fetchone()[0]
            if person_ok:
                v = con.execute(f"SELECT count(*) FROM {_q(tbl)} t JOIN person p ON p.person_id = t.person_id "
                                f"WHERE t.{_q(d)} IS NOT NULL AND p.year_of_birth IS NOT NULL AND year(t.{_q(d)}) < p.year_of_birth").fetchone()[0]
                add("plausibleAfterBirth", tbl, d, f"{tbl}.{d} is not before the person's birth year.", v, nn, threshold_pct)
            if death_ok and tbl != "death":
                v = con.execute(f"SELECT count(*) FROM {_q(tbl)} t JOIN (SELECT person_id, min(death_date) AS dd FROM death GROUP BY 1) x "
                                f"ON x.person_id = t.person_id WHERE t.{_q(d)} > x.dd + INTERVAL '{int(death_grace_days)}' DAY").fetchone()[0]
                add("plausibleBeforeDeath", tbl, d, f"{tbl}.{d} is within {int(death_grace_days)} days after death at most.", v, nn, threshold_pct)
            v = con.execute(f"SELECT count(*) FROM {_q(tbl)} WHERE {_q(d)} > DATE '{today.isoformat()}'").fetchone()[0]
            add("plausibleFutureDate", tbl, d, f"{tbl}.{d} is not after {today.isoformat()}.", v, nn, threshold_pct)

    return _package(results, cdm_version, output_json)


def _package(results: list[dict[str, Any]], cdm_version: str, output_json: str | Path | None) -> dict[str, Any]:
    applicable = [r for r in results if r["execution_status"] != "NOT_APPLICABLE"]
    failed = sum(1 for r in applicable if r["failed"])
    out = {"Metadata": {"cdm_version": cdm_version, "dqa_tool": "omop-duck-db native DQD catalog",
                        "execution_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()},
           "Overview": {"count_total": len(results), "count_not_applicable": len(results) - len(applicable),
                        "count_passed": len(applicable) - failed, "count_failed": failed,
                        "percent_passed": round(100.0 * (len(applicable) - failed) / len(applicable), 2) if applicable else 100.0},
           "CheckResults": results}
    if output_json is not None:
        p = Path(output_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(strict_dumps(out, indent=2), encoding="utf-8")
    return out


def run_dqd_full(con: duckdb.DuckDBPyConnection, output_json: str | Path | None = None, **catalog_kwargs: Any) -> dict[str, Any]:
    """``run_dqd`` (the hand-written checks) plus ``run_dqd_catalog`` in one DQD-shaped result."""
    base = run_dqd(con)
    cat = run_dqd_catalog(con, **catalog_kwargs)
    return _package(base["CheckResults"] + cat["CheckResults"], catalog_kwargs.get("cdm_version", "5.4"), output_json)
