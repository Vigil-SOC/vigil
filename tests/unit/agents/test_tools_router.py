import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents import internal_auth, tools_router
from core.agents.mcp_tools import MCPFailure
from core.integrations.mcp.registry import MCPRegistry

BOUNDS = {"max_rows": 2, "timeout_ms": 500}
AUTH = {"Authorization": "Bearer shhh"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "shhh")
    app = FastAPI()
    # The provider reads the instance the owner put on app.state (#659); a bare
    # app has none, and every request would fail before reaching the gate.
    app.state.mcp_registry = MCPRegistry()
    app.include_router(tools_router.router, prefix=tools_router.ROUTER_META.prefix)
    # No `client=` address: nothing reads the peer since ADR 0014.
    return TestClient(app)


def _answers(monkeypatch, result, handled=True):
    async def fake(name, args, **kwargs):
        return result, handled

    monkeypatch.setattr(tools_router, "execute_backend_tool", fake)


def _raises(monkeypatch, error):
    async def fake(name, args, **kwargs):
        raise error

    monkeypatch.setattr(tools_router, "execute_backend_tool", fake)


def _invoke(client, tool="list_findings", args=None, bounds=None, headers=AUTH):
    body = {"tool": tool, "args": args or {}, "bounds": bounds or BOUNDS}
    return client.post("/internal/tools/invoke", json=body, headers=headers)


class TestAuthorisation:
    def test_refuses_without_a_token(self, client):
        assert _invoke(client, headers={}).status_code == 401

    def test_refuses_a_wrong_token(self, client):
        assert (
            _invoke(client, headers={"Authorization": "Bearer nope"}).status_code == 401
        )

    # A deployment that never set the token must not read as a bad caller.
    def test_says_so_when_no_secret_is_configured(self, client, monkeypatch):
        monkeypatch.setattr(internal_auth, "get_secret", lambda name: None)
        response = _invoke(client)
        assert response.status_code == 503
        assert "AGENT_INTERNAL_TOKEN" in response.json()["detail"]

    def test_a_valid_bearer_gets_through(self, client, monkeypatch):
        """No longer loopback-gated: ADR 0014 left the token as the only check,
        because the agent layer now calls in from its own pods."""
        _answers(monkeypatch, [])
        assert _invoke(client).status_code == 200


class TestBoundsAtTheSource:
    def test_caps_rows_and_says_it_capped(self, client, monkeypatch):
        _answers(monkeypatch, [{"id": 1}, {"id": 2}, {"id": 3}])
        body = _invoke(client).json()
        assert body["rows"] == [{"id": 1}, {"id": 2}]
        assert body["rowCount"] == 2
        assert body["capped"] is True

    def test_does_not_claim_capped_when_it_fitted(self, client, monkeypatch):
        _answers(monkeypatch, [{"id": 1}])
        body = _invoke(client).json()
        assert body["rowCount"] == 1
        assert body["capped"] is False

    def test_a_single_mapping_is_one_row_not_none(self, client, monkeypatch):
        _answers(monkeypatch, {"total": 7})
        body = _invoke(client).json()
        assert body["rows"] == [{"total": 7}]
        assert body["rowCount"] == 1

    # An MCP server answers an envelope. Reading the whole object as one row made
    # rowCount 1 for every call however much came back, so max_rows never bit, capped
    # was never true, and the console's row counts meant nothing.
    def test_reads_the_rows_out_of_an_envelope_rather_than_counting_it_as_one(
        self, client, monkeypatch
    ):
        _answers(
            monkeypatch,
            {"success": True, "query": "q", "count": 3, "results": [1, 2, 3]},
        )
        body = _invoke(client).json()
        assert body["rowCount"] == 2
        assert body["capped"] is True

    # And an envelope holding nothing is nothing, not one row of "success": true. A
    # dispatch salvages on rowCount, so this decided whether an empty answer read as
    # gathered data.
    def test_an_empty_envelope_is_no_rows(self, client, monkeypatch):
        _answers(monkeypatch, {"success": True, "count": 0, "results": []})
        body = _invoke(client).json()
        assert body["rowCount"] == 0

    def test_a_tool_over_its_timeout_reports_timeout(self, client, monkeypatch):
        async def slow(name, args, **kwargs):
            await asyncio.sleep(1)
            return [], True

        monkeypatch.setattr(tools_router, "execute_backend_tool", slow)
        body = _invoke(client, bounds={"max_rows": 2, "timeout_ms": 20}).json()
        assert body == {"ok": False, "failure": {"kind": "timeout", "timeoutMs": 20}}


