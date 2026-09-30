"""The self-hosted Splunk MCP tool reads its config through the descriptor resolver.

``core/integrations/splunk/tool.py`` used to read SPLUNK_URL, SPLUNK_USERNAME and
SPLUNK_PASSWORD through the secrets store and then the environment. ``server_url``
and ``username`` are not secrets: Settings writes them onto the integration row,
where that read never looked, so every hunt reported "Splunk not configured" no
matter what the operator saved (#1113). These tests lock the two paths that must
both construct the client: a Settings save with no SPLUNK_* in the environment,
and an env-only deployment with nothing saved.

The fake ``get_secret`` answers only from the dict a test hands it. It has no
environment fallback on purpose: the env-only case then proves the resolver asks
for the exact keys the real chain reads out of the environment (SPLUNK_URL, not
the canonical SPLUNK_SERVER_URL), and the Settings case proves nothing leaked in
from the process environment.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from pathlib import Path

import pytest

import core.integrations._base.config as resolver
import core.integrations.splunk.client as splunk_client
from core.integrations.mcp.service import MCPService

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent.parent.parent
SPLUNK_TOOL = REPO / "core" / "integrations" / "splunk" / "tool.py"

_SPLUNK_ENV = ("SPLUNK_URL", "SPLUNK_USERNAME", "SPLUNK_PASSWORD", "SPLUNK_VERIFY_SSL")
_STORED = {"server_url": "https://stored.example:8089", "username": "stored-admin"}
_ENV_ONLY = {
    "SPLUNK_URL": "https://env-only.example:8089",
    "SPLUNK_USERNAME": "env-admin",
    "SPLUNK_PASSWORD": "env-password",
}


class _Constructed:
    """Stand-in for SplunkService that records what it was built with."""

    def __init__(
        self, server_url, username, password, verify_ssl=False, ca_cert_path=None
    ):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.ca_cert_path = ca_cert_path


@pytest.fixture
def splunk_mod(monkeypatch):
    """Import the Splunk MCP tool in isolation, with SPLUNK_* env cleared.

    The module's import-time ``load_dotenv()`` is skipped under the suite's
    ``VIGIL_DISABLE_DOTENV`` (tests/conftest.py), so a developer's ``.env``
    cannot repopulate these keys; see test_tool_dotenv_disabled.py.
    """
    spec = importlib.util.spec_from_file_location("splunk_tool_under_test", SPLUNK_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in _SPLUNK_ENV:
        monkeypatch.delenv(key, raising=False)
    # The tool imports SplunkService lazily from the client module, so patching
    # it there is what get_splunk_service() sees.
    monkeypatch.setattr(splunk_client, "SplunkService", _Constructed)
    return module


def _seed(monkeypatch, stored, secrets):
    """Seed the resolver the way production writes: non-secrets on the
    integration row, secrets in the store. ``secrets`` also stands in for the
    environment, which the real get_secret reads by the same key names.
    Follows tests/unit/integrations/test_config_resolver.py."""
    monkeypatch.setattr(resolver, "get_integration_config", lambda _id: dict(stored))
    monkeypatch.setattr(
        resolver, "get_secret", lambda key, default=None: secrets.get(key, default)
    )


def _tool_reply(content):
    assert len(content) == 1
    return json.loads(content[0].text)


def test_settings_saved_config_reaches_the_client(splunk_mod, monkeypatch):
    """A URL and username saved in Settings, plus the password the save routed
    to the secrets store, are what the client is constructed with -- with no
    SPLUNK_* in the environment."""
    _seed(
        monkeypatch,
        stored={**_STORED, "verify_ssl": "true"},
        secrets={"SPLUNK_PASSWORD": "from-secrets-store"},
    )

    service = splunk_mod.get_splunk_service()

    assert service is not None, "Settings-saved Splunk read as not configured"
    assert service.server_url == "https://stored.example:8089"
    assert service.username == "stored-admin"
    assert service.password == "from-secrets-store"
    assert service.verify_ssl is True


def test_env_only_deployment_still_constructs_the_client(splunk_mod, monkeypatch):
    """Nothing saved in Settings: SPLUNK_URL, SPLUNK_USERNAME and SPLUNK_PASSWORD
    still build the client. SPLUNK_URL, not the canonical SPLUNK_SERVER_URL,
    because that is the name env.example and every deployment already set."""
    _seed(monkeypatch, stored={}, secrets=dict(_ENV_ONLY))

    service = splunk_mod.get_splunk_service()

    assert service is not None, "env-only Splunk read as not configured"
    assert service.server_url == "https://env-only.example:8089"
    assert service.username == "env-admin"
    assert service.password == "env-password"


def test_canonical_server_url_name_is_not_read(splunk_mod, monkeypatch):
    """The override is exclusive: SPLUNK_SERVER_URL was never documented and
    must not quietly become a second spelling."""
    _seed(
        monkeypatch,
        stored={},
        secrets={**_ENV_ONLY, "SPLUNK_URL": "", "SPLUNK_SERVER_URL": "https://x:8089"},
    )

    assert splunk_mod.get_splunk_service() is None


def test_process_env_reaches_the_child_without_placeholders(monkeypatch):
    """Dropping the ${SPLUNK_*} placeholders must not cut the child off from the
    backend's environment. stdio_client narrows the child env to six names plus
    the server's env block, and that block is built from a copy of os.environ
    (core/integrations/mcp/service.py), so an env-only deployment with no .env
    file still hands the tool SPLUNK_URL."""
    for key, value in _ENV_ONLY.items():
        monkeypatch.setenv(key, value)

    service = MCPService(project_root=REPO)
    service.reload_server_configs()
    server = service.servers["splunk-selfhosted"]

    assert server.required_env_vars == [], "a placeholder crept back in"
    for key, value in _ENV_ONLY.items():
        assert server.env.get(key) == value, f"{key} did not reach the child env"


def test_settings_url_beats_the_environment(splunk_mod, monkeypatch):
    """Both set: the row wins for non-secrets. An operator who saved a URL in
    Settings gets that URL even while a stale SPLUNK_URL lingers in .env. The
    password is store-first too (SecretsManager reads the encrypted backend
    before the environment), so both halves point at the Settings save."""
    _seed(monkeypatch, stored=_STORED, secrets={**_ENV_ONLY, "SPLUNK_PASSWORD": "s"})

    service = splunk_mod.get_splunk_service()

    assert service.server_url == "https://stored.example:8089"
    assert service.username == "stored-admin"


def test_empty_stored_url_falls_back_to_the_environment(splunk_mod, monkeypatch):
    """An empty string on the row is unset, not a URL of ""."""
    _seed(
        monkeypatch,
        stored={"server_url": "", "username": ""},
        secrets=dict(_ENV_ONLY),
    )

    service = splunk_mod.get_splunk_service()

    assert service.server_url == "https://env-only.example:8089"
    assert service.username == "env-admin"


@pytest.mark.parametrize(
    ("stored_value", "expected"),
    [
        ("true", True),
        ("false", False),
        ("0", False),
        (True, True),
        (False, False),
    ],
    ids=["str-true", "str-false", "str-0", "bool-true", "bool-false"],
)
def test_verify_ssl_is_parsed_not_truth_tested(
    splunk_mod, monkeypatch, stored_value, expected
):
    """The descriptor declares verify_ssl as a bool, so the resolver coerces the
    strings the env channel and older rows hold: "false" must reach the client as
    False, never as a truthy non-empty string."""
    _seed(
        monkeypatch,
        stored={**_STORED, "verify_ssl": stored_value},
        secrets={"SPLUNK_PASSWORD": "secret"},
    )

    assert splunk_mod.get_splunk_service().verify_ssl is expected


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("true", True), ("false", False)],
    ids=["env-true", "env-false"],
)
def test_verify_ssl_from_the_environment(splunk_mod, monkeypatch, env_value, expected):
    """SPLUNK_VERIFY_SSL reaches the client through the same coercion. The
    "true" case is the discriminating one: False is also the unset default, so
    "false" alone would pass with the env channel ignored."""
    _seed(monkeypatch, stored={}, secrets={**_ENV_ONLY, "SPLUNK_VERIFY_SSL": env_value})

    assert splunk_mod.get_splunk_service().verify_ssl is expected


def test_unset_verify_ssl_means_no_verification(splunk_mod, monkeypatch):
    """resolve() hands back None for an unset verify_ssl. Unset has always meant
    no verification for the self-hosted server (port 8089 ships a self-signed
    certificate), so None must stay False rather than default to True."""
    _seed(monkeypatch, stored=_STORED, secrets={"SPLUNK_PASSWORD": "secret"})

    service = splunk_mod.get_splunk_service()

    assert service is not None
    assert service.verify_ssl is False


def test_no_server_url_anywhere_is_not_configured(splunk_mod, monkeypatch):
    _seed(monkeypatch, stored={"username": "admin"}, secrets={"SPLUNK_PASSWORD": "x"})

    assert splunk_mod.get_splunk_service() is None


@pytest.mark.parametrize(
    ("stored", "secrets"),
    [
        ({"server_url": "https://stored.example:8089"}, {"SPLUNK_PASSWORD": "x"}),
        (_STORED, {}),
        (_STORED, {"SPLUNK_PASSWORD": ""}),
    ],
    ids=["no-username", "no-password", "empty-password"],
)
def test_partial_config_is_not_configured(splunk_mod, monkeypatch, stored, secrets):
    """A URL with no credentials is not a configured Splunk: the REST login needs
    all three, and a client built without them fails every search at auth."""
    _seed(monkeypatch, stored=stored, secrets=secrets)

    assert splunk_mod.get_splunk_service() is None


def test_partial_config_names_the_missing_fields_at_debug(
    splunk_mod, monkeypatch, caplog
):
    """URL and username saved, password never entered: the trace says which
    fields are missing, at DEBUG because an unconfigured install hits this on
    every call, and never what the present fields hold."""
    _seed(monkeypatch, stored=_STORED, secrets={})

    with caplog.at_level(logging.DEBUG, logger=splunk_mod.logger.name):
        assert splunk_mod.get_splunk_service() is None

    record = next(r for r in caplog.records if "not configured" in r.getMessage())
    assert record.levelno == logging.DEBUG
    assert "password" in record.getMessage()
    assert "server_url" not in record.getMessage()
    assert "stored.example" not in caplog.text
    assert "stored-admin" not in caplog.text


def test_resolver_failure_is_logged_without_its_message(
    splunk_mod, monkeypatch, caplog
):
    """A DB or decrypt error reads as "not configured" to the caller, but it
    must leave a trace: silent None is how #1113 stayed invisible. The trace is
    the exception class, never its message, which a secret-backend error can
    fill with the value it failed on."""
    sentinel = "hunter2-sentinel-secret"

    class DecryptFailure(RuntimeError):
        pass

    def _boom(_id):
        raise DecryptFailure(f"could not decrypt SPLUNK_PASSWORD={sentinel}")

    monkeypatch.setattr(resolver, "get_integration_config", _boom)

    with caplog.at_level(logging.DEBUG, logger=splunk_mod.logger.name):
        assert splunk_mod.get_splunk_service() is None

    assert "DecryptFailure" in caplog.text
    assert sentinel not in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_unconfigured_server_lists_tools_and_answers_not_configured(
    splunk_mod, monkeypatch
):
    """splunk-selfhosted is default-enabled and, with no placeholder to hold it
    dormant, starts on a fresh install. Every handler that reaches Splunk must
    then reply with a structured "Splunk not configured" rather than raise;
    splunk_generate_spl needs no server and keeps working; and list_tools must
    still succeed with no telemetry summary to add."""
    _seed(monkeypatch, stored={}, secrets={})

    tools = asyncio.run(splunk_mod.handle_list_tools())
    assert [t.name for t in tools] == [
        "splunk_generate_spl",
        "splunk_execute",
        "splunk_search_ip",
        "splunk_search_host",
        "splunk_nl_search",
    ]

    generated = _tool_reply(
        asyncio.run(
            splunk_mod.handle_call_tool("splunk_generate_spl", {"query": "brute force"})
        )
    )
    assert generated["pattern"] == "brute force"
    assert generated["spl_query"].startswith("index=*")
    assert "error" not in generated

    calls = {
        "splunk_execute": {"spl_query": "index=* | head 1"},
        "splunk_search_ip": {"ip_address": "10.0.0.1"},
        "splunk_search_host": {"hostname": "host-1"},
        "splunk_nl_search": {"query": "failed login"},
    }
    for name, args in calls.items():
        reply = _tool_reply(asyncio.run(splunk_mod.handle_call_tool(name, args)))
        assert reply["error"] == "Splunk not configured", name
        assert "success" not in reply, name


def test_no_direct_environ_reads_for_splunk_creds():
    """Guardrail: the tool must not regress to reading SPLUNK_* itself, from
    the environment or the secrets store. ``resolve`` is the one path."""
    text = SPLUNK_TOOL.read_text()
    forbidden = [
        'os.environ.get("SPLUNK_',
        'get_secret("SPLUNK_',
        "_read_credential(",
    ]
    for pattern in forbidden:
        assert pattern not in text, f"Direct SPLUNK_* read resurfaced: {pattern}"
