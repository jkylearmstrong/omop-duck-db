import importlib.util
import json
import zipfile
from pathlib import Path

import pandas as pd

_spec = importlib.util.spec_from_file_location("check_run_privacy", Path(__file__).resolve().parents[1] / "scripts" / "check_run_privacy.py")
crp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crp)


def test_clean_folder_passes(tmp_path):
    (tmp_path / "Study_Table1.csv").write_text("variable,Site A\nage,60\n", encoding="utf-8")
    (tmp_path / "Study_run.json").write_text(json.dumps({"labels": ["Site A"], "timings_seconds": {"site_Site A": 1.0}}), encoding="utf-8")
    (tmp_path / "fig.png").write_bytes(b"\x89PNG XYZ")  # binary: reported as not scanned, never a hit
    hits, skipped = crp.scan(tmp_path, ["Springfield", "XYZ"])
    assert hits == [] and skipped == ["fig.png"]


def test_finds_names_in_file_names_text_zip_parts_and_parquet(tmp_path):
    (tmp_path / "features_Springfield.parquet").write_bytes(b"")  # name in the file name; empty file is reported as unreadable, not clean
    (tmp_path / "run.json").write_text('{"timings_seconds": {"site_Shelbyville": 1.0}}', encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "wb.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", "<t>from SPRINGFIELD</t>")
    pd.DataFrame({"site_source": ["Capital City"], "n": [1]}).to_parquet(tmp_path / "f.parquet")
    hits, skipped = crp.scan(tmp_path, ["Springfield", "Shelbyville", "Capital City"])
    got = {(h[0], h[1]) for h in hits}
    assert ("features_Springfield.parquet", 1) in got and ("run.json", 2) in got and ("wb.xlsx", 1) in got and ("f.parquet", 3) in got
    assert any("features_Springfield.parquet" in s for s in skipped)


def test_whole_word_only(tmp_path):
    (tmp_path / "a.txt").write_text("Ogdenville of doom; XYZEDO; xXYZ", encoding="utf-8")
    assert crp.scan(tmp_path, ["XYZ"])[0] == []
    assert crp.scan(tmp_path, ["Ogdenville"])[0] != []


def test_cli_exit_code_and_names_not_echoed(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "log.txt").write_text("loaded Springfield", encoding="utf-8")
    names = tmp_path / "names.txt"
    names.write_text("# real names\nSpringfield\n", encoding="utf-8")
    assert crp.main(["--dir", str(out), "--names-file", str(names)]) == 1
    text = capsys.readouterr().out
    assert "name #1" in text and "Springfield" not in text
    (out / "log.txt").write_text("loaded Site A", encoding="utf-8")
    assert crp.main(["--dir", str(out), "--names-file", str(names)]) == 0
