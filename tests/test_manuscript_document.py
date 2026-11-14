"""Renderer paths of the manuscript document, the privacy scanner's edge cases and relabel errors (no Quarto needed: a stand-in renderer)."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import omop_etl.manuscript_document as md  # noqa: E402
import omop_etl.run_privacy as rp  # noqa: E402
from omop_etl.study_relabel import label_translation, relabel_outputs  # noqa: E402
from omop_etl.study_run import RunPrivacyError, _privacy_names, run_study  # noqa: E402


def _workbook(path: Path, labels=("Site A", "Site B")) -> Path:
    """A two-sheet manuscript workbook in the layout manuscript_workbook writes (title row 1, header row 3)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Table 5 - Cross-Site LOSO"
    ws.append(["Table 5: LOSO by site"])
    ws.append([])
    ws.append(["Held-out site", "AUC-ROC", "Test patients"])
    for i, lab in enumerate(labels):
        ws.append([lab, f"0.7{i}", 100 + i])
    ws2 = wb.create_sheet("Table 9 - Empty")
    ws2.append(["Table 9: nothing yet"])
    ws3 = wb.create_sheet("Manifest & Index")
    ws3.append(["Manifest"])
    wb.save(path)
    return path


def _docx(path: Path, text: str = "Site A") -> None:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    d.core_properties.author = "someone.local"
    d.core_properties.last_modified_by = "someone.local"
    d.save(str(path))


def _fake_quarto(tmp_path, monkeypatch, behaviour):
    """find_renderer reports Quarto; subprocess.run does what ``behaviour(fmt, target)`` says."""
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": "quarto", "pandoc": None, "typst": None})
    calls = []

    def run(cmd, cwd, check, capture_output, timeout):
        calls.append(cmd)
        fmt = "pdf" if "typst" in cmd else "docx"
        target = Path(cwd) / cmd[cmd.index("--output") + 1]
        (Path(cwd) / "Study_Manuscript_files").mkdir(exist_ok=True)  # Quarto leaves these behind
        (Path(cwd) / "Study_Manuscript.typ").write_text("#x", encoding="utf-8")
        behaviour(fmt, target)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    return calls


def test_quarto_path_scrubs_word_properties_and_cleans_up(tmp_path, monkeypatch):
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    fig_dir = tmp_path / "figs"
    fig_dir.mkdir()
    (fig_dir / "Study_Figure_Flow.png").write_bytes(b"\x89PNG")

    def make(fmt, target):
        if fmt == "docx":
            _docx(target)
        else:
            target.write_bytes(b"%PDF-1.4 stand-in")

    calls = _fake_quarto(tmp_path, monkeypatch, make)
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[fig_dir / "Study_Figure_Flow.png"], author="Study team", window=("2017-01-01", "2022-09-30"))
    assert r["renderer"] == "quarto" and set(r["files"]) == {"docx", "pdf"} and r["missing"] == [] and r["note"] == ""
    assert [c[3:5] for c in calls] == [["--to", "docx"], ["--to", "typst"]]
    assert (tmp_path / "doc" / "Study_Figure_Flow.png").exists() and r["copied"]
    assert not (tmp_path / "doc" / "Study_Manuscript_files").exists() and not (tmp_path / "doc" / "Study_Manuscript.typ").exists()
    with zipfile.ZipFile(r["files"]["docx"]) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "someone.local" not in core and "<dc:creator>Study team</dc:creator>" in core
    qmd = r["qmd"].read_text(encoding="utf-8")
    assert 'author: "Study team"' in qmd and "_(empty)_" in qmd and "Manifest" not in qmd and 'fig-alt="Figure Flow"' in qmd


def test_renderer_failures_are_reported_not_raised(tmp_path, monkeypatch):
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")

    def fail(fmt, target):
        if fmt == "docx":
            raise subprocess.CalledProcessError(1, "quarto", stderr=b"line one\nERROR: typst missing font")
        raise subprocess.TimeoutExpired("quarto", 5)

    _fake_quarto(tmp_path, monkeypatch, fail)
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[])
    assert r["files"] == {} and r["missing"] == ["docx", "pdf"]
    assert "quarto failed" in r["note"] and "missing font" in r["note"] and "timed out" in r["note"]

    _fake_quarto(tmp_path, monkeypatch, lambda fmt, target: None)  # ran, wrote nothing
    r = md.render_manuscript_document(wb, tmp_path / "doc2", formats=["pdf"])
    assert r["missing"] == ["pdf"] and "wrote no file" in r["note"]
    with pytest.raises(ValueError, match="Unknown formats"):
        md.render_manuscript_document(wb, tmp_path / "doc3", formats=["html"])


def test_pandoc_fallback_uses_typst_for_the_pdf(tmp_path, monkeypatch):
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": "pandoc", "typst": "typst"})
    seen = []

    def run(cmd, cwd, check, capture_output, timeout):
        seen.append(cmd)
        target = Path(cwd) / cmd[cmd.index("-o") + 1]
        _docx(target) if target.suffix == ".docx" else target.write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    r = md.render_manuscript_document(wb, tmp_path / "doc")
    assert r["renderer"] == "pandoc" and set(r["files"]) == {"docx", "pdf"}
    assert seen[1][-2:] == ["--pdf-engine", "typst"]


def test_quarto_path_from_environment(monkeypatch):
    monkeypatch.setenv("QUARTO_PATH", "/opt/q/bin/quarto")
    assert md.find_renderer()["quarto"] == "/opt/q/bin/quarto"


