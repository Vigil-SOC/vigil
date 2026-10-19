"""Live checks for the self-hosted search backends: Elastic, OpenSearch, Splunk.

``tests/live/ci/search_backends.sh up`` starts each backend with auth on and
seeds five events, three of which carry ``MARKER`` and ``IOC_IP`` (and two of
which are decoys with neither). Every probe is a read that only a connector that
authenticated, reached the instance and parsed its answer can satisfy: it must
return exactly the three seeded events, so an empty list, an error payload, a
"not configured" answer or a search that matches everything all fail.
"""

import os

import pytest

from tests.live.harness import Probe, check

MARKER = os.environ.get("VIGIL_LIVE_MARKER", "vigil_live_7f3a9c")
IOC_IP = "203.0.113.77"
SEEDED = 3  # events carrying MARKER (and, bar one, IOC_IP)


def _hits(payload) -> list[dict]:
    assert isinstance(payload, dict), f"not a JSON object: {str(payload)[:200]}"
    assert "error" not in payload, f"error payload: {payload['error']}"
    return payload.get("results") or []


def _marker_events(payload) -> str:
    """A search for MARKER returns exactly the seeded events, and only those."""
    hits = _hits(payload)
    assert len(hits) == SEEDED, f"expected {SEEDED} seeded events, got {len(hits)}: {hits}"
    assert all(MARKER in str(hit) for hit in hits), f"a hit lacks the marker: {hits}"
    return f"{len(hits)} seeded events returned"


def _ioc_events(payload) -> str:
    hits = _hits(payload)
    assert len(hits) == 2, f"expected the 2 events from {IOC_IP}, got {len(hits)}: {hits}"
    assert all(IOC_IP in str(hit) for hit in hits), f"a hit lacks {IOC_IP}: {hits}"
    return f"{len(hits)} events for {IOC_IP}"


def _splunk_host(payload) -> str:
    hits = _hits(payload)
    assert len(hits) == 1, f"expected the 1 event from vigil-live-host-b, got {hits}"
    assert MARKER in str(hits[0]) and hits[0].get("host") == "vigil-live-host-b", hits
    return "1 event for vigil-live-host-b"


def _in_indices(payload) -> str:
    assert isinstance(payload, dict) and "error" not in payload, payload
    assert "vigil-live-events" in payload["indices"], payload
    return f"{payload['count']} indices"


ELASTIC = ("ELASTIC_SIEM_ELASTICSEARCH_URL", "ELASTIC_SIEM_USERNAME", "ELASTIC_SIEM_PASSWORD")
OPENSEARCH = ("OPENSEARCH_OPENSEARCH_URL", "OPENSEARCH_USERNAME", "OPENSEARCH_PASSWORD")
SPLUNK = ("SPLUNK_URL", "SPLUNK_USERNAME", "SPLUNK_PASSWORD")

# The query DSL / SPL and the index are left to each connector's own default
# (the configured index pattern, ``*,-.*``, ``index=*``) so the config path is
# part of what passes.
_MARKER_QUERY = f'{{"match": {{"message": "{MARKER}"}}}}'

PROBES = [
    Probe(
        "elastic", "elastic_search_logs", {"query": _MARKER_QUERY},
        expect=_marker_events, requires=ELASTIC, name="search_logs",
    ),
    Probe(
        "elastic", "elastic_search_by_ioc", {"ioc_type": "ip", "ioc_value": IOC_IP},
        expect=_ioc_events, requires=ELASTIC, name="search_by_ioc",
        tools=("elastic_search_by_ioc", "elastic_get_indices"),
    ),
    Probe(
        "elastic", "elastic_get_indices", {},
        expect=_in_indices, requires=ELASTIC, name="get_indices",
    ),
    Probe(
        "opensearch", "opensearch_search_logs", {"query": _MARKER_QUERY},
        expect=_marker_events, requires=OPENSEARCH, name="search_logs",
    ),
    Probe(
        "opensearch", "opensearch_search_by_ioc", {"ioc_type": "ip", "ioc_value": IOC_IP},
        expect=_ioc_events, requires=OPENSEARCH, name="search_by_ioc",
    ),
    Probe(
        "splunk-selfhosted", "splunk_execute",
        {"spl_query": f"index=vigil_live {MARKER} | table _raw, host"},
        expect=_marker_events, requires=SPLUNK, name="execute",
        timeout=120.0,
    ),
    Probe(
        "splunk-selfhosted", "splunk_search_host", {"hostname": "vigil-live-host-b"},
        expect=_splunk_host, requires=SPLUNK, name="search_host",
        timeout=120.0,
    ),
]


@pytest.mark.parametrize("probe", PROBES, ids=repr)
async def test_connector(probe, mcp_client, request):
    await check(probe, mcp_client, request)
