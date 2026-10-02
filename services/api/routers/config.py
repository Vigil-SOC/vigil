"""Configuration API endpoints."""

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.v1.findings_router import data_service as findings_data_service
from core.config import (
    get_settings,
    is_demo_mode,
    load_integrations_config,
    state_dir_status,
    vigil_path,
)
from core.deps import (
    provide_demo_data,
    provide_detection_rules,
    provide_integration_bridge,
    provide_mcp_client,
)
from core.detections.detection_rules_service import DetectionRulesService
from core.integrations._base.descriptor import iter_descriptors
from core.integrations.integration_bridge_service import IntegrationBridgeService
from core.integrations.integration_secrets import (
    redact_secrets,
    secret_fields_for,
    split_secrets,
)
from core.intent import intent_file
from core.llm.defaults import DEFAULT_MODEL
from core.response.approval_service import APPROVAL_CONFIG_KEY
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import get_secret, set_secret
from core.secrets_manager import get_secrets_manager
from core.storage.config_service import get_config_service
from core.storage.models import AIModelConfig, CustomAgent, User
from core.time import utcnow
from services.api.middleware.auth import (
    get_current_active_user,
    require_integrations_admin,
)
from services.daemon.config import DaemonConfig
from services.daemon.intent import effective_daemon_config, intent_report

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/config",
    tags=["config"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


def _for_user(user: User):
    """Config service stamped with the signed-in user for the audit row."""
    return get_config_service(user_id=str(user.user_id))


def _mirror_to_file(filename: str, config_data: Dict[str, Any]) -> None:
    """Copy config the database already owns, for backward compatibility.

    Best-effort: the database write has committed, so an unwritable State
    Directory must not fail a request that succeeded.
    """
    try:
        with open(vigil_path(filename, write=True), "w") as f:
            json.dump(config_data, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not mirror {filename} to the State Directory: {e}")


class ClaudeConfig(BaseModel):
    """Claude API configuration."""

    api_key: str


class S3Config(BaseModel):
    """S3 configuration."""

    bucket_name: str
    region: str = "us-east-1"
    auth_method: str = "credentials"  # "credentials" or "profile"
    aws_profile: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    parquet_prefix: str = ""


class ThemeConfig(BaseModel):
    """Theme configuration."""

    theme: str = "dark"  # dark or light


class IntegrationsConfig(BaseModel):
    """Integrations configuration."""

    enabled_integrations: list[str] = []
    integrations: dict = {}


class GeneralConfig(BaseModel):
    """General application settings."""

    auto_start_sync: bool = False
    show_notifications: bool = True
    theme: str = "dark"
    enable_keyring: bool = False  # Whether to use OS keyring for secrets


class GitHubConfig(BaseModel):
    """GitHub integration configuration."""

    token: str


class PostgreSQLConfig(BaseModel):
    """PostgreSQL database backend configuration."""

    connection_string: str


class DemoModeConfig(BaseModel):
    """Demo mode configuration."""

    enabled: bool = False


class PlatformDatabaseProxyConfig(BaseModel):
    """Proxy configuration in front of the platform metadata Postgres.

    Persisted in the encrypted secrets store (DB-independent — read at
    boot before the engine exists). All fields optional; ``proxy_type``
    of ``"none"`` (the default) disables the feature.

    Empty-string secrets on POST mean "leave existing value untouched".
    """

    proxy_type: str = "none"  # none | pgbouncer | ssh_tunnel
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    proxy_password: str = ""
    ssh_private_key_path: str = ""
    ssh_key_passphrase: str = ""
    verify_proxy_tls: bool = True


@router.get("/demo-mode")
def get_demo_mode():
    """
    Get demo mode configuration.

    Returns:
        Demo mode status
    """
    try:
        demo_enabled = is_demo_mode()
        env_set = get_settings().demo_mode is not None  # supplied at all, true or false

        return {
            "enabled": demo_enabled,
            "source": "environment" if env_set else "config",
            "description": "Demo mode uses generated sample data instead of database",
        }
    except Exception as e:
        logger.error(f"Error getting demo mode: {e}")
        return {"enabled": False, "error": str(e)}


@router.post("/demo-mode")
def set_demo_mode(config: DemoModeConfig):
    """
    Set demo mode configuration.

    Note: Setting via API updates the config file. Environment variable takes precedence.

    Args:
        config: Demo mode configuration

    Returns:
        Success status
    """
    source_file = vigil_path("general_config.json")
    config_file = vigil_path("general_config.json", write=True)

    # Load existing config
    existing = {}
    if source_file.exists():
        with open(source_file, "r") as f:
            existing = json.load(f)

    # Update demo_mode setting
    existing["demo_mode"] = config.enabled

    with open(config_file, "w") as f:
        json.dump(existing, f, indent=2)

    return {
        "success": True,
        "enabled": config.enabled,
        "message": f"Demo mode {'enabled' if config.enabled else 'disabled'}. Restart the server for changes to take effect.",
        "note": "Set DEMO_MODE=true environment variable for immediate effect without restart",
    }


@router.post("/demo-mode/reset")
def reset_demo_data(demo_service=Depends(provide_demo_data)):
    """
    Reset demo data to regenerate sample findings and cases.

    Returns:
        Success status
    """
    if demo_service is None:
        raise HTTPException(status_code=400, detail="Demo mode is not enabled")

    demo_service.reset()

    return {
        "success": True,
        "message": "Demo data regenerated",
        "findings_count": len(demo_service.get_findings()),
        "cases_count": len(demo_service.get_cases()),
    }


@router.get("/claude")
def get_claude_config():
    """
    Get Claude API configuration status.

    Returns:
        Configuration status (without exposing the key)
    """
    try:
        # Try new key names first, then legacy names
        api_key = (
            get_secret("CLAUDE_API_KEY")
            or get_secret("ANTHROPIC_API_KEY")
            or get_secret("claude_api_key")
            or get_secret("anthropic_api_key")
        )

        has_key = bool(api_key)

        return {
            "configured": has_key,
            "key_preview": f"{api_key[:8]}..." if has_key else None,
        }
    except Exception as e:
        logger.error(f"Error getting Claude config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/claude")
def set_claude_config(config: ClaudeConfig):
    """
    Set Claude API configuration.

    Args:
        config: Claude configuration

    Returns:
        Success status
    """
    # Use standard environment variable name
    success = set_secret("CLAUDE_API_KEY", config.api_key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save API key")

    # GH #88: keep the new llm_provider_configs table in sync so the
    # Settings "AI Config" tab and the legacy endpoint agree on the
    # Anthropic default. Best-effort — a DB failure here (including the
    # commit) must NOT block the secret write that already succeeded, so
    # this runs in its own transaction rather than the request's.
    try:
        from core.storage.models import LLMProviderConfig
        from core.storage.unit_of_work import unit_of_work

        with unit_of_work() as session:
            row = session.get(LLMProviderConfig, "anthropic-default")
            if row is None:
                row = LLMProviderConfig(
                    provider_id="anthropic-default",
                    provider_type="anthropic",
                    name="Anthropic (default)",
                    api_key_ref="CLAUDE_API_KEY",
                    default_model=DEFAULT_MODEL,
                    is_active=True,
                    is_default=True,
                    config={},
                )
                session.add(row)
            else:
                row.api_key_ref = "CLAUDE_API_KEY"
                row.is_active = True
    except Exception as sync_err:  # noqa: BLE001
        logger.warning(
            "Legacy /config/claude could not sync llm_provider_configs: %s",
            sync_err,
        )

    return {"success": True, "message": "API key saved securely"}


@router.get("/s3")
def get_s3_config():
    """
    Get S3 configuration status.

    Returns:
        Configuration status
    """
    try:
        # Try database first
        config_service = get_config_service()
        s3_integration = config_service.get_integration_config("s3")

        if s3_integration and s3_integration.get("config"):
            config = s3_integration["config"]
            return {
                "configured": True,
                "bucket_name": config.get("bucket_name"),
                "region": config.get("region"),
                "parquet_prefix": config.get("parquet_prefix", ""),
                "auth_method": config.get("auth_method", "credentials"),
                "aws_profile": config.get("aws_profile", ""),
            }

        # Fallback to file-based config
        config_file = vigil_path("s3_config.json")
        if config_file.exists():
            with open(config_file, "r") as f:
                config = json.load(f)
                return {
                    "configured": True,
                    "bucket_name": config.get("bucket_name"),
                    "region": config.get("region"),
                    "parquet_prefix": config.get("parquet_prefix", ""),
                    "auth_method": config.get("auth_method", "credentials"),
                    "aws_profile": config.get("aws_profile", ""),
                }

        return {"configured": False}
    except Exception as e:
        logger.error(f"Error getting S3 config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/s3")
def set_s3_config(
    config: S3Config,
    current_user: User = Depends(get_current_active_user),
):
    """
    Set S3 configuration.

    Args:
        config: S3 configuration

    Returns:
        Success status
    """
    bucket_name = config.bucket_name
    parquet_prefix = config.parquet_prefix

    # Parse s3:// URIs: extract bucket name and use the path as prefix
    if bucket_name.startswith("s3://"):
        stripped = bucket_name[5:]
        parts = stripped.split("/", 1)
        bucket_name = parts[0]
        if len(parts) > 1 and parts[1]:
            path = parts[1].rstrip("/")
            # If the path ends with a file extension, trim to the parent directory
            last_segment = path.rsplit("/", 1)[-1] if "/" in path else path
            if "." in last_segment:
                path = path.rsplit("/", 1)[0] if "/" in path else ""
            parquet_prefix = (path + "/") if path else ""

    config_data = {
        "bucket_name": bucket_name,
        "region": config.region,
        "parquet_prefix": parquet_prefix,
        "auth_method": config.auth_method,
        "aws_profile": config.aws_profile,
    }

    # Save to database
    config_service = _for_user(current_user)
    success = config_service.set_integration_config(
        integration_id="s3",
        config=config_data,
        enabled=True,
        integration_name="AWS S3",
        integration_type="storage",
        description="AWS S3 storage configuration",
        change_reason="Updated via Settings UI",
    )

    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to save S3 config to database"
        )

    _mirror_to_file("s3_config.json", config_data)

    # Only overwrite credentials if new values were provided
    if config.access_key_id:
        set_secret("AWS_ACCESS_KEY_ID", config.access_key_id)
    if config.secret_access_key:
        set_secret("AWS_SECRET_ACCESS_KEY", config.secret_access_key)
    if config.session_token:
        set_secret("AWS_SESSION_TOKEN", config.session_token)
    elif config.access_key_id:
        # Clear session token when new non-STS credentials are provided
        set_secret("AWS_SESSION_TOKEN", "")

    return {"success": True, "message": "S3 configuration saved"}


# Maps the form field names exposed in the UI to the secrets-store keys
# read by ``core.storage.connection._load_platform_db_proxy`` at boot. These
# live in the encrypted secrets store rather than ``SystemConfig`` so
# they're readable before the metadata DB connection exists.
_PLATFORM_DB_PROXY_KEYS = {
    "proxy_type": "PLATFORM_DB_PROXY_TYPE",
    "proxy_host": "PLATFORM_DB_PROXY_HOST",
    "proxy_port": "PLATFORM_DB_PROXY_PORT",
    "proxy_username": "PLATFORM_DB_PROXY_USERNAME",
    "proxy_password": "PLATFORM_DB_PROXY_PASSWORD",
    "ssh_private_key_path": "PLATFORM_DB_SSH_PRIVATE_KEY_PATH",
    "ssh_key_passphrase": "PLATFORM_DB_SSH_KEY_PASSPHRASE",
    "verify_proxy_tls": "PLATFORM_DB_VERIFY_PROXY_TLS",
}
_PLATFORM_DB_SECRET_FIELDS = {"proxy_password", "ssh_key_passphrase"}


@router.get("/platform-database")
def get_platform_database_config():
    """Return the current proxy config in front of the platform DB.

    Secret fields (proxy password, SSH key passphrase) are redacted.
    A boolean ``has_*`` flag indicates whether a value is currently
    stored, so the UI can show "set"/"not set" without exposing the
    plaintext.
    """
    result: Dict[str, Any] = {}
    for field, key in _PLATFORM_DB_PROXY_KEYS.items():
        value = get_secret(key)
        if field in _PLATFORM_DB_SECRET_FIELDS:
            result[f"has_{field}"] = bool(value)
            continue
        if field == "proxy_port":
            try:
                result[field] = int(value) if value else 0
            except (TypeError, ValueError):
                result[field] = 0
            continue
        if field == "verify_proxy_tls":
            if value is None or value == "":
                result[field] = True
            else:
                result[field] = str(value).lower() not in (
                    "false",
                    "0",
                    "no",
                    "off",
                )
            continue
        result[field] = value or ""
    result.setdefault("proxy_type", "none")
    return result


@router.post("/platform-database")
def set_platform_database_config(config: PlatformDatabaseProxyConfig):
    """Persist the platform-DB proxy config to the encrypted secrets
    store. Takes effect on the next backend restart — the live engine
    can't be hot-swapped safely.

    Empty-string secrets mean "leave existing value untouched".
    """
    proxy_type = (config.proxy_type or "none").strip().lower()
    if proxy_type not in ("none", "pgbouncer", "ssh_tunnel"):
        raise HTTPException(
            status_code=400,
            detail=(
                "proxy_type must be one of: none, pgbouncer, ssh_tunnel "
                "(http/socks5 are not supported for the platform DB)"
            ),
        )

    # Non-secret fields — always overwrite so disabling a setting
    # actually clears the stored value.
    set_secret(_PLATFORM_DB_PROXY_KEYS["proxy_type"], proxy_type)
    set_secret(_PLATFORM_DB_PROXY_KEYS["proxy_host"], config.proxy_host or "")
    set_secret(
        _PLATFORM_DB_PROXY_KEYS["proxy_port"],
        str(config.proxy_port) if config.proxy_port else "",
    )
    set_secret(_PLATFORM_DB_PROXY_KEYS["proxy_username"], config.proxy_username or "")
    set_secret(
        _PLATFORM_DB_PROXY_KEYS["ssh_private_key_path"],
        config.ssh_private_key_path or "",
    )
    set_secret(
        _PLATFORM_DB_PROXY_KEYS["verify_proxy_tls"],
        "true" if config.verify_proxy_tls else "false",
    )

    # Secret fields — only overwrite when caller supplied a non-empty
    # value, matching the integrations-config convention.
    if config.proxy_password:
        set_secret(_PLATFORM_DB_PROXY_KEYS["proxy_password"], config.proxy_password)
    if config.ssh_key_passphrase:
        set_secret(
            _PLATFORM_DB_PROXY_KEYS["ssh_key_passphrase"],
            config.ssh_key_passphrase,
        )

    return {
        "success": True,
        "message": (
            "Platform DB proxy configuration saved. "
            "Restart the backend for changes to take effect."
        ),
        "restart_required": True,
    }


@router.post("/s3/test")
def test_s3_connection():
    """
    Test S3 connection with current configuration.

    Returns:
        Connection test result
    """
    from core.storage.s3_service import S3Service

    # Load S3 config
    config_service = get_config_service()
    s3_integration = config_service.get_integration_config("s3")

    if not s3_integration:
        # Fallback to file-based config
        config_file = vigil_path("s3_config.json")
        if config_file.exists():
            with open(config_file, "r") as f:
                s3_integration = json.load(f)
        else:
            raise HTTPException(status_code=400, detail="S3 not configured")

    # Unwrap nested config if present
    cfg = s3_integration
    if isinstance(s3_integration.get("config"), dict):
        cfg = s3_integration["config"]

    auth_method = cfg.get("auth_method", "credentials")
    aws_profile = cfg.get("aws_profile", "")

    if auth_method == "profile" and aws_profile:
        s3_service = S3Service(
            bucket_name=cfg.get("bucket_name"),
            region_name=cfg.get("region", "us-east-1"),
            aws_profile=aws_profile,
        )
    else:
        access_key_id = get_secret("AWS_ACCESS_KEY_ID")
        secret_access_key = get_secret("AWS_SECRET_ACCESS_KEY")

        if not access_key_id or not secret_access_key:
            raise HTTPException(
                status_code=400,
                detail="S3 credentials not found. Please configure S3 in Settings.",
            )

        s3_service = S3Service(
            bucket_name=cfg.get("bucket_name"),
            region_name=cfg.get("region", "us-east-1"),
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    # Test connection
    success, message = s3_service.test_connection()

    if success:
        files = s3_service.list_files()
        return {
            "success": True,
            "message": message,
            "bucket": cfg.get("bucket_name"),
            "region": cfg.get("region", "us-east-1"),
            "files_found": len(files),
        }
    else:
        return {
            "success": False,
            "message": message,
            "bucket": cfg.get("bucket_name"),
            "region": cfg.get("region", "us-east-1"),
        }


@router.get("/theme")
def get_theme_config():
    """
    Get theme configuration.

    Returns:
        Theme configuration
    """
    try:
        # Try database first
        config_service = get_config_service()
        config_value = config_service.get_system_config("theme.current")

        if config_value:
            return config_value

        # Fallback to file-based config
        config_file = vigil_path("theme_config.json")
        if config_file.exists():
            with open(config_file, "r") as f:
                config = json.load(f)
                return {"theme": config.get("theme", "dark")}

        return {"theme": "dark"}
    except Exception as e:
        logger.error(f"Error getting theme config: {e}")
        return {"theme": "dark"}


@router.post("/theme")
def set_theme_config(
    config: ThemeConfig,
    current_user: User = Depends(get_current_active_user),
):
    """
    Set theme configuration.

    Args:
        config: Theme configuration

    Returns:
        Success status
    """
    config_data = {"theme": config.theme}

    # Save to database
    config_service = _for_user(current_user)
    success = config_service.set_system_config(
        key="theme.current",
        value=config_data,
        description="Current UI theme",
        config_type="theme",
        change_reason="Updated via Settings UI",
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to save theme to database")

    _mirror_to_file("theme_config.json", config_data)

    return {"success": True, "message": "Theme saved"}


def assigned_model_ids(session) -> set[str]:
    """Distinct models actually assigned. ``fallback_model`` is not one of them."""
    found: set[str] = set()
    columns = (AIModelConfig.model_id, CustomAgent.model)
    for column in columns:
        for (value,) in session.query(column).all():
            text = value.strip() if isinstance(value, str) else ""
            if text:
                found.add(text)
    return found


def _step(step_id: str, title: str, state_line: str, done: bool, href: str) -> dict:
    return {
        "id": step_id,
        "title": title,
        "state_line": state_line,
        "done": done,
        "href": href,
    }


def build_setup_steps(
    *,
    loaded: dict,
    secrets_set: dict,
    sources: list,
    model_ids: set[str],
    descriptor_count: int,
    alerts_exist: int,
    demo_enabled: bool,
) -> dict:
    """Four setup steps from config that already exists. No ranking, no dismissal."""
    integrations = loaded.get("integrations") or {}
    connected = len(integrations)
    slack = (secrets_set.get("slack") or {}).get("bot_token") is True
    pagerduty = (secrets_set.get("pagerduty") or {}).get("api_token") is True
    notify_done = slack or pagerduty
    rules_done = any(
        source.get("status") == "ready" and (source.get("rule_count") or 0) > 0
        for source in sources
    )
    distinct = len(model_ids)
    if distinct == 0:
        model_line = "No model assigned"
    elif distinct == 1:
        model_line = "All agents use one model"
    else:
        model_line = "Agents use more than one model"
    integrations_href = "/settings?section=integrations"
    return {
        "steps": [
            _step(
                "connect_tools",
                "Connect more tools",
                f"{connected} of {descriptor_count} integrations connected",
                connected >= 1,
                integrations_href,
            ),
            _step(
                "notify",
                "Where Vigil pings you",
                (
                    "Slack or PagerDuty route is set"
                    if notify_done
                    else "No Slack or PagerDuty route yet"
                ),
                notify_done,
                integrations_href,
            ),
            _step(
                "rules",
                "Link detection rules",
                (
                    "Detection rules are on disk"
                    if rules_done
                    else "No detection rules on disk"
                ),
                rules_done,
                "/settings?section=data&tab=detection",
            ),
            _step(
                "per_agent",
                "Pick a model per agent",
                model_line,
                distinct >= 2,
                "/settings?section=ai-config",
            ),
        ],
        "alerts_exist": alerts_exist,
        "demo_enabled": demo_enabled,
    }


@router.get("/setup-steps")
def get_setup_steps(
    session: UnitOfWorkSession,
    detection_rules: DetectionRulesService = Depends(provide_detection_rules),
):
    """Home's setup list: tools, a notify route, rules on disk, and model variety."""
    loaded = load_integrations_config(get_config_service())
    return build_setup_steps(
        loaded=loaded,
        secrets_set=_secrets_set_map(loaded.get("integrations") or {}),
        sources=detection_rules.list_sources(),
        model_ids=assigned_model_ids(session),
        descriptor_count=len(iter_descriptors()),
        alerts_exist=findings_data_service.count_findings(),
        demo_enabled=is_demo_mode(),
    )


def _secrets_set_map(integrations: dict) -> dict:
    """Per-integration ``{secret_field: bool}`` — booleans only, never the
    values, so the wizard can show "saved" without the browser seeing a secret."""
    result: dict = {}
    for iid in integrations:
        fields = secret_fields_for(iid)
        if fields:
            result[iid] = {
                field: bool(get_secret(env)) for field, env in fields.items()
            }
    return result


@router.get("/integrations")
def get_integrations_config():
    """
    Get integrations configuration.

    Returns:
        Configuration status and enabled integrations
    """
    try:
        # Same reader the daemon uses: database rows when the table has any,
        # JSON file only when it is empty or unreachable.
        loaded = load_integrations_config(get_config_service())
        if not loaded["configured"]:
            return {
                "configured": False,
                "enabled_integrations": [],
                "integrations": {},
            }

        # Redact registered secret fields so the frontend never receives
        # plaintext credentials. Pre-secret-store rows may still contain
        # them — strip on read so any legacy plaintext is sanitized.
        redacted = {
            iid: redact_secrets(iid, cfg or {})
            for iid, cfg in loaded["integrations"].items()
        }
        return {
            "configured": True,
            "enabled_integrations": loaded["enabled_integrations"],
            "integrations": redacted,
            "secrets_set": _secrets_set_map(redacted),
        }
    except Exception as e:
        logger.error(f"Error getting integrations config: {e}")
        return {
            "configured": False,
            "enabled_integrations": [],
            "integrations": {},
            "error": str(e),
        }


@router.post("/integrations")
def set_integrations_config(
    config: IntegrationsConfig,
    current_user: User = Depends(get_current_active_user),
    bridge: IntegrationBridgeService = Depends(provide_integration_bridge),
):
    """
    Set integrations configuration.

    Secret-typed fields (registered in ``core.integrations.integration_secrets``) are
    routed to the encrypted secrets store via ``set_secret`` and stripped
    from the dict that lands in the DB / JSON file. Empty strings are
    treated as "keep existing secret" (matches the S3 endpoint convention)
    so editing non-secret fields without re-typing the password doesn't
    clobber stored credentials. A failed secret write or integration-config
    row is HTTP 500; the detail names the integration and field, never the value.

    Args:
        config: Integrations configuration

    Returns:
        Success status
    """
    config_service = _for_user(current_user)

    # Build a sanitized integrations dict (no secrets) for DB/JSON
    # persistence. Apply secret writes to the encrypted store.
    sanitized_integrations: dict = {}
    not_stored: list[str] = []
    for integration_id, raw_config in config.integrations.items():
        secrets, non_secrets = split_secrets(integration_id, raw_config)
        field_by_env = {
            env: field for field, env in secret_fields_for(integration_id).items()
        }

        # Empty string ⇒ user didn't re-type the secret on edit; leave
        # the existing encrypted value untouched. Non-empty ⇒ overwrite.
        for env_key, value in secrets.items():
            if value == "":
                continue
            if not set_secret(env_key, value):
                field = field_by_env.get(env_key, env_key)
                logger.error(
                    f"Failed to write secret '{env_key}' for "
                    f"integration '{integration_id}'"
                )
                not_stored.append(
                    f"integration '{integration_id}' field '{field}' ({env_key})"
                )

        sanitized_integrations[integration_id] = non_secrets

        enabled = integration_id in config.enabled_integrations
        success = config_service.set_integration_config(
            integration_id=integration_id,
            config=non_secrets,
            enabled=enabled,
            change_reason="Updated via Settings UI",
        )
        if not success:
            logger.error(f"Failed to save integration '{integration_id}'")
            not_stored.append(f"integration '{integration_id}' config")

    if not_stored:
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to save integrations configuration: "
                + "; ".join(f"{item} was not stored" for item in not_stored)
            ),
        )

    _mirror_to_file(
        "integrations_config.json",
        {
            "enabled_integrations": config.enabled_integrations,
            "integrations": sanitized_integrations,
        },
    )

    # Derive <ID>_MCP_URL env vars (e.g. LOGLM_MCP_URL) from any
    # connectorUrl just saved, so static mcp-config.json remote-MCP
    # entries resolve without a separately-set env var. Best-effort.
    try:
        bridge.derive_remote_mcp_env()
    except Exception as e:
        logger.warning(f"Could not derive remote MCP env vars: {e}")

    return {"success": True, "message": "Integrations configuration saved"}


@router.get("/state-directory")
def get_state_directory():
    """Resolved State Directory path and writability.

    Authenticated: /api/health carries only the booleans, since it is public and
    this names where credentials live.
    """
    return {"success": True, "state_directory": state_dir_status()}


@router.get("/integrations/status")
def get_integrations_status(
    bridge: IntegrationBridgeService = Depends(provide_integration_bridge),
):
    """
    Get status of all integrations.

    Returns:
        Status information for all integrations
    """
    statuses = bridge.get_all_integration_statuses()

    return {"success": True, "statuses": statuses}


def _connected_session(mcp_client: Any, server_name: str) -> Any:
    """Live session for a server that is already connected, if any.

    ``connect_to_server`` returns early in that case and does not call
    ``list_tools``. Anything else (including a MagicMock client) is not a session.
    """
    sessions = getattr(mcp_client, "persistent_sessions", None)
    if not isinstance(sessions, dict):
        return None
    holder = sessions.get(server_name)
    if holder is None or not getattr(holder, "is_connected", False):
        return None
    return getattr(holder, "session", None)


async def _probe_mcp_server(
    mcp_client: Any,
    server_name: str,
    *,
    persistent: bool,
    skip_enabled_check: bool,
) -> Dict[str, Any]:
    """Connect and, when a session was already up, list its tools."""
    already = _connected_session(mcp_client, server_name)
    error: Optional[str] = None
    missing: Optional[List[str]] = None
    try:
        ok = await mcp_client.connect_to_server(
            server_name,
            persistent=persistent,
            skip_enabled_check=skip_enabled_check,
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    else:
        if ok and already is not None:
            try:
                await already.list_tools()
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = f"{type(exc).__name__}: {exc}"
        if not ok and error is None:
            error = mcp_client.get_last_error(server_name)
            missing = mcp_client.get_missing_credentials(server_name)

    result: Dict[str, Any] = {"name": server_name, "success": bool(ok)}
    if not ok:
        result["error"] = error or "connection failed"
    if missing:
        result["missing_credentials"] = missing
    return result


def _probe_error_summary(servers: List[Dict[str, Any]]) -> Optional[str]:
    parts = []
    for server in servers:
        if server["success"]:
            continue
        error = server.get("error")
        parts.append(f"{server['name']}: {error}" if error else server["name"])
    return "; ".join(parts) or None


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
    bridge: IntegrationBridgeService = Depends(provide_integration_bridge),
    mcp_client=Depends(provide_mcp_client),
):
    """Probe the MCP servers behind an integration.

    Catalog entries have no descriptor, so they are not testable. A stored
    config of ``{}`` is still configured — secret-only rows keep the secret
    outside this dict. The integration's enabled flag does not block the
    probe: enabled MCP servers are contacted, and if none are enabled every
    declared server is probed with a temporary session.
    """
    require_integrations_admin(current_user)

    server_names = list(bridge.server_names_for(integration_id))
    status = bridge.get_integration_status(integration_id)
    if not server_names:
        return {
            "success": False,
            "reason": "not_testable",
            "message": f"Integration '{integration_id}' is not testable.",
            "status": status,
        }

    # Membership, not a non-empty dict. A secret-only row (VirusTotal) is stored
    # as {} after split_secrets and is still configured.
    if not status["configured"]:
        raise HTTPException(status_code=400, detail="Integration not configured")

    if mcp_client is None:
        return {
            "success": False,
            "message": "MCP client is not available.",
            "status": status,
            "server_names": server_names,
            "servers": [],
        }

    mcp_service = getattr(mcp_client, "mcp_service", None)
    if mcp_service is not None:
        try:
            mcp_service.reload_server_configs()
        except Exception as exc:  # noqa: BLE001
            logger.debug("reload_server_configs before test failed: %s", exc)

    enabled = [
        name
        for name in server_names
        if mcp_service is not None and mcp_service.is_server_enabled(name)
    ]
    # None enabled: probe every declared server without turning it on.
    temporary = not enabled
    targets = server_names if temporary else enabled

    servers = []
    for name in targets:
        servers.append(
            await _probe_mcp_server(
                mcp_client,
                name,
                persistent=not temporary,
                skip_enabled_check=temporary,
            )
        )
    success = all(server["success"] for server in servers)
    error_summary = None if success else _probe_error_summary(servers)

    recorded = get_config_service(user_id=current_user.user_id).record_integration_test(
        integration_id,
        success=success,
        error=error_summary,
        tested_at=utcnow(),
    )
    if not recorded:
        logger.warning("Integration '%s' test result was not saved", integration_id)

    if success:
        message = f"Integration '{integration_id}' connected."
    else:
        message = error_summary or f"Integration '{integration_id}' failed to connect."

    return {
        "success": success,
        "message": message,
        "status": status,
        "server_names": server_names,
        "servers": servers,
    }


@router.get("/general")
def get_general_config():
    """
    Get general application settings.

    Returns:
        General configuration
    """
    try:
        # Try database first
        config_service = get_config_service()
        config_value = config_service.get_system_config("general.settings")

        if config_value:
            return config_value

        # Fallback to file-based config
        config_file = vigil_path("general_config.json")

        if config_file.exists():
            with open(config_file, "r") as f:
                config = json.load(f)
                return {
                    "auto_start_sync": config.get("auto_start_sync", False),
                    "show_notifications": config.get("show_notifications", True),
                    "theme": config.get("theme", "dark"),
                    "enable_keyring": config.get("enable_keyring", False),
                }

        # Default values
        return {
            "auto_start_sync": False,
            "show_notifications": True,
            "theme": "dark",
            "enable_keyring": False,
        }
    except Exception as e:
        logger.error(f"Error getting general config: {e}")
        return {
            "auto_start_sync": False,
            "show_notifications": True,
            "theme": "dark",
            "enable_keyring": False,
        }


@router.post("/general")
def set_general_config(
    config: GeneralConfig,
    current_user: User = Depends(get_current_active_user),
):
    """
    Set general application settings.

    Args:
        config: General configuration

    Returns:
        Success status
    """
    config_data = {
        "auto_start_sync": config.auto_start_sync,
        "show_notifications": config.show_notifications,
        "theme": config.theme,
        "enable_keyring": config.enable_keyring,
    }

    # Save to database
    config_service = _for_user(current_user)
    success = config_service.set_system_config(
        key="general.settings",
        value=config_data,
        description="General application settings",
        config_type="general",
        change_reason="Updated via Settings UI",
    )

    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to save configuration to database"
        )

    _mirror_to_file("general_config.json", config_data)

    # Update the global secrets manager if keyring setting changed
    try:
        # Force reinitialize with new setting
        from core import secrets_manager as sm_module

        sm_module._secrets_manager = None  # Reset global instance
        get_secrets_manager(enable_keyring=config.enable_keyring)
        logger.info(f"Secrets manager updated: enable_keyring={config.enable_keyring}")
    except Exception as e:
        logger.warning(f"Could not update secrets manager: {e}")

    return {"success": True, "message": "General settings saved"}


@router.get("/github")
def get_github_config():
    """
    Get GitHub integration configuration status.

    Returns:
        Configuration status (without exposing the token)
    """
    try:
        token = get_secret("GITHUB_TOKEN")
        has_token = bool(token)

        return {
            "configured": has_token,
            "token_preview": f"{token[:12]}..." if has_token else None,
        }
    except Exception as e:
        logger.error(f"Error getting GitHub config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/github")
def set_github_config(config: GitHubConfig):
    """
    Set GitHub integration configuration.

    Args:
        config: GitHub configuration

    Returns:
        Success status
    """
    success = set_secret("GITHUB_TOKEN", config.token)
    if success:
        return {"success": True, "message": "GitHub token saved securely"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save GitHub token")


@router.get("/postgresql")
def get_postgresql_config():
    """
    Get PostgreSQL database backend configuration status.

    Returns:
        Configuration status
    """
    try:
        conn_str = get_secret("POSTGRESQL_CONNECTION_STRING")
        has_config = bool(conn_str)

        # Extract host from connection string for preview (if exists)
        preview = None
        if conn_str and "postgresql://" in conn_str:
            try:
                # Format: postgresql://user:pass@host:port/db
                parts = conn_str.split("@")
                if len(parts) > 1:
                    host_part = parts[1].split("/")[0]
                    preview = f"postgresql://***@{host_part}/***"
            except Exception as e:
                logger.debug(f"Error parsing connection string preview: {e}")
                preview = "postgresql://***:***@***/***"

        return {"configured": has_config, "connection_preview": preview}
    except Exception as e:
        logger.error(f"Error getting PostgreSQL config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/postgresql")
def set_postgresql_config(config: PostgreSQLConfig):
    """
    Set PostgreSQL database backend configuration.

    Args:
        config: PostgreSQL configuration

    Returns:
        Success status
    """
    success = set_secret("POSTGRESQL_CONNECTION_STRING", config.connection_string)
    if success:
        return {
            "success": True,
            "message": "PostgreSQL connection string saved securely",
        }
    else:
        raise HTTPException(
            status_code=500, detail="Failed to save PostgreSQL connection string"
        )


class AIOperationsSettingsConfig(BaseModel):
    """Local Ollama enrichment recovery toggles.

    Persisted in ``system_config`` at key ``ai_operations.settings``.
    Consumed via ``core.platform.runtime_config.get_ai_operations_setting``
    which layers DB → env var → default. Exposed in Settings → AI Config
    so operators can flip values live without restarting the backend.
    """

    local_ollama_recovery_enabled: bool = True
    local_ollama_recovery_retry_limit: int = Field(default=1, ge=0, le=3)
    local_ollama_recovery_restart_gateway: bool = True


AI_OPERATIONS_DEFAULTS = AIOperationsSettingsConfig().model_dump()


@router.get("/ai-operations")
def get_ai_operations_config():
    """Return the local-Ollama recovery toggles (defaults merged with DB overrides).

    Keys the schema no longer declares — leftover cost/perf knobs in an
    existing row — are dropped. They are not migrated and not fatal.
    """
    try:
        config_service = get_config_service()
        value = config_service.get_system_config("ai_operations.settings")
        allowed = AIOperationsSettingsConfig.model_fields
        stored = {
            key: value[key]
            for key in allowed
            if isinstance(value, dict) and key in value
        }
        return {**AI_OPERATIONS_DEFAULTS, **stored}
    except Exception as e:
        logger.error(f"Error getting AI operations config: {e}")
        return AI_OPERATIONS_DEFAULTS


@router.post("/ai-operations")
def set_ai_operations_config(
    config: AIOperationsSettingsConfig,
    current_user: User = Depends(get_current_active_user),
):
    """Persist the AI-operations toggles and invalidate the in-process cache."""
    config_data = config.model_dump()
    config_service = _for_user(current_user)
    success = config_service.set_system_config(
        key="ai_operations.settings",
        value=config_data,
        description="Local Ollama enrichment recovery toggles",
        config_type="ai_operations",
        change_reason="Updated via Settings UI",
    )
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to save AI operations config"
        )
    # Drop the in-process cache so the next read reflects the new values.
    # Note: this only clears THIS process's cache. The daemon / llm-worker
    # processes will pick up the new values on their next cache-TTL miss
    # (default 60s) — acceptable since these are rarely-flipped toggles.
    try:
        from core.platform.runtime_config import clear_cache

        clear_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"runtime_config cache clear skipped: {exc}")
    return {"success": True, "message": "AI operations config updated"}


