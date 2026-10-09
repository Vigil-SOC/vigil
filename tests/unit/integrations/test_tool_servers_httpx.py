"""The MCP tool servers on httpx: every call site, then the wire.

Two halves:

1. A source scan over ``core/integrations/*/tool.py``: no ``requests``
   import, and every ``httpx`` call states its own ``timeout``. Nothing
   else reaches all 39 call sites, and httpx's 5s default is short enough
   to break a sandbox report or a SIEM search.
2. Round trips through each server's dispatcher over a respx-mocked
   transport: ``raise_for_status()`` failures landing in the handler that
   reports a status code while transport failures stay in the generic one,
   the configured TLS policy forwarded, and ``files=`` / ``data=`` /
   ``params=`` encoding unchanged.

``tests/unit/_ratchets/test_no_tls_verify_disabled.py`` owns the separate
rule that no server may hardcode ``verify=False``.
"""

from __future__ import annotations

import ast
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.integrations.alienvault_otx.tool as otx  # noqa: E402
import core.integrations.anyrun.tool as anyrun  # noqa: E402
import core.integrations.azure_ad.tool as aad  # noqa: E402
import core.integrations.cape_sandbox.tool as cape  # noqa: E402
import core.integrations.carbon_black.tool as cbc  # noqa: E402
import core.integrations.hybrid_analysis.tool as ha  # noqa: E402
import core.integrations.ip_geolocation.tool as geo  # noqa: E402
import core.integrations.microsoft_defender.tool as mde_tool  # noqa: E402
import core.integrations.microsoft_teams.tool as teams  # noqa: E402
import core.integrations.misp.tool as misp  # noqa: E402
import core.integrations.palo_alto.tool as pan  # noqa: E402
import core.integrations.slack.tool as slack_tool  # noqa: E402
from core.integrations.cloudflare import tool as cf  # noqa: E402

pytestmark = pytest.mark.unit


def _body(out) -> dict:
    """Unwrap the JSON an MCP tool server returns as TextContent."""
    return json.loads(out[0].text)


def _stub_config(monkeypatch, module, config: dict) -> None:
    """Stand in for the descriptor lookup every server reads its config through.

    ``resolve()`` returns every declared field, so a server's ``missing()``
    guard only passes when the stub carries each name it requires.
    """
    monkeypatch.setattr(module, "resolve", lambda *a, **kw: config)


# --------------------------------------------------------------------- #
# Every call site — source scan
# --------------------------------------------------------------------- #

# The httpx module-level request helpers. A tool server that grows an
# httpx.Client would set its timeout once at construction instead, and
# needs its own check.
HTTP_VERBS = {
    "request",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
}

REQUIRED_KWARGS = ("timeout",)

# A floor, not a count: it fails loudly if the matcher below stops
# recognising call sites and starts passing every file vacuously.
MIN_TOTAL_CALL_SITES = 30


