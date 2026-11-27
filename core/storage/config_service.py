"""
Configuration Service - Database operations for configuration management

Provides high-level operations for managing system configs, user preferences,
and integration configurations with automatic audit logging.
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from core.audit import trail
from core.exceptions import default_on_error
from core.storage.connection import get_db_manager
from core.storage.models import ConfigAuditLog, IntegrationConfig, SystemConfig
from core.storage.schemas import IntegrationConfigSchema
from core.telemetry_sanitizer import _SECRET_VALUE_PATTERNS, SensitiveAttributeScrubber

logger = logging.getLogger(__name__)

# Payload ``action`` is the event kind (see audit.checkpoint). The
# create/update/delete word stays in ``verb``.
CONFIG_CHANGE = "config.change"
_REDACTED = "[REDACTED]"
_scrubber = SensitiveAttributeScrubber()
# The long-hex value pattern: on its own it blanks only the hex runs (_redact_tree).
_LONG_HEX = next(p for p in _SECRET_VALUE_PATTERNS if "{32,}" in p.pattern)
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
        *,
        gated: bool = True,
        session: Optional[Session] = None,
    ) -> bool:
        """
        Set a system configuration value.

        ``gated=False`` is for a change that only stops autonomy: it must land
        during a restore's write pause too. With ``session``, the write and its
        audit row join that transaction and the caller commits.

        Args:
            key: Configuration key
            value: Configuration value (will be stored as JSONB)
            description: Optional description of this config
            config_type: Type/category of config (general, approval, theme, etc.)
            change_reason: Reason for the change (for audit)

        Returns:
            True if successful
        """
        if session is not None:
            self._put_system_config(
                session, key, value, description, config_type, change_reason, gated
            )
            session.flush()
            return True
        with get_session() as own:
            action = self._put_system_config(
                own, key, value, description, config_type, change_reason, gated
            )
            own.commit()
            logger.info(f"System config '{key}' {action}d by {self.user_id}")
            return True

    def _put_system_config(
        self,
        session: Session,
        key: str,
        value: Dict[str, Any],
        description: Optional[str],
        config_type: str,
        change_reason: Optional[str],
        gated: bool,
    ) -> str:
        """Write the row and its audit entry in ``session``; returns the action."""
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
            gated=gated,
        )
        return action

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
        credential_changed: bool = False,
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
            credential_changed: A secret held outside ``config`` was replaced

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
            was_enabled = bool(integration and integration.enabled)
            meta_changed = False
            old_config = None

            if integration:
                # The on/off switch is recorded beside the settings it gates.
                old_value = {**(integration.config or {}), "enabled": was_enabled}
                meta_changed = any(
                    new is not None and new != getattr(integration, attr)
                    for attr, new in (
                        ("integration_name", integration_name),
                        ("integration_type", integration_type),
                        ("description", description),
                    )
                )
                old_config = integration.config
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

            # Switched on, or a changed endpoint or credential: its health starts
            # again from "no attempt yet". A save that changes nothing keeps it.
            if (
                (enabled and not was_enabled)
                or (action == "update" and old_config != config)
                or credential_changed
            ):
                from core.storage import source_health

                try:
                    # A savepoint: a failed reset never fails the save.
                    with session.begin_nested():
                        source_health.reset(integration_id, session=session)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Health reset for %s failed: %s",
                        integration_id,
                        type(e).__name__,
                    )

            new_value = {**config, "enabled": bool(enabled)}
            if credential_changed:
                # Secrets live outside ``config``; say one was replaced, never its value.
                new_value["secret_replaced"] = True
            # A save that changes nothing about this integration records nothing,
            # so the entry that switched it off or re-keyed it stands out.
            if action == "create" or old_value != new_value or meta_changed:
                self._create_audit_log(
                    session,
                    config_type="integration",
                    config_key=integration_id,
                    action=action,
                    old_value=old_value,
                    new_value=new_value,
                    change_reason=change_reason,
                )

            session.commit()
            logger.info(f"Integration '{integration_id}' {action}d by {self.user_id}")
            return True

    def record_integration_test(
        self,
        integration_id: str,
        *,
        success: bool,
        error: Optional[str],
        tested_at: datetime,
    ) -> bool:
        """Stamp a test send on the integration's Integration Health record.

        ``integration_configs.last_test_*`` is no longer written. Best-effort:
        a failed health write is logged by the health store.
        """
        from core.storage import source_health

        source_health.record_test(
            integration_id, success=success, error=error, when=tested_at
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
        *,
        gated: bool = True,
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
            gated=gated,
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


_trail_ready = trail.in_use


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
        if isinstance(value, str) and not _scrubber._should_redact(key, ""):
            # A long hex run alone (a commit SHA in a git error, or an old 40-hex
            # GitHub token) blanks just the run, keeping the message around it.
            hits = [p for p in _SECRET_VALUE_PATTERNS if p.search(value)]
            if hits == [_LONG_HEX]:
                return _LONG_HEX.sub(_REDACTED, value)
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
    gated: bool = True,
) -> None:
    """Append a trail entry, or insert ``config_audit_log`` when the trail is absent.

    One or the other, never both. The trail path does not catch ``append``:
    a failure rolls the caller's transaction back. Secret copies are redacted
    before they reach a trail column. ``gated=False`` is a safety write that
    still lands during a restore's write pause.
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
            gated=gated,
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


@contextmanager
def audited_change(
    changed_by: str,
    *,
    config_type: str,
    config_key: str,
    action: str,
    old_value: Optional[Dict[str, Any]],
    new_value: Optional[Dict[str, Any]],
):
    """Record a change whose state lives outside the database (a State
    Directory file, the skills folder), then let the caller make it.

    The entry is written first, so a refused one stops the change; an
    exception in the block rolls the entry back.
    """
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        write_config_audit(
            session,
            config_type=config_type,
            config_key=config_key,
            action=action,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
        )
        session.flush()
        yield session


def get_config_service(user_id: str = "system") -> ConfigService:
    """A config service whose writes are audited as ``user_id``.

    One instance per call. The service holds no other state, and a shared
    instance would record a later caller under an earlier caller's id.
    """
    return ConfigService(user_id=user_id)
