"""Elastic Security federation adapter."""

from __future__ import annotations

from daemon.federation.adapters._siem_base import SIEMIngestionAdapter
from daemon.federation.registry import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from services.elastic_ingestion import ElasticIngestion

        return ElasticIngestion()

    return SIEMIngestionAdapter(
        name="elastic",
        # Note: integration_id matches what core.config / settings UI use
        # ("elastic-siem"); the adapter name is shorter for the source_id PK.
        integration_id="elastic-siem",
        default_interval=300,  # SIEM cadence
        service_factory=make_service,
        external_id_prefix="elastic",
    )


register_adapter("elastic", _factory)