class OrchestratorSettingsConfig(BaseModel):
    """Orchestrator configuration for autonomous investigations."""

    # Opt-in; also feeds ORCHESTRATOR_DEFAULTS. Matches GET /api/orchestrator/status,
    # which already defaults False.
    enabled: bool = False
    dry_run: bool = False
    max_concurrent_agents: int = 3
    max_iterations_per_agent: int = 50
    max_runtime_per_investigation: int = 3600
    max_cost_per_investigation: float = 5.0
    max_total_hourly_cost: float = 20.0
    loop_interval: int = 60
    stale_threshold: int = 300
    workdir_base: str = "data/investigations"


ORCHESTRATOR_DEFAULTS = OrchestratorSettingsConfig().model_dump()


class InvestigationProfileValues(BaseModel):
    """The five limits a profile sets in one click."""

    max_concurrent_agents: int
    max_iterations_per_agent: int
    max_runtime_per_investigation: int
    max_cost_per_investigation: float
    max_total_hourly_cost: float


class InvestigationProfile(BaseModel):
    """One Settings card. The name is not stored on the saved config."""

    label: str
    recommended: bool = False
    values: InvestigationProfileValues


class InvestigationProfiles(BaseModel):
    """Keys the Auto Investigate section renders. ``aggressive`` is labelled Broad."""

    conservative: InvestigationProfile
    balanced: InvestigationProfile
    aggressive: InvestigationProfile


