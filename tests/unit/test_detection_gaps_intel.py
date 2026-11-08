"""The daily gaps run checks recent threat intel in code: the techniques OpenCTI's
newest intrusion sets and reports name go to identify_gaps(techniques=...), each one
with no proven rule is a gap raised once, and a day with no new gaps queues nothing,
OpenCTI or not. mitre_analyst keeps its OpenCTI tools for drafting."""

import asyncio
from pathlib import Path

import pytest
import yaml

from core.detections import gap_run
from tests.unit.test_detection_gaps_run import DAY, T0, _gaps
from tests.unit.test_detection_gaps_run import run as _run  # noqa: F401

ENTITIES = {
    "intrusion_set": [{"id": "is-1", "name": "APT Lab"}],
    "report": [{"id": "rep-1", "name": "Lab report"}],
}
# Each entity's attack patterns, as OpenCTI's filtered read gives them.
USED = {
    "is-1": ["T1059.001"],
    "rep-1": ["T1105", "t1059.001"],
}


class FakeOpenCTI:
    """OpenCTIService's two reads, from fixed content; ``hang`` never answers."""

    def __init__(self, hang=False, totals=None):
        self.hang = hang
        self.closed = False
        self.totals = totals or {}

    async def search_entities(self, query, entity_type, limit=10):
        assert query == ""  # newest first
        if self.hang:
            await asyncio.sleep(3600)
        return {"entities": ENTITIES.get(entity_type, [])[:limit]}

    async def techniques_of(self, entity_id, limit=500):
        used = USED.get(entity_id, [])
        total = self.totals.get(entity_id, len(used))
        return {
            "techniques": used[:limit],
            "read": min(limit, len(used)),
            "total": total,
        }

    async def close(self):
        self.closed = True


class Mcp:
    hang = False

    async def connect_to_server(self, name):
        if Mcp.hang:
            await asyncio.sleep(3600)
        return name == "opencti"


@pytest.fixture
def run(_run, monkeypatch):  # noqa: F811
    """The gaps run with OpenCTI on: identify_gaps checks the techniques it is
    passed, with ``proven`` the ones a SIEM rule proves."""
    proven = set()
    opencti = {"service": FakeOpenCTI()}
    passed = []

    class Tools:
        async def identify_gaps(self, since_days=30, since=None, techniques=None, **_):
            passed.append(techniques)
            report = _run.store.report(since)
            if techniques:
                report["techniques_checked"] = {
                    "gaps": [t for t in techniques if t not in proven]
                }
            return report

    monkeypatch.setattr(
        "core.detections.tools.get_security_detection_tools", lambda: Tools()
    )
    monkeypatch.setattr(Mcp, "hang", False)
    monkeypatch.setattr("core.integrations.mcp.client.process_mcp_client", Mcp)
    monkeypatch.setattr(
        "core.integrations.opencti.client.configured_service",
        lambda timeout=30: (opencti["service"], ()),
    )
    _run.proven, _run.opencti, _run.passed = proven, opencti, passed
    return _run


def test_the_newest_intel_is_read_into_techniques_the_same_every_time():
    first = asyncio.run(gap_run.intel_techniques(FakeOpenCTI()))
    assert first == {"T1059.001": ["APT Lab", "Lab report"], "T1105": ["Lab report"]}
    assert asyncio.run(gap_run.intel_techniques(FakeOpenCTI())) == first


def test_intel_gaps_reach_the_agent_once(run):
    run.proven.add("T1059.001")
    (payload,) = run(T0)
    assert run.passed[-1] == ["T1059.001", "T1105"]
    assert _gaps(payload) == [
        "T1105 named in recent threat intel, no proven rule (named by Lab report)"
    ]
    assert payload["case_techniques"] == ["T1105"]
    # The run no longer asks the agent to read OpenCTI itself.
    assert "opencti_opencti_search_entities" not in payload["hypothesis"]
    assert run(T0 + DAY) == []  # the same intel the next day: raised already
    assert run.opencti["service"].closed


