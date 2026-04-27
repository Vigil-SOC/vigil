"""Configuration API endpoints."""

from typing import Any, Dict, Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import json
import logging
import os

# Import new secrets manager
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from secrets_manager import get_secret, set_secret, delete_secret, get_secrets_manager

# Import database config service
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from database.config_service import get_config_service

router = APIRouter()
logger = logging.getLogger(__name__)


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
    findings_path: str = "findings.json"
    cases_path: str = "cases.json"
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


@router.get("/demo-mode")
async def get_demo_mode():
    """
    Get demo mode configuration.
    
    Returns:
        Demo mode status
    """
    try:
        from core.config import is_demo_mode
        import os
        
        demo_enabled = is_demo_mode()
        env_value = os.getenv('DEMO_MODE', '')
        
        return {
            "enabled": demo_enabled,
            "source": "environment" if env_value else "config",
            "description": "Demo mode uses generated sample data instead of database"
        }
    except Exception as e:
        logger.error(f"Error getting demo mode: {e}")
        return {"enabled": False, "error": str(e)}


@router.post("/demo-mode")
async def set_demo_mode(config: DemoModeConfig):
    """
    Set demo mode configuration.
    
    Note: Setting via API updates the config file. Environment variable takes precedence.
    
    Args:
        config: Demo mode configuration
    
    Returns:
        Success status
    """
    try:
        config_file = Path.home() / '.deeptempo' / 'general_config.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing config
        existing = {}
        if config_file.exists():
            with open(config_file, 'r') as f:
                existing = json.load(f)
        
        # Update demo_mode setting
        existing['demo_mode'] = config.enabled
        
        with open(config_file, 'w') as f:
            json.dump(existing, f, indent=2)
        
        return {
            "success": True, 
            "enabled": config.enabled,
            "message": f"Demo mode {'enabled' if config.enabled else 'disabled'}. Restart the server for changes to take effect.",
            "note": "Set DEMO_MODE=true environment variable for immediate effect without restart"
        }
    except Exception as e:
        logger.error(f"Error setting demo mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/demo-mode/reset")
