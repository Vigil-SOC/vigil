"""medic.sh: what `start.sh -d` does about Medic on a host-native install.

Drives the shell functions from a temp copy of scripts/ (so REPO_ROOT, logs/
and the runtime dir are temporary), with stub `sudo`, `id`, `uv` and `docker`
first on PATH. The stub sudo runs the command as the current user, so these
tests cover the launcher's decisions; running as a real `vigil-medic` user is
the CI job's e2e script (e2e_host_native.sh).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HOST_NATIVE = HERE.parent
REPO = HERE.parents[3]

STUB_SUDO = """#!/bin/bash
echo "$*" >> "$SANDBOX/sudo.calls"
[ "${STUB_SUDO:-ok}" = fail ] && { echo "sudo: a password is required" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in -n) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac
done
# Like sudo's env_reset: the target gets a fresh environment.
exec env -i PATH=/usr/bin:/bin HOME=/nonexistent "$@"
"""

STUB_ID = """#!/bin/bash
if [ "${1:-}" = -u ] && [ "${2:-}" = vigil-medic ]; then
  [ "${STUB_USER:-ok}" = missing ] && { echo "id: vigil-medic: no such user" >&2; exit 1; }
  exec /usr/bin/id -u
fi
exec /usr/bin/id "$@"
"""

STUB_UV = """#!/bin/bash
echo "$*" >> "$SANDBOX/uv.calls"
env | grep '^UV_' | sort >> "$SANDBOX/uv.env"
[ "${STUB_UV:-ok}" = fail ] && { echo "uv: no network" >&2; exit 1; }
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/bash\\nexec %s "$@"\\n' "$STUB_PY" > "$UV_PROJECT_ENVIRONMENT/bin/python"
chmod 0755 "$UV_PROJECT_ENVIRONMENT/bin/python"
"""


def _stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def sb(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "scripts" / "medic").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "lib.sh", root / "scripts" / "lib.sh")
    shutil.copytree(HOST_NATIVE, root / "scripts" / "medic" / "host-native")
    (root / "services").mkdir()
    (root / "services" / "medic").symlink_to(REPO / "services" / "medic")
    (root / "logs").mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _stub(bin_ / "sudo", STUB_SUDO)
    _stub(bin_ / "id", STUB_ID)
    _stub(bin_ / "uv", STUB_UV)
    _stub(bin_ / "docker", "#!/bin/bash\nexit 1\n")
    (tmp_path / "data").mkdir(mode=0o700)
    (tmp_path / "runtime").mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    for name in ("master.key", "secrets.enc", "jwt_secret"):
        (state / name).write_text("s3cret")
        # Unreadable even to its owner: the stub sudo runs as this same user.
        (state / name).chmod(0)
    yield tmp_path
    pidfile = root / "logs" / "medic.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


def _env(sb: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VIGIL_", "DEV_MODE", "UV"))
    }
    return {
        **env,
        "PATH": f"{sb / 'bin'}:{os.environ['PATH']}",
        "SANDBOX": str(sb),
        "STUB_PY": sys.executable,
        "UV": str(sb / "bin" / "uv"),
        "VIGIL_DIR": str(sb / "state"),
        "VIGIL_MEDIC_DATA_DIR": str(sb / "data"),
        "VIGIL_MEDIC_HOST_RUNTIME": str(sb / "runtime"),
        **extra,
    }


def _bash(sb: Path, body: str, **extra: str) -> subprocess.CompletedProcess[str]:
    root = sb / "repo"
    script = (
        f'source "{root}/scripts/lib.sh"\n'
        f'source "{root}/scripts/medic/host-native/medic.sh"\n'
        f'cd "{root}"\n{body}'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=_env(sb, **extra),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# lib.sh sets errexit, so a bare non-zero return would end the script first.
RUN = "rc=0; medic_host_start || rc=$?; echo rc=$rc"


def _calls(sb: Path, name: str) -> str:
    p = sb / name
    return p.read_text() if p.exists() else ""


def _check(sb: Path) -> subprocess.CompletedProcess[str]:
    rt = sb / "runtime"
    return subprocess.run(
        [
            str(rt / "bin" / "medic-loop"),
            "--python",
            str(rt / "venv" / "bin" / "python"),
            "--app",
            str(rt / "app"),
            "--data-dir",
            str(sb / "data"),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for(pred, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


# --- Off by default (check 12, and "nothing changes for anyone who doesn't opt in")


@pytest.mark.parametrize("flag", [None, "", "false", "0", "nope"])
def test_flag_not_on_is_a_silent_no_op(sb: Path, flag: str | None) -> None:
    extra = {} if flag is None else {"VIGIL_MEDIC_ENABLED": flag}
    out = _bash(sb, RUN, **extra)
    assert out.stdout == "rc=0\n" and out.stderr == ""
    assert not (sb / "repo" / "logs" / "medic.pid").exists()
    assert not (sb / "repo" / "logs" / "medic.log").exists()
    assert _calls(sb, "sudo.calls") == "" and _calls(sb, "uv.calls") == ""
    assert list((sb / "runtime").iterdir()) == []


@pytest.mark.parametrize("flag", ["true", "TRUE", " yes ", "1", "on"])
def test_flag_values_match_medic_config(sb: Path, flag: str) -> None:
    out = _bash(sb, "medic_host_enabled && echo on", VIGIL_MEDIC_ENABLED=flag)
    assert out.stdout == "on\n"


def test_foreground_notice_only_when_on(sb: Path) -> None:
    off = _bash(sb, "medic_host_foreground_notice")
    assert off.stdout == "" and off.stderr == ""
    out = _bash(sb, "medic_host_foreground_notice", VIGIL_MEDIC_ENABLED="true")
    assert "./start.sh -d" in out.stdout + out.stderr


# --- Opted in, prerequisites missing: warn, print setup, don't start ------------


@pytest.mark.parametrize(
    "os_name,creates_user", [("Linux", "useradd"), ("Darwin", "dscl")]
)
def test_missing_user_prints_exact_setup_and_does_not_start(
    sb: Path, os_name: str, creates_user: str
) -> None:
    out = _bash(
        sb,
        RUN,
        VIGIL_MEDIC_ENABLED="true",
        STUB_USER="missing",
        MEDIC_HOST_OS=os_name,
    )
    text = out.stdout + out.stderr
    assert "loopback" in text  # the K1 T-14 warning, shown whenever it's opted in
    assert creates_user in text
    assert f"install -d -o vigil-medic -g vigil-medic -m 0700 {sb / 'data'}" in text
    assert f"(vigil-medic) NOPASSWD: {sb / 'runtime'}/bin/medic-loop" in text
    assert "rc=1" in out.stdout
    assert not (sb / "repo" / "logs" / "medic.pid").exists()
    assert _calls(sb, "uv.calls") == ""


@pytest.mark.parametrize("problem", ["missing", "mode"])
def test_bad_data_dir_does_not_start(sb: Path, problem: str) -> None:
    if problem == "missing":
        (sb / "data").rmdir()
    else:
        (sb / "data").chmod(0o755)
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    assert "rc=1" in out.stdout
    assert str(sb / "data") in out.stdout + out.stderr
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


def test_no_sudo_rule_does_not_start(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", STUB_SUDO="fail")
    assert "rc=1" in out.stdout
    assert "NOPASSWD" in out.stdout + out.stderr
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


@pytest.mark.parametrize("name", ["master.key", "secrets.enc", "jwt_secret"])
def test_readable_secret_refuses_to_start(sb: Path, name: str) -> None:
    """Check 8: Medic's user must not be able to read the State Directory's secrets."""
    (sb / "state" / name).chmod(0o644)
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    text = out.stdout + out.stderr
    assert "rc=1" in out.stdout
    assert str(sb / "state" / name) in text and "chmod" in text
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


def test_readable_dotenv_refuses_to_start(sb: Path) -> None:
    (sb / "repo" / ".env").write_text("POSTGRES_PASSWORD=x\n")
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    assert "rc=1" in out.stdout
    assert str(sb / "repo" / ".env") in out.stdout + out.stderr


def test_venv_failure_does_not_start(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", STUB_UV="fail")
    assert "rc=1" in out.stdout
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


# --- Opted in, prerequisites met ------------------------------------------------


def test_starts_medic_as_vigil_medic_from_its_own_venv(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", DEV_MODE="true")
    text = out.stdout + out.stderr
    assert "rc=0" in out.stdout, text
    assert "loopback" in text and "DEV_MODE" in text
    rt = sb / "runtime"

    # Its own venv, from its own lock, with the interpreter inside the runtime dir
    # (vigil-medic can't read the Vigil user's home, where uv keeps it by default).
    uv_call = _calls(sb, "uv.calls")
    assert "sync" in uv_call and "--frozen" in uv_call and "--no-dev" in uv_call
    assert f"--project {rt}/app/services/medic" in uv_call
    uv_env = _calls(sb, "uv.env")
    assert f"UV_PROJECT_ENVIRONMENT={rt}/venv" in uv_env
    assert f"UV_PYTHON_INSTALL_DIR={rt}/python" in uv_env

    # The code copy matches the image: no test material.
    assert (rt / "app" / "services" / "medic" / "__main__.py").is_file()
    assert (rt / "app" / "services" / "medic" / "uv.lock").is_file()
    assert not (rt / "app" / "services" / "medic" / "tests").exists()
    assert not (rt / "app" / "services" / "medic" / "contracts" / "tests").exists()
    assert (rt / "bin" / "medic-loop").stat().st_mode & 0o777 == 0o755

    pidfile = sb / "repo" / "logs" / "medic.pid"
    assert pidfile.is_file()
    _wait_for(lambda: _check(sb).returncode == 0)

    # Run as vigil-medic through the one sudo rule: the probe, then the loop.
    sudo = _calls(sb, "sudo.calls").splitlines()
    assert len(sudo) == 2
    assert all(c.startswith(f"-n -u vigil-medic -- {rt}/bin/medic-loop") for c in sudo)
    assert "--probe" in sudo[0] and f"--data-dir {sb / 'data'}" in sudo[1]

    # A second start while it runs doesn't start a second Medic.
    again = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    assert "rc=0" in again.stdout and "already running" in again.stdout + again.stderr
    assert len(_calls(sb, "sudo.calls").splitlines()) == len(sudo)

    # Stop the way shutdown_all.sh does: TERM to the pidfile's process.
    loop_pid = int(pidfile.read_text())
    os.kill(loop_pid, signal.SIGTERM)
    _wait_for(
        lambda: (
            subprocess.run(["kill", "-0", str(loop_pid)], check=False).returncode != 0
        )
    )
    assert _check(sb).returncode != 0
    assert "stopped" in (sb / "repo" / "logs" / "medic.log").read_text()


# --- The hooks in start.sh and shutdown_all.sh ---------------------------------


def test_start_sh_hooks_medic_into_daemon_mode_only() -> None:
    text = (REPO / "start.sh").read_text()
    assert 'source "$(dirname "$0")/scripts/medic/host-native/medic.sh"' in text
    daemon = text.split("# Daemon", 1)[1]
    foreground = text.split("# Foreground", 1)[1].split("# Daemon", 1)[0]
    assert "medic_host_start" in daemon and "medic_host_start" not in foreground
    assert "medic_host_foreground_notice" in foreground


def test_shutdown_all_stops_medic_by_pidfile() -> None:
    text = (REPO / "shutdown_all.sh").read_text()
    assert "logs/medic.pid" in text.split("# Kill by process pattern", 1)[0]