class TestFailureKinds:
    def test_an_unknown_tool_is_a_defect_not_a_gap(self, client, monkeypatch):
        _answers(monkeypatch, None, handled=False)
        body = _invoke(client, tool="no_such_tool").json()
        assert body["failure"]["kind"] == "refused"

    # backend_error, not refused: the tool ran and could not answer, which is a
    # different thing from a name nothing implements.
    def test_an_in_band_error_is_a_backend_error(self, client, monkeypatch):
        _answers(monkeypatch, {"error": "the index is rebuilding"})
        assert _invoke(client).json()["failure"]["kind"] == "backend_error"

    def test_bad_arguments_are_invalid_args(self, client, monkeypatch):
        _raises(monkeypatch, TypeError("unexpected keyword argument 'nope'"))
        assert _invoke(client).json()["failure"]["kind"] == "invalid_args"

    # A TypeError from inside a tool is not a bad call. Reported as invalid_args it
    # tells the model to retry with different arguments, which it does until the cap.
    def test_a_typeerror_from_inside_the_tool_is_a_backend_error(
        self, client, monkeypatch
    ):
        _raises(
            monkeypatch, TypeError("unsupported operand type(s) for +: 'int' and 'str'")
        )
        assert _invoke(client).json()["failure"]["kind"] == "backend_error"

    def test_anything_else_is_a_backend_error(self, client, monkeypatch):
        _raises(monkeypatch, RuntimeError("the database went away"))
        assert _invoke(client).json()["failure"]["kind"] == "backend_error"

    def test_rejects_bounds_that_are_not_positive(self, client):
        assert (
            _invoke(client, bounds={"max_rows": 0, "timeout_ms": 500}).status_code
            == 422
        )


class TestBoundsReachTheTool:
    # Sliced after the fact the cap never reaches the source: the tool still fetches
    # everything and is billed for it. Anything paging on limit gets it up front.
    def test_the_row_cap_is_pushed_into_the_call(self, client, monkeypatch):
        seen: dict = {}

        async def _capture(tool, args):
            seen.update(args)
            return [], True

        monkeypatch.setattr("core.agents.tools_router.execute_backend_tool", _capture)
        _invoke(client, args={"query": "x"})
        assert seen["limit"] == BOUNDS["max_rows"]

    def test_a_tighter_limit_the_caller_asked_for_is_left_alone(
        self, client, monkeypatch
    ):
        seen: dict = {}

        async def _capture(tool, args):
            seen.update(args)
            return [], True

        monkeypatch.setattr("core.agents.tools_router.execute_backend_tool", _capture)
        _invoke(client, args={"limit": 1})
        assert seen["limit"] == 1

    # splunk_execute pages on max_results. Only `limit` was ever set, so the cap
    # reached the backend tools and never the MCP servers -- which are the ones that
    # answer in bulk.
    def test_the_cap_finds_the_name_the_tool_pages_on(self, client, monkeypatch):
        seen: dict = {}

        async def _capture(tool, args):
            seen.update(args)
            return [], True

        monkeypatch.setattr("core.agents.tools_router.execute_backend_tool", _capture)
        _invoke(client, args={"spl_query": "index=botsv3", "max_results": 5000})
        assert seen["max_results"] == BOUNDS["max_rows"]

    # A name the tool's signature does not take comes back as invalid_args, so the cap
    # lowers what the call already carries rather than teaching it a new keyword.
    def test_does_not_hand_a_tool_a_page_size_it_never_asked_for(
        self, client, monkeypatch
    ):
        seen: dict = {}

        async def _capture(tool, args):
            seen.update(args)
            return [], True

        monkeypatch.setattr("core.agents.tools_router.execute_backend_tool", _capture)
        _invoke(client, args={"spl_query": "index=botsv3", "max_results": 1})
        assert seen["max_results"] == 1
        assert "max_count" not in seen


