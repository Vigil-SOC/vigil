"""
Configuration Service - Database operations for configuration management

Provides high-level operations for managing system configs, user preferences,
and integration configurations with automatic audit logging.
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit import trail
from core.exceptions import default_on_error
from core.storage.connection import get_db_manager
from core.storage.models import ConfigAuditLog, IntegrationConfig, SystemConfig
from core.storage.schemas import IntegrationConfigSchema
from core.telemetry_sanitizer import SensitiveAttributeScrubber

logger = logging.getLogger(__name__)

# Payload ``action`` is the event kind (see audit.checkpoint). The
# create/update/delete word stays in ``verb``.
CONFIG_CHANGE = "config.change"
_REDACTED = "[REDACTED]"
_scrubber = SensitiveAttributeScrubber()
# Set by core.integrations.integration_secrets. Storage cannot import that
# module; an integration audit with no redactor fails the write.
_integration_redactor = None


@contextmanager
def get_session():
    """
    Context manager for database sessions.

    Yields:
        SQLAlchemy session
    """
    db_manager = get_db_manager()

    # Initialize database if not already done
    if db_manager._engine is None:
        db_manager.initialize()

    # Use session_scope for automatic commit/rollback
    with db_manager.session_scope() as session:
        yield session


class ConfigService:
    """Service for managing configurations in the database."""

    def __init__(self, user_id: str = "system"):
        """
        Initialize config service.

        Args:
            user_id: ID of the user making changes (for audit trail)
        """
        self.user_id = user_id

    # =========================================================================
    # System Configuration Methods
    # =========================================================================

    def get_system_config(
        self, key: str, default: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get a system configuration value.

        Args:
            key: Configuration key (e.g., 'general.settings', 'approval.force_manual_approval')
            default: Default value if not found

        Returns:
            Configuration value or default
        """
        try:
            with get_session() as session:
                config = session.query(SystemConfig).filter_by(key=key).first()
                if config:
                    return config.value
                return default
        except Exception as e:
            logger.error(f"Error getting system config '{key}': {e}")
            return default

    def read_system_config(self, key: str) -> Optional[Dict[str, Any]]:
        """Get a system configuration value; None when absent, raises on a failed read.

        For settings that gate safety controls, where "could not read" must not
        look like "not set".
        """
        with get_session() as session:
            config = session.query(SystemConfig).filter_by(key=key).first()
            return config.value if config else None

    @default_on_error(False)
    def set_system_config(
        self,
        key: str,
        value: Dict[str, Any],
        description: Optional[str] = None,
        config_type: str = "general",
        change_reason: Optional[str] = None,
    ) -> bool:
        """
        Set a system configuration value.

        Args:
            key: Configuration key
            value: Configuration value (will be stored as JSONB)
            description: Optional description of this config
            config_type: Type/category of config (general, approval, theme, etc.)
            change_reason: Reason for the change (for audit)

        Returns:
            True if successful
        """
        with get_session() as session:
            config = session.query(SystemConfig).filter_by(key=key).first()

            old_value = None
            action = "create"

            if config:
                old_value = config.value
                config.value = value
                config.updated_by = self.user_id
                if description:
                    config.description = description
                action = "update"
            else:
                config = SystemConfig(
                    key=key,
                    value=value,
                    description=description,
                    config_type=config_type,
                    updated_by=self.user_id,
                )
                session.add(config)

            # Create audit log
            self._create_audit_log(
                session,
                config_type=config_type,
                config_key=key,
                action=action,
                old_value=old_value,
                new_value=value,
                change_reason=change_reason,
            )

            session.commit()
            logger.info(f"System config '{key}' {action}d by {self.user_id}")
            return True

    # =========================================================================
    # User Preferences Methods
    # =========================================================================

    # =========================================================================
    # Integration Configuration Methods
    # =========================================================================

    @default_on_error(None)
    def get_integration_config(self, integration_id: str) -> Optional[Dict[str, Any]]:
        """
        Get integration configuration.

        Args:
            integration_id: Integration identifier

        Returns:
            Integration configuration or None
        """
        with get_session() as session:
            integration = (
                session.query(IntegrationConfig)
                .filter_by(integration_id=integration_id)
                .first()
            )

            if integration:
                return IntegrationConfigSchema.dump(integration)

            return None

    @default_on_error(False)
    def set_integration_config(
        self,
        integration_id: str,
        config: Dict[str, Any],
        enabled: bool = True,
        integration_name: Optional[str] = None,
        integration_type: Optional[str] = None,
        description: Optional[str] = None,
        change_reason: Optional[str] = None,
    ) -> bool:
        """
        Set integration configuration.

        Args:
            integration_id: Integration identifier
            config: Configuration dictionary (non-sensitive data only)
            enabled: Whether integration is enabled
            integration_name: Human-readable name
            integration_type: Type/category of integration
            description: Optional description
            change_reason: Reason for change (for audit)

        Returns:
            True if successful
        """
        with get_session() as session:
            integration = (
                session.query(IntegrationConfig)
                .filter_by(integration_id=integration_id)
                .first()
            )

            old_value = None
            action = "create"

            if integration:
                old_value = integration.config
                integration.config = config
                integration.enabled = enabled
                integration.updated_by = self.user_id
                if integration_name:
                    integration.integration_name = integration_name
                if integration_type:
                    integration.integration_type = integration_type
                if description:
                    integration.description = description
                action = "update"
            else:
                integration = IntegrationConfig(
                    integration_id=integration_id,
                    enabled=enabled,
                    config=config,
                    integration_name=integration_name,
                    integration_type=integration_type,
                    description=description,
                    updated_by=self.user_id,
                )
                session.add(integration)

            # Create audit log
            self._create_audit_log(
                session,
                config_type="integration",
                config_key=integration_id,
                action=action,
                old_value=old_value,
                new_value=config,
                change_reason=change_reason,
            )

            session.commit()
            logger.info(f"Integration '{integration_id}' {action}d by {self.user_id}")
            return True

    @default_on_error(False)
    def record_integration_test(
        self,
        integration_id: str,
        *,
        success: bool,
        error: Optional[str],
        tested_at: datetime,
    ) -> bool:
        """Stamp the last probe on an existing row.

        Does not rewrite ``config`` or ``enabled``. A missing row is left
        missing — creating one would invent a configuration.
        """
        with get_session() as session:
            integration = (
                session.query(IntegrationConfig)
                .filter_by(integration_id=integration_id)
                .first()
            )
            if integration is None:
                return False
            integration.last_test_at = tested_at
            integration.last_test_success = success
            integration.last_error = error
            integration.updated_by = self.user_id
            logger.info(
                "Integration '%s' test recorded success=%s by %s",
                integration_id,
                success,
                self.user_id,
            )
            return True

    @default_on_error(list)
    def list_integrations(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """
        List all integrations.

        Args:
            enabled_only: If True, only return enabled integrations

        Returns:
            List of integration configuration dictionaries
        """
        with get_session() as session:
            query = session.query(IntegrationConfig)

            if enabled_only:
                query = query.filter_by(enabled=True)

            integrations = query.all()
            return IntegrationConfigSchema.dump_many(integrations)

    @default_on_error(set)
    def get_disabled_integration_ids(self) -> Set[str]:
        """Registered-but-disabled integration IDs. Sources with no config row
        (webhook, flow) are never disabled. On DB error return empty (fail open)
        so a lookup blip can't silently drop ingestion."""
        with get_session() as session:
            integrations = (
                session.query(IntegrationConfig).filter_by(enabled=False).all()
            )
            return {i.integration_id for i in integrations}

    # =========================================================================
    # Audit Methods
    # =========================================================================

    def _create_audit_log(
        self,
        session: Session,
        config_type: str,
        config_key: str,
        action: str,
        old_value: Optional[Dict[str, Any]],
        new_value: Optional[Dict[str, Any]],
        change_reason: Optional[str] = None,
    ):
        """Record one config change on the caller's session.

        Deliberately unguarded: the entry joins the caller's transaction, so a
        failure here rolls the config change back with it. Swallowing it would
        let a config change commit with no audit record.
        """
        write_config_audit(
            session,
            config_type=config_type,
            config_key=config_key,
            action=action,
            old_value=old_value,
            new_value=new_value,
            changed_by=self.user_id,
            change_reason=change_reason,
        )

    def record_audit(
        self,
        config_type: str,
        config_key: str,
        action: str,
        old_value: Optional[Dict[str, Any]],
        new_value: Optional[Dict[str, Any]],
        change_reason: Optional[str] = None,
    ) -> None:
        """Write a standalone audit entry (no SystemConfig row touched)."""
        with get_session() as session:
            self._create_audit_log(
                session,
                config_type,
                config_key,
                action,
                old_value,
                new_value,
                change_reason,
            )


def _trail_ready(session: Session) -> bool:
    """Whether ``session`` is Postgres and its revision includes the trail.

    Anything else keeps today's ``config_audit_log`` insert. ``trail_ready``
    itself is the Postgres check; callers do not add a setting.
    """
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return False
    bind = get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return False
    if not trail.trail_ready(session):
        return False
    # stamp_schema_head records the head revision on a create_all database,
    # which has no audit_trail. The revision check would append into a
    # missing table and roll the caller's change back.
    return (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is not None
    )


def _redact_tree(value: Any, key: str = "") -> Any:
    """Copy ``value``, replacing sensitive keys and secret-shaped strings."""
    if isinstance(value, dict):
        if key and _scrubber._should_redact(key, ""):
            return _REDACTED
        return {str(k): _redact_tree(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        if key and _scrubber._should_redact(key, ""):
            return _REDACTED
        return [_redact_tree(item, key) for item in value]
    if key and _scrubber._should_redact(key, value):
        return _REDACTED
    return value


def register_integration_secret_redactor(redactor) -> None:
    """Install ``redact_secrets`` for integration values on the trail."""
    global _integration_redactor
    _integration_redactor = redactor


def redacted_config_copy(
    config_type: str, config_key: str, value: Optional[Dict[str, Any]]
) -> Any:
    """A copy safe to hash. The caller's object, and the saved row, stay intact."""
    if value is None:
        return None
    if config_type == "integration" and isinstance(value, dict):
        if _integration_redactor is None:
            raise RuntimeError("integration secret redactor is not registered")
        return _integration_redactor(config_key, value)
    return _redact_tree(value)


def write_config_audit(
    session: Session,
    *,
    config_type: str,
    config_key: str,
    action: str,
    old_value: Optional[Dict[str, Any]],
    new_value: Optional[Dict[str, Any]],
    changed_by: str,
    change_reason: Optional[str] = None,
) -> None:
    """Append a trail entry, or insert ``config_audit_log`` when the trail is absent.

    One or the other, never both. The trail path does not catch ``append``:
    a failure rolls the caller's transaction back. Secret copies are redacted
    before they reach a trail column.
    """
    if _trail_ready(session):
        trail.append(
            session,
            {
                "action": CONFIG_CHANGE,
                "actor": changed_by,
                "verb": action,
                "config_type": config_type,
                "config_key": config_key,
                "change_reason": change_reason,
                "old_value": redacted_config_copy(config_type, config_key, old_value),
                "new_value": redacted_config_copy(config_type, config_key, new_value),
            },
        )
        return
    session.add(
        ConfigAuditLog(
            config_type=config_type,
            config_key=config_key,
            action=action,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
            change_reason=change_reason,
        )
    )


def get_config_service(user_id: str = "system") -> ConfigService:
    """A config service whose writes are audited as ``user_id``.

    One instance per call. The service holds no other state, and a shared
    instance would record a later caller under an earlier caller's id.
    """
    return ConfigService(user_id=user_id)
