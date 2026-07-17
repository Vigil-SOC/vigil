"""
Configuration Service - Database operations for configuration management

Provides high-level operations for managing system configs, user preferences,
and integration configurations with automatic audit logging.
"""

import logging
from typing import Optional, Dict, Any, List, Set
from datetime import datetime
from contextlib import contextmanager

from database.connection import get_db_manager
from database.models import SystemConfig, UserPreference, IntegrationConfig, ConfigAuditLog
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


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
    
    def __init__(self, user_id: str = 'system'):
        """
        Initialize config service.
        
        Args:
            user_id: ID of the user making changes (for audit trail)
        """
        self.user_id = user_id
    
    # =========================================================================
    # System Configuration Methods
    # =========================================================================
    
    def get_system_config(self, key: str, default: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
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
    
    def set_system_config(
        self, 
        key: str, 
        value: Dict[str, Any], 
        description: Optional[str] = None,
        config_type: str = 'general',
        change_reason: Optional[str] = None
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
        try:
            with get_session() as session:
                config = session.query(SystemConfig).filter_by(key=key).first()
                
                old_value = None
                action = 'create'
                
                if config:
                    old_value = config.value
                    config.value = value
                    config.updated_by = self.user_id
                    if description:
                        config.description = description
                    action = 'update'
                else:
                    config = SystemConfig(
                        key=key,
                        value=value,
                        description=description,
                        config_type=config_type,
                        updated_by=self.user_id
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
                    change_reason=change_reason
                )
                
                session.commit()
                logger.info(f"System config '{key}' {action}d by {self.user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error setting system config '{key}': {e}")
            return False
    
    def delete_system_config(self, key: str, change_reason: Optional[str] = None) -> bool:
        """
        Delete a system configuration.
        
        Args:
            key: Configuration key to delete
            change_reason: Reason for deletion (for audit)
            
        Returns:
            True if successful
        """
        try:
            with get_session() as session:
                config = session.query(SystemConfig).filter_by(key=key).first()
                
                if not config:
                    logger.warning(f"System config '{key}' not found")
                    return False
                
                old_value = config.value
                config_type = config.config_type
                
                # Create audit log before deletion
                self._create_audit_log(
                    session,
                    config_type=config_type,
                    config_key=key,
                    action='delete',
                    old_value=old_value,
                    new_value=None,
                    change_reason=change_reason
                )
                
                session.delete(config)
                session.commit()
                logger.info(f"System config '{key}' deleted by {self.user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error deleting system config '{key}': {e}")
            return False
    
    def list_system_configs(self, config_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all system configurations.
        
        Args:
            config_type: Optional filter by config type
            
        Returns:
            List of configuration dictionaries
        """
        try:
            with get_session() as session:
                query = session.query(SystemConfig)
                
                if config_type:
                    query = query.filter_by(config_type=config_type)
                
                configs = query.all()
                return [config.to_dict() for config in configs]
                
        except Exception as e:
            logger.error(f"Error listing system configs: {e}")
            return []
    
    # =========================================================================
    # User Preferences Methods
    # =========================================================================
    
    def get_user_preferences(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get user preferences.
        
        Args:
            user_id: User ID (defaults to current user)
            
        Returns:
            User preferences dictionary
        """
        user_id = user_id or self.user_id
        
        try:
            with get_session() as session:
                user_pref = session.query(UserPreference).filter_by(user_id=user_id).first()
                
                if user_pref:
                    return user_pref.preferences
                
                # Return defaults if no preferences found
                return {
                    'theme': 'dark',
                    'show_notifications': True,
                    'auto_start_sync': False
                }
                
        except Exception as e:
            logger.error(f"Error getting user preferences for '{user_id}': {e}")
            return {}
    
    def set_user_preferences(
        self, 
        preferences: Dict[str, Any], 
        user_id: Optional[str] = None,
        display_name: Optional[str] = None,
        email: Optional[str] = None
    ) -> bool:
        """
        Set user preferences.
        
        Args:
            preferences: Preferences dictionary
            user_id: User ID (defaults to current user)
            display_name: Optional display name
            email: Optional email
            
        Returns:
            True if successful
        """
        user_id = user_id or self.user_id
        
        try:
            with get_session() as session:
                user_pref = session.query(UserPreference).filter_by(user_id=user_id).first()
                
                if user_pref:
                    user_pref.preferences = preferences
                    if display_name:
                        user_pref.display_name = display_name
                    if email:
                        user_pref.email = email
                else:
                    user_pref = UserPreference(
                        user_id=user_id,
                        preferences=preferences,
                        display_name=display_name,
                        email=email
                    )
                    session.add(user_pref)
                
                session.commit()
                logger.info(f"User preferences updated for '{user_id}'")
                return True
                
        except Exception as e:
            logger.error(f"Error setting user preferences for '{user_id}': {e}")
            return False
    
    def update_last_login(self, user_id: Optional[str] = None) -> bool:
        """
        Update last login timestamp for a user.
        
        Args:
            user_id: User ID (defaults to current user)
            
        Returns:
            True if successful
        """
        user_id = user_id or self.user_id
        
        try:
            with get_session() as session:
                user_pref = session.query(UserPreference).filter_by(user_id=user_id).first()
                
                if user_pref:
                    user_pref.last_login = datetime.utcnow()
                    session.commit()
                    return True
                
                return False
                
        except Exception as e:
            logger.error(f"Error updating last login for '{user_id}': {e}")
            return False
    
    # =========================================================================
    # Integration Configuration Methods
    # =========================================================================
    
    def get_integration_config(self, integration_id: str) -> Optional[Dict[str, Any]]:
        """
        Get integration configuration.
        
        Args:
            integration_id: Integration identifier
            
        Returns:
            Integration configuration or None
        """
        try:
            with get_session() as session:
                integration = session.query(IntegrationConfig).filter_by(
                    integration_id=integration_id
                ).first()
                
                if integration:
                    return integration.to_dict()
                
                return None
                
        except Exception as e:
            logger.error(f"Error getting integration config '{integration_id}': {e}")
            return None
    
    def set_integration_config(
        self,
        integration_id: str,
        config: Dict[str, Any],
        enabled: bool = True,
        integration_name: Optional[str] = None,
        integration_type: Optional[str] = None,
        description: Optional[str] = None,
        change_reason: Optional[str] = None
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
        try:
            with get_session() as session:
                integration = session.query(IntegrationConfig).filter_by(
                    integration_id=integration_id
                ).first()
                
                old_value = None
                action = 'create'
                
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
                    action = 'update'
                else:
                    integration = IntegrationConfig(
                        integration_id=integration_id,
                        enabled=enabled,
                        config=config,
                        integration_name=integration_name,
                        integration_type=integration_type,
                        description=description,
                        updated_by=self.user_id
                    )
                    session.add(integration)
                
                # Create audit log
                self._create_audit_log(
                    session,
                    config_type='integration',
                    config_key=integration_id,
                    action=action,
                    old_value=old_value,
                    new_value=config,
                    change_reason=change_reason
                )
                
                session.commit()
                logger.info(f"Integration '{integration_id}' {action}d by {self.user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error setting integration config '{integration_id}': {e}")
            return False
    
    def enable_integration(self, integration_id: str, enabled: bool = True) -> bool:
        """
        Enable or disable an integration.
        
        Args:
            integration_id: Integration identifier
            enabled: Whether to enable (True) or disable (False)
            
        Returns:
            True if successful
        """
        try:
            with get_session() as session:
                integration = session.query(IntegrationConfig).filter_by(
                    integration_id=integration_id
                ).first()
                
                if not integration:
                    logger.warning(f"Integration '{integration_id}' not found")
                    return False
                
                integration.enabled = enabled
                integration.updated_by = self.user_id
                session.commit()
                
                status = "enabled" if enabled else "disabled"
                logger.info(f"Integration '{integration_id}' {status} by {self.user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error enabling/disabling integration '{integration_id}': {e}")
            return False
    
    def update_integration_test_result(
        self,
        integration_id: str,
        success: bool,
        error: Optional[str] = None
    ) -> bool:
        """
        Update integration test result.
        
        Args:
            integration_id: Integration identifier
            success: Whether test was successful
            error: Error message if test failed
            
        Returns:
            True if successful
        """
        try:
            with get_session() as session:
                integration = session.query(IntegrationConfig).filter_by(
                    integration_id=integration_id
                ).first()
                
                if not integration:
                    return False
                
                integration.last_test_at = datetime.utcnow()
                integration.last_test_success = success
                integration.last_error = error
                session.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error updating test result for '{integration_id}': {e}")
            return False
    
    def list_integrations(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """
        List all integrations.
        
        Args:
            enabled_only: If True, only return enabled integrations
            
        Returns:
            List of integration configuration dictionaries
        """
        try:
            with get_session() as session:
                query = session.query(IntegrationConfig)
                
                if enabled_only:
                    query = query.filter_by(enabled=True)
                
                integrations = query.all()
                return [integration.to_dict() for integration in integrations]
                
        except Exception as e:
            logger.error(f"Error listing integrations: {e}")
            return []
    
    def get_enabled_integration_ids(self) -> List[str]:
        """
        Get list of enabled integration IDs.
        
        Returns:
            List of integration IDs
        """
        try:
            with get_session() as session:
                integrations = session.query(IntegrationConfig).filter_by(enabled=True).all()
                return [i.integration_id for i in integrations]
        except Exception as e:
            logger.error(f"Error getting enabled integrations: {e}")
            return []

    def get_disabled_integration_ids(self) -> Set[str]:
        """Registered-but-disabled integration IDs. Sources with no config row
        (webhook, flow) are never disabled. On DB error return empty (fail open)
        so a lookup blip can't silently drop ingestion."""
        try:
            with get_session() as session:
                integrations = session.query(IntegrationConfig).filter_by(enabled=False).all()
                return {i.integration_id for i in integrations}
        except Exception as e:
            logger.error(f"Error getting disabled integrations: {e}")
            return set()
    
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
        change_reason: Optional[str] = None
    ):
        """Create an audit log entry."""
        try:
            audit_entry = ConfigAuditLog(
                config_type=config_type,
                config_key=config_key,
                action=action,
                old_value=old_value,
                new_value=new_value,
                changed_by=self.user_id,
                change_reason=change_reason
            )
            session.add(audit_entry)
        except Exception as e:
            logger.error(f"Error creating audit log: {e}")
    
    def get_audit_log(
        self,
        config_type: Optional[str] = None,
        config_key: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get configuration audit log.
        
        Args:
            config_type: Optional filter by config type
            config_key: Optional filter by config key
            limit: Maximum number of entries to return
            
        Returns:
            List of audit log entries
        """
        try:
            with get_session() as session:
                query = session.query(ConfigAuditLog).order_by(
                    ConfigAuditLog.timestamp.desc()
                )
                
                if config_type:
                    query = query.filter_by(config_type=config_type)
                if config_key:
                    query = query.filter_by(config_key=config_key)
                
                entries = query.limit(limit).all()
                return [entry.to_dict() for entry in entries]
                
        except Exception as e:
            logger.error(f"Error getting audit log: {e}")
            return []


# Global instance for singleton pattern
_config_service: Optional[ConfigService] = None


def get_config_service(user_id: str = 'system') -> ConfigService:
    """
    Get or create the global config service instance.
    
    Args:
        user_id: ID of the user (for audit trail)
        
    Returns:
        ConfigService instance
    """
    global _config_service
    
    if _config_service is None or _config_service.user_id != user_id:
        _config_service = ConfigService(user_id=user_id)
    
    return _config_service

