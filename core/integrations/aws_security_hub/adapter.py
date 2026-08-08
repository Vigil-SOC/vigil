"""AWS Security Hub federation adapter."""

from __future__ import annotations

from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion

        return AWSSecurityHubIngestion()

    return SIEMIngestionAdapter(
        name="aws_security_hub",
        integration_id="aws-security-hub",
        default_interval=900,  # cloud cadence — Security Hub aggregates slowly
        service_factory=make_service,
        external_id_prefix="aws-securityhub",
    )


register_adapter("aws_security_hub", _factory)
