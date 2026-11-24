"""The manuscript's prose as versioned templates, filled from one run's results.

The study run computes; this module only describes. :func:`manuscript_context` reads what a run wrote (the manuscript workbook,
``<prefix>_run.json``, ``<prefix>_summary.json`` and the SHAP table) into one dictionary of numbers and labels, and
:func:`render_manuscript_body` fills the Jinja template of the installed package version with it. Every number in the text is a
template variable, so a rerun gives the same sentences with that run's numbers, and conditional phrasing handles a result that
changes direction (which model leads, whether an interval crosses 1).

Templates live in ``omop_etl/manuscript_templates/v<version>/``: ``manuscript.md.j2`` (the text, with ``{{ tables.<key> }}`` for table
and figure numbers) and ``layout.json`` (the order of the main tables, supplementary tables and figures, which sets their numbers, so a
reordered table renumbers every reference). :func:`template_dir` picks the newest template not newer than the installed package, so a
release that does not change the text keeps the previous one.

A value the run did not record renders as ``[not in this run: <name>]`` and is listed in the result's ``"missing"``; nothing is
guessed. Names, the review-board statement and the author list are not part of the template or the package: they come from a local
``params`` mapping (or JSON / YAML file) that stays with the study team.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["manuscript_context", "render_manuscript_body", "template_dir", "available_templates", "load_params", "MISSING_MARK"]

TEMPLATES = Path(__file__).with_name("manuscript_templates")
MISSING_MARK = "[not in this run: {}]"
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


# ------------------------------------------------------------------ templates
def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def available_templates() -> list[str]:
    """Template versions shipped with this package, oldest first (``["0.32.0", ...]``)."""
    return sorted((p.name[1:] for p in TEMPLATES.glob("v*") if (p / "manuscript.md.j2").exists()), key=_version_key)


def template_dir(version: str | None = None) -> Path:
    """The template folder for ``version`` (default: the installed package version): the newest shipped template that is not
    newer than it. A version older than every template gets the oldest one."""
    if version is None:
        from omop_etl.package_citations import omop_duck_db_version

        version = omop_duck_db_version() or "0"
    have = available_templates()
    if not have:
        raise FileNotFoundError(f"No manuscript templates under {TEMPLATES}.")
    ok = [v for v in have if _version_key(v) <= _version_key(version)]
    return TEMPLATES / f"v{(ok or have)[-1 if ok else 0]}"


def load_params(path: str | Path | None) -> dict[str, Any]:
    """Study-team details that are not part of the package (title, authors, contributions, review-board statement, consortium name),
    from a JSON or YAML file. ``None`` gives an empty mapping (the template then shows neutral placeholders)."""
    if path is None:
        return {}
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yml", ".yaml"):
        import yaml

        return dict(yaml.safe_load(text) or {})
    return dict(json.loads(text))


# ------------------------------------------------------------------ reading the run
def _first_number(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v).replace(",", "").replace("−", "-"))
    return float(m.group()) if m else None


def _ci(v: Any) -> dict[str, float] | None:
    """``"0.731 (0.728–0.735)"`` -> ``{"est": 0.731, "lo": 0.728, "hi": 0.735}``."""
    s = str(v).replace(",", "").replace("−", "-")
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*\((-?\d+(?:\.\d+)?)\s*[–-]\s*(-?\d+(?:\.\d+)?)\)", s)
    return {"est": float(m.group(1)), "lo": float(m.group(2)), "hi": float(m.group(3))} if m else None


def _sheet(tabs: Mapping[str, tuple[str, pd.DataFrame]], prefix: str) -> pd.DataFrame | None:
    for k, (_, t) in tabs.items():
        if k.startswith(prefix) and isinstance(t, pd.DataFrame) and len(t):
            return t
    return None


def _count_pct(v: Any) -> tuple[float | None, float | None]:
    """``"12,122 (51.7%)"`` -> ``(12122, 51.7)``."""
    nums = [float(x) for x in _NUM.findall(str(v).replace(",", ""))]
    return (nums[0] if nums else None, nums[1] if len(nums) > 1 else None)


def _cohort(tabs) -> dict[str, Any]:
    t1 = _sheet(tabs, "Table 1")
    out: dict[str, Any] = {}
    if t1 is None:
        return out
    row = t1[t1.iloc[:, 0].astype(str).str.contains("Unique Patients", na=False)]
    if len(row):
        r = row.iloc[0]
        n, n1, n0 = (_first_number(r.iloc[i]) for i in (1, 2, 3))
        if n:
            out.update(n=int(n), n_readmitted=int(n1 or 0), n_not=int(n0 or 0), rate_pct=100 * (n1 or 0) / n)
    contrast = []
    for pat, what in (("Prior Inpatient", "higher baseline acute care utilization"), ("Length of Stay", "longer index hospital length of stay")):
        row = t1[t1.iloc[:, 0].astype(str).str.contains(pat, na=False)]
        if len(row):
            a1, a0 = _first_number(row.iloc[0].iloc[2]), _first_number(row.iloc[0].iloc[3])
            if a1 is not None and a0 is not None:
                contrast.append(what if a1 > a0 else what.replace("higher", "lower").replace("longer", "shorter"))
    if contrast:
        out["contrast"] = contrast
    ama = t1[t1.iloc[:, 0].astype(str).str.contains("Against Medical Advice", na=False)]
    if len(ama) and out.get("n"):
        a, _ = _count_pct(ama.iloc[0].iloc[1])
        a1, _ = _count_pct(ama.iloc[0].iloc[2])
        if a:
            out["ama_n"] = int(a)
            out["ama_rate_pct"] = 100 * (a1 or 0) / a
            rest = out["n"] - a
            out["non_ama_rate_pct"] = 100 * (out["n_readmitted"] - (a1 or 0)) / rest if rest else None
    return out


def _models(tabs) -> dict[str, Any]:
    t2 = _sheet(tabs, "Table 2")
    if t2 is None:
        return {}
    models: dict[str, dict[str, Any]] = {}
    auc_col = next((c for c in t2.columns if "AUC" in c), None)
    for _, r in t2.iterrows():
        name = str(r.iloc[0]).strip()
        ci = _ci(r[auc_col]) if auc_col else None
        if not ci:
            continue
        models[name] = {"auc": ci["est"], "lo": ci["lo"], "hi": ci["hi"], "auc_text": str(r[auc_col]).strip(),
                        **{k.lower(): _first_number(r[c]) for k, c in ((k, c) for c in t2.columns for k in ("Sensitivity", "Specificity", "F1", "Brier") if c == k)}}
    if not models:
        return {}
    vals = list(models.values())
    rng = lambda key: (min(v[key] for v in vals if v.get(key) is not None), max(v[key] for v in vals if v.get(key) is not None))  # noqa: E731
    best = max(models, key=lambda m: models[m]["auc"])
    trees = [m for m in models if m in ("HistGBM", "XGBoost", "Random Forest")]
    boosting = [m for m in models if m in ("HistGBM", "XGBoost")]
    out = {"list": models, "names": list(models), "best": best, "auc_range": rng("auc"), "sens_range": rng("sensitivity"),
           "spec_range": rng("specificity"), "brier_range": rng("brier"), "n": len(models)}
    if boosting and "Logistic Regression" in models:
        out["boosting_auc_range"] = (min(models[m]["auc"] for m in boosting), max(models[m]["auc"] for m in boosting))
        out["boosting_beats_lr"] = out["boosting_auc_range"][0] > models["Logistic Regression"]["auc"]
    out["tree_models"] = trees
    return out


def _tuning(tabs) -> dict[str, Any]:
    t3 = _sheet(tabs, "Table 3")
    if t3 is None or "Selection" not in t3.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    model = None
    for _, r in t3.iterrows():
        model = str(r.iloc[0]).strip() if str(r.iloc[0]).strip() not in ("", "nan") else model
        sel = str(r["Selection"]).lower()
        key = "fixed" if "fixed" in sel else "auc" if "auc" in sel else "f1" if "f1" in sel else None
        if model and key:
            cfg = str(r.get("Configuration", "")).replace("(benchmark)", "").strip()
            out.setdefault(model, {})[key] = cfg
            out[model][key + "_auc"] = _first_number(r.get("AUC-ROC"))
    return out


def _loso(tabs) -> dict[str, Any]:
    t5 = _sheet(tabs, "Table 5")
    if t5 is None:
        return {}
    auc_col = next((c for c in t5.columns if "AUC" in c), None)
    site_rows = t5[t5.iloc[:, 0].astype(str).str.match(r"^Site\s+\S+$")]
    aucs = [c["est"] for c in (_ci(v) for v in site_rows[auc_col]) if c] if auc_col else []
    if not aucs:
        return {}
    out = {"n_sites": len(aucs), "auc_min": min(aucs), "auc_max": max(aucs), "auc_mean": sum(aucs) / len(aucs)}
    mean_row = t5[t5.iloc[:, 0].astype(str).str.startswith("Mean of")]
    if auc_col and len(mean_row):  # the text quotes the table's own (rounded) mean, not a mean of the rounded site values
        m = _first_number(mean_row.iloc[0][auc_col])
        if m is not None:
            out["auc_mean"] = m
    return out


def _regression(tabs) -> dict[str, Any]:
    t6 = _sheet(tabs, "Table 6")
    if t6 is None:
        return {}
    or_col = next((c for c in t6.columns if "OR" in c.split()), None) or next((c for c in t6.columns if "OR" in c), None)
    rows, group, labs = [], None, []
    for _, r in t6.iterrows():
        feat = str(r.iloc[0]).strip()
        if or_col and "reference" in str(r[or_col]).lower():
            continue  # reference category of a multilevel feature (OR 1.00 by definition)
        ci = _ci(r[or_col]) if or_col else None
        if ci is None:
            group = feat
            continue
        p_txt = str(r.get("P-Value", "")).strip()
        p = 0.0009 if p_txt.startswith("<") else _first_number(p_txt)
        beta = _first_number(r.get("Standardized Beta"))
        row = {"feature": feat, "group": group, "increment": str(r.get("Increment", "")).strip(), "or": ci["est"], "lo": ci["lo"], "hi": ci["hi"],
               "p": p, "p_text": p_txt, "beta": beta}
        rows.append(row)
        if group and group.lower().startswith("laboratory") and ":" not in feat:
            labs.append(feat)
    sig = [r for r in rows if r["p"] is not None and r["p"] < 0.05]
    # the abstract's "strongest independent risk factors": clinical terms only. A "not measured" / "missing" indicator says whether a
    # value was recorded (care process, data capture), not a patient characteristic, so it is not a headline risk factor (run C4 led
    # with "diastolic blood pressure: not measured").
    risk = sorted([r for r in sig if r["or"] > 1 and r["beta"] is not None and not _is_indicator_term(r["feature"])], key=lambda r: -r["beta"])
    protective = [r for r in sig if r["hi"] < 1]
    by = {r["feature"]: r for r in rows}
    per_unit = [r for r in risk if r["increment"] and "vs" not in r["increment"].lower()]
    example = next((r for r in per_unit if "readmission" in r["feature"].lower()), per_unit[0] if per_unit else None)
    # the clinical risk factor with the largest odds ratio when the standardized-beta ranking leaves it out (Kyle, 2026-11-22: run C7's
    # abstract named prior readmissions and anion gap but not discharge against medical advice, OR 1.88)
    largest = max(risk, key=lambda r: r["or"], default=None)
    largest_or = largest if largest is not None and largest not in risk[:2] else None
    return {"n_terms": len(rows), "rows": rows, "by_feature": by, "top_risk": risk[:2], "largest_or": largest_or, "protective": protective, "example": example,
            "protective_names": [r["feature"] for r in protective], "labs": labs,
            "indicator_terms": [r["feature"] for r in rows if r["feature"].endswith((": not measured", ": missing"))]}


def _is_indicator_term(feature: str) -> bool:
    """A Table 6 missing-value indicator ("Albumin: not measured", "SDI Quintile: Missing / Unknown" is a level, not one)."""
    return str(feature).endswith((": not measured", ": missing"))


#: Names that keep their capital inside a sentence (C8: "elixhauser score (van walraven)").
PROPER_NOUNS = frozenset({"Elixhauser", "Walraven", "Charlson", "Hispanic", "Medicare", "Medicaid", "Wald", "Gini", "Shapley"})


def lower_first(s: str | None) -> str | None:
    """A Table 6 / SHAP label as it reads inside a sentence: the ordinary capitalised words of the feature name (before the first
    ": ") are lower-cased, while acronyms and mixed-case words (ALT, BMI, RUCA, ED, HbA1c, pCO2, eGFR, mmHg) and the level after
    ": " stay as written. "Diastolic Blood Pressure (mmHg): not measured" -> "diastolic blood pressure (mmHg): not measured",
    "Race: Black / African American" -> "race: Black / African American", "ALT: not measured" unchanged."""
    if not s:
        return s
    name, sep, rest = s.partition(": ")

    def word(w: str) -> str:
        core = "".join(ch for ch in w if ch.isalpha())
        keep = core == "I" or core in PROPER_NOUNS
        return w.lower() if core[:1].isupper() and (len(core) == 1 or core[1:].islower()) and not keep else w

    return " ".join(word(w) for w in name.split(" ")) + sep + rest


def _stay_type(tabs) -> dict[str, Any]:
    t4 = _sheet(tabs, "Table 4")
    if t4 is None or len(t4) < 2:
        return {}
    out = {}
    for _, r in t4.iterrows():
        key = "first" if "first" in str(r.iloc[0]).lower() else "later"
        out[key] = {"n": _first_number(r.iloc[1]), "share_pct": _first_number(r.iloc[2]), "rate_pct": _first_number(r.iloc[3]), "auc": _first_number(r.iloc[4])}
    if "first" in out and "later" in out:
        out["model"] = str(t4.columns[4]).replace("AUC-ROC", "").strip() or "HistGBM"
    return out


def _completeness(tabs) -> dict[str, Any]:
    s2 = _sheet(tabs, "Table S2")
    if s2 is None:
        return {}
    out = {}
    for _, r in s2.iterrows():
        f = str(r.iloc[0])
        n, pct = _count_pct(r.iloc[1])
        if f.startswith("SDI"):
            out["sdi_missing_pct"] = pct
        elif f.startswith("RUCA"):
            out["ruca_missing_pct"] = pct
    return out


def _attrition(tabs, n: int | None) -> dict[str, Any]:
    s3 = _sheet(tabs, "Table S3")
    if s3 is None or "Remaining Stays (N)" not in s3.columns:
        return {}
    comb = s3[s3.iloc[:, 0].astype(str).str.contains("All_Sites|All Sites|Combined", case=False, na=False)]
    if comb.empty:
        return {}
    stage = comb.get("Stage / Description", pd.Series([""] * len(comb), index=comb.index)).astype(str)
    stays = [(_first_number(v) or 0) for v in comb["Remaining Stays (N)"]]
    if "Remaining Patients (P)" in comb and stays == [(_first_number(v) or 0) for v in comb["Remaining Patients (P)"]]:
        return {}  # patient-level steps only (stays equal patients): the table holds no stay count, so none is printed
    one = [i for i, s in enumerate(stage) if "one stay per patient" in s.lower() or "one stay" in s.lower()]
    qual = stays[one[0] - 1] if one and one[0] > 0 else max(stays)
    if not qual or (n and qual <= n):  # no step before the draw, or nothing was dropped by it: the count is not separately known
        return {}
    return {"qualifying_stays": int(qual), "stays_per_patient": qual / n if n else None}


def manuscript_context(workbook: str | Path, *, run: Mapping[str, Any] | str | Path | None = None,
                       summary: Mapping[str, Any] | str | Path | None = None, shap_table: str | Path | None = None,
                       params: Mapping[str, Any] | str | Path | None = None, facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the template may print, read from one run's files. ``workbook`` is ``<prefix>_Manuscript_Tables.xlsx``; ``run`` and
    ``summary`` are ``<prefix>_run.json`` and ``<prefix>_summary.json`` (paths or dicts; by default the files next to the workbook);
    ``shap_table`` the SHAP top-predictor CSV; ``params`` the study team's own details (:func:`load_params`); ``facts`` any further
    values (they win over what is read). Keys the files do not hold are left out, and the template shows them as missing."""
    from omop_etl.manuscript_report import read_workbook_tables

    wb = Path(workbook)
    prefix = wb.name.replace("_Manuscript_Tables.xlsx", "") if wb.name.endswith("_Manuscript_Tables.xlsx") else None

    def _json(v, suffix):
        # the run's own file next to the workbook, with what was passed on top: a caller that passes part of the record (or
        # none) still gets the rest, such as the qualifying-stay count C8 left out of the text
        own = wb.with_name(f"{prefix}{suffix}") if prefix else None
        if isinstance(v, Mapping):
            base = json.loads(own.read_text(encoding="utf-8")) if own is not None and own.exists() else {}
            return {**base, **dict(v)}
        p = Path(v) if v is not None else own
        return json.loads(p.read_text(encoding="utf-8")) if p is not None and p.exists() else {}

    run_rec, summ = _json(run, "_run.json"), _json(summary, "_summary.json")
    tabs = read_workbook_tables(wb)
    ctx: dict[str, Any] = {"cohort": _cohort(tabs), "models": _models(tabs), "tuning": _tuning(tabs), "loso": _loso(tabs),
                           "lr": _regression(tabs), "stay_type": _stay_type(tabs), "completeness": _completeness(tabs)}
    n_ = ctx["cohort"].get("n")
    q = run_rec.get("qualifying_stays")
    ctx["attrition"] = ({"qualifying_stays": int(q), "stays_per_patient": int(q) / n_ if n_ else None} if q and (not n_ or int(q) >= n_)
                        else _attrition(tabs, n_))
    ctx["random_draw"] = _random_draw(wb, prefix)
    if run_rec.get("random_draw_redrawn"):  # patients whose stay in the reused draw is no longer eligible, given another eligible stay
        ctx["random_draw"]["redrawn"] = int(run_rec["random_draw_redrawn"])
    # how a stay with no BMI recorded during the stay was filled (0.34.1); a run record without "bmi_fill" gets its profile's default
    ctx["bmi_fill"] = run_rec.get("bmi_fill") or ("derive_lookback" if run_rec.get("profile", "ederri") in ("ederri", "both") else "derive")
    if run_rec.get("sensitivity"):  # the seed and all-eligible-stays sensitivity analyses (run_study(sensitivity=True), 0.35.0)
        ctx["sensitivity"] = dict(run_rec["sensitivity"])
    if ctx["cohort"].get("contrast"):
        ctx["table1_contrast"] = " and ".join(ctx["cohort"].pop("contrast"))
    window = run_rec.get("window")
    if not window:
        cap = " ".join(str(c) for c, _ in tabs.values())
        m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{4})\s*[–-]\s*(\d{4}-\d{2}-\d{2})", cap)
        window = [m.group(1) if len(m.group(1)) > 4 else f"{m.group(1)}-01-01", m.group(2)] if m else None
    if window:
        ctx["window"] = {"start": window[0], "end": window[1], "start_text": _date_text(window[0]), "end_text": _date_text(window[1]),
                         "start_month": _date_text(window[0], month_only=True), "end_month": _date_text(window[1], month_only=True)}
    dec = run_rec.get("decisions") or {}
    ctx["decisions"] = dec
    ctx["run"] = {k: run_rec[k] for k in ("n_sites", "bootstraps", "random_state", "preset") if run_rec.get(k) is not None}
    ctx["cohort_call"] = cohort_call(run_rec.get("cohort_params"), window)
    labels = list(run_rec.get("labels") or [])
    if not labels:
        for prefix_ in ("Table 5", "Table S1"):
            t = _sheet(tabs, prefix_)
            if t is not None:
                labels = [str(x).strip() for x in t.iloc[:, 0] if re.match(r"^Site\s+\S+$", str(x).strip())]
                if labels:
                    break
    if labels:
        ctx["site_labels"] = labels
    if not ctx["run"].get("n_sites") and (labels or ctx["loso"].get("n_sites")):
        ctx["run"]["n_sites"] = len(labels) or ctx["loso"]["n_sites"]
    env = run_rec.get("environment") or {}
    if env:
        ctx["environment"] = env
    dates = summ.get("dates") or {}
    if dates.get("sites"):
        ctx["site_dates"] = [{"label": k, "first": _date_text(v.get("admit_first"), month_only=True), "last": _date_text(v.get("admit_last"), month_only=True)}
                             for k, v in dates["sites"].items()]  # file order, so relabelling a run changes only the letters
    if dates.get("discharge_last"):
        ctx.setdefault("window", {})["last_discharge_text"] = _date_text(dates["discharge_last"])
    if summ.get("n_features"):
        ctx["features"] = {"n": int(summ["n_features"])}
        for k in ("n_binary", "n_continuous", "n_columns"):
            if summ.get(k) is not None:
                ctx["features"][k] = int(summ[k])
        if summ.get("empty_columns"):
            ctx["features"]["n_empty"] = len(summ["empty_columns"])
            ctx["features"]["empty"] = list(dict.fromkeys(_empty_words(c) for c in summ["empty_columns"]))  # one name per measure
    miss = wb.with_name(f"{prefix}_Missingness.csv") if prefix else None
    if miss is not None and miss.exists() and ctx["lr"].get("labs"):
        # the laboratories the models used: Table 4's, then any the tables leave out (C8: hemoglobin was a top SHAP predictor
        # but missing from the Section 2.2 list, which was read from Table 4 only)
        from omop_etl.manuscript_tables import feature_label

        mt = pd.read_csv(miss)
        cols = mt.iloc[:, 0].astype(str)
        # a column with no values at any site is not a laboratory the models used (0.33.6 listed the ten empty columns here too)
        empty = set(map(str, summ.get("empty_columns") or []))
        if "pct_missing" in mt:
            empty |= set(cols[pd.to_numeric(mt["pct_missing"], errors="coerce") >= 100])
        model_labs = [feature_label(c) for c in cols if re.fullmatch(r"lab_[a-z0-9_]+", c) and not c.endswith("_last") and c not in empty]
        extra = [x for x in dict.fromkeys(model_labs) if x not in ctx["lr"]["labs"]]
        if extra:
            ctx["lr"]["labs"] = list(ctx["lr"]["labs"]) + extra
    ctx["files"] = {"workbook": wb.name}
    ctx["hyper"] = benchmark_hyperparameters(ctx["run"].get("random_state", 42))
    shap_path = Path(shap_table) if shap_table else (wb.with_name(f"{prefix}_Table_SHAP_Top_Predictors.csv") if prefix else None)
    if shap_path is not None and shap_path.exists():
        st = pd.read_csv(shap_path)
        col = next((c for c in st.columns if c.lower() in ("feature", "predictor", "label")), st.columns[0])
        from omop_etl.manuscript_tables import feature_label

        ctx["shap"] = {"top": [feature_label(x) for x in st[col].head(10)]}  # clinical labels, also for a table saved before 0.33.4
        if summ.get("shap_sample_n"):
            ctx["shap"]["n_sample"] = int(summ["shap_sample_n"])
    ctx["params"] = dict(load_params(params) if isinstance(params, (str, Path)) else (params or {}))
    for k, v in (facts or {}).items():
        if isinstance(v, Mapping) and isinstance(ctx.get(k), dict):
            ctx[k].update(v)
        else:
            ctx[k] = v
    return {k: v for k, v in ctx.items() if v not in ({}, None)}


