"""Live checks for the connectors that need no paid account.

security-detections (rules cloned by ci/detection_rules.sh), github (any token
that can read a public repo, e.g. the Actions GITHUB_TOKEN), and cribl-stream
(a local leader started by ci/cribl.sh).

Not here, on purpose:

* atomic-red-team: its only tool is ``atomic_red_team_execute``, which runs an
  operator-supplied runner (``--technique`` / ``--hostname``, a contract no
  upstream tool implements). There is no read-only call and no real instance to
  point it at.
* cape-sandbox: CAPE needs a KVM host with analysis VMs; its read tools need a
  running CAPE API. Nothing free to stand up in CI.
"""

from __future__ import annotations

import base64
import json

import pytest

from tests.live.harness import Probe, check


def _detections(payload) -> list:
    assert isinstance(payload, dict), f"expected an object, got {str(payload)[:200]}"
    detections = payload.get("detections")
    assert detections, f"no detections came back: {str(payload)[:200]}"
    return detections


def _stats_have_rules(payload):
    # With no rules on disk the server answers get_stats with zeros and no error.
    assert isinstance(payload, dict), f"expected an object, got {str(payload)[:200]}"
    total = payload.get("total")
    assert isinstance(total, int) and total > 0, f"no rules indexed: {str(payload)[:300]}"
    sigma = payload.get("sigma")
    assert isinstance(sigma, int) and sigma > 0, f"Sigma rules were not indexed: {str(payload)[:300]}"
    return f"{total} rules indexed ({sigma} Sigma)"


def _t1059_rules(payload):
    detections = _detections(payload)
    assert any("T1059" in json.dumps(d) for d in detections), "no returned rule maps to T1059"
    return f"{len(detections)} rules for T1059.001"


def _search_hits(payload):
    detections = _detections(payload)
    assert all("name" in d for d in detections), f"results are not rules: {detections[:1]}"
    return f"{len(detections)} rules match 'powershell'"


def _github_file(payload):
    assert isinstance(payload, dict) and payload.get("type") == "file", (
        f"not a file answer: {str(payload)[:300]}"
    )
    assert payload.get("path") == "mcp-config.json", f"wrong file: {payload.get('path')}"
    body = payload["content"]
    if not body.lstrip().startswith("{"):  # the server may hand back raw base64
        body = base64.b64decode(body).decode()
    assert "mcpServers" in json.loads(body), "file content is not Vigil's mcp-config.json"
    return f"read Vigil-SOC/vigil:{payload['path']} ({payload['size']} bytes)"


def _github_repo(payload):
    assert isinstance(payload, dict) and payload.get("items"), (
        f"no repositories came back: {str(payload)[:300]}"
    )
    names = [item["full_name"] for item in payload["items"]]
    assert "Vigil-SOC/vigil" in names, f"Vigil-SOC/vigil missing from {names}"
    return "found Vigil-SOC/vigil"


def _cribl_groups(payload):
    assert isinstance(payload, list) and payload, f"no worker groups: {str(payload)[:300]}"
    ids = [g.get("id") for g in payload]
    assert "default" in ids, f"default worker group missing from {ids}"
    return f"worker groups: {', '.join(ids)}"


def _cribl_pipelines(payload):
    assert isinstance(payload, list) and payload, f"no pipelines: {str(payload)[:300]}"
    ids = {p.get("id") for p in payload}
    assert {"main", "passthru"} <= ids, f"default pipelines missing from {sorted(ids)}"
    return f"{len(ids)} pipelines incl. main, passthru"


PROBES = [
    Probe("security-detections", "get_stats", {}, expect=_stats_have_rules, name="stats", timeout=120),
    Probe(
        "security-detections",
        "list_by_mitre",
        {"technique_id": "T1059.001", "limit": 5},
        expect=_t1059_rules,
        name="mitre",
        timeout=120,
    ),
    Probe(
        "security-detections",
        "search",
        {"query": "powershell", "limit": 5},
        expect=_search_hits,
        name="search",
        timeout=120,
    ),
    Probe(
        "github",
        "get_file_contents",
        {"owner": "Vigil-SOC", "repo": "vigil", "path": "mcp-config.json"},
        expect=_github_file,
        name="file",
    ),
    Probe(
        "github",
        "search_repositories",
        {"query": "repo:Vigil-SOC/vigil"},
        expect=_github_repo,
        name="search",
    ),
    Probe("cribl-stream", "cribl_listWorkerGroups", {}, expect=_cribl_groups, name="groups"),
    Probe(
        "cribl-stream",
        "cribl_getPipelines",
        {"groupName": "default"},
        expect=_cribl_pipelines,
        name="pipelines",
    ),
]


@pytest.mark.parametrize("probe", PROBES, ids=repr)
async def test_connector(probe, mcp_client, request):
    await check(probe, mcp_client, request)
