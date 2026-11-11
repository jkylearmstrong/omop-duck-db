"""Small-cell suppression / disclosure control for tables that leave a site (ODD-5).

Every aggregate this package writes for sharing (CONSORT attrition, pooled Table 1, parity ledgers, site summary
packages) goes through these helpers so one threshold, one masking rule and one set of checks apply everywhere.

* :func:`suppress_small_counts` blanks counts from 1 to ``threshold - 1`` (zeros are not disclosive) and the columns that
  would give them away (percentages, SMDs, ...). With ``group_by`` it also applies *complementary* suppression: when a
  group has exactly one suppressed cell and its total or margin is published, the smallest remaining cell is blanked too,
  so the hidden value cannot be recovered by subtraction.
* :func:`suppress_attrition_table` does the same for a CONSORT flow, where ``retained[i-1] - dropped[i] == retained[i]`` makes
  every hidden number recoverable from its neighbours unless a second one in the same equation is hidden as well.
* :func:`check_disclosure` / :func:`assert_disclosure_safe` re-check a finished table (the last line of defence before a
  file is written).

The default threshold is 11, the usual cell-size rule of data-use agreements (CMS, many OHDSI network studies).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "DEFAULT_MIN_CELL", "DisclosureError", "small_cell_mask", "format_count", "suppress_small_counts",
    "suppress_attrition_table", "check_disclosure", "assert_disclosure_safe",
]

DEFAULT_MIN_CELL = 11


class DisclosureError(ValueError):
    """A table that was about to leave the site still holds a small cell."""


def _check_threshold(threshold: int) -> int:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, np.integer)) or threshold < 1:
        raise ValueError("threshold must be a positive integer (use 1 to switch suppression off)")
    return int(threshold)


def small_cell_mask(values: Any, threshold: int = DEFAULT_MIN_CELL) -> pd.Series:
    """True where a count is between 1 and ``threshold - 1``. Zeros and missing values are not small cells."""
    t = _check_threshold(threshold)
    s = pd.to_numeric(pd.Series(values), errors="coerce")
    return (s > 0) & (s < t)


def format_count(n: Any, threshold: int = DEFAULT_MIN_CELL) -> str:
    """``"1,234"`` for a shown count, ``"<11"`` for a suppressed one (missing or small)."""
    t = _check_threshold(threshold)
    if n is None or (isinstance(n, float) and np.isnan(n)) or n is pd.NA:
        return f"<{t}"
    n = float(n)
    return f"<{t}" if 0 < n < t else f"{int(round(n)):,}"


def suppress_small_counts(
    df: pd.DataFrame,
    count_cols: Sequence[str],
    threshold: int = DEFAULT_MIN_CELL,
    *,
    also: Iterable[str] = (),
    group_by: str | Sequence[str] | None = None,
    complementary: bool = True,
    flag_col: str | None = "suppressed",
) -> pd.DataFrame:
    """Returns a copy of ``df`` with small counts replaced by NaN.

    Args:
        count_cols: Columns holding counts. A value from 1 to ``threshold - 1`` is blanked.
        also: Columns blanked on every row that has a blanked count (percentages, means, SMDs, ... that would reveal it).
        group_by: Column(s) defining groups whose total or margin is published. With ``complementary=True`` a group (per
            count column) that ends up with exactly one blanked cell gets its smallest remaining non-zero cell blanked as
            well. Leave ``None`` when no total is published beside the cells.
        flag_col: Name of the boolean column marking rows that lost a value (``None`` to omit it).

    Counts are returned as float64 so NaN can stand for "suppressed"; render with :func:`format_count`.
    """
    t = _check_threshold(threshold)
    count_cols = list(count_cols)
    missing = [c for c in count_cols + list(also) if c not in df.columns]
    if missing:
        raise KeyError(f"columns not in the table: {missing}")
    out = df.copy()
    for c in count_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    mask = pd.DataFrame({c: (out[c] > 0) & (out[c] < t) for c in count_cols}, index=out.index)
    if complementary and group_by is not None and t > 1:
        for _, idx in out.groupby(group_by, dropna=False).groups.items():
            for c in count_cols:
                hidden = mask.loc[idx, c]
                if int(hidden.sum()) == 1:
                    cand = out.loc[idx, c][(~hidden) & (out.loc[idx, c] > 0)]
                    if len(cand):
                        mask.loc[cand.idxmin(), c] = True
    row_mask = mask.any(axis=1)
    for c in count_cols:
        out.loc[mask[c], c] = np.nan
    for c in also:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64") if pd.api.types.is_numeric_dtype(out[c]) else out[c]
        out.loc[row_mask, c] = np.nan
    if flag_col:
        out[flag_col] = row_mask.to_numpy()
    return out


def suppress_attrition_table(
    df: pd.DataFrame,
    threshold: int = DEFAULT_MIN_CELL,
    *,
    retained_col: str = "subjects_retained",
    dropped_col: str = "subjects_dropped",
    also: Iterable[str] = ("percent_retained",),
    flag_col: str | None = "suppressed",
) -> pd.DataFrame:
    """Suppresses a CONSORT attrition table so no small count can be recovered by subtraction.

    Rows are consecutive steps; step ``i`` obeys ``retained[i-1] - dropped[i] == retained[i]``. A hidden number is
    recoverable whenever the other two terms of one equation are shown, so this closes the table to a fixed point:
    a lone hidden term in any equation hides a second one (the drop, or - when the drop is the hidden term - the retained
    count, which carries the block forward). Finally any run of hidden steps must account for either no one or at least
    ``threshold`` subjects between the two shown retained counts around it; if not, the next retained count is hidden too.
    """
    t = _check_threshold(threshold)
    out = df.copy().reset_index(drop=True)
    n = len(out)
    ret = pd.to_numeric(out[retained_col], errors="coerce").to_numpy(dtype="float64")
    if dropped_col in out.columns:
        drop = pd.to_numeric(out[dropped_col], errors="coerce").to_numpy(dtype="float64")
    else:
        drop = np.r_[0.0, np.maximum(ret[:-1] - ret[1:], 0.0)] if n else np.array([])
        out[dropped_col] = drop
    if n == 0 or t <= 1:
        if flag_col:
            out[flag_col] = False
        return out
    hr = (ret > 0) & (ret < t)           # hidden retained
    hd = (drop > 0) & (drop < t)         # hidden dropped
    hd[0] = False                        # step 1 has no exclusion
    changed = True
    guard = 0
    while changed and guard < 10 * n + 10:
        guard += 1
        changed = False
        for i in range(1, n):
            hidden = int(hr[i - 1]) + int(hd[i]) + int(hr[i])
            if hidden == 1:
                if hd[i]:
                    hr[i] = True
                elif hr[i - 1] or hr[i]:
                    hd[i] = True
                changed = True
        # a run of hidden steps must account for 0 or >= threshold subjects between the shown counts around it
        shown = [i for i in range(n) if not hr[i]]
        for a, b in zip(shown, shown[1:]):
            if b - a > 1 and not (ret[a] - ret[b] == 0 or ret[a] - ret[b] >= t):
                hr[b] = True
                changed = True
    row_mask = hr | hd
    out[retained_col] = ret
    out[dropped_col] = drop
    out.loc[hr, retained_col] = np.nan
    out.loc[hd, dropped_col] = np.nan
    for c in also:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
            out.loc[hr, c] = np.nan
    if flag_col:
        out[flag_col] = row_mask
    return out


def check_disclosure(df: pd.DataFrame, count_cols: Sequence[str], threshold: int = DEFAULT_MIN_CELL) -> pd.DataFrame:
    """Rows and columns of ``df`` that still hold a count from 1 to ``threshold - 1`` (empty when the table is safe)."""
    rows = []
    for c in count_cols:
        if c not in df.columns:
            raise KeyError(f"column not in the table: {c}")
        bad = small_cell_mask(df[c], threshold)
        for i in df.index[bad.to_numpy()]:
            rows.append({"row": i, "column": c, "value": df.at[i, c]})
    return pd.DataFrame(rows, columns=["row", "column", "value"])


def assert_disclosure_safe(df: pd.DataFrame, count_cols: Sequence[str], threshold: int = DEFAULT_MIN_CELL, *,
                           what: str = "table") -> None:
    """Raises :class:`DisclosureError` naming the offending cells (never their values) when a small count is present."""
    bad = check_disclosure(df, count_cols, threshold)
    if len(bad):
        cells = ", ".join(f"{r.column}[row {r.row}]" for r in bad.head(10).itertuples())
        raise DisclosureError(f"The {what} holds {len(bad)} count(s) below {threshold}: {cells}. Suppress it before sharing.")
