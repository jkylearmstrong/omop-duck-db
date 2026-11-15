"""Unit tests for Delta Lake export, the workbook delta CLI, the age_method study decision and the sqlite Decimal adapter."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import subprocess
import sys
import duckdb
import pandas as pd
import pytest

from omop_etl.lakehouse import export_to_delta_lake, omop_connect_lakehouse
from test_golden_workbook import build as build_manuscript_wb


def _delta_or_skip(fn):
    """Runs a Delta write; skips when the deltalake package is missing or the OS sandbox refuses its atomic rename."""
    pytest.importorskip("deltalake")
    try:
        return fn()
    except OSError as err:
        if "Access is denied" in str(err) or "os error 5" in str(err).lower():
            pytest.skip("DeltaLake atomic rename not permitted in current OS sandbox")
        raise


def _read_delta(path: Path) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(str(path)).to_pandas()


def test_export_to_delta_lake_dataframe(tmp_path):
    df = pd.DataFrame({
        "person_id": [101, 102, 103],
        "gender_concept_id": [8507, 8532, 8507],
        "year_of_birth": [1975, 1982, 1991],
        "site": ["Site A", "Site B", "Site A"],
    })
    target = tmp_path / "delta_person"
    out = _delta_or_skip(lambda: export_to_delta_lake(df, target, partition_by=["site"]))
    assert Path(out).exists()
    assert (Path(out) / "_delta_log").exists()
    back = _read_delta(out)
    assert sorted(back["person_id"]) == [101, 102, 103]

    # Connect and verify via omop_connect_lakehouse
    con = omop_connect_lakehouse(tmp_path, format="delta", tables=["delta_person"])
    try:
        assert con.execute("SELECT count(*) FROM delta_person").fetchone()[0] == 3
    finally:
        con.close()


def test_export_to_delta_lake_duckdb(tmp_path):
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA s; CREATE TABLE s.test_tbl AS SELECT 1 AS id, 'A' AS val UNION ALL SELECT 2, 'B';")
        out = _delta_or_skip(lambda: export_to_delta_lake(con, tmp_path / "delta_duck", table_name="s.test_tbl"))
        assert (Path(out) / "_delta_log").exists()
        assert sorted(_read_delta(out)["id"]) == [1, 2]
        rel = _delta_or_skip(lambda: export_to_delta_lake(con.sql("SELECT * FROM s.test_tbl WHERE id = 2"), tmp_path / "delta_rel"))
        assert _read_delta(rel)["val"].tolist() == ["B"]
        with pytest.raises(ValueError, match="table_name"):
            export_to_delta_lake(con, tmp_path / "delta_none")
    finally:
        con.close()


def test_export_to_delta_lake_parquet_directory_reads_each_file_once(tmp_path):
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    pd.DataFrame({"id": [1, 2]}).to_parquet(src / "top.parquet")
    pd.DataFrame({"id": [3]}).to_parquet(src / "nested" / "deep.parquet")
    out = _delta_or_skip(lambda: export_to_delta_lake(src, tmp_path / "delta_dir"))
    assert sorted(_read_delta(out)["id"]) == [1, 2, 3]  # the top-level file used to be read twice


def test_export_to_delta_lake_rejects_bad_input(tmp_path):
    pytest.importorskip("deltalake")
    with pytest.raises(ValueError, match="mode"):
        export_to_delta_lake(pd.DataFrame({"a": [1]}), tmp_path / "x", mode="merge")
    with pytest.raises(FileNotFoundError):
        export_to_delta_lake(tmp_path / "missing", tmp_path / "y")
    with pytest.raises(TypeError):
        export_to_delta_lake(42, tmp_path / "z")


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
    cells = pd.read_csv(out_dir / "TestRun_Deltas.csv")
    assert len(cells) > 0  # dropping the SDI-missing term moves Table 6 cells
    assert (out_dir / "TestRun_Delta_Rows.csv").exists()
    content = md_out.read_text(encoding="utf-8")
    assert "Manuscript Workbook Delta Summary" in content
    assert "TestRun" in content

    res = subprocess.run(cmd + ["--fail-on-diff"], capture_output=True, text=True)
    assert res.returncode == 1


class _Stop(Exception):
    pass


@pytest.mark.parametrize("cohort_params, age_method, expected", [
    (None, None, "completed_years"),                                            # preset default
    (None, "year_difference", "year_difference"),                               # explicit argument
    ({"min_age": 18, "age_method": "year_difference"}, None, "year_difference"),  # cohort_params kept over the preset
    ({"min_age": 18, "age_method": "year_difference"}, "completed_years", "completed_years"),  # explicit argument wins
])
def test_run_study_age_method_precedence(tmp_path, monkeypatch, cohort_params, age_method, expected):
    from omop_etl import study_run

    seen = {}

    def fake_site_frame(db, site, *, params, **kw):
        seen.update(params)
        raise _Stop

    monkeypatch.setattr(study_run, "_site_frame", fake_site_frame)
    with pytest.raises(_Stop):
        study_run.run_study(tmp_path, tmp_path / "out", sites=["A"], cohort_params=cohort_params, age_method=age_method)
    assert seen["age_method"] == expected
    if cohort_params is not None:
        assert "age_method" in cohort_params and cohort_params["age_method"] == "year_difference"  # caller's mapping untouched


def test_andromeda_registers_decimal_adapter():
    import omop_etl.andromeda  # noqa: F401  (registers the adapter on import)

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (v REAL)")
        conn.execute("INSERT INTO t VALUES (?)", (Decimal("1.25"),))
        assert conn.execute("SELECT v FROM t").fetchone()[0] == 1.25
    finally:
        conn.close()
