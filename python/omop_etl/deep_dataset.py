"""Multimodal deep-learning datasets, a frozen concept vocabulary, intra-stay tokens and self-supervised targets (v0.13.0).

* :func:`extract_multimodal_deep_dataset` binds the prior-visit concept tensor of :func:`extract_sard_visit_tensors` to the
  dense benchmark covariates and the label, optionally adds intra-admission trajectory tokens and BERT-style
  masked-concept / next-visit targets, and can stream a large cohort to disk in chunks (``.npy`` memory maps or Arrow
  fixed-size nested lists) instead of holding ``(N, V, L)`` in memory.
* :func:`export_concept_vocabulary` / :func:`import_concept_vocabulary` freeze a tokenizer so every site uses the same
  token ids.
* :func:`extract_intra_stay_tokens` tokenizes what happens between admission and discharge, never after discharge.

Token layout: ids 0-4 are ``[BOS] [EOS] [SEP] [PAD] [UNK]`` (PAD = 3), concepts start at 5. The ``[MASK]`` token is
``vocab_size`` (one past the last concept), so a model needs an embedding of ``vocab_size + 1`` rows.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

from omop_etl.ml_features import (
    N_SPECIAL_TOKENS, PAD_TOKEN_IDX, SPECIAL_TOKENS, UNK_TOKEN_IDX, build_concept_tokenizer, extract_sard_visit_tensors,
    extract_sparse_concept_matrix,
)

__all__ = ["extract_multimodal_deep_dataset", "extract_intra_stay_tokens", "extract_intra_stay_measurements", "export_concept_vocabulary",
           "import_concept_vocabulary", "make_pretraining_targets"]

VOCABULARY_FORMAT = "omop-duck-db/concept-vocabulary"
_BLOCK = 1024            # rows per random-number block: makes masks independent of the chunk size
IGNORE_INDEX = -100      # label value of positions that are not predicted


# ---------------------------------------------------------------------------------------------------------------------
# Frozen vocabulary
# ---------------------------------------------------------------------------------------------------------------------
def _digest(tokens: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()


def export_concept_vocabulary(tokenizer: Any, path: str | Path, *, name: str = "history") -> dict[str, Any]:
    """Write a tokenizer as a JSON manifest a lead site distributes to the other sites.

    The manifest holds the ordered token list (specials first), its SHA-256 and the vocabulary size, and nothing about
    patients. Returns the manifest dict.
    """
    tokens = list(tokenizer.concept_list)
    if tuple(tokens[:N_SPECIAL_TOKENS]) != SPECIAL_TOKENS:
        raise ValueError(f"Unexpected special tokens {tokens[:N_SPECIAL_TOKENS]}; expected {list(SPECIAL_TOKENS)}.")
    from omop_etl import __version__
    manifest = {"format": VOCABULARY_FORMAT, "format_version": 1, "name": name, "omop_duck_db_version": __version__,
                "vocab_size": len(tokens), "n_special_tokens": N_SPECIAL_TOKENS, "pad_token_idx": PAD_TOKEN_IDX,
                "unk_token_idx": UNK_TOKEN_IDX, "mask_token_id": len(tokens), "sha256": _digest(tokens), "tokens": tokens}
    Path(path).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def import_concept_vocabulary(path: str | Path) -> Any:
    """Rebuild the tokenizer of a manifest written by :func:`export_concept_vocabulary`, with identical token ids.

    Raises ``ValueError`` when the file is not a vocabulary manifest, its checksum does not match its tokens, or the
    rebuilt tokenizer would assign different ids (for example a different omop-learn version).
    """
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    if m.get("format") != VOCABULARY_FORMAT:
        raise ValueError(f"{str(path)!r} is not a concept vocabulary manifest.")
    tokens = m["tokens"]
    if _digest(tokens) != m.get("sha256") or len(tokens) != m.get("vocab_size"):
        raise ValueError(f"{str(path)!r} is corrupted: the token list does not match its checksum.")
    tok = build_concept_tokenizer(tokens[N_SPECIAL_TOKENS:])
    if list(tok.concept_list) != tokens:
        raise ValueError("The rebuilt tokenizer assigns different token ids than the manifest; refusing to continue.")
    return tok


# ---------------------------------------------------------------------------------------------------------------------
# Self-supervised targets
# ---------------------------------------------------------------------------------------------------------------------
def make_pretraining_targets(concept_tensor: np.ndarray, n_visits: np.ndarray, *, vocab_size: int, mask_token_id: int | None = None,
                             mask_prob: float = 0.15, seed: int = 20261104, row_offset: int = 0) -> dict[str, np.ndarray]:
    """Masked-concept and next-visit targets for ``concept_tensor`` (N, V, L).

    ``masked_concept_tensor``: of the non-padding positions each is selected with probability ``mask_prob``; a selected
    position becomes ``[MASK]`` 80% of the time, a random concept 10% and stays as is 10% (BERT). ``mlm_labels`` holds the
    original id at selected positions and -100 elsewhere. ``next_visit_flag[i, v]`` is 1 when row ``i`` has a visit after
    visit ``v`` inside the window (the next-visit prediction pair is valid).

    Draws are made in blocks of 1024 absolute rows (``row_offset`` must be a multiple of 1024), so the same seed gives
    the same masks however the cohort is chunked.
    """
    if not 0.0 < mask_prob < 1.0:
        raise ValueError("mask_prob must be in (0, 1).")
    if row_offset % _BLOCK:
        raise ValueError(f"row_offset must be a multiple of {_BLOCK}.")
    mask_id = int(vocab_size if mask_token_id is None else mask_token_id)
    n, v, l = concept_tensor.shape
    masked = concept_tensor.copy()
    labels = np.full(concept_tensor.shape, IGNORE_INDEX, dtype=concept_tensor.dtype)
    for b in range(0, n, _BLOCK):
        rng = np.random.default_rng([int(seed), row_offset + b])
        rows = min(_BLOCK, n - b)
        u = rng.random((_BLOCK, v, l))[:rows]
        kind = rng.random((_BLOCK, v, l))[:rows]
        rand_tok = rng.integers(N_SPECIAL_TOKENS, max(vocab_size, N_SPECIAL_TOKENS + 1), size=(_BLOCK, v, l))[:rows]
        block = concept_tensor[b:b + rows]
        sel = (block >= N_SPECIAL_TOKENS) & (u < mask_prob)
        labels[b:b + rows][sel] = block[sel]
        out = masked[b:b + rows]
        out[sel & (kind < 0.8)] = mask_id
        swap = sel & (kind >= 0.8) & (kind < 0.9)
        out[swap] = rand_tok[swap]
    slots = np.arange(v)[None, :]
    next_flag = (slots + 1 < np.asarray(n_visits)[:, None]).astype(np.int8)
    return {"masked_concept_tensor": masked, "mlm_labels": labels, "next_visit_flag": next_flag}


# ---------------------------------------------------------------------------------------------------------------------
# Intra-admission trajectory
# ---------------------------------------------------------------------------------------------------------------------
def extract_intra_stay_tokens(con: duckdb.DuckDBPyConnection, features_df: pd.DataFrame, *, max_days: int = 14,
                              max_tokens_per_day: int = 30, tokenizer: Any = None, trend_threshold: float = 0.10,
                              include: Sequence[str] = ("lab", "drug", "procedure")) -> dict[str, Any]:
    """Tokenize the stay itself, day by day, from admission to discharge (inclusive) and never beyond.

    ``features_df`` needs ``person_id``, ``index_date`` (admission) and ``discharge_date``. Tokens per stay day:
    ``"<concept> - lab:L|N|H"`` (the day's mean against the measurement's own reference range, ``N`` without a range),
    ``"<concept> - labtrend:up|down"`` (day mean changed by more than ``trend_threshold`` against the previous day with a
    value), ``"<concept> - drug"`` (exposure started that day) and ``"<concept> - procedure"``.

    Returns ``intra_tensor`` (N, max_days, max_tokens_per_day) padded with 3, ``intra_day`` (N, max_days) day offset from
    admission (-1 padding), ``intra_lengths``, ``tokenizer`` (built from these tokens unless one is passed; unseen
    tokens map to ``[UNK]``). Stays longer than ``max_days`` keep their first ``max_days`` days.
    """
    need = {"person_id", "index_date", "discharge_date"} - set(features_df.columns)
    if need:
        raise ValueError(f"features_df is missing {sorted(need)}.")
    bad = set(include) - {"lab", "drug", "procedure"}
    if bad:
        raise ValueError(f"Unknown intra-stay event kinds {sorted(bad)}.")
    n = len(features_df)
    stays = pd.DataFrame({"row_idx": np.arange(n, dtype="int64"), "person_id": features_df["person_id"].astype("int64").to_numpy(),
                          "admit": pd.to_datetime(features_df["index_date"]).to_numpy(),
                          "disch": pd.to_datetime(features_df["discharge_date"]).to_numpy()})
    parts = []
    if "lab" in include:
        parts.append("""
            SELECT row_idx, day, CAST(concept_id AS VARCHAR) || ' - lab:' || flag AS tok FROM lab_day
            UNION ALL SELECT row_idx, day, CAST(concept_id AS VARCHAR) || ' - labtrend:' || CASE WHEN chg > 0 THEN 'up' ELSE 'down' END
            FROM (SELECT *, mean_v / NULLIF(LAG(mean_v) OVER (PARTITION BY row_idx, concept_id ORDER BY day), 0) - 1 AS chg FROM lab_day)
            WHERE chg IS NOT NULL AND abs(chg) > ?""")
    if "drug" in include:
        parts.append("""
            SELECT s.row_idx, CAST(d.drug_exposure_start_date AS DATE) - CAST(s.admit AS DATE) AS day, CAST(d.drug_concept_id AS VARCHAR) || ' - drug' AS tok
            FROM stays s JOIN drug_exposure d ON d.person_id = s.person_id AND d.drug_concept_id <> 0
                 AND d.drug_exposure_start_date BETWEEN CAST(s.admit AS DATE) AND CAST(s.disch AS DATE)""")
    if "procedure" in include:
        parts.append("""
            SELECT s.row_idx, CAST(p.procedure_date AS DATE) - CAST(s.admit AS DATE) AS day, CAST(p.procedure_concept_id AS VARCHAR) || ' - procedure' AS tok
            FROM stays s JOIN procedure_occurrence p ON p.person_id = s.person_id AND p.procedure_concept_id <> 0
                 AND p.procedure_date BETWEEN CAST(s.admit AS DATE) AND CAST(s.disch AS DATE)""")
    params: list[Any] = [float(trend_threshold)] if "lab" in include else []
    sql = """
        WITH lab_day AS (
            SELECT s.row_idx, CAST(m.measurement_date AS DATE) - CAST(s.admit AS DATE) AS day, m.measurement_concept_id AS concept_id,
                   avg(m.value_as_number) AS mean_v,
                   CASE WHEN avg(m.value_as_number) < any_value(m.range_low) THEN 'L'
                        WHEN avg(m.value_as_number) > any_value(m.range_high) THEN 'H' ELSE 'N' END AS flag
            FROM stays s JOIN measurement m ON m.person_id = s.person_id AND m.measurement_concept_id <> 0
                 AND m.value_as_number IS NOT NULL
                 AND m.measurement_date BETWEEN CAST(s.admit AS DATE) AND CAST(s.disch AS DATE)
            GROUP BY ALL)
        SELECT DISTINCT row_idx, day, tok FROM (""" + " UNION ALL ".join(f"({p})" for p in parts) + ") WHERE day >= 0 AND day < ?"
    params.append(int(max_days))
    con.register("stays", stays)
    try:
        ev = con.execute(sql, params).df() if parts else pd.DataFrame(columns=["row_idx", "day", "tok"])
    finally:
        con.unregister("stays")
    if tokenizer is None:
        tokenizer = build_concept_tokenizer(sorted(ev["tok"].unique().tolist()))
    ids = tokenizer.concept_map
    ev["token_id"] = ev["tok"].map(lambda t: ids.get(t, UNK_TOKEN_IDX)).astype("int64")
    ev = ev.sort_values(["row_idx", "day", "token_id"], kind="stable").reset_index(drop=True)
    tensor = np.full((n, int(max_days), int(max_tokens_per_day)), PAD_TOKEN_IDX, dtype=np.int32)
    day_of = np.full((n, int(max_days)), -1, dtype=np.int32)
    lengths = np.zeros((n, int(max_days)), dtype=np.int32)
    if len(ev):
        r, d = ev["row_idx"].to_numpy(), ev["day"].to_numpy()
        new = np.r_[True, (r[1:] != r[:-1]) | (d[1:] != d[:-1])]
        pos = np.arange(len(ev)) - np.maximum.accumulate(np.where(new, np.arange(len(ev)), 0))
        keep = pos < max_tokens_per_day
        tensor[r[keep], d[keep], pos[keep]] = ev["token_id"].to_numpy()[keep]
        np.add.at(lengths, (r[keep], d[keep]), 1)
        day_of[r, d] = d
    return {"intra_tensor": tensor, "intra_day": day_of, "intra_lengths": lengths, "tokenizer": tokenizer}


def extract_intra_stay_measurements(con: duckdb.DuckDBPyConnection, features_df: pd.DataFrame, *, concept_ids: Sequence[int] | None = None,
                                    top_concepts: int = 50, max_days: int = 14, max_obs_per_day: int = 20,
                                    normalization: dict[int, tuple[float, float]] | None = None) -> dict[str, Any]:
    """Continuous in-stay measurement arrays for time-series models (GRU-D, continuous-time transformers, numeric heads).

    ``intra_obs[i, d, k] = [time_offset, concept_idx, normalized_value]`` for the ``k``-th numeric measurement (time order)
    on stay day ``d`` of row ``i``: ``time_offset`` is days since the admission date (fractional when the measurement has a
    time), ``concept_idx`` is 1..K into ``concepts`` (0 = padding), ``normalized_value`` the z-score with the concept's
    ``normalization`` (mean, sd). Padding is ``[-1, 0, 0]`` and ``intra_obs_mask`` is False there. Only measurements dated
    from the admission to the discharge date are used. Stays longer than ``max_days`` keep their first days, days with more
    than ``max_obs_per_day`` keep the earliest observations.

    Parameters
    ----------
    features_df : needs ``person_id``, ``index_date`` (admission) and ``discharge_date``.
    concept_ids : measurement concept ids to include (frozen list shared across sites); default the ``top_concepts`` most
        frequent numeric concepts inside these stays.
    normalization : ``{concept_id: (mean, sd)}``. When ``None`` it is computed from these rows: for training / test use,
        take it from the training table (returned as ``normalization``) and pass it to the other tables.
    """
    need = {"person_id", "index_date", "discharge_date"} - set(features_df.columns)
    if need:
        raise ValueError(f"features_df is missing {sorted(need)}.")
    n = len(features_df)
    stays = pd.DataFrame({"row_idx": np.arange(n, dtype="int64"), "person_id": features_df["person_id"].astype("int64").to_numpy(),
                          "admit": pd.to_datetime(features_df["index_date"]).to_numpy(),
                          "disch": pd.to_datetime(features_df["discharge_date"]).to_numpy()})
    con.register("stays", stays)
    try:
        ev = con.execute("""
            SELECT s.row_idx, m.measurement_concept_id AS concept_id, m.value_as_number AS value,
                   epoch(COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP)) - CAST(CAST(s.admit AS DATE) AS TIMESTAMP)) / 86400.0 AS t
            FROM stays s JOIN measurement m ON m.person_id = s.person_id AND m.measurement_concept_id <> 0
                 AND m.value_as_number IS NOT NULL AND isfinite(m.value_as_number)
                 AND m.measurement_date BETWEEN CAST(s.admit AS DATE) AND CAST(s.disch AS DATE)""").df()
    finally:
        con.unregister("stays")
    if concept_ids is None:
        concept_ids = ev["concept_id"].value_counts().head(int(top_concepts)).index.tolist() if len(ev) else []
    concepts = [int(c) for c in concept_ids]
    idx_of = {c: i + 1 for i, c in enumerate(concepts)}
    ev = ev[ev["concept_id"].isin(idx_of)].copy()
    if normalization is None:
        g = ev.groupby("concept_id")["value"]
        normalization = {int(c): (float(m), float(sd) if sd and sd > 0 else 1.0) for c, m, sd in zip(g.mean().index, g.mean(), g.std(ddof=0))}
    mean = ev["concept_id"].map(lambda c: normalization.get(int(c), (0.0, 1.0))[0]).astype("float64")
    sd = ev["concept_id"].map(lambda c: normalization.get(int(c), (0.0, 1.0))[1]).astype("float64")
    ev["z"] = (ev["value"] - mean) / sd.where(sd > 0, 1.0)
    ev["cidx"] = ev["concept_id"].map(idx_of).astype("int64")
    ev["day"] = np.floor(ev["t"]).clip(lower=0).astype("int64")
    ev = ev[ev["day"] < int(max_days)].sort_values(["row_idx", "t", "cidx"], kind="stable").reset_index(drop=True)
    obs = np.zeros((n, int(max_days), int(max_obs_per_day), 3), dtype="float32")
    obs[..., 0] = -1.0
    mask = np.zeros((n, int(max_days), int(max_obs_per_day)), dtype=bool)
    if len(ev):
        r, d = ev["row_idx"].to_numpy(), ev["day"].to_numpy()
        order = np.lexsort((np.arange(len(ev)), d, r))      # position within (row, day) in time order
        r, d = r[order], d[order]
        e = ev.iloc[order].reset_index(drop=True)
        new = np.r_[True, (r[1:] != r[:-1]) | (d[1:] != d[:-1])]
        pos = np.arange(len(e)) - np.maximum.accumulate(np.where(new, np.arange(len(e)), 0))
        keep = pos < int(max_obs_per_day)
        rr, dd, pp = r[keep], d[keep], pos[keep]
        obs[rr, dd, pp, 0] = e["t"].to_numpy()[keep]
        obs[rr, dd, pp, 1] = e["cidx"].to_numpy()[keep]
        obs[rr, dd, pp, 2] = e["z"].to_numpy()[keep]
        mask[rr, dd, pp] = True
    return {"intra_obs": obs, "intra_obs_mask": mask, "concepts": concepts, "normalization": normalization}


# ---------------------------------------------------------------------------------------------------------------------
# The multimodal dataset
# ---------------------------------------------------------------------------------------------------------------------
_KEY_COLS = {"person_id", "visit_occurrence_id", "index_date", "discharge_date", "y", "site", "site_anon"}


def _dense_matrix(features_df: pd.DataFrame, dense_columns: Sequence[str] | None, label_col: str | None) -> tuple[np.ndarray, list[str]]:
    if dense_columns is None:
        dense_columns = [c for c in features_df.columns
                         if c not in _KEY_COLS and c != label_col and pd.api.types.is_numeric_dtype(features_df[c])]
    else:
        missing = [c for c in dense_columns if c not in features_df.columns]
        if missing:
            raise ValueError(f"dense_columns not in features_df: {missing}")
    arr = features_df[list(dense_columns)].to_numpy(dtype="float32") if len(dense_columns) else np.zeros((len(features_df), 0), "float32")
    return arr, list(dense_columns)


def _to_torch(d: dict[str, Any]) -> dict[str, Any]:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - exercised only without torch
        raise ImportError("output_format='torch_dict' needs PyTorch (pip install torch); 'numpy_dict' needs nothing extra.") from exc
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, np.ndarray) and v.dtype != object:
            t = torch.from_numpy(np.ascontiguousarray(v))
            if k in ("concept_tensor", "masked_concept_tensor", "mlm_labels", "intra_tensor", "person_id"):
                t = t.long()
            elif k == "y":
                t = t.float()      # BCEWithLogitsLoss and friends need a float32 target
            out[k] = t
        else:
            out[k] = v
    return out


def _arrow_nested(arr: np.ndarray):
    import pyarrow as pa
    inner = pa.FixedSizeListArray.from_arrays(pa.array(arr.reshape(-1)), arr.shape[2])
    return pa.FixedSizeListArray.from_arrays(inner, arr.shape[1])


def extract_multimodal_deep_dataset(
    con: duckdb.DuckDBPyConnection,
    features_df: pd.DataFrame,
    *,
    max_visits: int = 20,
    max_tokens_per_visit: int = 30,
    lookback_days: int = 365,
    domains: Sequence[str] = ("condition", "drug"),
    dense_columns: Sequence[str] | None = None,
    label_col: str | None = "y",
    output_format: str = "numpy_dict",
    tokenizer: Any = None,
    min_patient_freq: int = 1,
    include_intra_stay: bool = False,
    intra_max_days: int = 14,
    intra_max_tokens_per_day: int = 30,
    intra_tokenizer: Any = None,
    pretraining_targets: bool = False,
    mask_prob: float = 0.15,
    seed: int = 20261104,
    output_dir: str | Path | None = None,
    chunk_size: int = 50_000,
) -> dict[str, Any]:
    """Multimodal tensors for a model that fuses visit-token sequences with dense clinical covariates.

    Parameters
    ----------
    features_df : pandas.DataFrame
        The table from :func:`extract_benchmark_features` (needs ``person_id`` and ``index_date``; ``discharge_date`` for
        ``include_intra_stay``). Row ``i`` of every output is row ``i`` of this frame.
    max_visits, max_tokens_per_visit, lookback_days, domains
        Prior-visit tensor shape and window (visits strictly before the admission, newest ``max_visits`` kept).
    dense_columns : sequence of str, optional
        Dense covariates; default every numeric non-key column. Missing values are 0 in ``dense_covariates`` and flagged
        in ``dense_missing``; no scaling is applied (fit it on the training sites).
    tokenizer : ConceptTokenizer, optional
        A frozen vocabulary (:func:`import_concept_vocabulary`): concepts outside it are dropped, so token ids match across sites.
    include_intra_stay : bool
        Adds ``intra_tensor`` / ``intra_day`` / ``intra_lengths`` (see :func:`extract_intra_stay_tokens`).
    pretraining_targets : bool
        Adds ``masked_concept_tensor``, ``mlm_labels``, ``next_visit_flag`` (:func:`make_pretraining_targets`).
    output_format : {"numpy_dict", "torch_dict", "memmap", "arrow", "zarr"}
        ``torch_dict``: ``{'concept_tensor', 'times', 'dense_covariates', 'y', 'person_id', ...}`` as torch tensors
        (concept ids as int64). ``memmap``: chunks are written to ``output_dir`` as ``.npy`` files opened with
        ``mmap_mode='r'`` (never more than ``chunk_size`` rows in memory). ``arrow``: ``output_dir/part-*.parquet`` with
        fixed-size nested list columns; returns the file list. ``zarr``: one chunked ``<array>.zarr`` per output (needs
        the optional ``zarr`` package, v2 or v3), returned as lazily read zarr arrays.
    chunk_size : int
        Rows per chunk for ``memmap`` / ``arrow`` (rounded up to a multiple of 1024).

    Returns
    -------
    dict
        ``concept_tensor`` (N, V, L), ``times`` (N, V; days since 1900-01-01, -1 padding), ``visit_days_before_index``,
        ``visit_lengths``, ``n_visits``, ``dense_covariates`` (N, F), ``dense_missing``, ``dense_columns``, ``y``,
        ``person_id``, ``tokenizer``, ``vocab_size``, ``mask_token_id`` (+ optional blocks above).
    """
    fmt = output_format.lower().strip()
    if fmt not in ("numpy_dict", "torch_dict", "memmap", "arrow", "zarr"):
        raise ValueError("output_format must be 'numpy_dict', 'torch_dict', 'memmap', 'arrow' or 'zarr'.")
    if fmt == "zarr":
        try:
            import zarr  # noqa: F401
        except ImportError as exc:
            raise ImportError("output_format='zarr' needs the zarr package (pip install zarr).") from exc
    need = {"person_id", "index_date"} - set(features_df.columns)
    if need:
        raise ValueError(f"features_df is missing {sorted(need)}.")
    if fmt in ("memmap", "arrow", "zarr") and output_dir is None:
        raise ValueError(f"output_format={fmt!r} needs output_dir.")
    if include_intra_stay and "discharge_date" not in features_df.columns:
        raise ValueError("include_intra_stay=True needs a discharge_date column.")
    n = len(features_df)
    features_df = features_df.reset_index(drop=True)
    dense, dense_cols = _dense_matrix(features_df, dense_columns, label_col)
    dense_missing = np.isnan(dense).astype("int8")
    dense = np.nan_to_num(dense, nan=0.0).astype("float32")
    y = features_df[label_col].to_numpy() if label_col and label_col in features_df.columns else None
    person = features_df["person_id"].to_numpy("int64")
    chunk = max(_BLOCK, -(-int(chunk_size) // _BLOCK) * _BLOCK) if fmt in ("memmap", "arrow", "zarr") else max(_BLOCK, -(-n // _BLOCK) * _BLOCK)

    sard_kw = dict(lookback_days=lookback_days, min_patient_freq=min_patient_freq, domains=tuple(domains),
                   max_nvisits=max_visits, max_visit_len=max_tokens_per_visit, dtype=np.int32,
                   person_col="person_id", index_date_col="index_date")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        def _parquet(df: pd.DataFrame, name: str) -> str:
            path = tmp_dir / name
            keep = df[["person_id", "index_date"]].copy()
            keep["index_date"] = pd.to_datetime(keep["index_date"])
            keep.to_parquet(path, index=False)
            return str(path)

        if tokenizer is None and n > chunk:           # one vocabulary for every chunk
            pre = extract_sparse_concept_matrix(con, _parquet(features_df, "all.parquet"), lookback_days=lookback_days,
                                                min_patient_freq=min_patient_freq, domains=tuple(domains),
                                                person_col="person_id", index_date_col="index_date")
            tokenizer = pre["tokenizer"]
            del pre

        def _chunk(start: int) -> dict[str, Any]:
            sub = features_df.iloc[start:start + chunk]
            res = extract_sard_visit_tensors(con, _parquet(sub, f"c{start}.parquet"), tokenizer=tokenizer, **sard_kw)
            nonlocal_tok = res["tokenizer"]
            block = {k: res[k] for k in ("concept_tensor", "times", "visit_days_before_index", "visit_lengths", "n_visits")}
            if include_intra_stay:
                intra = extract_intra_stay_tokens(con, sub, max_days=intra_max_days, max_tokens_per_day=intra_max_tokens_per_day,
                                                  tokenizer=intra_tokenizer)
                block.update({k: intra[k] for k in ("intra_tensor", "intra_day", "intra_lengths")})
                block["_intra_tokenizer"] = intra["tokenizer"]
            if pretraining_targets:
                block.update(make_pretraining_targets(block["concept_tensor"], block["n_visits"], vocab_size=nonlocal_tok.vocab_size,
                                                      mask_prob=mask_prob, seed=seed, row_offset=start))
            block["_tokenizer"] = nonlocal_tok
            return block

        def _attach(block: dict[str, Any], sl: slice) -> dict[str, Any]:
            block["dense_covariates"], block["dense_missing"] = dense[sl], dense_missing[sl]
            block["person_id"] = person[sl]
            if y is not None:
                block["y"] = y[sl]
            return block

        meta_common = {"max_visits": max_visits, "max_tokens_per_visit": max_tokens_per_visit, "lookback_days": lookback_days,
                       "n_rows": n, "dense_columns": dense_cols, "pad_token_idx": PAD_TOKEN_IDX}

        if fmt in ("numpy_dict", "torch_dict"):
            block = _chunk(0) if n else None
            if block is None:
                raise ValueError("features_df is empty.")
            tok = block.pop("_tokenizer")
            itok = block.pop("_intra_tokenizer", None)
            out = _attach(block, slice(0, n))
            out.update({"dense_columns": dense_cols, "tokenizer": tok, "vocab_size": tok.vocab_size, "mask_token_id": tok.vocab_size})
            if itok is not None:
                out["intra_tokenizer"] = itok
            return _to_torch(out) if fmt == "torch_dict" else out

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        mm: dict[str, Any] = {}
        tok = itok = None
        for start in range(0, n, chunk):
            sl = slice(start, min(start + chunk, n))
            block = _attach(_chunk(start), sl)
            tok, itok = block.pop("_tokenizer"), block.pop("_intra_tokenizer", itok)
            if fmt == "zarr":
                import zarr
                for k, v in block.items():
                    if k not in mm:
                        mm[k] = zarr.open_array(str(out_dir / f"{k}.zarr"), mode="w", shape=(n,) + v.shape[1:],
                                                chunks=(min(chunk, n),) + v.shape[1:], dtype=v.dtype)
                    mm[k][sl] = v
            elif fmt == "memmap":
                for k, v in block.items():
                    if k not in mm:
                        mm[k] = np.lib.format.open_memmap(out_dir / f"{k}.npy", mode="w+", dtype=v.dtype, shape=(n,) + v.shape[1:])
                    mm[k][sl] = v
            else:
                import pyarrow as pa
                import pyarrow.parquet as pq
                cols = {k: (_arrow_nested(v) if v.ndim == 3 else (pa.array(v) if v.ndim == 1 else
                        pa.FixedSizeListArray.from_arrays(pa.array(v.reshape(-1)), v.shape[1]))) for k, v in block.items()}
                path = out_dir / f"part-{start // chunk:05d}.parquet"
                pq.write_table(pa.table(cols), path)
                files.append(str(path))
        for m in mm.values():
            if hasattr(m, "flush"):
                m.flush()
        meta = {**meta_common, "format": fmt, "vocab_size": tok.vocab_size if tok else None,
                "mask_token_id": tok.vocab_size if tok else None, "chunk_size": chunk, "arrays": sorted(mm) if fmt in ("memmap", "zarr") else None,
                "files": [Path(f).name for f in files] or None}
        (out_dir / "manifest.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        if tok is not None:
            export_concept_vocabulary(tok, out_dir / "vocabulary.json")
        if itok is not None:
            export_concept_vocabulary(itok, out_dir / "intra_vocabulary.json", name="intra")
        if fmt == "memmap":
            res = {k: np.load(out_dir / f"{k}.npy", mmap_mode="r") for k in mm}
            res.update({"dense_columns": dense_cols, "tokenizer": tok, "vocab_size": tok.vocab_size, "mask_token_id": tok.vocab_size})
            return res
        if fmt == "zarr":
            import zarr
            res = {k: zarr.open_array(str(out_dir / f"{k}.zarr"), mode="r") for k in mm}
            res.update({"dense_columns": dense_cols, "tokenizer": tok, "vocab_size": tok.vocab_size, "mask_token_id": tok.vocab_size})
            return res
        return {"files": files, "manifest": str(out_dir / "manifest.json"), "tokenizer": tok, "vocab_size": tok.vocab_size,
                "mask_token_id": tok.vocab_size, "dense_columns": dense_cols}
