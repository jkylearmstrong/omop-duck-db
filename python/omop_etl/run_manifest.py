"""Reproducibility manifest for every pipeline run (FR-A5).

Each CLI entry point can write ``run_manifest.json`` recording package / interpreter / DuckDB versions, random seeds,
SHA-256 of the input files and of the feature spec, the git commit, wall time and the cohort size at every attrition step.
:func:`verify_manifest` (``omop-duckdb verify-manifest`` / ``--verify-manifest``) re-hashes the inputs before scoring, so a
changed input file fails fast.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import datetime as _dt
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["RunManifest", "ManifestVerificationError", "verify_manifest", "file_sha256"]

MANIFEST_VERSION = 1


class ManifestVerificationError(RuntimeError):
    """An input recorded in the manifest is missing or changed."""


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg_versions() -> dict[str, str | None]:
    from importlib import metadata

    out: dict[str, str | None] = {}
    for name in ("omop-duck-db", "duckdb", "pandas", "numpy", "pyarrow", "sqlalchemy", "xgboost", "onnxruntime"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    if out["omop-duck-db"] is None:
        try:
            from omop_etl import __version__

            out["omop-duck-db"] = __version__
        except Exception:
            pass
    return out


def _git_commit(cwd: str | Path | None) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        r = subprocess.run([git, "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _r_version() -> str | None:
    r = shutil.which("Rscript")
    if not r:
        return None
    try:
        p = subprocess.run([r, "--version"], capture_output=True, text=True, timeout=20)
        return (p.stderr or p.stdout).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class RunManifest:
    """Collects run provenance; use as a context manager or call :meth:`write`.

    >>> with RunManifest("cohort define", seeds={"random_state": 42}, inputs=["site.duckdb"]) as m:  # doctest: +SKIP
    ...     cohort = define_study_cohort(con)
    ...     m.add_attrition(cohort.attrs["attrition"])
    >>> m.write("run_manifest.json")  # doctest: +SKIP
    """

    def __init__(self, entry_point: str, *, seeds: Mapping[str, Any] | None = None,
                 inputs: Iterable[str | Path] = (), feature_spec: str | Path | None = None,
                 extra: Mapping[str, Any] | None = None, git_dir: str | Path | None = None, include_r: bool = False):
        self.entry_point = entry_point
        self.seeds = dict(seeds or {})
        self.inputs = [Path(p) for p in inputs]
        self.feature_spec = None if feature_spec is None else Path(feature_spec)
        self.extra = dict(extra or {})
        self.attrition: list[dict[str, Any]] = []
        self._git_dir = git_dir
        self._include_r = include_r
        self._t0 = time.perf_counter()
        self._started = _dt.datetime.now(_dt.timezone.utc)
        self.wall_time_seconds: float | None = None

    def add_input(self, path: str | Path) -> None:
        """Records an input file (hashed when the manifest is written; add it once it is in its final state)."""
        self.inputs.append(Path(path))

    def add_step(self, step: str, n: int, **detail: Any) -> None:
        self.attrition.append({"step": str(step), "n": int(n), **detail})

    def add_attrition(self, attrition: Any) -> None:
        """Takes the DataFrame from ``define_study_cohort(...).attrs['attrition']`` (or a list of dicts)."""
        rows = attrition.to_dict("records") if hasattr(attrition, "to_dict") else list(attrition)
        for r in rows:
            step = r.get("step") or r.get("step_name") or r.get("name") or str(len(self.attrition))
            n = r.get("subjects_retained", r.get("n_patients", r.get("n", 0)))
            self.add_step(step, int(n))

    def finish(self) -> "RunManifest":
        self.wall_time_seconds = round(time.perf_counter() - self._t0, 3)
        return self

    def __enter__(self) -> "RunManifest":
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    def to_dict(self) -> dict[str, Any]:
        if self.wall_time_seconds is None:
            self.finish()
        return {
            "manifest_version": MANIFEST_VERSION, "entry_point": self.entry_point,
            "started": self._started.isoformat(timespec="seconds"), "wall_time_seconds": self.wall_time_seconds,
            "python": sys.version.split()[0], "platform": platform.platform(),
            "r_version": _r_version() if self._include_r else None,
            "packages": _pkg_versions(), "seeds": self.seeds,
            "git_commit": _git_commit(self._git_dir),
            "inputs": {str(p): file_sha256(p) for p in self.inputs},
            "feature_spec": None if self.feature_spec is None else {"path": str(self.feature_spec),
                                                                    "sha256": file_sha256(self.feature_spec)},
            "attrition": self.attrition, "extra": self.extra,
        }

    def write(self, path: str | Path = "run_manifest.json") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
        return p


def verify_manifest(path: str | Path, *, base_dir: str | Path | None = None,
                    attrition: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Re-hashes every recorded input (and the feature spec) and optionally compares attrition counts.

    Raises :class:`ManifestVerificationError` listing each changed / missing file or differing count; returns the loaded
    manifest when everything matches. Relative input paths resolve against ``base_dir`` (default: current directory).
    """
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(base_dir) if base_dir else Path.cwd()
    problems: list[str] = []
    entries = dict(m.get("inputs", {}))
    if m.get("feature_spec"):
        entries[m["feature_spec"]["path"]] = m["feature_spec"]["sha256"]
    for name, want in entries.items():
        f = Path(name)
        f = f if f.is_absolute() else base / f
        if not f.is_file():
            problems.append(f"{name}: file not found")
        elif file_sha256(f) != want:
            problems.append(f"{name}: SHA-256 changed since the manifest was written")
    if attrition is not None:
        old = [(a["step"], a["n"]) for a in m.get("attrition", [])]
        new = [(a["step"], int(a["n"])) for a in attrition]
        if old != new:
            problems.append(f"attrition counts differ: manifest {old} vs rerun {new}")
    if problems:
        raise ManifestVerificationError("Manifest verification failed:\n  " + "\n  ".join(problems))
    return m
