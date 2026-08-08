import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

_VIGIL_DIRNAME = '.vigil'
_LEGACY_DIRNAME = '.deeptempo'

REQUEST_TIMEOUT = 30
STREAM_TIMEOUT = 120

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_SANDBOX_FILE_TYPES = "exe,dll,doc,docx,xls,xlsx,pdf,js,vbs,ps1,bat,msi"


# Reads prefer ~/.vigil and fall back to the legacy ~/.deeptempo copy; writes
# always target ~/.vigil, so data drifts over on the next save.
def vigil_path(*parts: str, write: bool = False) -> Path:
    target = Path.home() / _VIGIL_DIRNAME  # per call, so tests can patch home
    legacy = Path.home() / _LEGACY_DIRNAME
    if parts:
        target, legacy = target.joinpath(*parts), legacy.joinpath(*parts)
    if write:
        (target.parent if parts else target).mkdir(parents=True, exist_ok=True)
        return target
    if not target.exists() and legacy.exists():
        return legacy
    return target


REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Anchored to the repo so the same .env loads regardless of working directory.
    # Real env vars still win, keeping container and Helm injection authoritative.
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime
    dev_mode: bool = False
    testing: bool = False
    environment: str = "development"
    release_version: str = "unknown"
    demo_mode: Optional[bool] = None
    data_backend: str = "database"
    autostart_services: Optional[str] = None
    max_upload_size_mb: int = 500
    vigil_context_path: str = ""
    vigil_frontend_url: str = ""
    mempalace_palace_path: Optional[str] = None
    # Call sites disagree on the default (shared_intel off, orchestrator on), so
    # this stays tri-state and each site supplies its own fallback.
    mempalace_daemon_enabled: Optional[bool] = None

    # Database
    database_url: Optional[str] = None
    postgresql_connection_string: Optional[str] = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "deeptempo_soc"
    postgres_user: str = "deeptempo"
    postgres_ssl_mode: str = "prefer"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 3600
    db_config_check_interval: float = 5.0

    # Redis / queue. None means "no Redis configured" — the rate limiter falls back
    # to in-memory on None, so a default here would silently change its behavior.
    redis_url: Optional[str] = None
    llm_max_concurrent: int = 5

    # HTTP security
    vigil_cors_origins: Optional[str] = None
    vigil_csp_policy: Optional[str] = None
    vigil_csp_enabled: bool = True
    vigil_hsts_enabled: bool = True
    vigil_hsts_max_age: int = 31536000
    vigil_frame_options_enabled: bool = True
    vigil_content_type_options_enabled: bool = True
    vigil_referrer_policy_enabled: bool = True
    vigil_csrf_enabled: bool = True
    vigil_csrf_report_only: bool = True
    vigil_csrf_exempt_paths: Optional[str] = None
    vigil_cookie_secure: bool = True
    vigil_cookie_samesite: str = "strict"

    # Auth
    jwt_access_expiration_minutes: int = 30
    jwt_refresh_expiration_days: int = 7
    auth_lockout_threshold: int = 5
    auth_lockout_duration_minutes: int = 15
    auth_password_history_limit: int = 5
    auth_min_password_length: int = 12
    # bcrypt raises above 72 bytes rather than truncating, so a higher ceiling
    # here means a long password validates and then 500s at hash time.
    auth_max_password_bytes: int = 72
    auth_min_zxcvbn_score: int = 3
    password_reset_ttl_seconds: int = 3600
    revocation_fail_open: bool = False

    # LLM / gateway
    # Host-run default: `bifrost` resolves only inside the compose network, and
    # compose, Helm and start.sh all inject the right hostname explicitly.
    bifrost_url: str = "http://localhost:8080"
    anthropic_base_url: str = ""
    ollama_url: str = "http://localhost:11434"
    default_model: str = "claude-sonnet-4-6"
    ollama_extra_tool_models: str = ""
    model_catalog_refresh_interval_s: int = 300
    prompt_injection_block: bool = False
    mcp_auto_connect_on_startup: Optional[bool] = None
    llm_budget_unlimited: bool = False
    extension_connector_allowlist: Annotated[List[str], NoDecode] = []

    # Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_username: Optional[str] = None
    smtp_from: str = "noreply@vigil.local"
    smtp_tls: bool = True
    vigil_email_backend: str = "console"

    # Observability
    sentry_dsn: str = ""
    vigil_otel_enabled: bool = False
    vigil_otel_record_llm_content: bool = False
    vigil_otel_record_ioc_values: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    # Daemon
    daemon_log_level: str = "INFO"
    daemon_splunk_poll_interval: int = 300
    daemon_crowdstrike_poll_interval: int = 60
    daemon_webhook_enabled: bool = True
    daemon_webhook_port: int = 8081
    daemon_auto_triage: bool = True
    daemon_auto_enrich: bool = True
    daemon_batch_size: int = 10
    daemon_enrich_max_inflight: int = 50
    daemon_enrich_backfill: bool = True
    daemon_enrich_backfill_interval: int = 300
    daemon_enrich_backfill_batch: int = 50
    daemon_enrich_backfill_max_age_hours: int = 168
    daemon_auto_response: bool = True
    daemon_confidence_threshold: float = 0.90
    daemon_force_approval: bool = False
    daemon_dry_run: bool = False
    daemon_escalation_enabled: bool = True
    daemon_escalate_severities: Annotated[List[str], NoDecode] = ["critical", "high"]
    # Call sites disagree on the default (config.from_env on, orchestrator off), so
    # this stays tri-state and each site supplies its own fallback.
    daemon_slack_enabled: Optional[bool] = None
    daemon_slack_channel: str = "#soc-alerts"
    daemon_pagerduty_enabled: bool = False
    daemon_threat_hunt_enabled: bool = True
    daemon_threat_hunt_interval: int = 86400
    daemon_cleanup_retention_days: int = 90
    daemon_metrics_enabled: bool = True
    daemon_health_host: str = "localhost"
    daemon_health_port: int = 9091

    # Orchestrator
    orchestrator_enabled: bool = False
    orchestrator_loop_interval: int = 60
    orchestrator_max_agents: int = 3
    orchestrator_max_iterations: int = 50
    orchestrator_max_cost: float = 5.0
    orchestrator_max_hourly_cost: float = 20.0
    orchestrator_max_daily_cost: float = 100.0
    orchestrator_max_runtime: int = 3600
    orchestrator_stale_threshold: int = 300
    orchestrator_workdir: str = "data/investigations"
    orchestrator_auto_assign: bool = True
    orchestrator_auto_severities: Annotated[List[str], NoDecode] = ["critical", "high"]
    orchestrator_dry_run: bool = False
    orchestrator_dedup_window: int = 30
    orchestrator_agent_loop_delay: int = 2
    orchestrator_context_max_chars: int = 10000

    # Kafka ingestion. Credentials go through the secrets store, not here.
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_consumer_group: str = "vigil-soc"
    kafka_topics: Annotated[List[str], NoDecode] = []
    kafka_auto_offset_reset: str = "latest"
    kafka_max_poll_records: int = 500
    kafka_session_timeout_ms: int = 30000
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_sasl_mechanism: Optional[str] = None
    kafka_ssl_ca_location: Optional[str] = None

    # Ingestion / webhooks
    darktrace_enabled: bool = False
    darktrace_url: str = ""
    darktrace_max_body_kb: int = 1024
    cloudy_ingestion_enabled: bool = False
    cloudy_webhook_max_body_kb: int = 1024
    threat_feed_poll_interval: int = 900

    # Sandbox
    sandbox_auto_submit: bool = False
    sandbox_poll_interval: int = 60
    sandbox_allowed_file_types: str = DEFAULT_SANDBOX_FILE_TYPES
    sandbox_max_file_size_mb: int = 100
    sandbox_analysis_timeout: int = 300
    joe_sandbox_enabled: bool = False
    joe_sandbox_url: str = "https://jbxcloud.joesecurity.org/api"
    cape_sandbox_enabled: bool = False
    cape_sandbox_url: str = ""
    hybrid_analysis_enabled: bool = False
    anyrun_enabled: bool = False

    @field_validator(
        "extension_connector_allowlist",
        "daemon_escalate_severities",
        "orchestrator_auto_severities",
        "kafka_topics",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator(
        "demo_mode",
        "mempalace_daemon_enabled",
        "daemon_slack_enabled",
        "mcp_auto_connect_on_startup",
        mode="before",
    )
    # Tri-state: blank means "no opinion, use the call site's fallback".
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def is_demo_mode() -> bool:
    enabled = get_settings().demo_mode
    if enabled is not None:
        return enabled
    return get_general_config('demo_mode', False)


def _load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Config load error {path}: {e}")
        return {}


def get_integration_config(integration_id: str) -> dict[str, Any]:
    data = _load_json_config(vigil_path('integrations_config.json'))
    if integration_id not in data.get('enabled_integrations', []):
        return {}
    return data.get('integrations', {}).get(integration_id, {})


def is_integration_enabled(integration_id: str) -> bool:
    data = _load_json_config(vigil_path('integrations_config.json'))
    return integration_id in data.get('enabled_integrations', [])


def get_general_config(key: str, default: Any = None) -> Any:
    data = _load_json_config(vigil_path('general_config.json'))
    return data.get(key, default)
