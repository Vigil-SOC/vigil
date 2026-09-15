"""Authentication is the default, and the bypass cannot be quiet or exposed.

Three properties, each of which has failed in this repository before:

* the shipped configuration requires a login — it shipped bypassed for a long
  time, and nothing noticed
* a bypassed service says so on every startup
* a bypassed service reachable from the network says *that*, separately and in
  its own words, because it is a different thing from a bypass on a laptop

The announcement runs at import of the API entrypoint, so these drive a
subprocess: asserting on it in-process would need the module imported fresh
each time.

It announces rather than refuses. Someone enabling the bypass on a cluster or a
throwaway box is doing it deliberately, and a service that will not boot is one
they cannot use to find out why.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.auth.dev_mode import BYPASSED_GATES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_api(**env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DEV_MODE", None)
    env.pop("BIND_HOST", None)
    env["JWT_SECRET_KEY"] = "test-only-signing-secret-not-for-any-real-use"
    env["VIGIL_DISABLE_DOTENV"] = "1"
    env["TESTING"] = "true"
    env.update(env_overrides)
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH")
        else str(REPO_ROOT)
    )
    return subprocess.run(
        [sys.executable, "-c", "import services.api.main"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


class TestShippedConfiguration:
    def test_env_example_requires_authentication(self):
        """A fresh install copies this file. It is the default."""
        text = (REPO_ROOT / "env.example").read_text()

        assert "\nDEV_MODE=false\n" in text, (
            "env.example must ship with authentication on — this is the file "
            "setup_dev.sh copies to .env on a fresh checkout"
        )
        assert "\nDEV_MODE=true\n" not in text

    def test_env_example_does_not_point_at_a_file_that_does_not_exist(self):
        text = (REPO_ROOT / "env.example").read_text()

        assert "DEV_MODE.md" not in text or (REPO_ROOT / "DEV_MODE.md").exists()

    def test_no_script_creates_an_account_with_a_password_we_chose(self):
        """A known default credential is the same hole wherever it is written."""
        offenders = []
        for path in (REPO_ROOT / "scripts").rglob("*.py"):
            if "admin123" in path.read_text():
                offenders.append(path.relative_to(REPO_ROOT))

        assert not offenders, f"default credentials still shipped in: {offenders}"


class TestBypassIsLoud:
    def test_bypass_on_loopback_starts_and_announces_itself(self):
        result = _import_api(DEV_MODE="true", BIND_HOST="127.0.0.1")

        assert result.returncode == 0, result.stderr
        assert "DEV_MODE IS ON" in result.stderr
        # Naming the gates individually: "auth is off" is not actionable when
        # what you need to know is whether the worker's path is open too.
        for gate in BYPASSED_GATES:
            assert gate in result.stderr

    def test_authenticated_startup_says_nothing(self):
        result = _import_api(DEV_MODE="false", BIND_HOST="0.0.0.0")

        assert result.returncode == 0, result.stderr
        assert "DEV_MODE IS ON" not in result.stderr


class TestAnExposedBypassIsLouderStill:
    """Bypassed on a laptop and bypassed on a network are not the same fact."""

    @pytest.mark.parametrize("bind_host", ["0.0.0.0", "10.0.0.5", "::"])
    def test_an_exposed_bypass_says_it_is_exposed_and_still_starts(
        self, bind_host: str
    ):
        result = _import_api(DEV_MODE="true", BIND_HOST=bind_host)

        # Starts. Refusing would leave someone with a service they cannot use
        # to work out why it will not run.
        assert result.returncode == 0, result.stderr
        assert "NOT LOOPBACK" in result.stderr
        assert bind_host in result.stderr
        # And says what to do about it, not only that it is happening.
        assert "DEV_MODE=false" in result.stderr

    @pytest.mark.parametrize("bind_host", ["127.0.0.1", "::1", "localhost"])
    def test_a_loopback_bypass_does_not_claim_to_be_exposed(self, bind_host: str):
        result = _import_api(DEV_MODE="true", BIND_HOST=bind_host)

        assert result.returncode == 0, result.stderr
        assert "DEV_MODE IS ON" in result.stderr
        assert "NOT LOOPBACK" not in result.stderr
