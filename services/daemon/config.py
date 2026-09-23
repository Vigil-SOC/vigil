import logging
from dataclasses import dataclass, field
from typing import Dict, List

from core.config import DEFAULT_REDIS_URL, get_settings
from core.ingestion.kafka_config import KafkaConfig  # re-exported for DaemonConfig
from core.intent import INTENT_FIELDS
from core.response.config import ResponseConfig  # re-exported for DaemonConfig
from core.secrets import get_secret

logger = logging.getLogger(__name__)


@dataclass
class PollingConfig:
    splunk_interval: int = 300  # 5 minutes
    crowdstrike_interval: int = 60  # 1 minute
    generic_interval: int = 120  # 2 minutes for other sources
    webhook_enabled: bool = True
    webhook_port: int = 8081
    webhook_token: str = ""  # required bearer for /ingest; empty = fail closed


@dataclass
class ProcessingConfig:
    auto_triage_enabled: bool = True
    auto_enrich_enabled: bool = True
    batch_size: int = 10
    max_concurrent_tasks: int = 5
    triage_timeout: int = 60  # seconds
    enrich_max_inflight: int = (
        50  # cap on pending background enrich tasks (backpressure)
    )
    enrich_backfill_enabled: bool = True  # sweep for stored-but-never-enriched findings
    enrich_backfill_interval: int = 300  # seconds between sweeps
    enrich_backfill_batch: int = 50  # findings re-queued per sweep
    enrich_backfill_max_age_hours: int = (
        168  # only backfill findings newer than this (7d)
    )


@dataclass
class EscalationConfig:
    enabled: bool = True
    escalate_severities: List[str] = field(default_factory=lambda: ["critical", "high"])
    slack_enabled: bool = True
    slack_channel: str = "#soc-alerts"
    pagerduty_enabled: bool = False
    pagerduty_severity_map: dict = field(
        default_factory=lambda: {
            "critical": "critical",
            "high": "error",
            "medium": "warning",
            "low": "info",
        }
    )


@dataclass
class SchedulerConfig:
    threat_hunt_enabled: bool = True
    threat_hunt_interval: int = 86400  # Daily (24 hours)
    probes_enabled: bool = True  # known-answer probes (#923)
    probe_interval: int = 3600  # hourly tick; the day-scoped id makes it daily
    report_generation_enabled: bool = True
    report_interval: int = 604800  # Weekly (7 days)
    cleanup_enabled: bool = True
    cleanup_interval: int = 86400  # Daily
    cleanup_retention_days: int = 90
    approval_expiry_days: int = 7


@dataclass
class MetricsConfig:
    enabled: bool = True
    port: int = 9091
    path: str = "/metrics"


@dataclass
class OrchestratorConfig:
    enabled: bool = False
    loop_interval: int = 60
    max_concurrent_agents: int = 3
    max_iterations_per_agent: int = 50
    max_cost_per_investigation: float = 5.0
    max_total_hourly_cost: float = 20.0
    max_runtime_per_investigation: int = 3600
    stale_threshold: int = 300
    workdir_base: str = "data/investigations"
    dry_run: bool = False
    # ORCHESTRATOR_SHADOW_ADJUDICATION: enqueue a second, non-executing
    # `adjudicate` run beside every detection-finding investigation (#880).
    shadow_adjudication: bool = False
    # How long a queued trigger may wait, the last-quarter promotion
    # window, and the queued depth that warrants one human signal.
    # Constants, not settings: one policy, not an operator dial.
    intake_ttl_seconds: int = 4 * 3600
    intake_ttl_promote_fraction: float = 0.25
    intake_surge_depth: int = 20


@dataclass
class LLMQueueConfig:
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_llm_calls: int = 5
    investigation_timeout: int = 180
    chat_timeout: int = 120
    session_ttl: int = 14400  # 4 hours


