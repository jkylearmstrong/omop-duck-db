"""Strict RFC 8259 JSON: ``null`` instead of the IEEE tokens ``NaN`` / ``Infinity`` / ``-Infinity``.

Python's ``json`` emits ``NaN`` by default, which R (``jsonlite``), browsers and most external tools reject. Every file the
package writes for exchange (feature specs, bundles, manifests, telemetry packets, DQD reports) goes through
:func:`strict_dumps`; :func:`strict_loads` reads third-party files that may already contain the bad tokens.
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = ["sanitize", "strict_dumps", "strict_loads", "contains_nonfinite"]


def sanitize(obj: Any) -> Any:
    """Recursively replaces non-finite floats (Python or NumPy) and pandas missing scalars by ``None``."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    try:  # NumPy scalars / arrays and pandas NA without importing them eagerly
        import numpy as np

        if isinstance(obj, np.generic):
            return sanitize(obj.item())
        if isinstance(obj, np.ndarray):
            return sanitize(obj.tolist())
    except ImportError:  # pragma: no cover
        pass
    if obj is not None and type(obj).__name__ in ("NAType", "NaTType"):
        return None
    return obj


def strict_dumps(obj: Any, **kwargs: Any) -> str:
    """``json.dumps`` with non-finite numbers written as ``null`` and ``allow_nan=False`` as a backstop."""
    kwargs.setdefault("allow_nan", False)
    return json.dumps(sanitize(obj), **kwargs)


def contains_nonfinite(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(contains_nonfinite(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_nonfinite(v) for v in obj)
    return isinstance(obj, float) and not math.isfinite(obj)


def strict_loads(text: str, **kwargs: Any) -> Any:
    """Loads JSON, mapping the non-standard ``NaN`` / ``Infinity`` tokens to ``None`` (as ``null`` would be)."""
    return json.loads(text, parse_constant=lambda _tok: None, **kwargs)
