"""End to end: an operating site runs the study with any number of sister sites, under anonymous labels, and re-issues it with new ones.

Synthetic data only. Site keys here are made-up place names, so the privacy scan has real-looking names to look for.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import study_report as sr  # noqa: E402
from omop_etl.manuscript_document import find_renderer  # noqa: E402
from omop_etl.run_privacy import docx_text, pdf_text, scan  # noqa: E402
from omop_etl.study_relabel import relabel_outputs, relabel_text  # noqa: E402
from omop_etl.study_run import RunPrivacyError, assign_site_labels, load_site_labels, run_study, validate_site_labels  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402

NAMES = ["Springfield General", "Shelbyville Medical", "Capital City Health", "Ogdenville Clinic", "North Haverbrook Hospital",
         "Brockway Health", "Cypress Creek Medical", "Springfield Heights Clinic"]
KEYS = ["springfield", "shelbyville", "capitalcity", "ogdenville", "haverbrook", "brockway", "cypresscreek", "springheights"]
TOOLS = find_renderer()
CAN_DOCX = bool(TOOLS["quarto"] or TOOLS["pandoc"])
CAN_PDF = bool(TOOLS["quarto"] or (TOOLS["pandoc"] and TOOLS["typst"]))


def _study(tmp_path, monkeypatch, k):
    """k site databases and stand-ins for the cohort and feature builders, keyed by the (fake) real site names."""
    import duckdb
    from sklearn.linear_model import LogisticRegression

    import omop_etl.benchmark_features as bf
    import omop_etl.cohort as coh

    keys = KEYS[:k]
    feats = make_features(n=240 * k, seed=k)
    feats["site_key"] = [keys[i % k] for i in range(len(feats))]
    db = tmp_path / "db"
    db.mkdir()
    for s in keys:
        part = feats[feats.site_key == s]
        con = duckdb.connect(str(db / f"omop_{s}.duckdb"))
        con.register("p", pd.DataFrame({"visit_occurrence_id": part.visit_occurrence_id, "person_id": part.subject_id,
                                        "discharged_to_source_value": "IP", "discharged_to_concept_id": 0}))
        con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM p")
        con.close()
    by_id = feats.set_index("visit_occurrence_id")

    def fake_cohort(con, **kw):
        v = con.execute("SELECT visit_occurrence_id, person_id FROM visit_occurrence").df()
        out = pd.DataFrame({"subject_id": v.person_id, "visit_occurrence_id": v.visit_occurrence_id,
                            "outcome_flag": by_id.loc[v.visit_occurrence_id, "readmitted_30d"].to_numpy(),
                            "cohort_start_date": "2018-01-01", "cohort_end_date": "2018-01-05"})
        out.attrs["attrition"] = [{"step_name": "Index", "subjects_retained": len(out) + 5}, {"step_name": "Final", "subjects_retained": len(out)}]
        return out

    def fake_features(con, table, **kw):
        ids = con.execute(f"SELECT visit_occurrence_id FROM {table}").df().visit_occurrence_id
        cols = [c for c in feats.columns if c not in ("subject_id", "readmitted_30d", "site_anon", "site_key", "index_los_days", "visit_occurrence_id")]
        return by_id.loc[ids, cols].reset_index()

    monkeypatch.setattr(coh, "define_study_cohort", fake_cohort)
    monkeypatch.setattr(bf, "extract_benchmark_features", fake_features)
    monkeypatch.setattr(sr, "default_model_grid",
                        lambda *a, **kw: {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    return db, keys


def _all_text(path: Path) -> str:
    """Every string of a workbook / docx / pdf / text file, for comparing two runs."""
    suf = path.suffix.lower()
    if suf == ".xlsx":
        return "\n".join(t.astype(str).to_csv(index=False) for t in pd.read_excel(path, sheet_name=None, header=None).values())
    if suf == ".docx":
        import docx

        d = docx.Document(str(path))
        return "\n".join([p.text for p in d.paragraphs] + [c.text for t in d.tables for r in t.rows for c in r.cells])
    if suf == ".pdf":
        return pdf_text(path)
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("k", [3, 5, 8])
def test_any_number_of_sites_gives_an_anonymous_shareable_folder(tmp_path, monkeypatch, k):
    db, keys = _study(tmp_path, monkeypatch, k)
    out, priv = tmp_path / "out", tmp_path / "private"
    labels_file = priv / "labels.json"
    priv.mkdir()
    r = run_study(db, out, sites=keys, site_labels="random", label_seed=k, labels_out=labels_file, n_bootstraps=3, loso=True,
                  figures=True, profile=None, private_dir=priv, site_names=NAMES[:k])
    labels = load_site_labels(labels_file)
    assert sorted(labels.values()) == [f"Site {c}" for c in "ABCDEFGH"[:k]]
    # the shareable folder names no site anywhere (file names, CSV, JSON, workbook, DOCX, PDF), and the run said so
    hits, _ = scan(out, NAMES[:k] + keys)
    assert hits == []
    assert r["privacy_check"]["checked"] and r["privacy_check"]["hits"] == 0
    assert not list(out.glob("features_*")) and not list(out.glob("cohort_*")) and not list(out.glob("attrition_*"))
    assert {f"features_{s}.parquet" for s in keys} <= {p.name for p in priv.iterdir()}
    # every site is in the leave-one-site-out table under its own letter
    t5 = pd.read_excel(r["workbook"], sheet_name="Table 5 - Cross-Site LOSO", header=2)
    assert set(labels.values()) <= set(t5.iloc[:, 0].astype(str))
    run = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    assert run["n_sites"] == k and run["document"]["requested"]
    assert (out / "Study_Manuscript.qmd").exists()
    if CAN_DOCX:
        doc = _all_text(out / "Study_Manuscript.docx")
        assert set(re.findall(r"(?<![-A-Za-z])Site [A-Z]{1,2}\b", doc)) == set(labels.values())
        assert not any(n.lower() in doc.lower() for n in NAMES + KEYS)
    else:
        assert "docx" in run["document"]["missing"]
    if CAN_PDF:
        assert (out / "Study_Manuscript.pdf").exists()
        pdf = pdf_text(out / "Study_Manuscript.pdf")
        assert "Site A" in pdf and not any(n.lower() in pdf.lower() for n in NAMES + KEYS)


def test_a_site_without_a_label_is_refused_not_shown_under_its_name(tmp_path, monkeypatch, capsys):
    import reproduce_study

    db, keys = _study(tmp_path, monkeypatch, 6)
    five = assign_site_labels(keys[:5], mode="ordered")
    with pytest.raises(ValueError, match="1 of 6 site"):
        run_study(db, tmp_path / "o", sites=keys, site_labels=five, n_bootstraps=3, loso=False, figures=False, profile=None)
    with pytest.raises(SystemExit):  # six sites, five labels: used to pair them with zip and leave the sixth under its real name
        reproduce_study.main(["--db-dir", str(db), "--out", str(tmp_path / "o"), "--sites", *keys, "--labels", *[f"Site {c}" for c in "ABCDE"]])
    assert "5 label(s) for 6 site(s)" in capsys.readouterr().err
    with pytest.raises(ValueError, match="share a label"):
        validate_site_labels(["a", "b"], {"a": "Site A", "b": "Site A"})
    with pytest.raises(ValueError, match="same as a site name"):
        validate_site_labels(["north", "south"], {"north": "south", "south": "Site A"})
    # one consortium-wide file, more sites than this run: fine
    assert validate_site_labels(keys[:2], assign_site_labels(keys, seed=1)).keys() == {keys[0], keys[1]}


def test_a_real_name_in_the_document_fails_the_run_and_keeps_the_document_out(tmp_path, monkeypatch):
    db, keys = _study(tmp_path, monkeypatch, 3)
    out = tmp_path / "out"
    with pytest.raises(RunPrivacyError) as e:
        run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document_title=f"{NAMES[0]} readmission study",
                  site_names=NAMES[:3])
    assert NAMES[0] not in str(e.value) and "Study_Manuscript.qmd" in str(e.value)
    assert not (out / "Study_Manuscript.qmd").exists() and not (out / "Study_Manuscript.docx").exists()
    run = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    assert run["privacy_check"]["hits"] >= 1 and NAMES[0] not in json.dumps(run)


def test_script_exits_non_zero_on_a_privacy_hit_and_no_document_opt_out(tmp_path, monkeypatch, capsys):
    import reproduce_study

    db, keys = _study(tmp_path, monkeypatch, 3)
    names = tmp_path / "names.txt"
    names.write_text("# private\n" + "\n".join(NAMES[:3]) + "\n", encoding="utf-8")
    common = ["--db-dir", str(db), "--sites", *keys, "--bootstraps", "3", "--no-loso", "--no-figures", "--profile", "none", "--names-file", str(names)]
    assert reproduce_study.main([*common, "--out", str(tmp_path / "a"), "--doc-title", f"{NAMES[1]} study"]) == 1
    assert "PRIVACY CHECK FAILED" in capsys.readouterr().err
    assert reproduce_study.main([*common, "--out", str(tmp_path / "b"), "--no-document"]) == 0
    assert "privacy check: 0 hit(s)" in capsys.readouterr().out
    assert not list((tmp_path / "b").glob("Study_Manuscript.*[!x]"))  # only the .xlsx workbook
    assert (tmp_path / "b" / "Study_Manuscript_Tables.xlsx").exists()


def test_relabel_changes_the_letters_and_nothing_else(tmp_path, monkeypatch):
    import relabel_study

    db, keys = _study(tmp_path, monkeypatch, 5)
    out, priv = tmp_path / "out", tmp_path / "private"
    priv.mkdir()
    run_study(db, out, sites=keys, site_labels="random", label_seed=1, labels_out=priv / "v1.json", n_bootstraps=3, loso=True, figures=True,
              profile=None, private_dir=priv / "work")
    assert relabel_study.main(["--run-dir", str(out), "--out", str(tmp_path / "v2"), "--old-labels", str(priv / "v1.json"), "--new-labels", "random",
                               "--label-seed", "2", "--labels-out", str(priv / "v2.json"), "--names", *NAMES[:5]]) == 0
    old, new = load_site_labels(priv / "v1.json"), load_site_labels(priv / "v2.json")
    assert set(old) == set(new) == set(keys) and old != new
    trans = {old[s]: new[s] for s in keys}
    v2 = tmp_path / "v2"
    files = (["Study_Manuscript_Tables.xlsx", "Study_LOSO_By_Site.csv", "Study_summary.json", "Study_Manuscript.qmd"]
             + (["Study_Manuscript.docx"] if CAN_DOCX else []) + (["Study_Manuscript.pdf"] if CAN_PDF else []))
    for name in files:  # the DOCX / PDF / qmd are rebuilt from the relabelled workbook: same text once the letters are mapped
        before, after = _all_text(out / name), _all_text(v2 / name)
        assert relabel_text(before, trans) == after, name
        assert set(re.findall(r"(?<![-A-Za-z])Site [A-Z]{1,2}\b", after)) <= set(new.values())
    run = json.loads((v2 / "Study_run.json").read_text(encoding="utf-8"))
    assert run["relabelled"] and run["labels"] == sorted(new.values()) and set(run["timings_seconds"]) >= {f"site_{v}" for v in new.values()}
    assert scan(v2, NAMES[:5] + keys)[0] == []
    # the old run is untouched, and a mapping that does not match the run is refused
    assert "relabelled" not in json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="every label of the run"):
        relabel_outputs(out, tmp_path / "v3", old_labels={k: v for k, v in list(old.items())[:4]}, new_labels="random")


def test_relabel_is_one_pass_and_whole_word():
    m = {"Site A": "Site B", "Site B": "Site A", "Site C": "Site AA"}
    assert relabel_text("Site A, Site B; Site C; Site CA; site_Site A; Site AB", m) == "Site B, Site A; Site AA; Site CA; site_Site B; Site AB"


def test_privacy_scan_reads_docx_text_split_across_runs_and_pdf(tmp_path):
    import docx

    d = docx.Document()
    p = d.add_paragraph("Results from ")
    p.add_run("Spring")
    p.add_run("field General")  # Word often splits a phrase over runs; the XML alone would not match
    d.save(str(tmp_path / "m.docx"))
    assert "Springfield General" in docx_text(tmp_path / "m.docx")
    hits, _ = scan(tmp_path, ["Springfield General"])
    assert ("m.docx", 1, "document text") in hits

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    fig.text(0.1, 0.5, "AUC by site: Shelbyville Medical 0.71")
    fig.savefig(tmp_path / "f.pdf", metadata={"Author": "Site B"})
    plt.close(fig)
    hits, skipped = scan(tmp_path, ["Springfield General", "Shelbyville Medical"])
    assert ("f.pdf", 2, "PDF text or metadata") in hits and not any("f.pdf" in s for s in skipped)


def test_document_without_any_renderer_keeps_the_qmd_and_says_so(tmp_path, monkeypatch):
    import omop_etl.manuscript_document as md

    db, keys = _study(tmp_path, monkeypatch, 3)
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": None, "typst": None})
    r = run_study(db, tmp_path / "o", sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None)
    assert r["document"]["missing"] == ["docx", "pdf"] and "install Quarto" in r["document"]["note"]
    assert (tmp_path / "o" / "Study_Manuscript.qmd").exists()


@pytest.mark.skipif(not CAN_DOCX, reason="needs Quarto or Pandoc")
def test_docx_properties_name_no_one(tmp_path, monkeypatch):
    db, keys = _study(tmp_path, monkeypatch, 3)
    run_study(db, tmp_path / "o", sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document_formats=["docx"])
    with zipfile.ZipFile(tmp_path / "o" / "Study_Manuscript.docx") as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    for tag in ("dc:creator", "cp:lastModifiedBy"):  # absent or empty: the account that rendered the file is not recorded
        assert not re.search(rf"<{tag}>\s*[^<\s][^<]*</{tag}>", core), tag
