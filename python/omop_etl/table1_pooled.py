"""Generic Table 1 builder that works from aggregates, so one site or many sites give the same table (ODD-6).

``generate_table1`` (in :mod:`omop_etl.table1`) reads an OMOP database. This module takes a patient-level DataFrame of
whatever shape you built (cohort + demographics + labs), reduces it to a *site summary* (counts, means, SDs - no rows), and
turns one or several site summaries into the published table, with standardized mean differences (SMD) between two groups:

    summary_a = summarize_table1_site(df_a, continuous=["age", "creatinine"], categorical=["sex"], binary=["diabetes"], group_col="exposed")
    table = build_table1(pool_table1_summaries({"A": summary_a, "B": summary_b}), min_cell=11)

Continuous variables pool exactly (the pooled mean and SD equal those of the concatenated data). The summary holds no
patient-level values, so it is what ``export_site_summary`` writes. :func:`build_table1` suppresses counts below ``min_cell``
(and the percentages / SMDs they would reveal) using :mod:`omop_etl.disclosure`.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.disclosure import DEFAULT_MIN_CELL, suppress_small_counts

__all__ = ["summarize_table1_site", "pool_table1_summaries", "build_table1", "SUMMARY_COLUMNS"]

SUMMARY_COLUMNS = ["group", "variable", "kind", "level", "n", "n_missing", "mean", "sd", "count"]
_OVERALL = "Overall"


def summarize_table1_site(df: pd.DataFrame, continuous: Sequence[str] = (), categorical: Sequence[str] = (),
                          binary: Sequence[str] = (), group_col: str | None = None) -> pd.DataFrame:
    """Reduces patient-level rows to the aggregates Table 1 needs (one long row per group x variable [x level]).

    ``binary`` columns (True/False or 1/0) report the count of true values as one row; ``categorical`` columns report
    one row per level; ``continuous`` columns report n, n_missing, mean and SD. Missing group values form their own
    ``"(missing)"`` group so counts always add up.
    """
    for c in list(continuous) + list(categorical) + list(binary) + ([group_col] if group_col else []):
        if c not in df.columns:
            raise KeyError(f"column not in the data: {c}")
    groups = (df[group_col].astype(object).where(df[group_col].notna(), "(missing)").astype(str) if group_col
              else pd.Series(_OVERALL, index=df.index))
    rows: list[dict[str, Any]] = []
    for g, d in df.groupby(groups, sort=True):
        for v in continuous:
            x = pd.to_numeric(d[v], errors="coerce")
            rows.append({"group": g, "variable": v, "kind": "continuous", "level": "", "n": int(x.notna().sum()),
                         "n_missing": int(x.isna().sum()), "mean": float(x.mean()) if x.notna().any() else np.nan,
                         "sd": float(x.std(ddof=1)) if x.notna().sum() > 1 else np.nan, "count": np.nan})
        for v in binary:
            x = d[v]
            truth = x.astype(float).fillna(0) > 0 if pd.api.types.is_numeric_dtype(x) or pd.api.types.is_bool_dtype(x) else x.astype(str).str.lower().isin(["true", "yes", "y", "1"])
            rows.append({"group": g, "variable": v, "kind": "binary", "level": "1", "n": int(x.notna().sum()),
                         "n_missing": int(x.isna().sum()), "mean": np.nan, "sd": np.nan, "count": int((truth & x.notna()).sum())})
        for v in categorical:
            x = d[v].dropna().astype(str)
            vc = x.value_counts()
            all_levels = sorted(df[v].dropna().astype(str).unique())      # every level appears in every group, as a zero if absent
            for lev in all_levels:
                rows.append({"group": g, "variable": v, "kind": "categorical", "level": lev, "n": int(len(x)),
                             "n_missing": int(d[v].isna().sum()), "mean": np.nan, "sd": np.nan, "count": int(vc.get(lev, 0))})
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def pool_table1_summaries(summaries: Mapping[str, pd.DataFrame] | Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Pools site summaries into one (same schema). Counts add; continuous mean and SD are pooled exactly."""
    frames = list(summaries.values()) if isinstance(summaries, Mapping) else list(summaries)
    if not frames:
        raise ValueError("no site summaries given")
    allrows = pd.concat(frames, ignore_index=True)
    missing = [c for c in SUMMARY_COLUMNS if c not in allrows.columns]
    if missing:
        raise KeyError(f"summary is missing columns: {missing}")
    out = []
    for (g, v, kind, lev), d in allrows.groupby(["group", "variable", "kind", "level"], sort=False, dropna=False):
        if d["n"].isna().any() or (kind != "continuous" and d["count"].isna().any()):
            # a site hid this cell (small count): the pooled value cannot be computed from what is left
            out.append({"group": g, "variable": v, "kind": kind, "level": lev, "n": np.nan, "n_missing": np.nan,
                        "mean": np.nan, "sd": np.nan, "count": np.nan})
            continue
        n = d["n"].sum()
        rec = {"group": g, "variable": v, "kind": kind, "level": lev, "n": int(n), "n_missing": int(d["n_missing"].sum()),
               "mean": np.nan, "sd": np.nan, "count": np.nan}
        if kind == "continuous":
            ok = d[d["n"] > 0]
            if n > 0:
                m = float((ok["n"] * ok["mean"]).sum() / n)
                ss = float(((ok["n"] - 1).clip(lower=0) * ok["sd"].fillna(0) ** 2 + ok["n"] * ok["mean"] ** 2).sum())
                rec["mean"] = m
                rec["sd"] = float(np.sqrt(max((ss - n * m * m) / (n - 1), 0.0))) if n > 1 else np.nan
        else:
            rec["count"] = int(d["count"].sum())
        out.append(rec)
    return pd.DataFrame(out, columns=SUMMARY_COLUMNS)


