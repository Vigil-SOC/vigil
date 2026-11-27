"""AWS Security Hub federation adapter."""

from __future__ import annotations

from typing import Optional

from core.federation.adapters._base import parse_alert_time
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter
from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion


def _alert_time(alert):
    """Creation time of a raw Security Hub finding, before transform."""
    return parse_alert_time(alert.get("CreatedAt"))


def _factory(instance: Optional[str] = None) -> FederationAdapter:
    """The base source, or with ``instance`` that instance's own source (#5353)."""

    def make_service():
        return AWSSecurityHubIngestion(instance)

    return SIEMIngestionAdapter(
        name=instance or "aws_security_hub",
        integration_id=instance or "aws-security-hub",
        default_interval=900,  # cloud cadence — Security Hub aggregates slowly
        service_factory=make_service,
        external_id_prefix="aws-securityhub",
        alert_time=_alert_time,
    )


register_adapter("aws_security_hub", _factory)
