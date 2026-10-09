"""Unit tests for Delta Lake & Iceberg Lakehouse Connector."""

import tempfile
from pathlib import Path
import duckdb
import pandas as pd
import pytest

from omop_etl.lakehouse import (
    omop_connect_lakehouse,
)


@pytest.fixture
def mock_lakehouse_dir():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        person_dir = root / "person"
        person_dir.mkdir(parents=True)

        # Create mock partitioned person parquet files
        df1 = pd.DataFrame({
            "person_id": [1, 2],
            "gender_concept_id": [8507, 8532],
            "year_of_birth": [1980, 1990],
        })
        df2 = pd.DataFrame({
            "person_id": [3, 4],
            "gender_concept_id": [8507, 8532],
            "year_of_birth": [1985, 1995],
        })

        con = duckdb.connect()
        con.register("df1", df1)
        con.register("df2", df2)
        con.execute(f"COPY df1 TO '{person_dir / 'part-0.parquet'}' (FORMAT PARQUET);")
        con.execute(f"COPY df2 TO '{person_dir / 'part-1.parquet'}' (FORMAT PARQUET);")
        con.close()

        yield root


def test_lakehouse_connect_local(mock_lakehouse_dir):
    con = omop_connect_lakehouse(
        lake_uri=mock_lakehouse_dir,
        format="delta",
        tables=["person"],
    )

    # Check v_person view
    df_v = con.execute("SELECT * FROM v_person ORDER BY person_id").df()
    assert len(df_v) == 4
    assert list(df_v["person_id"]) == [1, 2, 3, 4]

    # Check bare alias person
    df_bare = con.execute("SELECT count(*) FROM person").fetchone()[0]
    assert df_bare == 4
    con.close()
