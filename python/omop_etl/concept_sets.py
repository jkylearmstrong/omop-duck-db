"""Feature definitions as OHDSI concept sets, resolved through the vocabulary (v0.29.0).

Labs, vitals and medication classes are defined by concept-set files in ``inst/extdata/concept_sets/<domain>/<name>.json``
(installed as ``omop_etl/resources/extdata/concept_sets``), not by code lists in the code. Each file holds a concept-set
expression in the format ATLAS, Circe and Capr use::

    {"name": "bilirubin_total", "domain": "labs", "expression": {"items": [
        {"concept": {"CONCEPT_ID": 3024128, "CONCEPT_CODE": "1975-2", "VOCABULARY_ID": "LOINC", "CONCEPT_NAME": "..."},
         "includeDescendants": false, "includeMapped": false, "isExcluded": false}, ...]}}

so a set can be pasted into ATLAS, and a set exported from ATLAS can be dropped in. :func:`resolve_concept_set` gives the
concept ids the same way Circe does: each item's concept, plus its descendants in ``concept_ancestor`` when
``includeDescendants``, plus the source concepts that map to those (``Maps to``) when ``includeMapped``, minus the same
for excluded items. An item may give only ``VOCABULARY_ID`` and ``CONCEPT_CODE``; the id is then looked up in ``concept``.

A LOINC lab test has no descendants, so ``includeDescendants`` cannot join the codes of one lab. The codes of one analyte
are linked through their LOINC parts instead (``Has component``, ``Has property``, ``Has scale type``, ``Has time aspect``,
``Has system``). :func:`expand_loinc_concept_set` adds every LOINC test that shares a listed code's component, property,
scale and time aspect and whose system is blood, serum or plasma, so 34543-9 (total bilirubin in blood) joins 1975-2
(serum or plasma). The property is kept, so molar codes (umol/L) never join a mass-unit set (mg/dL).
:func:`loinc_coverage` lists the codes a site's rows use for a lab's analyte that its set does not hold.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb
import pandas as pd

__all__ = ["CONCEPT_SET_VERSION", "CONCEPT_SET_DOMAINS", "concept_set_dir", "list_concept_sets", "load_concept_set",
           "load_concept_sets", "concept_set_manifest", "concept_set_codes", "concept_set_ids", "codes_by_name", "ancestor_specs", "concept_set_from_codes", "concept_set_from_ids", "resolve_concept_set",
           "resolve_concept_sets", "concept_set_fingerprint", "legacy_feature_codes", "concept_set_version_fingerprint", "LEGACY_CONCEPT_SETS", "expand_loinc_concept_set", "EXPANSION_MODES", "loinc_coverage", "write_concept_set", "LOINC_BLOOD_SYSTEM"]


#: Domains with bundled sets: ``labs`` and ``vitals`` (LOINC), ``medications_ederri`` and ``medications`` (ATC / RxNorm).
CONCEPT_SET_DOMAINS = ("labs", "vitals", "medications_ederri", "medications")

#: A LOINC system (specimen) part name that counts as blood, serum or plasma; cord blood, fetal blood, donor units,
#: dried blood spots, red blood cells and mixed urine-and-serum specimens do not.
LOINC_BLOOD_SYSTEM = re.compile(r"serum|plasma|blood|\bbld|\bser\b|\bplas\b", re.I)
_NOT_BLOOD = re.compile(r"cord|fetus|fetal|donor|spot|product|unit|newborn|red blood cell|erythrocyte|\brbc\b|urine|\^", re.I)

_PARTS = {"component": "Has component", "property": "Has property", "scale": "Has scale type", "time": "Has time aspect",
          "system": "Has system"}

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent.parent


def concept_set_dir() -> Path:
    """Folder of the bundled concept sets (installed resources first, then the repository's ``inst``)."""
    packaged = _PACKAGE_DIR / "resources" / "extdata" / "concept_sets"
    return packaged if packaged.is_dir() else _REPO_ROOT / "inst" / "extdata" / "concept_sets"


def concept_set_manifest() -> dict[str, Any]:
    """The bundled ``manifest.json``: ``version``, ``domains`` (``{domain: [name, ...]}`` in feature-column order) and
    ``other`` (sets that are not a feature column of their domain, such as ``labs/egfr``)."""
    return json.loads((concept_set_dir() / "manifest.json").read_text(encoding="utf-8"))


def list_concept_sets(domain: str | None = None) -> list[str]:
    """Names of the bundled sets as ``<domain>/<name>``, in the manifest's column order (one domain when given)."""
    m = concept_set_manifest()
    doms = [domain] if domain else list(m["domains"])
    out = [f"{d}/{n}" for d in doms for n in m["domains"].get(d, [])]
    return out if domain else out + list(m.get("other", []))


def load_concept_set(name: str | os.PathLike) -> dict[str, Any]:
    """One concept set: a bundled ``<domain>/<name>`` or a path to an ATLAS / Circe JSON file.

    Returns ``{"name", "domain", "expression": {"items": [...]}, ...}``; a bare ATLAS expression (``{"items": [...]}``) is
    wrapped."""
    p = Path(name)
    if not p.suffix:
        p = concept_set_dir() / f"{name}.json"
    if not p.is_file():
        raise FileNotFoundError(f"no concept set {str(name)!r} (bundled sets: {', '.join(list_concept_sets()) or 'none'})")
    data = json.loads(p.read_text(encoding="utf-8"))
    if "items" in data and "expression" not in data:
        data = {"name": p.stem, "expression": data}
    data.setdefault("name", p.stem)
    _check_expression(data["expression"], str(name))
    return data


def load_concept_sets(domain: str) -> dict[str, dict[str, Any]]:
    """``{name: concept-set expression}`` for every bundled set of ``domain``, in the manifest's column order."""
    out = {}
    for full in list_concept_sets(domain):
        cs = load_concept_set(full)
        out[cs["name"]] = cs["expression"]
    if not out:
        raise FileNotFoundError(f"no bundled concept sets for domain {domain!r} in {concept_set_dir()}")
    return out


def _check_expression(expr: Mapping[str, Any], where: str) -> None:
    items = expr.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"concept set {where}: 'items' must be a non-empty list")
    for i, it in enumerate(items):
        c = it.get("concept") or {}
        if c.get("CONCEPT_ID") is None and not (c.get("VOCABULARY_ID") and c.get("CONCEPT_CODE")):
            raise ValueError(f"concept set {where}, item {i}: needs CONCEPT_ID or VOCABULARY_ID + CONCEPT_CODE")