async def reset_demo_data():
    """
    Reset demo data to regenerate sample findings and cases.
    
    Returns:
        Success status
    """
    try:
        from core.config import is_demo_mode
        
        if not is_demo_mode():
            raise HTTPException(status_code=400, detail="Demo mode is not enabled")
        
        from services.demo_data_service import get_demo_service
        demo_service = get_demo_service()
        demo_service.reset()
        
        return {
            "success": True,
            "message": "Demo data regenerated",
            "findings_count": len(demo_service.get_findings()),
            "cases_count": len(demo_service.get_cases())
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resetting demo data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/claude")
async def get_claude_config():
    """
    Get Claude API configuration status.
    
    Returns:
        Configuration status (without exposing the key)
    """
    try:
        # Try new key names first, then legacy names
        api_key = (get_secret("CLAUDE_API_KEY") or 
                   get_secret("ANTHROPIC_API_KEY") or
                   get_secret("claude_api_key") or
                   get_secret("anthropic_api_key"))
        
        has_key = bool(api_key)
        
        return {
            "configured": has_key,
            "key_preview": f"{api_key[:8]}..." if has_key else None
        }
    except Exception as e:
        logger.error(f"Error getting Claude config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/claude")
async def set_claude_config(config: ClaudeConfig):
    """
    Set Claude API configuration.

    Args:
        config: Claude configuration

    Returns:
        Success status
    """
    try:
        # Use standard environment variable name
        success = set_secret("CLAUDE_API_KEY", config.api_key)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save API key")

        # GH #88: keep the new llm_provider_configs table in sync so the
        # Settings "AI Config" tab and the legacy endpoint agree on the
        # Anthropic default. Best-effort — a DB failure here should NOT
        # block the secret write that already succeeded.
        try:
            from database.connection import get_db_session
            from database.models import LLMProviderConfig

            session = get_db_session()
            try:
                row = session.get(LLMProviderConfig, "anthropic-default")
                if row is None:
                    row = LLMProviderConfig(
                        provider_id="anthropic-default",
                        provider_type="anthropic",
                        name="Anthropic (default)",
                        api_key_ref="CLAUDE_API_KEY",
                        default_model="claude-sonnet-4-5-20250929",
                        is_active=True,
                        is_default=True,
                        config={},
                    )
                    session.add(row)
                else:
                    row.api_key_ref = "CLAUDE_API_KEY"
                    row.is_active = True
                session.commit()
            finally:
                session.close()
        except Exception as sync_err:  # noqa: BLE001
            logger.warning(
                "Legacy /config/claude could not sync llm_provider_configs: %s",
                sync_err,
            )

        return {"success": True, "message": "API key saved securely"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting Claude config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/s3")
async def get_s3_config():
    """
    Get S3 configuration status.
    
    Returns:
        Configuration status
    """
    try:
        # Try database first
        config_service = get_config_service()
        s3_integration = config_service.get_integration_config('s3')
        
        if s3_integration and s3_integration.get('config'):
            config = s3_integration['config']
            return {
                "configured": True,
                "bucket_name": config.get('bucket_name'),
                "region": config.get('region'),
                "findings_path": config.get('findings_path'),
                "cases_path": config.get('cases_path'),
                "parquet_prefix": config.get('parquet_prefix', ''),
                "auth_method": config.get('auth_method', 'credentials'),
                "aws_profile": config.get('aws_profile', ''),
            }
        
        # Fallback to file-based config
        config_file = Path.home() / '.deeptempo' / 's3_config.json'
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {
                    "configured": True,
                    "bucket_name": config.get('bucket_name'),
                    "region": config.get('region'),
                    "findings_path": config.get('findings_path'),
                    "cases_path": config.get('cases_path'),
                    "parquet_prefix": config.get('parquet_prefix', ''),
                    "auth_method": config.get('auth_method', 'credentials'),
                    "aws_profile": config.get('aws_profile', ''),
                }
        
        return {"configured": False}
    except Exception as e:
        logger.error(f"Error getting S3 config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/s3")
async def set_s3_config(config: S3Config):
    """
    Set S3 configuration.
    
    Args:
        config: S3 configuration
    
    Returns:
        Success status
    """
    try:
        bucket_name = config.bucket_name
        parquet_prefix = config.parquet_prefix

        # Parse s3:// URIs: extract bucket name and use the path as prefix
        if bucket_name.startswith('s3://'):
            stripped = bucket_name[5:]
            parts = stripped.split('/', 1)
            bucket_name = parts[0]
            if len(parts) > 1 and parts[1]:
                path = parts[1].rstrip('/')
                # If the path ends with a file extension, trim to the parent directory
                last_segment = path.rsplit('/', 1)[-1] if '/' in path else path
                if '.' in last_segment:
                    path = path.rsplit('/', 1)[0] if '/' in path else ''
                parquet_prefix = (path + '/') if path else ''

        config_data = {
            "bucket_name": bucket_name,
            "region": config.region,
            "findings_path": config.findings_path,
            "cases_path": config.cases_path,
            "parquet_prefix": parquet_prefix,
            "auth_method": config.auth_method,
            "aws_profile": config.aws_profile,
        }
        
        # Save to database
        config_service = get_config_service(user_id='web_ui')
        success = config_service.set_integration_config(
            integration_id='s3',
            config=config_data,
            enabled=True,
            integration_name='AWS S3',
            integration_type='storage',
            description='AWS S3 storage configuration',
            change_reason='Updated via Settings UI'
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save S3 config to database")
        
        # Also save to file for backward compatibility
        config_file = Path.home() / '.deeptempo' / 's3_config.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=2)
        
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting S3 config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/s3/test")
async def test_s3_connection():
    """
    Test S3 connection with current configuration.
    
    Returns:
        Connection test result
    """
    try:
        from services.s3_service import S3Service
        
        # Load S3 config
        config_service = get_config_service()
        s3_integration = config_service.get_integration_config('s3')
        
        if not s3_integration:
            # Fallback to file-based config
            config_file = Path.home() / '.deeptempo' / 's3_config.json'
            if config_file.exists():
                with open(config_file, 'r') as f:
                    s3_integration = json.load(f)
            else:
                raise HTTPException(status_code=400, detail="S3 not configured")
        
        # Unwrap nested config if present
        cfg = s3_integration
        if isinstance(s3_integration.get('config'), dict):
            cfg = s3_integration['config']

        auth_method = cfg.get('auth_method', 'credentials')
        aws_profile = cfg.get('aws_profile', '')

        if auth_method == 'profile' and aws_profile:
            s3_service = S3Service(
                bucket_name=cfg.get('bucket_name'),
                region_name=cfg.get('region', 'us-east-1'),
                aws_profile=aws_profile,
            )
        else:
            access_key_id = get_secret("AWS_ACCESS_KEY_ID")
            secret_access_key = get_secret("AWS_SECRET_ACCESS_KEY")

            if not access_key_id or not secret_access_key:
                raise HTTPException(
                    status_code=400,
                    detail="S3 credentials not found. Please configure S3 in Settings."
                )

            s3_service = S3Service(
                bucket_name=cfg.get('bucket_name'),
                region_name=cfg.get('region', 'us-east-1'),
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )
        
        # Test connection
        success, message = s3_service.test_connection()
        
        if success:
            # Try to list files as an additional test
            findings_path = cfg.get('findings_path', 'findings.json')
            cases_path = cfg.get('cases_path', 'cases.json')
            
            files = s3_service.list_files()
            has_findings = findings_path in files
            has_cases = cases_path in files
            
            return {
                "success": True,
                "message": message,
                "bucket": cfg.get('bucket_name'),
                "region": cfg.get('region', 'us-east-1'),
                "files_found": len(files),
                "findings_file_exists": has_findings,
                "cases_file_exists": has_cases,
                "expected_findings_path": findings_path,
                "expected_cases_path": cases_path
            }
        else:
            return {
                "success": False,
                "message": message,
                "bucket": cfg.get('bucket_name'),
                "region": cfg.get('region', 'us-east-1')
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing S3 connection: {e}")
        raise HTTPException(status_code=500, detail=f"S3 test failed: {str(e)}")




@router.get("/theme")
async def get_theme_config():
    """
    Get theme configuration.
    
    Returns:
        Theme configuration
    """
    try:
        # Try database first
        config_service = get_config_service()
        config_value = config_service.get_system_config('theme.current')
        
        if config_value:
            return config_value
        
        # Fallback to file-based config
        config_file = Path.home() / '.deeptempo' / 'theme_config.json'
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {"theme": config.get('theme', 'dark')}
        
        return {"theme": "dark"}
    except Exception as e:
        logger.error(f"Error getting theme config: {e}")
        return {"theme": "dark"}


@router.post("/theme")
async def set_theme_config(config: ThemeConfig):
    """
    Set theme configuration.
    
    Args:
        config: Theme configuration
    
    Returns:
        Success status
    """
    try:
        config_data = {"theme": config.theme}
        
        # Save to database
        config_service = get_config_service(user_id='web_ui')
        success = config_service.set_system_config(
            key='theme.current',
            value=config_data,
            description='Current UI theme',
            config_type='theme',
            change_reason='Updated via Settings UI'
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save theme to database")
        
        # Also save to file for backward compatibility
        config_file = Path.home() / '.deeptempo' / 'theme_config.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=2)
        
        return {"success": True, "message": "Theme saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting theme config: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@router.get("/integrations")
async def get_integrations_config():
    """
    Get integrations configuration.
    
    Returns:
        Configuration status and enabled integrations
    """
    try:
        # Try database first
        config_service = get_config_service()
        integrations_list = config_service.list_integrations()
        
        if integrations_list:
            # Build response in the expected format
            enabled_integrations = [i['integration_id'] for i in integrations_list if i['enabled']]
            integrations = {i['integration_id']: i['config'] for i in integrations_list}
            
            return {
                "configured": True,
                "enabled_integrations": enabled_integrations,
                "integrations": integrations
            }
        
        # Fallback to file-based config
        config_file = Path.home() / '.deeptempo' / 'integrations_config.json'
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {
                    "configured": True,
                    "enabled_integrations": config.get('enabled_integrations', []),
                    "integrations": config.get('integrations', {})
                }
        
        return {"configured": False, "enabled_integrations": [], "integrations": {}}
    except Exception as e:
        logger.error(f"Error getting integrations config: {e}")
        return {"configured": False, "enabled_integrations": [], "integrations": {}, "error": str(e)}


@router.post("/integrations")
async def set_integrations_config(config: IntegrationsConfig):
    """
    Set integrations configuration.
    
    Args:
        config: Integrations configuration
    
    Returns:
        Success status
    """
    try:
        config_service = get_config_service(user_id='web_ui')
        
        # Save each integration to database
        for integration_id in config.integrations.keys():
            integration_config = config.integrations[integration_id]
            enabled = integration_id in config.enabled_integrations
            
            success = config_service.set_integration_config(
                integration_id=integration_id,
                config=integration_config,
                enabled=enabled,
                change_reason='Updated via Settings UI'
            )
            
            if not success:
                logger.error(f"Failed to save integration '{integration_id}'")
        
        # Also save to file for backward compatibility
        config_file = Path.home() / '.deeptempo' / 'integrations_config.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_data = {
            "enabled_integrations": config.enabled_integrations,
            "integrations": config.integrations
        }
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=2)
        
        return {"success": True, "message": "Integrations configuration saved"}
    except Exception as e:
        logger.error(f"Error setting integrations config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/integrations/status")
async def get_integrations_status():
    """
    Get status of all integrations.
    
    Returns:
        Status information for all integrations
    """
    try:
        # Import here to avoid circular dependencies
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from services.integration_bridge_service import get_integration_bridge
        
        bridge = get_integration_bridge()
        statuses = bridge.get_all_integration_statuses()
        
        return {
            "success": True,
            "statuses": statuses
        }
    except Exception as e:
        logger.error(f"Error getting integration statuses: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/integrations/{integration_id}/test")
async def test_integration(integration_id: str):
    """
    Test an integration connection.
    
    Args:
        integration_id: Integration identifier
    
    Returns:
        Test result with success/failure and message
    """
    try:
        # Import here to avoid circular dependencies
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from services.integration_bridge_service import get_integration_bridge
        
        bridge = get_integration_bridge()
        status = bridge.get_integration_status(integration_id)
        
        if not status['configured']:
            raise HTTPException(status_code=400, detail="Integration not configured")
        
        if not status['server_available']:
            return {
                "success": False,
                "message": f"Integration server not yet implemented. The '{integration_id}' integration is planned but the backend MCP server needs to be created.",
                "status": status,
                "implementation_status": "pending"
            }
        
        if not status['enabled']:
            return {
                "success": False,
                "message": "Integration is configured but not enabled. Please enable it in the integrations list.",
                "status": status
            }
        
        # TODO: Implement actual connection test using MCP client
        # For now, we just verify the configuration is complete
        integration_config = bridge.get_integration_config(integration_id)
        
        # Check if required fields are present (basic validation)
        if not integration_config:
            raise HTTPException(status_code=400, detail="Integration configuration is empty")
        
        # Prepare environment variables to verify they're being set correctly
        env_vars = bridge._config_to_env_vars(integration_id, integration_config)
        
        return {
            "success": True,
            "message": f"Integration '{integration_id}' is configured and ready. Configuration will be passed to the MCP server as environment variables.",
            "status": status,
            "env_var_count": len(env_vars),
            "server_name": status.get('server_name', 'unknown')
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing integration '{integration_id}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/general")
async def get_general_config():
    """
    Get general application settings.
    
    Returns:
        General configuration
    """
    try:
        # Try database first
        config_service = get_config_service()
        config_value = config_service.get_system_config('general.settings')
        
        if config_value:
            return config_value
        
        # Fallback to file-based config
        config_file = Path.home() / '.deeptempo' / 'general_config.json'
        
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {
                    "auto_start_sync": config.get('auto_start_sync', False),
                    "show_notifications": config.get('show_notifications', True),
                    "theme": config.get('theme', 'dark'),
                    "enable_keyring": config.get('enable_keyring', False)
                }
        
        # Default values
        return {
            "auto_start_sync": False, 
            "show_notifications": True, 
            "theme": "dark",
            "enable_keyring": False
        }
    except Exception as e:
        logger.error(f"Error getting general config: {e}")
        return {
            "auto_start_sync": False, 
            "show_notifications": True, 
            "theme": "dark",
            "enable_keyring": False
        }


@router.post("/general")
async def set_general_config(config: GeneralConfig):
    """
    Set general application settings.
    
    Args:
        config: General configuration
    
    Returns:
        Success status
    """
    try:
        config_data = {
            "auto_start_sync": config.auto_start_sync,
            "show_notifications": config.show_notifications,
            "theme": config.theme,
            "enable_keyring": config.enable_keyring
        }
        
        # Save to database
        config_service = get_config_service(user_id='web_ui')
        success = config_service.set_system_config(
            key='general.settings',
            value=config_data,
            description='General application settings',
            config_type='general',
            change_reason='Updated via Settings UI'
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save configuration to database")
        
        # Also save to file for backward compatibility (during transition)
        config_file = Path.home() / '.deeptempo' / 'general_config.json'
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=2)
        
        # Update the global secrets manager if keyring setting changed
        try:
            from secrets_manager import get_secrets_manager
            # Force reinitialize with new setting
            import secrets_manager as sm_module
            sm_module._secrets_manager = None  # Reset global instance
            get_secrets_manager(enable_keyring=config.enable_keyring)
            logger.info(f"Secrets manager updated: enable_keyring={config.enable_keyring}")
        except Exception as e:
            logger.warning(f"Could not update secrets manager: {e}")
        
        return {"success": True, "message": "General settings saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting general config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/github")
async def get_github_config():
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
            "token_preview": f"{token[:12]}..." if has_token else None
        }
    except Exception as e:
        logger.error(f"Error getting GitHub config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/github")
async def set_github_config(config: GitHubConfig):
    """
    Set GitHub integration configuration.
    
    Args:
        config: GitHub configuration
    
    Returns:
        Success status
    """
    try:
        success = set_secret("GITHUB_TOKEN", config.token)
        if success:
            return {"success": True, "message": "GitHub token saved securely"}
        else:
            raise HTTPException(status_code=500, detail="Failed to save GitHub token")
    except Exception as e:
        logger.error(f"Error setting GitHub config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/postgresql")
async def get_postgresql_config():
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
        
        return {
            "configured": has_config,
            "connection_preview": preview
        }
    except Exception as e:
        logger.error(f"Error getting PostgreSQL config: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/postgresql")
async def set_postgresql_config(config: PostgreSQLConfig):
    """
    Set PostgreSQL database backend configuration.
    
    Args:
        config: PostgreSQL configuration
    
    Returns:
        Success status
    """
    try:
        success = set_secret("POSTGRESQL_CONNECTION_STRING", config.connection_string)
        if success:
            return {"success": True, "message": "PostgreSQL connection string saved securely"}
        else:
            raise HTTPException(status_code=500, detail="Failed to save PostgreSQL connection string")
    except Exception as e:
        logger.error(f"Error setting PostgreSQL config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class AIOperationsSettingsConfig(BaseModel):
    """Runtime cost/perf toggles introduced across GH #84 PR-C/PR-D/PR-F.

    Persisted in ``system_config`` at key ``ai_operations.settings``.
    Consumed via ``services.runtime_config.get_ai_operations_setting``
    which layers DB → env var → default. Exposed in the Settings UI
    (AI Config → AI Operations) so operators can flip values live
    without restarting the backend / daemon / llm-worker.
    """
    prompt_cache_enabled: bool = True
    history_window: int = 20
    tool_response_budget_default: int = 8000
    thinking_budget: int = 10000


AI_OPERATIONS_DEFAULTS = AIOperationsSettingsConfig().model_dump()


@router.get("/ai-operations")
async def get_ai_operations_config():
    """Return the current AI-operations toggles (defaults merged with DB overrides)."""
    try:
        config_service = get_config_service()
        value = config_service.get_system_config("ai_operations.settings")
        if value:
            return {**AI_OPERATIONS_DEFAULTS, **value}
        return AI_OPERATIONS_DEFAULTS
    except Exception as e:
        logger.error(f"Error getting AI operations config: {e}")
        return AI_OPERATIONS_DEFAULTS


@router.post("/ai-operations")
async def set_ai_operations_config(config: AIOperationsSettingsConfig):
    """Persist the AI-operations toggles and invalidate the in-process cache."""
    try:
        config_data = config.model_dump()
        config_service = get_config_service(user_id="web_ui")
        success = config_service.set_system_config(
            key="ai_operations.settings",
            value=config_data,
            description="Runtime AI cost/perf toggles (GH #84 PR-F)",
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
            from services.runtime_config import clear_cache
            clear_cache()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"runtime_config cache clear skipped: {exc}")
        return {"success": True, "message": "AI operations config updated"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting AI operations config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class OrchestratorSettingsConfig(BaseModel):
    """Orchestrator configuration for autonomous investigations."""
    enabled: bool = True
    dry_run: bool = False
    auto_assign_findings: bool = True
    auto_assign_severities: List[str] = ["critical", "high"]
    max_concurrent_agents: int = 3
    max_iterations_per_agent: int = 50
    max_runtime_per_investigation: int = 3600
    max_cost_per_investigation: float = 5.0
    max_total_hourly_cost: float = 20.0
    max_total_daily_cost: float = 100.0
    loop_interval: int = 60
    agent_loop_delay: int = 2
    stale_threshold: int = 300
    dedup_window_minutes: int = 30
    context_max_chars: int = 10000
    plan_model: str = "claude-sonnet-4-5-20250929"
    review_model: str = "claude-sonnet-4-5-20250929"
    workdir_base: str = "data/investigations"


ORCHESTRATOR_DEFAULTS = OrchestratorSettingsConfig().model_dump()


@router.get("/orchestrator")
async def get_orchestrator_config():
    """Get orchestrator configuration."""
    try:
        config_service = get_config_service()
        config_value = config_service.get_system_config('orchestrator.settings')

        if config_value:
            merged = {**ORCHESTRATOR_DEFAULTS, **config_value}
            return merged

        return ORCHESTRATOR_DEFAULTS
    except Exception as e:
        logger.error(f"Error getting orchestrator config: {e}")
        return ORCHESTRATOR_DEFAULTS


@router.post("/orchestrator")
async def set_orchestrator_config(config: OrchestratorSettingsConfig):
    """Set orchestrator configuration. Persists settings AND syncs the
    runtime enabled flag used by GET /api/orchestrator/status (which
    NavigationRail uses to show/hide the Auto Ops tab).
    """
    try:
        config_data = config.model_dump()

        config_service = get_config_service(user_id='web_ui')
        success = config_service.set_system_config(
            key='orchestrator.settings',
            value=config_data,
            description='Autonomous orchestrator settings',
            config_type='orchestrator',
            change_reason='Updated via Settings UI'
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to save orchestrator config to database")

        # Poke the in-process orchestrator (if the API happens to share one)
        # so a running daemon reacts immediately. No-op in backend-only
        # deployments — the daemon polls orchestrator.settings every few
        # seconds.
        try:
            from backend.api.orchestrator import _get_orchestrator
            orch = _get_orchestrator()
            if orch is not None:
                if config_data.get("enabled"):
                    orch.enable()
                else:
                    orch.disable()
        except Exception as e:
            logger.debug("In-process orchestrator runtime apply skipped: %s", e)

        return {"success": True, "message": "Orchestrator settings saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting orchestrator config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
async def get_darktrace_config():
    """Return the current Darktrace webhook receiver config (without the secret)."""
    try:
        config_service = get_config_service()
        value = config_service.get_system_config(DARKTRACE_SETTINGS_KEY) or {}
        merged = {**DARKTRACE_DEFAULTS, **value}
        secret = get_secret("DARKTRACE_WEBHOOK_SECRET") or os.environ.get(
            "DARKTRACE_WEBHOOK_SECRET", ""
        )
        return {**merged, "configured": bool(secret)}
    except Exception as e:
        logger.error(f"Error getting Darktrace config: {e}")
        return {**DARKTRACE_DEFAULTS, "configured": False}


@router.post("/darktrace")
async def set_darktrace_config(config: DarktraceConfig):
    """Persist Darktrace config. The webhook_secret is stored separately via the
    secrets manager; if omitted, the existing secret is preserved."""
    try:
        config_service = get_config_service(user_id="web_ui")
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
            raise HTTPException(
                status_code=500, detail="Failed to save Darktrace settings"
            )
        if config.webhook_secret is not None and config.webhook_secret != "":
            if not set_secret("DARKTRACE_WEBHOOK_SECRET", config.webhook_secret):
                raise HTTPException(
                    status_code=500, detail="Failed to save Darktrace webhook secret"
                )
        return {"success": True, "message": "Darktrace config saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting Darktrace config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Mempalace health (#136)
#
# Mempalace is hidden from the MCP servers list because it's a core,
# always-on dependency. This endpoint surfaces enough signal — connection
# state, palace size, last write, entry counts — for operators to confirm
# the memory store is actually healthy from the General tab in Settings.
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _scan_palace(palace_path: Path) -> Dict[str, Any]:
    """Walk the palace tree once and return size + last-modified.

    Best-effort: any unreadable entry is skipped, not raised. Returns
    ``palace_exists=False`` if the root doesn't exist.
    """
    if not palace_path.exists():
        return {
            "palace_exists": False,
            "size_bytes": None,
            "size_human": None,
            "last_modified_iso": None,
        }

    total = 0
    latest_mtime = 0.0
    stack = [palace_path]
    while stack:
        current = stack.pop()
        try:
            for entry in os.scandir(current):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        st = entry.stat(follow_symlinks=False)
                        total += st.st_size
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                except OSError:
                    continue
        except OSError:
            continue

    from datetime import datetime, timezone
    last_iso = (
        datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if latest_mtime > 0
        else None
    )
    return {
        "palace_exists": True,
        "size_bytes": total,
        "size_human": _format_size(total),
        "last_modified_iso": last_iso,
    }


def _count_memories(palace_path: Path) -> Dict[str, Any]:
    """Best-effort ChromaDB collection count.

    Returns a dict with ``count`` (int|None) and ``source`` (one of
    ``chromadb``, ``unavailable``). Never raises — a missing or
    incompatible chromadb install just degrades to ``unavailable``.
    """
    try:
        import chromadb  # type: ignore
    except Exception:
        return {"count": None, "source": "unavailable"}

    try:
        client = chromadb.PersistentClient(path=str(palace_path))
        collections = client.list_collections()
        total = 0
        for coll in collections:
            try:
                total += coll.count()
            except Exception:
                continue
        return {"count": total, "source": "chromadb"}
    except Exception as e:
        logger.debug("ChromaDB count failed for %s: %s", palace_path, e)
        return {"count": None, "source": "unavailable"}


@router.get("/mempalace/health")
async def get_mempalace_health():
    """Health snapshot for the mempalace memory store.

    Aggregates MCP connection state with filesystem facts about the
    palace directory so operators can sanity-check at a glance whether
    memories are actually being persisted. Always returns 200 — failures
    are surfaced via ``connected: false`` and ``error`` fields rather
    than HTTP errors, so the panel can render even when mempalace is
    completely down.
    """
    import asyncio

    from services.mempalace_paths import (
        get_closed_cases_dir,
        get_palace_path,
    )

    # Connection state — reuse the same signal /api/mcp/connections/status uses.
    connected = False
    error: Optional[str] = None
    try:
        from services.mcp_client import get_mcp_client
        mcp_client = get_mcp_client()
        if mcp_client is not None:
            statuses = mcp_client.get_connection_status() or {}
            connected = bool(statuses.get("mempalace", False))
            error = mcp_client.get_last_error("mempalace")
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not read mempalace MCP status: %s", e)
        error = str(e)

    palace_path = get_palace_path(ensure_exists=False)

    fs_stats = await asyncio.to_thread(_scan_palace, palace_path)

    closed_cases_count: Optional[int] = None
    try:
        closed_dir = get_closed_cases_dir(ensure_exists=False)
        if closed_dir.exists():
            closed_cases_count = await asyncio.to_thread(
                lambda: len(list(closed_dir.glob("*.json")))
            )
        else:
            closed_cases_count = 0
    except Exception as e:  # noqa: BLE001
        logger.debug("Closed-cases count failed: %s", e)

    memories = await asyncio.to_thread(_count_memories, palace_path)

    return {
        "connected": connected,
        "error": error,
        "palace_path": str(palace_path),
        **fs_stats,
        "closed_cases_count": closed_cases_count,
        "memories_count": memories["count"],
        "memories_count_source": memories["source"],
    }
