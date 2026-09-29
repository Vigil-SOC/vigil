"""Elastic Security federation adapter."""

from __future__ import annotations

from datetime import timedelta

from core.federation.adapters._base import parse_alert_time
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.contract import FederationAdapter, register_adapter
from core.integrations.elastic.ingestion import ElasticIngestion

# Filebeat (Wazuh indexer) and Kibana's rule executor both write an alert some
# seconds after its @timestamp; reading only settled time keeps them in view.
SETTLE_DELAY = timedelta(seconds=60)


def _alert_time(alert):
    """Creation time of a raw hit (Kibana detection alert or indexer doc)."""
    return parse_alert_time((alert.get("_source") or {}).get("@timestamp"))


def _factory() -> FederationAdapter:
    def make_service():

        return ElasticIngestion()

    return SIEMIngestionAdapter(
        name="elastic",
        # Note: integration_id matches what core.config / settings UI use
        # ("elastic-siem"); the adapter name is shorter for the source_id PK.
        integration_id="elastic-siem",
        default_interval=300,  # SIEM cadence
        service_factory=make_service,
        external_id_prefix="elastic",
        alert_time=_alert_time,
        settle_delay=SETTLE_DELAY,
    )


register_adapter("elastic", _factory)
