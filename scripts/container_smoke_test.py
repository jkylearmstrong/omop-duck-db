"""Smoke test run inside a built image: renders a small manuscript to DOCX and PDF with Quarto, scans it, and writes and reads a Delta table.

Synthetic data only. Used by CI on the amd64 and arm64 images (see .github/workflows/ci.yml) and handy on a new machine:

    docker run --rm ghcr.io/jkylearmstrong/omop-duck-db:latest python scripts/container_smoke_test.py

Exit code 0 when everything passed; prints one line per check.
"""
import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))


def _workbook(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Table 5 - Cross-Site LOSO"
    ws.append(["Table 5: Leave-one-site-out AUC"])
    ws.append([])
    ws.append(["Held-out site", "AUC-ROC", "Test patients"])
    for i, lab in enumerate(["Site A", "Site B", "Site C"]):
        ws.append([lab, f"0.7{i}", 100 + i])
    wb.save(path)
    return path


def _figure(path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(3, 2))
    ax.bar(["Site A", "Site B", "Site C"], [0.70, 0.71, 0.72])
    fig.savefig(path, dpi=80)
    plt.close(fig)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-r", action="store_true", help="skip the R checks (Python-only images such as Dockerfile.dgx)")
    a = ap.parse_args(argv)
    import pandas as pd

    from omop_etl.lakehouse import export_to_delta_lake
    from omop_etl.manuscript_document import find_renderer, render_manuscript_document
    from omop_etl.run_privacy import scan

    failures = []

    def check(name, ok, note=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({note})" if note else ""))
        if not ok:
            failures.append(name)

    print(f"machine {platform.machine()}, python {platform.python_version()}")
    tools = find_renderer()
    q = tools["quarto"]
    check("quarto on PATH", bool(q), subprocess.run([q, "--version"], capture_output=True, text=True).stdout.strip() if q else "missing")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wb = _workbook(tmp / "Smoke_Manuscript_Tables.xlsx")
        fig = _figure(tmp / "Smoke_Figure_AUC.png")
        r = render_manuscript_document(wb, tmp / "doc", prefix="Smoke", figures=[fig], window=("2017-01-01", "2022-09-30"))
        check("manuscript DOCX", "docx" in r["files"], r["note"])
        check("manuscript PDF", "pdf" in r["files"], r["note"])
        hits, skipped = scan(tmp / "doc", ["Springfield General"])
        unread = [s for s in skipped if not s.endswith(".png")]
        check("privacy scan reads DOCX and PDF", not hits and not unread, f"{len(hits)} hits, unreadable: {unread}")
        df = pd.DataFrame({"person_id": [1, 2, 3], "site_anon": ["Site A", "Site B", "Site C"], "auc": [0.70, 0.71, 0.72]})
        export_to_delta_lake(df, tmp / "delta" / "smoke")
        from deltalake import DeltaTable

        back = DeltaTable(str(tmp / "delta" / "smoke")).to_pandas()
        check("Delta Lake write and read", len(back) == 3 and set(back.site_anon) == {"Site A", "Site B", "Site C"})
    if not a.skip_r:
        rs = shutil.which("Rscript")
        out = subprocess.run([rs, "-e", "suppressMessages({library(omopduckdb); library(rmarkdown); library(knitr)}); "
                                        "cat(as.character(packageVersion('omopduckdb')), rmarkdown::pandoc_available(), "
                                        "!is.null(quarto::quarto_path()), sep=' ')"],
                             capture_output=True, text=True) if rs else None
        ok = bool(out) and out.returncode == 0 and out.stdout.strip().endswith("TRUE TRUE")
        check("R: omopduckdb, rmarkdown + Pandoc, quarto CLI", ok, (out.stdout.strip() or out.stderr.strip()[-200:]) if out else "Rscript missing")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
