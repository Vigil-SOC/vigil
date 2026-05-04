"""Azure Sentinel federation adapter."""

from __future__ import annotations

from daemon.federation.adapters._siem_base import SIEMIngestionAdapter
from daemon.federation.registry import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from services.azure_sentinel_ingestion import AzureSentinelIngestion

        return AzureSentinelIngestion()

    return SIEMIngestionAdapter(
        name="azure_sentinel",
        integration_id="azure_sentinel",
        default_interval=300,  # cloud SIEM cadence
        service_factory=make_service,
        external_id_prefix="azure-sentinel",
    )


register_adapter("azure_sentinel", _factory)
