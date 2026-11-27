"""Azure Sentinel federation adapter."""

from __future__ import annotations

from typing import Optional

from core.federation.adapters._base import parse_alert_time
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion


def _alert_time(alert):
    """Creation time of a raw Sentinel incident record, before transform."""
    return parse_alert_time(alert.get("created_time"))


def _factory(instance: Optional[str] = None) -> FederationAdapter:
    """The base source, or with ``instance`` that instance's own source (#5353)."""

    def make_service():
        return AzureSentinelIngestion(instance)

    return SIEMIngestionAdapter(
        name=instance or "azure_sentinel",
        integration_id=instance or "azure-sentinel",
        default_interval=300,  # cloud SIEM cadence
        service_factory=make_service,
        external_id_prefix="azure-sentinel",
        alert_time=_alert_time,
    )


register_adapter("azure_sentinel", _factory)
