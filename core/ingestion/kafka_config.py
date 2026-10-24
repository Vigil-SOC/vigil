"""Kafka consumer configuration.

Lives with the consumer that reads it rather than in the daemon's config
module: core/ingestion/ owns the Kafka ingestion path, and a capability
domain must not reach up into a deployable for its own settings.
services/daemon/config.py re-exports it for DaemonConfig.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from core.config import get_settings
from core.secrets import get_secret


@dataclass
class KafkaConfig:
    # JSON-only deserialization, single consumer group. Non-secret fields are
    # overridable via SystemConfig["kafka.settings"]; credentials via get_secret.
    enabled: bool = False
    bootstrap_servers: str = "localhost:9092"
    consumer_group: str = "vigil-soc"
    topics: List[str] = field(default_factory=list)
    auto_offset_reset: str = "latest"  # "latest" | "earliest"
    max_poll_records: int = 500
    session_timeout_ms: int = 30_000
    # Security (env-only for secrets)
    security_protocol: str = "PLAINTEXT"  # PLAINTEXT | SASL_SSL | SSL
    sasl_mechanism: Optional[str] = None  # PLAIN | SCRAM-SHA-256 | SCRAM-SHA-512
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None
    ssl_ca_location: Optional[str] = None

    @classmethod
    def from_env(cls) -> "KafkaConfig":
        settings = get_settings()
        return cls(
            enabled=settings.kafka_enabled,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            consumer_group=settings.kafka_consumer_group,
            topics=list(settings.kafka_topics),
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            security_protocol=settings.kafka_security_protocol,
            sasl_mechanism=settings.kafka_sasl_mechanism or None,
            sasl_username=get_secret("KAFKA_SASL_USERNAME") or None,
            sasl_password=get_secret("KAFKA_SASL_PASSWORD") or None,
            ssl_ca_location=settings.kafka_ssl_ca_location or None,
        )
