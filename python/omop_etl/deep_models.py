"""Deep-learning models scored in the same tables as the traditional ones (unreleased; needs ``pip install 'omop-duck-db[deep]'``).

* :func:`device_report`           - what torch sees (CUDA devices, bf16, versions): run it first on a new machine such as a DGX Spark.
* :func:`make_mlp_fit_predict`    - a ``fit_predict(x_train, y_train, x_test) -> probabilities`` for a torch multilayer perceptron:
  median imputation and scaling fitted on the training rows, early stopping on a held-out slice of them, GPU when available.
  It plugs into :func:`omop_etl.transport.evaluate_loso_transport` (``model_type="mlp"``) and :func:`deep_oof_predictions`.
* :func:`deep_oof_predictions`    - out-of-fold probabilities on the folds of :func:`omop_etl.study_report.study_folds` with the 50/50
  training sample of :func:`omop_etl.study_report.balanced_training_indices`, so a network is scored on exactly the stays the
  traditional models were.
* :func:`deep_study_tables`       - the ``Model_Performance`` row (bootstrap CI) for the network and, with a site column, its
  leave-one-site-out table, both ready to append to :func:`omop_etl.study_report.build_study_tables` output.

Tabular only. Sequence models over visit tokens (SARD, DeepPLP via omop-learn) use the tensors of
:func:`omop_etl.deep_dataset.extract_multimodal_deep_dataset`; wrap their training loop as a ``fit_predict`` callable to reuse the
folds, the leave-one-site-out evaluation and the tables here.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["device_report", "make_mlp_fit_predict", "deep_oof_predictions", "deep_study_tables"]


def _torch():
    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise ImportError("torch is required: pip install 'omop-duck-db[deep]' (on a DGX Spark use the NGC PyTorch image, see Dockerfile.dgx).") from exc
    return torch


def device_report() -> dict[str, Any]:
    """Torch / CUDA facts as a dict (also printed by ``scripts/dgx_smoke_test.py``): versions, CUDA availability, device names and
    memory, bf16 support, and a small matrix multiplication timed on the chosen device."""
    torch = _torch()
    out: dict[str, Any] = {"torch": torch.__version__, "cuda_available": bool(torch.cuda.is_available()), "cuda_version": getattr(torch.version, "cuda", None),
                           "devices": []}
    if out["cuda_available"]:
        for i in range(torch.cuda.device_count()):
            pr = torch.cuda.get_device_properties(i)
            out["devices"].append({"index": i, "name": pr.name, "capability": f"{pr.major}.{pr.minor}", "memory_gb": round(pr.total_memory / 2**30, 1)})
        out["bf16"] = bool(torch.cuda.is_bf16_supported())
    dev = "cuda" if out["cuda_available"] else "cpu"
    a = torch.randn(2048, 2048, device=dev)
    t0 = time.time()
    for _ in range(5):
        b = a @ a
    if dev == "cuda":
        torch.cuda.synchronize()
    out["matmul_2048_x5_seconds"] = round(time.time() - t0, 4)
    out["checked_device"] = dev
    return out


def _resolve_device(device: str):
    torch = _torch()
    return torch.device("cuda" if (device == "auto" and torch.cuda.is_available()) else ("cpu" if device == "auto" else device))


def make_mlp_fit_predict(*, hidden: Sequence[int] = (256, 128), dropout: float = 0.2, epochs: int = 40, batch_size: int = 256, lr: float = 2e-3,
                         weight_decay: float = 1e-4, patience: int = 5, val_fraction: float = 0.1, device: str = "auto", random_state: int = 42,
                         num_threads: int | None = None) -> Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]:
    """A ``fit_predict(x_train, y_train, x_test)`` for a multilayer perceptron (``hidden`` widths, ReLU, dropout, Adam, binary cross entropy).

    Inside each call: columns that are NaN throughout the training rows are dropped, remaining NaN are filled with training medians, features
    are standardised with training statistics, ``val_fraction`` of the training rows is held out for early stopping (``patience`` epochs),
    and the best-validation weights predict ``x_test``. Seeded: the same data and ``random_state`` give the same probabilities on a device."""
    torch = _torch()
    nn = torch.nn

    def fit_predict(xtr: np.ndarray, ytr: np.ndarray, xte: np.ndarray) -> np.ndarray:
        if num_threads:
            torch.set_num_threads(int(num_threads))
        dev = _resolve_device(device)
        xtr, xte = np.asarray(xtr, dtype="float64"), np.asarray(xte, dtype="float64")
        ytr = np.asarray(ytr, dtype="float32")
        keep = ~np.all(np.isnan(xtr), axis=0)
        xtr, xte = xtr[:, keep], xte[:, keep]
        med = np.where(np.isnan(np.nanmedian(xtr, axis=0)), 0.0, np.nanmedian(xtr, axis=0))
        xtr, xte = np.where(np.isnan(xtr), med, xtr), np.where(np.isnan(xte), med, xte)
        mu, sd = xtr.mean(axis=0), xtr.std(axis=0)
        sd = np.where(sd < 1e-8, 1.0, sd)
        xtr, xte = ((xtr - mu) / sd).astype("float32"), ((xte - mu) / sd).astype("float32")
        rng = np.random.default_rng(random_state)
        order = rng.permutation(len(ytr))
        n_val = max(1, int(round(val_fraction * len(order)))) if len(order) > 20 else 0
        va, tr = order[:n_val], order[n_val:]
        torch.manual_seed(random_state)
        layers: list[Any] = []
        width = xtr.shape[1]
        for h in hidden:
            layers += [nn.Linear(width, int(h)), nn.ReLU(), nn.Dropout(dropout)]
            width = int(h)
        net = nn.Sequential(*layers, nn.Linear(width, 1)).to(dev)
        opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=weight_decay)
        loss_fn = nn.BCEWithLogitsLoss()
        Xt, Yt = torch.from_numpy(xtr[tr]).to(dev), torch.from_numpy(ytr[tr]).to(dev)
        Xv, Yv = (torch.from_numpy(xtr[va]).to(dev), torch.from_numpy(ytr[va]).to(dev)) if n_val else (None, None)
        best, best_state, bad = np.inf, None, 0
        gen = torch.Generator().manual_seed(random_state)
        for _ in range(int(epochs)):
            net.train()
            perm = torch.randperm(len(Xt), generator=gen)
            for i in range(0, len(perm), batch_size):
                b = perm[i:i + batch_size].to(dev)
                opt.zero_grad()
                loss_fn(net(Xt[b]).squeeze(1), Yt[b]).backward()
                opt.step()
            if Xv is not None:
                net.eval()
                with torch.no_grad():
                    v = float(loss_fn(net(Xv).squeeze(1), Yv))
                if v < best - 1e-5:
                    best, bad = v, 0
                    best_state = {k: t.detach().clone() for k, t in net.state_dict().items()}
                else:
                    bad += 1
                    if bad >= patience:
                        break
        if best_state is not None:
            net.load_state_dict(best_state)
        net.eval()
        out = []
        with torch.no_grad():
            for i in range(0, len(xte), 8192):
                out.append(torch.sigmoid(net(torch.from_numpy(xte[i:i + 8192]).to(dev)).squeeze(1)).cpu().numpy())
        return np.concatenate(out).astype("float64") if out else np.zeros(0)

    fit_predict.__name__ = "mlp"
    return fit_predict


def deep_oof_predictions(fit_predict: Callable, X: pd.DataFrame | np.ndarray, y: Any, *, folds: Sequence[tuple[np.ndarray, np.ndarray]] | None = None,
                         n_splits: int = 5, groups: Any = None, balanced: bool = True, random_state: int = 42) -> np.ndarray:
    """Out-of-fold probabilities of any ``fit_predict`` (a network from :func:`make_mlp_fit_predict`, or your own). ``folds`` defaults to
    :func:`omop_etl.study_report.study_folds`; each training fold is upsampled to 50/50 with
    :func:`omop_etl.study_report.balanced_training_indices` exactly as the traditional models are."""
    from omop_etl.study_report import balanced_training_indices, study_folds

    yv = np.asarray(y).astype(int)
    A = X.to_numpy("float64") if isinstance(X, pd.DataFrame) else np.asarray(X, dtype="float64")
    folds = list(folds) if folds is not None else study_folds(yv, n_splits=n_splits, groups=groups, random_state=random_state)
    out = np.zeros(len(yv))
    for tr, va in folds:
        keep = balanced_training_indices(yv[tr], random_state=random_state) if balanced else np.arange(len(tr))
        out[va] = fit_predict(A[tr][keep], yv[tr][keep], A[va])
    return out


def deep_study_tables(fit_predict: Callable, X: pd.DataFrame, y: Any, *, model_name: str = "MLP", sites: Any = None, n_bootstraps: int = 100,
                      folds: Sequence[tuple[np.ndarray, np.ndarray]] | None = None, groups: Any = None, random_state: int = 42,
                      loso_kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Scores a network the way :func:`omop_etl.study_report.build_study_tables` scores the traditional models.

    Returns ``{"performance": one Model_Performance row, "oof": probabilities, "loso": per-site leave-one-site-out table or None,
    "seconds": {...}}``. ``sites`` (a site label per row) adds the leave-one-site-out evaluation
    (:func:`omop_etl.transport.evaluate_loso_transport`, ``loso_kwargs`` passed on)."""
    from omop_etl.study_report import performance_from_predictions
    from omop_etl.transport import evaluate_loso_transport

    t0 = time.time()
    oof = deep_oof_predictions(fit_predict, X, y, folds=folds, groups=groups, random_state=random_state)
    t1 = time.time()
    perf = performance_from_predictions(y, oof, model_name=model_name, n_bootstraps=n_bootstraps, groups=groups, random_state=random_state)
    loso = None
    if sites is not None:
        feats = pd.DataFrame(np.asarray(X, dtype="float64"), columns=list(X.columns) if isinstance(X, pd.DataFrame) else None)
        feats["__y"], feats["__site"] = np.asarray(y).astype(int), np.asarray(sites)
        loso = evaluate_loso_transport(feats, site_col="__site", model_type=fit_predict, label_col="__y", n_bootstraps=n_bootstraps,
                                       random_state=random_state, **dict(loso_kwargs or {})).per_site
    return {"performance": perf, "oof": oof, "loso": loso, "seconds": {"oof": round(t1 - t0, 1), "total": round(time.time() - t0, 1)}}
