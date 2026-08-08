import logging
from dataclasses import dataclass, field

from core.ingestion.kafka_config import KafkaConfig  # re-exported for DaemonConfig
from typing import List, Optional

from core.config import DEFAULT_REDIS_URL, get_settings
from core.secrets import get_secret
from core.llm.defaults import DEFAULT_MODEL

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
    enrich_max_inflight: int = 50          # cap on pending background enrich tasks (backpressure)
    enrich_backfill_enabled: bool = True   # sweep for stored-but-never-enriched findings
    enrich_backfill_interval: int = 300    # seconds between sweeps
    enrich_backfill_batch: int = 50        # findings re-queued per sweep
    enrich_backfill_max_age_hours: int = 168  # only backfill findings newer than this (7d)


@dataclass
class ResponseConfig:
    auto_response_enabled: bool = True
    confidence_threshold: float = 0.90
    force_manual_approval: bool = False
    dry_run: bool = False  # Log actions without executing


@dataclass
class EscalationConfig:
    enabled: bool = True
    escalate_severities: List[str] = field(default_factory=lambda: ["critical", "high"])
    slack_enabled: bool = True
    slack_channel: str = "#soc-alerts"
    pagerduty_enabled: bool = False
    pagerduty_severity_map: dict = field(default_factory=lambda: {
        "critical": "critical",
        "high": "error",
        "medium": "warning",
        "low": "info"
    })


@dataclass
class SchedulerConfig:
    threat_hunt_enabled: bool = True
    threat_hunt_interval: int = 86400  # Daily (24 hours)
    report_generation_enabled: bool = True
    report_interval: int = 604800  # Weekly (7 days)
    cleanup_enabled: bool = True
    cleanup_interval: int = 86400  # Daily
    cleanup_retention_days: int = 90


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
    max_total_daily_cost: float = 100.0
    max_runtime_per_investigation: int = 3600
    stale_threshold: int = 300
    workdir_base: str = "data/investigations"
    auto_assign_findings: bool = True
    auto_assign_severities: List[str] = field(default_factory=lambda: ["critical", "high"])
    dry_run: bool = False
    dedup_window_minutes: int = 30
    agent_loop_delay: int = 2
    context_max_chars: int = 10000
    plan_model: str = DEFAULT_MODEL
    review_model: str = DEFAULT_MODEL
    # Provider that owns plan_model/review_model. Resolved alongside the model
    # from ai_model_configs so autonomous investigations can run on
    # non-Anthropic providers (Ollama/OpenAI/Groq). None means "the default
    # Anthropic provider" and preserves pre-multi-provider behavior.
    plan_provider_id: Optional[str] = None
    review_provider_id: Optional[str] = None


