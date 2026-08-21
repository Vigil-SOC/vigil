"""What the Splunk MCP tool tells a model about the deployment it is querying.

A description reading only "Execute SPL query" leaves an index and a time range to
be guessed, and a wrong guess comes back empty rather than wrong -- which reads as
"no evidence" and is really "no visibility". One hunt spent three iterations that
way against a 2018 dataset it kept querying with the -24h default.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(monkeypatch, rows, service=object()):
    """Fresh module per test: the summary is cached for the process's lifetime."""
    spec = importlib.util.spec_from_file_location(
        "splunk_tool_under_test", ROOT / "core/integrations/splunk/tool.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Service:
        def search(self, query, earliest_time="-24h", max_count=1000):
            if rows is None:
                raise RuntimeError("splunk is unreachable")
            return rows

    monkeypatch.setattr(
        module, "get_splunk_service", lambda: (None if service is None else _Service())
    )
    return module


def _describe(module):
    tools = asyncio.run(module.handle_list_tools())
    return {tool.name: tool.description for tool in tools}


_ROWS = [
    {
        "index": "botsv3",
        "sourcetype": "stream:dns",
        "count": "218456",
        "earliest": "1534723200",
        "latest": "1534809600",
    },
    {
        "index": "botsv3",
        "sourcetype": "cisco:asa",
        "count": "80192",
        "earliest": "1534723200",
        "latest": "1534809600",
    },
]


def test_names_every_index_and_sourcetype_it_can_see(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_execute"]

    assert "index=botsv3" in described
    assert "stream:dns:~218k" in described
    assert "cisco:asa" in described


# tstats reads the index rather than the events, so it is right about which
# sourcetype is large and wrong about how large. A number stated exactly is a
# number that gets believed.
def test_marks_the_counts_approximate_rather_than_stating_them_exactly(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_execute"]

    assert "counts approximate" in described
    assert "218456" not in described


# One line per index rather than per sourcetype: the span was the same date repeated
# once per row, and the description is charged on every call the tool is offered on.
def test_states_the_span_once_per_index(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_execute"]

    assert described.count("2018-08-20") == 1


# The date span is the part that was actually missing: everything else can be
# guessed from a hostname, and a window cannot.
def test_carries_the_date_span_and_says_why_it_matters(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_execute"]

    assert "2018-08-20" in described
    # All time, and said so: a narrower default returns a silent zero on any data
    # older than it, which is what sent three hunts looking at nothing.
    assert "defaults to 0" in described
    assert "not an absence of evidence" in described


# Verified against the running deployment, not read off Splunk's docs: the console
# form is silently empty through the REST search, so advising it would send a model
# down the same dead end this description exists to close.
def test_names_the_time_formats_that_work_and_the_one_that_does_not(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_execute"]

    assert "2018-08-19T00:00:00" in described
    assert "epoch second" in described
    assert "08/19/2018:00:00:00" in described and "does NOT take" in described
    # There is no such parameter, so telling it to set one would waste a turn.
    assert "no `latest` parameter" in described


# nl_search takes no time range at all, so the same map is a warning rather than
# an instruction: it cannot act on it.
def test_warns_that_the_natural_language_tool_cannot_set_a_range(monkeypatch):
    described = _describe(_load(monkeypatch, _ROWS))["splunk_nl_search"]

    assert "Takes no time range" in described
    # And carries no map: it has no way to act on one, and the description is
    # charged on every call. Repeating it there doubled the cost to say nothing.
    assert "index=botsv3" not in described


@pytest.mark.parametrize("rows,service", [(None, object()), ([], object()), ([], None)])
def test_leaves_the_plain_description_when_it_cannot_look(monkeypatch, rows, service):
    """A server answering no tools is worse than one whose description is thin."""
    described = _describe(_load(monkeypatch, rows, service=service))

    assert described["splunk_execute"] == "Execute SPL query"
    assert len(described) == 5


# This server starts with the backend, which on a fresh deployment is before anyone has
# entered a credential. Caching the resulting empty summary left every hunt for the life
# of the process querying blind -- the same failure this description exists to close.
def test_looks_again_after_a_deployment_that_was_not_configured_yet(monkeypatch):
    module = _load(monkeypatch, _ROWS)
    unreachable = {"still": True}

    class _Service:
        def search(self, query, earliest_time="-24h", max_count=1000):
            if unreachable["still"]:
                raise RuntimeError("splunk is unreachable")
            return _ROWS

    monkeypatch.setattr(module, "get_splunk_service", lambda: _Service())
    assert _describe(module)["splunk_execute"] == "Execute SPL query"

    unreachable["still"] = False
    assert "index=botsv3" in _describe(module)["splunk_execute"]


def test_asks_splunk_once_however_often_the_tools_are_listed(monkeypatch):
    module = _load(monkeypatch, _ROWS)
    calls = {"n": 0}
    real = module.get_splunk_service

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(module, "get_splunk_service", counted)
    _describe(module)
    _describe(module)

    assert calls["n"] == 1
