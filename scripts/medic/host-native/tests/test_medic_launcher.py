"""medic.sh: what `start.sh -d` does about Medic on a host-native install.

Drives the shell functions from a temp copy of scripts/ (so REPO_ROOT, logs/
and the runtime dir are temporary), with stub `sudo`, `id`, `uv` and `docker`
first on PATH. The stub sudo runs the command as the current user, so these
tests cover the launcher's decisions; running as a real `vigil-medic` user is
the CI job's e2e script (e2e_host_native.sh).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import signal
import socket
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
pwd >> "$SANDBOX/sudo.cwd"
# Real sudo keeps the cwd; bash run as another user then warns on stderr.
echo "shell-init: error retrieving current directory" >&2
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
        if not k.startswith(("VIGIL_", "DEV_MODE", "UV", "AGENT_HEALTH_PORT"))
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


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_existing_user_is_never_recreated(sb: Path, os_name: str) -> None:
    """Re-running the printed setup must not re-number an existing vigil-medic."""
    out = _bash(
        sb, RUN, VIGIL_MEDIC_ENABLED="true", STUB_SUDO="fail", MEDIC_HOST_OS=os_name
    )
    text = out.stdout + out.stderr
    assert "rc=1" in out.stdout and "NOPASSWD" in text
    assert "dscl" not in text and "useradd" not in text
    assert "user vigil-medic exists" in text


def test_stale_pidfile_with_reused_pid_is_not_running(sb: Path) -> None:
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (sb / "repo" / "logs" / "medic.pid").write_text(str(sleeper.pid))
        out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", STUB_USER="missing")
        assert "already running" not in out.stdout + out.stderr
        assert "rc=1" in out.stdout
    finally:
        sleeper.kill()
        sleeper.wait()


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


def test_readable_dotenv_refuses_and_prints_the_exact_chmod(sb: Path) -> None:
    """S8-4: the repo .env (0644 when copied from env.example) gets its own fix."""
    dotenv = sb / "repo" / ".env"
    dotenv.write_text("POSTGRES_PASSWORD=x\n")
    dotenv.chmod(0o644)
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    lines = out.stderr.splitlines()
    assert "rc=1" in out.stdout
    assert f"  chmod 0600 {dotenv}" in lines
    # Only the State Directory's own secrets need its mode changed.
    assert not any(ln.startswith("  chmod 0700") for ln in lines), out.stderr
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


def test_printed_chmod_fix_is_shell_quoted(sb: Path) -> None:
    state = sb / "my state"
    (sb / "state").rename(state)
    (state / "jwt_secret").chmod(0o644)
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", VIGIL_DIR=str(state))
    lines = out.stderr.splitlines()
    assert f"  chmod 0600 {sb}/my\\ state/jwt_secret" in lines, out.stderr
    assert f"  chmod 0700 {sb}/my\\ state" in lines


# --- DEV_MODE (S8-7, K1 T-37): refused, not warned --------------------------


@pytest.mark.parametrize(
    "value", ["true", "TRUE", " yes ", "1", "on", "t", "y", "maybe"]
)
def test_dev_mode_refuses_to_start(sb: Path, value: str) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", DEV_MODE=value)
    text = out.stdout + out.stderr
    assert "rc=1" in out.stdout
    assert "Medic not started: DEV_MODE is on" in out.stderr, text
    assert "T-37" in out.stderr
    assert not (sb / "repo" / "logs" / "medic.pid").exists()
    # Refused before anything runs as vigil-medic or gets installed.
    assert _calls(sb, "sudo.calls") == "" and _calls(sb, "uv.calls") == ""
    assert list((sb / "runtime").iterdir()) == []


@pytest.mark.parametrize("value", ["", "false", "FALSE", "0", "no", "off", "f", "n"])
def test_dev_mode_off_values_do_not_refuse(sb: Path, value: str) -> None:
    out = _bash(
        sb, RUN, VIGIL_MEDIC_ENABLED="true", DEV_MODE=value, STUB_USER="missing"
    )
    assert "DEV_MODE" not in out.stdout + out.stderr
    assert "there is no OS user vigil-medic" in out.stderr


def test_venv_failure_does_not_start(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", STUB_UV="fail")
    assert "rc=1" in out.stdout
    assert not (sb / "repo" / "logs" / "medic.pid").exists()


# --- Opted in, prerequisites met ------------------------------------------------


def test_starts_medic_as_vigil_medic_from_its_own_venv(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    text = out.stdout + out.stderr
    assert "rc=0" in out.stdout, text
    assert "loopback" in text and "DEV_MODE" not in text
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
    # L49: the worker's readiness port on loopback (AGENT_HEALTH_PORT's default).
    assert "--agent-worker 127.0.0.1:6990" in sudo[1]
    assert set(_calls(sb, "sudo.cwd").split()) == {"/"}

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


def test_worker_address_ignores_agent_health_port(sb: Path) -> None:
    """scripts/agent_up.sh always starts the worker on 6990, whatever .env says."""
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true", AGENT_HEALTH_PORT="7001")
    assert "rc=0" in out.stdout, out.stdout + out.stderr
    # The loop is launched in the background: its sudo call lands a bit later.
    _wait_for(lambda: len(_calls(sb, "sudo.calls").splitlines()) == 2)
    assert "--agent-worker 127.0.0.1:6990" in _calls(sb, "sudo.calls").splitlines()[1]


def test_agent_up_still_pins_the_worker_port() -> None:
    """If agent_up.sh ever honours AGENT_HEALTH_PORT, medic.sh must follow it."""
    assert "AGENT_HEALTH_PORT=6990" in (REPO / "scripts" / "agent_up.sh").read_text()


# --- S9: the backend's path to Medic's status (no gateway on this shape) ------------

KEY_RE = r"[A-Za-z0-9_-]{43}"
BACKEND_ENV = (
    'rc=0; medic_host_backend_env || rc=$?; echo "rc=$rc"; '
    'echo "url=${VIGIL_MEDIC_API_URL-unset}"; echo "file=${VIGIL_MEDIC_API_KEY_FILE-unset}"'
)


def _vars(out: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in out.stdout.splitlines() if "=" in ln)


def test_backend_env_off_does_nothing(sb: Path) -> None:
    out = _bash(sb, BACKEND_ENV)
    assert _vars(out) == {"rc": "0", "url": "unset", "file": "unset"}
    assert not (sb / "state" / "medic_api_key").exists()
    assert out.stderr == ""


def test_backend_env_mints_a_private_key_and_points_the_backend_at_loopback(
    sb: Path,
) -> None:
    out = _bash(sb, BACKEND_ENV, VIGIL_MEDIC_ENABLED="true")
    got = _vars(out)
    key = sb / "state" / "medic_api_key"
    assert got == {"rc": "0", "url": "http://127.0.0.1:8470", "file": str(key)}
    assert re.fullmatch(KEY_RE, key.read_text().strip())
    assert key.stat().st_mode & 0o777 == 0o600
    first = key.read_text()
    # Idempotent: a restart keeps the key the running Medic already holds.
    _bash(sb, BACKEND_ENV, VIGIL_MEDIC_ENABLED="true")
    assert key.read_text() == first
    assert first.strip() not in out.stdout + out.stderr


def test_backend_env_keeps_operator_settings(sb: Path) -> None:
    own = sb / "own_key"
    own.write_text("Z" * 43)
    own.chmod(0o600)
    out = _bash(
        sb,
        BACKEND_ENV,
        VIGIL_MEDIC_ENABLED="true",
        VIGIL_MEDIC_API_URL="http://127.0.0.1:9470",
        VIGIL_MEDIC_API_KEY_FILE=str(own),
    )
    assert _vars(out)["url"] == "http://127.0.0.1:9470"
    assert _vars(out)["file"] == str(own)
    assert own.read_text() == "Z" * 43


def test_backend_env_replaces_a_key_not_in_x2s_shape(sb: Path) -> None:
    key = sb / "state" / "medic_api_key"
    key.write_text("0123abcd" * 8)  # V1's old 64-hex format
    _bash(sb, BACKEND_ENV, VIGIL_MEDIC_ENABLED="true")
    assert re.fullmatch(KEY_RE, key.read_text().strip())


def test_start_hands_the_key_to_the_loop_on_stdin(sb: Path) -> None:
    out = _bash(sb, RUN, VIGIL_MEDIC_ENABLED="true")
    assert "rc=0" in out.stdout, out.stdout + out.stderr
    _wait_for(lambda: len(_calls(sb, "sudo.calls").splitlines()) == 2)
    loop_call = _calls(sb, "sudo.calls").splitlines()[1]
    assert "--api-key-stdin" in loop_call
    key = (sb / "state" / "medic_api_key").read_text().strip()
    assert key not in loop_call
    copy = sb / "data" / "run" / "api_key"
    _wait_for(copy.exists)
    assert copy.read_text() == key


def test_backend_reads_medics_status_on_loopback(sb: Path) -> None:
    """What the backend's poll does on host-native (V2 fetch_status): GET
    http://127.0.0.1:8470/v1/status with the key file start.sh exported."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8470)) == 0:
            pytest.skip("something already listens on 127.0.0.1:8470")
    out = _bash(sb, BACKEND_ENV + "; " + RUN, VIGIL_MEDIC_ENABLED="true")
    assert "rc=0" in out.stdout, out.stdout + out.stderr
    key_file = _vars(out)["file"]
    key = Path(key_file).read_text().strip()

    def get(k: str):
        conn = http.client.HTTPConnection("127.0.0.1", 8470, timeout=5)
        try:
            conn.request("GET", "/v1/status", headers={"X-Medic-Key": k})
            resp = conn.getresponse()
            return resp.status, resp.read()
        except OSError:
            return None, b""
        finally:
            conn.close()

    _wait_for(lambda: get(key)[0] == 200, timeout=60)
    status, body = get(key)
    assert status == 200
    doc = json.loads(body)
    assert doc["state"] in ("starting", "running", "degraded", "blind")
    assert doc["instance_id"].startswith("mi_")
    assert get("W" * 43)[0] == 401
    log = (sb / "repo" / "logs" / "medic.log").read_text()
    assert "listening on 127.0.0.1:8470" in log and key not in log


# --- The hooks in start.sh and shutdown_all.sh ---------------------------------


def test_start_sh_hooks_medic_into_daemon_mode_only() -> None:
    text = (REPO / "start.sh").read_text()
    assert 'source "$(dirname "$0")/scripts/medic/host-native/medic.sh"' in text
    daemon = text.split("# Daemon", 1)[1]
    foreground = text.split("# Foreground", 1)[1].split("# Daemon", 1)[0]
    assert "medic_host_start" in daemon and "medic_host_start" not in foreground
    assert "medic_host_foreground_notice" in foreground
    # S9: the backend learns where Medic's status is before it starts.
    assert daemon.index("medic_host_backend_env") < daemon.index("nohup uvicorn")


def test_shutdown_all_stops_medic_by_pidfile() -> None:
    text = (REPO / "shutdown_all.sh").read_text()
    assert "logs/medic.pid" in text.split("# Kill by process pattern", 1)[0]
