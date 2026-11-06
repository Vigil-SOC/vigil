"""opencti_get_related on a container: its objects carry the ATT&CK id, and
object_count is what the container holds, not the capped page."""

import pytest

from core.integrations.opencti.client import OpenCTIService

REPORT = "a1b2c3d4-0000-4000-8000-000000000001"


def _service(monkeypatch, data):
    service = OpenCTIService("http://opencti.invalid", "token")
    asked = {}

    async def query(q, variables=None):
        asked["query"], asked["variables"] = q, variables
        return data

    monkeypatch.setattr(service, "_query", query)
    return service, asked


def _edges(*nodes):
    return {"edges": [{"node": n} for n in nodes]}


TECHNIQUE = {
    "id": "ap-1",
    "entity_type": "Attack-Pattern",
    "representative": {"main": "[T1059.001] PowerShell"},
    "x_mitre_id": "T1059.001",
}
GROUP = {
    "id": "is-1",
    "entity_type": "Intrusion-Set",
    "representative": {"main": "APT-X"},
}
INDICATOR = {
    "id": "in-1",
    "entity_type": "Indicator",
    "representative": {"main": "evil.example"},
}


@pytest.mark.asyncio
async def test_targets_and_objects_ask_for_the_attack_id(monkeypatch):
    service, asked = _service(monkeypatch, {"stixCoreRelationships": _edges()})
    await service.get_related(REPORT, limit=3)
    # once for relationship targets, once for container objects
    assert asked["query"].count("... on AttackPattern { x_mitre_id }") == 2


@pytest.mark.asyncio
async def test_report_objects_keep_x_mitre_id(monkeypatch):
    data = {
        "stixCoreRelationships": _edges(),
        "stixDomainObject": {
            "objects": {
                "pageInfo": {"globalCount": 3},
                **_edges(TECHNIQUE, GROUP, INDICATOR),
            }
        },
    }
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=50)
    assert [o.get("x_mitre_id") for o in out["objects"]] == ["T1059.001", None, None]


@pytest.mark.asyncio
async def test_object_count_is_the_report_total_not_the_page(monkeypatch):
    data = {
        "stixCoreRelationships": _edges(),
        "stixDomainObject": {
            "objects": {
                "pageInfo": {"globalCount": 9},
                **_edges(TECHNIQUE, GROUP, INDICATOR),
            }
        },
    }
    service, asked = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=3)
    assert asked["variables"]["n"] == 3
    assert len(out["objects"]) == 3
    assert out["object_count"] == 9


@pytest.mark.asyncio
async def test_object_count_falls_back_to_the_listed_objects(monkeypatch):
    data = {
        "stixCoreRelationships": _edges(),
        "stixDomainObject": {"objects": _edges(TECHNIQUE, GROUP)},
    }
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=3)
    assert out["object_count"] == 2


@pytest.mark.asyncio
async def test_a_non_container_has_no_objects(monkeypatch):
    rel = {"relationship_type": "uses", "confidence": 80, "to": TECHNIQUE}
    data = {"stixCoreRelationships": _edges(rel), "stixDomainObject": {}}
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=3)
    assert out["count"] == 1
    assert out["relationships"][0]["to"]["x_mitre_id"] == "T1059.001"
    assert "objects" not in out and "object_count" not in out


IP = {"id": "ip-1", "entity_type": "IPv4-Addr", "observable_value": "198.51.100.23"}


@pytest.mark.asyncio
async def test_an_indicators_targets_are_named(monkeypatch):
    # get_related on an indicator: which group it indicates, which IP it's based on.
    rels = [
        {"relationship_type": "indicates", "confidence": 80, "to": GROUP},
        {"relationship_type": "based-on", "confidence": 80, "to": IP},
    ]
    data = {"stixCoreRelationships": _edges(*rels), "stixDomainObject": {}}
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=5)
    assert [(r["relationship_type"], r["to"]) for r in out["relationships"]] == [
        ("indicates", {"id": "is-1", "entity_type": "Intrusion-Set", "name": "APT-X"}),
        (
            "based-on",
            {"id": "ip-1", "entity_type": "IPv4-Addr", "name": "198.51.100.23"},
        ),
    ]


@pytest.mark.asyncio
async def test_targets_and_objects_share_one_shape(monkeypatch):
    data = {
        "stixCoreRelationships": _edges(
            {"relationship_type": "uses", "confidence": 80, "to": TECHNIQUE}
        ),
        "stixDomainObject": {"objects": _edges(TECHNIQUE, GROUP, INDICATOR, IP)},
    }
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=5)
    technique = {
        "id": "ap-1",
        "entity_type": "Attack-Pattern",
        "name": "[T1059.001] PowerShell",
        "x_mitre_id": "T1059.001",
    }
    assert out["relationships"][0]["to"] == technique
    assert out["objects"] == [
        technique,
        {"id": "is-1", "entity_type": "Intrusion-Set", "name": "APT-X"},
        {"id": "in-1", "entity_type": "Indicator", "name": "evil.example"},
        {"id": "ip-1", "entity_type": "IPv4-Addr", "name": "198.51.100.23"},
    ]


@pytest.mark.asyncio
async def test_a_target_matching_no_fragment_reads_as_unnamed(monkeypatch):
    # A relationship to a relationship matches neither fragment: no name to give.
    data = {
        "stixCoreRelationships": _edges(
            {"relationship_type": "related-to", "confidence": 50, "to": {}}
        ),
        "stixDomainObject": {},
    }
    service, _ = _service(monkeypatch, data)
    out = await service.get_related(REPORT, limit=5)
    assert out["relationships"][0]["to"] == {
        "id": None,
        "entity_type": None,
        "name": None,
    }
