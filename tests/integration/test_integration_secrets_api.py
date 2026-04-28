"""Integration tests for the integration-config endpoints' secret handling.

The generic ``POST /config/integrations`` endpoint must route registered
secret fields through ``set_secret`` (so they land in the encrypted
secrets store) and strip them from the dict that gets persisted to the
database / JSON file. The matching ``GET`` endpoint must redact those
fields on read so plaintext credentials never leak to the frontend.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DEV_MODE", "true")


def _post_payload():
    """Realistic VStrike save payload from the Settings UI (JWT-only auth)."""
    from backend.api.config import IntegrationsConfig

    return IntegrationsConfig(
        enabled_integrations=["vstrike"],
        integrations={
            "vstrike": {
                "url": "https://vstrike.net",
                "verify_ssl": False,
                "username": "deeptempo_manager",
                "password": "shh-secret",
            }
        },
    )


def _invoke_post(payload, *, set_secret=None, config_service=None, tmp_home=None):
    """Run the async handler with patched secrets writer + config service."""
    from backend.api import config as config_module

    set_secret = set_secret or MagicMock(return_value=True)
    config_service = config_service or MagicMock()
    config_service.set_integration_config.return_value = True

    patches = [
        patch.object(config_module, "set_secret", set_secret),
        patch.object(config_module, "get_config_service", return_value=config_service),
    ]
    if tmp_home is not None:
        patches.append(patch.object(Path, "home", return_value=tmp_home))

    for p in patches:
        p.start()
    try:
        result = asyncio.run(config_module.set_integrations_config(payload))
    finally:
        for p in reversed(patches):
            p.stop()
    return result, set_secret, config_service


def test_post_routes_secrets_to_set_secret(tmp_path):
    payload = _post_payload()
    result, set_secret, config_service = _invoke_post(payload, tmp_home=tmp_path)

    assert result["success"] is True

    # Each registered secret field should go through set_secret with the
    # secrets-store key from services.integration_secrets.
    written = {call.args[0]: call.args[1] for call in set_secret.call_args_list}
    assert written["VSTRIKE_USERNAME"] == "deeptempo_manager"
    assert written["VSTRIKE_PASSWORD"] == "shh-secret"

    # The DB write should contain ONLY non-secret fields.
    saved_config = config_service.set_integration_config.call_args.kwargs["config"]
    assert saved_config == {"url": "https://vstrike.net", "verify_ssl": False}
    assert "username" not in saved_config
    assert "password" not in saved_config


def test_post_strips_secrets_from_json_mirror(tmp_path):
    payload = _post_payload()
    _invoke_post(payload, tmp_home=tmp_path)

    json_path = tmp_path / ".deeptempo" / "integrations_config.json"
    assert json_path.exists()
    import json

    on_disk = json.loads(json_path.read_text())
    assert on_disk["enabled_integrations"] == ["vstrike"]
    persisted = on_disk["integrations"]["vstrike"]
    assert persisted == {"url": "https://vstrike.net", "verify_ssl": False}
    # No secrets in plaintext
    for forbidden in ("username", "password"):
        assert forbidden not in persisted


def test_post_skips_empty_secret_means_keep_existing(tmp_path):
    """Empty-string secret fields must NOT call set_secret (overwrite-skip)."""
    from backend.api.config import IntegrationsConfig

    payload = IntegrationsConfig(
        enabled_integrations=["vstrike"],
        integrations={
            "vstrike": {
                "url": "https://vstrike.net",
                "verify_ssl": True,
                "username": "alice",
                "password": "",
            }
        },
    )
    _result, set_secret, _ = _invoke_post(payload, tmp_home=tmp_path)

    written = {call.args[0]: call.args[1] for call in set_secret.call_args_list}
    # Only the non-empty username should have been written.
    assert written == {"VSTRIKE_USERNAME": "alice"}


def test_post_unregistered_integration_pass_through(tmp_path):
    """Integrations without a secret-field registry retain old behavior."""
    from backend.api.config import IntegrationsConfig

    payload = IntegrationsConfig(
        enabled_integrations=["brand-new-thing"],
        integrations={"brand-new-thing": {"foo": "bar", "verify_ssl": True}},
    )
    _result, set_secret, config_service = _invoke_post(payload, tmp_home=tmp_path)

    # No secrets routed to the secrets store...
    assert set_secret.call_args_list == []
    # ...and the full config still reaches the DB write.
    saved = config_service.set_integration_config.call_args.kwargs["config"]
    assert saved == {"foo": "bar", "verify_ssl": True}


def test_get_redacts_registered_secret_fields(tmp_path):
    """GET handler must strip registered secret fields on read."""
    from backend.api import config as config_module

    fake_service = MagicMock()
    fake_service.list_integrations.return_value = [
        {
            "integration_id": "vstrike",
            "enabled": True,
            "config": {
                # Pretend a legacy plaintext row still exists in DB.
                "url": "https://vstrike.net",
                "verify_ssl": True,
                "username": "alice",
                "password": "wonderland",
            },
        }
    ]

    with patch.object(config_module, "get_config_service", return_value=fake_service):
        result = asyncio.run(config_module.get_integrations_config())

    cfg = result["integrations"]["vstrike"]
    assert cfg == {"url": "https://vstrike.net", "verify_ssl": True}
    for forbidden in ("username", "password"):
        assert forbidden not in cfg
