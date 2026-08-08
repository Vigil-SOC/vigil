from typing import Optional

from core import secrets_manager as _secrets_manager
from core.secrets_manager import SecretsManager, get_secrets_manager

__all__ = [
    "SecretsManager",
    "delete_secret",
    "get_secret",
    "get_secrets_manager",
    "set_secret",
]


# The one credential-read channel for backend, services, and daemon code. Order
# lives in secrets_manager: encrypted store, then env, dotenv, keyring.
def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    return _secrets_manager.get_secret(key, default)  # late lookup: one patch seam


def set_secret(key: str, value: str) -> bool:
    return _secrets_manager.set_secret(key, value)


def delete_secret(key: str) -> bool:
    return _secrets_manager.delete_secret(key)