def _smd_cont(m1, s1, m0, s0):
    if any(pd.isna(x) for x in (m1, m0)):
        return np.nan
    pooled = np.sqrt((np.nan_to_num(s1) ** 2 + np.nan_to_num(s0) ** 2) / 2)
    return (m1 - m0) / pooled if pooled > 0 else (0.0 if m1 == m0 else np.nan)


def _smd_prop(p1, p0):
    d = np.sqrt((p1 * (1 - p1) + p0 * (1 - p0)) / 2)
    return (p1 - p0) / d if d > 0 else (0.0 if p1 == p0 else np.nan)


def build_table1(summary: pd.DataFrame, *, group_order: Sequence[str] | None = None, overall: bool = True, smd: bool = True,
                 min_cell: int | None = DEFAULT_MIN_CELL, digits: int = 1) -> pd.DataFrame:
    """Formats a (pooled) summary as Table 1.

    Columns: ``variable, level`` then one column per group (``"mean (SD)"`` or ``"n (%)"``), an ``Overall`` column when
    there is more than one group, and ``SMD`` between the first two groups when ``smd=True`` and there are exactly two.
    For a multi-level categorical variable the SMD is on the variable's row: the largest absolute level-wise SMD.
    The numeric table is kept in ``result.attrs["numeric"]``. With ``min_cell`` a count from 1 to ``min_cell - 1`` is shown
    as ``<min_cell`` and its percentage and SMD are blanked; a continuous cell built on fewer than ``min_cell`` values is blanked.
    """
    groups = list(group_order) if group_order is not None else list(dict.fromkeys(summary["group"]))
    unknown = [g for g in groups if g not in set(summary["group"])]
    if unknown:
        raise KeyError(f"groups not in the summary: {unknown}")
    if smd and len(groups) > 2:
        raise ValueError("SMD is defined between two groups; pass group_order with two groups or smd=False")
    use_smd = smd and len(groups) == 2
    mc = min_cell if (min_cell and min_cell > 1) else None

    s = summary[summary["group"].isin(groups)].copy()
    # group denominators for percentages (non-missing n of the variable within the group)
    num = s.copy()
    num["pct"] = np.where(num["kind"] != "continuous", 100.0 * num["count"] / num["n"].replace(0, np.nan), np.nan)
    if mc:
        cnt = num["kind"] != "continuous"
        sub = suppress_small_counts(num.loc[cnt], ["count"], mc, also=["pct"], group_by=["group", "variable"], complementary=True)
        num.loc[cnt, ["count", "pct"]] = sub[["count", "pct"]].values
        num["hidden"] = False
        num.loc[cnt, "hidden"] = sub["suppressed"].values
        cont_small = (num["kind"] == "continuous") & (((num["n"] > 0) & (num["n"] < mc)) | num["n"].isna())
        num.loc[cnt & num["count"].isna(), "hidden"] = True
        num.loc[cont_small, ["mean", "sd"]] = np.nan
        num.loc[cont_small, "hidden"] = True
    else:
        num["hidden"] = False

    keyed = num.set_index(["variable", "level", "group"])
    order = list(dict.fromkeys(zip(summary["variable"], summary["level"], summary["kind"])))
    rows, numeric = [], []
    for variable, level, kind in order:
        rec: dict[str, Any] = {"variable": variable, "level": level if kind == "categorical" else ""}
        nrec: dict[str, Any] = dict(rec)
        for g in groups + ([_OVERALL] if overall and len(groups) > 1 else []):
            if g == _OVERALL:
                d = s[(s.variable == variable) & (s.level == level)]
                pooled = pool_table1_summaries([d.assign(group=_OVERALL)]).iloc[0]
                if mc and kind == "continuous":
                    pooled_hidden = 0 < pooled["n"] < mc
                    pm, ps = (np.nan, np.nan) if pooled_hidden else (pooled["mean"], pooled["sd"])
                    cell = "" if pooled_hidden else f"{pm:.{digits}f} ({ps:.{digits}f})" if pd.notna(ps) else f"{pm:.{digits}f}"
                    nrec[g] = pm
                elif kind == "continuous":
                    cell = f"{pooled['mean']:.{digits}f} ({pooled['sd']:.{digits}f})" if pd.notna(pooled["sd"]) else (f"{pooled['mean']:.{digits}f}" if pd.notna(pooled["mean"]) else "")
                    nrec[g] = pooled["mean"]
                else:
                    # an overall count is only safe when every group count is shown (else subtraction)
                    grp_hidden = any(keyed.loc[(variable, level, gg), "hidden"] for gg in groups if (variable, level, gg) in keyed.index) if mc else False
                    tot = pooled["count"]
                    if mc and (grp_hidden or 0 < tot < mc):
                        cell = f"<{mc}"
                        nrec[g] = np.nan
                    else:
                        cell = f"{int(tot):,} ({100.0 * tot / pooled['n']:.{digits}f}%)" if pooled["n"] else ""
                        nrec[g] = tot
                rec[g] = cell
                continue
            if (variable, level, g) not in keyed.index:
                rec[g], nrec[g] = "", np.nan
                continue
            r = keyed.loc[(variable, level, g)]
            if kind == "continuous":
                nrec[g] = r["mean"]
                rec[g] = "" if pd.isna(r["mean"]) else (f"{r['mean']:.{digits}f} ({r['sd']:.{digits}f})" if pd.notna(r["sd"]) else f"{r['mean']:.{digits}f}")
            else:
                nrec[g] = r["count"]
                rec[g] = f"<{mc}" if (mc and pd.isna(r["count"])) else f"{int(r['count']):,} ({r['pct']:.{digits}f}%)"
        if use_smd:
            a, b = groups
            try:
                ra, rb = keyed.loc[(variable, level, a)], keyed.loc[(variable, level, b)]
                hidden = bool(ra["hidden"] or rb["hidden"])
                if kind == "continuous":
                    val = _smd_cont(ra["mean"], ra["sd"], rb["mean"], rb["sd"])
                else:
                    val = _smd_prop(ra["count"] / ra["n"], rb["count"] / rb["n"]) if (ra["n"] and rb["n"] and not hidden) else np.nan
            except KeyError:
                val, hidden = np.nan, True
            nrec["SMD"] = np.nan if hidden else val
            rec["SMD"] = "" if (hidden or pd.isna(val)) else f"{val:.3f}"
        rows.append(rec)
        numeric.append(nrec)
    # first row: the size of each group (non-missing + missing of any one variable), suppressed like any count
    first_var = summary["variable"].iloc[0]
    sizes = {g: float(summary[(summary.group == g) & (summary.variable == first_var)].drop_duplicates("level").iloc[0][["n", "n_missing"]].sum(min_count=2))
             for g in groups}
    if overall and len(groups) > 1:
        sizes[_OVERALL] = float(sum(sizes[g] for g in groups))      # NaN if any group size is unknown
    hide_any = bool(mc) and any((pd.isna(v) or 0 < v < mc) for v in sizes.values())
    hide = {g: bool(mc) and (pd.isna(v) or 0 < v < mc or hide_any) for g, v in sizes.items()}
    size_rec = {"variable": "N", "level": "", **{g: (f"<{mc}" if hide[g] else f"{int(v):,}") for g, v in sizes.items()}}
    size_num = {"variable": "N", "level": "", **{g: (np.nan if hide[g] else float(v)) for g, v in sizes.items()}}
    if use_smd:
        size_rec["SMD"], size_num["SMD"] = "", np.nan
    rows.insert(0, size_rec)
    numeric.insert(0, size_num)
    table = pd.DataFrame(rows)
    nt = pd.DataFrame(numeric)
    if use_smd and (nt["variable"].duplicated(keep=False)).any():
        # multi-level categorical: put the max |level SMD| on a variable header row, per the docstring; hidden if any level hidden
        out_rows, out_num = [], []
        for variable in dict.fromkeys(table["variable"]):
            idx = table.index[table["variable"] == variable]
            if len(idx) == 1:
                out_rows.append(table.loc[idx[0]].to_dict()); out_num.append(nt.loc[idx[0]].to_dict())
                continue
            levels_smd = [nt.at[i, "SMD"] for i in idx]
            head = {c: "" for c in table.columns}; head["variable"] = variable
            nhead = {c: np.nan for c in nt.columns}; nhead["variable"] = variable; nhead["level"] = ""
            if any(pd.isna(v) for v in levels_smd):
                head["SMD"] = ""
            else:
                m = float(np.max(np.abs(levels_smd)))
                head["SMD"], nhead["SMD"] = f"{m:.3f}", m
            out_rows.append(head); out_num.append(nhead)
            for i in idx:
                r = table.loc[i].to_dict(); r["SMD"] = ""; out_rows.append(r)
                n_ = nt.loc[i].to_dict(); n_["SMD"] = np.nan; out_num.append(n_)
        table, nt = pd.DataFrame(out_rows), pd.DataFrame(out_num)
    table.attrs["numeric"] = nt
    table.attrs["min_cell"] = mc
    return table