# Same numbers the Settings cards used to hard-code. Balanced matches
# OrchestratorSettingsConfig's defaults.
INVESTIGATION_PROFILES = InvestigationProfiles.model_validate(
    {
        "conservative": {
            "label": "Conservative",
            "values": {
                "max_concurrent_agents": 2,
                "max_iterations_per_agent": 25,
                "max_runtime_per_investigation": 1800,
                "max_cost_per_investigation": 1.0,
                "max_total_hourly_cost": 5.0,
            },
        },
        "balanced": {
            "label": "Balanced",
            "recommended": True,
            "values": {
                "max_concurrent_agents": 3,
                "max_iterations_per_agent": 50,
                "max_runtime_per_investigation": 3600,
                "max_cost_per_investigation": 5.0,
                "max_total_hourly_cost": 20.0,
            },
        },
        "aggressive": {
            "label": "Broad",
            "values": {
                "max_concurrent_agents": 5,
                "max_iterations_per_agent": 100,
                "max_runtime_per_investigation": 7200,
                "max_cost_per_investigation": 15.0,
                "max_total_hourly_cost": 60.0,
            },
        },
    }
)


class OrchestratorConfigResponse(OrchestratorSettingsConfig):
    """Flat saved settings plus the profiles the Settings cards render.

    ``profiles`` is not part of the stored object. POST takes
    ``OrchestratorSettingsConfig`` and ignores the field.
    """

    profiles: InvestigationProfiles