def _tool_servers() -> list[Path]:
    """Every in-repo MCP server. `mcp-config.json` spawns each of these."""
    return sorted((ROOT / "core" / "integrations").glob("*/tool.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _httpx_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``httpx.<verb>(...)`` call in a module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr in HTTP_VERBS
    ]


SERVERS = _tool_servers()
SERVER_IDS = [str(p.relative_to(ROOT)) for p in SERVERS]


def test_tool_servers_are_discovered():
    assert SERVERS, "no MCP tool servers found — has the layout moved?"


@pytest.mark.parametrize("path", SERVERS, ids=SERVER_IDS)
def test_tool_server_does_not_import_requests(path: Path):
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            assert all(
                alias.name.split(".")[0] != "requests" for alias in node.names
            ), f"{path.name}:{node.lineno} imports requests"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root != "requests", f"{path.name}:{node.lineno} imports requests"


@pytest.mark.parametrize("path", SERVERS, ids=SERVER_IDS)
def test_every_httpx_call_states_its_own_timeout(path: Path):
    for call in _httpx_calls(_parse(path)):
        passed = {kw.arg for kw in call.keywords}
        # **kwargs forwarding shows up as arg=None; a wrapper may pass the
        # timeout through, so only reject a literal call that omits it.
        if None in passed:
            continue
        missing = [kw for kw in REQUIRED_KWARGS if kw not in passed]
        assert not missing, (
            f"{path.name}:{call.lineno} httpx.{call.func.attr}() omits "
            f"{', '.join(missing)} — httpx would fall back to 5s"
        )


def test_the_scan_actually_sees_the_call_sites():
    total = sum(len(_httpx_calls(_parse(path))) for path in SERVERS)
    assert total >= MIN_TOTAL_CALL_SITES, (
        f"only {total} httpx call sites found across {len(SERVERS)} tool "
        "servers; the matcher above has probably stopped matching"
    )


# --------------------------------------------------------------------- #
# Redirects — the endpoints are fixed, so a 3xx means a misconfiguration
# --------------------------------------------------------------------- #

OTX = "https://otx.alienvault.com/api/v1"


@respx.mock
async def test_otx_surfaces_a_redirect_rather_than_chasing_it(monkeypatch):
    """These are fixed API endpoints; a 3xx is a wrong base URL, not a hop.

    httpx doesn't follow redirects and raise_for_status() rejects a 3xx, so
    the tool reports the status instead of silently retrying elsewhere.
    """
    _stub_config(monkeypatch, otx, {"api_key": "k"})
    elsewhere = respx.get(f"{OTX}/indicators/IPv4/1.2.3.4/general/").mock(
        return_value=httpx.Response(200, json={"pulse_info": {"count": 3}})
    )
    respx.get(f"{OTX}/indicators/IPv4/1.2.3.4/general").mock(
        return_value=httpx.Response(
            301, headers={"Location": f"{OTX}/indicators/IPv4/1.2.3.4/general/"}
        )
    )

    body = _body(await otx.handle_call_tool("otx_check_ip", {"ip": "1.2.3.4"}))
    assert body == {"error": "API error", "status": 301}
    assert not elsewhere.called


# --------------------------------------------------------------------- #
# Error contracts — HTTPStatusError is the twin of requests.HTTPError
# --------------------------------------------------------------------- #


@respx.mock
async def test_otx_status_error_reports_the_status_code(monkeypatch):
    """raise_for_status() must still reach the handler that reads .response."""
    _stub_config(monkeypatch, otx, {"api_key": "k"})
    respx.get(f"{OTX}/indicators/domain/bad.example/general").mock(
        return_value=httpx.Response(503)
    )

    body = _body(
        await otx.handle_call_tool("otx_check_domain", {"domain": "bad.example"})
    )
    assert body == {"error": "API error", "status": 503}


@respx.mock
async def test_otx_transport_error_keeps_the_generic_handler(monkeypatch):
    """httpx.HTTPError would have widened the catch to transport failures.

    requests.HTTPError covered raise_for_status() only, so a refused
    connection reported its own message — it must keep doing that.
    """
    _stub_config(monkeypatch, otx, {"api_key": "k"})
    respx.get(f"{OTX}/indicators/file/{'a' * 64}/general").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    body = _body(await otx.handle_call_tool("otx_check_hash", {"hash": "a" * 64}))
    assert body["error"] != "API error"
    assert "status" not in body


@respx.mock
async def test_cape_status_error_reports_the_status_code(monkeypatch):
    monkeypatch.setattr(
        cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
    )
    respx.get("http://cape.test/apiv2/tasks/status/404/").mock(
        return_value=httpx.Response(404, json={"error": "no such task"})
    )

    body = _body(await cape.handle_call_tool("cape_task_status", {"task_id": "404"}))
    assert body["status_code"] == 404
    assert "CAPE HTTP error" in body["error"]


@respx.mock
async def test_cape_transport_error_keeps_the_generic_handler(monkeypatch):
    monkeypatch.setattr(
        cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
    )
    respx.get("http://cape.test/apiv2/tasks/list/20/0/").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    body = _body(await cape.handle_call_tool("cape_list_tasks", {}))
    assert body.get("status_code") is None
    assert "timed out" in body["error"]


# --------------------------------------------------------------------- #
# TLS verification — respx can't see it, so capture the kwargs instead
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "configured, expected",
    [({}, True), ({"verify_ssl": True}, True), ({"verify_ssl": False}, False)],
)
async def test_misp_forwards_the_configured_tls_policy(
    monkeypatch, configured, expected
):
    """verify_ssl is per-instance: httpx must get the value, not a literal."""
    _stub_config(
        monkeypatch, misp, {"api_key": "k", "url": "https://misp.test", **configured}
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200,
            json={"response": {"Attribute": []}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(misp.httpx, "post", fake_post)

    await misp.handle_call_tool("misp_search_ioc", {"value": "1.2.3.4"})
    assert seen["verify"] is expected
    assert seen["timeout"] == 30


@pytest.mark.parametrize(
    "configured, expected", [({}, True), ({"verify_ssl": False}, False)]
)
async def test_palo_alto_forwards_the_configured_tls_policy(
    monkeypatch, configured, expected
):
    _stub_config(
        monkeypatch, pan, {"hostname": "pan.test", "api_key": "k", **configured}
    )
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200, text="<response/>", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(pan.httpx, "get", fake_get)

    body = _body(
        await pan.handle_call_tool("pan_block_ip", {"ip": "1.2.3.4", "reason": "c2"})
    )
    assert body["success"] is True
    assert seen["verify"] is expected
    assert seen["timeout"] == 30


@respx.mock
async def test_palo_alto_xml_api_params_survive_encoding(monkeypatch):
    """The xpath/element payload is full of <, >, ' and [] — httpx must quote it."""
    _stub_config(
        monkeypatch, pan, {"hostname": "pan.test", "api_key": "k", "verify_ssl": True}
    )
    route = respx.get(url__startswith="https://pan.test/api/").mock(
        return_value=httpx.Response(200, text="<response status='success'/>")
    )

    body = _body(
        await pan.handle_call_tool("pan_block_ip", {"ip": "1.2.3.4", "reason": "c2"})
    )
    assert body["success"] is True
    params = route.calls.last.request.url.params
    assert params["xpath"].endswith("address/entry[@name='blocked-1.2.3.4']")
    assert params["element"] == (
        "<ip-netmask>1.2.3.4/32</ip-netmask><description>Blocked: c2</description>"
    )
    # …and the wire form is quoted, not raw
    assert "<" not in str(route.calls.last.request.url)


@respx.mock
async def test_palo_alto_block_carries_the_status_code(monkeypatch):
    """A 3xx means the configured hostname is wrong, and nothing follows it.

    pan_block_ip reads status_code instead of raising, so without the code in
    the payload an operator sees a bare `success: false` for a containment
    action and has nothing to debug from.
    """
    _stub_config(
        monkeypatch, pan, {"hostname": "pan.test", "api_key": "k", "verify_ssl": True}
    )
    respx.get(url__startswith="https://pan.test/api/").mock(
        return_value=httpx.Response(302, headers={"Location": "https://pan.test/php/"})
    )

    body = _body(
        await pan.handle_call_tool("pan_block_ip", {"ip": "1.2.3.4", "reason": "c2"})
    )
    assert body["success"] is False
    assert body["status_code"] == 302


# --------------------------------------------------------------------- #
# Null-valued params — requests dropped them, httpx sends `key=`
# --------------------------------------------------------------------- #


@respx.mock
async def test_azure_ad_sign_ins_drops_a_null_limit(monkeypatch):
    """A tool call may carry "limit": null; Graph 400s on a bare `$top=`."""
    _stub_config(
        monkeypatch,
        aad,
        {"tenant_id": "tid", "client_id": "cid", "client_secret": "sec"},
    )
    respx.post("https://login.microsoftonline.com/tid/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at"})
    )
    route = respx.get("https://graph.microsoft.com/v1.0/auditLogs/signIns").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    await aad.handle_call_tool("aad_get_sign_ins", {"limit": None})
    assert route.calls.last.request.url.params["$top"] == "20"


