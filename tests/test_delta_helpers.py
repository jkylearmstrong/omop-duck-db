"""Unit tests for Delta Lake export and workbook delta CLI helpers."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import duckdb
import pandas as pd
import pytest

from omop_etl.lakehouse import export_to_delta_lake, omop_connect_lakehouse
from omop_etl import manuscript_tables as mt
from test_golden_workbook import build as build_manuscript_wb


def test_export_to_delta_lake_dataframe(tmp_path):
    df = pd.DataFrame({
        "person_id": [101, 102, 103],
        "gender_concept_id": [8507, 8532, 8507],
        "year_of_birth": [1975, 1982, 1991],
        "site": ["Site A", "Site B", "Site A"],
    })
    target = tmp_path / "delta_person"
    try:
        out = export_to_delta_lake(df, target, partition_by=["site"])
        assert Path(out).exists()
        assert (Path(out) / "_delta_log").exists()

        # Connect and verify via omop_connect_lakehouse
        con = omop_connect_lakehouse(tmp_path, format="delta", tables=["delta_person"])
        res = con.execute("SELECT count(*) FROM delta_person").fetchone()[0]
        assert res == 3
        con.close()
    except OSError as err:
        if "Access is denied" in str(err) or "os error 5" in str(err).lower():
            pytest.skip("DeltaLake atomic rename not permitted in current OS sandbox")
        raise


def test_export_to_delta_lake_duckdb(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE test_tbl AS SELECT 1 AS id, 'A' AS val UNION ALL SELECT 2, 'B';")
    target = tmp_path / "delta_duck"
    try:
        out = export_to_delta_lake(con, target, table_name="test_tbl")
        assert Path(out).exists()
        assert (Path(out) / "_delta_log").exists()
    except OSError as err:
        if "Access is denied" in str(err) or "os error 5" in str(err).lower():
            pytest.skip("DeltaLake atomic rename not permitted in current OS sandbox")
        raise
    finally:
        con.close()


def test_compare_study_deltas_cli(tmp_path):
    # Create two synthetic manuscript workbooks
    wb1 = build_manuscript_wb(tmp_path / "wb1.xlsx")
    wb2 = build_manuscript_wb(tmp_path / "wb2.xlsx", sdi_missing_term=False)

    out_dir = tmp_path / "out"
    md_out = out_dir / "summary.md"
    script = Path(__file__).resolve().parents[1] / "scripts" / "compare_study_deltas.py"

    cmd = [
        sys.executable, str(script),
        "--new", str(wb2),
        "--baseline", str(wb1),
        "--out-dir", str(out_dir),
        "--prefix", "TestRun",
        "--markdown", str(md_out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert (out_dir / "TestRun_Deltas.csv").exists()
    assert (out_dir / "TestRun_Delta_Rows.csv").exists()
    assert md_out.exists()
    content = md_out.read_text(encoding="utf-8")
    assert "Manuscript Workbook Delta Summary" in content
    assert "TestRun" in content
