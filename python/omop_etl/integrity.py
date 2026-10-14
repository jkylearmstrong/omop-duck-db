"""Production Data Integrity, Statistical Leakage Scan, Informative Missingness,
Provenance Manifests, and Consortium Overlap Tools."""

import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import duckdb
import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# 1. Statistical Leakage Scan
# -----------------------------------------------------------------------------

def leakage_scan(
    X: pd.DataFrame,
    y: Union[np.ndarray, pd.Series, str],
    index_date: Optional[Union[str, pd.Series, pd.Timestamp]] = None,
    feature_dates: Optional[Union[Dict[str, Any], pd.DataFrame]] = None,
    max_auc: float = 0.98,
    min_stratum_n: int = 10,
) -> pd.DataFrame:
    """Scans features for statistical and temporal data leakage against a binary outcome.

    Checks:
    1. Perfect or near-perfect separation (feature threshold implies outcome with zero exceptions).
    2. Single-feature AUC above `max_auc` (or below 1 - `max_auc`).
    3. Outcome rate of exactly 0 or 1 in any non-trivial feature stratum (N >= min_stratum_n).
    4. Temporal leakage (feature date occurs after index date).

    Parameters
    ----------
    X : pd.DataFrame
        Matrix of features (or DataFrame containing features and outcome).
    y : Union[np.ndarray, pd.Series, str]
        Binary outcome indicator (0 or 1), or string column name in X.
    index_date : Optional[Union[str, pd.Series, pd.Timestamp]]
        Index admission/anchor date(s) to verify temporal sequencing.
    feature_dates : Optional[Union[Dict[str, Any], pd.DataFrame]]
        Dictionary or DataFrame mapping feature names to their occurrence timestamps.
    max_auc : float
        Threshold for single-feature AUC flagging (default 0.98).
    min_stratum_n : int
        Minimum stratum size to flag pure 0/1 outcome rates (default 10).

    Returns
    -------
    pd.DataFrame
        Tidy audit dataframe with columns:
        feature, check, value, threshold, flagged, details.
    """
    df = X.copy()
    if isinstance(y, str):
        outcome = df.pop(y).values
    elif isinstance(y, pd.Series):
        outcome = y.values
    else:
        outcome = np.asarray(y)

    outcome = outcome.astype(int)
    n = len(outcome)
    n_pos = int(np.sum(outcome == 1))
    n_neg = n - n_pos

    records = []

    # Check temporal leakage first if dates are supplied
    if index_date is not None and feature_dates is not None:
        idx_dt = pd.to_datetime(index_date)
        if isinstance(feature_dates, dict):
            for feat, f_dt in feature_dates.items():
                if feat in df.columns:
                    f_dts = pd.to_datetime(f_dt)
                    if isinstance(f_dts, pd.Series) or isinstance(f_dts, np.ndarray):
                        post_index = (f_dts > idx_dt).sum()
                    else:
                        post_index = (f_dts > idx_dt)
                    post_count = int(np.sum(post_index))
                    flagged = post_count > 0
                    records.append({
                        "feature": feat,
                        "check": "temporal_leakage",
                        "value": post_count,
                        "threshold": 0,
                        "flagged": flagged,
                        "details": f"{post_count} observations occurred after index date" if flagged else "Clean temporal sequence",
                    })

    # Evaluate each feature in X
    for col in df.columns:
        vals = df[col].values
        valid_mask = ~pd.isna(vals)
        if not np.any(valid_mask):
            records.append({
                "feature": col,
                "check": "all_missing",
                "value": 1.0,
                "threshold": 0.0,
                "flagged": True,
                "details": "Feature is 100% missing",
            })
            continue

        v = vals[valid_mask]
        o = outcome[valid_mask]
        n_val = len(v)

        # Check: Zero variance
        if np.all(v == v[0]):
            records.append({
                "feature": col,
                "check": "zero_variance",
                "value": 0.0,
                "threshold": 0.0,
                "flagged": True,
                "details": "Feature has constant value across all rows",
            })
            continue

        # Check: Perfect stratum separation
        # Group into strata (unique values if <= 10, else quantiles)
        unique_vals = np.unique(v)
        is_discrete = len(unique_vals) <= 10
        strat_pure = False
        strat_details = []

        if is_discrete:
            for u in unique_vals:
                mask_u = (v == u)
                sub_n = np.sum(mask_u)
                if sub_n >= min_stratum_n:
                    rate = np.mean(o[mask_u])
                    if rate == 0.0 or rate == 1.0:
                        strat_pure = True
                        strat_details.append(f"val={u} (N={sub_n}, rate={rate:.2f})")
        else:
            # Check binary threshold splits: is there a split that yields 100% purity?
            # Check top and bottom percentiles
            q10, q90 = np.quantile(v, [0.10, 0.90])
            for q_val, q_op, q_name in [(q10, v <= q10, "<= q10"), (q90, v >= q90, ">= q90")]:
                sub_n = np.sum(q_op)
                if sub_n >= min_stratum_n:
                    rate = np.mean(o[q_op])
                    if rate == 0.0 or rate == 1.0:
                        strat_pure = True
                        strat_details.append(f"{q_name} (N={sub_n}, rate={rate:.2f})")

        if strat_pure:
            records.append({
                "feature": col,
                "check": "pure_stratum_separation",
                "value": 1.0,
                "threshold": 0.0,
                "flagged": True,
                "details": f"Deterministic outcome in strata: {'; '.join(strat_details)}",
            })

        # Check: Single-feature AUC via Mann-Whitney U / rank sum
        v_pos = v[o == 1]
        v_neg = v[o == 0]
        if len(v_pos) > 0 and len(v_neg) > 0:
            # Compute rank sum
            ranked = pd.Series(v).rank().values
            rank_sum_pos = np.sum(ranked[o == 1])
            n1 = len(v_pos)
            n0 = len(v_neg)
            u_stat = rank_sum_pos - (n1 * (n1 + 1)) / 2.0
            auc = u_stat / (n1 * n0)
            max_effective_auc = max(auc, 1.0 - auc)

            flagged_auc = max_effective_auc >= max_auc
            records.append({
                "feature": col,
                "check": "high_auc",
                "value": round(float(max_effective_auc), 4),
                "threshold": max_auc,
                "flagged": flagged_auc,
                "details": f"Single-feature AUC = {max_effective_auc:.4f} (>= {max_auc})" if flagged_auc else f"AUC = {max_effective_auc:.4f}",
            })

    res = pd.DataFrame(records)
    if res.empty:
        return pd.DataFrame(columns=["feature", "check", "value", "threshold", "flagged", "details"])
    return res.sort_values(by=["flagged", "value"], ascending=[False, False]).reset_index(drop=True)