def cohort_call(params: Mapping[str, Any] | None = None, window: Sequence[str] | None = None) -> str:
    """The ``define_study_cohort(...)`` call of a run, as code text for the Data & Code Availability section: the run's own cohort
    parameters (run record ``cohort_params``), otherwise the package's protocol defaults, with the window and stay rule the study adds."""
    from omop_etl.study_run import PROTOCOL_COHORT_PARAMS

    p = dict(params or PROTOCOL_COHORT_PARAMS)
    if window:
        p.update(min_admit_date=str(window[0]), max_admit_date=str(window[1]))
    p["sample_stay_rule"] = "random"
    if "random_seed" not in p and "random_state" not in p:
        # the draw's seed is define_study_cohort's own default unless the run set one; print it so readers can repeat the draw
        import inspect

        from omop_etl.cohort import define_study_cohort

        default = inspect.signature(define_study_cohort).parameters.get("random_state")
        p["random_seed"] = default.default if default is not None else 42
    lines = [f"    {k}={v!r}," for k, v in p.items()]
    return "define_study_cohort(\n    con,\n" + "\n".join(lines) + "\n)"


_HYPER_KEYS = {"Logistic Regression": ("C",), "Random Forest": ("n_estimators", "max_depth", "min_samples_split"),
               "HistGBM": ("max_iter", "learning_rate", "max_depth", "l2_regularization"),
               "XGBoost": ("n_estimators", "learning_rate", "max_depth", "subsample", "colsample_bytree")}


