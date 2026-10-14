"""Prior healthcare utilization features (the strongest single family of readmission predictors).

:func:`extract_prior_utilization` counts, for every cohort row, the visits that precede the index admission: inpatient
(IP), emergency (ED) and outpatient (OS) counts, cumulative length of stay, days since the last encounter and the number
of prior 30-day readmissions. The look-back is strictly before the admission date, so nothing from the index stay (or
anything later) can leak into the features.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

__all__ = ["DEFAULT_VISIT_CONCEPT_GROUPS", "extract_prior_utilization"]

# 9201 inpatient, 9203 emergency room, 9202 outpatient, 262 emergency room + inpatient (counts as both IP and ED).
DEFAULT_VISIT_CONCEPT_GROUPS: dict[str, tuple[int, ...]] = {
    "ip": (9201, 262),
    "ed": (9203, 262),
    "os": (9202,),
}

_GROUP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def extract_prior_utilization(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    cohort_id: int | None = None,
    lookback_days: int = 365,
    anchor_date: str = "admission",
    visit_concept_groups: Mapping[str, Sequence[int]] | None = None,
    readmission_gap_days: int = 30,
    readmission_group: str = "ip",
    format: str = "df",
) -> Any:
    """Prior IP / ED / OS utilization per cohort row, strictly before the index admission.

    Output columns, one row per distinct (``subject_id``, ``cohort_start_date``) ordered by both:

    * ``prior_<group>_count``: visits of that group starting in ``[admission - lookback_days, admission)``
    * ``prior_<group>_los_days``: summed ``visit_end_date - visit_start_date`` of those visits (missing end = same day)
    * ``days_since_last_encounter``: days from the end of the most recent visit of ANY type that started within the
      look-back to the admission (NULL when there is none)
    * ``prior_readmit_<gap>d_count``: prior ``readmission_group`` visits in the look-back that began within
      ``readmission_gap_days`` days after the discharge of an earlier visit of that group (any earlier date, so the
      first visit of the window can still be a readmission)

    Args:
        con: DuckDB connection with ``visit_occurrence`` and the cohort table.
        cohort_table: Table or view with ``subject_id`` and ``cohort_start_date``. If it also has
            ``visit_occurrence_id`` that visit is excluded explicitly (same-day transfers).
        cohort_id: Optional ``cohort_definition_id`` filter.
        lookback_days: Window length in days (non-negative).
        anchor_date: Only ``'admission'`` is supported: a window anchored at discharge would count the index stay.
        visit_concept_groups: ``{group_name: [visit_concept_id, ...]}``. Defaults to
            :data:`DEFAULT_VISIT_CONCEPT_GROUPS`. A visit concept may belong to several groups.
        readmission_gap_days: Gap for the prior-readmission count.
        readmission_group: Group used for the prior-readmission count (must be a key of the groups).
        format: 'df', 'arrow' or 'polars'.
    """
    if anchor_date != "admission":
        raise ValueError("anchor_date must be 'admission': a look-back anchored at discharge would include the "
                         "index stay and leak post-admission information.")
    for name, val in (("lookback_days", lookback_days), ("readmission_gap_days", readmission_gap_days)):
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"{name} must be a non-negative integer; got {val!r}")
    groups = {k: tuple(int(i) for i in v) for k, v in (visit_concept_groups or DEFAULT_VISIT_CONCEPT_GROUPS).items()}
    if not groups:
        raise ValueError("visit_concept_groups must not be empty.")
    for name, ids in groups.items():
        if not _GROUP_RE.match(name):
            raise ValueError(f"visit group name {name!r} must be a plain identifier (letters, digits, underscore).")
        if not ids:
            raise ValueError(f"visit group {name!r} has no visit_concept_ids.")
    if readmission_group not in groups:
        raise ValueError(f"readmission_group {readmission_group!r} is not one of {sorted(groups)}.")
    if format.lower().strip() not in ("df", "arrow", "pyarrow", "polars"):
        raise ValueError("format must be 'df', 'arrow' or 'polars'.")

    cols = {r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()}
    for needed in ("subject_id", "cohort_start_date"):
        if needed not in cols:
            raise ValueError(f"cohort_table {cohort_table!r} has no {needed!r} column.")
    where = ""
    if cohort_id is not None:
        if "cohort_definition_id" not in cols:
            raise ValueError(f"cohort_id given but {cohort_table!r} has no cohort_definition_id column.")
        where = f"WHERE c.cohort_definition_id = {int(cohort_id)}"
    if "visit_occurrence_id" in cols:
        index_ids = "c.visit_occurrence_id"
    else:
        index_ids = "CAST(NULL AS BIGINT)"

    def in_list(ids: Sequence[int]) -> str:
        return ", ".join(str(i) for i in ids)

    aggs, outs = [], []
    for name, ids in groups.items():
        aggs.append(f"COUNT(*) FILTER (WHERE pv.visit_concept_id IN ({in_list(ids)})) AS prior_{name}_count")
        aggs.append(f"COALESCE(SUM(pv.los) FILTER (WHERE pv.visit_concept_id IN ({in_list(ids)})), 0) "
                    f"AS prior_{name}_los_days")
    ra = in_list(groups[readmission_group])
    aggs.append(
        f"COUNT(*) FILTER (WHERE pv.visit_concept_id IN ({ra}) AND pv.is_readmit) "
        f"AS prior_readmit_{readmission_gap_days}d_count")
    sql = f"""
    WITH base AS (
        SELECT DISTINCT c.subject_id AS person_key, CAST(c.cohort_start_date AS DATE) AS adm, {index_ids} AS idx_vid
        FROM {cohort_table} AS c {where}
    ),
    keys AS (SELECT DISTINCT person_key, adm FROM base),
    visits AS (
        SELECT v.person_id, v.visit_occurrence_id, v.visit_concept_id,
               CAST(v.visit_start_date AS DATE) AS s,
               CAST(COALESCE(v.visit_end_date, v.visit_start_date) AS DATE) AS e
        FROM visit_occurrence AS v
        WHERE v.person_id IN (SELECT person_key FROM keys) AND v.visit_start_date IS NOT NULL
    ),
    flagged AS (
        SELECT vi.*, vi.e - vi.s AS los,
               EXISTS (SELECT 1 FROM visits AS u
                       WHERE u.person_id = vi.person_id AND u.visit_occurrence_id <> vi.visit_occurrence_id
                         AND u.visit_concept_id IN ({ra}) AND vi.visit_concept_id IN ({ra})
                         AND u.e <= vi.s AND vi.s - u.e <= {int(readmission_gap_days)}) AS is_readmit
        FROM visits AS vi
    ),
    prior AS (
        SELECT k.person_key, k.adm, f.*
        FROM keys AS k
        JOIN flagged AS f ON f.person_id = k.person_key AND f.s < k.adm AND f.s >= k.adm - {int(lookback_days)}
        WHERE NOT EXISTS (SELECT 1 FROM base AS b WHERE b.person_key = k.person_key AND b.adm = k.adm
                          AND b.idx_vid = f.visit_occurrence_id)
    ),
    agg AS (
        SELECT person_key, adm, {', '.join(a.replace('pv.', 'pv.') for a in aggs)},
               MIN(adm - e) AS days_since_last_encounter
        FROM (SELECT person_key, adm, visit_concept_id, los, is_readmit, e FROM prior) AS pv
        GROUP BY person_key, adm
    )
    SELECT k.person_key AS subject_id, k.adm AS cohort_start_date,
           {', '.join(f"CAST(COALESCE(a.prior_{n}_count, 0) AS INTEGER) AS prior_{n}_count, "
                      f"CAST(COALESCE(a.prior_{n}_los_days, 0) AS INTEGER) AS prior_{n}_los_days" for n in groups)},
           CAST(COALESCE(a.prior_readmit_{readmission_gap_days}d_count, 0) AS INTEGER)
               AS prior_readmit_{readmission_gap_days}d_count,
           a.days_since_last_encounter
    FROM keys AS k LEFT JOIN agg AS a ON a.person_key = k.person_key AND a.adm = k.adm
    ORDER BY k.person_key, k.adm
    """
    res = con.execute(sql).df()
    fmt = format.lower().strip()
    if fmt in ("arrow", "pyarrow"):
        import pyarrow as pa
        return pa.Table.from_pandas(res)
    if fmt == "polars":
        import polars as pl
        return pl.from_pandas(res)
    return res
