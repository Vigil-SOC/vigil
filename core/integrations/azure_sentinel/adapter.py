"""Azure Sentinel federation adapter."""

from __future__ import annotations

from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

        return AzureSentinelIngestion()

    return SIEMIngestionAdapter(
        name="azure_sentinel",
        integration_id="azure-sentinel",
        default_interval=300,  # cloud SIEM cadence
        service_factory=make_service,
        external_id_prefix="azure-sentinel",
    )


register_adapter("azure_sentinel", _factory)