@respx.mock
async def test_palo_alto_threats_drops_a_null_limit(monkeypatch):
    _stub_config(
        monkeypatch, pan, {"hostname": "pan.test", "api_key": "k", "verify_ssl": True}
    )
    route = respx.get(url__startswith="https://pan.test/api/").mock(
        return_value=httpx.Response(200, text="<response/>")
    )

    await pan.handle_call_tool("pan_get_threats", {"limit": None})
    assert route.calls.last.request.url.params["nlogs"] == "20"


# --------------------------------------------------------------------- #
# Request encoding — files=, data=, params=, json=
# --------------------------------------------------------------------- #


@respx.mock
async def test_cape_submit_file_refuses_a_local_path(monkeypatch, tmp_path):
    """A model-supplied path is never opened: the tool takes content, not paths."""
    monkeypatch.setattr(
        cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
    )
    sample = tmp_path / "secret.txt"
    sample.write_bytes(b"do not upload")
    route = respx.post("http://cape.test/apiv2/tasks/create/file/").mock(
        return_value=httpx.Response(200, json={"task_ids": [11]})
    )

    body = _body(
        await cape.handle_call_tool("cape_submit_file", {"file_path": str(sample)})
    )
    assert "error" in body
    assert not route.called
    schema = next(
        t for t in await cape.handle_list_tools() if t.name == "cape_submit_file"
    ).input_schema
    assert "file_path" not in schema["properties"]