def concept_set_codes(expression: Mapping[str, Any], vocabulary: str = "LOINC") -> list[str]:
    """The ``CONCEPT_CODE`` of every included item of ``vocabulary`` (the readable code list behind a set)."""
    return [it["concept"]["CONCEPT_CODE"] for it in expression["items"]
            if not it.get("isExcluded") and it["concept"].get("VOCABULARY_ID") == vocabulary and it["concept"].get("CONCEPT_CODE")]


def concept_set_ids(expression: Mapping[str, Any]) -> list[int]:
    """The ``CONCEPT_ID`` of every included item that has one."""
    return [int(it["concept"]["CONCEPT_ID"]) for it in expression["items"]
            if not it.get("isExcluded") and it["concept"].get("CONCEPT_ID") is not None]


def _item(concept: Mapping[str, Any], descendants: bool = False, mapped: bool = False, excluded: bool = False) -> dict[str, Any]:
    return {"concept": dict(concept), "includeDescendants": bool(descendants), "includeMapped": bool(mapped), "isExcluded": bool(excluded)}


def concept_set_from_codes(codes: Iterable[str], vocabulary: str = "LOINC", *, include_descendants: bool = False) -> dict[str, Any]:
    """An expression of codes of one vocabulary (ids are looked up when it is resolved)."""
    return {"items": [_item({"CONCEPT_CODE": str(c), "VOCABULARY_ID": vocabulary}, include_descendants) for c in codes]}


def concept_set_from_ids(ids: Iterable[int], *, include_descendants: bool = True) -> dict[str, Any]:
    """An expression of concept ids, with their descendants by default (an ATC class, an ingredient, a SNOMED parent)."""
    return {"items": [_item({"CONCEPT_ID": int(i)}, include_descendants) for i in ids]}