def benchmark_hyperparameters(random_state: int = 42) -> dict[str, dict[str, Any]]:
    """``{model: {"text": "n_estimators=100, max_depth=12, ...", parameter: value}}`` for the fixed benchmark configuration of each model
    in :func:`omop_etl.study_report.default_model_grid`, read from the estimators themselves so the text cannot drift from the code."""
    try:
        from omop_etl.study_report import default_model_grid

        grid = default_model_grid(random_state, n_jobs=1)
    except Exception:  # scikit-learn missing: the template shows no hyperparameters
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, (_, configs) in grid.items():
        try:
            est = configs[0][1]()
            prm = est.get_params()
        except Exception:
            continue
        keys = [k for k in _HYPER_KEYS.get(name, ()) if prm.get(k) is not None]
        vals = {k: prm[k] for k in keys}
        out[name] = {**vals, "text": ", ".join(f"{k}={v}" for k, v in vals.items())}
    return out


def _column_words(col: str) -> str:
    """A feature-table column name as words: ``heart_rate_mean`` -> "heart rate"."""
    w = re.sub(r"_(mean|last|first|min|max|value|index|idx)$", "", str(col).lower())
    return w.replace("_", " ").strip()


def _empty_words(col: str) -> str:
    """An empty feature column as the text names it: its clinical label in sentence case ("lab_potassium" -> "potassium",
    "vital_heart_rate" -> "heart rate"; C8 printed "lab potassium" and "vital heart rate")."""
    from omop_etl.manuscript_tables import feature_label

    base = re.sub(r"_(mean|last|first|min|max|value|index|idx)$", "", str(col))
    lab = feature_label(base)
    return lab if lab == base else lower_first(lab)


