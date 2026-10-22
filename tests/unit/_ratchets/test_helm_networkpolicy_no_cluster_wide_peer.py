"""Chart NetworkPolicies must not admit every pod in the cluster.

Issue #2568: a bare `namespaceSelector: {}` peer selects every pod in every
namespace (not the kubelet), and since ingress rules are OR'd it overrode the
narrower rules beside it. Node probes get `networkPolicies.probeAllowFrom`
CIDRs on the httpGet probe ports only; an empty daemon `webhookAllowFrom`
means in-namespace pods only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "infra" / "helm" / "vigil"
PROBE_CIDR = "10.244.0.0/16"
POLICIES = ("backend", "daemon", "agent-worker", "agent-serve")


def _render(tmp_path: Path, *sets: str) -> dict[str, dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    # Subcharts are off by default and absent from a clean checkout.
    chart = tmp_path / "vigil"
    shutil.copytree(CHART, chart, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = (chart / "Chart.yaml").read_text()
    (chart / "Chart.yaml").write_text(meta[: meta.index("\ndependencies:")] + "\n")
    args = ["helm", "template", "v", str(chart), "-s", "templates/networkpolicy.yaml"]
    # -s only filters output; helm still renders secret.yaml, which requires a password.
    for s in (
        "networkPolicies.enabled=true",
        "secrets.postgresPassword=test-password,secrets.vigilAppPassword=test-app-password",
        *sets,
    ):
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d["spec"]
        for d in yaml.safe_load_all(out)
        if d and "app.kubernetes.io/component" in d["metadata"]["labels"]
    }


def _rules(spec: dict, port: int) -> list[list[dict]]:
    """The peer lists of every ingress rule that opens `port`."""
    return [
        r["from"]
        for r in spec.get("ingress") or []
        if any(p["port"] == port for p in r.get("ports", []))
    ]


def _ports(values: dict) -> dict[str, int]:
    return {
        "backend": values["backend"]["service"]["port"],
        "daemon": values["daemon"]["ports"]["health"],
        "agent-worker": 6990,
        "agent-serve": values["agentServe"]["service"]["port"],
    }


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def test_no_bare_namespace_selector(tmp_path: Path) -> None:
    for sets in ((), (f"networkPolicies.probeAllowFrom={{{PROBE_CIDR}}}",)):
        specs = _render(tmp_path / str(len(sets)), *sets)
        for name in POLICIES:
            for rule in specs[name].get("ingress") or []:
                for peer in rule["from"]:
                    assert peer.get("namespaceSelector") != {}, (name, rule)


def test_empty_webhook_allow_from_is_in_namespace_only(
    tmp_path: Path, values: dict
) -> None:
    daemon = _render(tmp_path)["daemon"]
    assert _rules(daemon, values["daemon"]["ports"]["webhook"]) == [
        [{"podSelector": {}}]
    ]


def test_probe_cidrs_on_probe_ports_only(tmp_path: Path, values: dict) -> None:
    specs = _render(tmp_path, f"networkPolicies.probeAllowFrom={{{PROBE_CIDR}}}")
    probe_peer = {"ipBlock": {"cidr": PROBE_CIDR}}
    for name, port in _ports(values).items():
        assert any(probe_peer in peers for peers in _rules(specs[name], port)), name
    for port in (
        values["daemon"]["ports"]["webhook"],
        values["daemon"]["ports"]["metrics"],
    ):
        assert all(
            probe_peer not in peers for peers in _rules(specs["daemon"], port)
        ), port


def test_allow_probes_from_any_namespace_is_gone() -> None:
    for path in CHART.rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".tpl", ".md"):
            assert "allowProbesFromAnyNamespace" not in path.read_text(), path
