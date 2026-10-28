"""Top-level package alias for omop_duck_db matching omop_etl."""

from omop_etl import *  # noqa: F401, F403
import omop_etl as _omop_etl

__version__ = getattr(_omop_etl, "__version__", "0.8.7")
