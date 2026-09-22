# Resolving a flat tool name back to a server, and an MCP answer back to rows.
# Both are where the bridge gets a name or a payload subtly wrong in silence.

from __future__ import annotations

import pytest

from core.agents.mcp_tools import rows_from, split_tool_name

pytestmark = pytest.mark.unit

SERVERS = [
    "splunk",
    "splunk-selfhosted",
    "gcp-secops",
    "security-detections",
    "virustotal",
]


class TestSplittingTheName:
    def test_finds_the_server_and_the_tool(self):
        assert split_tool_name("virustotal_lookup_ip", SERVERS) == (
            "virustotal",
            "lookup_ip",
        )

    # The case a naive split on "_" gets wrong: splunk-selfhosted_search starts
    # with neither a clean prefix nor one underscore, and splunk is a real server
    # whose name is a prefix of it.
    def test_prefers_the_longest_matching_server(self):
        assert split_tool_name("splunk-selfhosted_search", SERVERS) == (
            "splunk-selfhosted",
            "search",
        )

    def test_handles_a_server_whose_name_carries_a_hyphen(self):
        assert split_tool_name("gcp-secops_list_alerts", SERVERS) == (
            "gcp-secops",
            "list_alerts",
        )

    def test_reports_nothing_for_a_name_no_server_claims(self):
        assert split_tool_name("elastic_search", SERVERS) is None

    # A bare server name is not a tool call: there is nothing after the prefix.
    def test_refuses_a_server_name_with_no_tool(self):
        assert split_tool_name("splunk_", SERVERS) is None


class TestReadingTheAnswer:
    def test_parses_json_text_into_records(self):
        result = {"content": [{"type": "text", "text": '{"host": "10.0.0.5"}'}]}
        assert rows_from(result) == [{"host": "10.0.0.5"}]

    # A server answering with a JSON array means many rows, not one row that is
    # a list -- otherwise every result reads as a single record to the model.
    def test_flattens_a_json_array_into_rows(self):
        result = {"content": [{"type": "text", "text": '[{"n": 1}, {"n": 2}]'}]}
        assert rows_from(result) == [{"n": 1}, {"n": 2}]

    def test_keeps_prose_as_a_row_rather_than_dropping_it(self):
        result = {"content": [{"type": "text", "text": "no results in range"}]}
        assert rows_from(result) == [{"text": "no results in range"}]

    def test_reads_several_content_parts(self):
        result = {
            "content": [
                {"type": "text", "text": '{"a": 1}'},
                {"type": "text", "text": '{"b": 2}'},
            ]
        }
        assert rows_from(result) == [{"a": 1}, {"b": 2}]

    def test_falls_back_to_the_whole_answer_when_it_carries_no_content(self):
        assert rows_from({"total": 7}) == [{"total": 7}]


# The local indicator database, reachable as a tool. It needs no MCP server, so a
# deployment with no external intel still has something for an agent to ask.
class TestIndicatorLookup:
    def _lookup(self, monkeypatch, hits):
        import core.threat_intel.threat_feed_service as feed

        monkeypatch.setattr(feed, "lookup_indicators", lambda kind, values: hits)
        from core.agents.tool_registry import _INTEL_TOOLS

        return _INTEL_TOOLS["lookup_indicators"]

    def test_returns_what_the_feeds_know(self, monkeypatch):
        run = self._lookup(monkeypatch, {"1.2.3.4": {"threat_type": "c2"}})
        rows = run({"indicator_type": "ip", "values": ["1.2.3.4"]})

        assert rows == [
            {
                "indicator_type": "ip",
                "indicator_value": "1.2.3.4",
                "known": True,
                "threat_type": "c2",
            }
        ]

    # A miss is a row, not an omission: "no feed we carry knows this" is a real
    # answer, and dropping it would read as though the tool returned nothing.
    def test_reports_a_miss_rather_than_dropping_it(self, monkeypatch):
        run = self._lookup(monkeypatch, {})
        rows = run({"indicator_type": "ip", "values": ["10.0.0.5"]})

        assert rows == [
            {"indicator_type": "ip", "indicator_value": "10.0.0.5", "known": False}
        ]

    def test_accepts_a_single_value_as_well_as_a_batch(self, monkeypatch):
        run = self._lookup(monkeypatch, {})
        assert (
            run({"value": "evil.test", "indicator_type": "domain"})[0][
                "indicator_value"
            ]
            == "evil.test"
        )

    # invalid_args at the bridge rather than an empty answer: a call with nothing
    # to look up is a defect, and the router reads a TypeError as exactly that.
    def test_refuses_a_call_with_nothing_to_look_up(self, monkeypatch):
        run = self._lookup(monkeypatch, {})
        with pytest.raises(TypeError):
            run({"indicator_type": "ip"})


