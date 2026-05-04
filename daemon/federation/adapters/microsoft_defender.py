"""Microsoft Defender for Endpoint federation adapter."""

from __future__ import annotations

from daemon.federation.adapters._siem_base import SIEMIngestionAdapter
from daemon.federation.registry import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from services.microsoft_defender_ingestion import MicrosoftDefenderIngestion

        return MicrosoftDefenderIngestion()

    return SIEMIngestionAdapter(
        name="microsoft_defender",
        integration_id="microsoft_defender",
        default_interval=60,  # EDR cadence
        service_factory=make_service,
        external_id_prefix="defender",
    )


register_adapter("microsoft_defender", _factory)
