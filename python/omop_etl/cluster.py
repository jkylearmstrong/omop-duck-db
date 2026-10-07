"""Cluster / Remote Execution Connector for DGX, Slurm, and Containerized ML training.

Provides helpers for submitting large-scale Transformer (SARD) training and Parquet feature
extraction jobs to remote GPU clusters or container engines.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Sequence


def build_cluster_command(
    dataset_path: str,
    model: str = "sard",
    container: str | None = None,
    gpus: int = 1,
    script_path: str | None = None,
    extra_args: Sequence[str] | None = None,
) -> list[str]:
    """Constructs command array for local Docker/Apptainer or Slurm cluster execution.

    Args:
        dataset_path: Path to Parquet or DuckDB dataset file.
        model: Model architecture name ('sard', 'logistic', 'transformer').
        container: Docker / NGC container image (e.g. 'nvcr.io/nvidia/pytorch:25.01-py3').
        gpus: Number of GPUs to allocate (default 1).
        script_path: Optional path to training script.
        extra_args: Additional command line arguments.

    Returns:
        list[str]: Executable command list.
    """
    cmd = []
    if container:
        cmd.extend(["docker", "run", "--gpus", f"all", "-v", f"{os.getcwd()}:/workspace", "-w", "/workspace", container])

    cmd.extend(["python", script_path or "-m", model if not script_path else "", "--dataset", dataset_path, "--gpus", str(gpus)])
    if extra_args:
        cmd.extend(extra_args)

    return [c for c in cmd if c]


def cluster_submit(
    host: str = "localhost",
    dataset: str = "cohort.parquet",
    model: str = "sard",
    container: str | None = None,
    gpus: int = 1,
    dry_run: bool = True,
    extra_args: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Submits or prints a remote execution job for cluster GPU training.

    Args:
        host: Target host or Slurm controller.
        dataset: Path to cohort dataset.
        model: Model name ('sard', 'transformer', 'lasso').
        container: Container image.
        gpus: Number of GPUs requested.
        dry_run: If True, returns generated command and shell script without executing.
        extra_args: Extra command-line arguments.

    Returns:
        dict: Execution summary with host, command, and status.
    """
    cmd_list = build_cluster_command(
        dataset_path=dataset,
        model=model,
        container=container,
        gpus=gpus,
        extra_args=extra_args,
    )
    cmd_str = " ".join(shlex.quote(c) for c in cmd_list)

    if host not in ("localhost", "127.0.0.1"):
        full_command = f"ssh {shlex.quote(host)} {shlex.quote(cmd_str)}"
    else:
        full_command = cmd_str

    result = {
        "host": host,
        "model": model,
        "dataset": dataset,
        "gpus": gpus,
        "command": full_command,
        "dry_run": dry_run,
        "status": "DRY_RUN" if dry_run else "SUBMITTED",
    }

    if not dry_run:
        proc = subprocess.run(full_command, shell=True, capture_output=True, text=True)
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout
        result["stderr"] = proc.stderr
        result["status"] = "SUCCESS" if proc.returncode == 0 else "FAILED"

    return result
