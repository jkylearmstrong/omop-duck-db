"""Renderer paths of the manuscript document, the privacy scanner's edge cases and relabel errors (no Quarto needed: a stand-in renderer)."""
from __future__ import annotations

import json
import os
import re
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

    def run(cmd, cwd, check, capture_output, timeout, env=None):
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
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[fig_dir / "Study_Figure_Flow.png"], author="Study team", window=("2017-01-01", "2022-09-30"),
                                      profile="tables_only")  # the tables-only view lists every sheet, empty ones too
    assert r["renderer"] == "quarto" and set(r["files"]) == {"docx", "pdf"} and r["missing"] == [] and r["note"] == ""
    assert [c[3:5] for c in calls] == [["--to", "docx"], ["--to", "typst"]]
    assert (tmp_path / "doc" / "Study_Figure_Flow.png").exists() and r["copied"]
    assert not (tmp_path / "doc" / "Study_Manuscript_files").exists() and not (tmp_path / "doc" / "Study_Manuscript.typ").exists()
    with zipfile.ZipFile(r["files"]["docx"]) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "someone.local" not in core and "<dc:creator>Study team</dc:creator>" in core
    qmd = r["qmd"].read_text(encoding="utf-8")
    assert '  - name: "Study team"' in qmd and "_(empty)_" in qmd and "Manifest" not in qmd and 'fig-alt="Figure Flow"' in qmd


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

    def run(cmd, cwd, check, capture_output, timeout, env=None):
        seen.append(cmd)
        target = Path(cwd) / cmd[cmd.index("-o") + 1]
        _docx(target) if target.suffix == ".docx" else target.write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    r = md.render_manuscript_document(wb, tmp_path / "doc")
    assert r["renderer"] == "pandoc" and set(r["files"]) == {"docx", "pdf"}
    assert seen[1][seen[1].index("--pdf-engine") + 1] == "typst" and "margin=(x: 1in, y: 1in)" in seen[1]


# Quarto 1.10's output when %LOCALAPPDATA%\quarto is read only: the cause comes first, then a Deno stack ending in core/cri/cri.ts.
_QUARTO_DENIED = ("\x1b[31mERROR: PermissionDenied: Access is denied. (os error 5): mkdir 'C:\\Users\\{user}\\AppData\\Local\\quarto\\typst'\x1b[39m\n\n"
                  "Stack trace:\n" + "".join(f"    at fn{i} (file:///C:/q/bin/quarto.js:{i}:1)\n" for i in range(30))
                  + "    at async Object.run (file:///C:/q/src/core/cri/cri.ts:88:5)\n")


def test_failure_note_keeps_the_start_of_the_error_and_hides_the_account(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "jdoe42")
    monkeypatch.setenv("OMOP_QUARTO_CACHE_DIR", str(tmp_path / "cache"))  # configured, so no automatic retry
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    err = _QUARTO_DENIED.format(user="jdoe42").encode()

    def fail(fmt, target):
        raise subprocess.CalledProcessError(1, "quarto", stderr=err)

    _fake_quarto(tmp_path, monkeypatch, fail)
    r = md.render_manuscript_document(wb, tmp_path / "doc", formats=["docx"])
    note = r["note"]
    assert note.startswith("docx: quarto failed (exit 1): ERROR: PermissionDenied: Access is denied. (os error 5)")
    assert "cri.ts" in note and "more line(s)" in note and "\x1b" not in note
    assert "jdoe42" not in note and "<user>" in note and "OMOP_QUARTO_CACHE_DIR" in note
    assert len(note) < 1500 and r["cache"] == "configured"


def test_stderr_summary_redacts_home_and_given_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "pat"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home" / "pat"))
    s = md.stderr_summary(f"ERROR: cannot open {tmp_path / 'home' / 'pat' / 'x.qmd'}\nin {tmp_path / 'out'}", redact=[tmp_path / "out"])
    assert str(tmp_path) not in s and "~" in s and "<dir>" in s
    assert md.stderr_summary(b"") == "no error output"
    short = md.stderr_summary("\n".join(f"line {i}" for i in range(5)))
    assert short == "line 0 / line 1 / line 2 / line 3 / line 4"