def test_markdown_cells_are_escaped():
    assert md._esc("@ref $x$ <11 | a_b") == r"\@ref \$x\$ \<11 \| a\_b"
    assert md._esc(float("nan")) == "" and md._esc(None) == ""
    assert md._caption(Path("Study_Figure1_Cohort_Flowchart.png"), "Study") == "Figure1 Cohort Flowchart"


def test_scanner_reports_unreadable_and_missing_package(tmp_path, monkeypatch):
    (tmp_path / "bad.xlsx").write_bytes(b"not a zip")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "names.txt").write_text("# private\n\nSpringfield\n", encoding="utf-8")
    assert rp.read_names_file(tmp_path / "names.txt") == ["Springfield"]
    real_import = __import__

    def no_pypdf(name, *a, **k):
        if name == "pypdf":
            raise ImportError("no pypdf", name="pypdf")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", no_pypdf)
    hits, skipped = rp.scan_files([tmp_path / "bad.xlsx", tmp_path / "doc.pdf", tmp_path / "nowhere.csv"], ["Springfield"])
    assert hits == []
    assert any(s.startswith("bad.xlsx (BadZipFile") for s in skipped) and "doc.pdf (needs pypdf)" in skipped


def test_privacy_names_skip_keys_that_match_labels(tmp_path):
    names = tmp_path / "n.txt"
    names.write_text("Springfield General\n", encoding="utf-8")
    got = _privacy_names(["A", "site", "springfield"], {"A": "Site A", "site": "Site B", "springfield": "Site C"}, names)
    assert got == ["Springfield General", "springfield"]


def test_relabel_errors_and_options(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _workbook(run / "Study_Manuscript_Tables.xlsx")
    (run / "Study_run.json").write_text(json.dumps({"labels": ["Site A", "Site B"], "window": ["2017-01-01", "2022-09-30"]}), encoding="utf-8")
    (run / "Study_LOSO.csv").write_text("site,auc\nSite A,0.7\nSite B,0.6\n", encoding="utf-8")
    pd.DataFrame({"x": [1]}).to_parquet(run / "stray.parquet")
    (run / "Study_Manuscript.docx").write_bytes(b"old")
    old = {"north": "Site A", "south": "Site B"}
    with pytest.raises(ValueError, match="must differ"):
        relabel_outputs(run, run, old_labels=old, new_labels="ordered")
    with pytest.raises(ValueError, match="bare letter"):
        label_translation({"n": "A", "s": "B"}, {"n": "Site B", "s": "Site A"})
    r = relabel_outputs(run, tmp_path / "v2", old_labels=old, new_labels={"north": "Site B", "south": "Site A"}, document=False, privacy_check=False)
    assert r["translation"] == {"Site A": "Site B", "Site B": "Site A"} and r["prefix"] == "Study"
    assert set(r["skipped"]) == {"stray.parquet", "Study_Manuscript.docx"}  # not rebuilt when document=False, so not copied stale
    t5 = pd.read_excel(tmp_path / "v2" / "Study_Manuscript_Tables.xlsx", sheet_name="Table 5 - Cross-Site LOSO", header=2)
    assert t5.iloc[:, 0].tolist() == ["Site B", "Site A"]
    assert (tmp_path / "v2" / "Study_LOSO.csv").read_text(encoding="utf-8") == "site,auc\nSite B,0.7\nSite A,0.6\n"
    # a real name in a text output fails the relabel, and the error names files only
    (run / "notes.md").write_text("from Springfield General", encoding="utf-8")
    with pytest.raises(RunPrivacyError) as e:
        relabel_outputs(run, tmp_path / "v3", old_labels=old, new_labels="random", label_seed=1, site_names=["Springfield General"], document=False)
    assert "notes.md" in str(e.value) and "Springfield" not in str(e.value)
    (run / "Other_run.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="pass prefix"):
        relabel_outputs(run, tmp_path / "v4", old_labels=old, new_labels="ordered")


def test_run_study_refuses_private_dir_equal_to_out(tmp_path):
    with pytest.raises(ValueError, match="private_dir must not be out_dir"):
        run_study(tmp_path, tmp_path / "o", sites=["north"], private_dir=tmp_path / "o")


def test_reuse_moves_old_site_files_out_of_the_shared_folder(tmp_path, monkeypatch):
    from test_multisite_anonymous import KEYS, _study

    db, keys = _study(tmp_path, monkeypatch, 3)
    out = tmp_path / "out"
    kw = dict(sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False)
    run_study(db, out, **kw)
    priv = tmp_path / "out_private"
    for p in priv.glob("*"):  # put the files where 0.24.0 left them
        p.rename(out / p.name)
    r = run_study(db, out, reuse_features=True, site_labels={k: f"Site {c}" for k, c in zip(keys, "CAB")}, **kw)
    assert not list(out.glob("features_*")) and len(list(priv.glob("features_*"))) == 3 and len(list(priv.glob("cohort_*"))) == 3
    assert set(r["data"].site_anon) == {"Site A", "Site B", "Site C"} and KEYS[0] in r["data"].site_source.tolist()


def test_pdf_scan_reads_attachments_outline_annotations_and_xmp(tmp_path):
    from pypdf import PdfWriter
    from pypdf.annotations import Text

    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.add_attachment("Capital City extract.csv", b"x")
    w.add_outline_item("Ogdenville section", 0)
    w.add_annotation(0, Text(rect=(10, 10, 20, 20), text="note on Brockway"))
    w.add_metadata({"/Author": "Shelbyville"})
    w.xmp_metadata = None
    with open(tmp_path / "m.pdf", "wb") as fh:
        w.write(fh)
    text = rp.pdf_text(tmp_path / "m.pdf")
    for n in ("Capital City", "Ogdenville", "Brockway", "Shelbyville"):
        assert n in text, n
