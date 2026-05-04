"""AWS Security Hub federation adapter."""

from __future__ import annotations

from daemon.federation.adapters._siem_base import SIEMIngestionAdapter
from daemon.federation.registry import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from services.aws_security_hub_ingestion import AWSSecurityHubIngestion

        return AWSSecurityHubIngestion()

    return SIEMIngestionAdapter(
        name="aws_security_hub",
        integration_id="aws_security_hub",
        default_interval=900,  # cloud cadence — Security Hub aggregates slowly
        service_factory=make_service,
        external_id_prefix="aws-securityhub",
    )


register_adapter("aws_security_hub", _factory)
