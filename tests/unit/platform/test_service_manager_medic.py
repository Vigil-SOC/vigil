"""V1-4: the console's service controls keep Medic's overlay, as scripts/lib.sh dc does.

Once scripts/medic/enable-compose.sh has written <secrets dir>/compose.env, a
start, stop or restart from the console must not render Compose without Medic's
overlay: an `up` of a medic-net member without it drops that service off
medic-net, and Medic goes blind. tests/medic_compose/test_dc_overlay.py pins the
shell side with the same cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.platform import service_manager as sm

pytestmark = pytest.mark.unit

ENABLED = (
    "# Medic on Compose: written by scripts/medic/enable-compose.sh.\n"
    "VIGIL_MEDIC_ENABLED=true\n"
    "VIGIL_MEDIC_SECRETS_DIR=/srv/medic-secrets\n"
    "VIGIL_MEDIC_DOCKER_GID=0\n"
    "VIGIL_MEDIC_VIEWER_USER=medic-abcdefghijkl\n"
)


@pytest.fixture
def calls(monkeypatch, tmp_path: Path) -> list[tuple[list[str], dict[str, str]]]:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), dict(kw["env"])))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    monkeypatch.setattr(sm, "_dc_cmd", lambda: ["docker", "compose"])
    for name in ("VIGIL_MEDIC_SECRETS_DIR", "VIGIL_MEDIC_COMPOSE_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # The backend's own environment, as .env sets it (env.example ships false).
    monkeypatch.setenv("VIGIL_MEDIC_ENABLED", "false")
    return seen


def _write(tmp_path: Path, text: str, where: Path | None = None) -> None:
    d = where or tmp_path / "home" / ".vigil-medic" / "secrets"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / "compose.env").write_text(text)


def test_without_compose_env_the_command_is_unchanged(calls) -> None:
    sm._compose(["up", "-d", "postgres"], None, 10)
    ((cmd, env),) = calls
    assert cmd == [
        "docker",
        "compose",
        "-f",
        str(sm.COMPOSE_FILE),
        "up",
        "-d",
        "postgres",
    ]
    assert env["VIGIL_MEDIC_ENABLED"] == "false"


def test_enabled_adds_the_overlay_and_medics_settings(calls, tmp_path) -> None:
    _write(tmp_path, ENABLED)
    sm._compose(["restart", "redis"], None, 10)
    ((cmd, env),) = calls
    assert cmd == [
        "docker",
        "compose",
        "-f",
        str(sm.COMPOSE_FILE),
        "-f",
        str(sm.MEDIC_OVERLAY),
        "restart",
        "redis",
    ]
    # compose.env beats the backend's own environment (V1 review R1).
    assert env["VIGIL_MEDIC_ENABLED"] == "true"
    assert env["VIGIL_MEDIC_VIEWER_USER"] == "medic-abcdefghijkl"


def test_secrets_dir_setting_is_honoured(calls, tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write(tmp_path, ENABLED, where=elsewhere)
    monkeypatch.setenv("VIGIL_MEDIC_SECRETS_DIR", str(elsewhere))
    sm._compose(["ps"], None, 10)
    assert str(sm.MEDIC_OVERLAY) in calls[0][0]


def test_only_medic_settings_are_taken(calls, tmp_path) -> None:
    _write(
        tmp_path,
        ENABLED + "COMPOSE_FILE=/tmp/evil.yml\nLD_PRELOAD=/tmp/evil.so\nnot a line\n",
    )
    sm._compose(["ps"], None, 10)
    ((_, env),) = calls
    assert env.get("COMPOSE_FILE") != "/tmp/evil.yml"
    assert env.get("LD_PRELOAD") != "/tmp/evil.so"


def test_compose_env_cannot_name_compose_files(calls, tmp_path) -> None:
    """A compose file can run anything through Docker: only Vigil's env names one."""
    _write(tmp_path, ENABLED + "VIGIL_MEDIC_COMPOSE_OVERRIDE=/tmp/evil.yml\n")
    sm._compose(["ps"], None, 10)
    ((cmd, env),) = calls
    assert "/tmp/evil.yml" not in cmd
    assert env.get("VIGIL_MEDIC_COMPOSE_OVERRIDE") != "/tmp/evil.yml"


def test_overlay_missing_means_no_overlay(calls, tmp_path, monkeypatch) -> None:
    _write(tmp_path, ENABLED)
    monkeypatch.setattr(sm, "MEDIC_OVERLAY", tmp_path / "gone.yml")
    sm._compose(["ps"], None, 10)
    ((cmd, env),) = calls
    assert cmd == ["docker", "compose", "-f", str(sm.COMPOSE_FILE), "ps"]
    assert env["VIGIL_MEDIC_ENABLED"] == "false"


def test_operator_override_files_come_last(calls, tmp_path, monkeypatch) -> None:
    _write(tmp_path, ENABLED)
    monkeypatch.setenv("VIGIL_MEDIC_COMPOSE_OVERRIDE", "/o/a.yml::/o/b.yml")
    sm._compose(["ps"], None, 10)
    cmd = calls[0][0]
    assert cmd[cmd.index(str(sm.MEDIC_OVERLAY)) + 1 :] == [
        "-f",
        "/o/a.yml",
        "-f",
        "/o/b.yml",
        "ps",
    ]


def test_profile_still_selected_by_env(calls, tmp_path) -> None:
    _write(tmp_path, ENABLED)
    sm._compose(["up", "-d", "jaeger"], "observability", 10)
    ((_, env),) = calls
    assert env["COMPOSE_PROFILES"] == "observability"
