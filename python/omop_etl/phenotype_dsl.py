"""Composable phenotype cohort DSL compiled to single-pass DuckDB SQL.

Build a cohort definition out of clinical criteria and logical operators::

    t2dm = (
        ConditionCriteria(icd_codes=["E11"], concept_ids=[201826])
        | MeasurementCriteria(loinc_codes=["4548-4"], value_gte=6.5)
        | DrugCriteria(concept_ids=[21600712], min_days_supply=30)
    )
    execute_phenotype(con, t2dm & ~ConditionCriteria(icd_codes=["E10"]), "cohort", cohort_definition_id=7)

Criteria (``ConditionCriteria``, ``MeasurementCriteria``, ``DrugCriteria``, ``Everyone``) are leaves;
``|`` / :class:`Union`, ``&`` / :class:`Intersect` and ``~`` / :class:`Not` compose them.

**Single pass.** All leaves emit ``(person_id, event_date, leaf)`` rows into one ``UNION ALL``; one
``GROUP BY person_id`` computes every leaf's qualifying date with ``FILTER`` clauses, and the boolean tree is
evaluated as one ``WHERE`` over those columns. No leaf is re-scanned per operator, and no intermediate cohort
tables are created.

**Index date.** A person's index date is the earliest qualifying event for ``Union`` and the latest of the
operands' qualifying dates for ``Intersect`` (the moment every criterion is first satisfied). ``Not`` only
excludes. With ``index_table=...`` the index date is that table's date instead, and a leaf's ``lookback_days`` /
``lookahead_days`` are measured relative to it (e.g. a lab in the 30 days before the index admission).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = [
    "PhenotypeExpr", "ConditionCriteria", "MeasurementCriteria", "DrugCriteria", "Everyone",
    "Union", "Intersect", "Not", "compile_phenotype", "execute_phenotype", "phenotype_from_dict",
]

_PRIMARY_STATUS = (32902,)    # "Primary diagnosis"
_SECONDARY_STATUS = (32908,)  # "Secondary diagnosis"


def _q(s: Any) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _ints(values: Sequence[Any]) -> list[int]:
    return [int(v) for v in values]


# ----------------------------------------------------------------------------------------------
# expression tree
# ----------------------------------------------------------------------------------------------
class PhenotypeExpr:
    """Base class: supports ``|``, ``&`` and ``~``."""

    def __or__(self, other: "PhenotypeExpr") -> "Union":
        return Union(self, other)

    def __and__(self, other: "PhenotypeExpr") -> "Intersect":
        return Intersect(self, other)

    def __invert__(self) -> "Not":
        return Not(self)

    def to_dict(self) -> dict:
        raise NotImplementedError


class Union(PhenotypeExpr):
    """Persons satisfying at least one operand."""

    def __init__(self, *children: PhenotypeExpr):
        if len(children) < 2:
            raise ValueError("Union needs at least two operands.")
        self.children = tuple(c for ch in children for c in (ch.children if isinstance(ch, Union) else (ch,)))

    def to_dict(self) -> dict:
        return {"type": "union", "criteria": [c.to_dict() for c in self.children]}


class Intersect(PhenotypeExpr):
    """Persons satisfying every operand."""

    def __init__(self, *children: PhenotypeExpr):
        if len(children) < 2:
            raise ValueError("Intersect needs at least two operands.")
        self.children = tuple(c for ch in children for c in (ch.children if isinstance(ch, Intersect) else (ch,)))

    def to_dict(self) -> dict:
        return {"type": "intersect", "criteria": [c.to_dict() for c in self.children]}


class Not(PhenotypeExpr):
    """Persons *not* satisfying the operand. Combine with ``&`` (or use at top level: ``Everyone() & ~x``)."""

    def __init__(self, child: PhenotypeExpr):
        self.child = child

    def to_dict(self) -> dict:
        return {"type": "not", "criteria": self.child.to_dict()}


# ----------------------------------------------------------------------------------------------
# leaves
# ----------------------------------------------------------------------------------------------
@dataclass(eq=False, kw_only=True)
class _Leaf(PhenotypeExpr):
    min_occurrences: int = 1
    lookback_days: int | None = None
    lookahead_days: int | None = None

    def _window_sql(self, event_expr: str) -> str:
        """Predicate restricting ``event_expr`` to the lookback/lookahead window around ``idx.index_date``."""
        if not self.windowed:
            return ""
        before = f" - {int(self.lookback_days)}" if self.lookback_days is not None else ""
        after = f" + {int(self.lookahead_days)}" if self.lookahead_days is not None else ""
        # lookback only -> [index - n, index]; lookahead only -> [index, index + m]; both -> [index - n, index + m]
        return f"{event_expr} >= idx.index_date{before} AND {event_expr} <= idx.index_date{after}"

    @property
    def windowed(self) -> bool:
        return self.lookback_days is not None or self.lookahead_days is not None

    def _validate(self) -> None:
        if int(self.min_occurrences) < 1:
            raise ValueError("min_occurrences must be >= 1.")
        for name in ("lookback_days", "lookahead_days"):
            v = getattr(self, name)
            if v is not None and int(v) < 0:
                raise ValueError(f"{name} must be >= 0.")


def _concept_set_sql(name: str, ids: Sequence[int], include_descendants: bool, ancestor_table: str,
                     code_seeds: tuple[str, Sequence[str], str] | None = None) -> str | None:
    """CTE body for a concept set, or None when empty.

    ``code_seeds`` = (vocabulary LIKE pattern, codes, concept_table) adds concepts looked up by code.
    """
    seeds = []
    if ids:
        seeds.append("SELECT * FROM (VALUES " + ", ".join(f"({i})" for i in _ints(ids)) + ") t(concept_id)")
    if code_seeds and code_seeds[1]:
        vocab, codes, concept_table = code_seeds
        seeds.append(f"SELECT concept_id FROM {concept_table} WHERE vocabulary_id LIKE {_q(vocab)} "
                     f"AND concept_code IN ({', '.join(_q(c) for c in codes)})")
    if not seeds:
        return None
    seed_sql = " UNION ".join(seeds)
    if not include_descendants:
        return f"{name} AS ({seed_sql})"
    return (f"{name} AS (SELECT concept_id FROM ({seed_sql}) UNION "
            f"SELECT descendant_concept_id FROM {ancestor_table} "
            f"WHERE ancestor_concept_id IN (SELECT concept_id FROM ({seed_sql})))")


@dataclass(eq=False, kw_only=True)
class ConditionCriteria(_Leaf):
    """Diagnosis criterion.

    Args:
        concept_ids: Standard condition concepts (e.g. SNOMED); descendants are included by default.
        icd_codes: ICD-9/10 code prefixes (``"E11"`` matches ``E11.9``), matched against
            ``condition_source_value`` and the source concept's code.
        icd_vocabularies: Vocabularies the source-concept match is restricted to.
        diagnosis: ``'any'``, ``'primary'`` or ``'secondary'`` (via ``condition_status_concept_id``).
        include_descendants: Expand ``concept_ids`` through ``concept_ancestor``.
        min_occurrences: Require at least this many qualifying records; the index date is the n-th one.
        lookback_days, lookahead_days: Window around the index table's date (needs ``index_table``).
    """

    concept_ids: Sequence[int] = ()
    icd_codes: Sequence[str] = ()
    icd_vocabularies: Sequence[str] = ("ICD9CM", "ICD10CM")
    diagnosis: str = "any"
    include_descendants: bool = True

    def to_dict(self) -> dict:
        return {"type": "condition", "concept_ids": list(self.concept_ids), "icd_codes": list(self.icd_codes),
                "icd_vocabularies": list(self.icd_vocabularies), "diagnosis": self.diagnosis,
                "include_descendants": self.include_descendants, "min_occurrences": self.min_occurrences,
                "lookback_days": self.lookback_days, "lookahead_days": self.lookahead_days}


@dataclass(eq=False, kw_only=True)
class MeasurementCriteria(_Leaf):
    """Lab/vital criterion with numeric thresholds.

    Args:
        concept_ids: Measurement concepts (LOINC-standard); descendants included by default.
        loinc_codes: LOINC codes, matched on ``measurement_source_value`` and the concept's code.
        value_gte, value_gt, value_lte, value_lt: Thresholds on ``value_as_number`` (all must hold).
        unit_concept_ids: Restrict to these units.
        include_descendants, min_occurrences, lookback_days, lookahead_days: As for :class:`ConditionCriteria`.
    """

    concept_ids: Sequence[int] = ()
    loinc_codes: Sequence[str] = ()
    value_gte: float | None = None
    value_gt: float | None = None
    value_lte: float | None = None
    value_lt: float | None = None
    unit_concept_ids: Sequence[int] = ()
    include_descendants: bool = False

    def to_dict(self) -> dict:
        d = {"type": "measurement"}
        d.update({k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in self.__dict__.items()})
        return d


@dataclass(eq=False, kw_only=True)
class DrugCriteria(_Leaf):
    """Drug-exposure criterion.

    Args:
        concept_ids: RxNorm ingredient (or ATC class) concepts; descendants included by default.
        atc_codes: ATC codes resolved through ``concept`` as additional ancestor concepts.
        min_days_supply: Require ``days_supply`` (or end - start when unset) of at least this many days.
        include_descendants, min_occurrences, lookback_days, lookahead_days: As for :class:`ConditionCriteria`.
    """

    concept_ids: Sequence[int] = ()
    atc_codes: Sequence[str] = ()
    min_days_supply: int | None = None
    include_descendants: bool = True

    def to_dict(self) -> dict:
        d = {"type": "drug"}
        d.update({k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in self.__dict__.items()})
        return d


@dataclass(eq=False)
class Everyone(PhenotypeExpr):
    """All persons (index = start of observation, or the index table's date). Base population for ``~x``."""

    def to_dict(self) -> dict:
        return {"type": "everyone"}


# ----------------------------------------------------------------------------------------------
# compiler
# ----------------------------------------------------------------------------------------------
_LEAF_TYPES = (ConditionCriteria, MeasurementCriteria, DrugCriteria, Everyone)


def _collect_leaves(node: PhenotypeExpr, out: list) -> None:
    if isinstance(node, _LEAF_TYPES):
        if not any(node is x for x in out):
            out.append(node)
    elif isinstance(node, (Union, Intersect)):
        for c in node.children:
            _collect_leaves(c, out)
    elif isinstance(node, Not):
        _collect_leaves(node.child, out)
    else:
        raise TypeError(f"Unsupported phenotype node: {type(node).__name__}")


def compile_phenotype(
    expr: PhenotypeExpr,
    *,
    cohort_definition_id: int = 1,
    index_table: str | None = None,
    index_person_col: str = "subject_id",
    index_date_col: str = "cohort_start_date",
    concept_ancestor: str = "concept_ancestor",
    concept: str = "concept",
) -> str:
    """Compile a phenotype expression to one DuckDB ``SELECT``.

    Returns SQL yielding ``(cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)``; the end
    date equals the start date (an event cohort). No connection is needed - table names are parameters.
    """
    if isinstance(expr, Not):  # top-level NOT needs a base population
        expr = Intersect(Everyone(), expr)

    leaves: list = []
    _collect_leaves(expr, leaves)
    position = {id(leaf): k for k, leaf in enumerate(leaves)}
    ctes: list[str] = []
    selects: list[str] = []
    has_index = index_table is not None

    if has_index:
        ctes.append(f"idx AS (SELECT {index_person_col} AS person_id, MIN(CAST({index_date_col} AS DATE)) "
                    f"AS index_date FROM {index_table} GROUP BY 1)")

    for k, leaf in enumerate(leaves):
        if isinstance(leaf, Everyone):
            selects.append(
                "SELECT person_id, index_date AS event_date, %d AS leaf FROM idx" % k if has_index else
                "SELECT person_id, MIN(observation_period_start_date) AS event_date, %d AS leaf "
                "FROM observation_period GROUP BY person_id" % k)
            continue
        leaf._validate()
        if leaf.windowed and not has_index:
            raise ValueError("lookback_days / lookahead_days are relative to an index date: "
                             "pass index_table=... to compile_phenotype().")
        join_idx = " JOIN idx ON idx.person_id = t.person_id" if leaf.windowed else ""
        where: list[str] = []
        extra_join = ""

        if isinstance(leaf, ConditionCriteria):
            if leaf.diagnosis not in ("any", "primary", "secondary"):
                raise ValueError("diagnosis must be 'any', 'primary' or 'secondary'.")
            table, date_col, person = "condition_occurrence", "t.condition_start_date", "t.person_id"
            ors = []
            cs = _concept_set_sql(f"cs{k}", leaf.concept_ids, leaf.include_descendants, concept_ancestor)
            if cs:
                ctes.append(cs)
                ors.append(f"t.condition_concept_id IN (SELECT concept_id FROM cs{k})")
            if leaf.icd_codes:
                extra_join = (f" LEFT JOIN {concept} sc ON sc.concept_id = t.condition_source_concept_id "
                              f"AND sc.vocabulary_id IN ({', '.join(_q(v) for v in leaf.icd_vocabularies)})")
                for code in leaf.icd_codes:
                    ors.append(f"t.condition_source_value LIKE {_q(str(code) + '%')}")
                    ors.append(f"sc.concept_code LIKE {_q(str(code) + '%')}")
            if not ors:
                raise ValueError("ConditionCriteria needs concept_ids and/or icd_codes.")
            where.append("(" + " OR ".join(ors) + ")")
            if leaf.diagnosis != "any":
                status = _PRIMARY_STATUS if leaf.diagnosis == "primary" else _SECONDARY_STATUS
                where.append(f"t.condition_status_concept_id IN ({', '.join(map(str, status))})")

        elif isinstance(leaf, MeasurementCriteria):
            table, date_col, person = "measurement", "t.measurement_date", "t.person_id"
            ors = []
            cs = _concept_set_sql(f"cs{k}", leaf.concept_ids, leaf.include_descendants, concept_ancestor)
            if cs:
                ctes.append(cs)
                ors.append(f"t.measurement_concept_id IN (SELECT concept_id FROM cs{k})")
            if leaf.loinc_codes:
                codes = ", ".join(_q(c) for c in leaf.loinc_codes)
                extra_join = f" LEFT JOIN {concept} mc ON mc.concept_id = t.measurement_concept_id"
                ors.append(f"t.measurement_source_value IN ({codes})")
                ors.append(f"(mc.vocabulary_id = 'LOINC' AND mc.concept_code IN ({codes}))")
            if not ors:
                raise ValueError("MeasurementCriteria needs concept_ids and/or loinc_codes.")
            where.append("(" + " OR ".join(ors) + ")")
            for attr, op in (("value_gte", ">="), ("value_gt", ">"), ("value_lte", "<="), ("value_lt", "<")):
                v = getattr(leaf, attr)
                if v is not None:
                    where.append(f"t.value_as_number {op} {float(v)!r}")
            if leaf.unit_concept_ids:
                where.append(f"t.unit_concept_id IN ({', '.join(map(str, _ints(leaf.unit_concept_ids)))})")

        else:  # DrugCriteria
            table, date_col, person = "drug_exposure", "t.drug_exposure_start_date", "t.person_id"
            cs = _concept_set_sql(f"cs{k}", leaf.concept_ids, leaf.include_descendants, concept_ancestor,
                                  ("ATC%", leaf.atc_codes, concept) if leaf.atc_codes else None)
            if not cs:
                raise ValueError("DrugCriteria needs concept_ids and/or atc_codes.")
            ctes.append(cs)
            where.append(f"t.drug_concept_id IN (SELECT concept_id FROM cs{k})")
            if leaf.min_days_supply is not None:
                where.append("COALESCE(t.days_supply, date_diff('day', t.drug_exposure_start_date, "
                             f"t.drug_exposure_end_date)) >= {int(leaf.min_days_supply)}")

        window = leaf._window_sql(date_col)
        if window:
            where.append(window)
        selects.append(
            f"SELECT {person} AS person_id, CAST({date_col} AS DATE) AS event_date, {k} AS leaf "
            f"FROM {table} t{extra_join}{join_idx} WHERE {' AND '.join(where)}")

    ctes.append("events AS (\n    " + "\n    UNION ALL\n    ".join(selects) + "\n)")
    agg_cols = []
    for k, leaf in enumerate(leaves):
        n = int(getattr(leaf, "min_occurrences", 1))
        if n == 1:
            agg_cols.append(f"MIN(event_date) FILTER (WHERE leaf = {k}) AS d{k}")
        else:
            agg_cols.append(f"(array_agg(event_date ORDER BY event_date) FILTER (WHERE leaf = {k}))[{n}] AS d{k}")
    ctes.append("agg AS (SELECT person_id, " + ", ".join(agg_cols) + " FROM events GROUP BY person_id)")

    def truth(node: PhenotypeExpr) -> str:
        if isinstance(node, _LEAF_TYPES):
            return f"a.d{position[id(node)]} IS NOT NULL"
        if isinstance(node, Union):
            return "(" + " OR ".join(truth(c) for c in node.children) + ")"
        if isinstance(node, Intersect):
            return "(" + " AND ".join(truth(c) for c in node.children) + ")"
        return f"(NOT {truth(node.child)})"

    def date_of(node: PhenotypeExpr) -> str | None:
        """Date expression or None when the node carries no date (Not / Everyone)."""
        if isinstance(node, Everyone) or isinstance(node, Not):
            return None
        if isinstance(node, _LEAF_TYPES):
            return f"a.d{position[id(node)]}"
        parts = []
        for c in node.children:
            d = date_of(c)
            if d is None:
                continue
            # a composite branch must be satisfied for its date to count toward a Union's earliest date
            if isinstance(node, Union) and not isinstance(c, _LEAF_TYPES):
                d = f"(CASE WHEN {truth(c)} THEN {d} END)"
            parts.append(d)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return ("LEAST(" if isinstance(node, Union) else "GREATEST(") + ", ".join(parts) + ")"

    def fallback_of(node: PhenotypeExpr) -> str | None:
        for leaf in leaves:
            if isinstance(leaf, Everyone):
                return f"a.d{position[id(leaf)]}"
        return None

    start = date_of(expr)
    fb = fallback_of(expr)
    start = f"COALESCE({start}, {fb})" if (start and fb) else (start or fb)
    if start is None:
        raise ValueError("Phenotype has no criterion that yields an index date.")
    if has_index:
        start = "idx.index_date"
    join = " JOIN idx ON idx.person_id = a.person_id" if has_index else ""
    return (
        "WITH " + ",\n".join(ctes) + "\n"
        f"SELECT {int(cohort_definition_id)} AS cohort_definition_id, a.person_id AS subject_id, "
        f"CAST({start} AS DATE) AS cohort_start_date, CAST({start} AS DATE) AS cohort_end_date\n"
        f"FROM agg a{join}\nWHERE {truth(expr)} AND {start} IS NOT NULL"
    )


def execute_phenotype(
    con,
    expr: PhenotypeExpr,
    cohort_table: str = "cohort",
    cohort_definition_id: int = 1,
    *,
    replace: bool = True,
    **compile_kwargs: Any,
) -> pd.DataFrame:
    """Compile and run a phenotype, writing rows to ``cohort_table`` and returning them.

    ``replace=True`` first deletes any existing rows of the same ``cohort_definition_id``. The vocabulary
    tables are looked up on the connection (main schema, then the attached central vocabulary) unless
    ``concept_ancestor=`` / ``concept=`` are passed explicitly.
    """
    from omop_etl.medication_classes import _resolve_table

    leaves: list = []
    _collect_leaves(Intersect(Everyone(), expr) if isinstance(expr, Not) else expr, leaves)
    if "concept_ancestor" not in compile_kwargs and any(
            getattr(x, "include_descendants", False) and getattr(x, "concept_ids", ()) for x in leaves):
        compile_kwargs["concept_ancestor"] = _resolve_table(con, "concept_ancestor")
    if "concept" not in compile_kwargs and any(
            getattr(x, "atc_codes", ()) or getattr(x, "loinc_codes", ()) or getattr(x, "icd_codes", ())
            for x in leaves):
        try:
            compile_kwargs["concept"] = _resolve_table(con, "concept")
        except ValueError:
            compile_kwargs["concept"] = "concept"

    sql = compile_phenotype(expr, cohort_definition_id=cohort_definition_id, **compile_kwargs)
    con.execute(f"CREATE TABLE IF NOT EXISTS {cohort_table} (cohort_definition_id INTEGER, subject_id BIGINT, "
                "cohort_start_date DATE, cohort_end_date DATE)")
    if replace:
        con.execute(f"DELETE FROM {cohort_table} WHERE cohort_definition_id = {int(cohort_definition_id)}")
    con.execute(f"INSERT INTO {cohort_table} {sql}")
    return con.execute(
        f"SELECT * FROM {cohort_table} WHERE cohort_definition_id = {int(cohort_definition_id)} "
        "ORDER BY subject_id").df()


def phenotype_from_dict(spec: Mapping[str, Any]) -> PhenotypeExpr:
    """Rebuild an expression from :meth:`PhenotypeExpr.to_dict` output (e.g. loaded from JSON)."""
    kind = spec.get("type")
    body = {k: v for k, v in spec.items() if k != "type"}
    if kind == "union":
        return Union(*[phenotype_from_dict(c) for c in body["criteria"]])
    if kind == "intersect":
        return Intersect(*[phenotype_from_dict(c) for c in body["criteria"]])
    if kind == "not":
        return Not(phenotype_from_dict(body["criteria"]))
    if kind == "everyone":
        return Everyone()
    leaf_types = {"condition": ConditionCriteria, "measurement": MeasurementCriteria, "drug": DrugCriteria}
    if kind not in leaf_types:
        raise ValueError(f"Unknown phenotype node type {kind!r}.")
    return leaf_types[kind](**body)
