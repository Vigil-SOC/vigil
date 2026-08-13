"""Integration tests for /api/config/secrets/* admin routes."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DEV_MODE", "true")


def _fake_status(**overrides):
    base = {
        "encrypted": {
            "available": True,
            "path": "/tmp/secrets.enc",
            "secrets_file_exists": True,
            "master_key_present": True,
            "master_key_path": "/tmp/master.key",
            "description": "encrypted",
        },
        "environment": {"available": True, "description": "env"},
        "dotenv": {
            "available": True,
            "path": "/tmp/.env",
            "exists": True,
            "description": "dotenv",
        },
        "keyring": {"available": False, "description": "keyring"},
        "cryptography_available": True,
        "write_backend": "encrypted",
        "expected_write_backend": "encrypted",
        "read_backends": ["EncryptedFileBackend", "EnvironmentBackend"],
    }
    base.update(overrides)
    return base


def test_status_route_returns_backend_state():
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.get_backend_status.return_value = _fake_status()
    with patch("services.api.routers.config.get_secrets_manager", return_value=mgr):
        result = asyncio.run(config_module.secrets_status())
    assert result["write_backend"] == "encrypted"
    assert result["cryptography_available"] is True


def test_reinit_route_force_reloads_singleton():
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.get_backend_status.return_value = _fake_status(write_backend="encrypted")
    with patch(
        "services.api.routers.config.get_secrets_manager", return_value=mgr
    ) as mock_get:
        result = asyncio.run(config_module.secrets_reinit())
    # No body → no override; force_reload must still be True.
    mock_get.assert_called_with(write_backend=None, force_reload=True)
    assert result["reloaded"] is True
    assert result["status"]["write_backend"] == "encrypted"


def test_reinit_route_accepts_write_backend_override():
    """Pass {'write_backend': 'encrypted'} to force a backend even when
    os.environ['SECRETS_BACKEND'] is stale (e.g. user just edited .env)."""
    from services.api.routers import config as config_module
    from services.api.routers.config import _SecretsReinitRequest

    mgr = MagicMock()
    mgr.get_backend_status.return_value = _fake_status(write_backend="encrypted")
    with patch(
        "services.api.routers.config.get_secrets_manager", return_value=mgr
    ) as mock_get:
        asyncio.run(
            config_module.secrets_reinit(
                _SecretsReinitRequest(write_backend="encrypted")
            )
        )
    mock_get.assert_called_with(write_backend="encrypted", force_reload=True)


def test_migrate_route_routes_to_secrets_manager_helper():
    from services.api.routers import config as config_module
    from services.api.routers.config import _SecretsMigrateRequest

    mgr = MagicMock()
    mgr.migrate_dotenv_secrets_to_encrypted.return_value = {
        "migrated": ["FOO"],
        "already_present": [],
        "conflicts": [],
        "errors": [],
        "encrypted_available": True,
        "dotenv_path": "/tmp/.env",
    }
    with patch("services.api.routers.config.get_secrets_manager", return_value=mgr):
        result = asyncio.run(
            config_module.secrets_migrate_to_encrypted(
                _SecretsMigrateRequest(keys=["FOO"], remove_from_dotenv=False)
            )
        )

    mgr.migrate_dotenv_secrets_to_encrypted.assert_called_once_with(
        keys=["FOO"], remove_from_dotenv=False
    )
    assert result["migrated"] == ["FOO"]


def test_migrate_route_defaults_when_body_omitted():
    """Body is optional; defaults are keys=None, remove_from_dotenv=True."""
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.migrate_dotenv_secrets_to_encrypted.return_value = {
        "migrated": [],
        "already_present": [],
        "conflicts": [],
        "errors": [],
        "encrypted_available": True,
        "dotenv_path": "/tmp/.env",
    }
    with patch("services.api.routers.config.get_secrets_manager", return_value=mgr):
        asyncio.run(config_module.secrets_migrate_to_encrypted(None))

    mgr.migrate_dotenv_secrets_to_encrypted.assert_called_once_with(
        keys=None, remove_from_dotenv=True
    )
