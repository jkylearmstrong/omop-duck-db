# DGX Spark smoke test record, 2026-11-13

Image: built from this repository's Dockerfile.dgx (base nvcr.io/nvidia/pytorch:25.01-py3), with the NumPy pin. Host: aarch64, NVIDIA GB10, driver 580.178.04, Docker 29.6.2.
Command: docker run --gpus all --rm <image> dgx-smoke   (exit code 0)

```
{
 "torch": "2.6.0a0+ecf3bae40a.nv25.01",
 "cuda_available": true,
 "cuda_version": "12.8",
 "devices": [
  {
   "index": 0,
   "name": "NVIDIA GB10",
   "capability": "12.1",
   "memory_gb": 121.6
  }
 ],
 "bf16": true,
 "matmul_2048_x5_seconds": 0.0204,
 "checked_device": "cuda"
}
Model    AUC-ROC (95% CI) Sensitivity Specificity    F1 Brier
  MLP 0.659 (0.645–0.668)       0.411       0.786 0.310 0.183
site    n    auroc
   A 6667 0.707466
   B 6667 0.702812
   C 6666 0.721449
trained and scored in 9.8s on cuda
```

Before the pin the same image failed here with: RuntimeError: Numpy is not available (NumPy 2.2.6 installed over the base image's 1.26.4).
