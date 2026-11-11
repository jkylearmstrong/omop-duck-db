"""Study report tables: the aggregate-only tables behind a readmission-style prediction manuscript (unreleased).

Given a patient-level feature table (one randomly drawn index stay per patient is the usual cohort, see
``define_study_cohort(sample_stay_rule="random")``) this module produces the tables a protocol of this kind reports, from
out-of-fold (OOF) predictions with training folds balanced 50/50:

* :func:`prepare_model_matrix`  - feature table -> numeric ``X`` / ``y`` (dummy coding, all-NaN columns dropped).
* :func:`oof_predictions`       - stratified (optionally patient-grouped) k-fold; median imputation, optional scaling and
  upsampling to 50/50 are fitted on the training fold only.
* :func:`default_model_grid`    - logistic regression, random forest, HistGBM and XGBoost, benchmark configuration first.
* :func:`tune_models`           - every configuration scored OOF; picks the best by AUC-ROC and by F1 beside the fixed
  benchmark. Returns the tuning comparison table and the performance table (with bootstrap CIs) of the F1-tuned models.
* :func:`stay_type_auc`         - AUC for stays that were the patient's first vs a later admission.
* :func:`random_stay_draw` / :func:`seed_sensitivity` - the seeded one-stay-per-patient draw and its stability over seeds.
* :func:`los_by_site`, :func:`index_stay_dates` - length-of-stay table and the observed admission / discharge dates.
* :func:`export_workbook`       - the tables as one formatted Excel workbook with a manifest sheet.
* :func:`plot_cohort_flowchart` - Figure 1, the cohort selection flowchart, from an attrition table.
* :func:`explain_models` / :func:`plot_shap_beeswarm` / :func:`plot_shap_panels` - SHAP values for the four benchmark models,
  the ranked-predictor table and the beeswarm / 2 x 2 composite figures.
* :func:`build_study_tables`    - runs the tables (and, on request, the figures) and writes them to a folder.

Everything returned or written is aggregate (metrics, counts, intervals); no row-level prediction leaves these functions
unless you keep the ``oof`` dictionary yourself. Counts below ``min_cell`` are blanked with :mod:`omop_etl.disclosure`
where a table shows counts by subgroup. scikit-learn is required (``pip install 'omop-duck-db[sklearn]'``); xgboost is
used when installed (``[bundle]``) and its rows are skipped otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "prepare_model_matrix", "classification_metrics", "bootstrap_ci", "oof_predictions", "default_model_grid",
    "tune_models", "stay_type_auc", "random_stay_draw", "seed_sensitivity", "los_by_site", "index_stay_dates",
    "export_workbook", "build_study_tables", "DEFAULT_ID_COLUMNS", "plot_cohort_flowchart", "explain_models", "shap_ranking",
    "plot_shap_beeswarm", "plot_shap_panels",
]

DEFAULT_ID_COLUMNS = (
    "person_id", "visit_occurrence_id", "admit_date", "discharge_date", "encounterid", "patid", "site_source", "site_anon", "site_id",
)


def _sk():
    try:
        import sklearn  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError("scikit-learn is required: pip install 'omop-duck-db[sklearn]'") from exc


# ------------------------------------------------------------------ feature matrix
def prepare_model_matrix(df: pd.DataFrame, *, label_col: str = "readmitted_30d", drop_cols: Sequence[str] = (),
                         id_cols: Sequence[str] = DEFAULT_ID_COLUMNS) -> tuple[pd.DataFrame, pd.Series]:
    """Feature table -> ``(X, y)``: identifiers and ``drop_cols`` removed, numeric-looking text converted, categoricals dummy
    coded (first level dropped), characters XGBoost rejects (``[ ] <``) replaced, all-NaN columns dropped."""
    if label_col not in df:
        raise ValueError(f"label column {label_col!r} not found.")
    y = df[label_col].astype(int).reset_index(drop=True)
    drop = [c for c in [*id_cols, *drop_cols, label_col] if c in df.columns]
    X = df.drop(columns=drop).reset_index(drop=True)
    for c in X.select_dtypes(include=["object"]).columns:  # e.g. DuckDB DECIMAL arriving as Python Decimal
        conv = pd.to_numeric(X[c], errors="coerce")
        if conv.notna().sum() == X[c].notna().sum():
            X[c] = conv.astype(float)
    cats = X.select_dtypes(include=["object", "category"]).columns.tolist()
    if cats:
        X = pd.get_dummies(X, columns=cats, drop_first=True)
    X = X.astype({c: float for c in X.columns if X[c].dtype == bool})
    X.columns = [str(c).replace("<", "under ").replace("[", "(").replace("]", ")") for c in X.columns]
    return X.loc[:, ~X.isna().all()], y


# ------------------------------------------------------------------ metrics
def classification_metrics(y_true: Any, y_prob: Any, threshold: float = 0.5) -> dict[str, float]:
    """AUC, F1, sensitivity, specificity, accuracy and Brier score (rounded to 3 places) plus unrounded ``auc_raw`` / ``f1_raw``."""
    _sk()
    from sklearn.metrics import (accuracy_score, brier_score_loss, confusion_matrix, f1_score, recall_score, roc_auc_score)

    y, p = np.asarray(y_true).astype(int), np.asarray(y_prob, dtype=float)
    pred = (p >= threshold).astype(int)
    auc, f1 = float(roc_auc_score(y, p)), float(f1_score(y, pred, zero_division=0))
    tn, fp, _fn, _tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "AUC": round(auc, 3), "F1": round(f1, 3), "Sensitivity": round(float(recall_score(y, pred, zero_division=0)), 3),
        "Specificity": round(float(tn / (tn + fp)) if (tn + fp) else 0.0, 3), "Accuracy": round(float(accuracy_score(y, pred)), 3),
        "Brier": round(float(brier_score_loss(y, p)), 3), "auc_raw": auc, "f1_raw": f1,
    }


def bootstrap_ci(y_true: Any, y_prob: Any, *, n_bootstraps: int = 100, seed: int = 42, threshold: float = 0.5,
                 groups: Any = None) -> dict[str, tuple[float, float]]:
    """95% percentile bootstrap intervals for AUC, F1, sensitivity, specificity and Brier score.

    With ``groups`` (patient codes) whole patients are resampled instead of rows (cluster bootstrap) by weighting."""
    _sk()
    from sklearn.metrics import brier_score_loss, f1_score, recall_score, roc_auc_score

    y, p = np.asarray(y_true).astype(int), np.asarray(y_prob, dtype=float)
    rng = np.random.RandomState(seed)
    keys = ["AUC", "F1", "Sensitivity", "Specificity", "Brier"]
    vals: dict[str, list[float]] = {k: [] for k in keys}
    n = len(y)
    g = None
    if groups is not None:
        _, g = np.unique(np.asarray(groups), return_inverse=True)
    for _ in range(int(n_bootstraps)):
        if g is not None:
            w = np.bincount(rng.randint(0, g.max() + 1, g.max() + 1), minlength=g.max() + 1)[g].astype(float)
            idx, sw = np.arange(n), w
        else:
            idx, sw = rng.randint(0, n, n), None
        yy, pp = y[idx], p[idx]
        if len(np.unique(yy[(sw > 0) if sw is not None else slice(None)])) < 2:
            continue
        pred = (pp >= threshold).astype(int)
        neg = yy == 0
        wn = sw if sw is not None else np.ones(len(yy))
        vals["AUC"].append(roc_auc_score(yy, pp, sample_weight=sw))
        vals["F1"].append(f1_score(yy, pred, zero_division=0, sample_weight=sw))
        vals["Sensitivity"].append(recall_score(yy, pred, zero_division=0, sample_weight=sw))
        denom = float(wn[neg].sum())
        vals["Specificity"].append(float(wn[neg & (pred == 0)].sum() / denom) if denom else 0.0)
        vals["Brier"].append(brier_score_loss(yy, pp, sample_weight=sw))
    return {k: ((round(float(np.percentile(v, 2.5)), 3), round(float(np.percentile(v, 97.5)), 3)) if v else (0.0, 0.0))
            for k, v in vals.items()}


# ------------------------------------------------------------------ out-of-fold predictions
def oof_predictions(model_factory: Callable[[], Any], X: pd.DataFrame, y: pd.Series | np.ndarray, *, n_splits: int = 5,
                    scale: bool = False, balanced: bool = True, groups: Any = None, random_state: int = 42,
                    splits: Sequence[tuple[np.ndarray, np.ndarray]] | None = None) -> np.ndarray:
    """Out-of-fold probabilities. Imputation (median), scaling and the 50/50 upsampling of the minority class are fitted on
    each training fold only; validation folds keep their natural distribution. ``groups`` keeps a patient's rows in one fold."""
    _sk()
    from sklearn.impute import SimpleImputer
    from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold
    from sklearn.preprocessing import StandardScaler
    from sklearn.utils import resample

    yv = np.asarray(y).astype(int)
    if splits is None:
        splits = (StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv, groups)
                  if groups is not None else
                  StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv))
    out = np.zeros(len(yv))
    for tr, va in splits:
        Xtr, ytr = X.iloc[tr], yv[tr]
        if balanced:
            pos, neg = np.flatnonzero(ytr == 1), np.flatnonzero(ytr == 0)
            minority, majority = (pos, neg) if len(pos) <= len(neg) else (neg, pos)
            up = resample(minority, replace=True, n_samples=len(majority), random_state=random_state)
            keep = np.concatenate([majority, up])
            Xtr, ytr = Xtr.iloc[keep], ytr[keep]
        imp = SimpleImputer(strategy="median", keep_empty_features=True)
        A, B = imp.fit_transform(Xtr), imp.transform(X.iloc[va])
        if scale:
            sc = StandardScaler()
            A, B = sc.fit_transform(A), sc.transform(B)
        m = model_factory()
        m.fit(A, ytr)
        out[va] = m.predict_proba(B)[:, 1]
    return out


