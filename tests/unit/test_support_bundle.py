"""scripts/vigil-support/vigil-support.sh writes a safe bundle or nothing.

The real script and the real redact.awk run in a temp directory with stub
docker, curl, timedatectl, journalctl, dmesg and df on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh script"),
]

REPO = Path(__file__).resolve().parents[2]
SUPPORT = REPO / "scripts" / "vigil-support"
SCRIPT = SUPPORT / "vigil-support.sh"

PLANTED = "hunter2hunter2hunter2"

# Each stub reads its output from an environment variable so a test sets the
# scenario without rewriting the script.
DOCKER_STUB = r"""
case "$1" in
ps)
    case "$*" in
    *" -a"*) printf "%s" "${FAKE_DOCKER_PS_A:-${FAKE_DOCKER_PS:-}}" ;;
    *) printf "%s" "${FAKE_DOCKER_PS:-}" ;;
    esac ;;
compose) printf "%s\n" "${FAKE_COMPOSE_CONFIG-services: none}" ;;
logs)
    for last; do :; done
    case " ${FAKE_DOCKER_LOGS_FAIL:-} " in
    *" $last "*) echo "Error: No such container: $last" >&2; exit 1 ;;
    esac
    prev=; for a; do [ "$prev" = --since ] && since=$a; prev=$a; done
    echo "log of $last since=$since"; printf "%s\n" "${FAKE_LOG_LINE:-}" ;;
inspect)
    case "$*" in
    *.Mounts*) printf "%b" "${FAKE_DOCKER_MOUNTS-/app/data\n/home/vigil/.vigil\n}" ;;
    *) echo "$(for last; do :; done; echo "$last") state=running restarts=0" ;;
    esac ;;
