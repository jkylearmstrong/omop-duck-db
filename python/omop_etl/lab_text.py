"""What a laboratory row without a number holds (0.35.2, Kyle's C20r list): a censored result ("<0.01", ">14.0"), a number with a
flag the converter does not read ("7.5 H"), a qualitative result ("POSITIVE", "not detected"), a comment, or nothing at all.

:func:`lab_text_profile` counts these classes per laboratory, from ``measurement.value_source_value`` (the PCORnet converter keeps
``RESULT_NUM``, else ``RAW_RESULT``, there), so a study team can tell a source gap from a value the ETL missed. It returns shares
only, never the text itself. :data:`LAB_TEXT_CLASSES` lists the classes; :func:`lab_text_class_sql` is the SQL expression.

:func:`backfill_lab_values` (0.35.3, run C21) repairs a database whose builder read only ``RESULT_NUM``: it fills
``value_as_number`` (and ``operator_concept_id`` for a censored limit) from the raw ``LAB_RESULT_CM`` file.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd

__all__ = ["LAB_TEXT_CLASSES", "LAB_TEXT_PARSEABLE", "CENSORED_OPERATORS", "backfill_lab_values", "lab_text_class_sql",
           "lab_text_profile"]

#: the classes, in the order they are tested
LAB_TEXT_CLASSES = ("blank", "number", "censored", "number_with_flag", "percent", "range", "qualitative", "other_text")
#: classes holding a number the converter's LAB_VALUE_SQL would read ("number", "censored": the operator is dropped) or that a
#: parser could read ("number_with_flag", "percent")
LAB_TEXT_PARSEABLE = ("number", "censored", "number_with_flag", "percent")
#: OMOP Meas Value Operator concepts set by LAB_OPERATOR_SQL
CENSORED_OPERATORS = {4171756: "<", 4171754: "<=", 4172703: "=", 4171755: ">=", 4172704: ">"}

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


_DATE_FORMATS = ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d")


def _date_sql(cols: Sequence[str], alias: str = "r") -> str:
    """The first column that parses as a date (the formats of the PCORnet converter's ``parse_omop_date``), inline so the
    expression needs no macro and runs on a read-only connection."""
    one = lambda c: ("COALESCE(TRY_CAST(" + c + " AS DATE), " + ", ".join(f"CAST(TRY_STRPTIME({c}, '{f}') AS DATE)"
                                                                          for f in _DATE_FORMATS) + ")")
    return "COALESCE(" + ", ".join(one(f"NULLIF(trim({alias}.{c}), '')") for c in cols) + ")"


def _hhmm_sql(time_col: str, alias: str = "r") -> str:
    """'HH:MM' of a PCORnet time text ('4:15', '04:15:00', '0415'); NULL when blank or unreadable."""
    t = f"NULLIF(trim({alias}.{time_col}), '')"
    return (f"CASE WHEN regexp_full_match({t}, '\\d{{1,2}}:\\d{{2}}(:\\d{{2}})?') "
            f"THEN lpad(regexp_extract({t}, '^(\\d{{1,2}}):', 1), 2, '0') || ':' || regexp_extract({t}, '^\\d{{1,2}}:(\\d{{2}})', 1) "
            f"WHEN regexp_full_match({t}, '\\d{{3,4}}') THEN substr(lpad({t}, 4, '0'), 1, 2) || ':' || substr(lpad({t}, 4, '0'), 3, 2) END")


@contextlib.contextmanager
def _open_raw_relation(path_like) -> Iterator[tuple[str, Path]]:
    """Yields ``(duckdb_from_sql, parent_dir)`` for a ``.csv``, ``.parquet``, or single-file ``.zip`` archive."""
    from omop_etl.paths import sql_path_literal

    p = Path(os.fspath(path_like))
    if p.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="omop_raw_") as td:
            with zipfile.ZipFile(p) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/") and m.lower().endswith((".csv", ".parquet"))]
                if not members:
                    raise ValueError(f"{path_like!r} contains no .csv or .parquet entry.")
                extracted = Path(zf.extract(members[0], path=td))
            if extracted.suffix.lower() == ".parquet":
                rel = f"(SELECT COLUMNS(*)::VARCHAR FROM read_parquet({sql_path_literal(str(extracted))}))"
            else:
                rel = f"read_csv_auto({sql_path_literal(str(extracted))}, all_varchar = true, header = true, ignore_errors = true)"
            yield rel, p.parent
    elif p.suffix.lower() == ".parquet":
        yield f"(SELECT COLUMNS(*)::VARCHAR FROM read_parquet({sql_path_literal(str(p))}))", p.parent
    else:
        yield f"read_csv_auto({sql_path_literal(str(p))}, all_varchar = true, header = true, ignore_errors = true)", p.parent


def _resolve_col(have: dict[str, str], canonical: str, aliases: Sequence[str] = ()) -> str | None:
    from omop_etl.build_omop_cdm import DEFAULT_ALIASES

    for cand in list(aliases) + list(DEFAULT_ALIASES.get(canonical, [canonical])):
        if cand.upper() in have:
            return have[cand.upper()]
    return None


def backfill_lab_values(con, lab_result_cm, *, names: Sequence[str] | None = None, concept_sets: Any = None,
                        start: str | None = None, end: str | None = None, dry_run: bool = False,
                        stay_concepts: Sequence[int] = (9201, 262), crosswalk: Any = None) -> pd.DataFrame:
    """Fills ``value_as_number`` of laboratory rows that have none from the raw PCORnet ``LAB_RESULT_CM`` file (0.35.3, run C21:
    a site builder read only ``RESULT_NUM``, so a result held as text in ``RAW_RESULT``, such as a censored "<0.01", was lost).

    The value is the PCORnet converter's own (:data:`omop_etl.build_omop_cdm.LAB_VALUE_SQL`): RESULT_NUM, else RAW_RESULT when it
    is a number with an optional leading operator. A censored result is stored at its limit with the operator in
    ``operator_concept_id`` (:data:`omop_etl.build_omop_cdm.LAB_OPERATOR_SQL`), the OMOP convention, so a feature can tell "<0.01"
    from 0.01. A qualitative or other text result stays NULL.

    Matching: the database keeps no ``LAB_RESULT_CM_ID``, so a row is matched to the raw rows without a number in RESULT_NUM of the
    same patient (``PATID`` or its converter aliases ``SSID`` / ``PAT_ID`` / ``PERSON_ID`` matched to ``person.person_source_value``,
    with an optional ``PATID``-``ssid`` ``crosswalk`` CSV and ``ENCOUNTERID`` -> ``visit_occurrence.visit_source_value`` fallback),
    LOINC (the row's ``measurement_source_value``, or the LOINC code of its ``measurement_concept_id``) and date (RESULT_DATE, else
    SPECIMEN_DATE, else LAB_ORDER_DATE). When the row and raw rows both have a time, raw rows at the same minute are used first. A
    row is filled only when every matching raw row gives the same value and operator (duplicated raw rows are common, so this is
    looser than the C14 fill's single match, and never guesses between two different values).

    ``names``: laboratory concept-set names (default every set in ``concept_sets``, the package's benchmark laboratory sets).
    ``start`` / ``end``: limit to rows dated in that range. ``dry_run=True`` writes nothing (temporary tables only), so it runs on
    a read-only connection. Otherwise the connection must be writable; work on a copy.
    """
    import warnings

    from omop_etl.benchmark_features import BENCHMARK_LAB_CONCEPT_SETS, _concept_map, _ensure_vocabulary
    from omop_etl.build_omop_cdm import LAB_OPERATOR_SQL, LAB_VALUE_SQL
    from omop_etl.paths import sql_path_literal

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _ensure_vocabulary(con, "auto")
    sets = concept_sets or BENCHMARK_LAB_CONCEPT_SETS
    cmap = _concept_map(con, None, None, {n: sets[n] for n in (names or list(sets)) if n in sets})
    cmap = cmap[["name", "concept_id"]].drop_duplicates()
    cols = ["name", "null_rows", "filled", "filled_censored", "filled_in_stay", "ambiguous", "not_parseable", "no_match", "pct_filled"]
    if cmap.empty:
        return pd.DataFrame(columns=cols)

    with _open_raw_relation(lab_result_cm) as (src, parent_dir):
        have = {r[0].upper(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src} LIMIT 0").fetchall()}
        pat_col = _resolve_col(have, "PATID")
        eid_col = _resolve_col(have, "ENCOUNTERID")
        loinc_col = _resolve_col(have, "LAB_LOINC")
        rnum_col = _resolve_col(have, "RESULT_NUM")
        if pat_col is None and eid_col is None:
            raise ValueError(f"{lab_result_cm!r} has neither a PATID/SSID column nor an ENCOUNTERID column.")
        if loinc_col is None:
            raise ValueError(f"{lab_result_cm!r} has no LAB_LOINC column.")
        if rnum_col is None:
            raise ValueError(f"{lab_result_cm!r} has no RESULT_NUM column.")
        date_cols = [have[c] for c in ("RESULT_DATE", "SPECIMEN_DATE", "LAB_ORDER_DATE") if c in have]
        if not date_cols:
            raise ValueError(f"{lab_result_cm!r} has no RESULT_DATE, SPECIMEN_DATE or LAB_ORDER_DATE column.")
        raw_col = _resolve_col(have, "RAW_RESULT", ("RAW_RESULT",))
        value_sql = LAB_VALUE_SQL.replace("src.RESULT_NUM", f"r.{rnum_col}").replace(
            "src.RAW_RESULT", f"r.{raw_col}" if raw_col else "CAST(NULL AS VARCHAR)")
        op_sql = LAB_OPERATOR_SQL.replace("src.RESULT_NUM", f"r.{rnum_col}").replace(
            "src.RAW_RESULT", f"r.{raw_col}" if raw_col else "CAST(NULL AS VARCHAR)")
        hhmm = _hhmm_sql(have["RESULT_TIME"]) if "RESULT_TIME" in have else "CAST(NULL AS VARCHAR)"

        mcols = {r[0].lower() for r in con.execute("DESCRIBE SELECT * FROM measurement LIMIT 0").fetchall()}
        has_op = "operator_concept_id" in mcols
        has_dt = "measurement_datetime" in mcols
        tables_lower = {r[0].lower() for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        has_vo = "visit_occurrence" in tables_lower
        vcols = ({r[0].lower() for r in con.execute("DESCRIBE SELECT * FROM visit_occurrence LIMIT 0").fetchall()}
                 if has_vo else set())
        can_eid = bool(eid_col and has_vo and "visit_source_value" in vcols)

        where = "AND m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)" if start and end else ""
        params = [start, end] if where else []
        con.register("_blv_map", cmap)
        try:
            con.execute(f"""CREATE OR REPLACE TEMP TABLE _blv_t AS
                SELECT DISTINCT m.measurement_id AS mid, k.name, m.person_id AS pid,
                       m.measurement_date AS dt, upper(trim(CAST(m.measurement_source_value AS VARCHAR))) AS src_loinc,
                       upper(c.concept_code) AS code_loinc,
                       {"CASE WHEN CAST(m.measurement_datetime AS TIME) <> TIME '00:00:00' THEN strftime(m.measurement_datetime, '%H:%M') END"
                        if has_dt else "CAST(NULL AS VARCHAR)"} AS hhmm
                FROM measurement m JOIN _blv_map k ON k.concept_id = m.measurement_concept_id
                LEFT JOIN concept c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
                WHERE m.value_as_number IS NULL {where}""", params)

            # Build patient lookup table (_blv_pmap: raw_key -> person_id) from person.person_source_value and optional crosswalk
            con.execute("""CREATE OR REPLACE TEMP TABLE _blv_pmap AS
                SELECT DISTINCT trim(CAST(person_source_value AS VARCHAR)) AS rk, person_id AS pid
                FROM person WHERE person_source_value IS NOT NULL AND trim(CAST(person_source_value AS VARCHAR)) <> ''""")
            xw_path = Path(os.fspath(crosswalk)) if crosswalk is not None else (parent_dir / "crosswalk.csv")
            if pat_col and xw_path.is_file():
                xsrc = f"read_csv_auto({sql_path_literal(str(xw_path))}, all_varchar = true, header = true, ignore_errors = true)"
                xhave = {r[0].upper(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {xsrc} LIMIT 0").fetchall()}
                if "PATID" in xhave and "SSID" in xhave:
                    con.execute(f"""INSERT INTO _blv_pmap
                        SELECT DISTINCT trim(x.{xhave['SSID']}) AS rk, pm.pid
                        FROM {xsrc} x JOIN _blv_pmap pm ON pm.rk = trim(x.{xhave['PATID']})
                        WHERE trim(x.{xhave['SSID']}) NOT IN (SELECT rk FROM _blv_pmap)
                        UNION
                        SELECT DISTINCT trim(x.{xhave['PATID']}) AS rk, pm.pid
                        FROM {xsrc} x JOIN _blv_pmap pm ON pm.rk = trim(x.{xhave['SSID']})
                        WHERE trim(x.{xhave['PATID']}) NOT IN (SELECT rk FROM _blv_pmap)""")

            pid_joins = []
            pid_exprs = []
            if pat_col:
                pid_joins.append(f"LEFT JOIN _blv_pmap pm ON pm.rk = trim(r.{pat_col})")
                pid_exprs.append("pm.pid")
            if can_eid:
                con.execute("""CREATE OR REPLACE TEMP TABLE _blv_vmap AS
                    SELECT trim(CAST(visit_source_value AS VARCHAR)) AS eid, min(person_id) AS pid
                    FROM visit_occurrence
                    WHERE visit_source_value IS NOT NULL AND trim(CAST(visit_source_value AS VARCHAR)) <> ''
                    GROUP BY 1 HAVING count(DISTINCT person_id) = 1""")
                pid_joins.append(f"LEFT JOIN _blv_vmap vm ON vm.eid = trim(r.{eid_col})")
                pid_exprs.append("vm.pid")
            pid_sql = f"COALESCE({', '.join(pid_exprs)})" if len(pid_exprs) > 1 else pid_exprs[0]

            con.execute(f"""CREATE OR REPLACE TEMP TABLE _blv_r AS
                WITH codes AS (SELECT src_loinc AS l FROM _blv_t WHERE src_loinc IS NOT NULL
                               UNION SELECT code_loinc FROM _blv_t WHERE code_loinc IS NOT NULL)
                SELECT {pid_sql} AS pid, upper(trim(r.{loinc_col})) AS loinc, {_date_sql(date_cols)} AS dt,
                       {hhmm} AS hhmm, {value_sql} AS val, {op_sql} AS op
                FROM {src} r
                {' '.join(pid_joins)}
                WHERE upper(trim(r.{loinc_col})) IN (SELECT l FROM codes)
                  AND TRY_CAST(r.{rnum_col} AS DOUBLE) IS NULL
                  AND {pid_sql} IS NOT NULL""")
            con.execute("""CREATE OR REPLACE TEMP TABLE _blv_pick AS
                WITH pairs AS (
                    SELECT DISTINCT t.mid, t.name, r.val, r.op, (t.hhmm IS NOT NULL AND t.hhmm = r.hhmm) AS same_min, r.rowid_ AS rid
                    FROM _blv_t t JOIN (SELECT *, row_number() OVER () AS rowid_ FROM _blv_r) r
                      ON r.pid = t.pid AND r.dt = t.dt AND (r.loinc = t.src_loinc OR r.loinc = t.code_loinc)),
                lvl AS (SELECT mid, name, bool_or(same_min) AS any_min FROM pairs GROUP BY mid, name),
                use AS (SELECT p.* FROM pairs p JOIN lvl USING (mid, name) WHERE NOT lvl.any_min OR p.same_min)
                SELECT mid, name, count(*) AS n, count(*) FILTER (WHERE val IS NULL) AS n_null,
                       count(DISTINCT CAST(val AS VARCHAR) || '|' || CAST(op AS VARCHAR)) AS n_vals, max(val) AS val, max(op) AS op
                FROM use GROUP BY mid, name""")
            stays = ", ".join(str(int(c)) for c in stay_concepts) or "NULL"
            in_stay = (("EXISTS (SELECT 1 FROM measurement m JOIN visit_occurrence v ON v.person_id = m.person_id "
                        f"WHERE m.measurement_id = t.mid AND v.visit_concept_id IN ({stays}) "
                        "AND t.dt BETWEEN v.visit_start_date - 1 AND coalesce(v.visit_end_date, v.visit_start_date))")
                       if has_vo else "FALSE")
            out = con.execute(f"""
                SELECT t.name, count(*) AS null_rows,
                       count(*) FILTER (WHERE k.n_null = 0 AND k.n_vals = 1) AS filled,
                       count(*) FILTER (WHERE k.n_null = 0 AND k.n_vals = 1 AND k.op <> 0) AS filled_censored,
                       count(*) FILTER (WHERE k.n_null = 0 AND k.n_vals = 1 AND t.in_stay) AS filled_in_stay,
                       count(*) FILTER (WHERE k.n_null < k.n AND NOT (k.n_null = 0 AND k.n_vals = 1)) AS ambiguous,
                       count(*) FILTER (WHERE k.n_null = k.n) AS not_parseable,
                       count(*) FILTER (WHERE k.mid IS NULL) AS no_match
                FROM (SELECT DISTINCT t.mid, t.name, {in_stay} AS in_stay FROM _blv_t t) t LEFT JOIN _blv_pick k USING (mid, name)
                GROUP BY t.name ORDER BY t.name""").df()
            if not dry_run:
                con.execute("""CREATE OR REPLACE TEMP TABLE _blv_fill AS
                    SELECT mid, any_value(val) AS val, any_value(op) AS op FROM _blv_pick WHERE n_null = 0 AND n_vals = 1
                    GROUP BY mid HAVING count(DISTINCT CAST(val AS VARCHAR) || '|' || CAST(op AS VARCHAR)) = 1""")
                op_set = (", operator_concept_id = CASE WHEN coalesce(measurement.operator_concept_id, 0) = 0 THEN f.op "
                          "ELSE measurement.operator_concept_id END") if has_op else ""
                con.execute(f"UPDATE measurement SET value_as_number = f.val{op_set} FROM _blv_fill f "
                            "WHERE measurement.measurement_id = f.mid AND measurement.value_as_number IS NULL")
                con.execute("DROP TABLE IF EXISTS _blv_fill")
        finally:
            con.unregister("_blv_map")
            for t in ("_blv_t", "_blv_pmap", "_blv_vmap", "_blv_r", "_blv_pick"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
    for c in cols[1:-1]:
        out[c] = out[c].astype("int64")
    out["pct_filled"] = (100.0 * out["filled"] / out["null_rows"].where(out["null_rows"] > 0)).round(1)
    return out[cols]
