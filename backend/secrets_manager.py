"""
Secrets Manager for Vigil SOC

Provides pluggable secrets storage backends with priority fallback:
1. Encrypted local file at ``~/.vigil/secrets.enc`` (preferred; at-rest encrypted)
2. Environment variables
3. .env file (legacy / interoperability)
4. Keyring (only when explicitly enabled)

Usage:
    from backend.secrets_manager import get_secret, set_secret

    api_key = get_secret("CLAUDE_API_KEY")
    set_secret("CLAUDE_API_KEY", "sk-ant-...")
"""

import json
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


class EncryptedFileBackend(SecretsBackend):
    """Project-local, at-rest encrypted secret store.

    Secrets live in a Fernet-encrypted JSON blob at ``~/.vigil/secrets.enc``.
    The symmetric key lives alongside it in ``~/.vigil/master.key`` (chmod
    600, auto-generated on first write). Both files sit outside the repo
    so ``.env`` rewrites, ``setup_dev.sh``, or resetting the project dir
    never nuke stored credentials.

    This is the preferred backend when running locally. Keys stored here
    are never written to ``.env`` and are not exposed to other processes.
    """

    DEFAULT_DIR = Path.home() / ".vigil"
    SECRETS_FILENAME = "secrets.enc"
    MASTER_KEY_FILENAME = "master.key"

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or self.DEFAULT_DIR
        self.secrets_path = self.data_dir / self.SECRETS_FILENAME
        self.master_key_path = self.data_dir / self.MASTER_KEY_FILENAME
        self._fernet = None  # lazy
        self._cache: Optional[Dict[str, str]] = None
        # mtime of the last ``secrets.enc`` load. Used by ``_load_cache``
        # to detect cross-process writes so the backend picks up secrets
        # saved by sibling processes without a restart.
        self._cache_mtime: float = 0.0
        # `cryptography` is listed in requirements.txt; guard in case a
        # slim deploy is missing it.
        try:
            from cryptography.fernet import Fernet  # noqa: F401
            self._crypto_ok = True
        except Exception as e:
            logger.debug(f"EncryptedFileBackend disabled (cryptography missing): {e}")
            self._crypto_ok = False

    def _ensure_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass

    def _load_or_create_master_key(self) -> bytes:
        """Return the Fernet key bytes, creating ``master.key`` on first use."""
        from cryptography.fernet import Fernet

        self._ensure_dir()
        if self.master_key_path.exists():
            return self.master_key_path.read_bytes().strip()
        key = Fernet.generate_key()
        # Atomic write
        tmp = self.master_key_path.with_suffix(".tmp")
        tmp.write_bytes(key)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.master_key_path)
        logger.info(f"Generated new Vigil master key at {self.master_key_path}")
        return key

    def _get_fernet(self):
        from cryptography.fernet import Fernet
        if self._fernet is None:
            self._fernet = Fernet(self._load_or_create_master_key())
        return self._fernet

    def _current_mtime(self) -> float:
        """Return the secrets file's mtime, or 0 if it doesn't exist."""
        try:
            return self.secrets_path.stat().st_mtime
        except OSError:
            return 0.0

    def _load_cache(self) -> Dict[str, str]:
        # Invalidate the in-memory cache if another process (or another
        # instance in this process) has written to the file since we
        # last read it. Without this, the backend couldn't see secrets
        # saved by sibling processes (CLI tools, other workers, the MCP
        # dormant-retry auto-reconnect path, etc.).
        current_mtime = self._current_mtime()
        if (
            self._cache is not None
            and current_mtime
            and current_mtime != getattr(self, "_cache_mtime", 0.0)
        ):
            logger.debug("Secrets file changed on disk — reloading cache")
            self._cache = None

        if self._cache is not None:
            return self._cache
        if not self.secrets_path.exists():
            self._cache = {}
            self._cache_mtime = current_mtime
            return self._cache
        try:
            from cryptography.fernet import InvalidToken  # noqa: F401
            blob = self.secrets_path.read_bytes()
            plaintext = self._get_fernet().decrypt(blob)
            self._cache = json.loads(plaintext.decode("utf-8"))
            self._cache_mtime = current_mtime
            logger.debug(f"Loaded {len(self._cache)} secrets from {self.secrets_path}")
        except Exception as e:
            # Don't silently wipe: log and present an empty view, but leave
            # the encrypted file untouched so a bad master key doesn't
            # destroy data.
            logger.error(
                f"Could not decrypt {self.secrets_path} ({e}); "
                f"treating as empty. If the master key changed, restore "
                f"~/.vigil/master.key from a backup."
            )
            self._cache = {}
            self._cache_mtime = current_mtime
        return self._cache

    def _write_cache(self) -> bool:
        try:
            self._ensure_dir()
            plaintext = json.dumps(self._cache or {}, sort_keys=True).encode("utf-8")
            blob = self._get_fernet().encrypt(plaintext)
            tmp = self.secrets_path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.secrets_path)
            # Keep our mtime tracker in sync so we don't needlessly
            # re-read the file we just wrote.
            self._cache_mtime = self._current_mtime()
            return True
        except Exception as e:
            logger.error(f"Error writing encrypted secrets: {e}")
            return False

    def get(self, key: str) -> Optional[str]:
        if not self._crypto_ok:
            return None
        value = self._load_cache().get(key)
        if value:
            logger.debug(f"Found secret '{key}' in encrypted store")
        return value

    def set(self, key: str, value: str) -> bool:
        if not self._crypto_ok:
            logger.error("EncryptedFileBackend unavailable (cryptography missing)")
            return False
        cache = self._load_cache()
        cache[key] = value
        if self._write_cache():
            logger.info(f"Set secret '{key}' in encrypted store")
            return True
        return False

    def delete(self, key: str) -> bool:
        if not self._crypto_ok:
            return False
        cache = self._load_cache()
        if key in cache:
            del cache[key]
            if self._write_cache():
                logger.info(f"Deleted secret '{key}' from encrypted store")
        return True

    def is_available(self) -> bool:
        return self._crypto_ok


