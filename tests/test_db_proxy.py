"""Unit tests for services.db_proxy.

Each proxy mode is exercised with a dummy secrets-manager and (for the
SSH tunnel mode) a stubbed ``sshtunnel.SSHTunnelForwarder`` so the test
does not need a real ssh server.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from services import db_proxy
from services.db_proxy import (
    ProxyConfig,
    apply,
    child_env_for_proxy,
)


def test_proxy_config_disabled_by_default():
    cfg = ProxyConfig()
    assert cfg.enabled is False
    applied = apply("db.internal", 5432, cfg)
    assert applied.host == "db.internal"
    assert applied.port == 5432
    assert applied.http_proxy_url is None
    assert applied.tunnel_handle is None


def test_proxy_config_from_dict_none_returns_disabled():
    cfg = ProxyConfig.from_dict({"proxy_type": "none"})
    assert cfg.enabled is False
    cfg2 = ProxyConfig.from_dict({"proxy_type": ""})
    assert cfg2.enabled is False


def test_pgbouncer_rewrites_endpoint():
    cfg = ProxyConfig.from_dict(
        {
            "proxy_type": "pgbouncer",
            "proxy_host": "pgbouncer.svc",
            "proxy_port": 6432,
            "verify_proxy_tls": False,
        }
    )
    assert cfg.enabled is True
    applied = apply("db.internal", 5432, cfg)
    assert applied.host == "pgbouncer.svc"
    assert applied.port == 6432
    assert applied.ssl_disabled is True
    assert applied.tunnel_handle is None


def test_pgbouncer_requires_host_and_port():
    cfg = ProxyConfig.from_dict({"proxy_type": "pgbouncer", "proxy_port": 6432})
    with pytest.raises(ValueError):
        apply("db.internal", 5432, cfg)


def test_http_proxy_returns_proxy_url_and_passthrough_endpoint():
    cfg = ProxyConfig.from_dict(
        {
            "proxy_type": "http",
            "proxy_host": "egress.corp",
            "proxy_port": 3128,
            "proxy_username": "alice",
            "proxy_password": "s3cret",
        }
    )
    applied = apply("api.vendor.com", 443, cfg)
    assert applied.host == "api.vendor.com"
    assert applied.port == 443
    assert applied.http_proxy_url == "http://alice:s3cret@egress.corp:3128"


def test_socks5_proxy_uses_socks_scheme():
    cfg = ProxyConfig.from_dict(
        {"proxy_type": "socks5", "proxy_host": "socks.corp", "proxy_port": 1080}
    )
    applied = apply("api.vendor.com", 443, cfg)
    assert applied.http_proxy_url == "socks5://socks.corp:1080"


def test_proxy_password_resolved_from_secrets_manager():
    """When ``password_secret_key`` is provided, the secret value is
    fetched via ``backend.secrets_manager.get_secret`` rather than
    pulled inline from the config dict."""
    fake_secrets = {"SPLUNK_PROXY_PASSWORD": "from-store"}
    with patch.object(db_proxy, "get_secret", side_effect=fake_secrets.get):
        cfg = ProxyConfig.from_dict(
            {
                "proxy_type": "http",
                "proxy_host": "egress.corp",
                "proxy_port": 3128,
                "proxy_username": "u",
                "proxy_password": "",  # blank → use secret
            },
            password_secret_key="SPLUNK_PROXY_PASSWORD",
        )
        assert cfg.proxy_password == "from-store"


def test_child_env_for_http_proxy_emits_standard_env_vars():
    cfg = ProxyConfig.from_dict(
        {"proxy_type": "http", "proxy_host": "egress.corp", "proxy_port": 3128}
    )
    env = child_env_for_proxy(cfg)
    assert env["HTTPS_PROXY"] == "http://egress.corp:3128"
    assert env["HTTP_PROXY"] == "http://egress.corp:3128"
    assert env["ALL_PROXY"] == "http://egress.corp:3128"
    assert env["https_proxy"] == "http://egress.corp:3128"


def test_child_env_for_disabled_proxy_is_empty():
    assert child_env_for_proxy(ProxyConfig()) == {}


def test_child_env_for_pgbouncer_is_empty():
    cfg = ProxyConfig.from_dict(
        {"proxy_type": "pgbouncer", "proxy_host": "p", "proxy_port": 6432}
    )
    assert child_env_for_proxy(cfg) == {}


def test_unknown_proxy_type_is_treated_as_passthrough():
    cfg = ProxyConfig(proxy_type="garbage", proxy_host="x", proxy_port=1)
    # ``enabled`` returns False for unknown types so the passthrough
    # branch in apply() runs.
    assert cfg.enabled is False
    applied = apply("db.internal", 5432, cfg)
    assert applied.host == "db.internal"
    assert applied.port == 5432


def test_ssh_tunnel_opens_forwarder_and_rewrites_endpoint():
    """The SSH tunnel mode imports ``sshtunnel`` lazily. We inject a
    fake module so the test doesn't need a real SSH server."""

    started = {"ok": False, "stopped": False}

    class FakeForwarder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.local_bind_address = ("127.0.0.1", 54321)

        def start(self):
            started["ok"] = True

        def stop(self):
            started["stopped"] = True

    fake_module = ModuleType("sshtunnel")
    fake_module.SSHTunnelForwarder = FakeForwarder
    sys.modules["sshtunnel"] = fake_module
    try:
        cfg = ProxyConfig.from_dict(
            {
                "proxy_type": "ssh_tunnel",
                "proxy_host": "bastion.corp",
                "proxy_port": 22,
                "proxy_username": "vigil",
                "ssh_private_key_path": "/keys/id_ed25519",
            }
        )
        applied = apply("db.private.svc", 5432, cfg)
        assert started["ok"] is True
        assert applied.host == "127.0.0.1"
        assert applied.port == 54321
        assert isinstance(applied.tunnel_handle, FakeForwarder)
        # close() is idempotent and stops the forwarder.
        applied.close()
        assert started["stopped"] is True
        applied.close()  # second call is a no-op
    finally:
        sys.modules.pop("sshtunnel", None)


def test_ssh_tunnel_missing_dep_raises_helpful_error():
    """If sshtunnel isn't installed, the runtime raises a RuntimeError
    pointing the operator at ``pip install sshtunnel``."""
    sys.modules.pop("sshtunnel", None)
    # Force ImportError by putting a sentinel that fails on attribute
    # access — simulating "module installed but broken" is overkill,
    # so we just rely on the absence of the module.
    with patch.dict(sys.modules, {"sshtunnel": None}):
        cfg = ProxyConfig.from_dict(
            {
                "proxy_type": "ssh_tunnel",
                "proxy_host": "bastion.corp",
                "proxy_port": 22,
            }
        )
        with pytest.raises(RuntimeError, match="sshtunnel"):
            apply("db.private.svc", 5432, cfg)


def test_applied_proxy_close_is_safe_when_handle_lacks_stop():
    """Defensive: if a future tunnel object doesn't have ``stop``, the
    close() helper logs and returns instead of raising."""
    applied = db_proxy.AppliedProxy(host="h", port=1, tunnel_handle=SimpleNamespace())
    applied.close()  # should not raise
    assert applied.tunnel_handle is None
