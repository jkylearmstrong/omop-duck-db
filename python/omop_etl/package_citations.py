"""Software citations for the study manuscript, built with grateful_py (https://github.com/jkylearmstrong/grateful_py).

:func:`software_citations` returns the paragraph the manuscript's *Software* section prints and writes the BibTeX file its *References*
list is built from. The omop-duck-db version is the installed package's version at run time (``importlib.metadata``, falling back to
``omop_etl.__version__`` in a source checkout), never a hard-coded number, so the document names the version that produced the numbers.

Only package metadata goes into the paragraph and the ``.bib`` file (name, version, authors, homepage, summary): no paths, hosts or site
information, so both pass the run's privacy scan unchanged.
"""
from __future__ import annotations

import dataclasses
import re
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["CITED_PACKAGES", "omop_duck_db_version", "software_citations"]

#: Import names of the libraries a study run uses, in addition to omop-duck-db itself. Those not installed are left out.
CITED_PACKAGES: tuple[str, ...] = ("duckdb", "pandas", "numpy", "pyarrow", "sklearn", "scipy", "statsmodels", "xgboost", "shap", "matplotlib")

_SELF = "omop-duck-db"


def _canon(name: str) -> str:
    return name.lower().replace("_", "-")


_GROUP = re.compile(r"\b(foundation|team|developers|contributors|community|project|inc|ltd|organization)\b|et al", re.I)


def _bib_author(c: Any) -> str:
    """The author field as BibTeX needs it: a group name (``Python Software Foundation``) braced so it is not split into first / last
    name, several people joined with ``and``, and a package with no author in its metadata credited to its developers."""
    a = (c.author or "").strip()
    if not a or a.lower() == "unknown":
        return f"{{The {c.name} developers}}"
    if _GROUP.search(a):
        return "{" + a + "}"
    return " and ".join(x.strip() for x in a.split(",") if x.strip()) if "," in a and " and " not in a else a


def omop_duck_db_version() -> str:
    """The installed omop-duck-db version (``importlib.metadata``), or ``omop_etl.__version__`` when running from a source checkout."""
    try:
        return metadata.version(_SELF)
    except metadata.PackageNotFoundError:
        import omop_etl

        return str(getattr(omop_etl, "__version__", "unknown"))


def software_citations(bib_path: str | Path, *, packages: Sequence[str] = CITED_PACKAGES,
                       versions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Cite omop-duck-db, Python and the study's libraries with grateful_py; write the BibTeX file to ``bib_path``.

    ``versions`` (optional, e.g. the ``environment`` block of ``<prefix>_run.json``) overrides the installed versions, so a document
    re-rendered later (after relabelling) still names the versions that produced the run. Keys are distribution names
    (``"omop-duck-db"``, ``"scikit-learn"``) or ``"python"``; empty values are ignored.

    Returns ``{"paragraph": str (Quarto markdown with [@key] citations), "bib": Path, "citekeys": [...], "omop_duck_db_version": str,
    "grateful_py_version": str}``. Raises ``ImportError`` when grateful_py is not installed.
    """
    import grateful_py as gp
    from grateful_py.citation.bibtex import write_bibtex_file
    from grateful_py.citation.paragraph import build_paragraph
    from grateful_py.models import PackageCitation

    over = {_canon(k): str(v) for k, v in (versions or {}).items() if v}
    cites = gp.get_pkgs_info(pkgs=["omop_duck_db", *packages, "grateful_py"], omit="none", skip_missing=True, include_interpreter=True)
    if not any(_canon(c.name) == _SELF for c in cites):  # source checkout: omop-duck-db is importable but not an installed distribution
        cites.append(PackageCitation(name=_SELF, version=omop_duck_db_version(), author="J Kyle Armstrong", citekey="omopduckdb",
                                     summary="Tools for building an OMOP CDM v5.4 DuckDB database and mapping PCORnet-format source data into it.",
                                     homepage="https://github.com/jkylearmstrong/omop-duck-db"))
    # author_email is not printed by grateful_py's BibTeX writer; drop it anyway so no address is carried in the returned objects
    cites = [dataclasses.replace(c, version=over.get(_canon(c.name), c.version), author_email=None) for c in cites]
    cites = [dataclasses.replace(c, author=_bib_author(c)) for c in cites]
    # the reference list prints the title but not BibTeX's version field, so the version goes in the title too
    bib = write_bibtex_file([dataclasses.replace(c, name=f"{{{c.name}}} (version {c.version})") for c in cites], bib_path)

    self_c = next(c for c in cites if _canon(c.name) == _SELF)
    grate_c = next((c for c in cites if _canon(c.name) == "grateful-py"), None)
    others = [c for c in cites if c is not self_c and c is not grate_c]
    py = next((c for c in others if c.citekey == "python"), None)
    lead = (f"The study was run with omop-duck-db version {self_c.version} [@{self_c.citekey}], which builds the OMOP Common Data Model "
            f"in DuckDB, runs the cohort, feature and model pipeline at each site, and writes this document.")
    body = build_paragraph(others, include_interpreter=py is not None)
    tail = (f"Software citations were generated with grateful_py version {grate_c.version} [@{grate_c.citekey}]." if grate_c else "")
    paragraph = " ".join(x for x in (lead, body, tail) if x)
    return {"paragraph": paragraph, "bib": Path(bib), "citekeys": [c.citekey for c in cites], "omop_duck_db_version": self_c.version,
            "grateful_py_version": grate_c.version if grate_c else None}
