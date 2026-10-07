"""Comorbidity profilers (RFC 3.1): the 31 Elixhauser domains with the van Walraven score and the 17 Quan-Charlson
categories with the Charlson index, computed per cohort row in one set-based DuckDB query.

Both profilers are driven by curated code tables that ship as data, ``inst/extdata/comorbidity/elixhauser_codes.csv``
and ``charlson_codes.csv`` (sources, weights, hierarchy rules and known deviations are written up in ``PROVENANCE.md``
next to them). The R package reads the very same files and builds the very same SQL, so the two languages return the
same columns, values and NULL semantics. Weights come from the CSV ``weight`` column, never from code.

A condition record counts towards a domain when either

* its ``condition_source_value``, normalised (upper-case, ``.`` and whitespace removed), STARTS WITH one of the
  domain's ICD-10-CM / ICD-9-CM prefixes (``I50`` matches ``I50.9`` and ``i 50.9``; ``4280`` matches ``428.0``), or
* (``source_and_standard=True``) its ``condition_concept_id`` is one of the domain's ``SNOMED_ANCESTOR`` concepts or a
  descendant of one in ``concept_ancestor``.

Only records dated strictly before the index date are used (``[index - lookback_days, index)``), so nothing at or after
the index leaks in; this is the same rule as ``extract_sparse_concept_matrix``.

The query is a single statement of CTEs. Nothing is created, so it runs on read-only connections and leaves no objects
behind.
"""

from __future__ import annotations

import csv
import operator
import os
import re
import warnings
from dataclasses import dataclass
from typing import Any

import duckdb

from omop_etl.build_omop_cdm import _resource_path

_CSV_HEADER = ["index", "domain", "column", "label", "weight", "code_system", "code", "note"]
_ICD_SYSTEMS = ("ICD10CM", "ICD9CM")
_SNOMED_SYSTEM = "SNOMED_ANCESTOR"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ICD_CODE_RE = re.compile(r"^[A-Z0-9]+$")
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
_FORMATS = ("df", "arrow", "pyarrow", "polars")

# Where concept_ancestor may live, in order: the connection's search path (main, a TEMP view, or the vocabulary
# schema omop_connect() puts on it), then an attached central vocabulary.
_CONCEPT_ANCESTOR_CANDIDATES = (
    "concept_ancestor",
    "central_vocab.main.concept_ancestor",
    "central_vocab.concept_ancestor",
)
_CONDITION_COLUMNS = ("person_id", "condition_concept_id", "condition_start_date", "condition_source_value")


@dataclass(frozen=True)
class _Spec:
    """One comorbidity index: its code table file, summary columns and hierarchy rules."""

    index: str
    file: str
    score_col: str
    total_col: str
    # (dominant, suppressed): the suppressed domain adds nothing to the score or the count when the dominant one is
    # present (inst/extdata/comorbidity/PROVENANCE.md, "Hierarchy rules").
    hierarchy: tuple


_ELIXHAUSER = _Spec(
    index="elixhauser",
    file="elixhauser_codes.csv",
    score_col="elix_van_walraven_score",
    total_col="elix_total_conditions",
    hierarchy=(("dm_comp", "dm_uncomp"), ("mets", "solid_tumor"), ("htn_comp", "htn_uncomp")),
)
_CHARLSON = _Spec(
    index="charlson",
    file="charlson_codes.csv",
    score_col="charlson_index",
    total_col="cci_total_conditions",
    hierarchy=(("mod_severe_liver", "mild_liver"), ("dm_comp", "dm_uncomp"), ("mets", "malignancy")),
)


@dataclass(frozen=True)
class _CodeTable:
    domains: tuple  # ((domain, column, weight), ...) in CSV order
    icd: tuple  # ((domain, prefix), ...)
    ancestors: tuple  # ((domain, concept_id), ...)