# ------------------------------------------------------------------ model grid and tuning
def default_model_grid(random_state: int = 42, n_jobs: int = -1) -> dict[str, tuple[bool, list[tuple[str, Callable[[], Any]]]]]:
    """``{model: (needs_scaling, [(label, factory), ...])}``; the first configuration of each model is the fixed benchmark.

    XGBoost is left out when the package is not installed."""
    _sk()
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression

    def lr(C):
        return lambda: LogisticRegression(C=C, penalty="l2", max_iter=500, random_state=random_state)

    def rf(depth, leaf=1):
        return lambda: RandomForestClassifier(n_estimators=100, max_depth=depth, min_samples_split=10, min_samples_leaf=leaf,
                                              n_jobs=n_jobs, random_state=random_state)

    def hgb(it, lrate, depth):
        return lambda: HistGradientBoostingClassifier(max_iter=it, learning_rate=lrate, max_depth=depth, l2_regularization=1.0,
                                                      random_state=random_state)

    grid: dict[str, tuple[bool, list[tuple[str, Callable[[], Any]]]]] = {
        "Logistic Regression": (True, [("C=1 (benchmark)", lr(1.0)), ("C=0.01", lr(0.01)), ("C=0.1", lr(0.1)), ("C=10", lr(10.0))]),
        "Random Forest": (False, [("depth 12 (benchmark)", rf(12)), ("depth 8", rf(8)), ("depth 16", rf(16))]),
        "HistGBM": (False, [("100 iter, lr 0.1, depth 6 (benchmark)", hgb(100, 0.1, 6)), ("300 iter, lr 0.05, depth 6", hgb(300, 0.05, 6)),
                            ("300 iter, lr 0.1, depth 4", hgb(300, 0.1, 4))]),
    }
    try:
        import xgboost as xgb

        def xg(n, lrate, depth):
            return lambda: xgb.XGBClassifier(n_estimators=n, max_depth=depth, learning_rate=lrate, tree_method="hist", subsample=0.8,
                                             colsample_bytree=0.8, eval_metric="logloss", random_state=random_state, n_jobs=n_jobs)
        grid["XGBoost"] = (False, [("100 trees, lr 0.1, depth 6 (benchmark)", xg(100, 0.1, 6)),
                                   ("300 trees, lr 0.05, depth 6", xg(300, 0.05, 6)), ("300 trees, lr 0.1, depth 4", xg(300, 0.1, 4))])
    except ImportError:
        pass
    return grid