def test_a_day_with_no_new_gaps_queues_nothing_with_opencti_on(run):
    run.proven.update({"T1059.001", "T1105"})
    assert run(T0) == []


def test_an_intel_gap_an_open_case_carries_is_not_raised(run):
    run.proven.add("T1059.001")
    run.covered.add("T1105")
    assert run(T0) == []
    run.covered.clear()
    assert run(T0 + DAY) == []  # handled while the Case was open


def test_opencti_not_answering_the_run_goes_on_without_intel(run, monkeypatch):
    monkeypatch.setattr(gap_run, "INTEL_TIMEOUT", 0.2)
    run.opencti["service"] = FakeOpenCTI(hang=True)
    run.store.blind["logs-lab-*"] = 1
    (payload,) = run(T0)
    assert run.passed[-1] is None
    assert _gaps(payload) == ["logs-lab-* blind spot (data source) (1 rule starved)"]
    assert run.opencti["service"].closed


def test_an_intel_gap_is_raised_again_once_its_raise_leaves_the_window(run):
    run.proven.add("T1059.001")
    assert len(run(T0)) == 1
    assert run(T0 + 10 * DAY) == []
    (payload,) = run(T0 + (gap_run.WINDOW_DAYS + 1) * DAY)
    assert _gaps(payload)[0].startswith("T1105 named in recent threat intel")


def test_mitre_analyst_keeps_its_opencti_tools():
    manifest = Path(gap_run.__file__).parents[1] / "agents/manifests/mitre_analyst.yaml"
    tools = yaml.safe_load(manifest.read_text())["needs"]
    assert {"opencti_opencti_search_entities", "opencti_opencti_get_related"} <= set(
        tools
    )


def test_opencti_not_connecting_is_logged_and_the_run_goes_on(run, monkeypatch, caplog):
    monkeypatch.setattr(gap_run, "INTEL_TIMEOUT", 0.2)
    monkeypatch.setattr(Mcp, "hang", True)
    run.store.blind["logs-lab-*"] = 1
    with caplog.at_level("WARNING", logger=gap_run.logger.name):
        (payload,) = run(T0)
    assert run.passed[-1] is None
    assert "OpenCTI did not answer; the gaps run skips threat intel" in caplog.text


def test_a_read_cut_short_by_the_cap_is_logged(caplog):
    with caplog.at_level("WARNING", logger=gap_run.logger.name):
        asyncio.run(gap_run.intel_techniques(FakeOpenCTI(totals={"rep-1": 612})))
    assert "Lab report names 612 attack patterns; the gaps run read 2" in caplog.text


def _client(monkeypatch, data):
    from core.integrations.opencti.client import OpenCTIService

    service = OpenCTIService("http://opencti.invalid", "token")
    asked = {}

    async def query(q, variables=None):
        asked["query"], asked["variables"] = q, variables
        return data

    monkeypatch.setattr(service, "_query", query)
    return service, asked


def test_only_attack_patterns_are_read_so_nothing_else_takes_their_place(monkeypatch):
    # An intrusion set with many malware and tool relationships: only its attack
    # patterns are asked for, so none of them is pushed out of the page.
    data = {
        "stixCoreRelationships": {
            "pageInfo": {"globalCount": 2},
            "edges": [
                {"node": {"to": {"x_mitre_id": "T1105"}}},
                {"node": {"to": {"x_mitre_id": "T1566.001"}}},
            ],
        },
        "stixDomainObject": {
            "objects": {
                "pageInfo": {"globalCount": 70},
                "edges": [{"node": {"x_mitre_id": f"T{1000 + i}"}} for i in range(60)],
            }
        },
    }
    service, asked = _client(monkeypatch, data)
    out = asyncio.run(service.techniques_of("a1b2c3d4-0000-4000-8000-000000000001"))
    assert asked["variables"]["types"] == ["Attack-Pattern"]
    assert "toTypes: $types" in asked["query"] and "types: $types" in asked["query"]
    assert asked["variables"]["n"] == 500
    assert out["techniques"][:2] == ["T1105", "T1566.001"]
    assert (len(out["techniques"]), out["read"], out["total"]) == (62, 62, 72)
