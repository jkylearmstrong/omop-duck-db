"""Deterministic one-hot encoding of categorical model features from fixed level lists.

``pd.get_dummies(df, drop_first=True)`` derives its columns from the levels *present in the data it is given*, so the same
model sees different columns (and different reference levels) at a site that lacks a level, and a feature that is all
strings is silently lost if it goes through ``pd.to_numeric`` instead. A model release should therefore ship the level
list of every categorical feature and encode with :func:`encode_categoricals`, which always produces the same columns in
the same order and names them like ``get_dummies`` (``<feature>_<level>``), so existing feature names keep working.

Spec shape (``feature_spec.json["categorical_levels"]``)::

    {"sex": {"levels": ["Female", "Male", "Other"], "reference": "Female"}, ...}

``reference`` is the level that gets no column (``null`` keeps every level). Missing values encode as all zeros, which is
indistinguishable from the reference level; give "missing" its own level (as the SDI/RUCA labels do) when it matters.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

__all__ = ["encoded_column_names", "encode_categoricals"]


def _norm(levels: Mapping[str, Any]) -> dict[str, tuple[list[str], str | None]]:
    out = {}
    for col, v in levels.items():
        lv = [str(x) for x in (v["levels"] if isinstance(v, Mapping) else v)]
        ref = v.get("reference") if isinstance(v, Mapping) else None
        if len(set(lv)) != len(lv) or not lv:
            raise ValueError(f"levels for {col!r} must be non-empty and unique.")
        if ref is not None and str(ref) not in lv:
            raise ValueError(f"reference {ref!r} is not one of the levels of {col!r}: {lv}.")
        out[col] = (lv, None if ref is None else str(ref))
    return out


def encoded_column_names(levels: Mapping[str, Any]) -> list[str]:
    """The dummy columns :func:`encode_categoricals` produces, in order."""
    return [f"{c}_{lv}" for c, (ls, ref) in _norm(levels).items() for lv in ls if lv != ref]


def encode_categoricals(df: pd.DataFrame, levels: Mapping[str, Any], *, unknown: str = "error", keep: bool = False) -> pd.DataFrame:
    """Replace each categorical column of ``df`` by its fixed 0/1 dummies (float32).

    ``unknown='error'`` raises on a value outside the level list; ``'zero'`` encodes it like a missing value. A listed
    column absent from ``df`` raises. ``keep=True`` keeps the original string columns too.
    """
    if unknown not in ("error", "zero"):
        raise ValueError("unknown must be 'error' or 'zero'.")
    spec = _norm(levels)
    missing = [c for c in spec if c not in df.columns]
    if missing:
        raise KeyError(f"categorical column(s) missing from the frame: {missing}")
    blocks = {}
    for col, (ls, ref) in spec.items():
        s = df[col].astype("string").str.strip()
        bad = s[s.notna() & ~s.isin(ls)]
        if len(bad) and unknown == "error":
            raise ValueError(f"{col}: {bad.nunique()} value(s) not in the model's levels, e.g. {sorted(bad.unique())[:5]}; levels are {ls}.")
        for lv in ls:
            if lv != ref:
                blocks[f"{col}_{lv}"] = (s == lv).fillna(False).to_numpy(dtype=np.float32)
    rest = df if keep else df.drop(columns=list(spec))
    return pd.concat([rest, pd.DataFrame(blocks, index=df.index)], axis=1)
