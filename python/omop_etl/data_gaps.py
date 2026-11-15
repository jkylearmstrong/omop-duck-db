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

from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["DataGapWarning", "DataGapError", "GAP_CHECKS", "feature_coverage", "group_missing_shares", "assess", "format_findings", "sanitize"]

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
}

class DataGapWarning(UserWarning):
    """A gap that degrades results without stopping the code. ``check`` names it (a key of :data:`GAP_CHECKS`)."""

    def __init__(self, message: str, check: str = "group_missing") -> None:
        super().__init__(message)
        self.check = check


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
    and when it was skipped altogether. A single lab or vital that was skipped, or a column with no value at all, is a
    ``warning``. Without ``groups`` only the column-level warnings are produced.
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
            out.append({"site": site, "check": "feature_skipped", "group": s, "severity": "warning",
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
    reported = {c for f in out if f["severity"] == "error" and f.get("group") in groups for c in groups[f["group"]]}
    for c in frame.columns:
        if c not in keys and c not in reported and frame[c].isna().all():
            out.append({"site": site, "check": "column_missing", "column": c, "severity": "warning",
                        "message": f"column {c!r} has no value for any stay"})
    return out


def group_missing_shares(frame: pd.DataFrame, groups: Mapping[str, Sequence[str]] | None) -> dict[str, float]:
    """Share of stays with the whole group missing, per feature group (SDI / RUCA: ``sdoh_missing``), for the run record."""
    return {g: round(_missing_share(frame, g, cols)[0], 4) for g, cols in (groups or {}).items()} if len(frame) else {}


def _allowed(f: Mapping[str, Any], allow: set[str]) -> bool:
    return bool({"all", f.get("check"), f.get("group")} & allow)


def assess(findings: Sequence[dict[str, Any]], allow: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Marks each finding ``allowed`` (its check or group is in ``allow``) and raises :class:`DataGapError` for the errors that
    are not. Returns the findings (marked) when nothing fails."""
    allow_set = {str(a) for a in allow}
    unknown = allow_set - set(GAP_CHECKS) - {"all"} - {f.get("group") for f in findings if f.get("group")} \
        - {"labs", "vitals", "medications", "medications_ederri", "elixhauser", "utilization", "demographics", "disposition", "protocol_history", "study_features"}
    if unknown:
        raise ValueError(f"--allow-data-gaps: unknown check(s) {sorted(unknown)}; known: {sorted(GAP_CHECKS)} or a feature group name, or 'all'.")
    for f in findings:
        if f.get("severity") == "error":
            f["allowed"] = _allowed(f, allow_set)
    failing = [f for f in findings if f.get("severity") == "error" and not f.get("allowed")]
    if failing:
        names = sorted({f.get("group") or f["check"] for f in failing})
        raise DataGapError("data gap check failed:\n" + format_findings(failing) +
                           f"\nFix the input, or rerun with --allow-data-gaps {' '.join(names)} (run_study(allow_data_gaps=[...])) "
                           "to accept the gap; it is then recorded in the run record.", findings)
    return list(findings)


def format_findings(findings: Sequence[Mapping[str, Any]]) -> str:
    """One line per finding, for the console."""
    return "\n".join(f"  [{f.get('site', '-')}] {f['check']}: {f['message']}" + (" (allowed)" if f.get("allowed") else "") for f in findings)


def numpy_safe(o: Any) -> Any:  # pragma: no cover - json default hook
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)