def _has(con, table: str) -> bool:
    try:
        return con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except (duckdb.CatalogException, duckdb.BinderException):
        return False


def _items_frame(sets: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    rows = []
    for name, expr in sets.items():
        _check_expression(expr, name)
        for it in expr["items"]:
            c = it.get("concept") or {}
            rows.append({"name": name, "cid": None if c.get("CONCEPT_ID") is None else int(c["CONCEPT_ID"]),
                         "vocab": c.get("VOCABULARY_ID"), "code": None if c.get("CONCEPT_CODE") is None else str(c["CONCEPT_CODE"]),
                         "descendants": bool(it.get("includeDescendants")), "mapped": bool(it.get("includeMapped")),
                         "excluded": bool(it.get("isExcluded"))})
    return pd.DataFrame(rows, columns=["name", "cid", "vocab", "code", "descendants", "mapped", "excluded"]).astype({"cid": "Int64"})


def resolve_concept_sets(con, sets: Mapping[str, Mapping[str, Any]], *, concept_ancestor: str | None = "auto",
                         warn: bool = True) -> pd.DataFrame:
    """``name, concept_id`` rows for several concept sets at once (Circe semantics; see the module docstring).

    ``concept_ancestor``: the table to read descendants from; ``"auto"`` finds one on the connection, ``None`` uses none.
    An item whose code is not in ``concept`` is dropped, and a set that needs descendants when no ``concept_ancestor`` is
    reachable keeps only its listed concepts; both raise a :class:`omop_etl.data_gaps.DataGapWarning` (``vocabulary``)
    when ``warn``. ``attrs["missing"]`` lists ``(name, code)`` of the dropped items."""
    from omop_etl.data_gaps import DataGapWarning

    items = _items_frame(sets)
    if items.empty:
        return pd.DataFrame(columns=["name", "concept_id"])
    has_concept = _has(con, "concept")
    if concept_ancestor == "auto":
        from omop_etl.comorbidity import _resolve_concept_ancestor

        concept_ancestor = _resolve_concept_ancestor(con)[0] if has_concept or _has(con, "concept_ancestor") else None
    con.register("_cs_items", items)
    try:
        # an item with a vocabulary and code is looked up by them (ids can differ between vocabulary builds); its
        # CONCEPT_ID is used when the code is not found but that id is in this vocabulary, and as it is when there is no
        # concept table at all
        lookup = ("LEFT JOIN concept c ON c.vocabulary_id = i.vocab AND c.concept_code = i.code "
                  "LEFT JOIN concept k ON k.concept_id = i.cid"
                  if has_concept else "LEFT JOIN (SELECT NULL::BIGINT AS concept_id) c ON FALSE "
                                      "LEFT JOIN (SELECT i.cid AS concept_id FROM _cs_items i) k ON FALSE")
        idk = "k.concept_id" if has_concept else "i.cid"
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _cs_base AS
                        SELECT DISTINCT i.name, COALESCE(c.concept_id, {idk}) AS concept_id, i.code, i.descendants, i.mapped, i.excluded
                        FROM _cs_items i {lookup}""")
        missing = [tuple(r) for r in con.execute("SELECT DISTINCT name, code FROM _cs_base WHERE concept_id IS NULL ORDER BY 1, 2").fetchall()]
        need_ca = bool(con.execute("SELECT count(*) FROM _cs_base WHERE descendants AND concept_id IS NOT NULL").fetchone()[0])
        desc = (f"UNION SELECT b.name, ca.descendant_concept_id, b.mapped, b.excluded FROM _cs_base b "
                f"JOIN {concept_ancestor} ca ON ca.ancestor_concept_id = b.concept_id WHERE b.descendants")
        expanded = ("SELECT name, concept_id, mapped, excluded FROM _cs_base WHERE concept_id IS NOT NULL "
                    + (desc if concept_ancestor and need_ca else ""))
        has_cr = _has(con, "concept_relationship")
        mapped = ("UNION SELECT e.name, cr.concept_id_1, e.excluded FROM _cs_exp e JOIN concept_relationship cr "
                  "ON cr.concept_id_2 = e.concept_id AND cr.relationship_id = 'Maps to' WHERE e.mapped") if has_cr else ""
        con.execute(f"CREATE OR REPLACE TEMP TABLE _cs_exp AS {expanded}")
        got = con.execute(f"""
            WITH allc AS (SELECT name, concept_id, excluded FROM _cs_exp {mapped})
            SELECT DISTINCT name, CAST(concept_id AS BIGINT) AS concept_id FROM allc a WHERE NOT a.excluded
              AND NOT EXISTS (SELECT 1 FROM allc x WHERE x.excluded AND x.name = a.name AND x.concept_id = a.concept_id)
            ORDER BY name, concept_id""").df()
    finally:
        con.unregister("_cs_items")
        for t in ("_cs_base", "_cs_exp"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    if warn and missing:
        by = {}
        for n, c in missing:
            by.setdefault(n, []).append(c)
        warnings.warn(DataGapWarning("concept-set codes not in this vocabulary (dropped): "
                                     + "; ".join(f"{n}: {', '.join(cs)}" for n, cs in by.items()), "vocabulary"), stacklevel=2)
    if warn and need_ca and not concept_ancestor:
        warnings.warn(DataGapWarning("concept_ancestor not reachable: concept sets that include descendants keep only "
                                     "their listed concepts", "vocabulary"), stacklevel=2)
    got.attrs["missing"] = missing
    got.attrs["concept_ancestor"] = concept_ancestor
    return got


def resolve_concept_set(con, expression: Mapping[str, Any], **kw) -> list[int]:
    """Sorted concept ids of one concept-set expression (see :func:`resolve_concept_sets`)."""
    return [int(i) for i in resolve_concept_sets(con, {"set": expression}, **kw)["concept_id"]]


def _parts_available(con) -> bool:
    if not (_has(con, "concept") and _has(con, "concept_relationship")):
        return False
    n = con.execute("SELECT count(DISTINCT relationship_id) FROM concept_relationship WHERE relationship_id IN ('Has component', "
                    "'Has property', 'Has scale type', 'Has system')").fetchone()[0]
    return int(n) == 4


def _loinc_parts_sql() -> str:
    """One row per LOINC test: its component, property, scale, time aspect and system part ids, and the system's name."""
    cols = ",\n".join(f"MAX(CASE WHEN r.relationship_id = '{rel}' THEN r.concept_id_2 END) AS {k}" for k, rel in _PARTS.items())
    return f"""
        SELECT t.concept_id, t.concept_code, t.concept_name, t.standard_concept, {cols},
               MAX(CASE WHEN r.relationship_id = 'Has system' THEN s.concept_name END) AS system_name
        FROM concept t
        JOIN concept_relationship r ON r.concept_id_1 = t.concept_id
             AND r.relationship_id IN ({", ".join(f"'{v}'" for v in _PARTS.values())})
        LEFT JOIN concept s ON s.concept_id = r.concept_id_2
        WHERE t.vocabulary_id = 'LOINC' AND (t.invalid_reason IS NULL OR t.invalid_reason = '')
        GROUP BY ALL"""


def _blood(system_name: str | None) -> bool:
    return bool(system_name) and bool(LOINC_BLOOD_SYSTEM.search(system_name)) and not _NOT_BLOOD.search(system_name)


def _whole_blood(system_name: str | None) -> bool:
    """A blood system with no serum or plasma in it (Blood, Arterial blood, Capillary blood ...)."""
    return _blood(system_name) and not re.search(r"serum|plasma|\bser\b|\bplas\b", system_name or "", re.I)


#: How a lab set may grow (the set file's ``"expansion"``): ``blood`` (default) adds codes in any blood, serum or plasma
#: specimen; ``seed_systems`` only in the specimens the set already lists (blood gases: arterial pO2 must not take venous
#: pO2); ``none`` adds nothing (troponin I, where conventional and high-sensitivity assays share a property but not a unit).
EXPANSION_MODES = ("blood", "seed_systems", "none")


def expand_loinc_concept_set(con, expression: Mapping[str, Any], *, blood_only: bool = True,
                             mode: str = "blood", expand_from: Sequence[str] | None = None) -> dict[str, Any]:
    """``expression`` plus every LOINC test that shares a listed LOINC code's component, property, scale and time aspect.

    With ``blood_only`` (default) an added test's system must be blood, serum or plasma (:data:`LOINC_BLOOD_SYSTEM`);
    when every listed code is a whole-blood test, serum and plasma codes are not added. ``mode`` is one of
    :data:`EXPANSION_MODES` (``none`` returns the expression unchanged, ``seed_systems`` keeps the listed specimens).
    ``expand_from`` (a set file's ``"expand_from"``): grow only from these listed codes, e.g. hemoglobin from 718-7 (g/dL)
    and not from its molar code 59260-0, so no further molar codes join a feature read in g/dL.
    Added items carry ``CONCEPT_ID``, ``CONCEPT_CODE``, ``VOCABULARY_ID`` and ``CONCEPT_NAME``, so the result can be saved
    with :func:`write_concept_set` and read in ATLAS. Raises ``ValueError`` when the vocabulary has no LOINC part links."""
    if not _parts_available(con):
        raise ValueError("this vocabulary has no LOINC part relationships (Has component / Has property / Has scale type / "
                         "Has system); download LOINC from Athena with its parts to expand lab concept sets")
    if mode not in EXPANSION_MODES:
        raise ValueError(f"expansion mode must be one of {EXPANSION_MODES}, got {mode!r}")
    codes = concept_set_codes(expression)
    if mode == "none":
        return {**expression, "items": [dict(it) for it in expression["items"]]}
    parts = con.execute(_loinc_parts_sql()).df()
    seeds = parts[parts["concept_code"].isin(list(expand_from) if expand_from else codes)]
    key = ["component", "property", "scale", "time"]
    keys = seeds[key].dropna().drop_duplicates()
    cand = parts.merge(keys, on=key)
    if mode == "seed_systems":
        cand = cand[cand["system"].isin(set(seeds["system"].dropna()))]
    elif blood_only:
        seed_systems = set(seeds["system"].dropna())
        cand = cand[cand["system"].isin(seed_systems) | cand["system_name"].map(_blood)]
        if len(seeds) and seeds["system_name"].map(_whole_blood).all():
            # cell counts and other whole-blood tests: a serum or plasma code measures something else (platelets in plasma)
            cand = cand[cand["system"].isin(seed_systems) | cand["system_name"].map(_whole_blood)]
    have = set(codes)
    items = [dict(it) for it in expression["items"]]
    for r in cand.sort_values("concept_code").itertuples():
        if r.concept_code in have:
            continue
        have.add(r.concept_code)
        items.append(_item({"CONCEPT_ID": int(r.concept_id), "CONCEPT_CODE": r.concept_code, "VOCABULARY_ID": "LOINC",
                            "CONCEPT_NAME": r.concept_name, "STANDARD_CONCEPT": r.standard_concept}))
    # fill ids and names of the listed codes too, so the saved file is complete
    named = parts.set_index("concept_code")
    for it in items:
        c = it["concept"]
        if c.get("VOCABULARY_ID") == "LOINC" and c.get("CONCEPT_CODE") in named.index:
            row = named.loc[c["CONCEPT_CODE"]]
            row = row.iloc[0] if isinstance(row, pd.DataFrame) else row
            c.setdefault("CONCEPT_ID", int(row["concept_id"]))
            c.setdefault("CONCEPT_NAME", row["concept_name"])
            if c.get("CONCEPT_ID") is None:
                c["CONCEPT_ID"] = int(row["concept_id"])
    return {**{k: v for k, v in expression.items() if k != "items"}, "items": items}


def loinc_coverage(con, sets: Mapping[str, Mapping[str, Any]], *, cohort: pd.DataFrame | None = None, pre_days: int = 0,
                   min_share: float = 0.01) -> pd.DataFrame | None:
    """Codes a site's measurement rows use for each set's analyte that the set does not hold.

    For each set, the LOINC tests sharing a listed code's component (any property, scale or system) are counted in
    ``measurement`` (only rows in a cohort stay's ``[cohort_start_date - pre_days, cohort_end_date]`` when ``cohort`` has
    ``subject_id``, ``cohort_start_date`` and ``cohort_end_date``). Returns ``name, concept_code, concept_name,
    same_property, blood, share`` for each code outside the set holding at least ``min_share`` of the analyte's rows, or
    None when the vocabulary has no LOINC part links (nothing can be checked)."""
    if not _parts_available(con) or not _has(con, "measurement"):
        return None
    resolved = resolve_concept_sets(con, sets, warn=False)
    parts = con.execute(_loinc_parts_sql()).df()
    rows = []
    for name in sets:
        ids = set(resolved.loc[resolved["name"] == name, "concept_id"].astype(int))
        seeds = parts[parts["concept_id"].isin(ids)]
        if seeds.empty:
            continue
        cand = parts[parts["component"].isin(set(seeds["component"].dropna()))]
        props = set(map(tuple, seeds[["property", "scale"]].dropna().itertuples(index=False)))
        rows += [{"name": name, "concept_id": int(r.concept_id), "concept_code": r.concept_code, "concept_name": r.concept_name,
                  "in_set": int(r.concept_id) in ids, "same_property": (r.property, r.scale) in props, "blood": _blood(r.system_name)}
                 for r in cand.itertuples()]
    if not rows:
        return pd.DataFrame(columns=["name", "concept_code", "concept_name", "same_property", "blood", "share"])
    cand = pd.DataFrame(rows)
    con.register("_cs_cand", cand[["name", "concept_id"]])
    try:
        if cohort is not None and {"subject_id", "cohort_start_date", "cohort_end_date"} <= set(cohort.columns):
            con.register("_cs_coh", cohort[["subject_id", "cohort_start_date", "cohort_end_date"]])
            src = (f"FROM _cs_coh c JOIN measurement m ON m.person_id = c.subject_id "
                   f"AND m.measurement_date >= CAST(c.cohort_start_date AS DATE) - {int(pre_days)} "
                   f"AND m.measurement_date <= CAST(c.cohort_end_date AS DATE)")
        else:
            src = "FROM measurement m"
        counts = con.execute(f"SELECT k.name, k.concept_id, count(*) AS n {src} JOIN _cs_cand k ON k.concept_id = m.measurement_concept_id "
                             "GROUP BY ALL").df()
    finally:
        con.unregister("_cs_cand")
        try:
            con.unregister("_cs_coh")
        except Exception:
            pass
    out = cand.merge(counts, on=["name", "concept_id"], how="inner")
    if out.empty:
        return pd.DataFrame(columns=["name", "concept_code", "concept_name", "same_property", "blood", "share"])
    out["share"] = out["n"] / out.groupby("name")["n"].transform("sum")
    out = out[~out["in_set"] & (out["share"] >= min_share)]
    return out[["name", "concept_code", "concept_name", "same_property", "blood", "share"]].sort_values(["name", "share"],
                                                                                                         ascending=[True, False]).reset_index(drop=True)


def write_concept_set(path: str | os.PathLike, name: str, domain: str, expression: Mapping[str, Any], **meta: Any) -> Path:
    """Save one set in the bundled format (sorted keys, two-space indent, trailing newline)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "domain": domain, **meta, "expression": {"items": list(expression["items"])}}
    p.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def codes_by_name(domain: str, vocabulary: str = "LOINC") -> dict[str, list[str]]:
    """``{name: [code, ...]}`` of a bundled LOINC domain (what ``BENCHMARK_LAB_LOINCS`` used to be written as)."""
    return {n: concept_set_codes(e, vocabulary) for n, e in load_concept_sets(domain).items()}


def ancestor_specs(domain: str) -> dict[str, dict[str, list[int]]]:
    """``{name: {"ancestor_concept_ids": [...]}}`` of a bundled medication domain (every item includes descendants)."""
    out = {}
    for n, e in load_concept_sets(domain).items():
        if any(not it.get("includeDescendants") or it.get("isExcluded") for it in e["items"]):
            raise ValueError(f"medication concept set {domain}/{n}: every item must include descendants and none be excluded")
        out[n] = {"ancestor_concept_ids": concept_set_ids(e)}
    return out


def concept_set_fingerprint() -> dict[str, str]:
    """``{"version", "sha256"}`` of the bundled sets (every file, in path order): saved features built with other concept
    sets are refused by ``--reuse-features``, and the run record names the sets a run used."""
    import hashlib

    root = concept_set_dir()
    h = hashlib.sha256()
    for p in sorted(root.glob("**/*.json")):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return {"version": CONCEPT_SET_VERSION, "sha256": h.hexdigest()}


#: Version of the bundled concept sets (``manifest.json`` ``version``; bump it whenever a file changes).
CONCEPT_SET_VERSION: str = concept_set_manifest()["version"]


#: Name of the lab and vital definitions in force before the bundled concept sets: the plain LOINC lists of releases 0.20.0 to 0.28.1
#: (unchanged across them), which built the workbook delivered on 2026-11-11. No vocabulary expansion, no troponin I, lactate, pCO2
#: or pO2. ``run_study(concept_sets=LEGACY_CONCEPT_SETS)`` (and the ``delivered_2026_11_11`` preset) uses them.
LEGACY_CONCEPT_SETS: str = "legacy_2026_11_11"

_LEGACY_CODES: dict[str, dict[str, list[str]]] = {
    "labs": {
        "bun": ["3094-0", "6299-2"], "creatinine": ["2160-0", "38483-4"], "sodium": ["2951-2", "2947-0"],
        "potassium": ["2823-3", "6298-4"], "chloride": ["2075-0"], "bicarbonate": ["1963-8", "2028-9", "2026-3"],
        "glucose": ["2345-7", "2339-0", "41653-7"], "calcium": ["17861-6"], "albumin": ["1751-7", "2862-1"],
        "bilirubin_total": ["1975-2"], "ast": ["1920-8", "30239-8"], "alt": ["1742-6", "1743-4", "1744-2"],
        "wbc": ["6690-2", "26464-8"], "hemoglobin": ["718-7", "59260-0"], "platelets": ["777-3", "26515-7"],
        "hba1c": ["4548-4", "17856-6", "17855-8", "59261-8"], "hematocrit": ["20570-8", "4544-3", "4545-0"],
        "alkaline_phosphatase": ["6768-6"], "ldl": ["13457-7", "2089-1", "18262-6"], "anion_gap": ["33037-3", "41276-7", "1863-0"],
    },
    "vitals": {
        "bmi": ["39156-5"], "systolic_bp": ["8480-6"], "diastolic_bp": ["8462-4"], "heart_rate": ["8867-4"],
        "respiratory_rate": ["9279-1"], "body_temperature": ["8310-5"], "oxygen_saturation": ["2708-6", "59408-5"],
    },
}


def legacy_feature_codes() -> dict[str, dict[str, list[str]]]:
    """``{"labs": {name: [LOINC, ...]}, "vitals": {...}}`` of :data:`LEGACY_CONCEPT_SETS` (a copy; the lists are frozen)."""
    return {d: {n: list(c) for n, c in v.items()} for d, v in _LEGACY_CODES.items()}


def concept_set_version_fingerprint(version: str = "bundled") -> dict[str, str]:
    """:func:`concept_set_fingerprint` for ``"bundled"`` (or the bundled version string); for :data:`LEGACY_CONCEPT_SETS` the
    ``{"version", "sha256"}`` of the frozen LOINC lists. Any other version raises: it is not in this release."""
    import hashlib

    if version == LEGACY_CONCEPT_SETS:
        return {"version": LEGACY_CONCEPT_SETS, "sha256": hashlib.sha256(json.dumps(_LEGACY_CODES, sort_keys=True).encode()).hexdigest()}
    if version in ("bundled", CONCEPT_SET_VERSION):
        return concept_set_fingerprint()
    raise ValueError(f"concept-set version {version!r} is not in this release (bundled: {CONCEPT_SET_VERSION!r}, "
                     f"also available: {LEGACY_CONCEPT_SETS!r}); install the omop-duck-db release that ships it.")
