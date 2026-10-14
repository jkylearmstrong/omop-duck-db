"""Federation re-exports for omop_duck_db."""

from omop_etl.federation import (
    FederatedConsortium,
    PrivacyAuditResult,
    audit_cell_suppression,
    consortium_privacy_audit,
    create_federated_consortium,
    with_cell_suppression,
)

__all__ = [
    "FederatedConsortium",
    "create_federated_consortium",
    "with_cell_suppression",
    "PrivacyAuditResult",
    "audit_cell_suppression",
    "consortium_privacy_audit",
]
