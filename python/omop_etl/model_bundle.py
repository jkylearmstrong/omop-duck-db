"""Versioned model bundles: ship a trained model next to its feature contract and score a cohort (FR-A1).

Bundle layout (a directory)::

    manifest.json      name, version, created, feature_spec_sha256, model_sha256 {file: sha256}, license, intended_use
    feature_spec.json  ordered features, dtypes, physiologic bounds, imputation values, units
    blend.json         components [{name, kind, file, weight}], calibration, risk_tiers
    <component files>  gbdt_xgb.json (XGBoost native JSON), *.onnx, *.pt.ts (TorchScript), glm.json (logistic)

Security: no ``pickle`` / ``joblib`` / ``skops`` files are ever deserialised: such files in a bundle are refused, and every
file the blend reads must be listed in ``manifest.json`` with a matching SHA-256, else :func:`load_model_bundle` raises
:class:`BundleIntegrityError`. XGBoost JSON, ONNX and TorchScript need the optional ``xgboost`` / ``onnxruntime`` /
``torch`` packages (``pip install omop-duck-db[bundle]``); ``glm.json`` (logistic regression) needs only NumPy.

Scoring: component probabilities are blended as ``sum(w_i * p_i) / sum(w_i)``; the optional calibration (Platt on the
blended logit, or isotonic by linear interpolation) is applied last; tiers are ``[min, next_min)`` on the final probability.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.jsonio import strict_dumps
from omop_etl.feature_contract import FeatureValidationReport, load_feature_spec, validate_feature_frame

__all__ = ["BundleIntegrityError", "ModelBundle", "load_model_bundle", "score_cohort", "write_model_bundle",
           "sha256_file", "DEFAULT_RISK_TIERS"]

BUNDLE_SCHEMA_VERSION = 1
DEFAULT_RISK_TIERS = [{"name": "low", "min": 0.0}, {"name": "moderate", "min": 0.10}, {"name": "high", "min": 0.25}]
_FORBIDDEN_SUFFIXES = (".pkl", ".pickle", ".joblib", ".skops", ".dill", ".npy", ".npz")
_KIND_SUFFIX = {"logistic": (".json",), "xgboost_json": (".json", ".ubj"), "onnx": (".onnx",),
                "torchscript": (".ts", ".torchscript", ".pt"),
                "deep_residual": (".pt", ".ts", ".onnx", ".json")}


class BundleIntegrityError(RuntimeError):
    """A bundle file is missing, unlisted, tampered with, or of a refused (pickle-like) type."""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))


def _logit(p):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return np.log(p / (1 - p))


@dataclass
class _Component:
    name: str
    kind: str
    path: Path
    weight: float
    options: dict[str, Any]
    _model: Any = None

    def predict(self, X: np.ndarray, feature_names: list[str], X_seq: np.ndarray | None = None) -> np.ndarray:
        if self.kind == "logistic":
            if self._model is None:
                spec = json.loads(self.path.read_text(encoding="utf-8"))
                coef = np.asarray(spec["coef"], dtype="float64")
                if coef.shape[0] != len(feature_names):
                    raise BundleIntegrityError(f"{self.path.name}: {coef.shape[0]} coefficients for {len(feature_names)} features.")
                self._model = (float(spec.get("intercept", 0.0)), coef,
                               np.asarray(spec.get("mean", np.zeros_like(coef)), dtype="float64"),
                               np.asarray(spec.get("scale", np.ones_like(coef)), dtype="float64"))
            b, coef, mean, scale = self._model
            return _sigmoid(((X - mean) / scale) @ coef + b)
        if self.kind == "xgboost_json":
            try:
                import xgboost as xgb
            except ImportError as exc:
                raise ImportError("xgboost is required for 'xgboost_json' components: pip install 'omop-duck-db[bundle]'") from exc
            if self._model is None:
                self._model = xgb.Booster()
                self._model.load_model(str(self.path))
            return np.asarray(self._model.predict(xgb.DMatrix(X, feature_names=None)), dtype="float64")
        if self.kind == "onnx":
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise ImportError("onnxruntime is required for 'onnx' components: pip install 'omop-duck-db[bundle]'") from exc
            if self._model is None:
                self._model = ort.InferenceSession(str(self.path), providers=["CPUExecutionProvider"])
            sess = self._model
            outs = sess.run(None, {sess.get_inputs()[0].name: X.astype("float32")})
            names = [o.name for o in sess.get_outputs()]
            out = outs[names.index("probabilities")] if "probabilities" in names else outs[-1]
            if isinstance(out, list):  # ZipMap
                out = np.array([[d[k] for k in sorted(d)] for d in out])
            out = np.asarray(out, dtype="float64")
            return out[:, -1] if out.ndim == 2 and out.shape[1] > 1 else out.reshape(-1)
        if self.kind in ("torchscript", "deep_residual"):
            try:
                import torch
            except ImportError as exc:
                raise ImportError("torch is required for torchscript / deep_residual components.") from exc
            if self._model is None:
                try:
                    self._model = torch.jit.load(str(self.path), map_location="cpu").eval()
                except Exception:
                    self._model = torch.load(str(self.path), map_location="cpu", weights_only=False)
                    if hasattr(self._model, "eval"):
                        self._model.eval()
            inp_data = X_seq if (self.options.get("input") in ("sequence_tensor", "tensor") and X_seq is not None) else X
            with torch.no_grad():
                inp_tensor = torch.as_tensor(inp_data, dtype=torch.float32)
                out = self._model(inp_tensor)
                if hasattr(out, "logits"):
                    out = out.logits
                out = out.detach().cpu().numpy().astype("float64").reshape(-1)
            return _sigmoid(out) if self.options.get("output", "prob") == "logit" else out
        raise BundleIntegrityError(f"unknown component kind {self.kind!r}")


@dataclass
class ModelBundle:
    path: Path
    manifest: dict[str, Any]
    feature_spec: dict[str, Any]
    components: list[_Component]
    calibration: dict[str, Any] | None = None
    risk_tiers: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_RISK_TIERS))

    @property
    def feature_names(self) -> list[str]:
        return [f["name"] for f in self.feature_spec["features"]]

    def predict_proba(self, X: np.ndarray, X_seq: np.ndarray | None = None) -> np.ndarray:
        """Blend + calibrate a matrix already ordered as ``feature_names`` (no NaN)."""
        w = np.array([c.weight for c in self.components], dtype="float64")
        p = sum(wi * c.predict(X, self.feature_names, X_seq=X_seq) for wi, c in zip(w, self.components)) / w.sum()
        cal = self.calibration
        if cal:
            m = cal.get("method")
            if m == "platt":
                p = _sigmoid(float(cal["slope"]) * _logit(p) + float(cal["intercept"]))
            elif m == "isotonic":
                p = np.interp(p, np.asarray(cal["x"], dtype="float64"), np.asarray(cal["y"], dtype="float64"))
            else:
                raise BundleIntegrityError(f"unknown calibration method {m!r}")
        return np.asarray(p, dtype="float64")

    def tier(self, prob: np.ndarray) -> np.ndarray:
        tiers = sorted(self.risk_tiers, key=lambda t: t["min"])
        mins = np.array([t["min"] for t in tiers], dtype="float64")
        idx = np.clip(np.searchsorted(mins, prob, side="right") - 1, 0, len(tiers) - 1)
        return np.array([tiers[i]["name"] for i in idx], dtype=object)


def load_model_bundle(path: str | Path, *, verify: bool = True) -> ModelBundle:
    """Loads and verifies a bundle directory. Hash mismatches, unlisted or pickle-like files raise
    :class:`BundleIntegrityError`. ``verify=False`` is intentionally not exposed for pickles: it only skips hashing."""
    root = Path(path)
    mp = root / "manifest.json"
    if not mp.is_file():
        raise BundleIntegrityError(f"{root}: manifest.json not found.")
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    for key in ("name", "version", "feature_spec_sha256", "model_sha256"):
        if key not in manifest:
            raise BundleIntegrityError(f"manifest.json lacks '{key}'.")
    bad = [p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in _FORBIDDEN_SUFFIXES]
    if bad:
        raise BundleIntegrityError(f"Refusing bundle with pickle-like / opaque binary files: {bad}")
    spec_path = root / "feature_spec.json"
    if verify and not hmac.compare_digest(sha256_file(spec_path), manifest["feature_spec_sha256"]):
        raise BundleIntegrityError("feature_spec.json hash does not match manifest.json (tampered or stale).")
    spec = load_feature_spec(spec_path)
    blend_path = root / "blend.json"
    blend = json.loads(blend_path.read_text(encoding="utf-8")) if blend_path.is_file() else None
    listed: dict[str, str] = dict(manifest["model_sha256"])
    if blend is not None:
        if blend_path.name not in listed:
            raise BundleIntegrityError("blend.json is not listed in manifest model_sha256.")
        if verify and not hmac.compare_digest(sha256_file(blend_path), listed[blend_path.name]):
            raise BundleIntegrityError("blend.json hash does not match manifest.json.")
    comps_cfg = (blend or {}).get("components")
    if not comps_cfg:
        raise BundleIntegrityError("blend.json must list at least one component.")
    comps: list[_Component] = []
    for c in comps_cfg:
        f = str(c["file"])
        if "/" in f or "\\" in f or f.startswith("."):
            raise BundleIntegrityError(f"component file {f!r} must be a plain file name inside the bundle.")
        fp = root / f
        if f not in listed:
            raise BundleIntegrityError(f"component {f!r} is not listed in manifest model_sha256.")
        if not fp.is_file():
            raise BundleIntegrityError(f"component file {f!r} is missing.")
        if verify and not hmac.compare_digest(sha256_file(fp), listed[f]):
            raise BundleIntegrityError(f"{f}: SHA-256 mismatch with manifest.json (file changed after packaging).")
        kind = c["kind"]
        if kind not in _KIND_SUFFIX or not f.lower().endswith(_KIND_SUFFIX[kind]):
            raise BundleIntegrityError(f"component {f!r} is not a valid '{kind}' file.")
        w = float(c.get("weight", 1.0))
        if not np.isfinite(w) or w <= 0:
            raise BundleIntegrityError(f"component {f!r}: weight must be > 0.")
        comps.append(_Component(c.get("name", f), kind, fp, w, {k: v for k, v in c.items() if k not in
                                                                ("name", "kind", "file", "weight")}))
    return ModelBundle(root, manifest, spec, comps, (blend or {}).get("calibration"),
                       (blend or {}).get("risk_tiers") or list(DEFAULT_RISK_TIERS))


def score_cohort(
    bundle: ModelBundle | str | Path,
    features: pd.DataFrame | str | Path,
    *,
    key_columns: Sequence[str] | None = None,
    on_violation: str = "raise",
    return_validation: bool = False,
    X_seq: np.ndarray | None = None,
    check_concepts: Any = None,
) -> pd.DataFrame | tuple[pd.DataFrame, FeatureValidationReport]:
    """Scores a feature table (DataFrame, ``.parquet`` or ``.csv``) with a bundle.

    Returns the key columns (default: non-feature columns ending in ``_id``), ``prob`` and ``risk_tier``. The frame is
    validated against the bundle's feature contract first (``on_violation`` raise|clip|nullify, see
    :func:`omop_etl.feature_contract.validate_feature_frame`); remaining NaN are filled with each feature's ``impute``
    value (error if a NaN has none).

    ``check_concepts`` (a DuckDB connection, a registry mapping or a registry JSON file) first checks the custom concept ids recorded in the
    bundle's feature spec against this site's (:func:`omop_etl.bundle_concepts.validate_bundle_concepts`) and raises on a mismatch.
    """
    b = bundle if isinstance(bundle, ModelBundle) else load_model_bundle(bundle)
    if check_concepts is not None:
        from omop_etl.bundle_concepts import validate_bundle_concepts
        validate_bundle_concepts(b, check_concepts)
    if isinstance(features, (str, Path)):
        fp = Path(features)
        df = pd.read_parquet(fp) if fp.suffix.lower() == ".parquet" else pd.read_csv(fp)
    else:
        df = features
    rep = validate_feature_frame(df, b.feature_spec, on_violation)
    X = rep.frame.copy()
    for f in b.feature_spec["features"]:
        n = f["name"]
        if X[n].isna().any():
            if f.get("impute") is None:
                raise ValueError(f"feature {n!r} has missing values and no 'impute' value in the feature spec.")
            X[n] = X[n].fillna(f["impute"])
    names = b.feature_names
    prob = b.predict_proba(X[names].to_numpy(dtype="float64"), X_seq=X_seq)
    keys = list(key_columns) if key_columns is not None else [c for c in df.columns if c.endswith("_id") and c not in names]
    out = df[keys].reset_index(drop=True).copy()
    out["prob"] = prob
    out["risk_tier"] = b.tier(prob)
    return (out, rep) if return_validation else out


def write_model_bundle(
    out_dir: str | Path,
    *,
    name: str,
    version: str,
    feature_spec: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any] | None = None,
    risk_tiers: Sequence[Mapping[str, Any]] | None = None,
    license: str = "UNLICENSED",
    intended_use: str = "",
) -> Path:
    """Packages a bundle and writes ``manifest.json`` with the hashes.

    Each component is ``{"name", "kind", "weight", "file": <existing path to copy> | "content": <dict for logistic>,
    "filename": <name inside the bundle>}``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = load_feature_spec(dict(feature_spec))
    (out / "feature_spec.json").write_text(strict_dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
    blend_comps = []
    for c in components:
        fname = c["filename"]
        if "content" in c:
            (out / fname).write_text(strict_dumps(c["content"], indent=2, sort_keys=True), encoding="utf-8")
        else:
            shutil.copyfile(c["file"], out / fname)
        blend_comps.append({"name": c.get("name", fname), "kind": c["kind"], "file": fname,
                            "weight": c.get("weight", 1.0), **({"output": c["output"]} if "output" in c else {})})
    blend = {"components": blend_comps, "calibration": dict(calibration) if calibration else None,
             "risk_tiers": [dict(t) for t in (risk_tiers or DEFAULT_RISK_TIERS)]}
    (out / "blend.json").write_text(strict_dumps(blend, indent=2, sort_keys=True), encoding="utf-8")
    hashes = {c["file"]: sha256_file(out / c["file"]) for c in blend_comps}
    hashes["blend.json"] = sha256_file(out / "blend.json")
    manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION, "name": name, "version": version,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "feature_spec_sha256": sha256_file(out / "feature_spec.json"), "model_sha256": hashes,
        "license": license, "intended_use": intended_use,
    }
    (out / "manifest.json").write_text(strict_dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out
