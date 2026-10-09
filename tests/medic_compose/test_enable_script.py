"""`scripts/medic/enable-compose.sh --secrets-only`: random secrets, 0600, never shown.

C3 §7 check 13 / K1 T-34: the installer generates a random Viewer password; no
literal one exists anywhere. The script also generates the Medic API secret
(X2's `X-Medic-Key`) and the Viewer's random name (D2-17). `--secrets-only`
skips the Docker work, which is what makes this test runnable anywhere; the full
run is `test_enable_flow.py`.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from services.medic_gateway.server import MEDIC_KEY
from tests.medic_compose.compose import ENABLE, clean_env

pytestmark = pytest.mark.unit

NAMES = ("viewer_password", "api_key")


def _run(home: Path, secrets: Path, *args: str) -> subprocess.CompletedProcess:
    env = clean_env(home, VIGIL_MEDIC_SECRETS_DIR=str(secrets))
    return subprocess.run(
        [str(ENABLE), "--secrets-only", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_generates_private_random_secrets(tmp_path) -> None:
    secrets = tmp_path / "s"
    done = _run(tmp_path, secrets)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700
    values = []
    for name in NAMES:
        path = secrets / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
        value = path.read_text()
        assert value.endswith("\n") and value.count("\n") == 1
        value = value.strip()
        assert len(value) >= 40, name
        if name == "api_key":
            # X2: 32 random bytes, base64url; the gateway refuses any other shape.
            assert MEDIC_KEY.fullmatch(value), value
        else:
            assert value.isalnum(), name
        values.append(value)
        # Never echoed: not in what the operator sees, not in a log they paste.
        assert value not in done.stdout and value not in done.stderr
    assert values[0] != values[1]


def test_two_installs_get_different_secrets(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    assert _run(tmp_path, a).returncode == 0
    assert _run(tmp_path, b).returncode == 0
    for name in NAMES:
        assert (a / name).read_text() != (b / name).read_text()


def test_rerun_keeps_existing_secrets(tmp_path) -> None:
    # A silent rotation would leave the Viewer account on the old password and
    # walk the gateway into bad_password (S5-4).
    secrets = tmp_path / "s"
    _run(tmp_path, secrets)
    before = {n: (secrets / n).read_text() for n in NAMES}
    done = _run(tmp_path, secrets)
    assert done.returncode == 0
    assert {n: (secrets / n).read_text() for n in NAMES} == before
    assert "kept" in done.stdout


def test_rotate_replaces_them(tmp_path) -> None:
    secrets = tmp_path / "s"
    _run(tmp_path, secrets)
    before = {n: (secrets / n).read_text() for n in NAMES}
    assert _run(tmp_path, secrets, "--rotate").returncode == 0
    for name in NAMES:
        assert (secrets / name).read_text() != before[name]
        assert stat.S_IMODE((secrets / name).stat().st_mode) == 0o600


def test_refuses_a_loose_secrets_dir(tmp_path) -> None:
    secrets = tmp_path / "s"
    secrets.mkdir(mode=0o755)
    secrets.chmod(0o755)
    done = _run(tmp_path, secrets)
    assert done.returncode != 0
    assert "0700" in done.stderr


def test_prints_the_compose_command_with_medics_settings(tmp_path) -> None:
    secrets = tmp_path / "s"
    out = _run(tmp_path, secrets).stdout
    assert f"--env-file {secrets / 'compose.env'}" in out
    assert "--profile medic" in out
    assert "infra/docker/medic/docker-compose.medic.yml" in out
    # No manual account step any more: the full run creates it.
    assert "create the user" not in out


def test_unknown_argument_is_refused(tmp_path) -> None:
    env = clean_env(tmp_path, VIGIL_MEDIC_SECRETS_DIR=str(tmp_path / "s"))
    done = subprocess.run(
        [str(ENABLE), "--bogus"], env=env, capture_output=True, text=True
    )
    assert done.returncode == 2
    assert not (tmp_path / "s").exists()


def test_rotate_says_to_recreate_the_containers(tmp_path) -> None:
    # Compose bind-mounts the file: a running gateway keeps the old inode.
    secrets = tmp_path / "s"
    _run(tmp_path, secrets)
    out = _run(tmp_path, secrets, "--rotate").stdout
    assert "--force-recreate medic medic-gateway" in out


def test_relative_secrets_dir_is_refused(tmp_path) -> None:
    env = clean_env(tmp_path, VIGIL_MEDIC_SECRETS_DIR="relative/secrets")
    done = subprocess.run(
        [str(ENABLE), "--secrets-only"],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert done.returncode == 1
    assert "absolute" in done.stderr
    assert not (tmp_path / "relative").exists()


def test_a_damaged_secret_is_not_kept(tmp_path) -> None:
    secrets = tmp_path / "s"
    _run(tmp_path, secrets)
    (secrets / "viewer_password").write_text("\n")
    done = _run(tmp_path, secrets)
    assert done.returncode == 1
    assert "--rotate" in done.stderr


def test_default_dir_is_outside_vigils_state_dir(tmp_path) -> None:
    env = clean_env(tmp_path)
    done = subprocess.run(
        [str(ENABLE), "--secrets-only"], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".vigil-medic" / "secrets" / "viewer_password").is_file()
    assert not (tmp_path / ".vigil").exists()


def test_a_hex_api_key_from_an_earlier_run_is_not_kept(tmp_path) -> None:
    # V1 minted 64 hex characters, which the gateway rejects (X2 is base64url).
    secrets = tmp_path / "s"
    _run(tmp_path, secrets)
    (secrets / "api_key").write_text("ab" * 32 + "\n")
    done = _run(tmp_path, secrets)
    assert done.returncode == 1
    assert "--rotate" in done.stderr
    assert _run(tmp_path, secrets, "--rotate").returncode == 0
    assert MEDIC_KEY.fullmatch((secrets / "api_key").read_text().strip())
