"""Manuscript scaffold re-exports for omop_duck_db."""

from omop_etl.manuscript import (
    ConsortiumManuscript,
    build_manuscript_scaffold,
)

__all__ = [
    "ConsortiumManuscript",
    "build_manuscript_scaffold",
]
