"""`scripts/medic/enable-compose.sh`: the whole enable, against a fake `docker`.

The fake records every docker call and anything piped to `exec`, so these check
the order of the steps, that the account step gets the password on stdin and
never on a command line, the retry on "database not ready", that a refusal
stops before Medic starts, and that nothing printed holds a secret. The same
script against a real stack is `test_enable_live.py`.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.medic_compose.compose import ENABLE, OVERLAY, clean_env

pytestmark = pytest.mark.unit

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Test double for docker: one line per call in $FAKE_LOG (args joined by |).
log="$FAKE_DIR/calls"
n=$(wc -l < "$log" 2>/dev/null || echo 0)
( IFS='|'; printf '%s\n' "$*" ) >> "$log"
case " $* " in
    *" compose version "*) exit 0 ;;
    *" config "*" backend "*|*" config backend "*)
        env | grep -E '^(VIGIL_MEDIC_ENABLED|JWT_SECRET_KEY|OLLAMA_URL)=' | sort > "$FAKE_DIR/config_env"
        [ -n "${FAKE_CONFIG_FAIL:-}" ] && { echo "config error" >&2; exit 15; }
        printf '    environment:\n      DEV_MODE: "%s"\n      JWT_SECRET_KEY: %s\n' \
            "${FAKE_DEV_MODE:-false}" "${FAKE_JWT-jwt-from-env}"
        exit 0 ;;
    *" exec "*)
        cat > "$FAKE_DIR/stdin.$n"
        code=0
        if [ -s "$FAKE_DIR/exec_codes" ]; then
            code=$(head -n 1 "$FAKE_DIR/exec_codes")
            tail -n +2 "$FAKE_DIR/exec_codes" > "$FAKE_DIR/exec_codes.tmp"
            mv "$FAKE_DIR/exec_codes.tmp" "$FAKE_DIR/exec_codes"
        fi
        [ "$code" = 0 ] && echo "service account x: created"
        [ "$code" != 0 ] && echo "error $code" >&2
        exit "$code" ;;
    *" ps "*)
        # This call is already logged, so the first ps counts 1.
        ps_n=$(grep -c '|ps|' "$log")
        [ -n "${FAKE_PS_FAIL:-}" ] && [ "$ps_n" -gt "${FAKE_PS_FAIL_AFTER:-0}" ] && exit 1
        printf '%b' "${FAKE_RUNNING-backend\n}"; exit 0 ;;
esac
exit 0
"""


class Run:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.secrets = tmp / "secrets"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.fake.mkdir()
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        # The retry loop sleeps between tries; not in a unit test.
        sleep = self.bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)

    def __call__(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        e = clean_env(
            self.tmp,
            VIGIL_MEDIC_SECRETS_DIR=str(self.secrets),
            FAKE_DIR=str(self.fake),
            **env,
        )
        e["PATH"] = f"{self.bin}:{e['PATH']}"
        return subprocess.run(
            [str(ENABLE), *args], env=e, capture_output=True, text=True, check=False
        )

    def exec_codes(self, *codes: int) -> None:
        (self.fake / "exec_codes").write_text("".join(f"{c}\n" for c in codes))

    def calls(self) -> list[list[str]]:
        path = self.fake / "calls"
        if not path.exists():
            return []
        return [line.split("|") for line in path.read_text().splitlines()]

    def compose_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["compose"] and "version" not in c]

    def stdins(self) -> list[str]:
        return [p.read_text() for p in sorted(self.fake.glob("stdin.*"))]

    def secret(self, name: str) -> str:
        return (self.secrets / name).read_text().strip()


@pytest.fixture
def run(tmp_path) -> Run:
    return Run(tmp_path)


def _sub(call: list[str]) -> list[str]:
    """The compose subcommand and its arguments, after the global flags."""
    for i, word in enumerate(call):
        if word in ("config", "build", "up", "exec", "ps"):
            return call[i:]
    raise AssertionError(call)


def test_full_run_creates_the_account_then_starts_medic(run) -> None:
    done = run(FAKE_RUNNING="backend\\nagent-worker\\nsoc-daemon\\nredis\\n")
    assert done.returncode == 0, done.stderr

    steps = [_sub(c) for c in run.compose_calls()]
    assert steps[0][:2] == ["config", "backend"]
    names = [s[0] for s in steps]
    assert names.index("build") < names.index("up") < names.index("exec")
    assert ["build", "medic", "medic-gateway", "medic-dockerproxy"] in steps
    # db-seed seeds the Viewer role; nothing else starts it (review S3).
    assert ["up", "-d", "backend", "db-seed"] in steps
    user = run.secret("viewer_username")
    assert [
        "exec",
        "-T",
        "backend",
        "python",
        "-m",
        "core.auth.service_account",
        "ensure",
        user,
    ] in steps
    # Only the medic-net members already running are recreated.
    assert steps[-1] == [
        "up",
        "-d",
        "medic",
        "medic-gateway",
        "medic-dockerproxy",
        "agent-worker",
        "soc-daemon",
    ]
    assert names.index("exec") < len(names) - 1


