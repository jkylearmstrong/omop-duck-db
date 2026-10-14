"""Production scoring bundle with a train/serve skew guard.

:class:`OMOPScoringPipeline` ties a fitted imputer, scaler and model to the ORDERED feature schema they were trained on,
and refuses to score a frame whose columns do not match that schema. It is deliberately model-agnostic: any object with
``fit``/``transform`` (imputer, scaler) and ``predict_proba`` or ``predict`` (model) works, e.g. scikit-learn estimators.

Pair it with the leak-free feature extractors (``extract_elixhauser_comorbidities(..., exclude_index_diagnoses=True)``,
``extract_prior_utilization``) so the features available at training time are the ones available at the bedside.

Security: bundles are pickles. Only load files you created or trust.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["OMOPScoringPipeline", "FeatureSchemaError"]

_BUNDLE_VERSION = 1


class FeatureSchemaError(ValueError):
    """The frame to score does not match the feature schema the pipeline was fitted on."""


class OMOPScoringPipeline:
    """Fitted imputer + scaler + model bundled with the ordered feature schema.

    Build one with :meth:`fit` (fits every stage on a training frame and records the schema) or directly from already
    fitted stages. :meth:`validate` is called by every scoring method.

    Args:
        model: Fitted estimator with ``predict_proba`` (preferred) or ``predict``.
        feature_names: Ordered feature columns the stages were fitted on.
        imputer, scaler: Optional fitted transformers (``transform`` is applied imputer first, then scaler).
        metadata: Free-form dict stored with the bundle (cohort definition, git sha, training dates, ...).
        strict: If True (default) columns not in the schema raise instead of being silently dropped.
        allow_missing_values: If False (default) NaN in the input raises unless an imputer is present.
    """

    def __init__(self, model: Any, feature_names: Sequence[str], imputer: Any = None, scaler: Any = None,
                 metadata: Mapping[str, Any] | None = None, strict: bool = True,
                 allow_missing_values: bool = False, ranges: Mapping[str, tuple[float, float]] | None = None):
        names = [str(n) for n in feature_names]
        if not names:
            raise ValueError("feature_names must not be empty.")
        if len(set(names)) != len(names):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"feature_names contains duplicates: {dup}")
        if not (hasattr(model, "predict_proba") or hasattr(model, "predict")):
            raise TypeError("model needs predict_proba() or predict().")
        self.model, self.imputer, self.scaler = model, imputer, scaler
        self.feature_names = names
        self.metadata = dict(metadata or {})
        self.strict = bool(strict)
        self.allow_missing_values = bool(allow_missing_values)
        self.ranges = {k: (float(v[0]), float(v[1])) for k, v in (ranges or {}).items()}

    # ------------------------------------------------------------------ fitting
    @classmethod
    def fit(cls, X: pd.DataFrame, y: Any, model: Any, feature_names: Sequence[str] | None = None,
            imputer: Any = None, scaler: Any = None, metadata: Mapping[str, Any] | None = None,
            strict: bool = True) -> "OMOPScoringPipeline":
        """Fit ``imputer`` -> ``scaler`` -> ``model`` on ``X[feature_names]`` and return the bundle."""
        if not isinstance(X, pd.DataFrame):
            raise TypeError("X must be a pandas DataFrame so the column schema can be recorded.")
        names = list(feature_names) if feature_names is not None else [str(c) for c in X.columns]
        missing = [c for c in names if c not in X.columns]
        if missing:
            raise FeatureSchemaError(f"training frame is missing feature column(s) {missing}")
        data = X[names].apply(pd.to_numeric, errors="raise").to_numpy(dtype=float)
        ranges = {n: (float(np.nanmin(data[:, i])), float(np.nanmax(data[:, i])))
                  for i, n in enumerate(names) if not np.all(np.isnan(data[:, i]))}
        if imputer is not None:
            data = imputer.fit_transform(data)
        if scaler is not None:
            data = scaler.fit_transform(data)
        model.fit(data, y)
        return cls(model, names, imputer=imputer, scaler=scaler, metadata=metadata, strict=strict,
                   allow_missing_values=imputer is not None, ranges=ranges)

    # ------------------------------------------------------------------ validation
    def validate(self, X: pd.DataFrame) -> pd.DataFrame:
        """Return ``X`` restricted to and ordered by the schema, or raise :class:`FeatureSchemaError`."""
        if not isinstance(X, pd.DataFrame):
            raise FeatureSchemaError("scoring input must be a pandas DataFrame with named columns.")
        cols = [str(c) for c in X.columns]
        if len(set(cols)) != len(cols):
            raise FeatureSchemaError("scoring input has duplicate column names.")
        missing = [c for c in self.feature_names if c not in cols]
        extra = [c for c in cols if c not in self.feature_names]
        if missing or (extra and self.strict):
            parts = []
            if missing:
                parts.append(f"missing column(s) {missing}")
            if extra and self.strict:
                parts.append(f"unexpected column(s) {extra}")
            raise FeatureSchemaError("feature schema mismatch: " + "; ".join(parts))
        out = X.copy()
        out.columns = cols
        out = out[self.feature_names]
        try:
            out = out.apply(pd.to_numeric, errors="raise")
        except (ValueError, TypeError) as e:
            raise FeatureSchemaError(f"non-numeric feature values: {e}") from e
        if not self.allow_missing_values and out.isna().any().any():
            bad = out.columns[out.isna().any()].tolist()
            raise FeatureSchemaError(f"missing values in {bad} and the pipeline has no imputer.")
        if np.isinf(out.to_numpy(dtype=float)).any():
            raise FeatureSchemaError("infinite values in scoring input.")
        return out

    def out_of_range(self, X: pd.DataFrame) -> dict[str, int]:
        """Rows per feature outside the min/max seen at fit time (a drift check; empty when not recorded)."""
        data = self.validate(X)
        return {n: int(((data[n] < lo) | (data[n] > hi)).sum()) for n, (lo, hi) in self.ranges.items()
                if n in data and ((data[n] < lo) | (data[n] > hi)).any()}

    # ------------------------------------------------------------------ scoring
    def _transform(self, X: pd.DataFrame) -> np.ndarray:
        data = self.validate(X).to_numpy(dtype=float)
        if self.imputer is not None:
            data = self.imputer.transform(data)
        if self.scaler is not None:
            data = self.scaler.transform(data)
        return data

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Positive-class probability, one value per row."""
        data = self._transform(X)
        if hasattr(self.model, "predict_proba"):
            p = np.asarray(self.model.predict_proba(data))
            return p[:, -1] if p.ndim == 2 else p
        return np.asarray(self.model.predict(data), dtype=float)

    def predict(self, X: pd.DataFrame, threshold: float = 0.5) -> np.ndarray:
        """0/1 class at ``threshold`` on :meth:`predict_proba`."""
        return (self.predict_proba(X) >= threshold).astype(int)

    # ------------------------------------------------------------------ persistence
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bundle_version": _BUNDLE_VERSION, "pipeline": self}
        with open(path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "OMOPScoringPipeline":
        """Load a bundle written by :meth:`save`. Pickle: only load trusted files."""
        with open(path, "rb") as fh:
            payload = pickle.load(fh)  # noqa: S301 - documented trusted-file contract
        if not isinstance(payload, dict) or not isinstance(payload.get("pipeline"), cls):
            raise ValueError(f"{path} is not an OMOPScoringPipeline bundle.")
        if payload.get("bundle_version") != _BUNDLE_VERSION:
            raise ValueError(f"unsupported bundle version {payload.get('bundle_version')!r}.")
        return payload["pipeline"]

    def __repr__(self) -> str:
        return (f"OMOPScoringPipeline(model={type(self.model).__name__}, n_features={len(self.feature_names)}, "
                f"imputer={type(self.imputer).__name__ if self.imputer is not None else None}, "
                f"scaler={type(self.scaler).__name__ if self.scaler is not None else None}, strict={self.strict})")