@respx.mock
async def test_cape_submit_file_b64_sends_multipart(monkeypatch):
    """httpx builds multipart from files= the same way requests did."""
    monkeypatch.setattr(
        cape, "_load_config", lambda: {"url": "http://cape.test", "api_key": "k"}
    )
    seen = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["content"] = request.content
        return httpx.Response(200, json={"task_ids": [7]})

    respx.post("http://cape.test/apiv2/tasks/create/file/").mock(side_effect=_capture)

    body = _body(
        await cape.handle_call_tool(
            "cape_submit_file",
            {
                "file_b64": base64.b64encode(b"MZ\x90payload").decode(),
                "filename": "sample.exe",
                "timeout": 120,
            },
        )
    )
    assert body["task_ids"] == [7]
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'filename="sample.exe"' in seen["content"]
    assert b"MZ\x90payload" in seen["content"]
    # data= fields ride along in the same body
    assert b"120" in seen["content"]


@respx.mock
async def test_hybrid_analysis_form_encodes_the_hash(monkeypatch):
    _stub_config(monkeypatch, ha, {"api_key": "k"})
    route = respx.post("https://www.hybrid-analysis.com/api/v2/search/hash").mock(
        return_value=httpx.Response(200, json=[{"job_id": "j1"}])
    )

    body = _body(await ha.handle_call_tool("ha_search_hash", {"hash": "b" * 64}))
    assert body["found"] is True
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.content == f"hash={'b' * 64}".encode()


@respx.mock
async def test_anyrun_sends_the_hash_as_a_query_param(monkeypatch):
    _stub_config(monkeypatch, anyrun, {"api_key": "k"})
    route = respx.get("https://api.any.run/v1/tasks", params={"hash": "c" * 64}).mock(
        return_value=httpx.Response(200, json={"data": {"tasks": [{"id": 1}]}})
    )

    body = _body(
        await anyrun.handle_call_tool("anyrun_search_hash", {"hash": "c" * 64})
    )
    assert body["found"] is True
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "API-Key k"


@respx.mock
async def test_teams_posts_the_message_card_and_reports_a_bad_status(monkeypatch):
    """This one reads resp.status_code directly instead of raising."""
    _stub_config(monkeypatch, teams, {"webhook_url": "https://hook.test/a"})
    route = respx.post("https://hook.test/a").mock(
        return_value=httpx.Response(200, text="1")
    )

    args = {"title": "t", "message": "m", "severity": "high"}
    body = _body(await teams.handle_call_tool("teams_send_alert", args))
    assert body == {"success": True, "title": "t"}
    card = json.loads(route.calls.last.request.content)
    assert card["summary"] == "t"
    assert card["themeColor"] == "FFA500"

    route.mock(return_value=httpx.Response(429, text="slow down"))
    body = _body(await teams.handle_call_tool("teams_send_alert", args))
    assert body == {"error": "HTTP 429"}


@respx.mock
async def test_carbon_black_posts_the_search_payload(monkeypatch):
    _stub_config(
        monkeypatch,
        cbc,
        {
            "url": "https://cbc.test",
            "api_key": "sec",
            "api_id": "id",
            "org_key": "ORG",
        },
    )
    route = respx.post("https://cbc.test/appservices/v6/orgs/ORG/devices/_search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1}]})
    )

    body = _body(await cbc.handle_call_tool("cb_search_device", {"ip": "10.0.0.1"}))
    assert body["count"] == 1
    request = route.calls.last.request
    assert json.loads(request.content)["query"] == "device_external_ip:10.0.0.1"
    assert request.headers["X-Auth-Token"] == "sec/id"


