"""Loud failures for silent data gaps in a study run.

A study run used to "pass" when a whole feature group was quietly empty: the address-history table was not found, so
every stay had ``sdoh_missing = 1``; the vocabulary was not attached, so descendant concepts were not resolved; a pinned
stay list did not match, so a site lost stays. Each of these only printed a warning that scrolled past, if that.

This module gives such gaps a name and a rule:

* :class:`DataGapWarning` is the warning library code raises for a gap it can detect where it happens (no ZIP for any
  stay, no ``concept_ancestor``, protocol history skipped). Outside a study run it is an ordinary ``UserWarning``.
* :func:`feature_coverage` looks at one site's feature table after the fact: a feature group that is missing (or all zero)
  for at least ``threshold`` of the stays, a group that was skipped, a column with no value at all.
* :func:`assess` turns the findings into a pass or a :class:`DataGapError`. A gap fails the run unless its check is named
  in ``allow`` (``--allow-data-gaps`` on ``scripts/reproduce_study.py``); allowed gaps are still written to the run record.

Findings are keyed by the anonymous site label and carry shares (percentages), never counts or site names.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = ["DataGapWarning", "DataGapError", "ConceptSetExclusionWarning", "GAP_CHECKS", "GAP_CAUSES", "classify_findings",
           "collapse_concept_set_exclusions", "load_known_source_gaps", "feature_coverage", "geography_findings", "group_missing_shares", "FEATURE_PREFIXES", "assess", "format_findings", "mask_counts", "sanitize"]

#: Every check that can fail a study run, with what it means. ``--allow-data-gaps`` takes these names (or a feature
#: group's name, or ``all``).
GAP_CHECKS: dict[str, str] = {
    "address_table": "no usable dated address table was found, or the name given does not exist (SDI / RUCA would all be missing)",
    "sdoh": "SDI / RUCA missing for (nearly) every stay at a site",
    "vocabulary": "concept_ancestor not reachable: descendant concepts are not resolved, so cohort and features shrink",
    "transfer": "the transfer exclusion matches no stay because discharge status was never mapped",
    "pinned": "stays in the pinned list were not found in the cohort, or the list names sites that are not in the run",
    "empty_site": "a site contributed no stays",
    "group_missing": "a whole feature group is missing (or all zero) for (nearly) every stay at a site",
    "group_skipped": "a whole feature group could not be computed at a site (a table or concept is absent)",
    "stale_features": "--reuse-features found features saved with other settings (window, address table, inputs, preset ...)",
    "feature_skipped": "a lab, vital or medication class has no concept in this database, so its column is absent or all zero",
    "column_missing": "a feature column has no value for any stay at a site",
    "window": "a site's data do not cover the study window (it starts late or ends early), so stays or outcomes are quietly lost",
    "observation_period": "observation periods pile up on one end date (events filled with the build date), so follow-up is unreliable",
    "concept_set_coverage": "a lab's rows at a site use LOINC codes its concept set does not hold (blood vs serum/plasma, molar units)",
    "sdoh_partial": "many stays have a ZIP that is not in the SDI or RUCA table (ZIP vs ZCTA format, an old crosswalk)",
}

#: Column-name prefixes of the benchmark features; ``--allow-data-gaps lab_ldl`` accepts that column (and ``lab_ldl_last`` ...).
FEATURE_PREFIXES = ("lab_", "vital_", "med_", "ederri_med_", "elix_", "ederri_elix_", "disp_", "acs_")
_GROUP_NAMES = {"labs", "vitals", "medications", "medications_ederri", "elixhauser", "elixhauser_ederri", "utilization", "demographics",
                "disposition", "protocol_history", "study_features", "sdoh"}


class DataGapWarning(UserWarning):
    """A gap that degrades results without stopping the code. ``check`` names it (a key of :data:`GAP_CHECKS`)."""

    def __init__(self, message: str, check: str = "group_missing", column: str | None = None) -> None:
        super().__init__(message)
        self.check = check
        self.column = column


class ConceptSetExclusionWarning(UserWarning):
    """A lab's rows sit under codes its concept set leaves out on purpose (another unit type or specimen). Not a gap: a study run
    lists each such lab once per study (check ``concept_set_exclusion``, cause ``intentional``), not once per site (0.35.0)."""

    def __init__(self, message: str, column: str | None = None, codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.column = column
        self.codes = list(codes)


#: Why a finding is there (0.35.0, issue 106). ``source_gap``: a gap in the source extract that the study team has confirmed
#: (listed in the known-source-gaps file; expected); ``intentional``: a design choice such as a concept set that leaves codes out on
#: purpose (expected); ``etl_mapping``: rows that exist but were not mapped or read (vocabulary, concept-set coverage, unmapped
#: discharge status, a feature with no concept: fix the ETL); ``unexplained``: anything else, to be looked at.
GAP_CAUSES: dict[str, str] = {
    "source_gap": "confirmed gap in the source extract (listed in the known-source-gaps file)",
    "intentional": "a deliberate design choice (a concept set leaves these codes out on purpose)",
    "etl_mapping": "rows exist but were not mapped or not read: an ETL or vocabulary gap to fix",
    "unexplained": "not explained yet: look at it",
}
_ETL_CHECKS = {"vocabulary", "concept_set_coverage", "transfer", "feature_skipped", "address_table", "sdoh_partial"}


def load_known_source_gaps(spec: Any) -> dict[str, str]:
    """The study team's confirmed source gaps: ``{key: reason}``. ``spec`` is a mapping, a sequence of keys, a JSON file (an object
    ``{key: reason}`` or a list of keys) or a text file with one key per line (``# reason`` after it). A key is a feature column
    (``lab_potassium``; ``lab_potassium`` also covers ``lab_potassium_last``), a feature group (``vitals``) or a check, optionally
    for one site only as ``<site>:<key>`` (the site's name as given to ``--sites``; keep the file private)."""
    import json
    from pathlib import Path

    if spec is None:
        return {}
    if isinstance(spec, Mapping):
        return {str(k).strip(): str(v) for k, v in spec.items() if str(k).strip()}
    if isinstance(spec, (str, Path)) and Path(spec).exists():
        text = Path(spec).read_text(encoding="utf-8")
        if str(spec).lower().endswith(".json"):
            data = json.loads(text)
            return load_known_source_gaps(data if isinstance(data, Mapping) else list(data))
        out = {}
        for line in text.splitlines():
            key, _, why = line.partition("#")
            if key.strip():
                out[key.strip()] = why.strip()
        return out
    if isinstance(spec, (str, Path)):
        raise ValueError(f"known source gaps file {str(spec)!r} does not exist")
    return {str(k).strip(): "" for k in spec if str(k).strip()}


def _known_match(f: Mapping[str, Any], known: Mapping[str, str], site_keys: Mapping[str, str]) -> str | None:
    """The key of ``known`` that covers finding ``f`` (``site_keys``: anonymous label -> site name), or None. A finding of an ETL
    check (an unmapped or unread input, :data:`_ETL_CHECKS`) is covered only by a key naming that check, never by a column or group
    key: listing ``lab_potassium`` as a source gap must not hide potassium rows that exist but were not mapped."""
    names = ([f.get("check")] if f.get("check") in _ETL_CHECKS else
             [n for n in (f.get("column"), f.get("group"), f.get("check")) if n])
    names = [n for n in names if n]
    site = site_keys.get(str(f.get("site")), None)
    for key in known:
        scope, _, item = key.rpartition(":")
        if scope and scope != site:
            continue
        for n in names:
            if n == item or (n == f.get("column") and str(n).startswith(item + "_")):
                return key
    return None


def classify_findings(findings: Sequence[dict[str, Any]], known_source_gaps: Mapping[str, str] | None = None,
                      site_keys: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Adds ``cause`` (a key of :data:`GAP_CAUSES`) and ``expected`` to each finding, in place, and returns them. A finding covered by
    ``known_source_gaps`` (:func:`load_known_source_gaps`) is a ``source_gap`` with its reason in ``known_reason``; an intentional
    concept-set exclusion is ``intentional``; the checks of an unmapped or unread input are ``etl_mapping``; the rest
    ``unexplained``. Expected findings (source gaps and intentional exclusions) never fail a run (:func:`assess`)."""
    known = dict(known_source_gaps or {})
    keys = dict(site_keys or {})
    for f in findings:
        hit = _known_match(f, known, keys) if known else None
        if hit is not None:
            f["cause"] = "source_gap"
            if known[hit]:
                f["known_reason"] = known[hit]
        elif f.get("check") == "concept_set_exclusion":
            f["cause"] = "intentional"
        elif f.get("check") in _ETL_CHECKS:
            f["cause"] = "etl_mapping"
        else:
            f["cause"] = f.get("cause") if f.get("cause") in GAP_CAUSES else "unexplained"
        f["expected"] = f["cause"] in ("source_gap", "intentional")
    return list(findings)


def collapse_concept_set_exclusions(findings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The per-site ``concept_set_exclusion`` findings merged into one study-level finding per lab (``site`` = ``"all"``), listing the
    left-out codes and the number of sites, never per-site shares. Other findings are returned unchanged, in order."""
    out: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f.get("check") != "concept_set_exclusion":
            out.append(f)
            continue
        col = str(f.get("column") or "?")
        m = merged.get(col)
        if m is None:
            m = merged[col] = {"site": "all", "check": "concept_set_exclusion", "column": col, "severity": "info", "codes": [],
                               "_sites": set(), "cause": "intentional", "expected": True}
            out.append(m)
        m["_sites"].add(f.get("site"))
        m["codes"] = sorted(set(m["codes"]) | set(f.get("codes") or ()))
    for m in merged.values():
        n = len(m.pop("_sites"))
        m["n_sites"] = n
        m["message"] = (f"{m['column']}: rows under {', '.join(m['codes']) or 'other codes'} (same analyte, another unit type or "
                        f"specimen) are left out of the concept set on purpose, at {n} site(s)")
    return out


class DataGapError(RuntimeError):
    """A study run found a data gap that was not explicitly allowed. ``findings`` lists every finding of that site."""

    def __init__(self, message: str, findings: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.findings = list(findings)


def sanitize(text: str, replacements: Mapping[str, str]) -> str:
    """Replaces each key (a site key, a database path) with its value (the anonymous label), longest first."""
    out = str(text)
    for k in sorted((k for k in replacements if k), key=len, reverse=True):
        out = out.replace(k, replacements[k])
    return out


_COUNT = re.compile(r"(?<![\w.\-/'])\d{1,3}(?:,\d{3})+(?![\w.\-/%])|(?<![\w.\-/'])\d{2,}(?![\w.\-/%])")


_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")


def mask_counts(text: str) -> str:
    """Replaces dates with ``<date>`` (a site's last data date can identify it) and whole numbers of two digits or more (``61,204``, ``33``) with ``<n>``. Percentages, decimals and codes joined to letters,
    dots or hyphens (``E11.9``, ``2160-0``, ``lab_ldl``) are kept. For messages that were not written for a shareable record."""
    return _COUNT.sub("<n>", _DATE.sub("<date>", str(text)))


def _missing_share(frame: pd.DataFrame, group: str, cols: Sequence[str]) -> tuple[float, bool]:
    """Share of rows where every column of the group is missing, and whether every present value is zero."""
    if group == "sdoh" and "sdoh_missing" in frame:
        return float(pd.to_numeric(frame["sdoh_missing"], errors="coerce").fillna(1).eq(1).mean()), False
    sub = frame[[c for c in cols if c in frame]]
    if sub.shape[1] == 0 or len(sub) == 0:
        return 1.0, False
    share = float(sub.isna().all(axis=1).mean())
    num = sub.apply(pd.to_numeric, errors="coerce")
    present = num.notna() & sub.notna()
    all_zero = bool(present.to_numpy().any()) and bool(num.where(present).fillna(0).eq(0).to_numpy().all()) \
        and sub.select_dtypes(exclude="number").shape[1] == 0
    return share, all_zero


def feature_coverage(frame: pd.DataFrame, *, site: str, groups: Mapping[str, Sequence[str]] | None = None,
                     skipped: Iterable[str] = (), threshold: float = 0.95) -> list[dict[str, Any]]:
    """Findings for one site's feature table.

    ``groups`` maps a feature group to its columns (``attrs["feature_groups"]`` of
    :func:`omop_etl.benchmark_features.extract_benchmark_features`). A group is a gap (severity ``error``) when at least
    ``threshold`` of the stays have none of its columns, or when every value in it is zero (a join that matched nothing),
    and when it was skipped altogether. A single lab, vital or medication class that was skipped (no concept), or a feature
    column with no value for any stay, is also an ``error`` (accepted by its name, e.g. ``lab_ldl``). A medication column
    that is 0 for every stay, and an empty column outside the feature groups, are ``warning``s.
    """
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1].")
    out: list[dict[str, Any]] = []
    if len(frame) == 0:
        return [{"site": site, "check": "empty_site", "severity": "error", "message": "the site contributed no stays"}]
    whole_groups = {"protocol_history", "disposition", "sdoh", "labs", "vitals", "medications", "elixhauser", "utilization", "demographics"}
    for s in skipped:
        if s in whole_groups:
            out.append({"site": site, "check": "group_skipped", "group": s, "severity": "error",
                        "message": f"feature group {s!r} could not be computed and is absent"})
        else:
            out.append({"site": site, "check": "feature_skipped", "column": s, "severity": "error",
                        "message": f"{s} has no concept in this database and is absent"})
    for g, cols in (groups or {}).items():
        share, all_zero = _missing_share(frame, g, cols)
        check = "sdoh" if g == "sdoh" else "group_missing"
        if share >= threshold:
            out.append({"site": site, "check": check, "group": g, "severity": "error", "share_missing": round(share, 4),
                        "message": f"feature group {g!r} is missing for {share:.1%} of stays (threshold {threshold:.0%})"})
        elif all_zero and len(frame) >= 20:
            out.append({"site": site, "check": check, "group": g, "severity": "error", "share_missing": round(share, 4),
                        "message": f"every value of feature group {g!r} is 0 (a lookup that matched nothing?)"})
    keys = {"person_id", "subject_id", "visit_occurrence_id", "stay_id", "row_id", "site", "site_source", "site_anon"}
    groups = dict(groups or {})
    in_group = {c for cols in groups.values() for c in cols}
    reported = {c for f in out if f["severity"] == "error" and f.get("group") in groups for c in groups[f["group"]]}
    for c in frame.columns:
        if c in keys or c in reported:
            continue
        if frame[c].isna().all():
            # a feature column with no value at all (a lab never coded at this site): an error, accepted by its name
            hard = c in in_group
            out.append({"site": site, "check": "column_missing", "column": c, "severity": "error" if hard else "warning",
                        "message": f"column {c!r} has no value for any stay"})
        elif c.startswith(("med_", "ederri_med_")) and len(frame) >= 200 and pd.to_numeric(frame[c], errors="coerce").fillna(0).eq(0).all():
            out.append({"site": site, "check": "column_all_zero", "column": c, "severity": "warning",
                        "message": f"no stay has {c} (a medication class that matched no drug?)"})
    return out


def geography_findings(stats: Mapping[str, Any] | None, *, site: str, max_unmatched: float = 0.10,
                       max_earliest_after: float = 0.5) -> list[dict[str, Any]]:
    """Partial SDI / RUCA loss that the group threshold misses: ZIPs found but absent from the SDI or RUCA table for at least
    ``max_unmatched`` of the stays with a ZIP (``error``, check ``sdoh_partial``), and the address chosen by the "earliest
    address after admission" fallback for at least ``max_earliest_after`` of stays (``warning``: dates in the address table look wrong)."""
    if not stats:
        return []
    out = []
    for key, table in (("zip_not_in_sdi", "SDI"), ("zip_not_in_ruca", "RUCA")):
        v = stats.get(key)
        if v is not None and v >= max_unmatched:
            out.append({"site": site, "check": "sdoh_partial", "severity": "error", "share": v,
                        "message": f"{v:.1%} of the stays with a ZIP have one that is not in the {table} table (ZIP vs ZCTA, ZIP+4, an old file?)"})
    ea = (stats.get("rule_shares") or {}).get("earliest_after", 0.0)
    if ea >= max_earliest_after:
        out.append({"site": site, "check": "address_rule", "severity": "warning", "share": ea,
                    "message": f"{ea:.1%} of stays use the earliest address that starts after admission (address start dates look wrong)"})
    return out


def group_missing_shares(frame: pd.DataFrame, groups: Mapping[str, Sequence[str]] | None) -> dict[str, float]:
    """Share of stays with the whole group missing, per feature group (SDI / RUCA: ``sdoh_missing``), for the run record."""
    return {g: round(_missing_share(frame, g, cols)[0], 4) for g, cols in (groups or {}).items()} if len(frame) else {}


def _allowed(f: Mapping[str, Any], allow: set[str]) -> bool:
    if f.get("expected"):  # a confirmed source gap or an intentional exclusion (classify_findings)
        return True
    if {"all", f.get("check"), f.get("group")} & allow:
        return True
    col = f.get("column")
    return bool(col) and any(col == a or col.startswith(a + "_") for a in allow)


def assess(findings: Sequence[dict[str, Any]], allow: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Marks each finding ``allowed`` (its check or group is in ``allow``) and raises :class:`DataGapError` for the errors that
    are not. Returns the findings (marked) when nothing fails."""
    allow_set = {str(a) for a in allow}
    unknown = {a for a in allow_set - set(GAP_CHECKS) - {"all"} - _GROUP_NAMES - {f.get("group") for f in findings}
               if not a.startswith(FEATURE_PREFIXES)}
    if unknown:
        raise ValueError(f"--allow-data-gaps: unknown name(s) {sorted(unknown)}; give a check ({', '.join(sorted(GAP_CHECKS))}), a feature "
                         f"group, a feature column (starting {', '.join(FEATURE_PREFIXES)}) or 'all'.")
    for f in findings:
        if f.get("severity") == "error":
            f["allowed"] = _allowed(f, allow_set)
    failing = [f for f in findings if f.get("severity") == "error" and not f.get("allowed")]
    if failing:
        names = sorted({f.get("column") or f.get("group") or f["check"] for f in failing})
        raise DataGapError("data gap check failed:\n" + format_findings(failing) +
                           f"\nFix the input, or rerun with --allow-data-gaps {' '.join(names)} (run_study(allow_data_gaps=[...])) "
                           "to accept the gap; it is then recorded in the run record. Add --reuse-features to keep the sites already built, and "
                           "--check-data to see every site's gaps in one pass.", findings)
    return list(findings)


def format_findings(findings: Sequence[Mapping[str, Any]]) -> str:
    """One line per finding, for the console."""
    return "\n".join(f"  [{f.get('site', '-')}] {f['check']}: {f['message']}"
                     + (f" [{f['cause']}" + (f": {f['known_reason']}" if f.get("known_reason") else "") + "]" if f.get("cause") else "")
                     + (" (allowed)" if f.get("allowed") else "") for f in findings)
