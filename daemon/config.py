"""Daemon configuration management."""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PollingConfig:
    """Configuration for data source polling intervals."""
    splunk_interval: int = 300  # 5 minutes
    crowdstrike_interval: int = 60  # 1 minute
    generic_interval: int = 120  # 2 minutes for other sources
    webhook_enabled: bool = True
    webhook_port: int = 8081


@dataclass
class ProcessingConfig:
    """Configuration for AI processing pipeline."""
    auto_triage_enabled: bool = True
    auto_enrich_enabled: bool = True
    batch_size: int = 10
    max_concurrent_tasks: int = 5
    triage_timeout: int = 60  # seconds


@dataclass
class ResponseConfig:
    """Configuration for autonomous response."""
    auto_response_enabled: bool = True
    confidence_threshold: float = 0.90
    force_manual_approval: bool = False
    dry_run: bool = False  # Log actions without executing


@dataclass
class EscalationConfig:
    """Configuration for alert escalation."""
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
    """Configuration for scheduled tasks."""
    threat_hunt_enabled: bool = True
    threat_hunt_interval: int = 86400  # Daily (24 hours)
    report_generation_enabled: bool = True
    report_interval: int = 604800  # Weekly (7 days)
    cleanup_enabled: bool = True
    cleanup_interval: int = 86400  # Daily
    cleanup_retention_days: int = 90


@dataclass
class MetricsConfig:
    """Configuration for metrics/health endpoint."""
    enabled: bool = True
    port: int = 9091
    path: str = "/metrics"


@dataclass
class OrchestratorConfig:
    """Configuration for the autonomous agent orchestrator."""
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
    plan_model: str = "claude-sonnet-4-5-20250929"
    review_model: str = "claude-sonnet-4-5-20250929"


@dataclass
class KafkaConfig:
    """Configuration for Kafka ingestion.

    MVP scope: JSON-only deserialization, single consumer group,
    enable/disable + bootstrap servers + topics overridable via
    ``SystemConfig["kafka.settings"]``. Secrets (SASL password,
    SSL cert path) stay in env only.
    """
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


@dataclass
class LLMQueueConfig:
    """Configuration for the ARQ-based LLM request queue."""
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_llm_calls: int = 5
    triage_timeout: int = 90
    investigation_timeout: int = 180
    chat_timeout: int = 120
    session_ttl: int = 14400  # 4 hours


