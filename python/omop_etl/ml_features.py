"""Set-based ML feature extraction: the DuckDB side of the omop-learn / OHDSI PLP bridge.

Where ``features.py`` produces dense, hand-engineered covariates, this module produces the two
*representation* families that omop-learn, scikit-learn and sequence models consume:

* ``extract_sparse_concept_matrix``: a ``scipy.sparse.csr_matrix`` of concept counts inside a
  per-row lookback window (LASSO / omop-learn windowed models / PLP-style sparse covariates).
* ``extract_sard_visit_tensors``: a padded ``(N, max_nvisits, max_visit_len)`` array of
  concept tokens plus visit times and lengths (SARD ``VisitTransformer`` / omop-learn torch datasets).

Both are driven by a *cohort parquet* (one row per index event) and run as a single set-based
DuckDB query over the whole cohort, instead of omop-learn's one-query-per-patient loop.

Conventions deliberately match omop-learn so outputs drop into its models and tokenizer:

* special tokens ``[BOS]=0, [EOS]=1, [SEP]=2, [PAD]=3, [UNK]=4``; concept tokens start at 5 and are
  ordered like ``omop_learn.data.common.ConceptTokenizer`` (sorted token string);
* token strings look like ``"<concept_id> - <domain> - <concept_name>"``;
* visit times are integer days since 1900-01-01 (``omop_learn.utils.date_utils.to_unixtime``) with
  ``-1`` as padding.

Temporal safety: by default a feature is only used if its event date is strictly *before* the row's
index date (``[index - lookback_days, index)``), so nothing at or after the index leaks in. Concept
id ``0`` ("No matching concept") is always dropped; besides carrying no information it would collide
with the padding index.
"""

from __future__ import annotations

import json
import re
from collections import namedtuple
from pathlib import Path
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

SPECIAL_TOKENS = ("[BOS]", "[EOS]", "[SEP]", "[PAD]", "[UNK]")
N_SPECIAL_TOKENS = len(SPECIAL_TOKENS)
PAD_TOKEN_IDX = 3
UNK_TOKEN_IDX = 4
UNIX_REFERENCE_DATE = pd.Timestamp("1900-01-01")  # omop_learn.utils.date_utils.REFTIME

_Domain = namedtuple("_Domain", "table concept_col date_col")
DOMAINS = {
    "condition": _Domain("condition_occurrence", "condition_concept_id", "condition_start_date"),
    "drug": _Domain("drug_exposure", "drug_concept_id", "drug_exposure_start_date"),
    "procedure": _Domain("procedure_occurrence", "procedure_concept_id", "procedure_date"),
}

_PERSON_ALIASES = ("person_id", "subject_id", "patid", "patient_id")
_INDEX_DATE_ALIASES = (
    "index_date", "cohort_start_date", "end_date", "admit_date", "admission_date",
    "admit_dt", "visit_start_date",
)
_OUTCOME_ALIASES = ("y", "outcome_flag", "label", "outcome")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


# --------------------------------------------------------------------------------------
# Tokenizer (omop-learn compatible)
# --------------------------------------------------------------------------------------
class FallbackConceptTokenizer:
    """Same interface and token layout as ``omop_learn.data.common.ConceptTokenizer``.

    Used only when omop-learn is not installed; ``build_concept_tokenizer`` returns the real class
    when it is importable.
    """

    def __init__(self, concept_set, bos_token="[BOS]", eos_token="[EOS]", sep_token="[SEP]",
                 pad_token="[PAD]", unk_token="[UNK]"):
        self.concept_list = [bos_token, eos_token, sep_token, pad_token, unk_token] + sorted(set(concept_set))
        self.concept_map = {concept: i for i, concept in enumerate(self.concept_list)}
        self.bos_token, self.eos_token, self.sep_token = bos_token, eos_token, sep_token
        self.pad_token, self.unk_token = pad_token, unk_token
        self.bos_token_idx, self.eos_token_idx, self.sep_token_idx = 0, 1, 2
        self.pad_token_idx, self.unk_token_idx = PAD_TOKEN_IDX, UNK_TOKEN_IDX
        self.num_special_tokens = N_SPECIAL_TOKENS
        self.vocab_size = len(self.concept_list)

    def concepts_to_ids(self, concept_list):
        return [self.concept_map.get(c, self.unk_token_idx) for c in concept_list]

    def ids_to_concepts(self, id_list):
        return [self.concept_list[i] for i in id_list]

    def serialize(self, filename):
        with open(filename, "w") as fh:
            json.dump(vars(self), fh)