def _date_text(d: str, month_only: bool = False) -> str:
    try:
        t = pd.Timestamp(d)
    except (ValueError, TypeError):
        return str(d)
    return f"{t.strftime('%B')} {t.year}" if month_only else f"{t.strftime('%B')} {t.day}, {t.year}"


# ------------------------------------------------------------------ rendering
def _environment(missing: list[str]):
    import jinja2

    class Missing(jinja2.Undefined):
        """Renders a visible marker and records the name, instead of an empty string or an exception."""

        def _name(self) -> str:
            return str(self._undefined_name or "value")

        def __str__(self) -> str:
            missing.append(self._name())
            return MISSING_MARK.format(self._name())

        def __format__(self, spec: str) -> str:
            return str(self)

        def __getattr__(self, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            return Missing(name=f"{self._name()}.{name}")

        __getitem__ = lambda self, key: Missing(name=f"{self._name()}.{key}")  # noqa: E731

        def __call__(self, *args, **kwargs):  # models.list.items() on a run without Table 2: nothing to list
            return Missing(name=self._name())

    env = jinja2.Environment(undefined=Missing, autoescape=False, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)

    def _ok(v) -> bool:
        return not isinstance(v, jinja2.Undefined) and v is not None

    def fmt(v, digits=3):
        return f"{float(v):.{digits}f}" if _ok(v) else str(v if isinstance(v, jinja2.Undefined) else Missing(name="number"))

    def n(v):
        return f"{int(round(float(v))):,}" if _ok(v) else str(v)

    def fmt_range(r, digits=3, sep="–"):
        """``(lo, hi)`` as "lo–hi" (or "lo to hi"), or one value when both print the same (C8: "AUC-ROC 0.777–0.777")."""
        if not _ok(r) or len(r) != 2:
            return str(r)
        lo, hi = fmt(r[0], digits), fmt(r[1], digits)
        return lo if lo == hi else f"{lo}{sep}{hi}"

    def pct(v, digits=2):
        return f"{float(v):.{digits}f}%" if _ok(v) else str(v)

    def or_ci(r):
        return f"OR {r['or']:.2f}, 95% CI {r['lo']:.2f}–{r['hi']:.2f}" if _ok(r) else str(r)

    def join(items, word="and"):
        items = [str(i) for i in (items or [])]
        if len(items) <= 2:
            return f" {word} ".join(items)
        return f"{', '.join(items[:-1])}, {word} {items[-1]}"  # serial comma, as in the delivered text

    words_ = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}

    def words(v):  # any count: "five" up to ten, digits above (house style for numbers in running text)
        if not _ok(v):
            return str(v)
        return words_.get(int(v), f"{int(v):,}")

    def label_span(labels):
        """"Site A through Site H" when the labels run in sequence, otherwise every label listed; any number of sites."""
        if not _ok(labels) or not labels:
            return str(labels if isinstance(labels, jinja2.Undefined) else Missing(name="site_labels"))
        labels = list(labels)
        if len(labels) == 1:
            return labels[0]
        head = [re.match(r"(.*?)([A-Z]+)$", x) for x in labels]
        if all(head) and len({m.group(1) for m in head}) == 1:
            def idx(code):
                v = 0
                for ch in code:
                    v = v * 26 + ord(ch) - 64
                return v
            nums = sorted(idx(m.group(2)) for m in head)
            if nums == list(range(nums[0], nums[0] + len(nums))):
                order = sorted(labels, key=lambda x: idx(re.match(r".*?([A-Z]+)$", x).group(1)))
                return f"{order[0]} through {order[-1]}"
        return join(sorted(labels))

    env.filters.update(fmt=fmt, fmt_range=fmt_range, n=n, pct=pct, or_ci=or_ci, join_and=join, lower_first=lower_first,
                       words=words, label_span=label_span)
    return env