def _orchestrator_payload(stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = {**ORCHESTRATOR_DEFAULTS, **(stored or {})}
    flat = {k: merged[k] for k in ORCHESTRATOR_DEFAULTS}
    flat["profiles"] = INVESTIGATION_PROFILES.model_dump()
    return flat


class IntentDiffRow(BaseModel):
    """One manifest key beside the value the daemon is running with."""

    key: str
    declared: Any
    effective: Any
    source: str
    label: str


class IntentReportResponse(BaseModel):
    """Declared INTENT.md beside effective daemon config. Read-only."""

    path: str
    readable: bool
    rows: list[IntentDiffRow] = Field(default_factory=list)


@router.get("/intent", response_model=IntentReportResponse)
def get_intent_report() -> IntentReportResponse:
    """Declared intent beside effective config.

    A missing or unreadable manifest is 200 with ``readable`` false and no
    rows, so the Settings card can say so in one line.
    """
    rows = intent_report(include_same=True)
    path = str(intent_file())
    if rows is None:
        return IntentReportResponse(path=path, readable=False, rows=[])
    return IntentReportResponse(
        path=path,
        readable=True,
        rows=[
            IntentDiffRow(
                key=row.key,
                declared=row.declared,
                effective=row.effective,
                source=row.source,
                label=row.label,
            )
            for row in rows
        ],
    )


class AutonomyConfig(BaseModel):
    """The two flags the console chip folds into Assist or Act.

    Not stored. ``force_manual_approval`` is the env flag OR the
    ``approval.force_manual_approval`` row. ``auto_response_enabled`` is
    ``Settings.daemon_auto_response``.
    """

    auto_response_enabled: bool
    force_manual_approval: bool


@router.get("/autonomy", response_model=AutonomyConfig)
def get_autonomy_config() -> AutonomyConfig:
    """Effective response autonomy for the console chip."""
    effective = effective_daemon_config(DaemonConfig.from_env())
    return AutonomyConfig(
        auto_response_enabled=effective.response.auto_response_enabled,
        force_manual_approval=effective.response.force_manual_approval,
    )


@router.get("/orchestrator", response_model=OrchestratorConfigResponse)
def get_orchestrator_config():
    """Get orchestrator configuration.

    ``profiles`` is extra on this body so the Settings cards can render it.
    It is not read back from storage.
    """
    try:
        config_service = get_config_service()
        config_value = config_service.get_system_config("orchestrator.settings")
        return _orchestrator_payload(config_value if config_value else None)
    except Exception as e:
        logger.error(f"Error getting orchestrator config: {e}")
        return _orchestrator_payload(None)


@router.post("/orchestrator")
def set_orchestrator_config(
    config: OrchestratorSettingsConfig,
    current_user: User = Depends(get_current_active_user),
):
    """Set orchestrator configuration. Persists settings AND syncs the
    runtime enabled flag used by GET /api/orchestrator/status (which
    NavigationRail uses to show/hide the Auto Ops tab).

    A ``profiles`` field on the body is ignored. The stored object stays the
    flat keys; no profile name is written.
    """
    config_data = config.model_dump()

    config_service = _for_user(current_user)
    success = config_service.set_system_config(
        key="orchestrator.settings",
        value=config_data,
        description="Autonomous orchestrator settings",
        config_type="orchestrator",
        change_reason="Updated via Settings UI",
    )

    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to save orchestrator config to database"
        )

    # Poke the in-process orchestrator (if the API happens to share one)
    # so a running daemon reacts immediately. No-op in backend-only
    # deployments — the daemon polls orchestrator.settings every few
    # seconds.
    try:
        from services.api.routers.orchestrator import _get_orchestrator

        orch = _get_orchestrator()
        if orch is not None:
            if config_data.get("enabled"):
                orch.enable()
            else:
                orch.disable()
    except Exception as e:
        logger.debug("In-process orchestrator runtime apply skipped: %s", e)

    return {"success": True, "message": "Orchestrator settings saved"}


