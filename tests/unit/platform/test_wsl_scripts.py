"""WSL2 handling in scripts/lib.sh, and the Ollama install hint.

Drives the shell functions from a temp copy of lib.sh with stub commands and
links to a few host tools as the whole PATH, so a real wslview, explorer.exe or
docker on the host is never found. `uname` is stubbed and the copy reads a temp
file for /proc/version, so the Linux and WSL branches run the same on any host,
WSL included, and no test can launch a real browser or touch a real Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]

# Host tools lib.sh and the stubs run; nothing else from the host is on PATH.
HOST_TOOLS = ("basename", "dirname", "grep", "touch")
BASH = shutil.which("bash") or "/bin/bash"

# Records its name and arguments in $ROOT_FOR_STUB/calls, then exits $rc.
RECORDER = """#!/bin/bash
echo "$(basename "$0") $*" >> "$ROOT_FOR_STUB/calls"
exit "${STUB_RC:-0}"
"""

# `docker info` fails until $ROOT_FOR_STUB/up exists; every other call succeeds.
STUB_DOCKER = """#!/bin/bash
echo "docker $*" >> "$ROOT_FOR_STUB/calls"
if [ "$1" = info ]; then
  [ -e "$ROOT_FOR_STUB/up" ] && exit 0
  touch "$ROOT_FOR_STUB/up"
  exit 1
fi
exit 0
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    lib = (REPO / "scripts" / "lib.sh").read_text()
    assert "/proc/version" in lib
    (tmp_path / "scripts" / "lib.sh").write_text(
        lib.replace("/proc/version", str(tmp_path / "proc_version"))
    )
    (tmp_path / "bin").mkdir()
    for name in HOST_TOOLS:
        found = shutil.which(name)
        assert found, f"{name} not on PATH"
        (tmp_path / "bin" / name).symlink_to(found)
    (tmp_path / "calls").write_text("")
    for name in ("sleep",):
        stub = tmp_path / "bin" / name
        stub.write_text("#!/bin/bash\nexit 0\n")
        stub.chmod(0o755)
    stub = tmp_path / "bin" / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    return tmp_path


def _stub(root: Path, name: str, body: str = RECORDER) -> None:
    path = root / "bin" / name
    path.write_text(body)
    path.chmod(0o755)


