"""Helm: the backend reads ``medic.enabled`` as VIGIL_MEDIC_ENABLED (Off ≠ Down, C8).

Rendered on a copy of the chart without its subcharts (the db-init and Medic
render tests do the same), so it needs the helm binary but no network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm"),
]

CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil"
USER = "medic-a1b2c3d4e5f6"
# What any Medic-on render needs since S7 (the chart refuses without them).
MEDIC_ON = (
    "medic.enabled=true",
    "medic.kubeApi.cidrs[0]=10.0.0.1/32",
    f"medic.gateway.viewer.username={USER}",
    "medic.gateway.viewer.passwordSecret.name=medic-viewer",
)


@pytest.fixture(scope="module")
def chart(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("chart") / "vigil"
    shutil.copytree(CHART, dest, ignore=shutil.ignore_patterns("medic-tests"))
    meta = yaml.safe_load((dest / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (dest / "Chart.yaml").write_text(yaml.safe_dump(meta))
    (dest / "Chart.lock").unlink(missing_ok=True)
    return dest


def _helm(chart: Path, *args: str) -> str:
    done = subprocess.run(
        ["helm", *args, str(chart), "--set", "secrets.jwtSecretKey=x"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _backend_env(chart: Path, *sets: str) -> dict:
    out = _helm(
        chart,
        "template",
        "t",
        "--show-only",
        "templates/backend-deployment.yaml",
        *[a for s in sets for a in ("--set", s)],
    )
    doc = yaml.safe_load(out)
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e.get("value") for e in env}


@pytest.mark.parametrize(
    "sets, expected",
    [
        ((), "false"),
        (("medic.enabled=false",), "false"),
        (MEDIC_ON, "true"),
    ],
)
def test_backend_flag_follows_medic_enabled(chart, sets, expected) -> None:
    assert _backend_env(chart, *sets)["VIGIL_MEDIC_ENABLED"] == expected


def _notes(chart: Path, *sets: str) -> str:
    out = subprocess.run(
        [
            "helm",
            "install",
            "t",
            str(chart),
            "--dry-run=client",
            "--namespace",
            "ns",
            *[a for s in sets for a in ("--set", s)],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.split("NOTES:", 1)[1]


def test_notes_give_the_account_step_only_when_medic_is_on(chart) -> None:
    assert "core.auth.service_account" not in _notes(chart)
    notes = _notes(chart, *MEDIC_ON, "medic.gateway.viewer.passwordSecret.key=password")
    assert f"ensure {USER}" in notes
    assert "get secret medic-viewer -o jsonpath='{.data.password}'" in notes
    # The password travels on a pipe: no --from-literal, no value on a command line.
    assert "exec -i" in notes and "--from-literal" not in notes