@dataclass
class LLMQueueConfig:
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_llm_calls: int = 5
    triage_timeout: int = 90
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

    # Database
    database_url: Optional[str] = None

    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        config = cls()
        settings = get_settings()

        config.database_url = settings.database_url
        config.log_level = settings.daemon_log_level

        config.polling.splunk_interval = settings.daemon_splunk_poll_interval
        config.polling.crowdstrike_interval = settings.daemon_crowdstrike_poll_interval
        config.polling.webhook_enabled = settings.daemon_webhook_enabled
        config.polling.webhook_port = settings.daemon_webhook_port
        config.polling.webhook_token = get_secret("DAEMON_WEBHOOK_TOKEN") or ""

        config.processing.auto_triage_enabled = settings.daemon_auto_triage
        config.processing.auto_enrich_enabled = settings.daemon_auto_enrich
        config.processing.batch_size = settings.daemon_batch_size
        config.processing.enrich_max_inflight = settings.daemon_enrich_max_inflight
        config.processing.enrich_backfill_enabled = settings.daemon_enrich_backfill
        config.processing.enrich_backfill_interval = settings.daemon_enrich_backfill_interval
        config.processing.enrich_backfill_batch = settings.daemon_enrich_backfill_batch
        config.processing.enrich_backfill_max_age_hours = settings.daemon_enrich_backfill_max_age_hours

        config.response.auto_response_enabled = settings.daemon_auto_response
        config.response.confidence_threshold = settings.daemon_confidence_threshold
        config.response.force_manual_approval = settings.daemon_force_approval
        config.response.dry_run = settings.daemon_dry_run

        config.escalation.enabled = settings.daemon_escalation_enabled
        config.escalation.slack_enabled = (
            True if settings.daemon_slack_enabled is None else settings.daemon_slack_enabled
        )
        config.escalation.slack_channel = settings.daemon_slack_channel
        config.escalation.pagerduty_enabled = settings.daemon_pagerduty_enabled
        config.escalation.escalate_severities = list(settings.daemon_escalate_severities)

        config.scheduler.threat_hunt_enabled = settings.daemon_threat_hunt_enabled
        config.scheduler.threat_hunt_interval = settings.daemon_threat_hunt_interval
        config.scheduler.cleanup_retention_days = settings.daemon_cleanup_retention_days

        config.metrics.enabled = settings.daemon_metrics_enabled
        config.metrics.port = settings.daemon_health_port

        config.orchestrator.enabled = settings.orchestrator_enabled
        config.orchestrator.loop_interval = settings.orchestrator_loop_interval
        config.orchestrator.max_concurrent_agents = settings.orchestrator_max_agents
        config.orchestrator.max_iterations_per_agent = settings.orchestrator_max_iterations
        config.orchestrator.max_cost_per_investigation = settings.orchestrator_max_cost
        config.orchestrator.max_total_hourly_cost = settings.orchestrator_max_hourly_cost
        config.orchestrator.max_total_daily_cost = settings.orchestrator_max_daily_cost
        config.orchestrator.max_runtime_per_investigation = settings.orchestrator_max_runtime
        config.orchestrator.stale_threshold = settings.orchestrator_stale_threshold
        config.orchestrator.workdir_base = settings.orchestrator_workdir
        config.orchestrator.auto_assign_findings = settings.orchestrator_auto_assign
        config.orchestrator.dry_run = settings.orchestrator_dry_run
        config.orchestrator.dedup_window_minutes = settings.orchestrator_dedup_window
        config.orchestrator.agent_loop_delay = settings.orchestrator_agent_loop_delay
        config.orchestrator.context_max_chars = settings.orchestrator_context_max_chars
        config.orchestrator.auto_assign_severities = list(settings.orchestrator_auto_severities)

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
            db_config = config_service.get_system_config('orchestrator.settings')
            if db_config and isinstance(db_config, dict):
                field_map = {
                    'enabled': bool,
                    'loop_interval': int,
                    'max_concurrent_agents': int,
                    'max_iterations_per_agent': int,
                    'max_cost_per_investigation': float,
                    'max_total_hourly_cost': float,
                    'max_total_daily_cost': float,
                    'max_runtime_per_investigation': int,
                    'stale_threshold': int,
                    'workdir_base': str,
                    'auto_assign_findings': bool,
                    'dry_run': bool,
                    'dedup_window_minutes': int,
                    'agent_loop_delay': int,
                    'context_max_chars': int,
                    'plan_model': str,
                    'review_model': str,
                }
                for key, cast in field_map.items():
                    if key in db_config:
                        setattr(config.orchestrator, key, cast(db_config[key]))
                if 'auto_assign_severities' in db_config:
                    val = db_config['auto_assign_severities']
                    if isinstance(val, list):
                        config.orchestrator.auto_assign_severities = val
                logger.info("Orchestrator config overridden from database settings")
        except Exception as e:
            logger.debug(f"Could not load orchestrator config from DB (using env/defaults): {e}")

        # GH #89 — ai_model_configs takes precedence over orchestrator.settings
        # for plan_model/review_model. This is the same override layer that
        # powers the "Model Assignment" section of the AI Config tab.
        try:
            from core.llm.providers.registry import get_registry
            registry = get_registry()
            plan_pick = registry.resolve_model_for_component('orchestrator_plan')
            review_pick = registry.resolve_model_for_component('orchestrator_review')
            # resolve_model_for_component returns (provider_id, model_id). Keep
            # BOTH: the provider_id is what lets the daemon route a non-Anthropic
            # model through Bifrost instead of silently assuming Anthropic.
            if plan_pick is not None:
                config.orchestrator.plan_provider_id = plan_pick[0]
                config.orchestrator.plan_model = plan_pick[1]
            if review_pick is not None:
                config.orchestrator.review_provider_id = review_pick[0]
                config.orchestrator.review_model = review_pick[1]
            if plan_pick or review_pick:
                logger.info(
                    "Orchestrator models resolved from ai_model_configs: "
                    "plan=%s review=%s",
                    config.orchestrator.plan_model,
                    config.orchestrator.review_model,
                )
        except Exception as e:
            logger.debug(f"ai_model_configs override skipped: {e}")

        # Kafka: merge non-secret DB settings on top of env defaults
        try:
            from core.storage.config_service import get_config_service
            config_service = get_config_service()
            kafka_db = config_service.get_system_config('kafka.settings')
            if kafka_db and isinstance(kafka_db, dict):
                if 'enabled' in kafka_db:
                    config.kafka.enabled = bool(kafka_db['enabled'])
                if 'bootstrap_servers' in kafka_db:
                    config.kafka.bootstrap_servers = str(kafka_db['bootstrap_servers'])
                if 'consumer_group' in kafka_db:
                    config.kafka.consumer_group = str(kafka_db['consumer_group'])
                if 'topics' in kafka_db and isinstance(kafka_db['topics'], list):
                    config.kafka.topics = [str(t) for t in kafka_db['topics']]
                if 'auto_offset_reset' in kafka_db:
                    config.kafka.auto_offset_reset = str(kafka_db['auto_offset_reset'])
                if 'security_protocol' in kafka_db:
                    config.kafka.security_protocol = str(kafka_db['security_protocol'])
                if 'sasl_mechanism' in kafka_db:
                    config.kafka.sasl_mechanism = kafka_db['sasl_mechanism'] or None
                if 'sasl_username' in kafka_db:
                    config.kafka.sasl_username = kafka_db['sasl_username'] or None
                if 'max_poll_records' in kafka_db:
                    config.kafka.max_poll_records = int(kafka_db['max_poll_records'])
                if 'session_timeout_ms' in kafka_db:
                    config.kafka.session_timeout_ms = int(kafka_db['session_timeout_ms'])
                logger.info("Kafka config overridden from database settings")
        except Exception as e:
            logger.debug(f"Could not load Kafka config from DB (using env/defaults): {e}")

        return config

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.log_level.upper()),
            format=self.log_format
        )
        logger.info(f"Logging configured at {self.log_level} level")