@respx.mock
async def test_azure_ad_exchanges_a_form_encoded_token_then_reads_a_user(monkeypatch):
    _stub_config(
        monkeypatch,
        aad,
        {"tenant_id": "tid", "client_id": "cid", "client_secret": "sec"},
    )
    token = respx.post("https://login.microsoftonline.com/tid/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at"})
    )
    respx.get("https://graph.microsoft.com/v1.0/users/a@b.test").mock(
        return_value=httpx.Response(200, json={"id": "u1", "accountEnabled": True})
    )

    body = _body(await aad.handle_call_tool("aad_get_user", {"user": "a@b.test"}))
    assert body == {
        "found": True,
        "id": "u1",
        "displayName": None,
        "accountEnabled": True,
        "mail": None,
    }
    assert b"grant_type=client_credentials" in token.calls.last.request.content


@respx.mock
async def test_azure_ad_reports_a_missing_user_without_raising(monkeypatch):
    """The 404 branch is checked before raise_for_status() — keep it that way."""
    _stub_config(
        monkeypatch,
        aad,
        {"tenant_id": "tid", "client_id": "cid", "client_secret": "sec"},
    )
    respx.post("https://login.microsoftonline.com/tid/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at"})
    )
    respx.get("https://graph.microsoft.com/v1.0/users/ghost@b.test").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NotFound"}})
    )

    body = _body(await aad.handle_call_tool("aad_get_user", {"user": "ghost@b.test"}))
    assert body == {"user": "ghost@b.test", "found": False}


@respx.mock
async def test_defender_isolate_posts_the_isolation_payload(monkeypatch):
    _stub_config(
        monkeypatch,
        mde_tool,
        {"tenant_id": "tid", "client_id": "cid", "client_secret": "sec"},
    )
    respx.post("https://login.microsoftonline.com/tid/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at"})
    )
    route = respx.post(
        "https://api.securitycenter.microsoft.com/api/machines/m1/isolate"
    ).mock(return_value=httpx.Response(201, json={"id": "action-1"}))

    body = _body(
        await mde_tool.handle_call_tool(
            "mde_isolate", {"machine_id": "m1", "comment": "ransomware"}
        )
    )
    assert body == {"success": True, "machine_id": "m1", "action": "isolated"}
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"Comment": "ransomware", "IsolationType": "Full"}


@respx.mock
async def test_slack_reports_the_api_level_error(monkeypatch):
    """Slack answers 200 with ok:false — the body, not the status, decides."""
    _stub_config(monkeypatch, slack_tool, {"bot_token": "xoxb"})
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(
            200, json={"ok": False, "error": "channel_not_found"}
        )
    )

    body = _body(
        await slack_tool.handle_call_tool(
            "slack_send_alert", {"channel": "#soc", "message": "m"}
        )
    )
    assert body == {"error": "channel_not_found"}


@respx.mock
async def test_ip_geolocation_maps_a_successful_lookup(monkeypatch):
    respx.get("http://ip-api.com/json/8.8.8.8").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "country": "United States", "isp": "Google"},
        )
    )

    body = _body(await geo.handle_call_tool("geolocate_ip", {"ip": "8.8.8.8"}))
    assert body["country"] == "United States"
    assert body["isp"] == "Google"


@respx.mock
async def test_cloudflare_waf_block_ip_round_trips(monkeypatch):
    """The REST helpers are shared with the approved-action executor."""
    route = respx.post(
        f"{cf.CF_API_BASE}/accounts/acct/firewall/access_rules/rules"
    ).mock(
        return_value=httpx.Response(
            201, json={"success": True, "result": {"id": "rule-9"}}
        )
    )

    out = cf._waf_block_ip(
        api_token="tok", account_id="acct", ip="9.9.9.9", reason="c2"
    )
    assert out["success"] is True
    assert out["rule_id"] == "rule-9"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_cloudflare_waf_unblock_ip_handles_an_empty_204(monkeypatch):
    """The helpers guard resp.json() with `if resp.content` — httpx keeps that
    honest on a 204, where Cloudflare sends no body at all."""
    respx.delete(
        f"{cf.CF_API_BASE}/accounts/acct/firewall/access_rules/rules/rule-9"
    ).mock(return_value=httpx.Response(204))

    out = cf._waf_unblock_ip(api_token="tok", account_id="acct", rule_id="rule-9")
    assert out == {
        "success": True,
        "status_code": 204,
        "rule_id": "rule-9",
        "errors": None,
    }
