"""OpenSearch federation adapter."""

from __future__ import annotations

from datetime import timedelta

from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter
from core.integrations.opensearch.ingestion import OpenSearchIngestion, finding_time

# A detector's doc-level monitor writes a finding some seconds after the
# triggering event; reading only settled time keeps findings in view.
SETTLE_DELAY = timedelta(seconds=60)


def _alert_time(alert):
    """Creation time of a raw finding hit (epoch-ms ``timestamp`` on the document)."""
    return finding_time(alert.get("_source") or {})


def _factory() -> FederationAdapter:
    def make_service():

        return OpenSearchIngestion()

    return SIEMIngestionAdapter(
        name="opensearch",
        integration_id="opensearch",
        default_interval=300,  # SIEM cadence
        service_factory=make_service,
        external_id_prefix="opensearch",
        alert_time=_alert_time,
        settle_delay=SETTLE_DELAY,
    )


register_adapter("opensearch", _factory)