def test_every_compose_call_carries_medics_settings_and_overlay(run) -> None:
    assert run().returncode == 0
    settings = str(run.secrets / "compose.env")
    for call in run.compose_calls():
        assert ["--env-file", settings] == call[call.index(settings) - 1 :][:2], call
        assert str(OVERLAY) in call and "--profile" in call, call


def test_password_goes_on_stdin_never_on_a_command_line(run) -> None:
    assert run().returncode == 0
    password = run.secret("viewer_password")
    assert run.stdins() == [password + "\n"]
    for call in run.calls():
        assert not any(password in word for word in call), call
        assert not any(run.secret("api_key") in word for word in call), call


def test_nothing_printed_holds_a_secret_or_the_name(run) -> None:
    done = run()
    assert done.returncode == 0
    for name in ("viewer_username", "viewer_password", "api_key"):
        value = run.secret(name)
        assert value not in done.stdout and value not in done.stderr, name


def test_retries_while_the_database_is_not_ready(run) -> None:
    run.exec_codes(75, 75, 0)
    done = run()
    assert done.returncode == 0, done.stderr
    assert len(run.stdins()) == 3
    assert _sub(run.compose_calls()[-1])[:3] == ["up", "-d", "medic"]


def test_waits_for_the_backend_container_before_the_account_step(run) -> None:
    done = run(FAKE_RUNNING="")
    assert done.returncode == 1
    assert "backend" in done.stderr
    assert run.stdins() == []


@pytest.mark.parametrize("code", [1, 64, 70, 77])
def test_a_refusal_stops_before_medic_starts(run, code) -> None:
    run.exec_codes(code)
    done = run()
    assert done.returncode == 1
    assert f"exit {code}" in done.stderr
    assert len(run.stdins()) == 1
    assert not any(_sub(c)[:3] == ["up", "-d", "medic"] for c in run.compose_calls())


def test_gives_up_after_the_retry_budget(run) -> None:
    run.exec_codes(*([75] * 60))
    done = run()
    assert done.returncode == 1
    assert len(run.stdins()) == 60
    assert "service account" in done.stderr


@pytest.mark.parametrize("jwt", ['""', ""])
def test_refuses_to_recreate_the_backend_without_its_jwt_secret(run, jwt) -> None:
    done = run(FAKE_JWT=jwt)
    assert done.returncode == 1
    assert "JWT_SECRET_KEY" in done.stderr
    assert {_sub(c)[0] for c in run.compose_calls()} == {"config"}


def test_dev_mode_needs_no_jwt_secret(run) -> None:
    done = run(FAKE_JWT='""', FAKE_DEV_MODE="true")
    assert done.returncode == 0, done.stderr


def test_rotate_changes_the_password_and_recreates_medic_and_gateway(run) -> None:
    assert run().returncode == 0
    old = run.secret("viewer_password")
    user = run.secret("viewer_username")

    done = run("--rotate")

    assert done.returncode == 0, done.stderr
    new = run.secret("viewer_password")
    assert new != old
    assert run.secret("viewer_username") == user
    assert run.stdins()[-1] == new + "\n"
    steps = [_sub(c) for c in run.compose_calls()]
    recreate = steps.index(["up", "-d", "--force-recreate", "medic", "medic-gateway"])
    # Straight after the password changed: the gateway stops after 2 refusals.
    assert steps[recreate - 1][0] == "exec"


def test_rerun_is_idempotent(run) -> None:
    assert run().returncode == 0
    before = {
        n: run.secret(n) for n in ("viewer_username", "viewer_password", "api_key")
    }
    done = run()
    assert done.returncode == 0
    assert {n: run.secret(n) for n in before} == before
    assert run.stdins()[0] == run.stdins()[1]
    assert done.stdout.count("kept") == 3


def test_override_file_goes_last_on_every_call(run, tmp_path) -> None:
    extra = tmp_path / "override.yml"
    extra.write_text("services: {}\n")
    assert run(VIGIL_MEDIC_COMPOSE_OVERRIDE=str(extra)).returncode == 0
    for call in run.compose_calls():
        files = [call[i + 1] for i, w in enumerate(call) if w == "-f"]
        assert files[-1] == str(extra), call


# --- what --secrets-only writes ---------------------------------------------


def test_username_is_random_private_and_kept(run) -> None:
    assert run("--secrets-only").returncode == 0
    path = run.secrets / "viewer_username"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    name = run.secret("viewer_username")
    assert re.fullmatch(r"medic-[a-z0-9]{12}", name)
    assert run("--secrets-only", "--rotate").returncode == 0
    assert run.secret("viewer_username") == name