def test_permission_error_is_retried_once_with_a_temporary_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("OMOP_QUARTO_CACHE_DIR", raising=False)
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": "quarto", "pandoc": None, "typst": None})
    envs = []

    def run(cmd, cwd, check, capture_output, timeout, env=None):
        envs.append(env)
        if env is None:  # the user's own cache folder is read only
            raise subprocess.CalledProcessError(1, cmd, stderr=_QUARTO_DENIED.format(user="x").encode())
        assert Path(env["LOCALAPPDATA"]).is_dir() and Path(env["APPDATA"]).is_dir() and Path(env["XDG_CACHE_HOME"]).is_dir()
        target = Path(cwd) / cmd[cmd.index("--output") + 1]
        _docx(target) if target.suffix == ".docx" else target.write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    r = md.render_manuscript_document(wb, tmp_path / "doc")
    assert set(r["files"]) == {"docx", "pdf"} and r["note"] == "" and r["cache"].startswith("temporary")
    assert envs[0] is None and envs[1] is not None and envs[2] is envs[1]  # one retry, then the writable cache is kept
    assert str(tmp_path / "doc") in envs[1]["LOCALAPPDATA"] and not (tmp_path / "doc" / ".quarto_cache").exists()


def test_configured_cache_dir_is_passed_to_quarto(tmp_path, monkeypatch):
    monkeypatch.delenv("OMOP_QUARTO_CACHE_DIR", raising=False)
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": "quarto", "pandoc": None, "typst": None})
    envs = []

    def run(cmd, cwd, check, capture_output, timeout, env=None):
        envs.append(env)
        _docx(Path(cwd) / cmd[cmd.index("--output") + 1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    r = md.render_manuscript_document(wb, tmp_path / "doc", formats=["docx"], cache_dir=tmp_path / "qcache")
    assert r["cache"] == "configured" and envs[0]["LOCALAPPDATA"] == str((tmp_path / "qcache" / "local").resolve())
    assert envs[0]["DENO_DIR"].startswith(str((tmp_path / "qcache").resolve())) and "PATH" in envs[0]
    monkeypatch.setenv("OMOP_QUARTO_CACHE_DIR", str(tmp_path / "envcache"))
    assert md.renderer_env()["APPDATA"] == str((tmp_path / "envcache" / "roaming").resolve())
    monkeypatch.delenv("OMOP_QUARTO_CACHE_DIR")
    assert md.renderer_env() is None


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
    for p in priv.glob("*"):  # put the files where 0.24.0 left them (it wrote no .meta.json or run_private.json)
        if p.suffix in (".parquet", ".csv"):
            p.rename(out / p.name)
    r = run_study(db, out, reuse_features=True, site_labels={k: f"Site {c}" for k, c in zip(keys, "CAB")}, **kw)
    assert not list(out.glob("features_*")) and len(list(priv.glob("features_*.parquet"))) == 3 and len(list(priv.glob("cohort_*"))) == 3
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


def _png(text_chunks=(), pixels=b"\x00\x01zzBrockwayzz\x00"):
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    for kind, body in text_chunks:
        out += chunk(kind, body)
    return out + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def test_image_bytes_inside_a_docx_are_not_a_hit_but_png_text_chunks_are(tmp_path):
    """Regression: a short site name matched by chance inside a compressed figure in the DOCX and withheld a real manuscript."""
    import zlib

    clean = tmp_path / "clean"
    clean.mkdir()
    with zipfile.ZipFile(clean / "Study_Manuscript.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Site A</w:t></w:r></w:p></w:body></w:document>")
        z.writestr("word/media/image1.png", _png())
        z.writestr("word/fonts/font1.odttf", b"\x00\x00Brockway")
        z.writestr("word/embeddings/blob.dat", b"\x00binary Brockway")
    (clean / "Study_Figure.png").write_bytes(_png())
    hits, skipped = rp.scan(clean, ["Brockway"])
    assert hits == [] and skipped == ["Study_Figure.png"]
    assert rp.scan(clean, ["Site A"])[0]  # the document text is still scanned

    tagged = tmp_path / "tagged"
    tagged.mkdir()
    chunks = [(b"tEXt", b"Author\x00Ogdenville"), (b"zTXt", b"Title\x00\x00" + zlib.compress(b"Capital City")),
              (b"iTXt", b"Description\x00\x00\x00\x00\x00Shelbyville")]
    with zipfile.ZipFile(tagged / "m.docx", "w") as z:
        z.writestr("word/media/image1.png", _png(chunks))
    (tagged / "f.png").write_bytes(_png(chunks))
    got = {(h[0], h[1], h[2]) for h in rp.scan(tagged, ["Ogdenville", "Capital City", "Shelbyville"])[0]}
    assert {("m.docx", i, "part word/media/image1.png") for i in (1, 2, 3)} <= got
    assert {("f.png", i, "PNG text chunk") for i in (1, 2, 3)} <= got
    assert rp.png_text(b"not a png") == ""


def test_withheld_manuscript_is_recorded_as_withheld(tmp_path, monkeypatch):
    from test_multisite_anonymous import NAMES, _study

    db, keys = _study(tmp_path, monkeypatch, 3)
    with pytest.raises(RunPrivacyError):
        run_study(db, tmp_path / "o", sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None,
                  document_title=f"{NAMES[0]} study", site_names=NAMES[:3])
    doc = json.loads((tmp_path / "o" / "Study_run.json").read_text(encoding="utf-8"))["document"]
    assert doc["withheld"] is True and doc["files"] == [] and doc["missing"] == ["docx", "pdf"] and "withheld" in doc["note"]
    assert "Study_Manuscript.qmd" in doc["rendered"]


def test_two_runs_on_the_same_data_are_identical_and_record_their_environment(tmp_path, monkeypatch):
    """The model grid is seeded: two runs give the same workbook cell for cell. A difference between machines comes from library versions,
    which the run record now lists."""
    from test_multisite_anonymous import _study

    db, keys = _study(tmp_path, _KeepGrid(monkeypatch), 3)  # the real model grid (and XGBoost when installed)
    wbs = []
    for i in range(2):
        r = run_study(db, tmp_path / f"o{i}", sites=keys, n_bootstraps=5, loso=True, figures=False, profile=None, document=False)
        wbs.append(r["workbook"])
    a, b = (pd.read_excel(w, sheet_name=None, header=None) for w in wbs)
    assert a.keys() == b.keys()
    for k in a:
        pd.testing.assert_frame_equal(a[k], b[k], obj=k)
    env = json.loads((tmp_path / "o0" / "Study_run.json").read_text(encoding="utf-8"))["environment"]
    assert env["python"] and env["scikit-learn"] and "xgboost" in env
    assert env["platform"] and env["cpu_count"] and env["container"] in ("yes", "no") and "xgboost_build" in env and "thread_pools" in env
    assert not any(os.sep + "home" in str(v) or ":\\" in str(v) for v in env.values())  # no file paths in the shareable record


def test_model_tables_do_not_depend_on_sites_order_or_labels(tmp_path, monkeypatch):
    """The folds are drawn by row position, so the pooled rows are put in an order fixed by the data. A real five-site run with --sites
    in another order changed 118 model cells before. A new mapping may move rows of the by-site tables, but no model number."""
    from test_multisite_anonymous import _study

    db, keys = _study(tmp_path, _KeepGrid(monkeypatch), 3)
    lab = {k: f"Site {c}" for k, c in zip(keys, "ABC")}
    kw = dict(n_bootstraps=5, loso=True, figures=False, profile=None, document=False)
    a = run_study(db, tmp_path / "o0", sites=keys, site_labels=lab, **kw)
    b = run_study(db, tmp_path / "o1", sites=list(reversed(keys)), site_labels=lab, **kw)
    c = run_study(db, tmp_path / "o2", sites=keys, site_labels={k: f"Site {x}" for k, x in zip(keys, "CAB")}, **kw)
    A, B, C = (pd.read_excel(r["workbook"], sheet_name=None, header=None) for r in (a, b, c))
    for k in A:
        pd.testing.assert_frame_equal(A[k], B[k], obj=k)
    for k in A:
        if k.startswith(("Table 2", "Table 3", "Table 4", "Table S4")):
            pd.testing.assert_frame_equal(A[k], C[k], obj=k)
    pd.testing.assert_frame_equal(a["data"], b["data"])


class _KeepGrid:
    """Applies the study stand-ins for cohort and features but keeps the real model grid."""

    def __init__(self, mp):
        self.mp = mp

    def setattr(self, obj, name, value):
        if name != "default_model_grid":
            self.mp.setattr(obj, name, value)


def test_run_environment_without_xgboost_or_threadpoolctl(monkeypatch):
    """The platform details never fail a run: without XGBoost or threadpoolctl those two keys are None."""
    from omop_etl.study_run import run_environment

    monkeypatch.setitem(sys.modules, "xgboost", None)
    monkeypatch.setitem(sys.modules, "threadpoolctl", None)
    env = run_environment()
    assert env["xgboost_build"] is None and env["thread_pools"] is None
    assert env["platform"] and env["container"] in ("yes", "no")


def test_run_environment_reports_builds_without_paths(monkeypatch):
    """The XGBoost build and the thread pools are summarised without file paths (a fake build stands in for any installed one)."""
    import types

    from omop_etl.study_run import run_environment

    xgb = types.ModuleType("xgboost")
    xgb.build_info = lambda: {"GCC_VERSION": [10, 3, 1], "USE_OPENMP": True, "USE_CUDA": False}
    tp = types.ModuleType("threadpoolctl")
    tp.threadpool_info = lambda: [{"internal_api": "openblas", "version": "0.3.30", "num_threads": 8, "filepath": "/home/someone/lib.so"}]
    monkeypatch.setitem(sys.modules, "xgboost", xgb)
    monkeypatch.setitem(sys.modules, "threadpoolctl", tp)
    env = run_environment()
    assert env["xgboost_build"] == "GCC_VERSION=10.3.1, USE_OPENMP=True"
    assert env["thread_pools"] == "openblas 0.3.30 (8 threads)"
    assert "someone" not in json.dumps(env)

def test_manuscript_cites_omop_duck_db_with_its_installed_version_through_grateful_py(tmp_path, monkeypatch):
    pytest.importorskip("grateful_py")
    import omop_etl
    from omop_etl.package_citations import omop_duck_db_version, software_citations

    assert omop_duck_db_version() == omop_etl.__version__  # read from the installed package (or the checkout), never typed into the text
    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")
    _fake_quarto(tmp_path, monkeypatch, lambda fmt, target: _docx(target) if fmt == "docx" else target.write_bytes(b"%PDF"))
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[])
    v = omop_duck_db_version()
    qmd = r["qmd"].read_text(encoding="utf-8")
    assert r["software"] == v and r["bib"] == tmp_path / "doc" / "Study_Manuscript.bib" and r["bib"].exists()
    assert "bibliography: Study_Manuscript.bib" in qmd.split("---")[1]  # in the YAML header
    software = re.search(r"^## \d+ Software$", qmd, re.M)  # numbered as in the delivered manuscript
    assert software and f"omop-duck-db version {v} [@omopduckdb]" in qmd and "[@python]" in qmd and "grateful_py version" in qmd
    assert software.start() < qmd.index("## Supplementary Material") and qmd.rstrip().endswith("::: {#refs}\n:::")
    bib = r["bib"].read_text(encoding="utf-8")
    assert "@software{omopduckdb," in bib and f"version = {{{v}}}" in bib and "@software{gratefulpy," in bib and "@" not in bib.replace("@software{", "").replace("@article{", "").replace("@inproceedings{", "")
    assert not rp.scan_files([r["bib"]], ["Some Health System"])[1]  # the privacy scan reads the .bib as text

    # versions recorded with the run win over the installed ones (a relabelled document cites what made the numbers)
    s = software_citations(tmp_path / "x.bib", versions={"omop-duck-db": "0.0.1", "python": "3.13.17", "scikit-learn": None, "platform": "Linux"})
    assert "omop-duck-db version 0.0.1 [@omopduckdb]" in s["paragraph"] and "Python version 3.13.17 [@python]" in s["paragraph"]

    r = md.render_manuscript_document(wb, tmp_path / "plain", figures=[], cite_software=False)
    assert r["bib"] is None and not re.search(r"^## \d* ?Software$", r["qmd"].read_text(encoding="utf-8"), re.M) and not (tmp_path / "plain" / "Study_Manuscript.bib").exists()


def test_manuscript_without_grateful_py_says_so_and_pandoc_gets_citeproc(tmp_path, monkeypatch):
    import omop_etl.package_citations as sc

    wb = _workbook(tmp_path / "Study_Manuscript_Tables.xlsx")

    def no_grateful(*a, **k):
        raise ImportError("No module named 'grateful_py'")

    monkeypatch.setattr(sc, "software_citations", no_grateful)
    _fake_quarto(tmp_path, monkeypatch, lambda fmt, target: _docx(target) if fmt == "docx" else target.write_bytes(b"%PDF"))
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[])
    assert r["bib"] is None and set(r["files"]) == {"docx", "pdf"} and "grateful_py is not installed" in r["note"]
    monkeypatch.undo()

    pytest.importorskip("grateful_py")
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": "pandoc", "typst": None})
    seen = []

    def run(cmd, cwd, check, capture_output, timeout, env=None):
        seen.append(cmd)
        _docx(Path(cwd) / cmd[cmd.index("-o") + 1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(md.subprocess, "run", run)
    r = md.render_manuscript_document(wb, tmp_path / "doc2", figures=[], formats=["docx"])
    assert "--citeproc" in seen[0] and r["bib"].exists()


def test_source_checkout_without_installed_metadata_still_cites_omop_duck_db(tmp_path, monkeypatch):
    """Run from a git checkout (omop_etl importable, omop-duck-db not pip-installed): the version falls back to omop_etl.__version__."""
    gp = pytest.importorskip("grateful_py")
    import importlib.metadata as im

    import omop_etl
    import omop_etl.package_citations as pc

    real_version, real_get = im.version, gp.get_pkgs_info

    def version(name):
        if name == "omop-duck-db":
            raise im.PackageNotFoundError(name)
        return real_version(name)

    found = [c for c in real_get(pkgs=["omop_duck_db", "pandas"], omit="none", skip_missing=True, include_interpreter=True)
             if c.name.lower().replace("_", "-") != "omop-duck-db"]  # what grateful_py resolves when omop-duck-db is not installed
    monkeypatch.setattr(pc.metadata, "version", version)
    monkeypatch.setattr(gp, "get_pkgs_info", lambda **k: list(found))
    assert pc.omop_duck_db_version() == omop_etl.__version__
    s = pc.software_citations(tmp_path / "x.bib", packages=("pandas",))
    assert f"omop-duck-db version {omop_etl.__version__} [@omopduckdb]" in s["paragraph"] and s["omop_duck_db_version"] == omop_etl.__version__
    assert "@software{omopduckdb," in (tmp_path / "x.bib").read_text(encoding="utf-8")
