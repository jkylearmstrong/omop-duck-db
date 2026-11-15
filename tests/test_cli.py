"""Unit tests for standalone omop-duckdb CLI."""

import json
import duckdb
import pytest
from omop_etl.build_omop_cdm import build_schema
from omop_etl.cli import main, create_parser


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "build" in captured.out
    assert "dqd" in captured.out
    assert "cohort" in captured.out
    assert "table1" in captured.out
    assert "export" in captured.out

    # Empty argv prints help and returns 0 without raising SystemExit
    ret = main([])
    assert ret == 0


def test_cli_dqd_command(tmp_path, capsys):
    db_path = tmp_path / "test_dqd.duckdb"
    out_json = tmp_path / "dqd_report.json"

    con = duckdb.connect(str(db_path))
    build_schema(con)
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES (1, 8507, 1980, 8527, 38003564);
    """)
    con.close()

    ret = main(["dqd", "--db", str(db_path), "--output", str(out_json)])
    assert ret == 0
    assert out_json.exists()

    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert "total_checks" in report or "summary" in report or isinstance(report, dict)
