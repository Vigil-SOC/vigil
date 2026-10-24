"""The 5s Kafka sync applies the same saved settings as daemon startup."""

import dataclasses

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


def test_sync_tick_matches_startup(monkeypatch):
    class _Svc:
        def get_system_config(self, key):
            return dict(SAVED) if key == "kafka.settings" else None

    import core.storage.config_service as cs

    monkeypatch.setattr(cs, "get_config_service", lambda *a, **k: _Svc())

    startup = DaemonConfig.from_env().kafka
    ingestor = KafkaIngestor(KafkaConfig())
    ingestor._sync_config_from_db()

    assert dataclasses.asdict(ingestor.config) == dataclasses.asdict(startup)
    assert {k: getattr(startup, k) for k in SAVED} == SAVED