@dataclass
class DaemonConfig:
    polling: PollingConfig = field(default_factory=PollingConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    llm_queue: LLMQueueConfig = field(default_factory=LLMQueueConfig)
    kafka: KafkaConfig = field(default_factory=KafkaConfig)

    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Where each intent knob's value came from (env | db | default), keyed by
    # attribute path, recorded by from_env() for the INTENT.md observe report.
    sources: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        config = cls()
        settings = get_settings()

        # pydantic-settings marks a field set iff env or .env supplied it.
        for intent_field in INTENT_FIELDS:
            config.sources[intent_field.path] = (
                "env"
                if intent_field.setting in settings.model_fields_set
                else "default"
            )

        config.log_level = settings.daemon_log_level

        config.polling.splunk_interval = settings.daemon_splunk_poll_interval
        config.polling.crowdstrike_interval = settings.daemon_crowdstrike_poll_interval
        config.polling.webhook_enabled = settings.daemon_webhook_enabled
        config.polling.webhook_port = settings.daemon_webhook_port
        config.polling.webhook_token = get_secret("DAEMON_WEBHOOK_TOKEN") or ""

        config.processing.auto_triage_enabled = settings.daemon_auto_triage
        config.processing.auto_enrich_enabled = settings.daemon_auto_enrich
        config.processing.batch_size = settings.daemon_batch_size
        config.processing.triage_timeout = settings.daemon_triage_timeout
        config.processing.enrich_max_inflight = settings.daemon_enrich_max_inflight
        config.processing.enrich_backfill_enabled = settings.daemon_enrich_backfill
        config.processing.enrich_backfill_interval = (
            settings.daemon_enrich_backfill_interval
        )
        config.processing.enrich_backfill_batch = settings.daemon_enrich_backfill_batch
        config.processing.enrich_backfill_max_age_hours = (
            settings.daemon_enrich_backfill_max_age_hours
        )

        config.response = ResponseConfig.from_settings(settings)

        config.escalation.enabled = settings.daemon_escalation_enabled
        config.escalation.slack_enabled = (
            True
            if settings.daemon_slack_enabled is None
            else settings.daemon_slack_enabled
        )
        config.escalation.slack_channel = settings.daemon_slack_channel
        config.escalation.pagerduty_enabled = settings.daemon_pagerduty_enabled
        config.escalation.escalate_severities = list(
            settings.daemon_escalate_severities
        )

        config.scheduler.threat_hunt_enabled = settings.daemon_threat_hunt_enabled
        config.scheduler.threat_hunt_interval = settings.daemon_threat_hunt_interval
        config.scheduler.probes_enabled = settings.daemon_probes_enabled
        config.scheduler.probe_interval = settings.daemon_probe_interval
        config.scheduler.cleanup_retention_days = settings.daemon_cleanup_retention_days
        config.scheduler.approval_expiry_days = settings.daemon_approval_expiry_days

        config.metrics.enabled = settings.daemon_metrics_enabled
        config.metrics.port = settings.daemon_health_port

        config.orchestrator.enabled = settings.orchestrator_enabled
        config.orchestrator.loop_interval = settings.orchestrator_loop_interval
        config.orchestrator.max_concurrent_agents = settings.orchestrator_max_agents
        config.orchestrator.max_iterations_per_agent = (
            settings.orchestrator_max_iterations
        )
        config.orchestrator.max_cost_per_investigation = settings.orchestrator_max_cost
        config.orchestrator.max_total_hourly_cost = (
            settings.orchestrator_max_hourly_cost
        )
        config.orchestrator.max_runtime_per_investigation = (
            settings.orchestrator_max_runtime
        )
        config.orchestrator.stale_threshold = settings.orchestrator_stale_threshold
        config.orchestrator.workdir_base = settings.orchestrator_workdir
        config.orchestrator.dry_run = settings.orchestrator_dry_run
        config.orchestrator.shadow_adjudication = (
            settings.orchestrator_shadow_adjudication
        )

        config.llm_queue.redis_url = settings.redis_url or DEFAULT_REDIS_URL
        config.llm_queue.max_concurrent_llm_calls = settings.llm_max_concurrent

        config.kafka.enabled = settings.kafka_enabled  # SystemConfig may override below
        config.kafka.bootstrap_servers = settings.kafka_bootstrap_servers
        config.kafka.consumer_group = settings.kafka_consumer_group
        config.kafka.topics = list(settings.kafka_topics)
        config.kafka.auto_offset_reset = settings.kafka_auto_offset_reset
        config.kafka.max_poll_records = settings.kafka_max_poll_records
        config.kafka.session_timeout_ms = settings.kafka_session_timeout_ms
        config.kafka.security_protocol = settings.kafka_security_protocol
        config.kafka.sasl_mechanism = settings.kafka_sasl_mechanism or None
        config.kafka.sasl_username = get_secret("KAFKA_SASL_USERNAME") or None
        config.kafka.sasl_password = get_secret("KAFKA_SASL_PASSWORD") or None
        config.kafka.ssl_ca_location = settings.kafka_ssl_ca_location or None

        # Override with DB-persisted settings (set via Settings UI)
        try:
            from core.storage.config_service import get_config_service

            config_service = get_config_service()
            db_config = config_service.get_system_config("orchestrator.settings")
            if db_config and isinstance(db_config, dict):
                field_map = {
                    "enabled": bool,
                    "loop_interval": int,
                    "max_concurrent_agents": int,
                    "max_iterations_per_agent": int,
                    "max_cost_per_investigation": float,
                    "max_total_hourly_cost": float,
                    "max_runtime_per_investigation": int,
                    "stale_threshold": int,
                    "workdir_base": str,
                    "dry_run": bool,
                }
                for key, cast in field_map.items():
                    if key in db_config:
                        setattr(config.orchestrator, key, cast(db_config[key]))
                        config.sources[f"orchestrator.{key}"] = "db"
                logger.info("Orchestrator config overridden from database settings")
        except Exception as e:
            logger.debug(
                f"Could not load orchestrator config from DB (using env/defaults): {e}"
            )

        # Kafka: merge non-secret DB settings on top of env defaults
        try:
            from core.storage.config_service import get_config_service

            config_service = get_config_service()
            kafka_db = config_service.get_system_config("kafka.settings")
            if kafka_db and isinstance(kafka_db, dict):
                if "enabled" in kafka_db:
                    config.kafka.enabled = bool(kafka_db["enabled"])
                if "bootstrap_servers" in kafka_db:
                    config.kafka.bootstrap_servers = str(kafka_db["bootstrap_servers"])
                if "consumer_group" in kafka_db:
                    config.kafka.consumer_group = str(kafka_db["consumer_group"])
                if "topics" in kafka_db and isinstance(kafka_db["topics"], list):
                    config.kafka.topics = [str(t) for t in kafka_db["topics"]]
                if "auto_offset_reset" in kafka_db:
                    config.kafka.auto_offset_reset = str(kafka_db["auto_offset_reset"])
                if "security_protocol" in kafka_db:
                    config.kafka.security_protocol = str(kafka_db["security_protocol"])
                if "sasl_mechanism" in kafka_db:
                    config.kafka.sasl_mechanism = kafka_db["sasl_mechanism"] or None
                if "sasl_username" in kafka_db:
                    config.kafka.sasl_username = kafka_db["sasl_username"] or None
                if "max_poll_records" in kafka_db:
                    config.kafka.max_poll_records = int(kafka_db["max_poll_records"])
                if "session_timeout_ms" in kafka_db:
                    config.kafka.session_timeout_ms = int(
                        kafka_db["session_timeout_ms"]
                    )
                logger.info("Kafka config overridden from database settings")
        except Exception as e:
            logger.debug(
                f"Could not load Kafka config from DB (using env/defaults): {e}"
            )

        return config

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.log_level.upper()), format=self.log_format
        )
        logger.info(f"Logging configured at {self.log_level} level")