_MODEL_ORDER = ("HistGBM", "XGBoost", "Random Forest", "Logistic Regression")


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def tune_models(X: pd.DataFrame, y: pd.Series | np.ndarray, *, grid: Mapping[str, Any] | None = None, n_splits: int = 5,
                balanced: bool = True, groups: Any = None, n_bootstraps: int = 100, random_state: int = 42,
                keep_oof: bool = False) -> dict[str, Any]:
    """Scores every configuration OOF, then compares the benchmark configuration with the one chosen by AUC-ROC and by F1.

    Returns a dict with ``tuning_table`` (Model, Selection, metrics), ``performance_table`` (the F1-tuned model per
    algorithm with AUC 95% CI), ``all_configs`` (every configuration), ``summary`` (selected labels per model) and, if
    ``keep_oof``, ``oof`` (``{(model, config): probabilities}``; row-level, so not for sharing).

    The selection and the reported metrics use the same OOF predictions (not nested), so tuned rows are slightly optimistic."""
    _sk()
    from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold

    grid = dict(grid) if grid is not None else default_model_grid(random_state)
    yv = np.asarray(y).astype(int)
    splits = list((StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv, groups)
                   if groups is not None else StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv)))
    all_rows: list[dict[str, Any]] = []
    tuning_rows: list[dict[str, Any]] = []
    perf_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    oof_keep: dict[tuple[str, str], np.ndarray] = {}
    for name in [m for m in _MODEL_ORDER if m in grid] + [m for m in grid if m not in _MODEL_ORDER]:
        scale, cfgs = grid[name]
        scored = []
        for label, factory in cfgs:
            p = oof_predictions(factory, X, yv, scale=scale, balanced=balanced, splits=splits, random_state=random_state)
            m = classification_metrics(yv, p)
            scored.append((label, m, p))
            all_rows.append({"Model": name, "Configuration": label, **{k: v for k, v in m.items()}})
            if keep_oof:
                oof_keep[(name, label)] = p
        fixed = scored[0]
        by_auc = max(scored, key=lambda s: s[1]["auc_raw"])
        by_f1 = max(scored, key=lambda s: s[1]["f1_raw"])
        summary[name] = {"fixed": fixed[0], "best_by_auc": by_auc[0], "best_by_f1": by_f1[0], "same": by_auc[0] == by_f1[0],
                         "n_configs": len(scored)}
        for how, (label, m, _p) in (("Fixed (benchmark)", fixed), ("Tuned for AUC-ROC", by_auc), ("Tuned for F1", by_f1)):
            tuning_rows.append({"Model": name, "Selection": how, "Configuration": label.replace(" (benchmark)", ""),
                                "AUC-ROC": _fmt(m["AUC"]), "Sensitivity": _fmt(m["Sensitivity"]), "Specificity": _fmt(m["Specificity"]),
                                "F1": _fmt(m["F1"]), "Brier": _fmt(m["Brier"])})
        ci = bootstrap_ci(yv, by_f1[2], n_bootstraps=n_bootstraps, seed=random_state, groups=groups)
        m = by_f1[1]
        perf_rows.append({"Model": name, "AUC-ROC (95% CI)": f"{_fmt(m['AUC'])} ({_fmt(ci['AUC'][0])}–{_fmt(ci['AUC'][1])})",
                          "Sensitivity": _fmt(m["Sensitivity"]), "Specificity": _fmt(m["Specificity"]), "F1": _fmt(m["F1"]),
                          "Brier": _fmt(m["Brier"])})
    out = {"tuning_table": pd.DataFrame(tuning_rows), "performance_table": pd.DataFrame(perf_rows),
           "all_configs": pd.DataFrame(all_rows), "summary": summary}
    if keep_oof:
        out["oof"] = oof_keep
    return out


