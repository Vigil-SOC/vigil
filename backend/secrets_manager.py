"""
Secrets Manager for Vigil SOC

Provides pluggable secrets storage backends with priority fallback:
1. Environment variables (best for server deployments)
2. .env file (good for local development)
3. Keyring (fallback for desktop development)

Usage:
    from backend.secrets_manager import get_secret, set_secret
    
    # Get a secret (tries all backends in order)
    api_key = get_secret("CLAUDE_API_KEY")
    
    # Set a secret (uses configured backend)
    set_secret("CLAUDE_API_KEY", "sk-ant-...")
"""

import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Service name for keyring storage
SERVICE_NAME = "deeptempo-ai-soc"


class SecretsBackend(ABC):
    """Abstract base class for secrets storage backends."""
    
    @abstractmethod
    def get(self, key: str) -> Optional[str]:
        """Get a secret value."""
        pass
    
    @abstractmethod
    def set(self, key: str, value: str) -> bool:
        """Set a secret value."""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a secret value."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available."""
        pass


class EnvironmentBackend(SecretsBackend):
    """Store secrets in environment variables."""
    
    def get(self, key: str) -> Optional[str]:
        """Get secret from environment variable."""
        value = os.environ.get(key)
        if value:
            logger.debug(f"Found secret '{key}' in environment variables")
        return value
    
    def set(self, key: str, value: str) -> bool:
        """Set environment variable (only for current process)."""
        try:
            os.environ[key] = value
            logger.info(f"Set secret '{key}' in environment (process only)")
            return True
        except Exception as e:
            logger.error(f"Error setting environment variable '{key}': {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """Delete environment variable."""
        try:
            if key in os.environ:
                del os.environ[key]
                logger.info(f"Deleted secret '{key}' from environment")
            return True
        except Exception as e:
            logger.error(f"Error deleting environment variable '{key}': {e}")
            return False
    
    def is_available(self) -> bool:
        """Environment variables are always available."""
        return True


class DotEnvBackend(SecretsBackend):
    """Store secrets in a .env file."""
    
    def __init__(self, env_file: Optional[Path] = None):
        """Initialize with path to .env file."""
        self.env_file = env_file or Path.home() / ".deeptempo" / ".env"
        self._cache: Dict[str, str] = {}
        self._load_env_file()
    
    def _load_env_file(self):
        """Load .env file into cache."""
        if self.env_file.exists():
            try:
                with open(self.env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            # Remove quotes if present
                            value = value.strip('"').strip("'")
                            self._cache[key.strip()] = value
                logger.debug(f"Loaded {len(self._cache)} secrets from {self.env_file}")
            except Exception as e:
                logger.error(f"Error loading .env file: {e}")
    
    def get(self, key: str) -> Optional[str]:
        """Get secret from .env file."""
        value = self._cache.get(key)
        if value:
            logger.debug(f"Found secret '{key}' in .env file")
        return value
    
    def set(self, key: str, value: str) -> bool:
        """Set secret in .env file."""
        try:
            # Update cache
            self._cache[key] = value
            
            # Create directory if needed
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Write all secrets to file
            with open(self.env_file, 'w') as f:
                f.write("# Vigil SOC Secrets\n")
                f.write("# This file contains sensitive credentials - keep it secure!\n\n")
                for k, v in self._cache.items():
                    # Escape quotes in value
                    escaped_value = v.replace('"', '\\"')
                    f.write(f'{k}="{escaped_value}"\n')
            
            # Set restrictive permissions (owner read/write only)
            os.chmod(self.env_file, 0o600)
            
            logger.info(f"Set secret '{key}' in .env file")
            return True
        except Exception as e:
            logger.error(f"Error setting secret in .env file: {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """Delete secret from .env file."""
        try:
            if key in self._cache:
                del self._cache[key]
                
                # Rewrite file without this secret
                with open(self.env_file, 'w') as f:
                    f.write("# Vigil SOC Secrets\n\n")
                    for k, v in self._cache.items():
                        escaped_value = v.replace('"', '\\"')
                        f.write(f'{k}="{escaped_value}"\n')
                
                logger.info(f"Deleted secret '{key}' from .env file")
            return True
        except Exception as e:
            logger.error(f"Error deleting secret from .env file: {e}")
            return False
    
    def is_available(self) -> bool:
        """Check if .env file backend is available."""
        return True


class KeyringBackend(SecretsBackend):
    """Store secrets in system keyring (macOS Keychain, Windows Credential Manager, etc)."""
    
    def __init__(self, lazy_init: bool = True):
        """
        Initialize keyring backend.
        
        Args:
            lazy_init: If True, don't check availability until first use (prevents keychain prompts).
                      If False, check availability immediately.
        """
        self._lazy_init = lazy_init
        self._available = None if lazy_init else self._check_available()
        self._keyring_module = None
    
    def _check_available(self) -> bool:
        """Check if keyring is available."""
        if self._available is not None:
            return self._available
            
        try:
            import keyring
            self._keyring_module = keyring
            # Don't actually test keyring access - that triggers macOS prompts
            # Just check if the module imported successfully
            self._available = True
            logger.debug("Keyring module available")
            return True
        except ImportError as e:
            logger.debug(f"Keyring module not installed: {e}")
            self._available = False
            return False
        except Exception as e:
            logger.debug(f"Keyring not available: {e}")
            self._available = False
            return False
    
    def get(self, key: str) -> Optional[str]:
        """Get secret from keyring."""
        # Lazy initialization - check availability on first use
        if self._available is None:
            self._check_available()
        
        if not self._available:
            return None
        
        try:
            if self._keyring_module is None:
                import keyring
                self._keyring_module = keyring
            
            value = self._keyring_module.get_password(SERVICE_NAME, key)
            if value:
                logger.debug(f"Found secret '{key}' in keyring")
            return value
        except Exception as e:
            logger.debug(f"Error getting secret from keyring: {e}")
            return None
    
    def set(self, key: str, value: str) -> bool:
        """Set secret in keyring."""
        # Lazy initialization - check availability on first use
        if self._available is None:
            self._check_available()
        
        if not self._available:
            logger.warning("Keyring not available, cannot store secret")
            return False
        
        try:
            if self._keyring_module is None:
                import keyring
                self._keyring_module = keyring
            
            self._keyring_module.set_password(SERVICE_NAME, key, value)
            logger.info(f"Set secret '{key}' in keyring")
            return True
        except Exception as e:
            logger.error(f"Error setting secret in keyring: {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """Delete secret from keyring."""
        # Lazy initialization - check availability on first use
        if self._available is None:
            self._check_available()
        
        if not self._available:
            return True
        
        try:
            if self._keyring_module is None:
                import keyring
                self._keyring_module = keyring
            
            self._keyring_module.delete_password(SERVICE_NAME, key)
            logger.info(f"Deleted secret '{key}' from keyring")
            return True
        except Exception as e:
            logger.debug(f"Error deleting secret from keyring: {e}")
            return False
    
    def is_available(self) -> bool:
        """Check if keyring is available."""
        # For lazy init, return False until explicitly checked
        if self._available is None:
            return False
        return self._available


class SecretsManager:
    """
    Unified secrets manager that tries multiple backends in priority order.
    
    Priority for reading:
    1. Environment variables (process-level, ideal for containers/servers)
    2. .env file (file-based, good for local dev)
    3. Keyring (OS-level, only if explicitly enabled)
    
    Priority for writing (configurable):
    - By default writes to .env file for server deployments
    - Can be configured to write to keyring for desktop deployments
    """
    
    def __init__(self, write_backend: str = "dotenv", enable_keyring: bool = False):
        """
        Initialize secrets manager.
        
        Args:
            write_backend: Backend to use for writing ("env", "dotenv", or "keyring")
            enable_keyring: If True, include keyring in read backends. If False, only use
                          keyring if it's the write_backend. This prevents keychain prompts
                          on macOS unless explicitly needed.
        """
        self.env_backend = EnvironmentBackend()
        self.dotenv_backend = DotEnvBackend()
        # Use lazy init to avoid triggering keychain prompts on startup
        self.keyring_backend = KeyringBackend(lazy_init=True)
        self.enable_keyring = enable_keyring or (write_backend == "keyring")
        
        # Read priority - only include keyring if explicitly enabled
        if self.enable_keyring:
            self.read_backends = [
                self.env_backend,
                self.dotenv_backend,
                self.keyring_backend,
            ]
        else:
            self.read_backends = [
                self.env_backend,
                self.dotenv_backend,
            ]
            logger.debug("Keyring backend disabled - will not check keyring for secrets")
        
        # Write backend (configurable based on deployment)
        self.write_backend_name = write_backend
        backend_map = {
            "env": self.env_backend,
            "dotenv": self.dotenv_backend,
            "keyring": self.keyring_backend,
        }
        self.write_backend = backend_map.get(write_backend, self.dotenv_backend)
        
        logger.info(f"Secrets manager initialized (write backend: {write_backend})")
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Get a secret, trying all backends in priority order.
        
        Args:
            key: Secret key to retrieve
            default: Default value if not found
            
        Returns:
            Secret value or default
        """
        for backend in self.read_backends:
            if backend.is_available():
                value = backend.get(key)
                if value:
                    return value
        
        logger.debug(f"Secret '{key}' not found in any backend")
        return default
    
    def set(self, key: str, value: str) -> bool:
        """
        Set a secret using the configured write backend.
        
        Also updates os.environ so the in-process environment stays in sync
        (prevents stale values from the EnvironmentBackend shadowing new ones).
        
        Args:
            key: Secret key
            value: Secret value
            
        Returns:
            True if successful
        """
        if not self.write_backend.is_available():
            logger.error(f"Write backend '{self.write_backend_name}' not available")
            return False
        
        result = self.write_backend.set(key, value)
        if result:
            if value:
                os.environ[key] = value
            elif key in os.environ:
                del os.environ[key]
        return result
    
    def delete(self, key: str) -> bool:
        """
        Delete a secret from all backends.
        
        Args:
            key: Secret key to delete
            
        Returns:
            True if successful
        """
        success = True
        for backend in [self.env_backend, self.dotenv_backend, self.keyring_backend]:
            if backend.is_available():
                if not backend.delete(key):
                    success = False
        return success
    
    def get_backend_status(self) -> Dict[str, Any]:
        """Get status of all backends."""
        return {
            "environment": {
                "available": self.env_backend.is_available(),
                "description": "Environment variables (best for containers/servers)"
            },
            "dotenv": {
                "available": self.dotenv_backend.is_available(),
                "path": str(self.dotenv_backend.env_file),
                "description": "File-based secrets (good for local development)"
            },
            "keyring": {
                "available": self.keyring_backend.is_available(),
                "description": "OS keyring (macOS/Windows/Linux credential managers)"
            },
            "write_backend": self.write_backend_name
        }


# Global secrets manager instance
_secrets_manager: Optional[SecretsManager] = None


def get_secrets_manager(write_backend: Optional[str] = None, enable_keyring: Optional[bool] = None) -> SecretsManager:
    """
    Get or create the global secrets manager instance.
    
    Args:
        write_backend: Backend to use for writing secrets
                      Can be set via SECRETS_BACKEND env var
        enable_keyring: Whether to enable keyring for reading secrets
                       Can be set via ENABLE_KEYRING env var or general config
                       Default: False (prevents macOS keychain prompts)
    """
    global _secrets_manager
    
    if _secrets_manager is None:
        # Check environment variable for backend preference
        if write_backend is None:
            write_backend = os.environ.get("SECRETS_BACKEND", "dotenv")
        
        # Check if keyring should be enabled (priority order: arg > env var > config file)
        if enable_keyring is None:
            # Check environment variable first
            enable_keyring_env = os.environ.get("ENABLE_KEYRING", "").lower()
            if enable_keyring_env in ("true", "1", "yes", "on"):
                enable_keyring = True
            elif enable_keyring_env in ("false", "0", "no", "off"):
                enable_keyring = False
            else:
                # Check general config file
                try:
                    from pathlib import Path
                    import json
                    config_file = Path.home() / '.deeptempo' / 'general_config.json'
                    if config_file.exists():
                        with open(config_file, 'r') as f:
                            config = json.load(f)
                            enable_keyring = config.get('enable_keyring', False)
                    else:
                        enable_keyring = False
                except Exception as e:
                    logger.debug(f"Could not read general config for keyring setting: {e}")
                    enable_keyring = False
        
        _secrets_manager = SecretsManager(
            write_backend=write_backend,
            enable_keyring=enable_keyring
        )
        
        logger.info(f"Secrets manager initialized: backend={write_backend}, keyring={enable_keyring}")
    
    return _secrets_manager


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """Convenience function to get a secret."""
    return get_secrets_manager().get(key, default)


def set_secret(key: str, value: str) -> bool:
    """Convenience function to set a secret."""
    return get_secrets_manager().set(key, value)


def delete_secret(key: str) -> bool:
    """Convenience function to delete a secret."""
    return get_secrets_manager().delete(key)

