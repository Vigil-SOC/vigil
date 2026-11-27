"""Microsoft Defender for Endpoint federation adapter."""

from __future__ import annotations

from typing import Optional

from core.federation.adapters._base import parse_alert_time
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion


def _alert_time(alert):
    """Creation time of a raw Defender alert, before transform."""
    return parse_alert_time(alert.get("alertCreationTime"))


def _factory(instance: Optional[str] = None) -> FederationAdapter:
    """The base source, or with ``instance`` that instance's own source (#5353)."""

    def make_service():
        return MicrosoftDefenderIngestion(instance)

    return SIEMIngestionAdapter(
        name=instance or "microsoft_defender",
        integration_id=instance or "microsoft-defender",
        default_interval=60,  # EDR cadence
        service_factory=make_service,
        external_id_prefix="defender",
        alert_time=_alert_time,
    )


register_adapter("microsoft_defender", _factory)
