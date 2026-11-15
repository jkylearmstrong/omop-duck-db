"""v0.8.7: vectorized in-database Achilles analyses, checked against hand-computed values."""
import duckdb
import pytest

from omop_etl.achilles import ACHILLES_ANALYSES, achilles_analysis_catalog, run_achilles
from omop_etl.build_omop_cdm import build_schema


@pytest.fixture()
def con():
    c = duckdb.connect()
    build_schema(c)
    for pid, yob, g in ((1, 1980, 8507), (2, 1990, 8532), (3, 1990, 8532)):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, {g}, {yob}, 8527, 38003563)")
    ops = [(1, 1, "2010-01-01", "2010-12-31"), (2, 1, "2012-01-01", "2012-01-31"), (3, 2, "2015-06-15", "2016-06-14")]
    for oid, pid, s, e in ops:
        c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                  f"observation_period_end_date, period_type_concept_id) VALUES ({oid}, {pid}, DATE '{s}', DATE '{e}', 0)")
    for vid, pid, s in ((1, 1, "2010-03-05"), (2, 1, "2010-03-20"), (3, 2, "2015-07-01")):
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id) VALUES ({vid}, {pid}, 9201, DATE '{s}', DATE '{s}', 0)")
    return c


def _rows(con, q):
    return con.execute(q).fetchall()


def test_demographics_and_observation_periods(con):
    out = run_achilles(con)
    assert {0, 1, 2, 101, 103, 105, 108, 113, 200, 201, 202} <= set(out["run"])
    assert _rows(con, "SELECT count_value FROM achilles_results WHERE analysis_id = 1") == [(3,)]
    assert dict(_rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 2")) == {"8507": 1, "8532": 2}
    assert dict(_rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 3")) == {"1980": 1, "1990": 2}
    # age at first observation period: person 1 -> 30, person 2 -> 25 (3 has no period)
    assert dict(_rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 101")) == {"30": 1, "25": 1}
    d103 = con.execute("SELECT count_value, min_value, max_value, avg_value FROM achilles_results_dist WHERE analysis_id = 103").fetchone()
    assert d103 == (2, 25.0, 30.0, 27.5)
    d105 = con.execute("SELECT count_value, min_value, max_value FROM achilles_results_dist WHERE analysis_id = 105").fetchone()
    assert d105 == (2, 364.0, 365.0)          # first periods only: 2010 (364 d) and 2015-06-15..2016-06-14 (365 d)
    assert dict(_rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 113")) == {"2": 1, "1": 1}


def test_domain_analyses(con):
    run_achilles(con, [200, 201, 202])
    assert _rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 200") == [("9201", 2)]
    assert _rows(con, "SELECT stratum_1, count_value FROM achilles_results WHERE analysis_id = 201") == [("9201", 3)]
    assert dict(_rows(con, "SELECT stratum_2, count_value FROM achilles_results WHERE analysis_id = 202")) == {"201003": 1, "201507": 1}


def test_skips_missing_tables_and_guards(con):
    con.execute("DROP TABLE death")
    out = run_achilles(con, [500, 1, 2100])
    assert out["run"] == [1, 2100]
    assert 500 in out["skipped"] and "death" in out["skipped"][500]
    with pytest.raises(ValueError, match="unknown analysis"):
        run_achilles(con, [999999])
    with pytest.raises(ValueError, match="invalid table"):
        run_achilles(con, [1], results_table="x; DROP")


def test_min_cell_count_and_catalog(con):
    run_achilles(con, [2], min_cell_count=2)
    assert _rows(con, "SELECT stratum_1 FROM achilles_results WHERE analysis_id = 2") == [("8532",)]
    cat = achilles_analysis_catalog()
    assert len(cat) == len(ACHILLES_ANALYSES) and cat.loc[cat.analysis_id == 103, "is_distribution"].item() == 1


def test_cli_achilles_and_dqd_full(tmp_path, capsys):
    from omop_etl.cli import main

    db = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1980, 0, 0)")
    c.close()
    assert main(["achilles", "--db", str(db), "--analysis", "1", "2"]) == 0
    assert '"run"' in capsys.readouterr().out
    c = duckdb.connect(str(db), read_only=True)
    assert c.execute("SELECT count(*) FROM achilles_results").fetchone()[0] == 2
    c.close()
    out = tmp_path / "dqd.json"
    assert main(["dqd", "--db", str(db), "--full", "--output", str(out)]) == 0
    assert out.stat().st_size > 10000


def test_matches_official_achilles_on_the_fixture(con):
    """Golden values from the official R package (Achilles 1.7.2, DuckDB) run on this same fixture."""
    import math

    run_achilles(con, [0, 1, 2, 3, 4, 5, 101, 103, 105, 108, 111, 112, 113, 200, 201, 202])
    got = {(a, s1, s2): n for a, s1, s2, n in con.execute(
        "SELECT analysis_id, stratum_1, stratum_2, count_value FROM achilles_results").fetchall()}
    official = {(1, None, None): 3, (2, "8507", None): 1, (2, "8532", None): 2, (3, "1980", None): 1, (3, "1990", None): 2,
                (4, "8527", None): 3, (5, "38003563", None): 3, (101, "25", None): 1, (101, "30", None): 1,
                (111, "201001", None): 1, (111, "201201", None): 1, (111, "201506", None): 1,
                (112, "201012", None): 1, (112, "201201", None): 1, (112, "201606", None): 1,
                (113, "1", None): 1, (113, "2", None): 1, (200, "9201", None): 2, (201, "9201", None): 3,
                (202, "9201", "201003"): 1, (202, "9201", "201507"): 1}
    for key, n in official.items():
        assert got.get(key) == n, key
    assert got[(108, "12", None)] == 2                       # Achilles writes the bucket as "12.0" on DuckDB, "12" elsewhere
    dist = {r[0]: r[1:] for r in con.execute(
        "SELECT analysis_id, count_value, min_value, max_value, avg_value, stdev_value, median_value, p10_value, p25_value, "
        "p75_value, p90_value FROM achilles_results_dist").fetchall()}
    for aid, exp in ((103, (2, 25, 30, 27.5, 3.535534, 25, 25, 25, 30, 30)), (105, (2, 364, 365, 364.5, 0.707107, 364, 364, 364, 365, 365))):
        assert all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(dist[aid], exp)), aid


def test_single_value_distribution_has_null_stdev_like_achilles():
    c = duckdb.connect()
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1980, 0, 0)")
    c.execute("INSERT INTO observation_period VALUES (1, 1, DATE '2010-01-01', DATE '2010-12-31', 0)")
    run_achilles(c, [103])
    assert c.execute("SELECT count_value, stdev_value, median_value FROM achilles_results_dist WHERE analysis_id = 103").fetchone() == (1, None, 30.0)
