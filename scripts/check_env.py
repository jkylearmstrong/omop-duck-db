#!/usr/bin/env python3
"""Compare the active Python/R/Quarto environment (or a Docker image) against config/reference_env.json (0.36.0, M3).

Examples:
    python scripts/check_env.py                                    # report differences in the current environment
    python scripts/check_env.py --policy pinned                    # fail if pinned tools or compatible library ranges fail
    python scripts/check_env.py --policy exact                     # fail if any version differs from reference_env.json
    python scripts/check_env.py --image omopduckdb:ci --policy pinned
    python scripts/check_env.py --image omopduckdb:dgx --skip-r --policy report
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REF = REPO_ROOT / "config" / "reference_env.json"

_PY_MODULES = {
    "duckdb": "duckdb",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "scikit-learn": "sklearn",
    "xgboost": "xgboost",
    "numpy": "numpy",
}

_PROBE_SNIPPET = r"""
import importlib, json, platform, re, shutil, subprocess
mods = {"duckdb": "duckdb", "pandas": "pandas", "pyarrow": "pyarrow",
        "scikit-learn": "sklearn", "xgboost": "xgboost", "numpy": "numpy"}
out = {"python": platform.python_version()}
for name, mod in mods.items():
    try:
        m = importlib.import_module(mod)
        out[name] = getattr(m, "__version__", "unknown")
    except Exception:
        out[name] = None
for cmd, key, pat in ((["R", "--version"], "R", r"R version ([0-9]+\.[0-9]+\.[0-9]+)"),
                      (["quarto", "--version"], "quarto", r"([0-9]+\.[0-9]+\.[0-9]+)")):
    exe = shutil.which(cmd[0])
    if not exe:
        out[key] = None
        continue
    try:
        txt = subprocess.check_output([exe, cmd[1]], stderr=subprocess.STDOUT, text=True, timeout=30)
        m = re.search(pat, txt)
        out[key] = m.group(1) if m else txt.splitlines()[0].strip()
    except Exception:
        out[key] = None
print(json.dumps(out))
"""


def _ver_tuple(v: str | None) -> tuple[int, ...] | None:
    if not v:
        return None
    nums = re.findall(r"\d+", str(v).split("+")[0])
    return tuple(int(x) for x in nums[:3]) if nums else None


def _in_range(version: str | None, spec: str | None) -> bool:
    vt = _ver_tuple(version)
    if vt is None:
        return False
    if not spec:
        return True
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r"^(>=|<=|==|!=|>|<)\s*([0-9.]+)$", part)
        if not m:
            continue
        op, target_s = m.group(1), m.group(2)
        tt = _ver_tuple(target_s)
        if tt is None:
            continue
        # Pad shorter tuple with zeros for fair comparison (e.g. 2.0 -> 2.0.0)
        n = max(len(vt), len(tt))
        a = vt + (0,) * (n - len(vt))
        b = tt + (0,) * (n - len(tt))
        ok = (
            (op == ">=" and a >= b)
            or (op == "<=" and a <= b)
            or (op == ">" and a > b)
            or (op == "<" and a < b)
            or (op == "==" and a == b)
            or (op == "!=" and a != b)
        )
        if not ok:
            return False
    return True


def probe_local() -> dict[str, str | None]:
    out: dict[str, str | None] = {"python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"}
    for name, mod in _PY_MODULES.items():
        try:
            m = importlib.import_module(mod)
            out[name] = str(getattr(m, "__version__", "unknown"))
        except Exception:
            out[name] = None
    for cmd, key, pat in ((["R", "--version"], "R", r"R version ([0-9]+\.[0-9]+\.[0-9]+)"),
                          (["quarto", "--version"], "quarto", r"([0-9]+\.[0-9]+\.[0-9]+)")):
        exe = shutil.which(cmd[0])
        if not exe:
            out[key] = None
            continue
        try:
            txt = subprocess.check_output([exe, cmd[1]], stderr=subprocess.STDOUT, text=True, timeout=30)
            m = re.search(pat, txt)
            out[key] = m.group(1) if m else txt.splitlines()[0].strip()
        except Exception:
            out[key] = None
    return out


def probe_image(image: str, docker_bin: str = "docker") -> dict[str, str | None]:
    out = subprocess.check_output(
        [docker_bin, "run", "--rm", "--entrypoint", "python", image, "-c", _PROBE_SNIPPET],
        text=True,
        timeout=120,
    )
    return json.loads(out.strip().splitlines()[-1])


def compare_environment(
    actual: dict[str, str | None],
    ref_data: dict[str, Any],
    *,
    skip_r: bool = False,
    skip_pinned_python: bool = False,
) -> list[dict[str, Any]]:
    versions: dict[str, str] = dict(ref_data.get("versions", {}))
    pinned = set(ref_data.get("pinned", []))
    if skip_pinned_python:
        pinned.discard("python")
    ranges: dict[str, str] = dict(ref_data.get("compatible_ranges", {}))

    rows: list[dict[str, Any]] = []
    for name, ref_ver in versions.items():
        if skip_r and name == "R":
            continue
        got = actual.get(name)
        clean_got = got.split("+")[0] if isinstance(got, str) else None
        exact = clean_got == ref_ver
        is_pinned = name in pinned
        rng = ranges.get(name)
        compatible = exact if is_pinned else _in_range(clean_got, rng)
        status = "match" if exact else ("missing" if got is None else ("compatible_drift" if compatible else "incompatible"))
        rows.append({
            "component": name,
            "reference": ref_ver,
            "actual": got,
            "pinned": is_pinned,
            "range": rng,
            "exact": exact,
            "compatible": compatible,
            "status": status,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--reference", type=Path, default=DEFAULT_REF, help="Path to reference_env.json")
    p.add_argument("--image", help="Optional Docker image tag/digest to inspect via `docker run`")
    p.add_argument("--policy", choices=("report", "pinned", "compatible", "exact"), default="report",
                   help="Exit policy: 'report' prints diffs and exits 0; 'pinned' enforces exact pinned tools + library ranges; "
                        "'compatible' enforces library ranges + quarto (allows base-image Python on DGX); 'exact' enforces all exact.")
    p.add_argument("--skip-r", action="store_true", help="Skip R check (for Python-only or DGX containers)")
    p.add_argument("--json", action="store_true", help="Emit JSON report")
    args = p.parse_args(argv)

    ref_data = json.loads(args.reference.read_text(encoding="utf-8"))
    actual = probe_image(args.image) if args.image else probe_local()
    rows = compare_environment(
        actual,
        ref_data,
        skip_r=args.skip_r,
        skip_pinned_python=(args.policy == "compatible"),
    )

    diffs = [r for r in rows if not r["exact"]]
    if args.policy == "exact":
        failures = [r for r in rows if not r["exact"]]
    elif args.policy in ("pinned", "compatible"):
        failures = [r for r in rows if not r["compatible"]]
    else:
        failures = []

    if args.json:
        print(json.dumps({"policy": args.policy, "ok": not failures, "differences": len(diffs), "components": rows}, indent=2))
    else:
        print(f"{'Component':<15} {'Reference':<12} {'Actual':<14} {'Status'}")
        print("-" * 60)
        for r in rows:
            act = r["actual"] if r["actual"] is not None else "(missing)"
            print(f"{r['component']:<15} {r['reference']:<12} {act:<14} {r['status']}")
        if diffs:
            summary = ", ".join(f"{r['component']} ({r['actual'] or 'missing'} vs {r['reference']})" for r in diffs)
            print(f"\nDiffering from reference ({len(diffs)}): {summary}")
        else:
            print("\nAll checked components match config/reference_env.json.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
