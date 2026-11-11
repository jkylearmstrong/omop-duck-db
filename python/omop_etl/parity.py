"""Parity audit between a legacy EHR-derived table and its OMOP-derived counterpart (ODD-4).

``audit_matrix_parity(legacy_df, omop_df, key_col)`` matches rows on a key and reports, per shared column, how far the two
derivations agree: marginal distribution differences, missingness discrepancies and concordance statistics.
It is generic: it needs two DataFrames and a key, and knows nothing about any site or study.

Per numeric column: matched N, means, SDs, standardized mean difference (SMD), Pearson correlation, Lin's concordance
correlation coefficient (CCC), mean absolute difference and the share of matched pairs that agree within a tolerance.
Per categorical / boolean column: percent agreement, Cohen's kappa and the total variation distance between the two
marginal distributions. For every column: missingness in each table, the gap, and the pairs where exactly one side is missing.
A level-by-level table (``audit.levels``) gives each category's count in each table.

Outputs that leave a site are suppressed with :mod:`omop_etl.disclosure` (``min_cell``, default 11): counts below it are
blanked, as are the statistics computed from so few rows. The key values themselves are never written to the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.disclosure import DEFAULT_MIN_CELL, suppress_small_counts

__all__ = ["audit_matrix_parity", "ParityAudit"]

_STAT_COLS = ["smd", "correlation", "ccc", "mean_abs_diff", "pct_within_tolerance", "pct_agreement", "kappa", "tv_distance"]


@dataclass
class ParityAudit:
    """Result of :func:`audit_matrix_parity`: ``ledger`` (one row per column), ``levels``, ``keys`` (match counts)."""

    ledger: pd.DataFrame
    levels: pd.DataFrame
    keys: pd.DataFrame
    min_cell: int | None = None
    flagged: list[str] = field(default_factory=list)

    def to_csv(self, out_dir: str | Path) -> dict[str, Path]:
        """Writes ``parity_ledger.csv``, ``parity_levels.csv`` and ``parity_keys.csv`` (suppressed cells empty)."""
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, frame in (("parity_ledger", self.ledger), ("parity_levels", self.levels), ("parity_keys", self.keys)):
            paths[name] = d / f"{name}.csv"
            frame.to_csv(paths[name], index=False)
        return paths


def _is_numeric(s1: pd.Series, s2: pd.Series) -> bool:
    return (pd.api.types.is_numeric_dtype(s1) and pd.api.types.is_numeric_dtype(s2)
            and not pd.api.types.is_bool_dtype(s1) and not pd.api.types.is_bool_dtype(s2))


def _ccc(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2:
        return np.nan
    vx, vy = x.var(ddof=0), y.var(ddof=0)
    den = vx + vy + (x.mean() - y.mean()) ** 2
    return float(2 * np.cov(x, y, ddof=0)[0, 1] / den) if den > 0 else (1.0 if np.array_equal(x, y) else np.nan)


def _kappa(a: pd.Series, b: pd.Series) -> float:
    ct = pd.crosstab(a, b)
    n = ct.values.sum()
    if n == 0:
        return np.nan
    po = np.trace(ct.reindex(index=ct.index.union(ct.columns), columns=ct.index.union(ct.columns), fill_value=0).values) / n
    pe = float((ct.sum(axis=1).reindex(ct.index.union(ct.columns), fill_value=0)
                * ct.sum(axis=0).reindex(ct.index.union(ct.columns), fill_value=0)).sum() / n ** 2)
    return float((po - pe) / (1 - pe)) if pe < 1 else (1.0 if po == 1 else np.nan)


def audit_matrix_parity(
    legacy_df: pd.DataFrame,
    omop_df: pd.DataFrame,
    key_col: str | Sequence[str],
    *,
    columns: Sequence[str] | None = None,
    by: str | None = None,
    tolerance: float | Mapping[str, float] = 0.0,
    smd_threshold: float = 0.10,
    missingness_gap_threshold: float = 0.05,
    kappa_threshold: float = 0.80,
    ccc_threshold: float = 0.90,
    max_levels: int = 50,
    min_cell: int | None = DEFAULT_MIN_CELL,
) -> ParityAudit:
    """Audits how closely ``omop_df`` reproduces ``legacy_df``.

    Args:
        legacy_df, omop_df: The two tables. Rows are matched on ``key_col``; duplicate keys in either table raise (an
            ambiguous match would make every statistic meaningless).
        key_col: Column name (or list of names) present in both.
        columns: Columns to compare (default: all columns the two tables share, except the key and ``by``).
        by: Optional column (in either table) to stratify by, e.g. an anonymized site label; an ``ALL`` stratum is added.
        tolerance: A numeric pair agrees if ``|a - b| <= tolerance`` (one value, or ``{column: tol}``).
        smd_threshold, missingness_gap_threshold, kappa_threshold, ccc_threshold: Thresholds that set ``flagged``.
        max_levels: A non-numeric column with more levels than this is treated as an identifier-like text field: its
            agreement is computed but its levels are not tabulated.
        min_cell: Suppress counts below this (``None`` or ``1`` keeps exact counts - for use inside the site only).

    Returns:
        :class:`ParityAudit` with ``ledger`` [stratum, column, kind, n_matched, n_both_missing, n_one_missing,
        left_missing_rate, right_missing_rate, missingness_gap, left_mean, right_mean, left_sd, right_sd, smd,
        correlation, ccc, mean_abs_diff, pct_within_tolerance, pct_agreement, kappa, tv_distance, flagged, reason,
        suppressed], ``levels`` and ``keys``.
    """
    keys = [key_col] if isinstance(key_col, str) else list(key_col)
    for name, d in (("legacy_df", legacy_df), ("omop_df", omop_df)):
        absent = [k for k in keys if k not in d.columns]
        if absent:
            raise KeyError(f"{name} has no key column(s) {absent}")
        if d.duplicated(subset=keys).any():
            raise ValueError(f"{name} has duplicate values of the key {keys}; deduplicate before auditing parity")
    skip = set(keys) | ({by} if by else set())
    cols = list(columns) if columns is not None else [c for c in legacy_df.columns if c in omop_df.columns and c not in skip]
    if not cols:
        raise ValueError("the two tables share no columns to compare")
    left = legacy_df[keys + [c for c in cols if c != by] + ([by] if by and by in legacy_df.columns else [])]
    right = omop_df[keys + [c for c in cols if c != by] + ([by] if by and by in omop_df.columns and by not in left.columns else [])]
    merged = left.merge(right, on=keys, how="inner", suffixes=("__L", "__R"))
    n_left, n_right, n_match = len(legacy_df), len(omop_df), len(merged)

    keys_tbl = pd.DataFrame({"n_legacy": [n_left], "n_omop": [n_right], "n_matched": [n_match],
                             "n_only_legacy": [n_left - n_match], "n_only_omop": [n_right - n_match]})
    keys_tbl["pct_legacy_matched"] = 100.0 * n_match / n_left if n_left else np.nan
    keys_tbl["pct_omop_matched"] = 100.0 * n_match / n_right if n_right else np.nan

    strat_col = None
    if by:
        strat_col = f"{by}__L" if f"{by}__L" in merged.columns else by
    strata = [("ALL", merged)]
    if strat_col is not None:
        strata += [(str(k), g) for k, g in merged.groupby(strat_col, dropna=False)]

    tol_for = (lambda c: float(tolerance.get(c, 0.0))) if isinstance(tolerance, Mapping) else (lambda c: float(tolerance))
    rows, level_rows = [], []
    for stratum, g in strata:
        for c in cols:
            if c == by:
                continue
            a = g[f"{c}__L"] if f"{c}__L" in g.columns else g[c]
            b = g[f"{c}__R"] if f"{c}__R" in g.columns else g[c]
            n = len(g)
            ma, mb = a.isna(), b.isna()
            rec: dict[str, Any] = {
                "stratum": stratum, "column": c, "n_matched": n, "n_both_missing": int((ma & mb).sum()),
                "n_one_missing": int((ma ^ mb).sum()),
                "left_missing_rate": float(ma.mean()) if n else np.nan, "right_missing_rate": float(mb.mean()) if n else np.nan,
            }
            rec["missingness_gap"] = abs(rec["left_missing_rate"] - rec["right_missing_rate"]) if n else np.nan
            both = ~(ma | mb)
            reasons = []
            if _is_numeric(a, b):
                x, y = a[both].to_numpy(dtype=float), b[both].to_numpy(dtype=float)
                rec["kind"] = "numeric"
                rec.update(left_mean=float(np.nanmean(a)) if (~ma).any() else np.nan, right_mean=float(np.nanmean(b)) if (~mb).any() else np.nan,
                           left_sd=float(np.nanstd(a, ddof=1)) if (~ma).sum() > 1 else np.nan,
                           right_sd=float(np.nanstd(b, ddof=1)) if (~mb).sum() > 1 else np.nan)
                pooled = np.sqrt((np.nan_to_num(rec["left_sd"]) ** 2 + np.nan_to_num(rec["right_sd"]) ** 2) / 2)
                rec["smd"] = (rec["left_mean"] - rec["right_mean"]) / pooled if pooled > 0 else (0.0 if rec["left_mean"] == rec["right_mean"] else np.nan)
                rec["correlation"] = float(np.corrcoef(x, y)[0, 1]) if len(x) > 2 and x.std() > 0 and y.std() > 0 else np.nan
                rec["ccc"] = _ccc(x, y)
                rec["mean_abs_diff"] = float(np.mean(np.abs(x - y))) if len(x) else np.nan
                rec["pct_within_tolerance"] = 100.0 * float(np.mean(np.abs(x - y) <= tol_for(c))) if len(x) else np.nan
                if pd.notna(rec["smd"]) and abs(rec["smd"]) > smd_threshold:
                    reasons.append(f"|SMD| {abs(rec['smd']):.2f} > {smd_threshold}")
                if pd.notna(rec["ccc"]) and rec["ccc"] < ccc_threshold:
                    reasons.append(f"CCC {rec['ccc']:.2f} < {ccc_threshold}")
            else:
                xa, xb = a[both].astype(str), b[both].astype(str)
                rec["kind"] = "categorical"
                rec["pct_agreement"] = 100.0 * float((xa.values == xb.values).mean()) if len(xa) else np.nan
                rec["kappa"] = _kappa(xa, xb) if len(xa) else np.nan
                pa, pb = a.dropna().astype(str).value_counts(normalize=True), b.dropna().astype(str).value_counts(normalize=True)
                idx = pa.index.union(pb.index)
                rec["tv_distance"] = float(0.5 * np.abs(pa.reindex(idx, fill_value=0) - pb.reindex(idx, fill_value=0)).sum()) if len(idx) else np.nan
                if pd.notna(rec["kappa"]) and rec["kappa"] < kappa_threshold:
                    reasons.append(f"kappa {rec['kappa']:.2f} < {kappa_threshold}")
                if len(idx) <= max_levels:
                    ca, cb = a.dropna().astype(str).value_counts(), b.dropna().astype(str).value_counts()
                    for lev in sorted(set(ca.index) | set(cb.index)):
                        level_rows.append({"stratum": stratum, "column": c, "level": lev, "n_legacy": int(ca.get(lev, 0)),
                                           "n_omop": int(cb.get(lev, 0))})
            if pd.notna(rec["missingness_gap"]) and rec["missingness_gap"] > missingness_gap_threshold:
                reasons.append(f"missingness gap {rec['missingness_gap']:.2f} > {missingness_gap_threshold}")
            rec["flagged"] = bool(reasons)
            rec["reason"] = "; ".join(reasons)
            rows.append(rec)

    order = ["stratum", "column", "kind", "n_matched", "n_both_missing", "n_one_missing", "left_missing_rate", "right_missing_rate",
             "missingness_gap", "left_mean", "right_mean", "left_sd", "right_sd"] + _STAT_COLS + ["flagged", "reason"]
    ledger = pd.DataFrame(rows).reindex(columns=order)
    levels = pd.DataFrame(level_rows, columns=["stratum", "column", "level", "n_legacy", "n_omop"])
    flagged = sorted(set(ledger.loc[ledger["flagged"].fillna(False).astype(bool), "column"]))

    mc = min_cell if (min_cell and min_cell > 1) else None
    if mc:
        derived = ["left_missing_rate", "right_missing_rate", "missingness_gap", "left_mean", "right_mean", "left_sd", "right_sd"] + _STAT_COLS
        # statistics from a stratum with fewer than min_cell matched rows, or a one-sided-missing count that small, would expose records
        ledger = suppress_small_counts(ledger, ["n_matched", "n_both_missing", "n_one_missing"], mc, also=derived, complementary=False)
        ledger.loc[ledger["suppressed"], ["flagged", "reason"]] = [None, "suppressed (small cell)"]
        levels = suppress_small_counts(levels, ["n_legacy", "n_omop"], mc, complementary=True, group_by=["stratum", "column"]) if len(levels) else levels.assign(suppressed=pd.Series(dtype=bool))
        kc = ["n_legacy", "n_omop", "n_matched", "n_only_legacy", "n_only_omop"]
        keys_tbl = suppress_small_counts(keys_tbl, kc, mc, also=["pct_legacy_matched", "pct_omop_matched"], complementary=False)
        # the five counts are linked by n_legacy = n_matched + n_only_legacy (same for omop): one blank would be recoverable
        keys_tbl.loc[keys_tbl["suppressed"], kc] = np.nan
    else:
        ledger["suppressed"] = False
        keys_tbl["suppressed"] = False
        levels["suppressed"] = False
    return ParityAudit(ledger=ledger, levels=levels, keys=keys_tbl, min_cell=mc, flagged=flagged)
