"""What a laboratory row without a number holds (0.35.2, Kyle's C20r list): a censored result ("<0.01", ">14.0"), a number with a
flag the converter does not read ("7.5 H"), a qualitative result ("POSITIVE", "not detected"), a comment, or nothing at all.

:func:`lab_text_profile` counts these classes per laboratory, from ``measurement.value_source_value`` (the PCORnet converter keeps
``RESULT_NUM``, else ``RAW_RESULT``, there), so a study team can tell a source gap from a value the ETL missed. It returns shares
only, never the text itself. :data:`LAB_TEXT_CLASSES` lists the classes; :func:`lab_text_class_sql` is the SQL expression.
"""
from __future__ import annotations

from typing import Any, Sequence

import pandas as pd

__all__ = ["LAB_TEXT_CLASSES", "LAB_TEXT_PARSEABLE", "lab_text_class_sql", "lab_text_profile"]

#: the classes, in the order they are tested
LAB_TEXT_CLASSES = ("blank", "number", "censored", "number_with_flag", "percent", "range", "qualitative", "other_text")
#: classes holding a number the converter's LAB_VALUE_SQL would read ("number", "censored": the operator is dropped) or that a
#: parser could read ("number_with_flag", "percent")
LAB_TEXT_PARSEABLE = ("number", "censored", "number_with_flag", "percent")

_QUALITATIVE = ("pos", "neg", "detected", "not detected", "nondetected", "reactive", "nonreactive", "non-reactive", "trace", "normal",
                "abnormal", "present", "absent", "see note", "see comment", "cancelled", "canceled", "pending", "hemolyzed", "tnp",
                "qns", "invalid", "indeterminate", "equivocal")


def lab_text_class_sql(expr: str) -> str:
    """A DuckDB CASE expression giving the :data:`LAB_TEXT_CLASSES` class of the text ``expr``."""
    t = f"lower(trim(CAST({expr} AS VARCHAR)))"
    qual = " OR ".join(f"{t} LIKE '{w}%'" for w in _QUALITATIVE)
    num = r"[+-]?(\d+(\.\d*)?|\.\d+)"
    return (f"CASE WHEN {expr} IS NULL OR {t} = '' THEN 'blank' "
            f"WHEN regexp_full_match(replace(replace({t}, ',', ''), ' ', ''), '={{0,1}}{num}') THEN 'number' "
            f"WHEN regexp_full_match(replace({t}, ' ', ''), '(<|>|<=|>=|=<|=>){num}') THEN 'censored' "
            f"WHEN regexp_full_match({t}, '{num}\\s*(\\(?[a-z*!]{{1,3}}\\)?)') THEN 'number_with_flag' "
            f"WHEN regexp_full_match({t}, '{num}\\s*%') THEN 'percent' "
            f"WHEN regexp_full_match({t}, '{num}\\s*(-|to)\\s*{num}') THEN 'range' "
            f"WHEN {qual} THEN 'qualitative' ELSE 'other_text' END")


def lab_text_profile(con, names: Sequence[str] = ("hba1c", "lactate", "troponin_i"), *, start: str | None = None,
                     end: str | None = None, concept_sets: Any = None) -> pd.DataFrame:
    """Per laboratory (benchmark concept-set name): ``rows`` (measurement rows with the set's concepts, dated in ``start``..``end``
    when given), ``pct_no_number`` (value_as_number NULL), and for those rows the share in each :data:`LAB_TEXT_CLASSES` class
    (``pct_<class>``) and ``pct_parseable`` (the classes in :data:`LAB_TEXT_PARSEABLE`). Shares are of the rows without a number,
    rounded to 1 decimal. ``concept_sets`` defaults to the package's benchmark laboratory sets."""
    import warnings

    from omop_etl.benchmark_features import BENCHMARK_LAB_CONCEPT_SETS, _concept_map, _ensure_vocabulary

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _ensure_vocabulary(con, "auto")
    cmap = _concept_map(con, None, None, concept_sets or BENCHMARK_LAB_CONCEPT_SETS)
    cmap = cmap[cmap["name"].isin(list(names))][["name", "concept_id"]].drop_duplicates()
    con.register("_lt_map", cmap)
    where = "AND m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)" if start and end else ""
    cls = lab_text_class_sql("m.value_source_value")
    shares = ", ".join(f"round(100.0 * count(*) FILTER (WHERE x IS NULL AND c = '{k}') / nullif(count(*) FILTER (WHERE x IS NULL), 0), 1) "
                       f"AS pct_{k}" for k in LAB_TEXT_CLASSES)
    parse = " OR ".join(f"c = '{k}'" for k in LAB_TEXT_PARSEABLE)
    try:
        out = con.execute(f"""
            WITH r AS (SELECT k.name, m.value_as_number AS x, {cls} AS c
                       FROM measurement m JOIN _lt_map k ON k.concept_id = m.measurement_concept_id WHERE TRUE {where})
            SELECT name, count(*) AS rows, round(100.0 * count(*) FILTER (WHERE x IS NULL) / count(*), 1) AS pct_no_number, {shares},
                   round(100.0 * count(*) FILTER (WHERE x IS NULL AND ({parse})) / nullif(count(*) FILTER (WHERE x IS NULL), 0), 1) AS pct_parseable
            FROM r GROUP BY name ORDER BY name""", [start, end] if where else []).df()
    finally:
        con.unregister("_lt_map")
    return out
