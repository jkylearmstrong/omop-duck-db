"""CONSORT / STROBE attrition tables built from counts, for one site or pooled across sites (ODD-1).

The database-driven path (``compute_attrition`` -> ``generate_consort_attrition(con, steps=...)``) needs a CDM. Most papers
instead start from a handful of counts - the source population and how many were removed at each step (wash-in, age,
mortality, transfer, ...) - often from several sites. This module turns those counts into the same attrition table
(``step_number, step_name, subjects_retained, subjects_dropped, percent_retained, exclusion_reason``) that the exporters in
:class:`omop_etl.cohort.ConsortAttrition` render, and pools sites by summing the counts.

Nothing here reads patient-level data. Pooled tables are suppressed with :mod:`omop_etl.disclosure` by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.disclosure import DEFAULT_MIN_CELL, suppress_attrition_table

__all__ = ["consort_from_counts", "pool_consort_tables", "consort_table_to_csv", "CONSORT_COLUMNS"]

CONSORT_COLUMNS = ["step_number", "step_name", "subjects_retained", "subjects_dropped", "percent_retained", "exclusion_reason"]


def _norm_exclusions(exclusions: Any) -> list[dict[str, Any]]:
    """Accepts ``[(label, n_excluded), ...]``, ``{label: n_excluded}`` or ``[{label, excluded|remaining, reason}, ...]``."""
    if isinstance(exclusions, Mapping):
        exclusions = list(exclusions.items())
    out = []
    for e in exclusions or []:
        if isinstance(e, Mapping):
            d = dict(e)
        elif isinstance(e, (tuple, list)) and len(e) in (2, 3):
            d = {"label": e[0], "excluded": e[1]}
            if len(e) == 3:
                d["reason"] = e[2]
        else:
            raise ValueError("each exclusion must be (label, n_excluded), (label, n_excluded, reason) or a dict")
        if "label" not in d or ("excluded" not in d) == ("remaining" not in d):
            raise ValueError("an exclusion needs a 'label' and exactly one of 'excluded' or 'remaining'")
        out.append(d)
    return out


def consort_from_counts(initial_n: int, exclusions: Any, *, initial_label: str = "Source population",
                        min_cell: int | None = None) -> pd.DataFrame:
    """Builds an attrition table from the starting count and the exclusion at each step.

    ``exclusions`` is a list of ``(label, n_excluded)`` (or ``(label, n_excluded, reason)``), a ``{label: n_excluded}``
    dict, or dicts with ``label`` and either ``excluded`` or ``remaining`` (the count left after the step), optionally
    ``reason``. A step cannot remove more people than are left. With ``min_cell`` the table is suppressed for release
    (:func:`omop_etl.disclosure.suppress_attrition_table`); leave it ``None`` to keep exact counts inside the site.
    """
    if int(initial_n) != initial_n or initial_n < 0:
        raise ValueError("initial_n must be a non-negative integer")
    steps = [{"step_name": str(initial_label), "subjects_retained": int(initial_n), "subjects_dropped": 0, "exclusion_reason": ""}]
    left = int(initial_n)
    for e in _norm_exclusions(exclusions):
        if "excluded" in e:
            drop = e["excluded"]
        else:
            drop = left - e["remaining"]
        if int(drop) != drop or drop < 0 or drop > left:
            raise ValueError(f"step {e['label']!r}: cannot exclude {drop} from {left} remaining")
        left -= int(drop)
        steps.append({"step_name": str(e["label"]), "subjects_retained": left, "subjects_dropped": int(drop),
                      "exclusion_reason": str(e.get("reason") or e["label"])})
    df = pd.DataFrame(steps)
    df.insert(0, "step_number", np.arange(1, len(df) + 1))
    first = df["subjects_retained"].iloc[0]
    df["percent_retained"] = (100.0 * df["subjects_retained"] / first) if first else 0.0
    df = df[CONSORT_COLUMNS]
    return suppress_attrition_table(df, min_cell) if min_cell else df


def pool_consort_tables(tables: Mapping[str, pd.DataFrame] | Sequence[pd.DataFrame], *, min_cell: int | None = DEFAULT_MIN_CELL,
                        ) -> pd.DataFrame:
    """Pools per-site attrition tables into one by summing retained and dropped counts step by step.

    Every site must report the same steps in the same order (the labels are compared); a mismatch raises rather than
    guessing. A site table that is already suppressed (NaN cells) makes the pooled cell NaN, because the true sum is unknown.
    The pooled table is suppressed with ``min_cell`` (default 11; ``None`` keeps exact counts - only for use inside the
    environment that holds all the sites' data). Release the pooled table *or* the per-site tables, not both: with a
    suppressed site cell, the pooled total minus the other sites would give it back.
    """
    items = list(tables.items()) if isinstance(tables, Mapping) else [(f"site{i + 1}", t) for i, t in enumerate(tables)]
    if not items:
        raise ValueError("no site tables given")
    ref_name, ref = items[0]
    ref_steps = list(ref["step_name"].astype(str))
    for name, t in items[1:]:
        steps = list(t["step_name"].astype(str))
        if steps != ref_steps:
            raise ValueError(f"site {name!r} has different steps from {ref_name!r}: {steps} vs {ref_steps}")
    ret = np.vstack([pd.to_numeric(t["subjects_retained"], errors="coerce").to_numpy(dtype="float64") for _, t in items])
    drop_cols = []
    for _, t in items:
        if "subjects_dropped" in t.columns:
            drop_cols.append(pd.to_numeric(t["subjects_dropped"], errors="coerce").to_numpy(dtype="float64"))
        else:
            r = pd.to_numeric(t["subjects_retained"], errors="coerce").to_numpy(dtype="float64")
            drop_cols.append(np.r_[0.0, r[:-1] - r[1:]])
    drop = np.vstack(drop_cols)
    pooled_ret, pooled_drop = ret.sum(axis=0), drop.sum(axis=0)   # NaN propagates: an unknown site cell makes the sum unknown
    reasons = []
    for i in range(len(ref_steps)):
        r = next((str(t["exclusion_reason"].iloc[i]) for _, t in items
                  if "exclusion_reason" in t.columns and isinstance(t["exclusion_reason"].iloc[i], str) and t["exclusion_reason"].iloc[i]), "")
        reasons.append(r)
    first = pooled_ret[0]
    out = pd.DataFrame({
        "step_number": np.arange(1, len(ref_steps) + 1), "step_name": ref_steps,
        "subjects_retained": pooled_ret, "subjects_dropped": pooled_drop,
        "percent_retained": 100.0 * pooled_ret / first if first and not np.isnan(first) else np.nan,
        "exclusion_reason": reasons,
    })
    out["n_sites"] = len(items)
    return suppress_attrition_table(out, min_cell) if min_cell else out


def consort_table_to_csv(df: pd.DataFrame, path: str | Path, *, min_cell: int | None = None, digits: int = 1) -> Path:
    """Writes the attrition table as a publication-ready CSV (a suppressed count is written as ``<11``)."""
    out = df.copy()
    if min_cell:
        for c in ("subjects_retained", "subjects_dropped"):
            if c in out.columns:
                out[c] = [f"<{min_cell}" if pd.isna(v) else str(int(v)) for v in out[c]]
    elif "suppressed" in out.columns and out["suppressed"].any():
        raise ValueError("the table holds suppressed cells; pass min_cell=<threshold> so they are written as '<threshold>'")
    if "percent_retained" in out.columns:
        out["percent_retained"] = out["percent_retained"].round(digits)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(p, index=False)
    return p
