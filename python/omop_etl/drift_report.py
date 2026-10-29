"""Executive HTML report for the monthly monitoring log (PSI trend, Brier trajectory, calibration curve).

``write_drift_report(log, out_path)`` takes the CSV/JSONL written by :func:`omop_etl.monitoring.update_monitoring_log`
(or its DataFrame) and, optionally, row-level scores with outcomes for the predicted-vs-observed curve. The page is
one self-contained file (inline SVG, no scripts, no external requests) and holds aggregates only.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from omop_etl.monitoring import PSI_SHIFTED, PSI_WATCH

__all__ = ["read_monitoring_log", "calibration_curve", "write_drift_report"]

_COLOURS = {"ok": "#2e7d32", "watch": "#f9a825", "drift": "#c62828", "miscalibrated": "#6a1b9a", "insufficient_outcomes": "#757575"}


def read_monitoring_log(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".jsonl", ".json", ".ndjson"):
        return pd.read_json(p, lines=True)
    return pd.read_csv(p, dtype={"model_version": str}, keep_default_na=False, na_values=[""])


def calibration_curve(y: Any, p: Any, bins: int = 10) -> pd.DataFrame:
    """Equal-count bins: mean predicted vs observed rate (``predicted``, ``observed``, ``n``)."""
    y = np.asarray(y, dtype="float64")
    p = np.asarray(p, dtype="float64")
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if len(p) == 0:
        return pd.DataFrame(columns=["predicted", "observed", "n"])
    order = np.argsort(p)
    rows = [{"predicted": float(p[i].mean()), "observed": float(y[i].mean()), "n": int(len(i))}
            for i in np.array_split(order, min(bins, len(p))) if len(i)]
    return pd.DataFrame(rows)


def _line_chart(title: str, x_labels: list[str], series: dict[str, list[float | None]], *, hlines: dict[str, float] | None = None,
                width: int = 640, height: int = 240) -> str:
    L, R, T, B = 50, 16, 14, 44
    vals = [v for s in series.values() for v in s if v is not None and np.isfinite(v)] + list((hlines or {}).values())
    if not vals or not x_labels:
        return f"<h3>{html.escape(title)}</h3><p class='muted'>No data yet.</p>"
    lo, hi = min(0.0, min(vals)), max(vals) * 1.1 or 1.0
    n = len(x_labels)
    xs = [L + (width - L - R) * (i / max(n - 1, 1)) for i in range(n)]
    y = lambda v: T + (height - T - B) * (1 - (v - lo) / (hi - lo or 1))  # noqa: E731
    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)}'>"]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        parts.append(f"<line x1='{L}' x2='{width - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='grid'/>"
                     f"<text x='{L - 6}' y='{y(v) + 4:.1f}' class='tick' text-anchor='end'>{v:.3g}</text>")
    for name, v in (hlines or {}).items():
        parts.append(f"<line x1='{L}' x2='{width - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='ref'/>"
                     f"<text x='{width - R}' y='{y(v) - 3:.1f}' class='tick' text-anchor='end'>{html.escape(name)}</text>")
    step = max(1, n // 8)
    for i, lab in enumerate(x_labels):
        if i % step == 0:
            parts.append(f"<text x='{xs[i]:.1f}' y='{height - 22}' class='tick' text-anchor='middle'>{html.escape(lab)}</text>")
    for si, (name, s) in enumerate(series.items()):
        pts = [(xs[i], y(v)) for i, v in enumerate(s) if v is not None and np.isfinite(v)]
        if pts:
            parts.append(f"<polyline fill='none' class='s{si}' points='{' '.join(f'{a:.1f},{b:.1f}' for a, b in pts)}'/>")
            parts += [f"<circle cx='{a:.1f}' cy='{b:.1f}' r='3' class='d{si}'/>" for a, b in pts]
    parts.append("</svg>")
    return f"<h3>{html.escape(title)}</h3>" + "".join(parts)


def _calibration_svg(curve: pd.DataFrame, size: int = 280) -> str:
    if curve.empty:
        return "<p class='muted'>No outcome data supplied (pass --scores with an outcome column).</p>"
    L, B, T = 44, 36, 12
    hi = float(max(curve["predicted"].max(), curve["observed"].max(), 0.05)) * 1.1
    px = lambda v: L + (size - L - 10) * v / hi  # noqa: E731
    py = lambda v: T + (size - T - B) * (1 - v / hi)  # noqa: E731
    pts = " ".join(f"{px(a):.1f},{py(b):.1f}" for a, b in zip(curve["predicted"], curve["observed"]))
    dots = "".join(f"<circle cx='{px(a):.1f}' cy='{py(b):.1f}' r='4' class='d0'><title>n={n}</title></circle>"
                   for a, b, n in zip(curve["predicted"], curve["observed"], curve["n"]))
    return (f"<svg viewBox='0 0 {size} {size}' role='img' aria-label='Calibration curve'>"
            f"<line x1='{px(0):.1f}' y1='{py(0):.1f}' x2='{px(hi):.1f}' y2='{py(hi):.1f}' class='ref'/>"
            f"<line x1='{L}' x2='{L}' y1='{T}' y2='{size - B}' class='grid'/><line x1='{L}' x2='{size - 10}' y1='{size - B}' y2='{size - B}' class='grid'/>"
            f"<polyline fill='none' class='s0' points='{pts}'/>{dots}"
            f"<text x='{(L + size) / 2:.0f}' y='{size - 8}' class='tick' text-anchor='middle'>Predicted</text>"
            f"<text x='12' y='{size / 2:.0f}' class='tick' transform='rotate(-90 12 {size / 2:.0f})' text-anchor='middle'>Observed</text></svg>")


def write_drift_report(log: pd.DataFrame | str | Path, out_path: str | Path, *, scores: pd.DataFrame | None = None,
                       prob_col: str = "prob", outcome_col: str = "outcome_flag", model_version: str | None = None,
                       months: int = 12, title: str = "Model monitoring report") -> Path:
    """Write the HTML report for the last ``months`` months of ``log`` (one model version) and return its path."""
    df = read_monitoring_log(log) if isinstance(log, (str, Path)) else log.copy()
    if "month" not in df.columns:
        raise KeyError("monitoring log needs a 'month' column.")
    if "model_version" in df.columns:
        df["model_version"] = df["model_version"].fillna("").astype(str)
        mv = model_version if model_version is not None else df.sort_values("month")["model_version"].iloc[-1]
        df = df[df["model_version"] == str(mv)]
    else:
        mv = model_version or ""
    df = df.sort_values("month").tail(int(months)).reset_index(drop=True)
    labels = df["month"].astype(str).tolist()
    num = lambda c: [None if pd.isna(v) else float(v) for v in df[c]] if c in df.columns else [None] * len(df)  # noqa: E731
    charts = [
        _line_chart("Population stability index (PSI)", labels, {"PSI": num("psi")}, hlines={"watch": PSI_WATCH, "drift": PSI_SHIFTED}),
        _line_chart("Brier score", labels, {"Brier": num("brier")}),
        _line_chart("Observed / expected ratio", labels, {"O/E": num("oe_ratio")}, hlines={"1.0": 1.0}),
    ]
    curve = pd.DataFrame()
    if scores is not None and prob_col in scores.columns and outcome_col in scores.columns:
        curve = calibration_curve(scores[outcome_col], scores[prob_col])
    status = df["status"].astype(str).tolist() if "status" in df.columns else []
    latest = status[-1] if status else "n/a"
    flagged = [m for m, s in zip(labels, status) if s in ("drift", "miscalibrated")]
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape('' if pd.isna(r[c]) else (f'{r[c]:.4g}' if isinstance(r[c], float) else str(r[c])))}</td>"
                         for c in ("month", "n", "mean_prob", "psi", "brier", "oe_ratio", "slope", "status") if c in df.columns)
        + "</tr>" for _, r in df.iterrows())
    heads = "".join(f"<th>{c}</th>" for c in ("month", "n", "mean_prob", "psi", "brier", "oe_ratio", "slope", "status") if c in df.columns)
    chip = f"<span class='chip' style='background:{_COLOURS.get(latest, '#757575')}'>{html.escape(latest)}</span>"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--bg:#fff;--fg:#1b1f23;--mut:#667;--grid:#dde1e6;--c0:#1565c0;--c1:#ef6c00}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14171a;--fg:#e6e8ea;--mut:#9aa3ab;--grid:#2d343b;--c0:#64b5f6;--c1:#ffb74d}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:900px;margin:auto}} h1{{margin:0 0 4px}} h3{{margin:24px 0 4px}} .muted{{color:var(--mut)}}
svg{{width:100%;height:auto}} .grid{{stroke:var(--grid)}} .ref{{stroke:var(--mut);stroke-dasharray:4 4}}
.tick{{fill:var(--mut);font-size:10px}} .s0{{stroke:var(--c0);stroke-width:2}} .d0{{fill:var(--c0)}}
.s1{{stroke:var(--c1);stroke-width:2}} .d1{{fill:var(--c1)}}
.chip{{color:#fff;padding:2px 10px;border-radius:12px;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid var(--grid);padding:4px 8px;text-align:right}}
th:first-child,td:first-child{{text-align:left}} .tw{{overflow-x:auto}}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">Model version: {html.escape(str(mv) or 'n/a')} &middot; {len(df)} month(s) &middot; latest status {chip}
&middot; flagged months: {html.escape(', '.join(flagged) if flagged else 'none')}</p>
{''.join(charts)}
<h3>Predicted vs observed (calibration)</h3>{_calibration_svg(curve)}
<h3>Monthly detail</h3><div class="tw"><table><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table></div>
<p class="muted">Aggregates only. PSI &ge; {PSI_WATCH} = watch, &ge; {PSI_SHIFTED} = drift.</p>
</main></body></html>"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
