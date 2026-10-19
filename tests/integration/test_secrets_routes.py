"""Integration tests for /api/config/secrets/* admin routes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


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


_USER = MagicMock(user_id="u-1")


def _report(**overrides):
    base = {
        "migrated": [],
        "already_present": [],
        "conflicts": [],
        "errors": [],
        "encrypted_available": True,
        "dotenv_path": "/tmp/.env",
    }
    base.update(overrides)
    return base


def test_status_route_returns_backend_state():
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.get_backend_status.return_value = _fake_status()
    with patch("services.api.routers.config.get_secrets_manager", return_value=mgr):
        result = config_module.secrets_status()
    assert result["write_backend"] == "encrypted"
    assert result["cryptography_available"] is True


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
        result = config_module.secrets_migrate_to_encrypted(
            _SecretsMigrateRequest(keys=["FOO"], remove_from_dotenv=False),
            _USER,
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
        config_module.secrets_migrate_to_encrypted(None, _USER)

    mgr.migrate_dotenv_secrets_to_encrypted.assert_called_once_with(
        keys=None, remove_from_dotenv=True
    )


def _migrate(report):
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.migrate_dotenv_secrets_to_encrypted.return_value = report
    svc = MagicMock()
    with patch(
        "services.api.routers.config.get_secrets_manager", return_value=mgr
    ), patch("services.api.routers.config._for_user", return_value=svc):
        result = config_module.secrets_migrate_to_encrypted(None, _USER)
    return result, svc


def test_migrate_route_status_codes_carry_the_report():
    unavailable = _report(
        encrypted_available=False, errors=[{"key": "<all>", "error": "nope"}]
    )
    result, _ = _migrate(unavailable)
    assert result.status_code == 503
    assert json.loads(result.body) == unavailable

    errored = _report(errors=[{"key": "FOO", "error": "boom"}])
    result, _ = _migrate(errored)
    assert result.status_code == 500
    assert json.loads(result.body) == errored

    # Conflicts alone are a successful run.
    conflicted = _report(conflicts=[{"key": "FOO", "reason": "value_mismatch"}])
    result, _ = _migrate(conflicted)
    assert result == conflicted


def test_migrate_route_audits_key_names_only():
    report = _report(
        migrated=["A"],
        already_present=["B"],
        conflicts=[{"key": "C", "reason": "value_mismatch"}],
        errors=[{"key": "D", "error": "secret-ish detail"}],
    )
    _, svc = _migrate(report)
    svc.record_audit.assert_called_once()
    assert svc.record_audit.call_args.kwargs["new_value"] == {
        "remove_from_dotenv": True,
        "migrated": ["A"],
        "already_present": ["B"],
        "conflicts": ["C"],
        "errors": ["D"],
    }


def test_migrate_route_survives_audit_failure():
    from services.api.routers import config as config_module

    mgr = MagicMock()
    mgr.migrate_dotenv_secrets_to_encrypted.return_value = _report(migrated=["A"])
    with patch(
        "services.api.routers.config.get_secrets_manager", return_value=mgr
    ), patch(
        "services.api.routers.config._for_user", side_effect=RuntimeError("db down")
    ):
        result = config_module.secrets_migrate_to_encrypted(None, _USER)
    assert result["migrated"] == ["A"]
