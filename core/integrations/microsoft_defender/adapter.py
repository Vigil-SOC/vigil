"""Microsoft Defender for Endpoint federation adapter."""

from __future__ import annotations

from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter


def _factory() -> FederationAdapter:
    def make_service():
        from core.integrations.microsoft_defender.ingestion import (
            MicrosoftDefenderIngestion,
        )

        return MicrosoftDefenderIngestion()

    return SIEMIngestionAdapter(
        name="microsoft_defender",
        integration_id="microsoft-defender",
        default_interval=60,  # EDR cadence
        service_factory=make_service,
        external_id_prefix="defender",
    )


register_adapter("microsoft_defender", _factory)
