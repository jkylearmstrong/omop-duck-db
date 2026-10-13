"""Vectorized, fault-tolerant datetime/timestamp normalization for messy EHR feeds.

Everything here compiles to a single DuckDB SQL expression: no Python UDFs and no per-row fallbacks, so it
stays fully vectorized on multi-million-row tables.
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "DEFAULT_DATETIME_FORMATS",
    "normalize_time_sql",
    "vectorized_strptime",
    "install_vectorized_strptime",
]

# The three formats seen in PCORnet feeds come first; seconds / minute-less variants follow.
DEFAULT_DATETIME_FORMATS: tuple[str, ...] = (
    "%d%b%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d%b%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%d%b%Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
)

_ON_ERROR = ("null", "default_date", "raise")


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def normalize_time_sql(time_col: str | None) -> str:
    """SQL expression normalizing a time-of-day string to zero-padded ``HH:MM`` (or ``HH:MM:SS``).

    Handles ``4:15`` -> ``04:15``, ``4`` -> ``04:00``, ``415``/``0415`` -> ``04:15``, ``4:15 PM`` -> ``16:15``,
    ``12:05 AM`` -> ``00:05``, and NULL/blank -> ``00:00`` (the ``LPAD(COALESCE(time, '00:00'), 5, '0')``
    behavior, extended). Anything unrecognizable is left-padded to width 5 so the date parse, not this step,
    decides whether the row is an error.
    """
    if time_col is None:
        return "'00:00'"
    t = f"UPPER(TRIM(CAST({time_col} AS VARCHAR)))"
    core = f"TRIM(regexp_replace({t}, '\\s*(AM|PM)$', ''))"
    meridiem = f"regexp_extract({t}, '(AM|PM)$', 1)"
    colon = f"regexp_matches({core}, '^[0-9]{{1,2}}(:[0-9]{{1,2}}){{0,2}}$')"
    bare = f"regexp_matches({core}, '^[0-9]{{3,4}}$')"
    hh_raw = (f"(CASE WHEN {bare} THEN CAST(LEFT(LPAD({core}, 4, '0'), 2) AS INTEGER) "
              f"ELSE TRY_CAST(regexp_extract({core}, '^([0-9]{{1,2}})', 1) AS INTEGER) END)")
    mm = (f"(CASE WHEN {bare} THEN RIGHT({core}, 2) "
          f"ELSE COALESCE(NULLIF(regexp_extract({core}, '^[0-9]{{1,2}}:([0-9]{{1,2}})', 1), ''), '00') END)")
    ss = f"NULLIF(regexp_extract({core}, '^[0-9]{{1,2}}:[0-9]{{1,2}}:([0-9]{{1,2}})', 1), '')"
    hh24 = (f"(CASE WHEN {meridiem} = 'PM' THEN ({hh_raw} % 12) + 12 "
            f"WHEN {meridiem} = 'AM' THEN ({hh_raw} % 12) ELSE {hh_raw} END)")
    structured = (f"LPAD(CAST({hh24} AS VARCHAR), 2, '0') || ':' || LPAD({mm}, 2, '0') || "
                  f"COALESCE(':' || LPAD({ss}, 2, '0'), '')")
    return (f"(CASE WHEN {t} IS NULL OR {t} = '' THEN '00:00' "
            f"WHEN {colon} OR {bare} THEN {structured} "
            f"ELSE LPAD({t}, 5, '0') END)")


def vectorized_strptime(
    date_col: str,
    time_col: str | None = None,
    formats: Sequence[str] | None = None,
    on_error: str = "null",
    default_date: str = "1900-01-01",
) -> str:
    """Build a vectorized SQL expression parsing ``date_col`` (+ optional ``time_col``) to TIMESTAMP.

    Args:
        date_col: Column name/expression holding the date (or a full datetime string).
        time_col: Optional column holding time-of-day; normalized by :func:`normalize_time_sql`
            (single-digit hours, missing minutes, 12/24-hour notation) and appended to the date.
        formats: Prioritized strptime formats, tried in order via one ``try_strptime(x, [..])`` call.
            Defaults to :data:`DEFAULT_DATETIME_FORMATS`.
        on_error: What to do with a non-blank input that matches no format: ``'null'`` (return NULL),
            ``'default_date'`` (return ``default_date``), or ``'raise'`` (fail the query naming the value).
            NULL/blank dates always yield NULL.
        default_date: Timestamp literal for ``on_error='default_date'``.

    Returns:
        A SQL expression string, usable in SELECT/INSERT/UPDATE.
    """
    if on_error not in _ON_ERROR:
        raise ValueError(f"on_error must be one of {_ON_ERROR}, got {on_error!r}.")
    fmts = list(formats) if formats is not None else list(DEFAULT_DATETIME_FORMATS)
    if not fmts:
        raise ValueError("formats must contain at least one format string.")
    fmt_list = "[" + ", ".join(_q(f) for f in fmts) + "]"

    d = f"TRIM(CAST({date_col} AS VARCHAR))"
    if time_col is None:
        parsed = f"try_strptime({d}, {fmt_list})"
    else:
        # A date that already carries a time component (contains ':') is not given a second one.
        text = f"(CASE WHEN {d} LIKE '%:%' THEN {d} ELSE {d} || ' ' || {normalize_time_sql(time_col)} END)"
        # Retry the bare date so date-only formats still match when the time suffix defeats them.
        parsed = f"COALESCE(try_strptime({text}, {fmt_list}), try_strptime({d}, {fmt_list}))"

    if on_error == "null":
        return parsed
    present = f"({d} IS NOT NULL AND {d} <> '')"
    if on_error == "default_date":
        return f"(CASE WHEN {present} THEN COALESCE({parsed}, CAST({_q(default_date)} AS TIMESTAMP)) END)"
    return (f"(CASE WHEN {present} THEN COALESCE({parsed}, "
            f"CAST(error('vectorized_strptime: unparseable datetime ''' || {d} || '''') AS TIMESTAMP)) END)")


def install_vectorized_strptime(con, formats: Sequence[str] | None = None) -> None:
    """Register SQL macros ``normalize_time(t)`` and ``vectorized_strptime(d, t)`` on a connection.

    The macro uses the default fallback chain with ``on_error='null'``; call :func:`vectorized_strptime`
    directly for ``'default_date'`` / ``'raise'`` behavior.
    """
    con.execute(f"CREATE OR REPLACE TEMPORARY MACRO normalize_time(t) AS {normalize_time_sql('t')}")
    con.execute(
        "CREATE OR REPLACE TEMPORARY MACRO vectorized_strptime(d, t) AS "
        + vectorized_strptime("d", "t", formats)
    )