@dataclass
class DaemonConfig:
    """Main daemon configuration."""
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
        """Load configuration from environment variables."""
        config = cls()
        
        # Database
        config.database_url = os.getenv("DATABASE_URL")
        
        # Logging
        config.log_level = os.getenv("DAEMON_LOG_LEVEL", "INFO")
        
        # Polling intervals
        config.polling.splunk_interval = int(os.getenv("DAEMON_SPLUNK_POLL_INTERVAL", "300"))
        config.polling.crowdstrike_interval = int(os.getenv("DAEMON_CROWDSTRIKE_POLL_INTERVAL", "60"))
        config.polling.webhook_enabled = os.getenv("DAEMON_WEBHOOK_ENABLED", "true").lower() == "true"
        config.polling.webhook_port = int(os.getenv("DAEMON_WEBHOOK_PORT", "8081"))
        
        # Processing
        config.processing.auto_triage_enabled = os.getenv("DAEMON_AUTO_TRIAGE", "true").lower() == "true"
        config.processing.auto_enrich_enabled = os.getenv("DAEMON_AUTO_ENRICH", "true").lower() == "true"
        config.processing.batch_size = int(os.getenv("DAEMON_BATCH_SIZE", "10"))
        
        # Response
        config.response.auto_response_enabled = os.getenv("DAEMON_AUTO_RESPONSE", "true").lower() == "true"
        config.response.confidence_threshold = float(os.getenv("DAEMON_CONFIDENCE_THRESHOLD", "0.90"))
        config.response.force_manual_approval = os.getenv("DAEMON_FORCE_APPROVAL", "false").lower() == "true"
        config.response.dry_run = os.getenv("DAEMON_DRY_RUN", "false").lower() == "true"
        
        # Escalation
        config.escalation.enabled = os.getenv("DAEMON_ESCALATION_ENABLED", "true").lower() == "true"
        config.escalation.slack_enabled = os.getenv("DAEMON_SLACK_ENABLED", "true").lower() == "true"
        config.escalation.slack_channel = os.getenv("DAEMON_SLACK_CHANNEL", "#soc-alerts")
        config.escalation.pagerduty_enabled = os.getenv("DAEMON_PAGERDUTY_ENABLED", "false").lower() == "true"
        
        severities = os.getenv("DAEMON_ESCALATE_SEVERITIES", "critical,high")
        config.escalation.escalate_severities = [s.strip() for s in severities.split(",")]
        
        # Scheduler
        config.scheduler.threat_hunt_enabled = os.getenv("DAEMON_THREAT_HUNT_ENABLED", "true").lower() == "true"
        config.scheduler.threat_hunt_interval = int(os.getenv("DAEMON_THREAT_HUNT_INTERVAL", "86400"))
        config.scheduler.cleanup_retention_days = int(os.getenv("DAEMON_CLEANUP_RETENTION_DAYS", "90"))
        
        # Metrics
        config.metrics.enabled = os.getenv("DAEMON_METRICS_ENABLED", "true").lower() == "true"
        config.metrics.port = int(os.getenv("DAEMON_HEALTH_PORT", "9091"))
        
        # Orchestrator
        config.orchestrator.enabled = os.getenv("ORCHESTRATOR_ENABLED", "false").lower() == "true"
        config.orchestrator.loop_interval = int(os.getenv("ORCHESTRATOR_LOOP_INTERVAL", "60"))
        config.orchestrator.max_concurrent_agents = int(os.getenv("ORCHESTRATOR_MAX_AGENTS", "3"))
        config.orchestrator.max_iterations_per_agent = int(os.getenv("ORCHESTRATOR_MAX_ITERATIONS", "50"))
        config.orchestrator.max_cost_per_investigation = float(os.getenv("ORCHESTRATOR_MAX_COST", "5.0"))
        config.orchestrator.max_total_hourly_cost = float(os.getenv("ORCHESTRATOR_MAX_HOURLY_COST", "20.0"))
        config.orchestrator.max_total_daily_cost = float(os.getenv("ORCHESTRATOR_MAX_DAILY_COST", "100.0"))
        config.orchestrator.max_runtime_per_investigation = int(os.getenv("ORCHESTRATOR_MAX_RUNTIME", "3600"))
        config.orchestrator.stale_threshold = int(os.getenv("ORCHESTRATOR_STALE_THRESHOLD", "300"))
        config.orchestrator.workdir_base = os.getenv("ORCHESTRATOR_WORKDIR", "data/investigations")
        config.orchestrator.auto_assign_findings = os.getenv("ORCHESTRATOR_AUTO_ASSIGN", "true").lower() == "true"
        config.orchestrator.dry_run = os.getenv("ORCHESTRATOR_DRY_RUN", "false").lower() == "true"
        config.orchestrator.dedup_window_minutes = int(os.getenv("ORCHESTRATOR_DEDUP_WINDOW", "30"))
        
        severities = os.getenv("ORCHESTRATOR_AUTO_SEVERITIES", "critical,high")
        config.orchestrator.auto_assign_severities = [s.strip() for s in severities.split(",")]
        
        # LLM Queue
        config.llm_queue.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        config.llm_queue.max_concurrent_llm_calls = int(os.getenv("LLM_MAX_CONCURRENT", "5"))

        # Kafka ingestion (env defaults; may be overridden by SystemConfig below)
        config.kafka.enabled = os.getenv("KAFKA_ENABLED", "false").lower() == "true"
        config.kafka.bootstrap_servers = os.getenv(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        config.kafka.consumer_group = os.getenv("KAFKA_CONSUMER_GROUP", "vigil-soc")
        topics = os.getenv("KAFKA_TOPICS", "").strip()
        config.kafka.topics = [t.strip() for t in topics.split(",") if t.strip()]
        config.kafka.auto_offset_reset = os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest")
        config.kafka.max_poll_records = int(os.getenv("KAFKA_MAX_POLL_RECORDS", "500"))
        config.kafka.session_timeout_ms = int(
            os.getenv("KAFKA_SESSION_TIMEOUT_MS", "30000")
        )
        config.kafka.security_protocol = os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
        config.kafka.sasl_mechanism = os.getenv("KAFKA_SASL_MECHANISM") or None
        config.kafka.sasl_username = os.getenv("KAFKA_SASL_USERNAME") or None
        config.kafka.sasl_password = os.getenv("KAFKA_SASL_PASSWORD") or None
        config.kafka.ssl_ca_location = os.getenv("KAFKA_SSL_CA_LOCATION") or None

        # Override with DB-persisted settings (set via Settings UI)
        try:
            from database.config_service import get_config_service
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

        # Kafka: merge non-secret DB settings on top of env defaults
        try:
            from database.config_service import get_config_service
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
        """Configure logging based on settings."""
        logging.basicConfig(
            level=getattr(logging, self.log_level.upper()),
            format=self.log_format
        )
        logger.info(f"Logging configured at {self.log_level} level")