def test_two_installs_get_different_names(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = Run(tmp_path / "a"), Run(tmp_path / "b")
    assert a("--secrets-only").returncode == 0
    assert b("--secrets-only").returncode == 0
    assert a.secret("viewer_username") != b.secret("viewer_username")


def test_a_damaged_username_is_refused(run) -> None:
    assert run("--secrets-only").returncode == 0
    (run.secrets / "viewer_username").write_text("medic-viewer\n")
    done = run("--secrets-only")
    assert done.returncode == 1
    assert "viewer_username" in done.stderr


def test_compose_env_holds_the_settings_and_no_secret(run) -> None:
    assert run("--secrets-only", VIGIL_MEDIC_DOCKER_GID="987").returncode == 0
    path = run.secrets / "compose.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    values = dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#")
    )
    assert values == {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_SECRETS_DIR": str(run.secrets),
        "VIGIL_MEDIC_DOCKER_GID": "987",
        "VIGIL_MEDIC_VIEWER_USER": run.secret("viewer_username"),
    }
    for name in ("viewer_password", "api_key"):
        assert run.secret(name) not in text


def test_a_secret_replaced_without_rotate_still_reaches_the_gateway(run) -> None:
    """A deleted file is re-minted; the running gateway holds the old inode (S4)."""
    assert run().returncode == 0
    (run.secrets / "viewer_password").unlink()
    assert run().returncode == 0
    steps = [_sub(c) for c in run.compose_calls()]
    assert ["up", "-d", "--force-recreate", "medic", "medic-gateway"] in steps


def test_a_failing_compose_config_stops_the_run(run) -> None:
    done = run(FAKE_CONFIG_FAIL="1")
    assert done.returncode != 0
    assert not any(_sub(c)[0] in ("build", "up") for c in run.compose_calls())


def test_a_failing_compose_ps_stops_before_medic_starts(run) -> None:
    # The first ps (waiting for the backend) works; the members ps fails.
    done = run(FAKE_PS_FAIL="1", FAKE_PS_FAIL_AFTER="1")
    assert done.returncode != 0
    # Failed at the members step, not while waiting for the backend.
    assert "isn't running" not in done.stderr
    assert any(_sub(c)[:2] == ["exec", "-T"] for c in run.compose_calls())
    assert not any(_sub(c)[:3] == ["up", "-d", "medic"] for c in run.compose_calls())


def test_a_failing_ps_while_waiting_is_a_compose_error_not_a_stopped_backend(run):
    done = run(FAKE_PS_FAIL="1")
    assert done.returncode != 0
    assert "isn't running" not in done.stderr
    assert len([c for c in run.compose_calls() if "ps" in c]) == 1


def _dotenv(run, text: str) -> str:
    path = run.tmp / "dotenv"
    path.write_text(text)
    return str(path)


def _config_env(run) -> dict:
    lines = (run.fake / "config_env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_dotenv_from_env_example_does_not_switch_medic_off(run) -> None:
    """env.example ships VIGIL_MEDIC_ENABLED="false"; compose.env must win."""
    dotenv = _dotenv(run, 'VIGIL_MEDIC_ENABLED="false"\nJWT_SECRET_KEY="from-dotenv"\n')
    done = run(VIGIL_MEDIC_DOTENV=dotenv, FAKE_JWT="x")
    assert done.returncode == 0, done.stderr
    env = _config_env(run)
    assert env["VIGIL_MEDIC_ENABLED"] == "true"
    assert env["JWT_SECRET_KEY"] == "from-dotenv"


def test_exported_variables_beat_dotenv(run) -> None:
    """The documented flow exports JWT_SECRET_KEY; a blank .env line mustn't erase it."""
    dotenv = _dotenv(run, 'JWT_SECRET_KEY=""\nOLLAMA_URL="http://gpu-box:11434"\n')
    done = run(VIGIL_MEDIC_DOTENV=dotenv, JWT_SECRET_KEY="exported")
    assert done.returncode == 0, done.stderr
    env = _config_env(run)
    assert env["JWT_SECRET_KEY"] == "exported"
    # .env values the shell didn't set still arrive (review S1).
    assert env["OLLAMA_URL"] == "http://gpu-box:11434"


def test_a_secrets_only_rotate_is_finished_by_the_next_full_run(run) -> None:
    assert run().returncode == 0
    assert run("--secrets-only", "--rotate").returncode == 0
    assert run().returncode == 0
    steps = [_sub(c) for c in run.compose_calls()]
    last_exec = max(i for i, s in enumerate(steps) if s[0] == "exec")
    assert ["up", "-d", "--force-recreate", "medic", "medic-gateway"] in steps[
        last_exec:
    ]