# --------------------------------------------------------------------------------------
# Code table
# --------------------------------------------------------------------------------------
def _load_code_table(spec: _Spec) -> _CodeTable:
    """Read and validate the bundled code table of ``spec``."""
    path = _resource_path("extdata", "comorbidity", spec.file)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Comorbidity code table not found at {path}. A source checkout needs inst/extdata/comorbidity/{spec.file}; "
            f"an installed wheel ships it as omop_etl/resources/extdata/comorbidity/{spec.file}.")
    domains: dict[str, tuple[str, int]] = {}
    icd, ancestors = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != _CSV_HEADER:
            raise ValueError(f"{path}: expected header {_CSV_HEADER}, got {reader.fieldnames}")
        for rec in reader:
            where = f"{path} line {reader.line_num}"
            domain, column, system, code = rec["domain"], rec["column"], rec["code_system"], rec["code"]
            if rec["index"] != spec.index:
                raise ValueError(f"{where}: index is {rec['index']!r}, expected {spec.index!r}")
            if not _NAME_RE.match(domain) or not _NAME_RE.match(column):
                raise ValueError(f"{where}: domain/column must be lower-case identifiers, got {domain!r}/{column!r}")
            try:
                weight = int(rec["weight"])
            except ValueError as e:
                raise ValueError(f"{where}: weight {rec['weight']!r} is not an integer") from e
            if domains.setdefault(domain, (column, weight)) != (column, weight):
                raise ValueError(f"{where}: domain {domain!r} has more than one column or weight")
            if system in _ICD_SYSTEMS:
                if not _ICD_CODE_RE.match(code):
                    raise ValueError(f"{where}: ICD prefix {code!r} must be upper-case letters/digits without dots")
                icd.append((domain, code))
            elif system == _SNOMED_SYSTEM:
                try:
                    ancestors.append((domain, int(code)))
                except ValueError as e:
                    raise ValueError(f"{where}: ancestor {code!r} is not an integer concept_id") from e
            else:
                raise ValueError(f"{where}: unknown code_system {system!r}")
    if not domains or not icd:
        raise ValueError(f"{path} contains no domains or no ICD prefixes")
    columns = [c for c, _ in domains.values()]
    if len(set(columns)) != len(columns):
        raise ValueError(f"{path}: two domains share a column name")
    for dominant, suppressed in spec.hierarchy:
        missing = [d for d in (dominant, suppressed) if d not in domains]
        if missing:
            raise ValueError(f"{path}: hierarchy rule {dominant} > {suppressed} refers to missing domain(s) {missing}")
    return _CodeTable(
        domains=tuple((d, c, w) for d, (c, w) in domains.items()), icd=tuple(icd), ancestors=tuple(ancestors))


# --------------------------------------------------------------------------------------
# Argument validation and name resolution
# --------------------------------------------------------------------------------------
def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _as_int(value: Any, what: str, allow_none: bool, minimum: int | None = None) -> int | None:
    """``value`` as a Python int (ints, numpy ints and whole floats), None if allowed; ValueError otherwise."""
    if value is None and allow_none:
        return None
    n = None
    if not isinstance(value, bool):
        try:
            n = operator.index(value)
        except TypeError:
            if isinstance(value, float) and value.is_integer():
                n = int(value)
    if n is None or (minimum is not None and n < minimum):
        need = "a non-negative integer" if minimum == 0 else "an integer"
        raise ValueError(f"{what} must be {need}{' or None' if allow_none else ''}; got {value!r}")
    return n


def _qualified_table(name: Any) -> str:
    if not isinstance(name, str) or not _TABLE_RE.match(name):
        raise ValueError(
            f"cohort_table must be a table or view name, optionally schema-qualified (letters, digits and "
            f"underscores only); got {name!r}")
    return ".".join(_quote_ident(p) for p in name.split("."))


def _columns_of(con, qualified: str) -> list[str] | None:
    """Column names of a table or view, or None when it does not exist."""
    try:
        return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {qualified} LIMIT 0").fetchall()]
    except duckdb.CatalogException:
        return None


def _find_column(columns: list[str], wanted: Any, param: str, table: str) -> str:
    """The table's own spelling of column ``wanted`` (matched case-insensitively); ValueError if absent."""
    if not isinstance(wanted, str) or not wanted:
        raise ValueError(f"{param} must be a column name; got {wanted!r}")
    by_lower = {c.lower(): c for c in columns}
    if wanted.lower() not in by_lower:
        raise ValueError(
            f"{param} {wanted!r} is not a column of {table!r} (columns: {', '.join(columns)}). "
            f"Set {param}= explicitly; columns are never guessed.")
    return by_lower[wanted.lower()]


