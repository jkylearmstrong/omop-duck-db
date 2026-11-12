"""Smoke test for a deep-learning machine (for example a DGX Spark): prints the torch / CUDA report, trains the study's MLP on
synthetic data and scores it with the study tables. Run inside the Dockerfile.dgx image:

    python scripts/dgx_smoke_test.py            # add --rows 200000 to time a larger fit

Exit code 0 only if the network trained and scored (on GPU if one is visible; --require-gpu fails when there is none)."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from omop_etl.deep_models import deep_study_tables, device_report, make_mlp_fit_predict  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--features", type=int, default=60)
    ap.add_argument("--require-gpu", action="store_true")
    a = ap.parse_args(argv)
    rep = device_report()
    print(json.dumps(rep, indent=1))
    if a.require_gpu and not rep["cuda_available"]:
        print("no CUDA device visible", file=sys.stderr)
        return 2
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.normal(size=(a.rows, a.features)), columns=[f"f{i}" for i in range(a.features)])
    y = (rng.random(a.rows) < 1 / (1 + np.exp(-(X["f0"] * 0.8 + X["f1"] * 0.4 - 2.0)))).astype(int)
    t0 = time.time()
    res = deep_study_tables(make_mlp_fit_predict(epochs=10), X, y, sites=np.array(["A", "B", "C"])[np.arange(a.rows) % 3], n_bootstraps=20)
    print(res["performance"].to_string(index=False))
    print(res["loso"][["site", "n", "auroc"]].to_string(index=False))
    print(f"trained and scored in {time.time() - t0:.1f}s on {rep['checked_device']}")
    return 0 if float(res["loso"]["auroc"].min()) > 0.6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