class ForceManualApprovalConfig(BaseModel):
    """``approval.force_manual_approval``. Assist is true, Act is false."""

    enabled: bool


class ForceManualApprovalResponse(ForceManualApprovalConfig):
    """The stored flag, plus whether daemon env overrides Act."""

    environment_wins: bool


def _environment_wins() -> bool:
    """Force-approval, or auto-response turned off, beats a stored Act."""
    settings = get_settings()
    return bool(settings.daemon_force_approval) or not settings.daemon_auto_response


def _stored_force_manual(config_service) -> bool:
    value = config_service.get_system_config(APPROVAL_CONFIG_KEY)
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return False


@router.get("/force-manual-approval", response_model=ForceManualApprovalResponse)
def get_force_manual_approval():
    """Read ``approval.force_manual_approval`` without inserting a default row.

    A failed read is an error, not Act: reporting the default would show
    approvals as off while the stored flag may be forcing them on.
    """
    try:
        enabled = _stored_force_manual(get_config_service())
    except Exception as e:
        logger.error(f"Error getting force-manual approval: {e}")
        raise HTTPException(
            status_code=503, detail="Could not read the approval setting"
        ) from e
    return ForceManualApprovalResponse(
        enabled=enabled, environment_wins=_environment_wins()
    )


@router.post("/force-manual-approval", response_model=ForceManualApprovalResponse)
def set_force_manual_approval(
    config: ForceManualApprovalConfig,
    current_user: User = Depends(get_current_active_user),
):
    """Persist Assist or Act. Act is refused while the environment wins, so it
    is not stored for later. Assist may still be stored.
    """
    if not config.enabled and _environment_wins():
        raise HTTPException(
            status_code=409,
            detail="The environment wins; Act was not saved.",
        )
    config_service = _for_user(current_user)
    success = config_service.set_system_config(
        key=APPROVAL_CONFIG_KEY,
        value={"enabled": config.enabled},
        description="Force manual approval for all actions",
        config_type="approval",
        change_reason="Updated via Settings UI",
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save approval config")
    return ForceManualApprovalResponse(
        enabled=config.enabled, environment_wins=_environment_wins()
    )


# ---- Darktrace webhook receiver config ----
class DarktraceConfig(BaseModel):
    """Darktrace webhook receiver configuration.

    Non-secret fields live in ``system_config['darktrace.settings']``; the
    HMAC shared secret is stored via the secrets manager under
    ``DARKTRACE_WEBHOOK_SECRET``. Env vars are honoured as fallback so
    existing deployments keep working.
    """

    enabled: bool = False
    url: str = ""
    max_body_kb: int = 1024
    webhook_secret: Optional[str] = None  # write-only; never returned


DARKTRACE_SETTINGS_KEY = "darktrace.settings"
DARKTRACE_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "url": "",
    "max_body_kb": 1024,
}