# ------------------------------------------------------------------ stay type, draws, seeds
def stay_type_auc(y: Any, prob: Any, is_first: Any, *, model_name: str = "HistGBM", min_cell: int | None = 11) -> pd.DataFrame:
    """AUC-ROC (and readmission rate) separately for index stays that were the patient's first admission and for later ones.

    ``is_first`` is a boolean per row (for example ``stay_rank == 1`` or ``prior_ip_count == 0``). A subset with fewer than
    ``min_cell`` stays, or a single outcome class, gets a blank AUC and rate."""
    from sklearn.metrics import roc_auc_score

    yv, p, first = np.asarray(y).astype(int), np.asarray(prob, dtype=float), np.asarray(is_first).astype(bool)
    rows = []
    for label, m in (("Drawn stay was patient's first admission", first), ("Drawn stay was a later admission", ~first)):
        n = int(m.sum())
        small = min_cell is not None and 0 < n < min_cell
        ok = n > 0 and not small and len(np.unique(yv[m])) == 2
        rows.append({"Subset": label, "Stays": ("<" + str(min_cell)) if small else n, "Share": (n / len(yv)) if not small and len(yv) else np.nan,
                     "Readmit rate": float(yv[m].mean()) if ok else np.nan, f"{model_name} AUC": float(roc_auc_score(yv[m], p[m])) if ok else np.nan})
    return pd.DataFrame(rows)


def random_stay_draw(df: pd.DataFrame, seed: int, *, site_col: str = "site_source", person_col: str = "person_id",
                     visit_col: str = "visit_occurrence_id") -> pd.DataFrame:
    """One stay per patient chosen by a seeded uniform draw (``numpy.random.default_rng(seed)``), deterministic for the data and seed.

    Rows are sorted by site, person and visit first, so the result does not depend on input order. Patients are
    ``site:person`` pairs. (The in-database equivalent is ``define_study_cohort(sample_stay_rule="random", random_seed=)``,
    which uses a different, MD5-based rule; the two draws are both valid but not identical.)"""
    d = df.sort_values([site_col, person_col, visit_col], kind="mergesort").reset_index(drop=True)
    codes, _ = pd.factorize(d[site_col].astype(str) + ":" + d[person_col].astype(str))
    u = np.random.default_rng(seed).random(len(d))
    d = d.assign(_u=u, _g=codes)
    pick = d.loc[d.groupby("_g")["_u"].idxmin()].drop(columns=["_u", "_g"])
    return pick.sort_values([site_col, person_col], kind="mergesort").reset_index(drop=True)


