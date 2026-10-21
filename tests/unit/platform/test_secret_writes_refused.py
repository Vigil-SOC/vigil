"""On a per-pod state directory, Settings refuses to save a credential (GH #2577).

The Helm chart sets VIGIL_DIR_EPHEMERAL when the State Directory is an emptyDir.
A secret written there would reach one backend pod and vanish on restart, while
the UI reported success. Env-injected credentials must still resolve.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.secrets_manager import SECRET_WRITE_REFUSED_DETAIL, SecretsManager

pytestmark = pytest.mark.unit


@pytest.fixture
def ephemeral_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.setenv("VIGIL_DIR_EPHEMERAL", "true")
    monkeypatch.delenv("SPLUNK_PASSWORD", raising=False)
    return tmp_path


def test_set_writes_nothing(ephemeral_state_dir):
    for backend in ("encrypted", "dotenv"):
        assert SecretsManager(write_backend=backend).set("SPLUNK_PASSWORD", "x") is False
    assert list(ephemeral_state_dir.iterdir()) == []
    assert SecretsManager().get("SPLUNK_PASSWORD") is None  # os.environ untouched


def test_env_injected_credential_still_reads(ephemeral_state_dir, monkeypatch):
    monkeypatch.setenv("SPLUNK_PASSWORD", "from-existing-secret")
    assert SecretsManager().get("SPLUNK_PASSWORD") == "from-existing-secret"


def test_integrations_save_names_the_alternatives(ephemeral_state_dir):
    from services.api.routers import config as config_module

    payload = config_module.IntegrationsConfig(
        enabled_integrations=["vstrike"],
        integrations={"vstrike": {"url": "https://vstrike.net", "password": "s"}},
    )
    svc = MagicMock()
    with patch.object(config_module, "get_config_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            config_module.set_integrations_config(payload, current_user=MagicMock())

    assert exc.value.detail == SECRET_WRITE_REFUSED_DETAIL
    assert "secrets.existingSecret" in exc.value.detail
    assert "ExternalSecrets" in exc.value.detail
    svc.set_integration_config.assert_not_called()
    assert list(ephemeral_state_dir.iterdir()) == []


def test_s3_save_does_not_report_success(ephemeral_state_dir):
    from services.api.routers import config as config_module

    svc = MagicMock()
    with patch.object(config_module, "_for_user", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            config_module.set_s3_config(
                config_module.S3Config(
                    bucket_name="b", access_key_id="AKIA", secret_access_key="s"
                ),
                current_user=MagicMock(),
            )
    assert exc.value.detail == SECRET_WRITE_REFUSED_DETAIL
    svc.set_integration_config.assert_not_called()