def _bash(
    root: Path, body: str, *, wsl: bool, system: str = "Linux", **env: str
) -> subprocess.CompletedProcess[str]:
    _stub(
        root, "uname", f'#!/bin/bash\n[ "$1" = -s ] && echo {system} || echo x86_64\n'
    )
    (root / "proc_version").write_text(
        "Linux version 5.15.153.1-microsoft-standard-WSL2\n"
        if wsl
        else "Linux version 6.8.0\n"
    )
    full_env = {
        **{k: v for k, v in os.environ.items() if k != "WSL_DISTRO_NAME"},
        "PATH": str(root / "bin"),
        "ROOT_FOR_STUB": str(root),
        **env,
    }
    if wsl:
        full_env["WSL_DISTRO_NAME"] = "Ubuntu"
    return subprocess.run(
        [BASH, "-c", f'source "{root}/scripts/lib.sh"\n{body}'],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(root: Path) -> list[str]:
    # lib.sh probes `docker compose version` when sourced; not under test.
    lines = (root / "calls").read_text().splitlines()
    return [c for c in lines if c != "docker compose version"]


def test_is_wsl_follows_distro_name(sandbox: Path) -> None:
    assert _bash(sandbox, "is_wsl", wsl=True).returncode == 0


def test_is_wsl_false_elsewhere(sandbox: Path) -> None:
    assert _bash(sandbox, "is_wsl", wsl=False).returncode != 0


def test_open_url_prefers_wslview_on_wsl(sandbox: Path) -> None:
    for name in ("wslview", "explorer.exe", "xdg-open", "open"):
        _stub(sandbox, name)
    done = _bash(sandbox, 'open_url "http://localhost:6988/"', wsl=True)
    assert done.returncode == 0
    assert _calls(sandbox) == ["wslview http://localhost:6988/"]


def test_open_url_falls_back_to_explorer_and_ignores_its_exit_code(
    sandbox: Path,
) -> None:
    _stub(sandbox, "explorer.exe")
    done = _bash(
        sandbox,
        'set -e; open_url "http://localhost:6988/"; echo ok',
        wsl=True,
        STUB_RC="1",
    )
    assert done.returncode == 0
    assert "ok" in done.stdout
    assert _calls(sandbox) == ["explorer.exe http://localhost:6988/"]


def test_open_url_survives_a_failing_launcher(sandbox: Path) -> None:
    _stub(sandbox, "xdg-open")
    done = _bash(
        sandbox,
        'set -e; open_url "http://localhost:6988/"; echo ok',
        wsl=True,
        STUB_RC="3",
    )
    assert done.returncode == 0
    assert "ok" in done.stdout
    assert done.stderr == ""
    assert _calls(sandbox) == ["xdg-open http://localhost:6988/"]


def test_open_url_never_calls_open_on_linux(sandbox: Path) -> None:
    # Debian and Ubuntu ship `open` as openvt.
    _stub(sandbox, "open")
    _stub(sandbox, "xdg-open")
    done = _bash(sandbox, 'open_url "http://localhost:6988/"', wsl=False)
    assert done.returncode == 0
    assert _calls(sandbox) == ["xdg-open http://localhost:6988/"]


def test_open_url_uses_open_on_macos(sandbox: Path) -> None:
    _stub(sandbox, "open")
    _stub(sandbox, "xdg-open")
    done = _bash(
        sandbox, 'open_url "http://localhost:6988/"', wsl=False, system="Darwin"
    )
    assert done.returncode == 0
    assert _calls(sandbox) == ["open http://localhost:6988/"]


def test_ensure_docker_on_wsl_asks_for_docker_desktop_without_sudo(
    sandbox: Path,
) -> None:
    _stub(sandbox, "systemctl", "#!/bin/bash\nexit 1\n")  # no docker.service
    _stub(sandbox, "sudo")
    done = _bash(sandbox, "ensure_docker", wsl=True)
    assert done.returncode == 0
    assert "Docker Desktop" in done.stderr
    assert not any(c.startswith(("sudo", "systemctl start")) for c in _calls(sandbox))


def test_ensure_docker_on_wsl_with_docker_service_still_starts_it(
    sandbox: Path,
) -> None:
    _stub(sandbox, "systemctl")  # `systemctl cat docker.service` succeeds
    done = _bash(sandbox, "ensure_docker", wsl=True)
    assert done.returncode == 0
    assert "systemctl start docker" in _calls(sandbox)


def test_ensure_docker_on_plain_linux_is_unchanged(sandbox: Path) -> None:
    _stub(sandbox, "systemctl")
    done = _bash(sandbox, "ensure_docker", wsl=False)
    assert done.returncode == 0
    assert "Docker Desktop" not in done.stderr
    assert "systemctl start docker" in _calls(sandbox)


def test_wsl_preflight_warns_about_the_windows_drive(sandbox: Path) -> None:
    done = _bash(sandbox, 'REPO_ROOT="/mnt/c/Users/me/vigil" wsl_preflight', wsl=True)
    assert done.returncode == 0
    assert "Linux home" in done.stderr


def test_wsl_preflight_is_quiet_in_the_linux_home_and_off_wsl(sandbox: Path) -> None:
    home = _bash(sandbox, 'REPO_ROOT="/home/me/vigil" wsl_preflight', wsl=True)
    off = _bash(sandbox, 'REPO_ROOT="/mnt/c/Users/me/vigil" wsl_preflight', wsl=False)
    assert (home.returncode, home.stderr) == (0, "")
    assert (off.returncode, off.stderr) == (0, "")


def test_failed_ollama_start_on_wsl_explains_and_does_not_fail(sandbox: Path) -> None:
    _stub(sandbox, "python3", "#!/bin/bash\nexit 1\n")
    done = _bash(sandbox, "set -e; ensure_ollama; echo survived", wsl=True)
    assert done.returncode == 0
    assert "survived" in done.stdout
    assert "Warning: Ollama not started" in done.stderr
    assert "WSL2" in done.stderr and "mirrored" in done.stderr


def test_failed_ollama_start_off_wsl_keeps_the_old_message(sandbox: Path) -> None:
    _stub(sandbox, "python3", "#!/bin/bash\nexit 1\n")
    done = _bash(sandbox, "ensure_ollama", wsl=False)
    assert done.returncode == 0
    assert done.stderr == "Warning: Ollama not started; see logs/ollama.log\n"


def test_ollama_install_hint_is_brew_only_on_macos(monkeypatch) -> None:
    pytest.importorskip("httpx")
    from core.platform import ollama_supervisor

    monkeypatch.setattr(ollama_supervisor.sys, "platform", "darwin")
    assert (
        ollama_supervisor._install_hint(sentence=False)
        == "Install with: brew install ollama"
    )
    assert "`brew install ollama`" in ollama_supervisor._install_hint(sentence=True)

    for platform in ("linux", "win32"):
        monkeypatch.setattr(ollama_supervisor.sys, "platform", platform)
        for sentence in (True, False):
            hint = ollama_supervisor._install_hint(sentence=sentence)
            assert "brew" not in hint
            assert "https://ollama.com/download" in hint