cp)
    src=${2#*:}; f=${src##*/}
    case "$src" in
    /home/vigil/.vigil/*) ;;
    *) echo "Error response from daemon: Could not find the file $src in container ${2%%:*}" >&2; exit 1 ;;
    esac
    if [ ! -e "$FAKE_CP_DIR/$f" ] && [ ! -L "$FAKE_CP_DIR/$f" ]; then
        echo "Error response from daemon: Could not find the file $src in container ${2%%:*}" >&2; exit 1
    fi
    exec tar -cf - -C "$FAKE_CP_DIR" "$f" ;;
esac
"""

CURL_STUB = r"""
case "$*" in
*:9091/*) echo "curl: (7) Failed to connect to localhost port 9091" >&2; exit 7 ;;
esac
echo '{"status": "healthy", "version": "9.9.9"}'
"""

STUBS = {
    "docker": DOCKER_STUB,
    "curl": CURL_STUB,
    "timedatectl": f'echo "POSTGRES_PASSWORD={PLANTED}"; echo "NTP service: active"',
    "journalctl": 'eval "${FAKE_JOURNAL:-echo journal-line}"',
    "log": "echo mac-log-line",
    "dmesg": 'eval "${FAKE_DMESG:-echo kernel-line}"',
    "df": 'if [ -n "${FAKE_DF:-}" ]; then eval "$FAKE_DF"; else exec /bin/df "$@"; fi',
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    (tmp_path / "tmp").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "home").mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path / "tmp"),
        "HOME": str(tmp_path / "home"),
        "FAKE_DOCKER_PS": "",
    }


def run(env, tmp_path, *args, timeout=60):
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        cwd=tmp_path / "out",
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def bundles(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "out").glob("vigil-support-*"))


def unpack(path: Path, dest: Path) -> Path:
    with tarfile.open(path) as tar:
        tar.extractall(dest)
    (root,) = dest.iterdir()
    return root


def manifest_of(tmp_path: Path) -> dict:
    (bundle,) = bundles(tmp_path)
    root = unpack(bundle, tmp_path / "x")
    return json.loads((root / "manifest.json").read_text())


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_for(path: Path, seconds: float = 10) -> None:
    deadline = time.time() + seconds
    while not path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert path.exists()


def test_host_only_bundle(env, tmp_path):
    proc = run(env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    (bundle,) = bundles(tmp_path)
    assert bundle.name.startswith("vigil-support-host-")
    assert oct(bundle.stat().st_mode & 0o777) == "0o600"
    # notice at start and end, then path, size, SHA-256
    assert proc.stdout.count("DATA NOTICE") == 2
    assert str(bundle) in proc.stdout and "SHA-256: " in proc.stdout
    assert list((tmp_path / "tmp").iterdir()) == []

    root = unpack(bundle, tmp_path / "x")
    for part in ("configuration", "health", "logs", "system"):
        assert (root / part).is_dir()
    assert "no Vigil install found" in (root / "SUMMARY.txt").read_text()
    for path in root.rglob("*"):
        if path.is_file():
            path.read_text(encoding="utf-8")  # plain text only

    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["format_version"] == 1 and manifest["mode"] == "host"
    assert manifest["install"]["found"] is False and manifest["looked"]
    paths = [e["path"] for e in manifest["entries"]]
    assert len(paths) == len(set(paths))
    for entry in manifest["entries"]:
        assert entry["state"] in ("collected", "not collected")
        assert entry["reason"]
    states = {e["path"]: e["state"] for e in manifest["entries"]}
    assert states["configuration/"] == "not collected"
    assert states["system/host.txt"] == "collected"


def test_planted_secret_is_redacted_by_the_real_filter(env, tmp_path):
    assert run(env, tmp_path).returncode == 0
    (bundle,) = bundles(tmp_path)
    root = unpack(bundle, tmp_path / "x")
    for path in root.rglob("*"):
        if path.is_file():
            assert PLANTED not in path.read_text()
    assert "[REDACTED]" in (root / "system" / "timezone-sync.txt").read_text()
    counts = {e["path"]: e.get("redactions") for e in manifest_of(tmp_path)["entries"]}
    assert counts["system/timezone-sync.txt"] >= 1


def test_two_installs_write_nothing(env, tmp_path):
    env["FAKE_DOCKER_PS"] = "deeptempo-backend|docker\nvigil-backend-1|vigil\n"
    proc = run(env, tmp_path)
    assert proc.returncode == 1
    assert bundles(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []
    assert "deeptempo-backend" in proc.stdout and "vigil-backend-1" in proc.stdout
    assert "--mode" in proc.stdout

    # --mode picks one of them
    proc = run(env, tmp_path, "--mode", "desktop")
    assert proc.returncode == 0, proc.stderr
    manifest = manifest_of(tmp_path)
    assert manifest["mode"] == "desktop" and manifest["install"]["found"] is True
    assert manifest["install"]["api_version"] == "9.9.9"
    (bundle,) = bundles(tmp_path)
    root = unpack(bundle, tmp_path / "x")
    assert "/api/health reports 9.9.9" in (root / "SUMMARY.txt").read_text()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_leaves_nothing_behind(env, tmp_path, sig):
    pidfile = tmp_path / "sleep.pid"
    env["FAKE_JOURNAL"] = f"echo $$ > {pidfile}; exec sleep 60"
    env["FAKE_DMESG"] = env["FAKE_JOURNAL"]
    proc = subprocess.Popen(
        ["sh", str(SCRIPT)], cwd=tmp_path / "out", env=env, stdout=subprocess.DEVNULL
    )
    wait_for(pidfile)
    helper = int(pidfile.read_text())
    proc.send_signal(sig)
    assert proc.wait(timeout=15) != 0
    deadline = time.time() + 5
    while alive(helper) and time.time() < deadline:
        time.sleep(0.05)
    assert not alive(helper)
    assert bundles(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_insufficient_space_writes_nothing(env, tmp_path):
    env["FAKE_DF"] = (
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'; "
        "echo '/dev/x 1000 900 100 90% /'"
    )
    proc = run(env, tmp_path)
    assert proc.returncode == 1
    assert "not enough free space" in proc.stderr
    assert bundles(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_simultaneous_runs_get_distinct_names(env, tmp_path):
    env["VIGIL_SUPPORT_NOW"] = "20261005T120000Z"
    procs = [
        subprocess.Popen(
            ["sh", str(SCRIPT)],
            cwd=tmp_path / "out",
            env=env,
            stdout=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    names = {b.name for b in bundles(tmp_path)}
    assert len(names) == 2
    assert all(n.startswith("vigil-support-host-") for n in names)


def test_slow_source_times_out_and_the_rest_is_kept(env, tmp_path):
    env["FAKE_DMESG"] = "exec sleep 30"
    env["VIGIL_SUPPORT_LOG_SECS"] = "1"
    started = time.time()
    assert run(env, tmp_path).returncode == 0
    assert time.time() - started < 20
    entries = {e["path"]: e for e in manifest_of(tmp_path)["entries"]}
    assert entries["system/kernel.txt"]["state"] == "not collected"
    assert "timed out" in entries["system/kernel.txt"]["reason"]
    assert entries["system/host.txt"]["state"] == "collected"


def test_source_over_its_ceiling_is_cut_to_the_newest(env, tmp_path):
    env["FAKE_DMESG"] = "seq 1 500"
    env["VIGIL_SUPPORT_SOURCE_MAX"] = "100"
    assert run(env, tmp_path).returncode == 0
    entry = {e["path"]: e for e in manifest_of(tmp_path)["entries"]}[
        "system/kernel.txt"
    ]
    assert entry["state"] == "collected" and entry["bytes_cut"] > 0
    root = next((tmp_path / "x").iterdir())
    kernel = (root / "system" / "kernel.txt").read_text()
    assert len(kernel) <= 100 and kernel.rstrip().endswith("500")


def test_cut_inside_a_private_key_body_leaks_nothing(env, tmp_path):
    # 84 bytes; a 60-byte ceiling keeps the tail of the key body but not its BEGIN
    env["FAKE_DMESG"] = (
        "printf '%s\\n' xxxxxxxxxxxxxxxx SECRETKEYBODYSECRETKEYBODY "
        "'-----END RSA PRIVATE KEY-----' tail-line"
    )
    env["VIGIL_SUPPORT_SOURCE_MAX"] = "60"
    assert run(env, tmp_path).returncode == 0
    manifest_of(tmp_path)
    root = next((tmp_path / "x").iterdir())
    kernel = (root / "system" / "kernel.txt").read_text()
    assert "KEYBODY" not in kernel and "tail-line" in kernel


def test_symlink_leaving_its_location_is_recorded_not_followed(env, tmp_path):
    fs = tmp_path / "root"
    (fs / "etc").mkdir(parents=True)
    (fs / "outside").mkdir()
    (fs / "outside" / "secret").write_text("PRETTY_NAME=outside\n")
    (fs / "etc" / "os-release").symlink_to("../outside/secret")
    env["VIGIL_SUPPORT_FS_ROOT"] = str(fs)
    assert run(env, tmp_path).returncode == 0
    entry = {e["path"]: e for e in manifest_of(tmp_path)["entries"]}[
        "system/os-release.txt"
    ]
    assert entry["state"] == "not collected" and "symlink" in entry["reason"]
    root = next((tmp_path / "x").iterdir())
    assert not (root / "system" / "os-release.txt").exists()


def test_version_file_follows_the_release():
    release = (REPO / "VERSION").read_text().split()[0]
    assert (SUPPORT / "VERSION").read_text().split()[0] == release
    config = json.loads((REPO / ".github" / "release-please-config.json").read_text())
    extra = config["packages"]["."]["extra-files"]
    assert {"type": "generic", "path": "scripts/vigil-support/VERSION"} in extra
    assert "x-release-please-version" in (SUPPORT / "VERSION").read_text()


ENV_SECRET = "Zq9-env-secret-value"
CFG_SECRET = "Kd4-config-secret-value"
NEVER = "NEVERINCLUDEDCONTENT"


def install(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with .env, logs and config, and a State Directory with the
    files that must show existence only."""
    checkout, state = tmp_path / "checkout", tmp_path / "state"
    (checkout / "infra" / "docker").mkdir(parents=True)
    (checkout / "infra" / "docker" / "docker-compose.yml").write_text("services: {}\n")
    (checkout / "logs" / "containers").mkdir(parents=True)
    (checkout / "logs" / "backend.log").write_text(f"started; saw {ENV_SECRET}\n")
    (checkout / "logs" / "backend.log.1").write_text("older\n")
    (
        checkout / "logs" / "containers" / "deeptempo-redis-20261005T000000Z.log"
    ).write_text("r\n")
    (checkout / ".env").write_text(f"POSTGRES_PASSWORD={ENV_SECRET}\nDEV_MODE=false\n")
    (checkout / "mcp-config.json").write_text("{}\n")
    (checkout / "INTENT.md").write_text("---\nmode: x\n---\n")
    (checkout / ".vigil-autostart").write_text("postgres\n")
    state.mkdir()
    for name in ("secrets.enc", "master.key", "jwt_secret", ".env"):
        (state / name).write_text(NEVER)
    (state / "backups.json").write_text(
        '[{"name": "a", "repo": "' + str(tmp_path / "repo") + '"}]'
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "data").write_text(NEVER)
    (state / "vigil.log").write_text("state log\n")
    return checkout, state


def entries_of(tmp_path: Path) -> dict:
    return {e["path"]: e for e in manifest_of(tmp_path)["entries"]}


def everything(tmp_path: Path, proc) -> str:
    """The tarball's own bytes, every unpacked file, and the run's output."""
    (bundle,) = bundles(tmp_path)
    root = next((tmp_path / "x").iterdir())
    parts = [bundle.read_bytes().decode("latin-1"), proc.stdout, proc.stderr]
    parts += [p.read_text() for p in root.rglob("*") if p.is_file()]
    return "\n".join(parts)


def test_compose_install_is_collected_and_secrets_stay_out(env, tmp_path):
    checkout, state = install(tmp_path)
    env["VIGIL_REPO_ROOT"] = str(checkout)
    env["FAKE_DOCKER_PS"] = (
        "deeptempo-backend|docker|/x/docker-compose.yml\ndeeptempo-redis|docker|\n"
    )
    env["FAKE_DOCKER_PS_A"] = (
        "deeptempo-backend|docker|running|backend\ndeeptempo-redis|docker|exited|redis\n"
        "deeptempo-splunk|docker|running|splunk\ndeeptempo-misp-core|docker|exited|\n"
        "ollama|other|running\nunrelated|other|running\n"
    )
    env["FAKE_COMPOSE_CONFIG"] = (
        f"services:\n  backend:\n    environment:\n      SERVICE_API_KEY: {CFG_SECRET}"
    )
    env["FAKE_LOG_LINE"] = f"free text {ENV_SECRET} and {CFG_SECRET}"
    env["FAKE_DOCKER_LOGS_FAIL"] = "deeptempo-redis"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr

    entries = entries_of(tmp_path)
    assert manifest_of(tmp_path)["mode"] == "compose"
    collected = {p for p, e in entries.items() if e["state"] == "collected"}
    assert {
        "configuration/env",
        "configuration/compose-config.yml",
        "configuration/state/backups.json",
        "configuration/state/mcp-config.json",
        "configuration/state/INTENT.md",
        "configuration/state/vigil-autostart",
        "configuration/deployment/docker-compose.yml",
        "health/api.json",
        "health/containers.txt",
        "logs/checkout/backend.log",
        "logs/checkout/backend.log.1",
        "logs/checkout/containers/deeptempo-redis-20261005T000000Z.log",
        "logs/state/vigil.log",
        "logs/docker/deeptempo-backend.log",
    } <= collected

    # unreachable endpoint, absent container, excluded containers: reasons, exit 0
    assert entries["health/daemon-health.json"]["state"] == "not collected"
    assert "Failed to connect" in entries["health/daemon-health.json"]["reason"]
    assert "No such container" in entries["logs/docker/deeptempo-redis.log"]["reason"]
    for name in ("deeptempo-splunk", "deeptempo-misp-core", "ollama"):
        entry = entries[f"logs/docker/{name}.log"]
        assert entry["state"] == "not collected"
        assert entry["reason"].startswith("excluded: lab/demo container; state ")
    assert "logs/docker/unrelated.log" not in entries
    containers = (
        next((tmp_path / "x").iterdir()) / "health" / "containers.txt"
    ).read_text()
    assert "deeptempo-backend" in containers and "splunk" not in containers

    # never included: existence only
    for label in ("secrets.enc", "master.key", "jwt_secret", "state-dir-env"):
        entry = entries[f"configuration/never-included/{label}"]
        assert (
            entry["state"] == "not collected"
            and entry["reason"] == "never included; present"
        )
    assert (
        entries["configuration/never-included/home-deeptempo-env"]["reason"]
        == "never included; absent"
    )
    assert (
        entries["configuration/never-included/backup-repository-1"]["reason"]
        == "never included; present"
    )

    blob = everything(tmp_path, proc)
    assert NEVER not in blob
    for secret in (ENV_SECRET, CFG_SECRET):
        assert secret not in blob
    root = next((tmp_path / "x").iterdir())
    assert (
        "[REDACTED]" in (root / "logs" / "docker" / "deeptempo-backend.log").read_text()
    )
    assert not (root / "configuration" / "state" / "secrets.enc").exists()


def test_native_install_collects_dependency_container_logs(env, tmp_path):
    checkout, state = install(tmp_path)
    env["VIGIL_REPO_ROOT"] = str(checkout)
    names = ("deeptempo-postgres", "deeptempo-redis", "deeptempo-bifrost")
    env["FAKE_DOCKER_PS"] = "".join(f"{n}|docker|\n" for n in names)
    env["FAKE_DOCKER_PS_A"] = "".join(f"{n}|docker|running|\n" for n in names)
    env["FAKE_LOG_LINE"] = f"free text {ENV_SECRET} and POSTGRES_PASSWORD={PLANTED}"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    assert manifest_of(tmp_path)["mode"] == "native"
    assert entries["configuration/compose-config.yml"]["state"] == "not collected"
    for path in (
        "logs/checkout/backend.log",
        "logs/checkout/containers/deeptempo-redis-20261005T000000Z.log",
        "logs/state/vigil.log",
        *(f"logs/docker/{n}.log" for n in names),
    ):
        assert entries[path]["state"] == "collected", path
    root = next((tmp_path / "x").iterdir())
    log = (root / "logs" / "docker" / "deeptempo-postgres.log").read_text()
    assert "since=168h" in log and "[REDACTED]" in log
    blob = everything(tmp_path, proc)
    assert ENV_SECRET not in blob and PLANTED not in blob


def container_state(tmp_path: Path, env, **files: str) -> Path:
    """The stub backend container's State Directory, served by `docker cp`."""
    cp = tmp_path / "container-state"
    cp.mkdir()
    for name, text in files.items():
        (cp / name).write_text(text)
    env["FAKE_CP_DIR"] = str(cp)
    return cp


BACKUPS = '[{"name": "a", "repo": "/vol/repo", "password": "' + PLANTED + '"}]'
STATE_ENTRIES = (
    "configuration/state/backups.json",
    "configuration/state/detection_sources.json",
    "logs/state/vigil.log",
)


def compose_env(tmp_path: Path, env, backend_row: str):
    checkout, _ = install(tmp_path)
    env["VIGIL_REPO_ROOT"] = str(checkout)
    env["FAKE_DOCKER_PS"] = "deeptempo-backend|docker|\n"
    env["FAKE_DOCKER_PS_A"] = backend_row
    container_state(
        tmp_path,
        env,
        **{
            "backups.json": BACKUPS,
            "detection_sources.json": "{}\n",
            "vigil.log": f"state log {ENV_SECRET}\n",
        },
    )


def test_compose_reads_the_state_directory_from_the_backend_container(env, tmp_path):
    compose_env(tmp_path, env, "deeptempo-backend|docker|exited|backend\n")
    proc = run(env, tmp_path)  # no --state-dir: the container, not ~/.vigil
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    for path in STATE_ENTRIES:
        assert entries[path]["state"] == "collected", path
        assert entries[path]["source"].startswith("docker cp deeptempo-backend:")
    root = next((tmp_path / "x").iterdir())
    assert "[REDACTED]" in (root / "configuration/state/backups.json").read_text()
    assert "state log" in (root / "logs/state/vigil.log").read_text()
    # repositories named by the copied file are listed, never checked on the host
    never = entries["configuration/never-included/backup-repository-1"]
    assert never["reason"].startswith("never included; not checked")
    blob = everything(tmp_path, proc)
    assert PLANTED not in blob and ENV_SECRET not in blob


def test_desktop_finds_its_backend_by_service_label_and_mount(env, tmp_path):
    standalone = tmp_path / "state" / "standalone"
    standalone.mkdir(parents=True)
    (standalone / "docker-compose.yml").write_text("name: vigil\n")
    env["FAKE_DOCKER_PS"] = f"vigil-backend-1|vigil|{standalone}/docker-compose.yml\n"
    env["FAKE_DOCKER_PS_A"] = (
        "vigil-postgres-1|vigil|running|postgres\nvigil-backend-1|vigil|running|backend\n"
    )
    container_state(tmp_path, env, **{"backups.json": BACKUPS, "vigil.log": "app\n"})
    env["FAKE_DOCKER_MOUNTS"] = "/elsewhere\n/home/vigil/.vigil\n"
    proc = run(env, tmp_path, "--mode", "desktop")
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    for path in ("configuration/state/backups.json", "logs/state/vigil.log"):
        assert entries[path]["state"] == "collected", path
        assert "vigil-backend-1" in entries[path]["source"]
    # a file missing in the container is not collected, with docker's message
    entry = entries["configuration/state/detection_sources.json"]
    assert entry["state"] == "not collected" and "Could not find" in entry["reason"]
    assert PLANTED not in everything(tmp_path, proc)


def test_state_directory_not_found_is_recorded_per_file(env, tmp_path):
    # no backend container
    compose_env(tmp_path, env, "deeptempo-redis|docker|running|redis\n")
    assert run(env, tmp_path).returncode == 0
    entries = entries_of(tmp_path)
    for path in STATE_ENTRIES:
        assert entries[path]["state"] == "not collected", path
        assert "no backend container" in entries[path]["reason"]


def test_backend_without_a_state_mount_is_recorded(env, tmp_path):
    compose_env(tmp_path, env, "deeptempo-backend|docker|running|backend\n")
    env["FAKE_DOCKER_MOUNTS"] = "/app/data\n"
    assert run(env, tmp_path).returncode == 0
    entry = entries_of(tmp_path)["logs/state/vigil.log"]
    assert (
        entry["state"] == "not collected"
        and "no State Directory mount" in entry["reason"]
    )


def test_container_symlink_or_directory_is_never_followed(env, tmp_path):
    compose_env(tmp_path, env, "deeptempo-backend|docker|running|backend\n")
    cp = Path(env["FAKE_CP_DIR"])
    (cp / "vigil.log").unlink()
    (cp / "vigil.log").symlink_to("/etc/hostname")
    (cp / "backups.json").unlink()
    (cp / "backups.json").mkdir()
    (cp / "backups.json" / "inner").write_text(NEVER)
    proc = run(env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    for path in ("logs/state/vigil.log", "configuration/state/backups.json"):
        assert entries[path]["state"] == "not collected", path
        assert "not a regular file" in entries[path]["reason"]
    assert NEVER not in everything(tmp_path, proc)


def test_state_dir_flag_makes_the_host_copy_win(env, tmp_path):
    compose_env(tmp_path, env, "deeptempo-backend|docker|running|backend\n")
    _, state = install(tmp_path / "host")
    (state / "detection_sources.json").write_text("{}\n")
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    for path in STATE_ENTRIES:
        assert entries[path]["state"] == "collected", path
        assert entries[path]["source"].startswith(str(state)), path
    root = next((tmp_path / "x").iterdir())
    assert (root / "logs/state/vigil.log").read_text() == "state log\n"


def test_desktop_install_reads_the_app_log_directory(env, tmp_path):
    state = tmp_path / "state"
    (state / "logs" / "containers").mkdir(parents=True)
    (state / "logs" / "vigil-desktop.log").write_text("app\n")
    (state / "logs" / "vigil-desktop.log.1").write_text("older app\n")
    (state / "logs" / "containers" / "vigil-20261005T000000Z.log").write_text("snap\n")
    (state / "logs" / "other.log").write_text("not ours\n")
    (state / "config.json").write_text(NEVER)
    standalone = state / "standalone"
    standalone.mkdir()
    (standalone / "docker-compose.yml").write_text("name: vigil\n")
    (state / "vigil.log").write_text("host state log\n")
    env["FAKE_DOCKER_PS"] = f"vigil-backend-1|vigil|{standalone}/docker-compose.yml\n"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    assert manifest_of(tmp_path)["mode"] == "desktop"
    assert entries["logs/state/vigil.log"]["state"] == "collected"
    for path in (
        "logs/desktop/vigil-desktop.log",
        "logs/desktop/vigil-desktop.log.1",
        "logs/desktop/containers/vigil-20261005T000000Z.log",
        "logs/docker/vigil-backend-1.log",
        "configuration/compose-config.yml",
        "configuration/deployment/docker-compose.yml",
    ):
        assert entries[path]["state"] == "collected", path
    assert "logs/desktop/other.log" not in entries
    assert (
        entries["configuration/never-included/desktop-config"]["reason"]
        == "never included; present"
    )
    assert NEVER not in everything(tmp_path, proc)


def test_summary_lists_never_included_apart_from_not_collected(env, tmp_path):
    checkout, state = install(tmp_path)
    env["VIGIL_REPO_ROOT"] = str(checkout)
    env["FAKE_DOCKER_PS"] = "deeptempo-postgres|docker|\n"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    root = unpack(bundles(tmp_path)[0], tmp_path / "x")
    summary = (root / "SUMMARY.txt").read_text()
    head, _, never = summary.partition("Never included (existence only)")
    assert never and "never-included/secrets.enc: never included; present" in never
    assert "never included" not in head
    assert "compose-config.yml" in head
    # the final output splits the same way, and the manifest still has every entry
    out_head, _, out_never = proc.stdout.partition("Never included (existence only):")
    assert "never included" not in out_head and "secrets.enc" in out_never
    entries = entries_of(tmp_path)
    assert (
        entries["configuration/never-included/secrets.enc"]["state"] == "not collected"
    )


def test_learning_skips_comments_and_usernames_and_respects_word_edges(env, tmp_path):
    checkout, state = install(tmp_path)
    (checkout / ".env").write_text(
        '# ELASTIC_SIEM_USERNAME="elastic"\nELASTIC_SIEM_USERNAME="elastic"\n'
        'SPLUNK_USERNAME="admin"\nCRIBL_USERNAME="monitor"\n'
        f"POSTGRES_PASSWORD={ENV_SECRET}\nDEV_MODE=false\n"
    )
    survivors = [
        "docker.elastic.co/elasticsearch/elasticsearch:8.15.0",
        "deeptempo-elasticsearch",
        "elastic_data:",
        '"elastic": {',
        "core/integrations/elastic/tool.py",
        "worker monitor reported 3 events",
        f"{ENV_SECRET}x",
    ]
    log_line = "\n".join(survivors + [f"auth failed with {ENV_SECRET}"])
    env["VIGIL_REPO_ROOT"] = str(checkout)
    env["FAKE_DOCKER_PS"] = "deeptempo-backend|docker|/x/docker-compose.yml\n"
    env["FAKE_DOCKER_PS_A"] = "deeptempo-backend|docker|running\n"
    env["FAKE_LOG_LINE"] = log_line
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    manifest_of(tmp_path)
    root = next((tmp_path / "x").iterdir())
    logged = (root / "logs" / "docker" / "deeptempo-backend.log").read_text()
    assert logged.splitlines()[1:] == survivors + ["auth failed with [REDACTED]"]
    assert ENV_SECRET not in everything(tmp_path, proc).replace(f"{ENV_SECRET}x", "")
    assert (root / "configuration" / "env").read_text().splitlines()[:4] == [
        '# ELASTIC_SIEM_USERNAME="[REDACTED]"',
        'ELASTIC_SIEM_USERNAME="[REDACTED]"',
        'SPLUNK_USERNAME="[REDACTED]"',
        'CRIBL_USERNAME="[REDACTED]"',
    ]


# --- Helm ---------------------------------------------------------------------

BASE64_VALUE = "c3VwZXItc2VjcmV0LXZhbHVl"
HELM_LIST = (
    '[{"name":"vigil","namespace":"vigil","revision":"3","status":"deployed",'
    '"chart":"vigil-0.6.0","app_version":"0.6.0"},'
    '{"name":"grafana","namespace":"mon","revision":"1","status":"deployed",'
    '"chart":"grafana-8.0.0","app_version":"11.0.0"}]'
)
HELM_VALUES = (
    "secrets:\n  postgresPassword: {p}\n  jwtSecretKey: {p}jwt\n"
    "postgresql:\n  auth:\n    existingSecret: vigil-prod-secrets\n"
    "    existingSecretKey: POSTGRES_PASSWORD\n"
    "redis:\n  bitnami:\n    auth:\n      existingSecretPasswordKey: redis-password\n"
).format(p=PLANTED)
FORBIDDEN = (
    'Error from server (Forbidden): pods is forbidden: User "ann" cannot list '
    'resource "pods" in API group "" in the namespace "vigil"'
)

KUBECTL_STUB = r"""
echo "kubectl $*" >>"$FAKE_KUBE_LOG"
case " $* " in
*" version "*) echo "Client Version: v1.31.0"; exit 0 ;;
*" port-forward "*)
    [ "${FAKE_PF_HANG:-}" = 1 ] || echo "Forwarding from 127.0.0.1:41234 -> 6987"
    exec sleep "$FAKE_PF_MARKER" ;;
*" get pods "* | *" describe pods "*)
    if [ -n "${FAKE_FORBID_PODS:-}" ]; then echo "$FAKE_FORBID_PODS" >&2; exit 1; fi ;;
esac
case " $* " in
*" get pods "*custom-columns*) printf '%s\n' "$FAKE_PODS" ;;
*" get pods "*" -o wide"*) printf '%s\n' "$FAKE_WIDE" ;;
*" get pods "*) echo "NAME READY STATUS"; printf '%s\n' "$FAKE_PODS" ;;
*" describe pods "*) echo "Name: pod"; echo "Environment: DB_PASSWORD=$FAKE_PLANTED" ;;
*" get secrets "*)
    case "$*" in
    *go-template*) printf '%s\n' "vigil-prod-secrets Opaque POSTGRES_PASSWORD ANTHROPIC_API_KEY" ;;
    *) printf '%s\n' "vigil-prod-secrets Opaque POSTGRES_PASSWORD: $FAKE_B64" ;;
    esac ;;
*" get configmaps "*) printf '%s\n' vigil-config vigil-db-init-sql ;;
*" get configmap "*) printf 'kind: ConfigMap\ndata:\n  LOG_LEVEL: info\n' ;;
*" get service "*) echo vigil-backend ;;
*" get deploy"*) echo "NAME READY"; echo "deployment.apps/vigil-backend 2/2" ;;
*" get events "*) echo "LAST SEEN TYPE REASON"; echo "1m Warning BackOff" ;;
*" logs "*)
    pod= c= prev=0
    while [ $# -gt 0 ]; do
        case $1 in
        logs) pod=$2; shift ;;
        -c) c=$2; shift ;;
        --previous) prev=1 ;;
        esac
        shift
    done
    if [ $prev = 1 ]; then
        case " ${FAKE_PREVIOUS:-} " in
        *" $pod/$c "*) echo "before restart: $pod/$c"; exit 0 ;;
        esac
        echo "Error from server (BadRequest): previous terminated container \"$c\" in pod \"$pod\" not found" >&2
        exit 1
    fi
    echo "log of $pod/$c"; printf '%s\n' "${FAKE_LOG_LINE:-}" ;;
esac
"""

HELM_STUB = r"""
echo "helm $*" >>"$FAKE_KUBE_LOG"
case "$1" in
list)
    if [ -n "${FAKE_HELM_LIST_ERR:-}" ]; then echo "$FAKE_HELM_LIST_ERR" >&2; exit 1; fi
    printf '%s' "$FAKE_HELM_LIST" ;;
get) case "$*" in *--all*) printf '%s\n' "$FAKE_VALUES" "defaultsOnly: true" ;; *) printf '%s\n' "$FAKE_VALUES" ;; esac ;;
version) echo 'version.BuildInfo{Version:"v3.17.0"}' ;;
esac
"""


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) + 3 for i in range(len(rows[0]))]
    return "\n".join(
        "".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows
    )


@pytest.fixture(scope="module")
def chart_pods(tmp_path_factory) -> list[dict]:
    """Pods of the real chart (plus its lab pods), from `helm template`."""
    yaml = pytest.importorskip("yaml")
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    # The subcharts are off by default and are not in a clean checkout, so render
    # a copy without the dependency declaration.
    chart = tmp_path_factory.mktemp("chart") / "vigil"
    shutil.copytree(
        REPO / "infra" / "helm" / "vigil",
        chart,
        ignore=shutil.ignore_patterns("charts", "Chart.lock"),
    )
    meta = (chart / "Chart.yaml").read_text()
    (chart / "Chart.yaml").write_text(meta[: meta.index("\ndependencies:")] + "\n")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "vigil",
            str(chart),
            "--namespace",
            "vigil",
            "--set",
            "pgadmin.enabled=true",
            "--set",
            "splunk.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pods = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "Job"):
            continue
        labels = doc["metadata"]["labels"]
        assert labels["app.kubernetes.io/instance"] == "vigil"
        spec = doc["spec"]["template"]["spec"]
        name = doc["metadata"]["name"]
        replicas = {"Deployment": 2, "StatefulSet": 1, "Job": 1}[doc["kind"]]
        for i in range(replicas):
            suffix = f"{i}" if doc["kind"] == "StatefulSet" else f"7d9c5b6f4-x{i}k8p"
            pods.append(
                {
                    "name": f"{name}-{suffix}",
                    "component": labels["app.kubernetes.io/component"],
                    "init": [c["name"] for c in spec.get("initContainers", [])],
                    "containers": [c["name"] for c in spec["containers"]],
                }
            )
    assert {"backend", "pgadmin", "splunk", "postgres", "redis"} <= {
        p["component"] for p in pods
    }
    return pods


@pytest.fixture
def helm_env(env, tmp_path, chart_pods):
    for name, body in {"kubectl": KUBECTL_STUB, "helm": HELM_STUB}.items():
        stub = tmp_path / "bin" / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env.update(
        FAKE_KUBE_LOG=str(tmp_path / "kube.log"),
        FAKE_HELM_LIST=HELM_LIST,
        FAKE_VALUES=HELM_VALUES,
        FAKE_PLANTED=PLANTED,
        FAKE_B64=BASE64_VALUE,
        FAKE_PF_MARKER=str(600000 + os.getpid() % 100000),
        FAKE_LOG_LINE=f"connecting with {PLANTED}",
        FAKE_PODS=_table(
            [
                [
                    p["name"],
                    p["component"],
                    "Running",
                    ",".join(p["init"]) or "<none>",
                    ",".join(p["containers"]),
                ]
                for p in chart_pods
            ]
        ),
        FAKE_WIDE=_table(
            [
                "NAME READY STATUS RESTARTS AGE IP NODE NOMINATED_NODE READINESS_GATES".split()
            ]
            + [
                [p["name"], "1/1", "Running", "1 (5m ago)", "2d", f"10.0.0.{i}"]
                + [f"node-{'ab'[i % 2]}", "<none>", "<none>"]
                for i, p in enumerate(chart_pods)
            ]
        )
        .replace("NOMINATED_NODE", "NOMINATED NODE")
        .replace("READINESS_GATES", "READINESS GATES"),
    )
    (tmp_path / "kube.log").write_text("")
    return env


def helm_run(env, tmp_path, *args, **kw):
    return run(env, tmp_path, "--mode", "helm", *args, **kw)


def marker_pids(marker: str) -> list[int]:
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if marker.encode() in cmdline.read_bytes().split(b"\0"):
                found.append(int(cmdline.parent.name))
        except OSError:
            pass
    return [p for p in found if alive(p)]


def test_helm_release_is_collected_and_secrets_stay_out(helm_env, tmp_path, chart_pods):
    helm_env["FAKE_PREVIOUS"] = "vigil-daemon-0/daemon"
    proc = helm_run(helm_env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    manifest = manifest_of(tmp_path)
    entries = {e["path"]: e for e in manifest["entries"]}
    root = next((tmp_path / "x").iterdir())
    assert manifest["mode"] == "helm" and manifest["install"]["found"] is True
    assert "Helm release vigil, namespace vigil, chart vigil-0.6.0" in (
        manifest["install"]["description"]
    )
    assert manifest["install"]["api_version"] == "9.9.9"  # from the forwarded answer
    assert not any("docker" in line for line in manifest["looked"])

    for path in (
        "configuration/helm-values.yaml",
        "configuration/helm-values-all.yaml",
        "configuration/kubernetes-secrets.txt",
        "configuration/configmaps/vigil-config.yaml",
        "configuration/configmaps/vigil-db-init-sql.yaml",
        "health/pods.txt",
        "health/workloads.txt",
        "health/pods-describe.txt",
        "health/api.json",
        "system/events.txt",
        "system/nodes.txt",
        "system/tools.txt",
        "system/host.txt",
    ):
        assert entries[path]["state"] == "collected", (path, entries.get(path))
    for name in ("journal", "syslog", "kernel", "processes", "disk", "timezone-sync"):
        entry = entries[f"system/{name}.txt"]
        assert entry["reason"] == "Helm: the host is not the install"
    assert entries["health/daemon-health.json"]["reason"].startswith(
        "Helm: only /api/health"
    )
    assert (root / "system" / "nodes.txt").read_text().split() == ["node-a", "node-b"]

    # every replica's every container, init containers too; lab pods excluded
    for pod in chart_pods:
        if pod["component"] in ("pgadmin", "splunk"):
            entry = entries[f"logs/pods/{pod['name']}/"]
            assert entry["reason"].startswith("excluded: lab/demo component ")
            assert not any(
                p.startswith(f"logs/pods/{pod['name']}/") and e["state"] == "collected"
                for p, e in entries.items()
            )
            continue
        for container in pod["init"] + pod["containers"]:
            base = f"logs/pods/{pod['name']}/{container}"
            assert entries[f"{base}.log"]["state"] == "collected", base
            if f"{pod['name']}/{container}" == "vigil-daemon-0/daemon":
                assert entries[f"{base}.previous.log"]["state"] == "collected"
                assert "before restart" in (root / f"{base}.previous.log").read_text()
            else:
                assert (
                    entries[f"{base}.previous.log"]["reason"] == "no previous container"
                )
    assert "replaced or rescheduled" in entries["logs/pods/"]["reason"]

    # planted secrets appear nowhere; the Secret names and keys stay readable
    assert PLANTED not in everything(tmp_path, proc)
    assert BASE64_VALUE not in everything(tmp_path, proc)
    values = (root / "configuration" / "helm-values.yaml").read_text()
    assert "existingSecret: vigil-prod-secrets" in values
    assert "existingSecretKey: POSTGRES_PASSWORD" in values
    assert "existingSecretPasswordKey: redis-password" in values
    assert "postgresPassword: [REDACTED]" in values
    assert (
        "[REDACTED]" in next((root / "logs" / "pods").rglob("backend.log")).read_text()
    )
    assert (root / "configuration" / "kubernetes-secrets.txt").read_text() == (
        "vigil-prod-secrets Opaque POSTGRES_PASSWORD ANTHROPIC_API_KEY\n"
    )

    # what was asked of the cluster
    calls = (tmp_path / "kube.log").read_text().splitlines()
    kube = [c for c in calls if c.startswith("kubectl ") and " version " not in c]
    assert kube and all(" -n vigil " in c for c in kube)
    logs = [c for c in kube if " logs " in c]
    assert logs and all(re.search(r"--since \d+h(\s|$)", c) for c in logs)
    assert all("--since 168h" in c for c in logs)
    assert all("go-template" in c for c in kube if " get secrets " in c)
    assert not any("manifest" in c for c in calls)
    assert any(c.endswith("port-forward svc/vigil-backend :6987") for c in kube)
    assert "helm get values vigil -n vigil -o yaml" in calls
    assert "helm list -o json -A" in calls
    assert marker_pids(helm_env["FAKE_PF_MARKER"]) == []


def test_helm_external_postgres_and_redis_are_not_applicable(helm_env, tmp_path):
    helm_env["FAKE_PODS"] = "\n".join(
        line
        for line in helm_env["FAKE_PODS"].splitlines()
        if "postgres" not in line and "redis" not in line
    )
    assert helm_run(helm_env, tmp_path).returncode == 0
    entries = entries_of(tmp_path)
    for name in ("postgres", "redis"):
        entry = entries[f"logs/pods/{name}/"]
        assert entry["state"] == "not collected"
        assert entry["reason"].startswith("external, not applicable")


def test_helm_two_releases_write_nothing_until_one_is_named(helm_env, tmp_path):
    helm_env["FAKE_HELM_LIST"] = HELM_LIST.replace(
        "grafana-8.0.0", "vigil-0.5.0"
    ).replace(
        '"name":"grafana","namespace":"mon"', '"name":"vigil-lab","namespace":"lab"'
    )
    proc = helm_run(helm_env, tmp_path)
    assert proc.returncode == 1
    assert bundles(tmp_path) == [] and list((tmp_path / "tmp").iterdir()) == []
    assert "vigil-lab" in proc.stdout and "namespace lab" in proc.stdout
    assert "--release NAME --namespace NS" in proc.stdout

    proc = helm_run(helm_env, tmp_path, "--release", "vigil-lab", "--namespace", "lab")
    assert proc.returncode == 0, proc.stderr
    assert (
        "Helm release vigil-lab, namespace lab"
        in manifest_of(tmp_path)["install"]["description"]
    )
    calls = (tmp_path / "kube.log").read_text()
    assert "helm list -o json -n lab" in calls
    assert "kubectl -n lab " in calls and "kubectl -n vigil " not in calls


def test_helm_no_vigil_release_writes_a_host_bundle(helm_env, tmp_path):
    helm_env["FAKE_HELM_LIST"] = (
        '[{"name":"g","namespace":"m","chart":"grafana-1.0.0"}]'
    )
    assert helm_run(helm_env, tmp_path).returncode == 0
    manifest = manifest_of(tmp_path)
    entries = {e["path"]: e for e in manifest["entries"]}
    assert manifest["mode"] == "host" and manifest["install"]["found"] is False
    assert any("1 releases returned, 0 matching" in line for line in manifest["looked"])
    assert entries["logs/"]["state"] == "not collected"
    assert entries["system/events.txt"]["state"] == "not collected"
    assert entries["system/host.txt"]["state"] == "collected"


def test_helm_missing_tool_writes_a_host_bundle(helm_env, tmp_path):
    safe = tmp_path / "safe-bin"
    safe.mkdir()
    for tool in (
        "sh awk mkfifo mktemp tar tee tail wc tr date sleep ps kill sed grep cut head "
        "sort df uname hostname cat rm mkdir cp chmod ls dirname basename find mv gzip"
    ).split():
        found = shutil.which(tool, path="/usr/bin:/bin")
        if found:
            (safe / tool).symlink_to(found)
    helm_env["PATH"] = str(safe)
    proc = helm_run(helm_env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    manifest = manifest_of(tmp_path)
    entries = {e["path"]: e for e in manifest["entries"]}
    assert manifest["mode"] == "host"
    assert any(line.startswith("helm: not found") for line in manifest["looked"])
    for path in (
        "configuration/",
        "health/",
        "logs/",
        "system/tools.txt",
        "system/nodes.txt",
    ):
        assert (
            entries[path]["state"] == "not collected"
            and "helm not found" in entries[path]["reason"]
        ), path
    assert entries["system/host.txt"]["state"] == "collected"


def test_helm_forbidden_pods_name_the_missing_permission(helm_env, tmp_path):
    helm_env["FAKE_FORBID_PODS"] = FORBIDDEN
    proc = helm_run(helm_env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    reason = f"needs elevation: {FORBIDDEN}"
    for path in (
        "logs/pods/",
        "health/pods.txt",
        "health/pods-describe.txt",
        "system/nodes.txt",
    ):
        assert (
            entries[path]["state"] == "not collected"
            and entries[path]["reason"] == reason
        ), path
    assert not any(p.startswith("logs/pods/") and p != "logs/pods/" for p in entries)
    assert entries["configuration/helm-values.yaml"]["state"] == "collected"
    assert reason in proc.stdout
    assert "Cluster permissions are missing" in proc.stdout
    assert "Run with sudo" not in proc.stdout


def test_helm_port_forward_that_never_answers_is_killed(helm_env, tmp_path):
    helm_env["FAKE_PF_HANG"] = "1"
    helm_env["VIGIL_SUPPORT_SOURCE_SECS"] = "2"
    started = time.time()
    assert helm_run(helm_env, tmp_path).returncode == 0
    assert time.time() - started < 60
    entry = entries_of(tmp_path)["health/api.json"]
    assert (
        entry["state"] == "not collected" and entry["reason"] == "timed out after 2 s"
    )
    assert marker_pids(helm_env["FAKE_PF_MARKER"]) == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_helm_signal_takes_the_port_forward_with_it(helm_env, tmp_path, sig):
    helm_env["FAKE_PF_HANG"] = "1"
    marker = helm_env["FAKE_PF_MARKER"]
    proc = subprocess.Popen(
        ["sh", str(SCRIPT), "--mode", "helm"],
        cwd=tmp_path / "out",
        env=helm_env,
        stdout=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while not marker_pids(marker) and time.time() < deadline:
        time.sleep(0.05)
    assert marker_pids(marker)
    proc.send_signal(sig)
    assert proc.wait(timeout=15) != 0
    deadline = time.time() + 5
    while marker_pids(marker) and time.time() < deadline:
        time.sleep(0.05)
    assert marker_pids(marker) == []
    assert bundles(tmp_path) == [] and list((tmp_path / "tmp").iterdir()) == []


def test_helm_options_need_helm_mode(env, tmp_path):
    for args in (
        ("--release", "vigil"),
        ("--namespace", "vigil"),
        ("--mode", "compose", "--release", "x"),
    ):
        proc = run(env, tmp_path, *args)
        assert proc.returncode == 1 and "need --mode helm" in proc.stderr
    assert bundles(tmp_path) == []
    # without --mode, Helm is named but never probed
    assert run(env, tmp_path).returncode == 0
    assert "helm: not probed, use --mode helm" in manifest_of(tmp_path)["looked"]


def test_helm_list_forbidden_is_named_and_the_tools_are_still_recorded(
    helm_env, tmp_path
):
    helm_env["FAKE_HELM_LIST_ERR"] = (
        "Error: list: secrets is forbidden: cannot list at the cluster scope"
    )
    proc = helm_run(helm_env, tmp_path)
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    assert entries["logs/"]["reason"].startswith(
        "needs elevation: Error: list: secrets is forbidden"
    )
    assert "--namespace NS" in entries["logs/"]["reason"]
    assert entries["system/tools.txt"]["state"] == "collected"
    assert "Cluster permissions are missing" in proc.stdout
