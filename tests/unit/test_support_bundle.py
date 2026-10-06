"""scripts/vigil-support/vigil-support.sh writes a safe bundle or nothing.

The real script and the real redact.awk run in a temp directory with stub
docker, curl, timedatectl, journalctl, dmesg and df on PATH.
"""

from __future__ import annotations

import json
import os
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
    echo "log of $last"; printf "%s\n" "${FAKE_LOG_LINE:-}" ;;
inspect) echo "$(for last; do :; done; echo "$last") state=running restarts=0" ;;
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
        "deeptempo-backend|docker|running\ndeeptempo-redis|docker|exited\n"
        "deeptempo-splunk|docker|running\ndeeptempo-misp-core|docker|exited\n"
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


def test_native_install_collects_files_and_no_container_logs(env, tmp_path):
    checkout, state = install(tmp_path)
    env["VIGIL_REPO_ROOT"] = str(checkout)
    env["FAKE_DOCKER_PS"] = "deeptempo-postgres|docker|\n"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    assert manifest_of(tmp_path)["mode"] == "native"
    assert entries["logs/checkout/backend.log"]["state"] == "collected"
    assert entries["configuration/compose-config.yml"]["state"] == "not collected"
    assert not any(
        p.startswith("logs/docker/") and e["state"] == "collected"
        for p, e in entries.items()
    )
    assert ENV_SECRET not in everything(tmp_path, proc)


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
    env["FAKE_DOCKER_PS"] = f"vigil-backend-1|vigil|{standalone}/docker-compose.yml\n"
    proc = run(env, tmp_path, "--state-dir", str(state))
    assert proc.returncode == 0, proc.stderr
    entries = entries_of(tmp_path)
    assert manifest_of(tmp_path)["mode"] == "desktop"
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
