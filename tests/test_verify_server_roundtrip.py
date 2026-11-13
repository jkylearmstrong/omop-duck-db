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


def test_missing_env_var_is_an_error(monkeypatch):
    monkeypatch.delenv("OMOP_SERVER_URL", raising=False)
    with pytest.raises(SystemExit):
        vsr.main([])
