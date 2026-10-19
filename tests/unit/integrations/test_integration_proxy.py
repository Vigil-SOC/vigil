"""``integration_proxy``: stored proxy settings reach each integration's httpx client."""

from __future__ import annotations

import httpx
import pytest

import core.integrations._base.config as resolver
import core.integrations.misp.tool as misp
from core.integrations._base.proxy import integration_proxy
from core.integrations.elastic.client import ElasticService
from core.integrations.integration_secrets import secret_fields_for
from core.integrations.opensearch.client import OpenSearchService
from core.integrations.splunk.client import SplunkService

_HTTP = {"proxy_type": "http", "proxy_host": "proxy.corp", "proxy_port": 3128}


@pytest.fixture
def seed(monkeypatch):
    """Stored config plus secrets store, as production writes them."""

    def _seed(integration_id, stored, secrets=None):
        monkeypatch.setattr(
            resolver, "get_integration_config", lambda _id: dict(stored)
        )
        monkeypatch.setattr(
            resolver,
            "get_secret",
            lambda key, default=None: (secrets or {}).get(key, default),
        )

    return _seed


def test_http_proxy_with_auth_reads_the_password_from_the_secrets_store(seed):
    key = secret_fields_for("elastic-siem")["proxy_password"]
    seed(
        "elastic-siem",
        {**_HTTP, "proxy_username": "svc", "proxy_port": "3128"},
        {key: "p@ss"},
    )
    assert integration_proxy("elastic-siem") == "http://svc:p%40ss@proxy.corp:3128"


def test_socks5_with_auth(seed):
    key = secret_fields_for("splunk")["proxy_password"]
    seed(
        "splunk",
        {**_HTTP, "proxy_type": "socks5", "proxy_username": "svc"},
        {key: "pw"},
    )
    assert integration_proxy("splunk") == "socks5://svc:pw@proxy.corp:3128"


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"proxy_type": "none", "proxy_host": "proxy.corp", "proxy_port": 3128},
        {**_HTTP, "proxy_type": "pgbouncer"},
        {**_HTTP, "proxy_type": "ssh_tunnel"},
        {**_HTTP, "proxy_host": ""},
        {**_HTTP, "proxy_port": ""},
    ],
)
def test_no_usable_proxy_keeps_httpx_env_defaults(seed, stored):
    seed("opensearch", stored)
    assert integration_proxy("opensearch") is None


def _proxy_host(client: httpx.AsyncClient | httpx.Client):
    """Host of the proxy httpx mounted for the client's requests, if any."""
    for transport in client._mounts.values():
        if transport is not None:
            return transport._pool._proxy_url.host
    return None


@pytest.mark.parametrize("configured, host", [(_HTTP, b"proxy.corp"), ({}, None)])
def test_splunk_client_is_built_with_the_proxy(seed, configured, host):
    seed("splunk", configured)
    svc = SplunkService("https://splunk.test:8089", "u", "p")
    assert _proxy_host(svc.session) == host


@pytest.mark.parametrize("configured, host", [(_HTTP, b"proxy.corp"), ({}, None)])
def test_elastic_clients_are_built_with_the_proxy(seed, configured, host):
    seed("elastic-siem", configured)
    svc = ElasticService("https://es.test:9200", kibana_url="https://kb.test:5601")
    assert _proxy_host(svc.es_client) == host
    assert _proxy_host(svc.kibana_client) == host


@pytest.mark.parametrize("configured, host", [(_HTTP, b"proxy.corp"), ({}, None)])
def test_opensearch_clients_are_built_with_the_proxy(seed, configured, host):
    seed("opensearch", configured)
    svc = OpenSearchService(
        "https://os.test:9200", dashboards_url="https://osd.test:5601"
    )
    assert _proxy_host(svc.client) == host
    assert _proxy_host(svc.dashboards_client) == host


@pytest.mark.parametrize(
    "tool, args",
    [
        ("misp_search_ioc", {"value": "1.2.3.4"}),
        ("misp_get_events", {}),
    ],
)
async def test_misp_posts_through_the_proxy(seed, monkeypatch, tool, args):
    seed(
        "misp",
        {"url": "https://misp.test", **_HTTP},
        {secret_fields_for("misp")["api_key"]: "k"},
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200, json={"response": []}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(misp.httpx, "post", fake_post)
    await misp.handle_call_tool(tool, args)
    assert seen["proxy"] == "http://proxy.corp:3128"