def _resolve_concept_ancestor(con) -> tuple[str | None, str]:
    """First reachable, non-empty concept_ancestor -> ``(reference, "")``; else ``(None, reason)``."""
    seen_empty = False
    for ref in _CONCEPT_ANCESTOR_CANDIDATES:
        try:
            row = con.execute(f"SELECT ancestor_concept_id, descendant_concept_id FROM {ref} LIMIT 1").fetchone()
        except (duckdb.CatalogException, duckdb.BinderException):
            continue
        if row is not None:
            return ref, ""
        seen_empty = True
    if seen_empty:
        return None, "concept_ancestor exists on this connection but is empty"
    return None, ("concept_ancestor could not be resolved on this connection (looked for "
                  + ", ".join(_CONCEPT_ANCESTOR_CANDIDATES) + ")")


# --------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------
def _build_sql(spec, codes, *, qualified, person, index_col, person_out, index_out, cohort_where, lookback_days,
               concept_ancestor, hierarchy_adjusted):
    """The profiler as one statement of CTEs.

    ``cohort_base`` is the distinct (person, index date) rows and ``cond`` the condition records of those persons with a
    normalised source value. ``src_hit`` / ``std_hit`` give the domains each distinct source value / concept id belongs
    to; ICD prefixes are matched by exact equality on ``left(src, len)`` for each distinct prefix length, so the join is
    a hash join and every distinct source value is classified once. ``hits`` is the distinct (cohort row, domain) pairs
    inside the window and ``flat`` one 0/1 column per domain, zero for cohort rows without a hit.
    """
    icd_values = ",\n      ".join(f"({_quote_str(d)}, {_quote_str(c)})" for d, c in codes.icd)
    ctes = [
        f"icd AS (\n  SELECT domain, code, length(code) AS len FROM (VALUES\n      {icd_values}\n  ) AS v(domain, code)\n)",
        "cohort_base AS (\n"
        f"  SELECT DISTINCT c.{_quote_ident(person)} AS person_key, c.{_quote_ident(index_col)} AS index_key,\n"
        f"         CAST(c.{_quote_ident(index_col)} AS DATE) AS idx_d\n"
        f"  FROM {qualified} AS c{' WHERE ' + cohort_where if cohort_where else ''}\n)",
        "cond AS (\n"
        "  SELECT co.person_id AS pid, co.condition_concept_id AS cid,\n"
        "         CAST(co.condition_start_date AS DATE) AS cdate,\n"
        "         upper(replace(regexp_replace(coalesce(co.condition_source_value, ''), '\\s+', '', 'g'), '.', '')) AS src\n"
        "  FROM condition_occurrence AS co\n"
        "  WHERE co.condition_start_date IS NOT NULL\n"
        "    AND co.person_id IN (SELECT person_key FROM cohort_base)\n)",
        "src_hit AS (\n"
        "  SELECT DISTINCT s.src, i.domain\n"
        "  FROM (SELECT DISTINCT src FROM cond) AS s\n"
        "  CROSS JOIN (SELECT DISTINCT len FROM icd) AS l\n"
        "  JOIN icd AS i ON i.len = l.len AND i.code = left(s.src, l.len)\n)",
    ]
    cond_hit = "SELECT c.pid, c.cdate, h.domain FROM cond AS c JOIN src_hit AS h ON h.src = c.src"
    if concept_ancestor is not None:
        anc_values = ",\n      ".join(f"({_quote_str(d)}, {int(a)})" for d, a in codes.ancestors)
        ctes.append(
            f"anc AS (\n  SELECT domain, CAST(concept_id AS BIGINT) AS concept_id FROM (VALUES\n      {anc_values}\n"
            "  ) AS v(domain, concept_id)\n)")
        ctes.append(
            "std_hit AS (\n"
            "  SELECT DISTINCT domain, concept_id FROM (\n"
            "    SELECT a.domain, ca.descendant_concept_id AS concept_id FROM anc AS a\n"
            f"    JOIN {concept_ancestor} AS ca ON ca.ancestor_concept_id = a.concept_id\n"
            "    UNION ALL\n"
            "    SELECT domain, concept_id FROM anc\n"
            "  )\n)")
        cond_hit += (
            "\n    UNION ALL\n    SELECT c.pid, c.cdate, h.domain FROM cond AS c JOIN std_hit AS h ON h.concept_id = c.cid")
    window = "ch.cdate < cb.idx_d"
    if lookback_days is not None:
        window += f" AND ch.cdate >= cb.idx_d - {int(lookback_days)}"
    ctes.append(f"cond_hit AS (\n    {cond_hit}\n)")
    ctes.append(
        "hits AS (\n"
        "  SELECT DISTINCT cb.person_key, cb.index_key, ch.domain\n"
        "  FROM cohort_base AS cb\n"
        f"  JOIN cond_hit AS ch ON ch.pid = cb.person_key AND {window}\n)")

    flag_aggs = ",\n         ".join(
        f"MAX(CASE WHEN domain = {_quote_str(d)} THEN 1 ELSE 0 END) AS {_quote_ident(c)}" for d, c, _ in codes.domains)
    ctes.append(
        f"flags AS (\n  SELECT person_key, index_key,\n         {flag_aggs}\n  FROM hits GROUP BY person_key, index_key\n)")
    flag_cols = ",\n         ".join(
        f"CAST(COALESCE(f.{_quote_ident(c)}, 0) AS INTEGER) AS {_quote_ident(c)}" for _, c, _ in codes.domains)
    ctes.append(
        f"flat AS (\n  SELECT cb.person_key, cb.index_key,\n         {flag_cols}\n  FROM cohort_base AS cb\n"
        "  LEFT JOIN flags AS f ON f.person_key = cb.person_key AND f.index_key = cb.index_key\n)")

    col_of = {d: c for d, c, _ in codes.domains}
    suppressed_by = {s: d for d, s in spec.hierarchy} if hierarchy_adjusted else {}
    terms = []
    for d, c, w in codes.domains:
        flag = _quote_ident(c)
        if d in suppressed_by:
            flag = f"(CASE WHEN {_quote_ident(col_of[suppressed_by[d]])} = 1 THEN 0 ELSE {flag} END)"
        terms.append((flag, w))
    total_sql = " + ".join(flag for flag, _ in terms)
    score_sql = " + ".join(f"({flag} * ({w}))" for flag, w in terms)
    flag_names = ", ".join(_quote_ident(c) for _, c, _ in codes.domains)
    return (
        "WITH\n" + ",\n".join(ctes) + "\n"
        f"SELECT person_key AS {_quote_ident(person_out)}, index_key AS {_quote_ident(index_out)}, {flag_names},\n"
        f"       CAST(({score_sql}) AS INTEGER) AS {_quote_ident(spec.score_col)},\n"
        f"       CAST(({total_sql}) AS INTEGER) AS {_quote_ident(spec.total_col)}\n"
        "FROM flat\n"
        "ORDER BY person_key, index_key"
    )


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------
def _profile(con, spec: _Spec, fn: str, cohort_table, cohort_id, person_col, index_date_col, lookback_days,
             source_and_standard, hierarchy_adjusted, format):
    if not isinstance(format, str) or format.lower().strip() not in _FORMATS:
        raise ValueError(f"format must be one of {_FORMATS}; got {format!r}")
    fmt = format.lower().strip()
    lookback = _as_int(lookback_days, "lookback_days", allow_none=True, minimum=0)
    cohort = _as_int(cohort_id, "cohort_id", allow_none=True)
    for name, value in (("source_and_standard", source_and_standard), ("hierarchy_adjusted", hierarchy_adjusted)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be True or False; got {value!r}")
    if fmt == "polars":
        try:
            import polars  # noqa: F401
        except ImportError as e:
            raise ImportError("format='polars' needs the optional 'polars' package; use 'df' or 'arrow'.") from e
    codes = _load_code_table(spec)
    qualified = _qualified_table(cohort_table)

    cohort_cols = _columns_of(con, qualified)
    if cohort_cols is None:
        raise ValueError(f"cohort_table {cohort_table!r} does not exist on this connection")
    person = _find_column(cohort_cols, person_col, "person_col", cohort_table)
    index_col = _find_column(cohort_cols, index_date_col, "index_date_col", cohort_table)
    if person == index_col:
        raise ValueError("person_col and index_date_col must be different columns")
    clash = sorted({person_col.lower(), index_date_col.lower()} & ({c for _, c, _ in codes.domains}
                                                                    | {spec.score_col, spec.total_col}))
    if clash:
        raise ValueError(f"person_col / index_date_col would collide with output column(s) {clash}")

    condition_cols = _columns_of(con, _quote_ident("condition_occurrence"))
    if condition_cols is None:
        raise ValueError("condition_occurrence does not exist on this connection")
    missing = [c for c in _CONDITION_COLUMNS if c not in {x.lower() for x in condition_cols}]
    if missing:
        raise ValueError(f"condition_occurrence is missing column(s) {missing}")

    cohort_where = ""
    if cohort is not None:
        cdef = {c.lower(): c for c in cohort_cols}.get("cohort_definition_id")
        if cdef is None:
            warnings.warn(
                f"{fn}(): cohort_id={cohort} was ignored because {cohort_table!r} has no cohort_definition_id "
                "column; every row of the table is profiled.", UserWarning, stacklevel=3)
        else:
            cohort_where = f"c.{_quote_ident(cdef)} = {cohort}"
    null_rows = con.execute(
        f"SELECT count(*) FROM {qualified} AS c WHERE (c.{_quote_ident(person)} IS NULL "
        f"OR c.{_quote_ident(index_col)} IS NULL)" + (f" AND {cohort_where}" if cohort_where else "")
    ).fetchone()[0]
    if null_rows:
        raise ValueError(
            f"{cohort_table!r} has {null_rows} row(s) with a NULL {person_col} or {index_date_col}; "
            "an index date is required to place the look-back window. Remove those rows first.")

    concept_ancestor = None
    if source_and_standard and codes.ancestors:
        concept_ancestor, reason = _resolve_concept_ancestor(con)
        if concept_ancestor is None:
            warnings.warn(
                f"{fn}(): source_and_standard=True but {reason}. Falling back to source-value (ICD prefix) matching "
                "only; standard-concept (SNOMED descendant) matches are NOT included. Load or attach the vocabulary "
                "(e.g. omop_connect()) or pass source_and_standard=False to silence this warning.",
                UserWarning, stacklevel=3)

    sql = _build_sql(
        spec, codes, qualified=qualified, person=person, index_col=index_col, person_out=person_col,
        index_out=index_date_col, cohort_where=cohort_where, lookback_days=lookback,
        concept_ancestor=concept_ancestor, hierarchy_adjusted=hierarchy_adjusted)
    res = con.execute(sql)
    if fmt == "df":
        return res.df()
    if hasattr(res, "to_arrow_table"):
        arrow = res.to_arrow_table()
    elif hasattr(res, "fetch_arrow_table"):  # duckdb < 1.4
        arrow = res.fetch_arrow_table()
    else:
        arrow = res.arrow().read_all()
    if fmt == "polars":
        import polars as pl
        return pl.from_arrow(arrow)
    return arrow


def extract_elixhauser_comorbidities(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    person_col: str = "subject_id",
    index_date_col: str = "cohort_start_date",
    lookback_days: int | None = 365,
    source_and_standard: bool = True,
    hierarchy_adjusted: bool = True,
    format: str = "df",
) -> Any:
    """Elixhauser comorbidity profile (31 domains, Quan et al. 2005) with the van Walraven 2009 score.

    One row per DISTINCT (person, index date) of the cohort table, ordered by person then index date. Persons with no
    qualifying condition still appear, with every flag 0 and a score of 0. Code lists, weights and the hierarchy rules
    are in ``inst/extdata/comorbidity/elixhauser_codes.csv`` and ``PROVENANCE.md`` next to it.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection holding ``condition_occurrence`` and the cohort table. Read-only is fine; nothing is created.
        ``concept_ancestor`` is looked up as ``concept_ancestor`` (main, a TEMP view or the connection's
        ``search_path``, e.g. after ``omop_connect()``), then as ``central_vocab.main.concept_ancestor``.
    cohort_table : str, default 'cohort'
        Table or view with one row per index event; may be schema-qualified.
    cohort_id : int, optional
        Keep only rows with ``cohort_definition_id = cohort_id``. If the table has no ``cohort_definition_id``
        column a warning is emitted and every row is profiled.
    person_col : str, default 'subject_id'
        Person id column of ``cohort_table``, joined to ``condition_occurrence.person_id``. It must exist: columns are
        never guessed.
    index_date_col : str, default 'cohort_start_date'
        Index date column of ``cohort_table``; must exist. It is cast to DATE to place the window.
    lookback_days : int or None, default 365
        Window ``[index - lookback_days, index)``: ``condition_start_date`` strictly before the index date and no more
        than ``lookback_days`` days earlier. ``None`` means all history before the index date.
    source_and_standard : bool, default True
        Also match ``condition_concept_id`` against the domain's SNOMED ancestors and their descendants. If
        ``concept_ancestor`` cannot be resolved on the connection (or is empty) a ``UserWarning`` is emitted and only
        the source side is used. ``False`` uses the source side only, without a warning.
    hierarchy_adjusted : bool, default True
        Apply the hierarchy rules (``dm_comp`` > ``dm_uncomp``, ``mets`` > ``solid_tumor``, ``htn_comp`` >
        ``htn_uncomp``) to the score and to the condition count. The 0/1 domain columns are always the raw flags.
        ``False`` gives the plain weighted sum and the plain count of raw flags.
    format : {'df', 'arrow', 'polars'}, default 'df'
        Return a pandas DataFrame, a pyarrow Table or a polars DataFrame. ``'polars'`` needs the optional ``polars``
        package and raises ``ImportError`` without it.

    Returns
    -------
    pandas.DataFrame, pyarrow.Table or polars.DataFrame
        Columns, in order: ``person_col``, ``index_date_col``, the 31 ``elix_<domain>`` flags (integer 0/1: chf,
        arrhythmia, valvular, pulm_circ, pvd, htn_uncomp, htn_comp, paralysis, neuro_other, copd, dm_uncomp, dm_comp,
        hypothyroid, renal_failure, liver_disease, pud, hiv, lymphoma, mets, solid_tumor, rheumatic, coagulopathy,
        obesity, weight_loss, fluid_electrolyte, blood_loss_anemia, deficiency_anemia, alcohol_abuse, drug_abuse,
        psychoses, depression), ``elix_van_walraven_score`` (integer; the weights run from -7 to 12, so it can be
        negative) and ``elix_total_conditions`` (integer count of flagged domains).

    Raises
    ------
    ValueError
        ``cohort_table``, ``person_col`` or ``index_date_col`` do not exist or are not valid names, ``lookback_days``
        is not a non-negative integer or None, ``format`` is unknown, or the cohort has NULL person ids or index dates.

    Notes
    -----
    A condition record counts for a domain when its ``condition_source_value``, upper-cased with ``.`` and whitespace
    removed, starts with one of the domain's ICD-10-CM / ICD-9-CM prefixes, or (``source_and_standard``) its
    ``condition_concept_id`` is a descendant, or a SNOMED ancestor itself, of one of the domain's ancestor concepts.

    Hierarchy (``hierarchy_adjusted=True``): only the mets rule changes the score (12 instead of 16 with solid tumour);
    the diabetes and hypertension rules change the count. The standard-concept side of ``dm_uncomp`` is the set of
    diabetes types, which includes every complicated form, so its raw flag is only meaningful after the hierarchy is
    applied.

    Prefixes are matched against any source value, whatever its coding system, so an ICD-9-CM ``V`` prefix can match an
    ICD-10-CM transport-accident code and the ICD-10-CM prefixes E00-E03, E86, E87 and E890 can match ICD-9-CM
    external-cause E codes (``PROVENANCE.md`` lists them); profile data whose coding system you know.

    Examples
    --------
    >>> import duckdb
    >>> from omop_etl.comorbidity import extract_elixhauser_comorbidities
    >>> con = duckdb.connect()
    >>> _ = con.execute("CREATE TABLE condition_occurrence AS SELECT 1 AS person_id, 0 AS condition_concept_id, "
    ...                 "DATE '2021-05-01' AS condition_start_date, 'I50.9' AS condition_source_value")
    >>> _ = con.execute("CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
    >>> df = extract_elixhauser_comorbidities(con, source_and_standard=False)
    >>> df[["subject_id", "elix_chf", "elix_van_walraven_score"]].values.tolist()
    [[1, 1, 7]]
    """
    return _profile(con, _ELIXHAUSER, "extract_elixhauser_comorbidities", cohort_table, cohort_id, person_col,
                    index_date_col, lookback_days, source_and_standard, hierarchy_adjusted, format)


def extract_charlson_index(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    person_col: str = "subject_id",
    index_date_col: str = "cohort_start_date",
    lookback_days: int | None = 365,
    source_and_standard: bool = True,
    hierarchy_adjusted: bool = True,
    format: str = "df",
) -> Any:
    """Charlson comorbidity profile (17 categories, Quan et al. 2005 coding) with the Charlson index.

    One row per DISTINCT (person, index date) of the cohort table, ordered by person then index date. Persons with no
    qualifying condition still appear, with every flag 0 and an index of 0. Code lists, the original Charlson 1987
    weights and the hierarchy rules are in ``inst/extdata/comorbidity/charlson_codes.csv`` and ``PROVENANCE.md`` next
    to it.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection holding ``condition_occurrence`` and the cohort table. Read-only is fine; nothing is created.
        ``concept_ancestor`` is looked up as ``concept_ancestor`` (main, a TEMP view or the connection's
        ``search_path``, e.g. after ``omop_connect()``), then as ``central_vocab.main.concept_ancestor``.
    cohort_table : str, default 'cohort'
        Table or view with one row per index event; may be schema-qualified.
    cohort_id : int, optional
        Keep only rows with ``cohort_definition_id = cohort_id``. If the table has no ``cohort_definition_id``
        column a warning is emitted and every row is profiled.
    person_col : str, default 'subject_id'
        Person id column of ``cohort_table``, joined to ``condition_occurrence.person_id``. It must exist: columns are
        never guessed.
    index_date_col : str, default 'cohort_start_date'
        Index date column of ``cohort_table``; must exist. It is cast to DATE to place the window.
    lookback_days : int or None, default 365
        Window ``[index - lookback_days, index)``: ``condition_start_date`` strictly before the index date and no more
        than ``lookback_days`` days earlier. ``None`` means all history before the index date.
    source_and_standard : bool, default True
        Also match ``condition_concept_id`` against the category's SNOMED ancestors and their descendants. If
        ``concept_ancestor`` cannot be resolved on the connection (or is empty) a ``UserWarning`` is emitted and only
        the source side is used. ``False`` uses the source side only, without a warning.
    hierarchy_adjusted : bool, default True
        Apply the hierarchy rules (``mod_severe_liver`` > ``mild_liver``, ``dm_comp`` > ``dm_uncomp``, ``mets`` >
        ``malignancy``) to the index and to the condition count. The 0/1 category columns are always the raw flags.
        ``False`` gives the plain weighted sum and the plain count of raw flags.
    format : {'df', 'arrow', 'polars'}, default 'df'
        Return a pandas DataFrame, a pyarrow Table or a polars DataFrame. ``'polars'`` needs the optional ``polars``
        package and raises ``ImportError`` without it.

    Returns
    -------
    pandas.DataFrame, pyarrow.Table or polars.DataFrame
        Columns, in order: ``person_col``, ``index_date_col``, the 17 ``cci_<category>`` flags (integer 0/1: mi, chf,
        pvd, cevd, dementia, copd, rheum, pud, mild_liver, dm_uncomp, dm_comp, plegia, renal, malignancy,
        mod_severe_liver, mets, hiv), ``charlson_index`` (integer weighted sum) and ``cci_total_conditions`` (integer
        count of flagged categories).

    Raises
    ------
    ValueError
        ``cohort_table``, ``person_col`` or ``index_date_col`` do not exist or are not valid names, ``lookback_days``
        is not a non-negative integer or None, ``format`` is unknown, or the cohort has NULL person ids or index dates.

    Notes
    -----
    A condition record counts for a category when its ``condition_source_value``, upper-cased with ``.`` and whitespace
    removed, starts with one of the category's ICD-10-CM / ICD-9-CM prefixes, or (``source_and_standard``) its
    ``condition_concept_id`` is a descendant, or a SNOMED ancestor itself, of one of the category's ancestor concepts.

    Metastatic solid tumour has no standard-concept side and is detected from ICD source values only. Non-melanoma
    skin cancer cannot be excluded on the standard side. Both caveats, and the ICD-9-CM ``V`` code collision with
    ICD-10-CM, are described in ``PROVENANCE.md``.

    See Also
    --------
    extract_elixhauser_comorbidities : the same profiler for the 31 Elixhauser domains.

    Examples
    --------
    >>> import duckdb
    >>> from omop_etl.comorbidity import extract_charlson_index
    >>> con = duckdb.connect()
    >>> _ = con.execute("CREATE TABLE condition_occurrence AS SELECT 1 AS person_id, 0 AS condition_concept_id, "
    ...                 "DATE '2021-05-01' AS condition_start_date, 'C78.0' AS condition_source_value")
    >>> _ = con.execute("CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
    >>> df = extract_charlson_index(con, source_and_standard=False)
    >>> df[["subject_id", "cci_mets", "charlson_index"]].values.tolist()
    [[1, 1, 6]]
    """
    return _profile(con, _CHARLSON, "extract_charlson_index", cohort_table, cohort_id, person_col,
                    index_date_col, lookback_days, source_and_standard, hierarchy_adjusted, format)
