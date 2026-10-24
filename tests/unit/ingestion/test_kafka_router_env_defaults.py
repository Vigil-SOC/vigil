"""GET /api/kafka/config and /status report the env Kafka settings the daemon runs with."""

import asyncio
from unittest.mock import MagicMock

import pytest

from core.config import get_settings
from core.ingestion import kafka_router
from services.daemon.config import DaemonConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def env_kafka(monkeypatch):
    monkeypatch.setenv("KAFKA_ENABLED", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker-1:9092")
    monkeypatch.setenv("KAFKA_TOPICS", "sec.alerts,edr.events")
    monkeypatch.setenv("KAFKA_CONSUMER_GROUP", "grp-x")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "hunter2")
    get_settings.cache_clear()
    stored = {}
    svc = MagicMock()
    svc.get_system_config.side_effect = lambda key: stored.get(key)
    monkeypatch.setattr(kafka_router, "get_config_service", lambda *a, **k: svc)
    monkeypatch.setattr(
        "core.storage.config_service.get_config_service", lambda *a, **k: svc
    )

    async def no_daemon():
        return None

    monkeypatch.setattr(kafka_router, "_fetch_daemon_kafka_stats", no_daemon)
    yield stored
    get_settings.cache_clear()


def test_config_without_saved_row_matches_daemon(env_kafka):
    daemon = DaemonConfig.from_env().kafka
    config = asyncio.run(kafka_router.get_kafka_config())

    assert config == {
        "enabled": True,
        "bootstrap_servers": "broker-1:9092",
        "consumer_group": "grp-x",
        "topics": ["sec.alerts", "edr.events"],
        "auto_offset_reset": daemon.auto_offset_reset,
        "security_protocol": daemon.security_protocol,
        "sasl_mechanism": daemon.sasl_mechanism,
        "sasl_username": daemon.sasl_username,
        "max_poll_records": daemon.max_poll_records,
        "session_timeout_ms": daemon.session_timeout_ms,
    }
    assert daemon.enabled is True
    assert daemon.topics == ["sec.alerts", "edr.events"]


def test_status_without_saved_row_matches_daemon(env_kafka):
    status = asyncio.run(kafka_router.get_kafka_status())

    assert status["enabled"] is True
    assert status["config"]["bootstrap_servers"] == "broker-1:9092"
    assert "sasl_password" not in status["config"]
    assert status["stats"]["topics"] == ["sec.alerts", "edr.events"]
    assert status["stats"]["consumer_group"] == "grp-x"


def test_saved_row_overrides_env(env_kafka):
    env_kafka["kafka.settings"] = {"enabled": False, "topics": ["ui.topic"]}

    config = asyncio.run(kafka_router.get_kafka_config())
    daemon = DaemonConfig.from_env().kafka

    assert (config["enabled"], config["topics"]) == (False, ["ui.topic"])
    assert (daemon.enabled, daemon.topics) == (False, ["ui.topic"])
    assert config["bootstrap_servers"] == daemon.bootstrap_servers == "broker-1:9092"
