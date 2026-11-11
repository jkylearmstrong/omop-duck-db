"""Census ACS area features for OMOP ZIP codes, without staging files by hand.

:func:`fetch_acs_zcta` downloads the American Community Survey 5-year estimates for every ZIP Code Tabulation Area (ZCTA)
from the public Census Bureau API once and caches them as CSV (median household income, poverty rate, educational
attainment, population). :func:`acs_zip3_table` aggregates them to 3-digit ZIPs, the geography many de-identified OMOP
extracts keep. :func:`acs_feature` turns one of those indicators into a :class:`~omop_etl.study_features.StudyFeature`
(``FeatureSet().add(acs_feature("median_household_income"))``) keyed on ``location.zip``, with the same log / percentile /
z-score transforms, fitted state and missing indicator as :func:`~omop_etl.study_features.area_feature`.

Nothing is bundled in the wheel: the tables are public, change every year and are large, so a site fetches the vintage its
protocol names (``year=``) and the cache directory keeps it. A Census API key is optional (``CENSUS_API_KEY`` or ``api_key=``).
Pass ``fetcher=`` (a callable returning the API's JSON rows) to run offline or behind a proxy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from omop_etl.study_features import StudyFeature, area_feature, register_feature_factory

__all__ = ["fetch_acs_zcta", "acs_zip3_table", "acs_feature", "ACS_INDICATORS"]

_API = "https://api.census.gov/data/{year}/acs/acs5"
_EDU_LT_HS = [f"B15003_{i:03d}E" for i in range(2, 17)]        # no schooling .. 12th grade, no diploma
_EDU_BACH = [f"B15003_{i:03d}E" for i in (22, 23, 24, 25)]      # bachelor's, master's, professional, doctorate
_RAW = ["B01003_001E", "B19013_001E", "B17001_001E", "B17001_002E", "B15003_001E", *_EDU_LT_HS, *_EDU_BACH]

ACS_INDICATORS = {
    "median_household_income": "B19013_001E: median household income in the past 12 months (dollars)",
    "poverty_rate": "B17001_002E / B17001_001E: share of people below the poverty level",
    "pct_bachelors_plus": "share of adults 25+ with a bachelor's degree or higher (B15003)",
    "pct_less_than_hs": "share of adults 25+ without a high-school diploma (B15003)",
    "population": "B01003_001E: total population",
}


def _http_fetch(url: str) -> list[list[str]]:
    import urllib.request
    with urllib.request.urlopen(url, timeout=120) as r:       # public, read-only Census endpoint; no patient data is sent
        return json.loads(r.read().decode("utf-8"))


def _cache_dir(cache_dir: str | os.PathLike | None) -> Path:
    base = Path(cache_dir) if cache_dir else Path(os.environ.get("OMOP_DUCK_DB_CACHE", Path.home() / ".cache" / "omop-duck-db"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def fetch_acs_zcta(year: int = 2022, *, cache_dir: str | os.PathLike | None = None, refresh: bool = False,
                   api_key: str | None = None, fetcher: Callable[[str], list[list[str]]] | None = None) -> pd.DataFrame:
    """ACS 5-year indicators per ZCTA: ``zip5`` plus the columns of :data:`ACS_INDICATORS`.

    Cached in ``cache_dir`` (default ``~/.cache/omop-duck-db``, or ``$OMOP_DUCK_DB_CACHE``) as ``acs5_<year>_zcta.csv``;
    ``refresh=True`` downloads again. Census sentinel values (negative codes such as -666666666 for "not available") become
    NaN. Rates are fractions in 0-1.
    """
    path = _cache_dir(cache_dir) / f"acs5_{int(year)}_zcta.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"zip5": str})
    key = api_key or os.environ.get("CENSUS_API_KEY")
    url = f"{_API.format(year=int(year))}?get={','.join(_RAW)}&for=zip%20code%20tabulation%20area:*" + (f"&key={key}" if key else "")
    rows = (fetcher or _http_fetch)(url)
    raw = pd.DataFrame(rows[1:], columns=rows[0])
    zcol = next(c for c in raw.columns if "zip code tabulation area" in c)
    num = raw[_RAW].apply(pd.to_numeric, errors="coerce")
    num = num.where(num >= 0)                                  # -666666666 and friends mean "no estimate"
    out = pd.DataFrame({"zip5": raw[zcol].astype(str).str.zfill(5)})
    out["median_household_income"] = num["B19013_001E"]
    out["poverty_rate"] = num["B17001_002E"] / num["B17001_001E"].where(num["B17001_001E"] > 0)
    adults = num["B15003_001E"].where(num["B15003_001E"] > 0)
    out["pct_bachelors_plus"] = num[_EDU_BACH].sum(axis=1, min_count=1) / adults
    out["pct_less_than_hs"] = num[_EDU_LT_HS].sum(axis=1, min_count=1) / adults
    out["population"] = num["B01003_001E"]
    out = out.drop_duplicates("zip5").reset_index(drop=True)
    out.to_csv(path, index=False)
    return out


def acs_zip3_table(zcta: pd.DataFrame) -> pd.DataFrame:
    """Population-weighted aggregate of a :func:`fetch_acs_zcta` table to 3-digit ZIPs (``zip3``).

    Income is the population-weighted mean of ZCTA medians, an approximation (a true 3-digit median needs household counts);
    rates are weighted the same way. ZCTAs without an estimate or population are ignored for that indicator.
    """
    z = zcta.copy()
    z["zip3"] = z["zip5"].astype(str).str.zfill(5).str[:3]
    w = z["population"].fillna(0).clip(lower=0)
    out = {"zip3": sorted(z["zip3"].unique())}
    res = pd.DataFrame(out)
    for col in ("median_household_income", "poverty_rate", "pct_bachelors_plus", "pct_less_than_hs"):
        ok = z[col].notna() & (w > 0)
        g = (z[col][ok] * w[ok]).groupby(z["zip3"][ok]).sum() / w[ok].groupby(z["zip3"][ok]).sum()
        res[col] = res["zip3"].map(g)
    res["population"] = res["zip3"].map(z.groupby("zip3")["population"].sum(min_count=1))
    return res


def acs_feature(indicator: str = "median_household_income", *, name: str | None = None, year: int = 2022, geography: str = "zip5",
                transform: str | None = None, impute: bool = False, missing_indicator: bool = True,
                table: pd.DataFrame | None = None, cache_dir: str | os.PathLike | None = None, refresh: bool = False,
                api_key: str | None = None, fetcher: Callable[[str], list[list[str]]] | None = None) -> StudyFeature:
    """An ACS indicator of the residence ZIP at admission as a study feature.

    ``indicator`` is a key of :data:`ACS_INDICATORS`. ``geography='zip5'`` joins on the 5-digit ZIP; ``'zip3'`` on the first
    three digits (for extracts that only keep those). ``transform`` defaults to ``log1p`` for income and population, ``none``
    for rates. Pass ``table`` (a :func:`fetch_acs_zcta` result) to avoid any download; otherwise the table is fetched once and
    cached. The year and indicator are recorded in the feature parameters (and so in the manifest) for replication.
    """
    if indicator not in ACS_INDICATORS:
        raise ValueError(f"indicator must be one of {sorted(ACS_INDICATORS)}.")
    if geography not in ("zip5", "zip3"):
        raise ValueError("geography must be 'zip5' or 'zip3'.")
    zcta = table if table is not None else fetch_acs_zcta(year, cache_dir=cache_dir, refresh=refresh, api_key=api_key, fetcher=fetcher)
    tab = zcta if geography == "zip5" else acs_zip3_table(zcta)
    tf = transform or ("log1p" if indicator in ("median_household_income", "population") else "none")
    feat = area_feature(name or f"acs_{indicator}", tab[[geography, indicator]], indicator, key_column=geography, transform=tf,
                        impute=impute, missing_indicator=missing_indicator, geography=geography,
                        description=f"ACS 5-year {year} {ACS_INDICATORS[indicator]} by {geography}")
    feat.params.update({"source": "US Census ACS 5-year", "year": int(year), "indicator": indicator})
    return feat


register_feature_factory("acs", acs_feature, overwrite=True)
