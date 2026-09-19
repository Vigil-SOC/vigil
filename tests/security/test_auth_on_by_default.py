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

from core.auth.dev_mode import BYPASSED_GATES, is_exposed

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_api(
    *argv: str, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
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
        [sys.executable, "-c", "import services.api.main", *argv],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


# Every file a fresh install copies from. They document one bypass between them.
ENV_EXAMPLES = (
    REPO_ROOT / "env.example",
    REPO_ROOT / "clients" / "web" / "env.development.example",
)


# Not part of what an install ships: build output, dependencies, local state.
UNSHIPPED_DIRS = frozenset(
    {
        ".git",
        ".pytest_cache",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
        ".venv",
    }
)

# Files that name the password without offering it to anyone:
CREDENTIAL_SWEEP_ALLOWED = frozenset(
    {
        # a breach wordlist — it is supposed to contain weak passwords
        REPO_ROOT / "data" / "common_passwords.txt",
        # types a password into a login form; creates no account
        REPO_ROOT
        / "clients"
        / "web"
        / "src"
        / "screens"
        / "login"
        / "LoginScreen.test.tsx",
        # this file
        Path(__file__).resolve(),
    }
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

    def test_no_env_example_points_at_a_file_that_does_not_exist(self):
        """Both files describe the same bypass, so both can dangle."""
        if (REPO_ROOT / "DEV_MODE.md").exists():
            return

        offenders = [
            path.relative_to(REPO_ROOT)
            for path in ENV_EXAMPLES
            if "DEV_MODE.md" in path.read_text()
        ]

        assert not offenders, f"reference to a file that does not exist in: {offenders}"

    def test_nothing_shipped_creates_or_announces_a_password_we_chose(self):
        """A known default credential is the same hole wherever it is written.

        The whole tree, not just ``scripts/``: the last one to survive a sweep
        of the scripts was a helm NOTES.txt telling operators to log in with a
        password nothing had set.
        """
        offenders = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or path in CREDENTIAL_SWEEP_ALLOWED:
                continue
            if set(path.relative_to(REPO_ROOT).parts) & UNSHIPPED_DIRS:
                continue
            if path.stat().st_size > 1_000_000:
                continue
            if "admin123" in path.read_text(errors="ignore"):
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

    @pytest.mark.parametrize("flag", ["--host 0.0.0.0", "--host=0.0.0.0"])
    def test_an_address_typed_on_the_command_line_counts(self, flag: str):
        """`uvicorn --host 0.0.0.0` exports no BIND_HOST, and used to read as
        loopback — the banner reassuring the one person who needed warning."""
        result = _import_api(*flag.split(), DEV_MODE="true")

        assert result.returncode == 0, result.stderr
        assert "NOT LOOPBACK" in result.stderr
        assert "0.0.0.0" in result.stderr

    def test_the_command_line_wins_over_the_environment(self):
        """uvicorn binds what it was passed, so that is what the banner reports."""
        result = _import_api("--host", "127.0.0.1", DEV_MODE="true", BIND_HOST="0.0.0.0")

        assert result.returncode == 0, result.stderr
        assert "DEV_MODE IS ON" in result.stderr
        assert "NOT LOOPBACK" not in result.stderr


class TestWhatCountsAsLoopback:
    """The classifier behind the exposed/loopback split.

    Driven directly rather than through a subprocess: the interesting cases are
    addresses no launcher in this repo passes, and each one costs a process.
    """

    @pytest.mark.parametrize(
        "bind_host",
        [
            "127.0.0.1",
            "::1",
            "localhost",
            "LOCALHOST",
            "  127.0.0.1  ",
            # The rest of 127/8 is loopback too, and a set of three strings
            # called this exposed.
            "127.0.0.2",
            "127.255.255.254",
            # The IPv4-mapped form, as an IPv6 socket reports a v4 loopback.
            "::ffff:127.0.0.1",
            "[::1]",
        ],
    )
    def test_addresses_that_reach_only_this_machine(self, bind_host: str):
        assert is_exposed(bind_host) is False

    @pytest.mark.parametrize(
        "bind_host",
        [
            "0.0.0.0",
            "::",
            "10.0.0.5",
            "192.168.1.20",
            # Unset is uvicorn's "every interface", not loopback. Classing it
            # loopback silenced the exposure line on the one bind that most
            # needs it.
            "",
            "   ",
            # A name this process cannot resolve tells us nothing, and nothing
            # is not evidence of local.
            "soc-box.internal",
        ],
    )
    def test_addresses_that_are_not_known_to_be_local(self, bind_host: str):
        assert is_exposed(bind_host) is True

    def test_an_unset_bind_is_announced_by_name_not_by_a_gap(self):
        """The banner has to name the address it is warning about."""
        result = _import_api(DEV_MODE="true", BIND_HOST="")

        assert result.returncode == 0, result.stderr
        assert "NOT LOOPBACK" in result.stderr
        assert "every interface" in result.stderr