# The one branch nothing exercised: every router test monkeypatches
# execute_mcp_tool away, so the client import inside it was never run and a name
# that does not exist there read as a working dispatch until a real tool call.
class TestReachingTheClient:
    class _Registry:
        def get_active_servers(self):
            return ["splunk-selfhosted"]

        def get_tool_names(self):
            return ["splunk-selfhosted_splunk_nl_search"]

    @pytest.mark.asyncio
    async def test_names_the_accessor_the_client_module_actually_exports(
        self, monkeypatch
    ):
        import core.integrations.mcp.client as client
        from core.agents.mcp_tools import UNAVAILABLE, MCPFailure, execute_mcp_tool

        monkeypatch.setattr(client, "_process_client", None)
        with pytest.raises(MCPFailure) as raised:
            await execute_mcp_tool(
                "splunk-selfhosted_splunk_nl_search", {}, 5.0, self._Registry()
            )

        assert raised.value.kind == UNAVAILABLE


# --- Vigil's own tools carry no prefix --------------------------------------
#
# They are the same tools an external caller reaches at /mcp. A tool that
# answers to two names is two tools to anyone writing against it.


class TestVigilsOwnToolsAreUnprefixed:
    def test_a_bare_name_routes_to_vigil(self):
        from core.agents.mcp_tools import split_tool_name

        assert split_tool_name("list_findings", ["vigil", "crowdstrike"]) == (
            "vigil",
            "list_findings",
        )

    def test_a_vendor_prefix_still_wins_over_the_bare_fallback(self):
        from core.agents.mcp_tools import split_tool_name

        assert split_tool_name(
            "crowdstrike_isolate_host", ["vigil", "crowdstrike"]
        ) == (
            "crowdstrike",
            "isolate_host",
        )

    def test_a_bare_name_routes_nowhere_when_vigil_is_not_connected(self):
        from core.agents.mcp_tools import split_tool_name

        assert split_tool_name("list_findings", ["crowdstrike"]) is None


class TestTheDestructiveGateReadsBareNames:
    """It decides on the verb, and a bare name's verb is its first token."""

    def test_a_bare_destructive_tool_is_dropped(self):
        from core.llm.chat_layers import _is_destructive_mcp

        assert _is_destructive_mcp("isolate_host") is True
        assert _is_destructive_mcp("block_ip") is True

    def test_a_prefixed_destructive_tool_is_still_dropped(self):
        from core.llm.chat_layers import _is_destructive_mcp

        assert _is_destructive_mcp("crowdstrike_isolate_host") is True

    def test_vigils_own_read_tools_are_not_dropped(self):
        from core.llm.chat_layers import _is_destructive_mcp

        for name in ("list_findings", "get_finding", "list_cases", "close_case"):
            assert _is_destructive_mcp(name) is False, name

    def test_the_named_execute_id_is_still_dropped(self):
        from core.llm.chat_layers import EXECUTE_IDS, _is_destructive_mcp

        for name in EXECUTE_IDS:
            assert _is_destructive_mcp(name) is True, name



# --- A tool both sides carry is described by the side that answers it --------
#
# Vigil's own tools are registered unprefixed, so thirteen of their names are
# also backend tool names. tools_router tries the backend first and only reaches
# an MCP server for a name the backend does not claim, so the backend is what
# answers all thirteen -- and the declaration a model reads has to be that one's.
#
# Building the catalogue the other way around declared the MCP tool's schema
# against the backend's implementation, and silently dropped six tools: those
# MCP tools carry no docstring, so their description was empty, and a tool with
# no description is not offered at all.


def _offered(mcp_tools):
    from core.llm.chat_layers import _declare

    return {tool["id"] for tool in _declare(None, mcp_tools)}


def test_registering_vigils_own_tools_takes_none_of_the_backend_tools_away():
    from core.integrations.mcp import in_process

    alone = _offered(None)
    together = _offered(in_process.list_tools())

    missing = sorted(alone - together)
    assert not missing, (
        f"chat stopped offering {missing} once Vigil's own MCP tools were "
        "registered. They share a name with a backend tool, and the MCP one "
        "has no docstring, so an empty description won the catalogue and the "
        "tool was dropped."
    )


def test_a_shared_name_is_declared_with_the_schema_that_will_run():
    """The backend answers every name it claims, so its schema is the honest one."""
    from core.agents.tool_registry import MANIFEST
    from core.integrations.mcp import in_process
    from core.llm.chat_layers import _declare

    mcp_tools = in_process.list_tools()
    shared = {t["name"] for t in mcp_tools} & set(MANIFEST)
    assert shared, "nothing shares a name; this test has stopped testing anything"

    declared = {t["id"]: t for t in _declare(None, mcp_tools)}
    for name in shared:
        if name not in declared:
            continue
        assert declared[name]["parameters"] == (
            MANIFEST[name].get("input_schema") or {"type": "object"}
        ), f"{name} is declared with a schema the backend implementation will not honour"