def build_concept_tokenizer(concept_tokens: Sequence[str]):
    """Return an omop-learn ``ConceptTokenizer`` (or an equivalent fallback) for ``concept_tokens``."""
    try:
        from omop_learn.data.common import ConceptTokenizer
    except ImportError:
        return FallbackConceptTokenizer(concept_tokens)
    return ConceptTokenizer(concept_tokens)


def make_token(concept_id: int, domain: str, concept_name: str | None) -> str:
    """Token string in omop-learn's example convention: ``"<id> - <domain> - <name>"``."""
    name = (concept_name or "").strip()
    return f"{concept_id} - {domain} - {name}" if name else f"{concept_id} - {domain}"


def _parse_token(token: str):
    """Inverse of ``make_token`` -> ``(domain, concept_id)``; ``None`` for tokens we did not write."""
    parts = token.split(" - ", 2)
    if len(parts) < 2 or parts[1] not in DOMAINS:
        return None
    try:
        return parts[1], int(parts[0])
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Cohort parquet + event window
# --------------------------------------------------------------------------------------
def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_path(path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _resolve_column(cols: dict, explicit: str | None, aliases: Sequence[str], what: str, param: str,
                    required: bool = True):
    if explicit is not None:
        if explicit.lower() not in cols:
            raise ValueError(f"{what} column '{explicit}' not found in cohort parquet. "
                             f"Available columns: {list(cols.values())}")
        return cols[explicit.lower()]
    for alias in aliases:
        if alias in cols:
            return cols[alias]
    if required:
        raise ValueError(f"Could not find a {what} column in cohort parquet (looked for {list(aliases)}). "
                         f"Available columns: {list(cols.values())}. Pass {param}=... explicitly.")
    return None


def _load_cohort(con, features_parquet, person_col, index_date_col, outcome_col):
    """Read the cohort parquet in file order. Returns ``(cohort_sql, cohort_df)``.

    ``cohort_sql`` assigns a 0-based ``row_idx`` from ``file_row_number`` so row ``i`` of every output
    array is row ``i`` of the parquet (multi-file globs are ordered by filename, then row).
    """
    src = f"read_parquet('{_quote_path(features_parquet)}', file_row_number = true, filename = true)"
    desc = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
    cols = {r[0].lower(): r[0] for r in desc}
    p = _resolve_column(cols, person_col, _PERSON_ALIASES, "person id", "person_col")
    d = _resolve_column(cols, index_date_col, _INDEX_DATE_ALIASES, "index date", "index_date_col")
    y = _resolve_column(cols, outcome_col, _OUTCOME_ALIASES, "outcome", "outcome_col", required=False)

    y_sql = f", {_ident(y)} AS y" if y else ""
    cohort_sql = (
        "SELECT CAST(ROW_NUMBER() OVER (ORDER BY filename, file_row_number) - 1 AS BIGINT) AS row_idx, "
        f"TRY_CAST({_ident(p)} AS BIGINT) AS person_id, TRY_CAST({_ident(d)} AS DATE) AS index_date{y_sql} "
        f"FROM {src}"
    )
    df = con.execute(cohort_sql + " ORDER BY row_idx").df()
    bad_person, bad_date = int(df["person_id"].isna().sum()), int(df["index_date"].isna().sum())
    if bad_person or bad_date:
        raise ValueError(
            f"Cohort parquet has {bad_person} rows with a missing/non-integer person id ('{p}') and "
            f"{bad_date} rows with a missing/unparseable index date ('{d}'). Rows are aligned to the "
            "parquet by position, so they cannot be silently dropped; clean or filter them first."
        )
    df["person_id"] = df["person_id"].astype("int64")
    df["index_date"] = pd.to_datetime(df["index_date"])
    return cohort_sql, df


def _validate_options(domains, lookback_days, min_patient_freq):
    domains = tuple(domains)
    unknown = [d for d in domains if d not in DOMAINS]
    if not domains or unknown:
        raise ValueError(f"domains must be a non-empty subset of {sorted(DOMAINS)}; got {list(domains)}")
    if lookback_days is not None and int(lookback_days) < 0:
        raise ValueError("lookback_days must be >= 0 or None (unbounded)")
    if int(min_patient_freq) < 1:
        raise ValueError("min_patient_freq must be >= 1")
    return domains


def _events_ctes(cohort_sql, domains, lookback_days, include_index_date, min_patient_freq, filter_pairs):
    """CTE chain ``cohort -> ev0 -> ev``: windowed, vocabulary-filtered events for every cohort row."""
    unions = " UNION ALL ".join(
        f"SELECT person_id, {DOMAINS[d].concept_col} AS concept_id, {DOMAINS[d].date_col} AS event_date, "
        f"visit_occurrence_id, '{d}' AS domain FROM {DOMAINS[d].table}"
        for d in domains
    )
    lower = f"AND e.event_date >= c.index_date - {int(lookback_days)} " if lookback_days is not None else ""
    upper = "<=" if include_index_date else "<"
    if filter_pairs:
        vocab = ("JOIN (SELECT unnest(?::VARCHAR[]) AS domain, unnest(?::BIGINT[]) AS concept_id) v "
                 "ON ev0.domain = v.domain AND ev0.concept_id = v.concept_id")
    elif int(min_patient_freq) > 1:
        vocab = ("JOIN (SELECT domain, concept_id FROM ev0 GROUP BY domain, concept_id "
                 f"HAVING COUNT(DISTINCT person_id) >= {int(min_patient_freq)}) v "
                 "ON ev0.domain = v.domain AND ev0.concept_id = v.concept_id")
    else:
        vocab = ""
    return (
        f"WITH cohort AS ({cohort_sql}), ev_all AS ({unions}), "
        "ev0 AS (SELECT c.row_idx, c.person_id, c.index_date, e.domain, e.concept_id, e.event_date, "
        "e.visit_occurrence_id FROM cohort c JOIN ev_all e ON e.person_id = c.person_id "
        f"{lower}AND e.event_date {upper} c.index_date "
        "WHERE e.concept_id IS NOT NULL AND e.concept_id <> 0), "
        f"ev AS (SELECT ev0.* FROM ev0 {vocab})"
    )


def _concept_metadata(con, concept_ids, concept_table=None):
    """Best-effort ``concept_id -> (name, domain_id, vocabulary_id, standard_concept)``.

    Tries ``concept`` then the ``central_vocab`` catalog (see ``attach_central_vocabulary``), filling
    only ids still missing, so a stub local ``concept`` table does not mask a populated central one.
    """
    wanted = sorted({int(i) for i in concept_ids})
    found: dict = {}
    if concept_table is not None and not _IDENT_RE.match(concept_table):
        raise ValueError(f"Invalid concept_table identifier: {concept_table!r}")
    candidates = [concept_table] if concept_table else ["concept", "central_vocab.concept", "central_vocab.main.concept"]
    for cand in candidates:
        missing = [i for i in wanted if i not in found]
        if not missing:
            break
        try:
            rows = con.execute(
                "SELECT concept_id, concept_name, domain_id, vocabulary_id, standard_concept "
                f"FROM {cand} WHERE concept_id IN (SELECT unnest(?::BIGINT[]))", [missing]
            ).fetchall()
        except duckdb.Error:
            continue
        for r in rows:
            found[int(r[0])] = tuple(r[1:])
    return found


def _tokenizer_vocab(tokenizer):
    """Vocabulary rows from an existing tokenizer (train/test reuse): concepts we did not write are
    kept as columns but can never match."""
    rows = []
    for idx in range(N_SPECIAL_TOKENS, len(tokenizer.concept_list)):
        token = tokenizer.concept_list[idx]
        parsed = _parse_token(token)
        rows.append({"token_id": idx, "token": token,
                     "domain": parsed[0] if parsed else None,
                     "concept_id": parsed[1] if parsed else None})
    return pd.DataFrame(rows, columns=["token_id", "token", "domain", "concept_id"])


def _n_patients(pairs: pd.DataFrame, person_of_row: np.ndarray) -> pd.DataFrame:
    """Distinct persons per (domain, concept_id); ``pairs`` has row_idx/domain/concept_id."""
    t = pairs[["row_idx", "domain", "concept_id"]].drop_duplicates()
    t = t.assign(person_id=person_of_row[t["row_idx"].to_numpy()])
    t = t.drop_duplicates(["person_id", "domain", "concept_id"])
    return t.groupby(["domain", "concept_id"]).size().rename("n_patients").reset_index()


def _assemble_vocab(con, triples, person_of_row, tokenizer, concept_table):
    """Build the vocabulary DataFrame for ``triples`` (any frame with row_idx/domain/concept_id).

    Columns: token_id (tokenizer index, >= 5), column_index (token_id - 5), token, domain, concept_id,
    concept_name, domain_id, vocabulary_id, standard_concept, n_patients. Returns ``(vocab, tokenizer)``.
    """
    counts = _n_patients(triples, person_of_row) if len(triples) else pd.DataFrame(
        {"domain": pd.Series(dtype=object), "concept_id": pd.Series(dtype="int64"),
         "n_patients": pd.Series(dtype="int64")})

    if tokenizer is None:
        meta = _concept_metadata(con, counts["concept_id"].unique(), concept_table)
        counts["token"] = [make_token(int(c), d, (meta.get(int(c)) or (None,))[0])
                           for c, d in zip(counts["concept_id"], counts["domain"])]
        tokenizer = build_concept_tokenizer(counts["token"].tolist())
        base = pd.DataFrame({"token": tokenizer.concept_list[N_SPECIAL_TOKENS:]})
        base["token_id"] = np.arange(N_SPECIAL_TOKENS, N_SPECIAL_TOKENS + len(base))
        vocab = base.merge(counts, on="token", how="left")
    else:
        base = _tokenizer_vocab(tokenizer)
        meta = _concept_metadata(con, base["concept_id"].dropna().unique(), concept_table)
        vocab = base.merge(counts, on=["domain", "concept_id"], how="left")

    def _meta(concept_id, i):
        return (meta.get(int(concept_id)) or (None,) * 4)[i] if pd.notna(concept_id) else None

    for i, col in enumerate(("concept_name", "domain_id", "vocabulary_id", "standard_concept")):
        vocab[col] = vocab["concept_id"].map(lambda c, i=i: _meta(c, i))
    vocab["n_patients"] = vocab["n_patients"].fillna(0).astype("int64")
    vocab["column_index"] = vocab["token_id"] - N_SPECIAL_TOKENS
    cols = ["token_id", "column_index", "token", "domain", "concept_id", "concept_name",
            "domain_id", "vocabulary_id", "standard_concept", "n_patients"]
    return vocab[cols].reset_index(drop=True), tokenizer


def _pair_index(vocab: pd.DataFrame, column: str) -> pd.DataFrame:
    """(domain, concept_id) -> ``column`` lookup, one row per pair (first token wins if a reused
    tokenizer holds several tokens for the same concept, so joins never duplicate events)."""
    return vocab.dropna(subset=["concept_id"]).drop_duplicates(["domain", "concept_id"])[
        ["domain", "concept_id", column]]


def _run(con, features_parquet, suffix_sql, *, domains, lookback_days, min_patient_freq,
         include_index_date, tokenizer, person_col, index_date_col, outcome_col):
    """Shared driver. Returns ``(cohort_df, frame, params_dict)`` where ``frame`` is the suffix query result."""
    domains = _validate_options(domains, lookback_days, min_patient_freq)
    cohort_sql, cohort_df = _load_cohort(con, features_parquet, person_col, index_date_col, outcome_col)

    query_params: list = []
    filter_pairs = tokenizer is not None
    if filter_pairs:
        v = _tokenizer_vocab(tokenizer).dropna(subset=["concept_id"])
        v = v[v["domain"].isin(domains)]
        query_params = [v["domain"].tolist(), v["concept_id"].astype("int64").tolist()]
    sql = _events_ctes(cohort_sql, domains, lookback_days, include_index_date, min_patient_freq, filter_pairs)
    frame = con.execute(sql + " " + suffix_sql, query_params).df()
    return cohort_df, frame, domains


def _cohort_outputs(cohort_df):
    cohort = cohort_df.drop(columns=["row_idx"])
    y = cohort["y"].to_numpy() if "y" in cohort.columns else None
    return cohort, y


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def extract_sparse_concept_matrix(
    con: duckdb.DuckDBPyConnection,
    features_parquet: str | Path,
    lookback_days: int | None = 365,
    min_patient_freq: int = 1,
    domains: Sequence[str] = ("condition", "drug"),
    value: str = "count",
    tokenizer: Any = None,
    include_index_date: bool = False,
    person_col: str | None = None,
    index_date_col: str | None = None,
    outcome_col: str | None = None,
    concept_table: str | None = None,
    dtype: Any = np.float32,
) -> dict:
    """High-dimensional sparse concept matrix over a per-row lookback window.

    One row per row of ``features_parquet`` (same order); one column per retained concept. Each cell
    is the number of condition/drug/procedure records of that concept in
    ``[index_date - lookback_days, index_date)`` (or ``<= index_date`` with ``include_index_date``).

    Args:
        con: DuckDB connection with the OMOP CDM tables (read-only is fine). If the vocabulary lives in
            an attached ``central_vocab`` catalog, concept names are picked up from it automatically.
        features_parquet: Cohort parquet (path or glob), one row per index event. Needs a person id and an
            index date column; an outcome column is carried through if present. Column names are
            auto-detected (``person_id``/``subject_id``/``patid``; ``index_date``/``cohort_start_date``/
            ``end_date``/``admit_date``...; ``y``/``outcome_flag``/``label``) or set explicitly.
        lookback_days: Window length in days, or ``None`` for all prior history.
        min_patient_freq: Keep a concept only if at least this many *distinct patients* have it in their
            window. Computed on the cohort passed in; to apply a train-fitted vocabulary to a test cohort,
            pass the train result's ``tokenizer`` instead.
        domains: Any of ``"condition"``, ``"drug"``, ``"procedure"``.
        value: ``"count"`` (records per concept) or ``"binary"`` (presence).
        tokenizer: Optional omop-learn style tokenizer from a previous call. Fixes the column space and
            order; ``min_patient_freq`` is not applied and concepts outside it are dropped.
        include_index_date: Also use events dated exactly on the index date (default excludes them).
        concept_table: Concept table to read names from (default: ``concept``, then ``central_vocab``).
        dtype: Matrix dtype (default float32).

    Returns:
        dict with ``X`` (``scipy.sparse.csr_matrix``, N x V), ``concepts`` (one row per column, aligned to
        ``X`` columns; see ``column_index``), ``tokenizer``, ``cohort`` (person_id, index_date[, y], row
        aligned to ``X``), ``y`` (or None) and ``params``. Column ``j`` is ``tokenizer.concept_list[j + 5]``.
    """
    try:
        from scipy import sparse
    except ImportError as exc:  # pragma: no cover - exercised only without scipy
        raise ImportError("extract_sparse_concept_matrix needs scipy: pip install 'omop-duck-db[ml]'") from exc
    if value not in ("count", "binary"):
        raise ValueError("value must be 'count' or 'binary'")

    cohort_df, triples, domains = _run(
        con, features_parquet,
        "SELECT row_idx, domain, concept_id, COUNT(*) AS n FROM ev GROUP BY row_idx, domain, concept_id",
        domains=domains, lookback_days=lookback_days, min_patient_freq=min_patient_freq,
        include_index_date=include_index_date, tokenizer=tokenizer, person_col=person_col,
        index_date_col=index_date_col, outcome_col=outcome_col,
    )
    person_of_row = cohort_df["person_id"].to_numpy()
    vocab, tokenizer = _assemble_vocab(con, triples, person_of_row, tokenizer, concept_table)

    n_rows, n_cols = len(cohort_df), len(vocab)
    if len(triples):
        cols = triples.merge(_pair_index(vocab, "column_index"), on=["domain", "concept_id"],
                             how="left")["column_index"].to_numpy()
        data = triples["n"].to_numpy() if value == "count" else np.ones(len(triples))
        X = sparse.coo_matrix((data.astype(dtype), (triples["row_idx"].to_numpy(), cols.astype(np.int64))),
                              shape=(n_rows, n_cols)).tocsr()
    else:
        X = sparse.csr_matrix((n_rows, n_cols), dtype=dtype)

    cohort, y = _cohort_outputs(cohort_df)
    return {
        "X": X, "concepts": vocab, "tokenizer": tokenizer, "cohort": cohort, "y": y,
        "params": {"lookback_days": lookback_days, "min_patient_freq": min_patient_freq,
                   "domains": domains, "value": value, "include_index_date": include_index_date},
    }


def extract_sard_visit_tensors(
    con: duckdb.DuckDBPyConnection,
    features_parquet: str | Path,
    lookback_days: int | None = 365,
    min_patient_freq: int = 1,
    domains: Sequence[str] = ("condition", "drug"),
    max_nvisits: int | None = None,
    max_visit_len: int | None = None,
    tokenizer: Any = None,
    include_index_date: bool = False,
    person_col: str | None = None,
    index_date_col: str | None = None,
    outcome_col: str | None = None,
    concept_table: str | None = None,
    dtype: Any = np.int64,
) -> dict:
    """Padded chronological visit tensors for SARD / omop-learn sequence models.

    ``concept_tensor[i, v, t]`` is the token id of the ``t``-th concept in the ``v``-th visit of row
    ``i``. Visits are ordered oldest first and left aligned, padded at the end with ``[PAD]`` (3).
    A visit is a ``visit_occurrence_id`` (events without one are grouped by event date); concepts are
    de-duplicated within a visit and ordered by token id.

    Args:
        max_nvisits: Keep only the most recent this-many visits per row. Default: the observed maximum.
            Set explicitly (with ``max_visit_len``) so train and test tensors share a shape.
        max_visit_len: Keep only the first this-many concepts (lowest token ids) per visit. Default:
            the observed maximum.
        (other arguments as in ``extract_sparse_concept_matrix``; ``dtype`` is the tensor dtype.)

    Returns:
        dict with ``concept_tensor`` (N, V, L); ``visit_lengths`` (N, V) concepts per visit (0 = padding);
        ``n_visits`` (N,); ``times`` (N, V) visit date as days since 1900-01-01 (omop-learn ``times``,
        ``-1`` padding); ``visit_days_before_index`` (N, V) (``-1`` padding); ``concepts``; ``tokenizer``
        (pad index 3); ``cohort``; ``y``; ``params``. Rows with no visits are kept (``n_visits == 0``) so
        row ``i`` always matches parquet row ``i``; omop-learn's own builder drops such patients.
    """
    if max_nvisits is not None and int(max_nvisits) < 1:
        raise ValueError("max_nvisits must be >= 1")
    if max_visit_len is not None and int(max_visit_len) < 1:
        raise ValueError("max_visit_len must be >= 1")

    suffix = (
        ", ev_v AS (SELECT ev.row_idx, ev.domain, ev.concept_id, COALESCE(ev.visit_occurrence_id, -1) AS vid, "
        "CASE WHEN ev.visit_occurrence_id IS NULL THEN ev.event_date ELSE DATE '1900-01-01' END AS vday, "
        "COALESCE(v.visit_start_date, ev.event_date) AS vdate FROM ev "
        "LEFT JOIN visit_occurrence v ON v.visit_occurrence_id = ev.visit_occurrence_id "
        "AND v.person_id = ev.person_id), "
        "toks AS (SELECT row_idx, vid, vday, domain, concept_id, MIN(vdate) AS vdate FROM ev_v "
        "GROUP BY row_idx, vid, vday, domain, concept_id), "
        "vis AS (SELECT *, MIN(vdate) OVER (PARTITION BY row_idx, vid, vday) AS visit_date FROM toks) "
        "SELECT row_idx, DENSE_RANK() OVER (PARTITION BY row_idx ORDER BY visit_date, vid, vday) - 1 AS v_ord, "
        "domain, concept_id, visit_date FROM vis"
    )
    cohort_df, toks, domains = _run(
        con, features_parquet, suffix,
        domains=domains, lookback_days=lookback_days, min_patient_freq=min_patient_freq,
        include_index_date=include_index_date, tokenizer=tokenizer, person_col=person_col,
        index_date_col=index_date_col, outcome_col=outcome_col,
    )
    person_of_row = cohort_df["person_id"].to_numpy()
    vocab, tokenizer = _assemble_vocab(con, toks, person_of_row, tokenizer, concept_table)

    n_rows = len(cohort_df)
    if len(toks):
        toks = toks.merge(_pair_index(vocab, "token_id"), on=["domain", "concept_id"], how="left")
        toks = toks.sort_values(["row_idx", "v_ord", "token_id"], kind="stable").reset_index(drop=True)
        row = toks["row_idx"].to_numpy(np.int64)
        v_ord = toks["v_ord"].to_numpy(np.int64)

        # Most recent `max_nvisits` visits: shift each row's visit index so its last visit is last slot.
        total_visits = np.zeros(n_rows, dtype=np.int64)
        np.maximum.at(total_visits, row, v_ord + 1)
        n_slots = int(max_nvisits) if max_nvisits is not None else max(1, int(total_visits.max()))
        slot = v_ord - np.maximum(total_visits[row] - n_slots, 0)

        # Position inside each (row, visit): running count within contiguous groups.
        new_group = np.r_[True, (row[1:] != row[:-1]) | (v_ord[1:] != v_ord[:-1])]
        group_start = np.maximum.accumulate(np.where(new_group, np.arange(len(row)), 0))
        pos = np.arange(len(row)) - group_start
        v_len = int(max_visit_len) if max_visit_len is not None else max(1, int(pos.max()) + 1)

        keep = (slot >= 0) & (pos < v_len)
        r, s, p = row[keep], slot[keep], pos[keep]
        tensor = np.full((n_rows, n_slots, v_len), PAD_TOKEN_IDX, dtype=dtype)
        tensor[r, s, p] = toks["token_id"].to_numpy()[keep]

        visit_lengths = np.zeros((n_rows, n_slots), dtype=np.int32)
        np.add.at(visit_lengths, (r, s), 1)

        vdate = pd.to_datetime(toks["visit_date"])
        times = np.full((n_rows, n_slots), -1, dtype=np.int64)
        days_before = np.full((n_rows, n_slots), -1, dtype=np.int64)
        unix = ((vdate - UNIX_REFERENCE_DATE) // pd.Timedelta("1d")).to_numpy(np.int64)
        before = ((cohort_df["index_date"].to_numpy()[row] - vdate.to_numpy()) // np.timedelta64(1, "D")).astype(np.int64)
        times[r, s] = unix[keep]
        days_before[r, s] = before[keep]
        n_visits = np.minimum(total_visits, n_slots).astype(np.int32)
    else:
        n_slots, v_len = int(max_nvisits or 1), int(max_visit_len or 1)
        tensor = np.full((n_rows, n_slots, v_len), PAD_TOKEN_IDX, dtype=dtype)
        visit_lengths = np.zeros((n_rows, n_slots), dtype=np.int32)
        times = np.full((n_rows, n_slots), -1, dtype=np.int64)
        days_before = np.full((n_rows, n_slots), -1, dtype=np.int64)
        n_visits = np.zeros(n_rows, dtype=np.int32)

    cohort, y = _cohort_outputs(cohort_df)
    return {
        "concept_tensor": tensor, "visit_lengths": visit_lengths, "n_visits": n_visits,
        "times": times, "visit_days_before_index": days_before,
        "concepts": vocab, "tokenizer": tokenizer, "cohort": cohort, "y": y,
        "params": {"lookback_days": lookback_days, "min_patient_freq": min_patient_freq,
                   "domains": domains, "max_nvisits": n_slots, "max_visit_len": v_len,
                   "include_index_date": include_index_date},
    }


def as_omop_learn_batch(result: dict) -> dict:
    """Re-key ``extract_sard_visit_tensors`` output like ``OMOPDatasetTorch.collate``'s batch.

    Returns numpy arrays under ``visits``, ``times``, ``lengths`` (visits per row) and, when the cohort
    had an outcome, ``y``. Wrap with ``torch.as_tensor`` to feed omop-learn models.
    """
    batch = {"visits": result["concept_tensor"], "times": result["times"],
             "lengths": result["n_visits"].astype(np.float32)}
    if result.get("y") is not None:
        batch["y"] = result["y"]
    return batch
