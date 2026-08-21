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