@router.get("/darktrace")
def get_darktrace_config():
    """Return the current Darktrace webhook receiver config (without the secret)."""
    try:
        config_service = get_config_service()
        value = config_service.get_system_config(DARKTRACE_SETTINGS_KEY) or {}
        merged = {**DARKTRACE_DEFAULTS, **value}
        secret = get_secret("DARKTRACE_WEBHOOK_SECRET") or ""
        return {**merged, "configured": bool(secret)}
    except Exception as e:
        logger.error(f"Error getting Darktrace config: {e}")
        return {**DARKTRACE_DEFAULTS, "configured": False}


@router.post("/darktrace")
def set_darktrace_config(
    config: DarktraceConfig,
    current_user: User = Depends(get_current_active_user),
):
    """Persist Darktrace config. The webhook_secret is stored separately via the
    secrets manager; if omitted, the existing secret is preserved."""
    config_service = _for_user(current_user)
    settings = {
        "enabled": config.enabled,
        "url": config.url,
        "max_body_kb": config.max_body_kb,
    }
    ok = config_service.set_system_config(
        key=DARKTRACE_SETTINGS_KEY,
        value=settings,
        description="Darktrace webhook receiver settings",
        config_type="darktrace",
        change_reason="Updated via Settings UI",
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save Darktrace settings")
    if config.webhook_secret is not None and config.webhook_secret != "":
        if not set_secret("DARKTRACE_WEBHOOK_SECRET", config.webhook_secret):
            raise HTTPException(
                status_code=500, detail="Failed to save Darktrace webhook secret"
            )
    return {"success": True, "message": "Darktrace config saved"}


# ---------------------------------------------------------------------------
# Secrets manager status / reinit / migration
# ---------------------------------------------------------------------------


class _SecretsReinitRequest(BaseModel):
    """Optional override for the reinit endpoint.

    The running process's ``os.environ`` may have a stale
    ``SECRETS_BACKEND`` from launch time. ``write_backend`` here lets an
    admin force the rebuilt singleton onto a specific backend without
    bouncing uvicorn.
    """

    write_backend: Optional[str] = None


class _SecretsMigrateRequest(BaseModel):
    keys: Optional[List[str]] = None
    remove_from_dotenv: bool = True


@router.get("/secrets/status")
def secrets_status() -> Dict[str, Any]:
    """Report which backend the secrets manager is using and why.

    Used by the Settings UI (and `curl` debugging) to answer "why are my
    creds not landing in ~/.vigil/secrets.enc?". Includes the chosen
    write backend, what was expected per ``SECRETS_BACKEND`` env, whether
    cryptography imported, and where each backend lives on disk.
    """
    mgr = get_secrets_manager()
    return mgr.get_backend_status()


@router.post("/secrets/reinit")
def secrets_reinit(
    request: Optional[_SecretsReinitRequest] = None,
) -> Dict[str, Any]:
    """Drop the cached secrets-manager singleton and rebuild it.

    Useful when the long-running process picked the wrong write backend
    on first init (e.g. ``SECRETS_BACKEND`` was stale in os.environ when
    the very first ``set_secret`` call ran during startup). After this
    call the manager re-evaluates backend availability fresh.

    Pass ``{"write_backend": "encrypted"}`` to force a specific backend
    regardless of what's currently in ``os.environ`` — useful when you
    just edited ``.env`` to switch from dotenv to encrypted but haven't
    bounced the process.
    """
    write_backend = request.write_backend if request else None
    mgr = get_secrets_manager(write_backend=write_backend, force_reload=True)
    return {
        "reloaded": True,
        "status": mgr.get_backend_status(),
    }


@router.post("/secrets/migrate-to-encrypted")
def secrets_migrate_to_encrypted(
    request: Optional[_SecretsMigrateRequest] = None,
) -> Dict[str, Any]:
    """Move secrets from the dotenv backend to ``~/.vigil/secrets.enc``.

    Encrypted store is authoritative on conflicts: if a key exists in
    both with different values, the dotenv entry is left in place and
    the conflict is reported so an operator can resolve it manually.

    Body is optional. Pass ``{"keys": ["FOO_BAR"]}`` to migrate only a
    subset, or ``{"remove_from_dotenv": false}`` for a dry-copy that
    leaves the source file alone.
    """

    mgr = get_secrets_manager()
    keys = request.keys if request else None
    remove = request.remove_from_dotenv if request else True
    return mgr.migrate_dotenv_secrets_to_encrypted(keys=keys, remove_from_dotenv=remove)