def seed_sensitivity(all_stays: pd.DataFrame, seeds: Sequence[int], *, model_factory: Callable[[], Any] | None = None,
                     model_name: str = "HistGBM", label_col: str = "readmitted_30d", site_col: str = "site_source",
                     person_col: str = "person_id", visit_col: str = "visit_occurrence_id", balanced: bool = True,
                     n_splits: int = 5, random_state: int = 42) -> pd.DataFrame:
    """OOF AUC-ROC of one model on the cohort drawn with each seed: how much the random-stay choice matters.

    Returns ``Seed, Stays (= patients), Readmit rate, <model> AUC`` (one row per seed)."""
    _sk()
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    factory = model_factory or (lambda: HistGradientBoostingClassifier(max_iter=100, max_depth=6, l2_regularization=1.0, random_state=random_state))
    rows = []
    for s in seeds:
        pick = random_stay_draw(all_stays, int(s), site_col=site_col, person_col=person_col, visit_col=visit_col)
        X, y = prepare_model_matrix(pick, label_col=label_col)
        p = oof_predictions(factory, X, y, balanced=balanced, n_splits=n_splits, random_state=random_state)
        rows.append({"Seed": int(s), "Stays (= patients)": int(len(y)), "Readmit rate": round(float(y.mean()), 4),
                     f"{model_name} AUC": round(float(roc_auc_score(y, p)), 4)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ descriptive tables
def los_by_site(df: pd.DataFrame, *, site_col: str = "site_anon", los_col: str = "index_los_days",
                label_col: str = "readmitted_30d", min_cell: int | None = 11) -> pd.DataFrame:
    """Length of stay (days) by site and overall: mean ± SD, median [IQR] for all, readmitted and not, with Welch t and Mann-Whitney p.

    A site (or outcome group) with fewer than ``min_cell`` stays is reported with its count blanked as ``<min_cell``
    and no statistics."""
    from scipy import stats

    def p_text(p):
        return "<0.001" if p < 0.001 else f"{p:.3f}"

    def ms(s):
        return f"{s.mean():.2f} ± {s.std():.2f}"

    def md(s):
        return f"{s.median():.1f} [{s.quantile(.25):.1f}–{s.quantile(.75):.1f}]"

    cols = ["Health System", "Total Cohort (N)", "Readmitted (N)", "Non-Readmitted (N)", "Readmission Rate (%)", "Overall Mean ± SD (days)",
            "Readmitted Mean ± SD (days)", "Non-Readmitted Mean ± SD (days)", "Overall Median [IQR] (days)", "Readmitted Median [IQR] (days)",
            "Non-Readmitted Median [IQR] (days)", "Readmitted Max (days)", "Non-Readmitted Max (days)", "Two-Sample T-Test P-Value",
            "Mann-Whitney U Test P-Value"]
    groups = [(str(s), df[df[site_col] == s]) for s in sorted(df[site_col].dropna().unique())] + [("All Sites Combined", df)]
    rows = []
    for name, d in groups:
        y = d[label_col].astype(bool)
        a, b = d.loc[y, los_col].dropna(), d.loc[~y, los_col].dropna()
        if min_cell is not None and (len(d) < min_cell or len(a) < min_cell or len(b) < min_cell):
            rows.append([name, f"<{min_cell}" if len(d) < min_cell else len(d)] + [""] * (len(cols) - 2))
            continue
        allv = d[los_col].dropna()
        rows.append([name, len(d), int(y.sum()), int((~y).sum()), f"{100 * y.mean():.2f}%", ms(allv), ms(a), ms(b), md(allv), md(a), md(b),
                     a.max(), b.max(), p_text(stats.ttest_ind(a, b, equal_var=False).pvalue), p_text(stats.mannwhitneyu(a, b).pvalue)])
    return pd.DataFrame(rows, columns=cols)


def index_stay_dates(df: pd.DataFrame, *, admit_col: str = "admit_date", discharge_col: str = "discharge_date",
                     site_col: str = "site_anon", window_start: str | None = None, window_end: str | None = None) -> dict[str, Any]:
    """Observed first/last admission and last discharge dates, overall and by site, plus counts outside an optional window.

    Use it to state in the Methods exactly which dates the analytic cohort covers (a stay admitted on the last day of the
    window is discharged later, so the last discharge date is after the window end)."""
    a, d = pd.to_datetime(df[admit_col]), pd.to_datetime(df[discharge_col])

    def block(mask):
        return {"n": int(mask.sum()), "admit_first": str(a[mask].min().date()), "admit_last": str(a[mask].max().date()),
                "discharge_last": str(d[mask].max().date())}

    out = block(pd.Series(True, index=df.index))
    out["sites"] = {str(s): block(df[site_col] == s) for s in sorted(df[site_col].dropna().unique())}
    if window_end:
        out["discharged_after_window_end"] = int((d > pd.Timestamp(window_end)).sum())
    if window_start:
        out["admitted_before_window_start"] = int((a < pd.Timestamp(window_start)).sum())
    return out


# ------------------------------------------------------------------ workbook
def export_workbook(sheets: Sequence[tuple[str, str, pd.DataFrame]], path: str | Path, *, title: str = "Study tables",
                    subtitle: str = "") -> Path:
    """Writes ``[(sheet_name, caption, table), ...]`` to one formatted .xlsx with a manifest sheet listing each table.

    Sheet names are truncated to Excel's 31 characters. Requires openpyxl (``pip install openpyxl``)."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover
        raise ImportError("openpyxl is required for export_workbook: pip install openpyxl") from exc
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "Manifest"
    head_fill, head_font = PatternFill("solid", start_color="1F4E79", end_color="1F4E79"), Font(bold=True, color="FFFFFF")
    ws0["A1"], ws0["A1"].font = title, Font(bold=True, size=14, color="1F4E79")
    if subtitle:
        ws0["A2"] = subtitle
    for j, h in enumerate(["Sheet", "Caption", "Rows"], 1):
        c = ws0.cell(row=4, column=j, value=h)
        c.fill, c.font = head_fill, head_font
    used: set[str] = set()
    for i, (name, caption, table) in enumerate(sheets, 5):
        sheet = name[:31]
        k = 1
        while sheet in used:
            sheet = f"{name[:28]}_{k}"
            k += 1
        used.add(sheet)
        ws = wb.create_sheet(sheet)
        ws["A1"], ws["A1"].font = caption, Font(bold=True, color="1F4E79")
        for j, col in enumerate(table.columns, 1):
            c = ws.cell(row=3, column=j, value=str(col))
            c.fill, c.font, c.alignment = head_fill, head_font, Alignment(wrap_text=True, vertical="center")
        for r, row in enumerate(table.itertuples(index=False), 4):
            for j, v in enumerate(row, 1):
                ws.cell(row=r, column=j, value=None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v))
        for j, col in enumerate(table.columns, 1):
            width = max([len(str(col))] + [len(str(v)) for v in table.iloc[:, j - 1].head(200)] + [8])
            ws.column_dimensions[get_column_letter(j)].width = min(width + 2, 60)
        ws.freeze_panes = "B4"
        ws0.cell(row=i, column=1, value=sheet)
        ws0.cell(row=i, column=2, value=caption)
        ws0.cell(row=i, column=3, value=int(len(table)))
    ws0.column_dimensions["A"].width, ws0.column_dimensions["B"].width = 34, 110
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ------------------------------------------------------------------ figures
def _figure(figsize: tuple[float, float]):
    """A matplotlib Figure that does not touch pyplot's global state or need a display."""
    try:
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover
        raise ImportError("matplotlib is required for figures: pip install 'omop-duck-db[plot]'") from exc
    return Figure(figsize=figsize)


def _save(fig: Any, path: str | Path | None, dpi: int) -> None:
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(path), dpi=dpi, bbox_inches="tight")


def plot_cohort_flowchart(attrition: pd.DataFrame, n: int, n_readmitted: int, *, path: str | Path | None = None,
                          first_box: str = "All patients with an inpatient or emergency-to-inpatient stay",
                          final_box: str = "one randomly drawn stay per patient", dpi: int = 200) -> Any:
    """Figure 1: cohort selection flowchart from an attrition table, with the analytic cohort and outcome split at the bottom.

    ``attrition`` needs ``step_name``, ``subjects_retained`` and ``subjects_dropped`` (the shape of
    :func:`omop_etl.consort.consort_from_counts` / ``pool_consort_tables``, summed over sites); the first row is the source
    population. ``n`` is the analytic cohort size and ``n_readmitted`` its events. Returns the matplotlib Figure."""
    from matplotlib.patches import FancyBboxPatch

    for c in ("step_name", "subjects_retained", "subjects_dropped"):
        if c not in attrition:
            raise ValueError(f"attrition needs a {c!r} column.")
    if n <= 0 or not 0 <= n_readmitted <= n:
        raise ValueError("n must be positive and 0 <= n_readmitted <= n.")
    steps = [(str(r.step_name), int(r.subjects_retained), int(r.subjects_dropped)) for r in attrition.itertuples()]
    fig = _figure((9.5, 1.05 * (len(steps) + 2)))
    ax = fig.subplots()
    ax.axis("off")
    h = len(steps) + 1.2
    ax.set_xlim(0, 10)
    for i, (name, kept, dropped) in enumerate(steps):
        y = h - i
        label = first_box if i == 0 else name
        ax.add_patch(FancyBboxPatch((0.2, y - 0.3), 5.2, 0.6, boxstyle="round,pad=0.02", fc="#eef5fb", ec="#2c7fb8", lw=1.6))
        ax.text(2.8, y + 0.08, label if len(label) < 70 else label[:67] + "...", ha="center", va="center", fontsize=8.5, fontweight="bold")
        ax.text(2.8, y - 0.14, f"P = {kept:,} patients", ha="center", va="center", fontsize=8.5)
        if i > 0 and dropped:
            ax.annotate("", xy=(5.9, y + 0.5), xytext=(2.8, y + 0.5), arrowprops=dict(arrowstyle="->", color="#c0392b", ls="--"))
            ax.add_patch(FancyBboxPatch((5.9, y + 0.28), 3.9, 0.46, boxstyle="round,pad=0.02", fc="#fdecea", ec="#e74c3c", lw=1.3))
            ax.text(7.85, y + 0.51, f"Excluded: {name[:46]}  P = {dropped:,}", ha="center", va="center", fontsize=7)
        if i < len(steps) - 1:
            ax.annotate("", xy=(2.8, y - 0.72), xytext=(2.8, y - 0.32), arrowprops=dict(arrowstyle="->", color="#2c3e50", lw=1.5))
    y = h - len(steps) + 0.2
    ax.add_patch(FancyBboxPatch((0.2, y - 0.95), 5.2, 0.6, boxstyle="round,pad=0.02", fc="#e8f6ef", ec="#27ae60", lw=1.8))
    ax.text(2.8, y - 0.65, f"Analytic cohort: {final_box}\nN = P = {n:,}", ha="center", va="center", fontsize=8.5, fontweight="bold")
    ax.add_patch(FancyBboxPatch((0.2, y - 1.95), 2.5, 0.6, boxstyle="round,pad=0.02", fc="#fdecea", ec="#c0392b", lw=1.5))
    ax.text(1.45, y - 1.65, f"30-day readmission\nN = {n_readmitted:,} ({100 * n_readmitted / n:.2f}%)", ha="center", va="center", fontsize=8)
    ax.add_patch(FancyBboxPatch((2.9, y - 1.95), 2.5, 0.6, boxstyle="round,pad=0.02", fc="#e8f6ef", ec="#27ae60", lw=1.5))
    ax.text(4.15, y - 1.65, f"No readmission\nN = {n - n_readmitted:,} ({100 * (n - n_readmitted) / n:.2f}%)", ha="center", va="center", fontsize=8)
    ax.set_ylim(y - 2.2, h + 0.8)
    _save(fig, path, dpi)
    return fig


def shap_ranking(values: Any, data: pd.DataFrame) -> pd.DataFrame:
    """Features ranked by mean |SHAP| with ``direction`` = sign of the correlation between feature value and SHAP value
    (+1 higher values raise risk, -1 lower, 0 undefined or flat). Aggregate: one row per feature."""
    sv = np.asarray(values, dtype=float)
    rows = []
    for j, col in enumerate(data.columns):
        a, b = data[col].to_numpy(dtype=float), sv[:, j]
        rows.append((col, float(np.abs(b).mean()), float(np.sign(np.corrcoef(a, b)[0, 1])) if np.std(a) > 0 and np.std(b) > 0 else 0.0))
    return pd.DataFrame(rows, columns=["feature", "mean_abs_shap", "direction"]).assign(
        mean_abs_shap=lambda d: d["mean_abs_shap"].round(4)).sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)


