"""The 5s Kafka sync applies the same saved settings as daemon startup."""

import dataclasses

import pytest

from core.ingestion.kafka_config import KafkaConfig
from services.daemon.config import DaemonConfig
from services.daemon.kafka_ingestor import KafkaIngestor

SAVED = {
    "enabled": True,
    "bootstrap_servers": "b:9093",
    "consumer_group": "g2",
    "topics": ["t1"],
    "auto_offset_reset": "earliest",
    "security_protocol": "SASL_SSL",
    "sasl_mechanism": "SCRAM-SHA-512",
    "sasl_username": "svc-vigil",
    "max_poll_records": 50,
    "session_timeout_ms": 10000,
}


@pytest.fixture
def saved_row(monkeypatch):
    row = dict(SAVED)

    class _Svc:
        def get_system_config(self, key):
            return dict(row) if key == "kafka.settings" else None

    import core.storage.config_service as cs

    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _Svc())
    return row


def test_sync_tick_matches_startup(saved_row):
    startup = DaemonConfig.from_env().kafka
    ingestor = KafkaIngestor(KafkaConfig())
    ingestor._sync_config_from_db()

    assert dataclasses.asdict(ingestor.config) == dataclasses.asdict(startup)
    assert {k: getattr(startup, k) for k in SAVED} == SAVED


def test_change_saved_after_startup_reaches_ingestor(saved_row):
    ingestor = KafkaIngestor(DaemonConfig.from_env().kafka)
    saved_row.update(
        sasl_mechanism="PLAIN",
        sasl_username="rotated",
        max_poll_records=7,
        session_timeout_ms=1234,
    )
    ingestor._sync_config_from_db()

    assert ingestor.config.sasl_mechanism == "PLAIN"
    assert ingestor.config.sasl_username == "rotated"
    assert ingestor.config.max_poll_records == 7
    assert ingestor.config.session_timeout_ms == 1234
