"""Re-issue a finished study run under new anonymous site letters, without re-running the study.

Copies the shareable outputs of --run-dir to --out with every label swapped (workbook cells and sheet names, CSV, JSON, Markdown, HTML,
the run record) and rebuilds the DOCX / PDF manuscript from the relabelled workbook, then scans the new folder for real site names.
Numbers do not change; only the letters do.

    python scripts/relabel_study.py --run-dir out/run --old-labels ../private/labels_v1.json \
        --new-labels random --label-seed 20261114 --labels-out ../private/labels_v2.json \
        --out out/run_v2 --names-file ../private/site_names.txt

Keep both mapping files and the names file outside the repository and away from the shared results.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.study_relabel import relabel_outputs  # noqa: E402
from omop_etl.study_run import RunPrivacyError  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True, help="the output folder of the run to relabel (left unchanged)")
    p.add_argument("--out", required=True, help="new folder for the relabelled outputs")
    p.add_argument("--old-labels", required=True, help="JSON site -> label mapping the run used (from --labels-out)")
    p.add_argument("--new-labels", required=True, help="JSON site -> label mapping to use, or 'random' / 'ordered'")
    p.add_argument("--label-seed", type=int, help="seed for --new-labels random")
    p.add_argument("--labels-out", help="save the new mapping here (keep it private)")
    p.add_argument("--prefix", help="run prefix (default: from the single *_run.json in --run-dir)")
    p.add_argument("--no-document", action="store_true", help="do not rebuild the DOCX / PDF")
    p.add_argument("--document-formats", nargs="+", default=["docx", "pdf"], choices=["docx", "pdf"])
    p.add_argument("--doc-title"); p.add_argument("--doc-author", default="")
    p.add_argument("--names-file", help="text file, one real site name per line: the new folder is scanned for them")
    p.add_argument("--names", nargs="*", default=[])
    p.add_argument("--no-privacy-check", action="store_true")
    a = p.parse_args(argv)
    names = list(a.names)
    if a.names_file:
        from omop_etl.run_privacy import read_names_file

        names += read_names_file(a.names_file)
    if a.new_labels == "random" and not a.labels_out:
        p.error("--new-labels random needs --labels-out, or the new mapping is lost")
    try:
        r = relabel_outputs(a.run_dir, a.out, old_labels=a.old_labels, new_labels=a.new_labels, label_seed=a.label_seed, labels_out=a.labels_out,
                            prefix=a.prefix, document=not a.no_document, document_formats=a.document_formats, document_title=a.doc_title,
                            document_author=a.doc_author, site_names=names, privacy_check=not a.no_privacy_check)
    except RunPrivacyError as e:
        print(f"PRIVACY CHECK FAILED: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"relabelled {len(r['translation'])} site label(s); {len(r['files'])} file(s) written to {a.out}")
    if r["unchanged"]:
        print(f"copied unchanged (images are not relabelled; check they show no site label): {', '.join(r['unchanged'])}")
    if r["skipped"]:
        print(f"not copied: {', '.join(r['skipped'])}")
    d = r["document"]
    if d.get("requested"):
        print(f"manuscript: {', '.join(d.get('files', []))}" + (f"; not rendered: {', '.join(d['missing'])} ({d['note']})" if d.get("missing") else ""))
    pc = r["privacy_check"]
    print(f"privacy check: {pc['hits']} hit(s) for {pc['n_names']} name(s)" if pc.get("checked") else "privacy check: no names to scan for (give --names-file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