class SecretsManager:
    """
    Unified secrets manager that tries multiple backends in priority order.

    Priority for reading:
    1. Encrypted local file (``~/.vigil/secrets.enc``; preferred)
    2. Environment variables
    3. .env file (legacy / interoperability)
    4. Keyring (only when explicitly enabled)

    Priority for writing: configurable via ``SECRETS_BACKEND``. Default is
    ``encrypted`` when ``cryptography`` is available, otherwise ``dotenv``.
    """

    def __init__(self, write_backend: str = "encrypted", enable_keyring: bool = False):
        """Initialize secrets manager.

        Args:
            write_backend: "encrypted", "env", "dotenv", or "keyring".
            enable_keyring: Include keyring in read backends when True. Keyring
                access triggers macOS keychain prompts, so off by default.
        """
        self.encrypted_backend = EncryptedFileBackend()
        self.env_backend = EnvironmentBackend()
        self.dotenv_backend = DotEnvBackend()
        # Use lazy init to avoid triggering keychain prompts on startup
        self.keyring_backend = KeyringBackend(lazy_init=True)
        self.enable_keyring = enable_keyring or (write_backend == "keyring")

        # Graceful fallback if user asked for "encrypted" but cryptography
        # isn't installed — prevents hard failure on slim deploys.
        if write_backend == "encrypted" and not self.encrypted_backend.is_available():
            logger.warning(
                "EncryptedFileBackend unavailable; falling back to dotenv write backend"
            )
            write_backend = "dotenv"

        # Read priority — encrypted first (preferred), then env, then dotenv,
        # then keyring only when explicitly enabled.
        self.read_backends = []
        if self.encrypted_backend.is_available():
            self.read_backends.append(self.encrypted_backend)
        self.read_backends.extend([self.env_backend, self.dotenv_backend])
        if self.enable_keyring:
            self.read_backends.append(self.keyring_backend)
        else:
            logger.debug("Keyring backend disabled - will not check keyring for secrets")

        # Write backend (configurable based on deployment)
        self.write_backend_name = write_backend
        backend_map = {
            "encrypted": self.encrypted_backend,
            "env": self.env_backend,
            "dotenv": self.dotenv_backend,
            "keyring": self.keyring_backend,
        }
        self.write_backend = backend_map.get(write_backend, self.encrypted_backend)

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
        for backend in [
            self.encrypted_backend,
            self.env_backend,
            self.dotenv_backend,
            self.keyring_backend,
        ]:
            if backend.is_available():
                if not backend.delete(key):
                    success = False
        return success
    
    def get_backend_status(self) -> Dict[str, Any]:
        """Get status of all backends."""
        return {
            "encrypted": {
                "available": self.encrypted_backend.is_available(),
                "path": str(self.encrypted_backend.secrets_path),
                "description": "Project-local encrypted file (preferred)"
            },
            "environment": {
                "available": self.env_backend.is_available(),
                "description": "Environment variables (best for containers/servers)"
            },
            "dotenv": {
                "available": self.dotenv_backend.is_available(),
                "path": str(self.dotenv_backend.env_file),
                "description": "File-based secrets (legacy)"
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
            write_backend = os.environ.get("SECRETS_BACKEND", "encrypted")
        
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