def render_manuscript_body(context: Mapping[str, Any], *, tables: Mapping[str, str] | None = None, figures: Mapping[str, str] | None = None,
                           version: str | None = None, template: str | Path | None = None) -> dict[str, Any]:
    """Fills the template with ``context`` (:func:`manuscript_context`). ``tables`` maps a layout key (``table1``, ``performance``,
    ``tuning``, ``loso``, ``lr``, ``stay_type``, ``completeness``, ...) to its Markdown table; ``figures`` maps a figure key
    (``flowchart``, ``shap``) to its Markdown image line. Table and figure numbers come from ``layout.json``: an item that is absent
    is skipped and the later ones move up, and every reference follows.

    Returns ``{"markdown": str, "missing": [names], "template": "<version>", "numbers": {key: "Table 4", ...}}``."""
    folder = Path(template) if template is not None else template_dir(version)
    layout = json.loads((folder / "layout.json").read_text(encoding="utf-8"))
    tables, figures = dict(tables or {}), dict(figures or {})  # values: markdown, or (caption without its number, markdown / image file)
    numbers: dict[str, str] = {}
    i = 0
    for key in layout.get("tables", []):
        if key in tables:
            i += 1
            numbers[key] = f"Table {i}"
    i = 0
    for key in layout.get("supplementary", []):
        if key in tables:
            i += 1
            numbers[key] = f"Table S{i}"
    i = 0
    for key in layout.get("figures", []):
        if key in figures:
            i += 1
            numbers[key] = f"Figure {i}"
    missing: list[str] = []
    env = _environment(missing)
    tpl = env.from_string((folder / "manuscript.md.j2").read_text(encoding="utf-8"))
    ref = {k: numbers.get(k) for k in set(layout.get("tables", [])) | set(layout.get("supplementary", [])) | set(layout.get("figures", []))}
    ref = {k: v for k, v in ref.items() if v}
    table_md = {}
    for k, v in tables.items():
        if k not in numbers:  # layout.json decides what is printed: a table it does not list is left out, not printed unnumbered
            continue
        cap, body = v if isinstance(v, tuple) else ("", v)
        table_md[k] = f"**{numbers.get(k, 'Table')}: {cap}**\n\n{body}" if cap else body
        if k in layout.get("keep_together", []):  # the PDF keeps the caption and the table on one page; Word ignores the raw Typst
            table_md[k] = f"```{{=typst}}\n#block(breakable: false)[\n```\n\n{table_md[k]}\n\n```{{=typst}}\n]\n```"
    figure_md = {}
    for k, v in figures.items():
        cap, file, width = (tuple(v) + ("100%",))[:3] if isinstance(v, tuple) else ("", v, "100%")
        cap = _caption_with_context(cap, context)
        text = f"{numbers.get(k, 'Figure')}: {cap}" if cap else numbers.get(k, "Figure")
        alt = text.replace('"', "")
        # Quarto numbers a figure with an id itself ("Figure 1:"), in document order, which is the layout's order
        figure_md[k] = f'![{cap}]({file}){{#fig-{k} fig-alt="{alt}" width="{width}"}}'
    md = tpl.render(**context, ref=ref, table_md=table_md, figure_md=figure_md)
    # Pandoc needs a blank line before a heading and around a figure, or it reads them as paragraph text
    md = re.sub(r"(?m)^(?!\n)(.+)\n(#{1,6} )", r"\1\n\n\2", md)
    md = re.sub(r"(?m)^(!\[.*)$", r"\n\1\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return {"markdown": md, "missing": sorted(set(missing)), "template": folder.name.lstrip("v"), "numbers": numbers}


#: workbook sheet (by its number) -> layout key of the template
SHEET_KEYS = {"Table 1": "table1", "Table 2": "performance", "Table 3": "tuning", "Table 4": "stay_type", "Table 5": "loso", "Table 6": "lr",
              "Table S1": "los", "Table S2": "completeness", "Table S3": "attrition", "Table S4": "grid"}
_FIGURE_KEYS = (("flowchart", ("Flowchart", "CONSORT")), ("shap", ("SHAP_XGBoost", "SHAP_4Panel", "SHAP")))
_FIGURE_CAPTIONS = {"flowchart": "Cohort selection flowchart. P = unique patients; each step is a patient-level count from the omop-duck-db cohort "
                                 "attrition table, summed over the {nsites_word} health systems. The final box is the analytic cohort: one qualifying "
                                 "stay drawn at random for each patient.{draw_note}",
                    "shap": "SHAP Summary Plot for XGBoost with 50/50 Balanced Training (Top 15 Predictors, {shap_n} Sampled Patients)"}


def _random_draw(wb: Path, prefix: str | None) -> dict[str, Any]:
    """The patients the study's reused random draw leaves out (Figure 1's last exclusion arrow), from ``<prefix>_CONSORT_Attrition.csv``
    next to the workbook: ``{"excluded": n}``, or ``{}`` for a run without a pinned draw."""
    f = wb.with_name(f"{prefix}_CONSORT_Attrition.csv") if prefix else None
    if f is None or not f.exists():
        return {}
    from omop_etl.study_run import mark_random_draw

    a = mark_random_draw(pd.read_csv(f))
    k = int(pd.to_numeric(a.loc[a["into_final"], "subjects_dropped"], errors="coerce").fillna(0).sum()) if "subjects_dropped" in a else 0
    return {"excluded": k} if k > 0 else {}


def _caption_with_context(cap: str, context: Mapping[str, Any]) -> str:
    """Fills ``{nsites_word}`` and ``{shap_n}`` in a figure caption from the run; a value the run does not hold is dropped with its word."""
    n = (context.get("run") or {}).get("n_sites")
    words_ = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    cap = cap.replace("{nsites_word}", words_.get(n, str(n)) if n else "").replace("  ", " ")
    k = (context.get("random_draw") or {}).get("excluded")
    cap = cap.replace("{draw_note}", (f" The study scores the stays of one random draw, made once and reused in every analysis; the {k:,} "
                                      + ("patient whose qualifying stays in the window were not in that draw branches" if k == 1 else
                                         "patients whose qualifying stays in the window were not in that draw branch")
                                      + " off the last arrow.") if k else "")
    shap_n = (context.get("shap") or {}).get("n_sample")
    return cap.replace("{shap_n}", f"{shap_n:,}") if shap_n else cap.replace(", {shap_n} Sampled Patients", "")


def metric_cells(t: pd.DataFrame, digits: int | None = None) -> pd.DataFrame:
    """Tables 2 and 3 as printed: every number in a metric column (one that holds a non-whole number) as text with ``digits`` places
    (default :data:`omop_etl.manuscript_tables.METRIC_DECIMALS`), so 0.6 prints as 0.60. The workbook keeps them as numbers, and
    Excel reads a number back without its trailing zero, so the printed table cannot rely on the cell. Count columns are left alone."""
    from omop_etl.manuscript_tables import METRIC_DECIMALS

    d = METRIC_DECIMALS if digits is None else digits
    out = t.copy().astype(object)
    for c in out.columns:
        vals = [v for v in out[c] if isinstance(v, (float, np.floating)) and not pd.isna(v)]
        if any(not float(v).is_integer() for v in vals):
            out[c] = [f"{float(v):.{d}f}" if isinstance(v, (float, np.floating)) and not pd.isna(v) else v for v in out[c]]
    return out


FIGURE_MAX_HEIGHT_IN = 7.2  # with 1-inch margins a letter page has 9 in; the rest is for the caption (C17: a 12-step Figure 1 ran into the page number)
TEXT_WIDTH_IN = 6.5


def figure_width(path: str | Path, *, max_height_in: float = FIGURE_MAX_HEIGHT_IN, text_width_in: float = TEXT_WIDTH_IN) -> str:
    """The printed width of a PNG figure, as a percentage of the text width: 100% unless the figure would then be taller than
    ``max_height_in``, in which case it is narrowed so that its height is ``max_height_in``. Anything that is not a readable PNG
    keeps 100%."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return "100%"
        w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except OSError:
        return "100%"
    if w <= 0 or h <= 0 or text_width_in * h / w <= max_height_in:
        return "100%"
    return f"{int(100 * max_height_in * w / (text_width_in * h))}%"


def full_manuscript_markdown(tabs: Mapping[str, tuple[str, pd.DataFrame]], images: Sequence[Path], *, workbook: str | Path,
                             run: Mapping[str, Any] | None = None, summary: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None,
                             software_paragraph: str | None = None, version: str | None = None) -> dict[str, Any]:
    """The full manuscript body (everything after the YAML header) from the workbook tables ``tabs`` (already checked and with small
    cells suppressed), the figure files ``images``, and the run's records. Returns :func:`render_manuscript_body`'s result."""
    from omop_etl.manuscript_document import _esc, _table_md

    folder = template_dir(version)
    layout = json.loads((folder / "layout.json").read_text(encoding="utf-8"))
    tables: dict[str, tuple[str, str]] = {}
    for name, (title, t) in tabs.items():
        m = re.match(r"(Table S?\d+)", name)
        key = SHEET_KEYS.get(m.group(1)) if m else None
        if key and isinstance(t, pd.DataFrame) and len(t):
            cap = re.sub(r"^Table S?\d+:\s*", "", str(title))
            if key == "grid":  # the workbook keeps the unrounded auc_raw / f1_raw tuning selects on; the printed table does not
                t = t.drop(columns=[c for c in t.columns if str(c).endswith("_raw")])
            if key in ("performance", "loso", "grid"):  # Table S4 too: Rubin's 2 decimals (C10 review)
                t = metric_cells(t)
            if key in layout.get("transpose", []):
                t = transpose_for_print(t)
            tables[key] = (_esc(cap), _table_md(t))
    figures: dict[str, tuple[str, str]] = {}
    for key, marks in _FIGURE_KEYS:
        for mark in marks:
            hit = next((p for p in images if mark.lower() in Path(p).name.lower()), None)
            if hit is not None:
                figures[key] = (_FIGURE_CAPTIONS[key], Path(hit).name, figure_width(hit))
                break
    ctx = manuscript_context(workbook, run=run or {}, summary=summary or {}, params=params or {})
    if software_paragraph:
        ctx["software_paragraph"] = software_paragraph
    return render_manuscript_body(ctx, tables=tables, figures=figures, template=folder)


def transpose_for_print(t: pd.DataFrame, stub: str = "Statistic") -> pd.DataFrame:
    """A one-row-per-site table turned for printing like a Table 1: the sites (and the combined row) become the columns, in their
    order, and each statistic becomes a row headed by its name. The workbook keeps the delivered orientation."""
    first = t.columns[0]
    out = t.set_index(first).T.reset_index().rename(columns={"index": stub})
    out.columns = [stub, *[str(c) for c in out.columns[1:]]]
    return out


# ------------------------------------------------------------------ comparing the text with a delivered manuscript
_SECTIONS = ("Abstract", "Introduction", "Methods", "Results", "Discussion", "Ethical", "Data & Code", "Author Contributions", "Software",
             "Supplementary", "Appendix A", "Appendix B", "References")


def _plain_from_markdown(md: str) -> str:
    """Markdown to running text laid out as a PDF's extracted text is: no tables, images, YAML or emphasis; a top-level section heading
    (``##``) on its own line; a sub-heading (``###``, ``####``) and its first paragraph run together, and list items run on from the
    paragraph above them (numbered items keep "1.", bullets become "•"), because the PDF text of the delivered manuscript reads that way."""
    out: list[str] = []
    in_yaml = False
    glue = False  # the next non-blank line continues the current paragraph
    for line in md.splitlines():
        if line.strip() == "---":
            in_yaml = not in_yaml
            continue
        img = re.match(r"^\s*!\[([^\]]*)\]", line)
        if img:  # a figure's caption ("Figure 1: ...", the alt text) is part of the PDF's text
            alt = re.search(r'fig-alt="([^"]*)"', line)
            out += ["", (alt.group(1) if alt else img.group(1)).replace("**", ""), ""]
            glue = False
            continue
        if in_yaml or line.lstrip().startswith(("|", ":::")):
            continue
        text = re.sub(r"[~^]([^~^\s]+)[~^]", r"\1", line.replace("**", "").replace("\\", ""))  # Pandoc sub/superscripts
        if not text.strip():
            if not glue:
                out.append("")
            continue
        m = re.match(r"^(#+)\s*(.*)$", text)
        if m:
            if len(m.group(1)) <= 2:
                out += ["", m.group(2), ""]
                glue = False
            else:
                out += ["", m.group(2)]
                glue = True
            continue
        item = re.match(r"^\s*([-*]|\d+\.)\s+(.*)$", text)
        if item:
            while out and not out[-1].strip():
                out.pop()  # the item continues the paragraph above it
            out.append(("• " if item.group(1) in "-*" else item.group(1) + " ") + item.group(2))
            glue = False
            continue
        out.append(text)
        glue = False
    return "\n".join(out)


def _normal(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("−", "-")
    s = s.replace("—", "-").replace("–", "-").replace("ﬁ", "fi").replace("ﬂ", "fl")
    # PDF text extraction drops or adds spaces between words, so spaces are removed; a list number or abstract label is dropped
    s = re.sub(r"^\s*(?:\d+\.\s*|(?:Background|Objective|Methods|Results|Conclusions):\s*)", "", s.strip())
    return re.sub(r"\s+", "", s).lower()


def _sentences_of(text: str) -> list[tuple[str, str]]:
    """``[(section, sentence)]`` for the prose of ``text``: lines joined (a word broken by a hyphen at a line end is rejoined), split at
    sentence ends, and kept when at least 6 words of letters make up at least 60% of the tokens (a printed table row is not prose)."""
    section = "Front matter"
    paras: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush():
        if buf:
            paras.append((section, " ".join(buf)))
            buf.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"\d{1,3}", line):
            flush()
            continue
        head = re.sub(r"^\d+(?:\.\d+)*\s+", "", line)
        hit = next((s for s in _SECTIONS if head.startswith(s) and len(head) < 90 and not head.endswith(".")
                    and (s.startswith("Appendix") or ":" not in head)), None)  # "Methods: We conducted ..." is abstract text
        if hit:
            flush()
            section = hit
            continue
        if buf and buf[-1].endswith("-") and line[:1].islower():
            buf[-1] = buf[-1][:-1] + line
        else:
            buf.append(line)
    flush()
    out = []
    for sec, p in paras:
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(•])", p):
            toks = s.split()
            words = [t for t in toks if re.fullmatch(r"[A-Za-z][A-Za-z'()\-]*[.,;:]?", t)]
            if len(words) >= 6 and len(words) >= 0.6 * len(toks):
                out.append((sec, s.strip()))
    return out


def compare_prose(reference_text: str, rendered_markdown: str, *, reworded_at: float = 0.6) -> dict[str, Any]:
    """Sentence by sentence, how much of a delivered manuscript's prose (``reference_text``, e.g. the delivered PDF's text) the rendered
    template reproduces. Each reference sentence is paired with its closest rendered sentence and counted as ``identical``,
    ``numbers_only`` (same words, some number differs), ``reworded`` (similarity at least ``reworded_at``) or ``absent``; rendered
    sentences that pair with none are ``added``.

    Returns ``{"answers": {...}, "pairs": [...]}``: ``answers`` holds counts only (overall and per section), ``pairs`` the sentences
    themselves, which quote the reference's numbers and stay with the data."""
    import difflib

    ref = [x for x in _sentences_of(reference_text) if x[0] not in ("Front matter", "References")]
    new = _sentences_of(_plain_from_markdown(rendered_markdown))
    new_norm = [_normal(s) for _, s in new]
    masked_new = [re.sub(r"\d+(?:[.,]\d+)*", "#", s) for s in new_norm]
    used: set[int] = set()
    pairs = []
    for sec, s in ref:
        a = _normal(s)
        am = re.sub(r"\d+(?:[.,]\d+)*", "#", a)
        best, score = None, 0.0
        for i, b in enumerate(new_norm):
            if b == a:
                best, score = i, 1.0
                break
            sm = difflib.SequenceMatcher(None, am, masked_new[i], autojunk=False)
            if sm.real_quick_ratio() < max(score, reworded_at * 0.9):
                continue
            r = sm.ratio()
            if r > score:
                best, score = i, r
        if best is not None and new_norm[best] == a:
            kind = "identical"
        elif best is not None and masked_new[best] == am:
            kind = "numbers_only"
        elif best is not None and score >= reworded_at:
            kind = "reworded"
        else:
            kind = "absent"
        if kind != "absent" and best is not None:
            used.add(best)
        pairs.append({"section": sec, "kind": kind, "similarity": round(score, 3), "reference": s,
                      "rendered": new[best][1] if best is not None and kind != "absent" else None})
    kinds = ("identical", "numbers_only", "reworded", "absent")
    by_section: dict[str, dict[str, int]] = {}
    for p in pairs:
        by_section.setdefault(p["section"], {k: 0 for k in kinds})[p["kind"]] += 1
    total = {k: sum(1 for p in pairs if p["kind"] == k) for k in kinds}
    n = len(pairs)
    answers = {"reference_sentences": n, "rendered_sentences": len(new), **total, "added": len(new) - len(used),
               "share_same_words": round((total["identical"] + total["numbers_only"]) / n, 3) if n else None,
               "share_matched": round((n - total["absent"]) / n, 3) if n else None, "by_section": by_section}
    return {"answers": answers, "pairs": pairs, "added_sentences": [s for i, (_, s) in enumerate(new) if i not in used]}
