"""Live checks for OpenCTI, against a local platform.

``tests/live/ci/opencti.sh up`` starts OpenCTI and seeds one connected set of
intel (``ci/opencti_seed.py``): intrusion set "Vigil Live Lynx" uses malware
"VigilLiveLoader" and T1071.001; the malware uses T1059.001; an indicator for
203.0.113.88 is based-on that observable and indicates the malware; a report
holds all six. It writes the platform's URL and token, and each object's id, to
the env file. Every probe asks for something only that seed can answer, so an
empty answer, an error payload or "not configured" fails.
"""

import os

import pytest

from tests.live.harness import Probe, check

IP = "203.0.113.88"
CONFIG = ("OPENCTI_URL", "OPENCTI_API_TOKEN")


def _id(name: str) -> str:
    return os.environ.get(f"OPENCTI_LIVE_{name}", "")


def _ok(payload) -> dict:
    assert isinstance(payload, dict), f"not a JSON object: {str(payload)[:200]}"
    assert "error" not in payload, f"error payload: {payload['error']}"
    return payload


def _observable(payload) -> str:
    """The seeded IP is found exactly, with its indicator."""
    found = _ok(payload)
    assert found["found"] is True, f"{IP} not found: {found}"
    exact = [o for o in found["observables"] if o.get("exact_match")]
    assert len(exact) == 1 and exact[0]["observable_value"] == IP, found
    indicators = {i["id"] for i in exact[0]["indicators"]}
    assert _id("INDICATOR") in indicators, f"indicator missing from {indicators}"
    return f"{IP} found with its indicator"


def _indicator(payload) -> str:
    found = _ok(payload)
    assert found["found"] is True, found
    pattern = found["indicator"]["pattern"]
    assert IP in pattern and found["indicator"]["pattern_type"] == "stix", pattern
    return "pattern and type read"


def _attack_pattern(payload) -> str:
    """An attack pattern carries its ATT&CK id as its own field."""
    ids = {e.get("x_mitre_id") for e in _ok(payload)["entities"]}
    assert "T1071.001" in ids, f"T1071.001 missing from {ids}"
    return "x_mitre_id T1071.001"


def _malware_related(payload) -> str:
    """Both directions: what the malware uses, and who uses or indicates it."""
    related = _ok(payload)
    out = {
        (r["relationship_type"], r["to"].get("x_mitre_id"))
        for r in related["relationships"]
    }
    assert ("uses", "T1059.001") in out, f"outgoing uses T1059.001 missing: {out}"
    incoming = {
        (r["relationship_type"], r["from"]["name"])
        for r in related.get("incoming") or []
    }
    assert ("uses", "Vigil Live Lynx") in incoming, f"incoming uses missing: {incoming}"
    assert ("indicates", IP) in incoming, f"incoming indicates missing: {incoming}"
    return "uses T1059.001; used by Vigil Live Lynx; indicated by the IP"


def _report_objects(payload) -> str:
    """A report lists what it holds, capped, with the full count."""
    report = _ok(payload)
    assert report.get("object_count") == 6, f"object_count {report.get('object_count')}"
    assert len(report["objects"]) == 3, f"limit 3 not applied: {len(report['objects'])}"
    return "6 objects, 3 listed"


PROBES = [
    Probe(
        "opencti",
        "opencti_search_observable",
        {"value": IP},
        expect=_observable,
        requires=CONFIG,
        name="search_observable",
    ),
    Probe(
        "opencti",
        "opencti_get_indicator",
        {"indicator_id": _id("INDICATOR")},
        expect=_indicator,
        requires=CONFIG + ("OPENCTI_LIVE_INDICATOR",),
        name="get_indicator",
    ),
    Probe(
        "opencti",
        "opencti_search_entities",
        {"query": "Web Protocols", "entity_type": "attack_pattern"},
        expect=_attack_pattern,
        requires=CONFIG,
        name="search_entities",
    ),
    Probe(
        "opencti",
        "opencti_get_related",
        {"entity_id": _id("MALWARE")},
        expect=_malware_related,
        requires=CONFIG + ("OPENCTI_LIVE_MALWARE",),
        name="get_related",
    ),
    Probe(
        "opencti",
        "opencti_get_related",
        {"entity_id": _id("REPORT"), "limit": 3},
        expect=_report_objects,
        requires=CONFIG + ("OPENCTI_LIVE_REPORT",),
        name="report_objects",
    ),
]


@pytest.mark.parametrize("probe", PROBES, ids=repr)
async def test_connector(probe, mcp_client, request):
    await check(probe, mcp_client, request)
