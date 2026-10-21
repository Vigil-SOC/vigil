"""The chart tells the backend when its State Directory cannot hold a credential.

Issue #2577: on the default emptyDir a secret saved in Settings reached one pod,
in plaintext (SECRETS_BACKEND=dotenv), and was gone on restart. The chart now
sets VIGIL_DIR_EPHEMERAL on an emptyDir so Settings refuses the write, and leaves
the encrypted backend default alone. A persistentVolumeClaim (the ReadWriteMany
setup values.yaml describes) does not set the flag.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil"
WORKLOADS = ("backend", "daemon", "llm-worker")


def _render(tmp_path: Path, *sets: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    # Subcharts are off by default and absent from a clean checkout.
    chart = tmp_path / "vigil"
    shutil.copytree(CHART, chart, ignore=shutil.ignore_patterns("charts", "Chart.lock"))
    meta = (chart / "Chart.yaml").read_text()
    (chart / "Chart.yaml").write_text(meta[: meta.index("\ndependencies:")] + "\n")
    args = ["helm", "template", "v", str(chart)]
    for s in ("secrets.postgresPassword=test-password", *sets):
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _state_env(docs: list[dict]) -> dict[str, dict[str, str]]:
    """VIGIL_DIR* env of each workload that mounts the State Directory."""
    found = {}
    for d in docs:
        component = d["metadata"].get("labels", {}).get("app.kubernetes.io/component")
        if d["kind"] not in ("Deployment", "StatefulSet") or component not in WORKLOADS:
            continue
        env = d["spec"]["template"]["spec"]["containers"][0].get("env", [])
        found[component] = {
            e["name"]: e.get("value") for e in env if e["name"].startswith("VIGIL_DIR")
        }
    return found


def test_default_render_refuses_secret_writes(tmp_path: Path) -> None:
    docs = _render(tmp_path)
    state = _state_env(docs)
    assert set(state) == set(WORKLOADS)
    for component, env in state.items():
        assert env.get("VIGIL_DIR_EPHEMERAL") == "true", component

    configmaps = [d for d in docs if d["kind"] == "ConfigMap"]
    for cm in configmaps:
        assert (cm.get("data") or {}).get("SECRETS_BACKEND") != "dotenv", cm["metadata"]


def test_persistent_volume_keeps_secret_writes(tmp_path: Path) -> None:
    docs = _render(
        tmp_path,
        "stateDirectory.volume.emptyDir=null",
        "stateDirectory.volume.persistentVolumeClaim.claimName=vigil-state",
    )
    state = _state_env(docs)
    assert set(state) == set(WORKLOADS)
    for component, env in state.items():
        assert "VIGIL_DIR" in env, component
        assert "VIGIL_DIR_EPHEMERAL" not in env, component