def _positive_class(sv: Any) -> np.ndarray:
    if isinstance(sv, list):
        sv = sv[-1]
    sv = np.asarray(sv, dtype=float)
    return sv[:, :, -1] if sv.ndim == 3 else sv


def explain_models(X: pd.DataFrame, y: pd.Series | np.ndarray, *, grid: Mapping[str, Any] | None = None,
                   models: Sequence[str] = ("Logistic Regression", "Random Forest", "HistGBM", "XGBoost"), sample_n: int = 2000,
                   n_splits: int = 5, balanced: bool = True, groups: Any = None, random_state: int = 42) -> dict[str, dict[str, Any]]:
    """SHAP values per model, following the benchmark's protocol: each model (its first, benchmark configuration) is trained on
    the balanced training part of the **last** cross-validation fold and explains ``sample_n`` randomly sampled patients.

    Returns ``{model: {"values": (n, p) array, "data": imputed feature DataFrame, "table": shap_ranking}}``. Logistic regression
    uses the exact linear SHAP (``coef * (x - mean)`` in the scaled space) and XGBoost its exact tree contributions, so neither
    needs the ``shap`` package; random forest and HistGBM use ``shap.TreeExplainer`` (``pip install shap``) and are skipped,
    with a warning, when it is missing. The per-patient values are row-level: keep them local, publish only ``table`` or the figure."""
    _sk()
    import warnings

    from sklearn.impute import SimpleImputer
    from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold
    from sklearn.preprocessing import StandardScaler
    from sklearn.utils import resample

    grid = dict(grid) if grid is not None else default_model_grid(random_state)
    yv = np.asarray(y).astype(int)
    splits = list(StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv, groups)
                  if groups is not None else StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state).split(X, yv))
    tr, _va = splits[-1]
    Xtr, ytr = X.iloc[tr], yv[tr]
    if balanced:
        pos, neg = np.flatnonzero(ytr == 1), np.flatnonzero(ytr == 0)
        minority, majority = (pos, neg) if len(pos) <= len(neg) else (neg, pos)
        keep = np.concatenate([majority, resample(minority, replace=True, n_samples=len(majority), random_state=random_state)])
        Xtr, ytr = Xtr.iloc[keep], ytr[keep]
    imp = SimpleImputer(strategy="median", keep_empty_features=True)
    A = imp.fit_transform(Xtr)
    full = pd.DataFrame(imp.transform(X), columns=X.columns)
    sample = np.sort(np.random.RandomState(random_state).choice(len(full), size=min(int(sample_n), len(full)), replace=False))
    data = full.iloc[sample].reset_index(drop=True)
    scaler = StandardScaler().fit(A)
    out: dict[str, dict[str, Any]] = {}
    for name in models:
        if name not in grid:
            warnings.warn(f"{name!r} is not in the model grid; skipped.")
            continue
        need_scale, cfgs = grid[name]
        model = cfgs[0][1]()
        model.fit(scaler.transform(A) if need_scale else A, ytr)
        if name == "Logistic Regression":
            xs = scaler.transform(data.to_numpy())
            vals = model.coef_.ravel() * (xs - scaler.transform(A).mean(axis=0))
        elif name == "XGBoost":
            import xgboost as xgb
            vals = model.get_booster().predict(xgb.DMatrix(data.to_numpy(), feature_names=list(X.columns)), pred_contribs=True)[:, :-1]
        else:
            try:
                import shap
            except ImportError:
                warnings.warn(f"{name}: the 'shap' package is not installed (pip install shap); skipped.")
                continue
            vals = _positive_class(shap.TreeExplainer(model).shap_values(data.to_numpy(), check_additivity=False))
        out[name] = {"values": np.asarray(vals, dtype=float), "data": data, "table": shap_ranking(vals, data)}
    return out