class TestWhichSystemAnswered:
    """sourceSystem is what a hunt counts corroboration over. Answering "vigil" for
    everything left the only real domain label a string the worker typed into its own
    emission, and one worker querying one system twice can type two."""

    def test_a_tool_this_backend_implements_is_vigil(self, client, monkeypatch):
        _answers(monkeypatch, [])
        assert _invoke(client).json()["sourceSystem"] == "vigil"

    def test_an_mcp_server_answers_under_its_own_name(self, client, monkeypatch):
        async def _no_backend_tool(name, args, **kwargs):
            return None, False

        async def _served(name, args, seconds, registry):
            return [{"host": "we8105desk"}], True

        monkeypatch.setattr(tools_router, "execute_backend_tool", _no_backend_tool)
        monkeypatch.setattr(tools_router, "execute_mcp_tool", _served)
        monkeypatch.setattr(
            MCPRegistry, "get_active_servers", lambda self: ["splunk-selfhosted"]
        )

        body = _invoke(client, tool="splunk-selfhosted_splunk_execute").json()
        assert body["sourceSystem"] == "splunk-selfhosted"


def _no_backend(monkeypatch):
    async def fake(name, args, **kwargs):
        return None, False

    monkeypatch.setattr(tools_router, "execute_backend_tool", fake)


def _mcp(monkeypatch, result=None, handled=True, error=None):
    async def fake(name, args, timeout_s, registry):
        if error is not None:
            raise error
        return result, handled

    monkeypatch.setattr(tools_router, "execute_mcp_tool", fake)


# The bridge reached 23 backend tools and none of the 40 configured MCP servers,
# so every integration the deployment carries was unreachable from an agent.
class TestMCPFallthrough:
    def test_serves_a_tool_no_backend_implements(self, client, monkeypatch):
        _no_backend(monkeypatch)
        _mcp(monkeypatch, [{"indicator": "1.2.3.4", "verdict": "malicious"}])

        body = _invoke(client, tool="virustotal_lookup_ip").json()
        assert body["ok"] is True
        assert body["rows"][0]["verdict"] == "malicious"

    # A backend tool must not reach MCP: the near side answers or nothing does.
    def test_does_not_reach_mcp_when_the_backend_handled_it(self, client, monkeypatch):
        _answers(monkeypatch, [{"finding_id": "f-1"}])

        def _boom(name, args, timeout_s, registry):
            raise AssertionError("MCP was called for a backend tool")

        monkeypatch.setattr(tools_router, "execute_mcp_tool", _boom)
        assert _invoke(client).status_code == 200

    # refused, not unavailable: a name nothing implements is a defect in the call,
    # and the hunt records a visibility gap only for the other kind.
    def test_a_name_nobody_carries_is_still_refused(self, client, monkeypatch):
        _no_backend(monkeypatch)
        _mcp(monkeypatch, None, handled=False)

        body = _invoke(client, tool="no_such_tool").json()
        assert body["failure"]["kind"] == "refused"

    def test_a_server_that_cannot_be_reached_is_a_visibility_gap(
        self, client, monkeypatch
    ):
        _no_backend(monkeypatch)
        _mcp(monkeypatch, error=MCPFailure("unavailable", "Unknown server: splunk"))

        body = _invoke(client, tool="splunk_search").json()
        assert body["failure"]["kind"] == "unavailable"

    def test_a_slow_server_reports_the_bound_it_broke(self, client, monkeypatch):
        _no_backend(monkeypatch)
        _mcp(
            monkeypatch,
            error=MCPFailure("timeout", "Tool call timed out after 0.5 seconds"),
        )

        body = _invoke(client, tool="splunk_search").json()
        assert body["failure"] == {"kind": "timeout", "timeoutMs": BOUNDS["timeout_ms"]}

    def test_a_server_that_answered_badly_is_a_defect_not_a_gap(
        self, client, monkeypatch
    ):
        _no_backend(monkeypatch)
        _mcp(
            monkeypatch,
            error=MCPFailure("backend_error", "Error: index does not exist"),
        )

        assert (
            _invoke(client, tool="splunk_search").json()["failure"]["kind"]
            == "backend_error"
        )

    # The bound is the bridge's, not the near side's: an MCP tool is capped the
    # same way a backend one is.
    def test_caps_rows_from_an_mcp_server(self, client, monkeypatch):
        _no_backend(monkeypatch)
        _mcp(monkeypatch, [{"n": 1}, {"n": 2}, {"n": 3}])

        body = _invoke(client, tool="splunk_search").json()
        assert body["rowCount"] == BOUNDS["max_rows"]
        assert body["capped"] is True
