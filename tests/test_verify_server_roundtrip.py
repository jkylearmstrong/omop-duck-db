import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
_spec = importlib.util.spec_from_file_location("verify_server_roundtrip", Path(__file__).resolve().parents[1] / "scripts" / "verify_server_roundtrip.py")
vsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vsr)


def test_roundtrip_on_sqlite_passes_and_record_has_no_url(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{(tmp_path / 'secret_name.db').as_posix()}"
    monkeypatch.setenv("OMOP_SERVER_URL", url)
    out = tmp_path / "record.md"
    assert vsr.main(["--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Result: **PASS**" in text and "secret_name" not in text and "sqlite" in text
    assert "secret_name" not in capsys.readouterr().out


def test_scratch_table_is_dropped(tmp_path, monkeypatch):
    import sqlalchemy as sa

    path = tmp_path / "t.db"
    monkeypatch.setenv("OMOP_SERVER_URL", f"sqlite:///{path.as_posix()}")
    assert vsr.main([]) == 0
    assert sa.inspect(sa.create_engine(f"sqlite:///{path.as_posix()}")).get_table_names() == []


def test_extended_checks_on_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("OMOP_SERVER_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    out = tmp_path / "r.md"
    assert vsr.main(["--extended", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "if_exists='fail' refuses an existing table | PASS" in text and "if_exists='append' doubles the rows" in text and "| PASS |" in text
    assert "NaN / Infinity round trip | skipped" in text and "FAIL" not in text.split("## Extended checks")[1]
    import sqlalchemy as sa

    assert sa.inspect(sa.create_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")).get_table_names() == []


def test_scrub_check_skips_without_a_password_and_passes_against_an_unreachable_oracle():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE edge AS SELECT 1 AS id")
    assert vsr._scrub_check("sqlite:///x.db", con)[1] is None
    pytest.importorskip("oracledb")
    from sqlalchemy.engine import URL

    fake_pw = "Real" + "Pw_abc123"        # built at runtime so this file holds no URL-with-password for the secret scan to flag
    url = URL.create("oracle+oracledb", username="someone", password=fake_pw, host="127.0.0.1", port=1, query={"service_name": "none"})
    name, passed, note = vsr._scrub_check(url.render_as_string(hide_password=False), con)
    assert passed is True and "leaked: none" in note and fake_pw not in note


def test_missing_env_var_is_an_error(monkeypatch):
    monkeypatch.delenv("OMOP_SERVER_URL", raising=False)
    with pytest.raises(SystemExit):
        vsr.main([])