def plot_shap_beeswarm(values: Any, data: pd.DataFrame, *, max_display: int = 15, title: str | None = None, ax: Any = None,
                       path: str | Path | None = None, dpi: int = 200, seed: int = 0) -> Any:
    """Beeswarm of SHAP values for the ``max_display`` most important features (one dot per patient, coloured by the
    feature's value rank from low to high). Returns the Figure (or the Axes' figure when ``ax`` is given).

    Drawn with matplotlib alone, so it looks the same whether or not the ``shap`` package is installed."""
    sv = np.asarray(values, dtype=float)
    if sv.shape != data.shape:
        raise ValueError(f"values {sv.shape} and data {data.shape} must have the same shape.")
    import matplotlib
    from matplotlib import cm

    top = np.argsort(-np.abs(sv).mean(axis=0))[: int(max_display)]
    rng = np.random.default_rng(seed)
    own = ax is None
    if own:
        fig = _figure((8, 0.38 * len(top) + 1.8))
        ax = fig.subplots()
    else:
        fig = ax.figure
    cmap = matplotlib.colormaps["coolwarm"]
    for row, j in enumerate(top[::-1]):
        a = data.iloc[:, j].to_numpy(dtype=float)
        rank = pd.Series(a).rank(pct=True).to_numpy() if np.nanstd(a) > 0 else np.full(len(a), 0.5)
        ax.scatter(sv[:, j], row + rng.uniform(-0.28, 0.28, len(a)), c=cmap(rank), s=5, linewidths=0, rasterized=True)
    ax.axvline(0, color="#888888", lw=0.8)
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels([str(data.columns[j]) for j in top[::-1]], fontsize=8)
    ax.set_xlabel("SHAP value (impact on model output)")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if title:
        ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    if own:
        sm = cm.ScalarMappable(cmap=cmap)
        sm.set_array([0, 1])
        cb = fig.colorbar(sm, ax=ax, ticks=[0, 1], pad=0.02, fraction=0.03)
        cb.ax.set_yticklabels(["Low", "High"])
        cb.set_label("Feature value", fontsize=8)
        _save(fig, path, dpi)
    return fig


def plot_shap_panels(explained: Mapping[str, Mapping[str, Any]], *, top: int = 10, path: str | Path | None = None, dpi: int = 200,
                     order: Sequence[str] = ("Logistic Regression", "Random Forest", "HistGBM", "XGBoost")) -> Any:
    """The 2 x 2 composite of SHAP beeswarms, panels lettered (A)-(D) in ``order``; ``explained`` is :func:`explain_models` output."""
    names = [m for m in order if m in explained] + [m for m in explained if m not in order]
    if not names:
        raise ValueError("explained is empty.")
    rows = (len(names) + 1) // 2
    fig = _figure((18, 4.2 + 0.42 * top * rows))
    for i, name in enumerate(names):
        ax = fig.add_subplot(rows, 2, i + 1)
        plot_shap_beeswarm(explained[name]["values"], explained[name]["data"], max_display=top, ax=ax,
                           title=f"({chr(65 + i)}) {name}")
    fig.tight_layout(w_pad=4, h_pad=2)
    _save(fig, path, dpi)
    return fig