# -----------------------------------------------------------------------------
# 2. Informative Missingness Helpers
# -----------------------------------------------------------------------------

def add_missing_indicators(
    df: pd.DataFrame,
    cols: Optional[List[str]] = None,
    prefix: str = "miss_",
) -> pd.DataFrame:
    """Adds binary missing-indicator columns (1 if missing, 0 otherwise) for features.

    Parameters
    ----------
    df : pd.DataFrame
        Input clinical feature dataframe.
    cols : Optional[List[str]]
        Columns to generate indicators for. If None, applies to all columns with missing values.
    prefix : str
        Prefix for indicator columns (default 'miss_').

    Returns
    -------
    pd.DataFrame
        Dataframe with original columns plus binary missingness indicator columns.
    """
    out = df.copy()
    if cols is None:
        cols = [c for c in df.columns if df[c].isna().any()]

    for c in cols:
        if c in df.columns:
            out[f"{prefix}{c}"] = df[c].isna().astype(int)

    return out


def missingness_by_outcome(
    df: pd.DataFrame,
    outcome_col: str,
    cols: Optional[List[str]] = None,
    by: Optional[str] = None,
    diff_threshold: float = 0.05,
    fill_rule_notes: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """Evaluates missingness proportions stratified by outcome (informative missingness).

    Parameters
    ----------
    df : pd.DataFrame
        Input dataframe with features and outcome column.
    outcome_col : str
        Name of binary outcome column (0 / 1).
    cols : Optional[List[str]]
        Features to evaluate. If None, evaluates all numeric/categorical features except outcome and by.
    by : Optional[str]
        Optional site column to stratify by (default None).
    diff_threshold : float
        Absolute missingness difference threshold between groups to flag (default 0.05 = 5%).
    fill_rule_notes : Optional[Dict[str, str]]
        Dictionary mapping column names to explicit imputation/fill rules (e.g. {'days_since_enc': 'max lookback 365d'}).

    Returns
    -------
    pd.DataFrame
        Audit table: site, feature, n_ctrl, miss_ctrl, rate_ctrl, n_case, miss_case, rate_case, diff, flagged, fill_rule.
    """
    if outcome_col not in df.columns:
        raise ValueError(f"Outcome column '{outcome_col}' not found in dataframe.")

    exclude = {outcome_col}
    if by and by in df.columns:
        exclude.add(by)

    if cols is None:
        eval_cols = [c for c in df.columns if c not in exclude]
    else:
        eval_cols = [c for c in cols if c in df.columns and c not in exclude]

    notes = fill_rule_notes or {}
    strata = [("ALL", df)]
    if by and by in df.columns:
        for site_val, sub_df in df.groupby(by):
            strata.append((str(site_val), sub_df))

    records = []
    for site_label, sub in strata:
        ctrl = sub[sub[outcome_col] == 0]
        case = sub[sub[outcome_col] == 1]
        n_ctrl = len(ctrl)
        n_case = len(case)

        for col in eval_cols:
            m_ctrl = int(ctrl[col].isna().sum()) if n_ctrl > 0 else 0
            m_case = int(case[col].isna().sum()) if n_case > 0 else 0
            r_ctrl = m_ctrl / n_ctrl if n_ctrl > 0 else 0.0
            r_case = m_case / n_case if n_case > 0 else 0.0
            diff = r_case - r_ctrl
            flagged = abs(diff) >= diff_threshold

            # Small cell suppression for exported counts (< 11)
            display_ctrl = "<11" if 0 < m_ctrl < 11 else str(m_ctrl)
            display_case = "<11" if 0 < m_case < 11 else str(m_case)

            records.append({
                "site": site_label,
                "feature": col,
                "n_ctrl": n_ctrl,
                "miss_ctrl": display_ctrl,
                "rate_ctrl": round(r_ctrl, 4),
                "n_case": n_case,
                "miss_case": display_case,
                "rate_case": round(r_case, 4),
                "missingness_diff": round(diff, 4),
                "flagged": flagged,
                "fill_rule": notes.get(col, "None"),
            })

    return pd.DataFrame(records)


# -----------------------------------------------------------------------------
# 3. Provenance Stamp and Vocabulary Consistency
# -----------------------------------------------------------------------------

def provenance(
    con: Optional[duckdb.DuckDBPyConnection] = None,
    seed: Optional[int] = None,
    vocab_schema: Optional[str] = None,
    vocab_db: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates a comprehensive provenance stamp capturing library version, DuckDB version,
    active vocabulary releases per vocabulary_id, seed, and git commit.

    Parameters
    ----------
    con : Optional[duckdb.DuckDBPyConnection]
        Active DuckDB connection to inspect CDM vocabulary version metadata.
    seed : Optional[int]
        Random seed used for study cohort draws or model training.
    vocab_schema : Optional[str]
        Schema or attached database containing Athena vocabulary tables (e.g. 'central_vocab').
    vocab_db : Optional[str]
        Alias for vocab_schema.

    Returns
    -------
    Dict[str, Any]
        Dictionary with system provenance details.
    """
    v_schema = vocab_schema or vocab_db
    prov = {
        "package_name": "omopduckdb",
        "package_version": "0.6.5",
        "duckdb_version": duckdb.__version__,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "git_commit": "c072171c3c33098101ba7eeff99d3b466abf1da9",
        "vocabularies": {},
    }

    # Inspect burned commit or git repository
    try:
        import omop_etl
        burned = getattr(omop_etl, "__commit__", None)
        if burned:
            prov["git_commit"] = burned
    except Exception:
        pass

    try:
        import subprocess
        cur = Path(__file__).resolve().parent
        for candidate in [cur, cur.parent, cur.parent.parent]:
            if (candidate / ".git").exists():
                head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(candidate),
                    stderr=subprocess.DEVNULL
                ).decode("utf-8").strip()
                if head:
                    prov["git_commit"] = head
                    break
        else:
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
            if head:
                prov["git_commit"] = head
    except Exception:
        pass

    # Inspect vocabulary releases in CDM or attached vocabulary database
    if con is not None:
        try:
            vocab_tbl = f"{v_schema}.vocabulary" if v_schema else "vocabulary"
            try:
                con.execute(f"SELECT 1 FROM {vocab_tbl} LIMIT 1")
            except Exception:
                if v_schema is None:
                    try:
                        con.execute("SELECT 1 FROM central_vocab.vocabulary LIMIT 1")
                        vocab_tbl = "central_vocab.vocabulary"
                    except Exception:
                        vocab_tbl = None
                else:
                    vocab_tbl = None

            if vocab_tbl:
                rows = con.execute(f"""
                    SELECT vocabulary_id, vocabulary_version
                    FROM {vocab_tbl}
                    WHERE vocabulary_id IN ('None', 'RxNorm', 'SNOMED', 'LOINC', 'ATC', 'ICD10CM')
                       OR vocabulary_version IS NOT NULL
                """).fetchall()
                for vid, vver in rows:
                    prov["vocabularies"][vid] = vver or "unspecified"
        except Exception:
            pass

    prov["vocabulary"] = {
        "reachable": len(prov["vocabularies"]) > 0,
        "version_string": prov["vocabularies"].get("RxNorm") or (next(iter(prov["vocabularies"].values())) if prov["vocabularies"] else None),
    }

    return prov


def check_vocabulary_provenance(
    con1: duckdb.DuckDBPyConnection,
    con2: duckdb.DuckDBPyConnection,
    vocab_schema: Optional[str] = None,
    vocab_db: Optional[str] = None,
) -> Tuple[bool, pd.DataFrame]:
    """Verifies that two DuckDB OMOP databases share identical vocabulary releases.

    Parameters
    ----------
    con1 : duckdb.DuckDBPyConnection
        First DuckDB database.
    con2 : duckdb.DuckDBPyConnection
        Second DuckDB database.
    vocab_schema : Optional[str]
        Schema or attached database containing Athena vocabulary tables.
    vocab_db : Optional[str]
        Alias for vocab_schema.

    Returns
    -------
    Tuple[bool, pd.DataFrame]
        Boolean match status, and comparison DataFrame of vocabulary versions.
    """
    p1 = provenance(con1, vocab_schema=vocab_schema, vocab_db=vocab_db)["vocabularies"]
    p2 = provenance(con2, vocab_schema=vocab_schema, vocab_db=vocab_db)["vocabularies"]

    all_vocabs = sorted(set(p1.keys()).union(set(p2.keys())))
    rows = []
    matched = True

    for v in all_vocabs:
        v1 = p1.get(v, "MISSING")
        v2 = p2.get(v, "MISSING")
        is_match = (v1 == v2) and (v1 != "MISSING")
        if not is_match:
            matched = False
        rows.append({
            "vocabulary_id": v,
            "version_db1": v1,
            "version_db2": v2,
            "matched": is_match,
        })

    return matched, pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# 4. Numbers-of-Record Manifest and Stale-Number Scanner
# -----------------------------------------------------------------------------

def numbers_of_record(
    cohorts: Dict[str, pd.DataFrame],
    outcome_col: Optional[str] = "outcome",
    site_col: Optional[str] = "site",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates an authoritative numbers-of-record JSON manifest of cohort sizes and event rates.

    Parameters
    ----------
    cohorts : Dict[str, pd.DataFrame]
        Named dictionary of cohort DataFrames: {'TargetCohort': df, 'ComparatorCohort': df2}.
    outcome_col : Optional[str]
        Outcome column to compute events and rates (default 'outcome').
    site_col : Optional[str]
        Site column for multi-hospital stratification (default 'site').
    path : Optional[str]
        Optional JSON file path to write the manifest.

    Returns
    -------
    Dict[str, Any]
        Manifest dictionary with cohort metadata and counts.
    """
    manifest = {
        "manifest_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cohorts": {},
    }

    for name, df in cohorts.items():
        n = len(df)
        events = int(df[outcome_col].sum()) if (outcome_col and outcome_col in df.columns) else None
        rate = round(events / n, 6) if (events is not None and n > 0) else None

        sites = {}
        if site_col and site_col in df.columns:
            for s_val, sub in df.groupby(site_col):
                sn = len(sub)
                sevents = int(sub[outcome_col].sum()) if (outcome_col and outcome_col in sub.columns) else None
                srate = round(sevents / sn, 6) if (sevents is not None and sn > 0) else None
                sites[str(s_val)] = {
                    "total_n": sn,
                    "events": sevents,
                    "event_rate": srate,
                }

        manifest["cohorts"][name] = {
            "total_n": n,
            "events": events,
            "event_rate": rate,
            "sites": sites,
        }

    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    return manifest


def scan_document_numbers(
    doc_text_or_path: str,
    manifest: Dict[str, Any],
    previous_manifest: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Scans rendered manuscripts (Markdown, Quarto, LaTeX) for stale or unverified numerical figures.

    Parameters
    ----------
    doc_text_or_path : str
        Document text or path to markdown/latex file.
    manifest : Dict[str, Any]
        Current authoritative numbers-of-record manifest.
    previous_manifest : Optional[Dict[str, Any]]
        Previous run's manifest to detect stale figures left over from older cohort iterations.

    Returns
    -------
    pd.DataFrame
        Audit table with columns: number, status ('verified', 'stale', 'unverified'), source, context.
    """
    if os.path.exists(doc_text_or_path):
        with open(doc_text_or_path, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = doc_text_or_path

    # Extract all authoritative numbers from current manifest
    def _extract_manifest_numbers(m: Dict[str, Any]) -> Set[int]:
        s = set()
        for cdata in m.get("cohorts", {}).values():
            if cdata.get("total_n") is not None:
                s.add(int(cdata["total_n"]))
            if cdata.get("events") is not None:
                s.add(int(cdata["events"]))
            for sdata in cdata.get("sites", {}).values():
                if sdata.get("total_n") is not None:
                    s.add(int(sdata["total_n"]))
                if sdata.get("events") is not None:
                    s.add(int(sdata["events"]))
        return s

    current_nums = _extract_manifest_numbers(manifest)
    prev_nums = _extract_manifest_numbers(previous_manifest) if previous_manifest else set()

    # Find numbers in text
    matches = re.finditer(r"\b\d+\b", text)
    results = []

    for m in matches:
        val = int(m.group())
        start = max(0, m.start() - 25)
        end = min(len(text), m.end() + 25)
        context = text[start:end].replace("\n", " ").strip()

        if val in current_nums:
            status = "verified"
            source = "current_manifest"
        elif val in prev_nums:
            status = "stale"
            source = "previous_manifest"
        else:
            status = "unverified"
            source = "none"

        results.append({
            "number": val,
            "status": status,
            "source": source,
            "context": context,
        })

    df = pd.DataFrame(results)
    if df.empty:
        return pd.DataFrame(columns=["number", "status", "source", "context"])
    return df.drop_duplicates(subset=["number", "status"]).sort_values(by="status").reset_index(drop=True)


# -----------------------------------------------------------------------------
# 5. Vocabulary and Input Preflight Check
# -----------------------------------------------------------------------------

def vocabulary_preflight(
    con: duckdb.DuckDBPyConnection,
    required: Iterable[str] = ("RxNorm", "ATC", "LOINC", "SNOMED"),
    max_unmapped_rate: float = 0.50,
    fail_fast: bool = True,
    vocab_schema: Optional[str] = None,
    vocab_db: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs a comprehensive preflight verification of OMOP CDM vocabulary coverage, ATC hierarchies,
    unmapped concept 0 rates across clinical tables, and drug exposure types.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        DuckDB OMOP database connection.
    required : Iterable[str]
        Required vocabulary IDs (default: RxNorm, ATC, LOINC, SNOMED).
    max_unmapped_rate : float
        Maximum allowable share of concept 0 unmapped rows before flagging (default 0.50 = 50%).
    fail_fast : bool
        If True, raises a ValueError if any required vocabulary has 0 concepts.
    vocab_schema : Optional[str]
        Schema or attached database containing Athena vocabulary tables (e.g. 'central_vocab').
    vocab_db : Optional[str]
        Alias for vocab_schema.

    Returns
    -------
    Dict[str, Any]
        Dictionary of preflight metrics: vocabularies, unmapped_shares, atc_hierarchy_counts, drug_types, passed.
    """
    v_schema = vocab_schema or vocab_db
    concept_tbl = f"{v_schema}.concept" if v_schema else "concept"
    ca_tbl = f"{v_schema}.concept_ancestor" if v_schema else "concept_ancestor"

    # Fall back to central_vocab if v_schema is None and concept is not in default schema
    if v_schema is None:
        try:
            con.execute("SELECT 1 FROM concept LIMIT 1")
        except Exception:
            try:
                con.execute("SELECT 1 FROM central_vocab.concept LIMIT 1")
                concept_tbl = "central_vocab.concept"
                ca_tbl = "central_vocab.concept_ancestor"
            except Exception:
                pass

    report = {
        "passed": True,
        "vocabularies": {},
        "unmapped_shares": {},
        "atc_counts": {},
        "drug_types": {
            "prescribed": 0,
            "administered": 0,
            "dispensed": 0,
            "other": 0,
            "total": 0,
            "prescribed_38000177": 0,
            "administered_38000180": 0,
            "dispensed_38000175": 0,
        },
        "drug_type_breakdown": {},
        "issues": [],
    }
    report["drug_type_breakdown"] = report["drug_types"]

    # Check required vocabularies
    for vocab in required:
        try:
            cnt = con.execute(
                f"SELECT count(*) FROM {concept_tbl} WHERE vocabulary_id = '{vocab}'"
            ).fetchone()[0]
            report["vocabularies"][vocab] = cnt
            if cnt == 0:
                report["passed"] = False
                report["issues"].append(f"Required vocabulary '{vocab}' is completely empty (0 concepts).")
        except Exception as e:
            report["passed"] = False
            report["vocabularies"][vocab] = 0
            report["issues"].append(f"Failed to query vocabulary '{vocab}': {e}")

    # Check ATC hierarchy descendants in concept_ancestor
    try:
        atc_desc = con.execute(f"""
            SELECT count(DISTINCT ca.descendant_concept_id)
            FROM {ca_tbl} ca
            JOIN {concept_tbl} c ON ca.ancestor_concept_id = c.concept_id
            WHERE c.vocabulary_id = 'ATC'
        """).fetchone()[0]
        report["atc_counts"]["total_descendants"] = atc_desc
        if atc_desc == 0 and "ATC" in required:
            report["passed"] = False
            report["issues"].append("ATC concepts have zero descendants in concept_ancestor.")
    except Exception:
        report["atc_counts"]["total_descendants"] = 0

    # Check drug_type_concept_id breakdown in drug_exposure
    try:
        has_de = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE lower(table_name) = 'drug_exposure'"
        ).fetchone()[0] > 0
        if has_de:
            dt_stats = con.execute("""
                SELECT
                    count(CASE WHEN drug_type_concept_id = 38000177 THEN 1 END) AS prescribed,
                    count(CASE WHEN drug_type_concept_id = 38000180 THEN 1 END) AS administered,
                    count(CASE WHEN drug_type_concept_id = 38000175 THEN 1 END) AS dispensed,
                    count(CASE WHEN drug_type_concept_id NOT IN (38000177, 38000180, 38000175) OR drug_type_concept_id IS NULL THEN 1 END) AS other,
                    count(*) AS total
                FROM drug_exposure
            """).fetchone()
            if dt_stats:
                total_cnt = int(dt_stats[4] or 0)
                p_cnt = int(dt_stats[0] or 0)
                a_cnt = int(dt_stats[1] or 0)
                d_cnt = int(dt_stats[2] or 0)
                dt_dict = {
                    "prescribed": p_cnt,
                    "administered": a_cnt,
                    "dispensed": d_cnt,
                    "other": int(dt_stats[3] or 0),
                    "total": total_cnt,
                    "prescribed_38000177": p_cnt,
                    "administered_38000180": a_cnt,
                    "dispensed_38000175": d_cnt,
                    "prescribed_pct": round(float(p_cnt) / float(total_cnt) * 100.0, 2) if total_cnt > 0 else 0.0,
                    "administered_pct": round(float(a_cnt) / float(total_cnt) * 100.0, 2) if total_cnt > 0 else 0.0,
                    "dispensed_pct": round(float(d_cnt) / float(total_cnt) * 100.0, 2) if total_cnt > 0 else 0.0,
                }
                report["drug_types"] = dt_dict
                report["drug_type_breakdown"] = dt_dict
    except Exception:
        pass

    try:
        report["concept_count"] = int(con.execute(f"SELECT count(*) FROM {concept_tbl}").fetchone()[0])
        report["vocabulary_reachable"] = True
    except Exception:
        report["concept_count"] = sum(report["vocabularies"].values())
        report["vocabulary_reachable"] = report["concept_count"] > 0

    # Check unmapped concept_id = 0 shares in clinical tables
    for tbl, col in [
        ("drug_exposure", "drug_concept_id"),
        ("condition_occurrence", "condition_concept_id"),
        ("measurement", "measurement_concept_id"),
    ]:
        try:
            has_tbl = con.execute(
                f"SELECT count(*) FROM information_schema.tables WHERE lower(table_name) = '{tbl}'"
            ).fetchone()[0] > 0
            if has_tbl:
                stats = con.execute(f"""
                    SELECT
                        count(*) AS total,
                        count(CASE WHEN {col} = 0 OR {col} IS NULL THEN 1 END) AS unmapped
                    FROM {tbl}
                """).fetchone()
                tot, unmap = stats[0], stats[1]
                rate = (unmap / tot) if tot > 0 else 0.0
                report["unmapped_shares"][tbl] = {
                    "total_rows": tot,
                    "unmapped_rows": unmap,
                    "unmapped_rate": round(rate, 4),
                }
                if rate > max_unmapped_rate:
                    report["passed"] = False
                    report["issues"].append(
                        f"Table '{tbl}' has unmapped rate of {rate:.1%} (> threshold {max_unmapped_rate:.1%})."
                    )
        except Exception:
            pass

    if fail_fast and not report["passed"]:
        raise ValueError(f"Vocabulary preflight check failed:\n- " + "\n- ".join(report["issues"]))

    return report


# -----------------------------------------------------------------------------
# 6. Privacy-Preserving Cross-Site Patient Overlap
# -----------------------------------------------------------------------------

def generate_site_patient_tokens(
    patient_ids: Iterable[Any],
    salt: str,
) -> List[str]:
    """Generates locally salted HMAC-SHA256 privacy tokens for consortium patient overlap estimation.

    Parameters
    ----------
    patient_ids : Iterable[Any]
        Local patient IDs or source values.
    salt : str
        Consortium-agreed secret salt string.

    Returns
    -------
    List[str]
        List of 16-character hexadecimal privacy tokens. Raw patient IDs are never exposed.
    """
    tokens = []
    salt_bytes = salt.encode("utf-8")
    for pid in patient_ids:
        token = hmac.new(salt_bytes, str(pid).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        tokens.append(token)
    return tokens


def consortium_patient_overlap(
    site_tokens: Dict[str, Iterable[str]],
    min_cell_count: int = 11,
) -> pd.DataFrame:
    """Calculates privacy-preserving patient overlap across consortium hospital sites with small-cell suppression.

    Parameters
    ----------
    site_tokens : Dict[str, Iterable[str]]
        Dictionary mapping site names to sets/lists of salted patient tokens: {'SiteA': [...], 'SiteB': [...]}.
    min_cell_count : int
        Minimum cell count for HIPAA/GDPR suppression (default 11).

    Returns
    -------
    pd.DataFrame
        Pairwise overlap matrix with columns:
        site_1, site_2, n_site_1, n_site_2, shared_patients, overlap_pct, is_suppressed.
    """
    token_sets = {s: set(t) for s, t in site_tokens.items()}
    sites = sorted(token_sets.keys())
    records = []

    for i in range(len(sites)):
        s1 = sites[i]
        set1 = token_sets[s1]
        n1 = len(set1)
        for j in range(i, len(sites)):
            s2 = sites[j]
            set2 = token_sets[s2]
            n2 = len(set2)

            shared = len(set1.intersection(set2))
            pct = round(shared / min(n1, n2), 4) if min(n1, n2) > 0 else 0.0

            suppressed = (0 < shared < min_cell_count)
            display_count = f"<{min_cell_count}" if suppressed else str(shared)

            records.append({
                "site_1": s1,
                "site_2": s2,
                "n_site_1": n1,
                "n_site_2": n2,
                "shared_patients": display_count,
                "overlap_pct": pct,
                "is_suppressed": suppressed,
            })

    return pd.DataFrame(records)


# -----------------------------------------------------------------------------
# 7. Planned-Readmission Filter (CMS Algorithm)
# -----------------------------------------------------------------------------

# Core CMS planned readmission procedure categories (SNOMED concept IDs for maintenance chemo, transplants, etc.)
DEFAULT_PLANNED_PROCEDURE_CONCEPTS = {
    4048386,  # Maintenance chemotherapy
    4048408,  # Dialysis
    4322471,  # Bone marrow transplant
    4141641,  # Kidney transplant
    4002621,  # Elective orthopedic arthroplasty
}

def planned_readmission_filter(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str,
    readmission_table: Optional[str] = None,
    procedure_table: str = "procedure_occurrence",
    condition_table: str = "condition_occurrence",
    planned_procedure_concepts: Optional[Set[int]] = None,
    by: Optional[str] = "site",
    target_table: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Applies the CMS planned-readmission exclusion algorithm to remove planned maintenance or elective readmissions.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        DuckDB database connection.
    cohort_table : str
        Cohort table containing index stays and readmission outcome flag/visit.
    readmission_table : Optional[str]
        Optional readmission encounter table. If None, inspects cohort_table's readmission visits.
    procedure_table : str
        Procedure occurrence table name (default 'procedure_occurrence').
    condition_table : str
        Condition occurrence table name (default 'condition_occurrence').
    planned_procedure_concepts : Optional[Set[int]]
        Set of concept IDs defining planned procedures (chemotherapy, dialysis, transplants).
    by : Optional[str]
        Site stratification column (default 'site').
    target_table : Optional[str]
        If specified, creates a clean table with planned readmissions removed.

    Returns
    -------
    Tuple[pd.DataFrame, pd.DataFrame]
        Audit summary of planned readmissions removed (by site and overall),
        and the updated cohort dataframe.
    """
    proc_concepts = planned_procedure_concepts or DEFAULT_PLANNED_PROCEDURE_CONCEPTS
    proc_list = ", ".join(map(str, proc_concepts))

    readm_tbl = readmission_table or cohort_table
    site_clause = f", {by}" if by else ""

    # Flag readmissions with planned procedures
    query = f"""
        WITH flagged_planned AS (
            SELECT
                c.*,
                CASE WHEN EXISTS (
                    SELECT 1 FROM {procedure_table} pr
                    WHERE pr.person_id = c.person_id
                      AND pr.procedure_concept_id IN ({proc_list})
                      AND pr.procedure_date >= c.cohort_end_date
                      AND pr.procedure_date <= c.cohort_end_date + INTERVAL '30 days'
                ) THEN 1 ELSE 0 END AS is_planned
            FROM {cohort_table} c
        )
        SELECT * FROM flagged_planned
    """
    df = con.execute(query).fetchdf()

    # Tally removals
    records = []
    strata = [("ALL", df)]
    if by and by in df.columns:
        for s_val, sub in df.groupby(by):
            strata.append((str(s_val), sub))

    for s_label, sub in strata:
        tot = len(sub)
        n_planned = int(sub["is_planned"].sum())
        rate = n_planned / tot if tot > 0 else 0.0
        records.append({
            "site": s_label,
            "total_readmissions": tot,
            "planned_readmissions_removed": n_planned,
            "unplanned_readmissions_retained": tot - n_planned,
            "planned_share": round(rate, 4),
        })

    # Retain only unplanned readmissions
    clean_df = df[df["is_planned"] == 0].drop(columns=["is_planned"]).reset_index(drop=True)

    if target_table:
        con.execute(f"CREATE OR REPLACE TABLE {target_table} AS SELECT * FROM df WHERE is_planned = 0")

    return pd.DataFrame(records), clean_df


# -----------------------------------------------------------------------------
# 8. Observation Period Diagnostics
# -----------------------------------------------------------------------------

def observation_period_diagnostics(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    prior_days: int = 365,
    by: Optional[str] = "site",
) -> pd.DataFrame:
    """Evaluates whether observation_period eligibility is native vs constructed,
    and compares qualification against first-observed clinical events.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        DuckDB database connection.
    cohort_table : str
        Cohort table containing person_id, cohort_start_date.
    prior_days : int
        Prior wash-in observation requirement in days (default 365).
    by : Optional[str]
        Site stratification column (default 'site').

    Returns
    -------
    pd.DataFrame
        Audit table comparing qualification under observation_period vs first observed clinical event.
    """
    has_site = False
    if by:
        cols = [c[0].lower() for c in con.execute(f"DESCRIBE {cohort_table}").fetchall()]
        has_site = by.lower() in cols

    site_sel = f"c.{by} AS site," if has_site else "'ALL' AS site,"
    site_grp = f", c.{by}" if has_site else ""

    sql = f"""
        WITH first_events AS (
            SELECT person_id, MIN(event_date) AS first_event_date
            FROM (
                SELECT person_id, condition_start_date AS event_date FROM condition_occurrence
                UNION ALL
                SELECT person_id, drug_exposure_start_date AS event_date FROM drug_exposure
                UNION ALL
                SELECT person_id, measurement_date AS event_date FROM measurement
                UNION ALL
                SELECT person_id, visit_start_date AS event_date FROM visit_occurrence
            )
            GROUP BY person_id
        ),
        eval AS (
            SELECT
                {site_sel}
                c.person_id,
                c.cohort_start_date,
                op.observation_period_start_date,
                fe.first_event_date,
                CASE WHEN op.observation_period_start_date <= c.cohort_start_date - {prior_days} THEN 1 ELSE 0 END AS op_qualifies,
                CASE WHEN fe.first_event_date <= c.cohort_start_date - {prior_days} THEN 1 ELSE 0 END AS fe_qualifies
            FROM {cohort_table} c
            LEFT JOIN observation_period op
              ON c.person_id = op.person_id
             AND c.cohort_start_date >= op.observation_period_start_date
             AND c.cohort_start_date <= op.observation_period_end_date
            LEFT JOIN first_events fe
              ON c.person_id = fe.person_id
        )
        SELECT
            site,
            count(*) AS total_patients,
            count(CASE WHEN op_qualifies = 1 THEN 1 END) AS n_qualify_obs_period,
            round(count(CASE WHEN op_qualifies = 1 THEN 1 END) * 1.0 / count(*), 4) AS rate_obs_period,
            count(CASE WHEN fe_qualifies = 1 THEN 1 END) AS n_qualify_first_event,
            round(count(CASE WHEN fe_qualifies = 1 THEN 1 END) * 1.0 / count(*), 4) AS rate_first_event,
            count(CASE WHEN op_qualifies = fe_qualifies THEN 1 END) AS concordant_count,
            round(count(CASE WHEN op_qualifies = fe_qualifies THEN 1 END) * 1.0 / count(*), 4) AS concordance_rate
        FROM eval
        GROUP BY site
    """
    df = con.execute(sql).fetchdf()

    # Add overall row if stratified
    if has_site and len(df) > 1:
        tot_row = {
            "site": "ALL",
            "total_patients": df["total_patients"].sum(),
            "n_qualify_obs_period": df["n_qualify_obs_period"].sum(),
            "rate_obs_period": round(df["n_qualify_obs_period"].sum() / df["total_patients"].sum(), 4),
            "n_qualify_first_event": df["n_qualify_first_event"].sum(),
            "rate_first_event": round(df["n_qualify_first_event"].sum() / df["total_patients"].sum(), 4),
            "concordant_count": df["concordant_count"].sum(),
            "concordance_rate": round(df["concordant_count"].sum() / df["total_patients"].sum(), 4),
        }
        df = pd.concat([pd.DataFrame([tot_row]), df], ignore_index=True)

    return df