# ------------------------------------------------------------------ driver
def build_study_tables(df: pd.DataFrame, out_dir: str | Path, *, prefix: str = "Study", label_col: str = "readmitted_30d",
                       site_col: str = "site_anon", los_col: str = "index_los_days", first_stay_col: str | None = "prior_ip_count",
                       n_bootstraps: int = 100, loso: bool = True, workbook: bool = True, window: tuple[str, str] | None = None,
                       min_cell: int | None = 11, random_state: int = 42, attrition: pd.DataFrame | None = None,
                       figures: bool = False, shap_sample_n: int = 2000, shap_top: int = 10,
                       first_box: str = "All patients with an inpatient or emergency-to-inpatient stay",
                       final_box: str = "one randomly drawn stay per patient") -> dict[str, Any]:
    """Runs the model benchmark, tuning comparison, stay-type split, length of stay, dates and (optionally) leave-one-site-out
    on ``df`` (one row per patient) and writes ``<prefix>_*.csv`` (plus an .xlsx workbook) to ``out_dir``.

    ``first_stay_col``: a count column that is 0 when the drawn stay was the patient's first admission (``prior_ip_count``);
    ``None`` skips the stay-type table. The leave-one-site-out table uses :func:`omop_etl.transport.evaluate_loso_transport`
    (balanced training, prior-shift calibration), which is not the same estimator as an uncalibrated pooled-OOF split, so
    expect similar but not identical AUCs to a hand-built script.

    ``figures=True`` also writes ``<prefix>_Figure1_Cohort_Flowchart.png`` (needs ``attrition``), ``<prefix>_Figure_SHAP_XGBoost.png``,
    ``<prefix>_Figure_SHAP_4Panel.png`` and ``<prefix>_Table_SHAP_Top_Predictors.csv`` (SHAP is computed on ``shap_sample_n``
    patients; random forest and HistGBM panels need the ``shap`` package and are left out without it). Only the ranked table
    and the figures are written; per-patient SHAP values are not.

    Returns ``{"tables": {name: DataFrame}, "files": [paths], "summary": {...}}``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    X, y = prepare_model_matrix(df, label_col=label_col)
    tuned = tune_models(X, y, n_bootstraps=n_bootstraps, random_state=random_state, keep_oof=True)
    tables: dict[str, pd.DataFrame] = {"Model_Performance": tuned["performance_table"], "Tuning_AUC_vs_F1": tuned["tuning_table"],
                                       "Tuning_All_Configs": tuned["all_configs"]}
    if first_stay_col and first_stay_col in df:
        best = tuned["summary"].get("HistGBM") or next(iter(tuned["summary"].values()))
        model = "HistGBM" if "HistGBM" in tuned["summary"] else next(iter(tuned["summary"]))
        tables["By_Stay_Type_AUC"] = stay_type_auc(y, tuned["oof"][(model, best["best_by_f1"])], (df[first_stay_col].to_numpy() == 0),
                                                   model_name=model, min_cell=min_cell)
    if los_col in df and site_col in df:
        tables["LOS_Distribution_By_Site"] = los_by_site(df, site_col=site_col, los_col=los_col, label_col=label_col, min_cell=min_cell)
    if loso and site_col in df and df[site_col].nunique() >= 2:
        from omop_etl.transport import evaluate_loso_transport

        feats = df.assign(y=df[label_col].astype(int))
        res = evaluate_loso_transport(feats, site_col=site_col, model_type="hist_gbm", label_col="y", n_bootstraps=n_bootstraps,
                                      random_state=random_state)
        tables["LOSO_By_Site"] = res.per_site
    summary: dict[str, Any] = {"n": int(len(y)), "n_features": int(X.shape[1]), "readmit_rate_pct": round(float(y.mean() * 100), 2),
                               "tuning": tuned["summary"]}
    if "admit_date" in df and "discharge_date" in df and site_col in df:
        summary["dates"] = index_stay_dates(df, site_col=site_col, window_start=window[0] if window else None,
                                            window_end=window[1] if window else None)
    files: list[str] = []
    for name, t in tables.items():
        f = out / f"{prefix}_{name}.csv"
        t.to_csv(f, index=False, encoding="utf-8")
        files.append(str(f))
    sf = out / f"{prefix}_summary.json"
    sf.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    files.append(str(sf))
    if figures:
        if attrition is not None:
            f = out / f"{prefix}_Figure1_Cohort_Flowchart.png"
            plot_cohort_flowchart(attrition, int(len(y)), int(y.sum()), path=f, first_box=first_box, final_box=final_box)
            files.append(str(f))
        explained = explain_models(X, y, sample_n=shap_sample_n, random_state=random_state)
        if "XGBoost" in explained:
            e = explained["XGBoost"]
            f = out / f"{prefix}_Figure_SHAP_XGBoost.png"
            plot_shap_beeswarm(e["values"], e["data"], max_display=15, title="SHAP summary: top 15 predictors of 30-day readmission", path=f)
            t = out / f"{prefix}_Table_SHAP_Top_Predictors.csv"
            e["table"].to_csv(t, index=False, encoding="utf-8")
            files += [str(f), str(t)]
        if explained:
            f = out / f"{prefix}_Figure_SHAP_4Panel.png"
            plot_shap_panels(explained, top=shap_top, path=f)
            files.append(str(f))
    if workbook:
        files.append(str(export_workbook([(n[:31], n.replace("_", " "), t) for n, t in tables.items()], out / f"{prefix}_Tables.xlsx",
                                         title=f"{prefix} tables", subtitle=f"N = {len(y):,}; aggregate results only")))
    return {"tables": tables, "files": files, "summary": summary}
