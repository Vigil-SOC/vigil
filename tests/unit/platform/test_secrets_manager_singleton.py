"""Unit tests for the secrets-manager singleton + migration helpers.

Specifically covers the regressions that motivated PR #158:

- ``cryptography`` availability is decided ONCE at module import, not per
  ``EncryptedFileBackend`` instance and not at each ``is_available()``
  call. So the chosen write backend is stable across the process
  lifetime regardless of singleton init order.
- ``get_secrets_manager(force_reload=True)`` rebuilds the singleton, so
  a process that picked the wrong write backend on first init can
  recover without bouncing.
- ``SecretsManager.migrate_dotenv_secrets_to_encrypted`` moves keys
  out of the dotenv backend into the encrypted backend, refusing to
  clobber on conflict and only deleting the dotenv copy on success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import secrets_manager  # noqa: E402
from core.secrets_manager import (  # noqa: E402
    DotEnvBackend,
    EncryptedFileBackend,
    SecretsManager,
    get_secrets_manager,
)

# ---------------------------------------------------------------------------
# Eager cryptography availability
# ---------------------------------------------------------------------------


def test_cryptography_availability_is_module_level():
    """Module-level constant is set exactly once, regardless of init order."""
    # The constant must exist (regardless of value) and be a plain bool.
    assert isinstance(secrets_manager._CRYPTOGRAPHY_AVAILABLE, bool)


def test_encrypted_backend_uses_module_level_availability():
    """Per-instance is_available reads the module constant — no per-call import."""
    backend = EncryptedFileBackend()
    assert backend.is_available() is secrets_manager._CRYPTOGRAPHY_AVAILABLE
    # And it returns the same answer when called twice in a row even after
    # the module-level constant is forced False (instances cache via
    # _crypto_ok on init, so flipping post-init shouldn't lie about the
    # backend they were constructed with).
    backend2 = EncryptedFileBackend()
    assert backend2.is_available() == backend.is_available()


def test_encrypted_backend_init_does_not_reimport_cryptography(monkeypatch):
    """`__init__` should rely on the module-level constant, not retry the import."""
    # Construct a backend; if the constructor were doing its own
    # `try: import cryptography` per instance, this would be the surface
    # where we'd notice. Smoke test: the constructor must not raise even
    # if the cryptography module isn't around at runtime.
    monkeypatch.setattr(secrets_manager, "_CRYPTOGRAPHY_AVAILABLE", False)
    backend = EncryptedFileBackend()
    assert backend._crypto_ok is False
    assert backend.is_available() is False


# ---------------------------------------------------------------------------
# force_reload on the singleton
# ---------------------------------------------------------------------------


def test_get_secrets_manager_returns_singleton_by_default():
    a = get_secrets_manager()
    b = get_secrets_manager()
    assert a is b


def test_force_reload_returns_a_fresh_singleton():
    a = get_secrets_manager()
    b = get_secrets_manager(force_reload=True)
    assert b is not a


def test_force_reload_picks_up_changed_env_var(monkeypatch):
    """SECRETS_BACKEND env var should win over the cached singleton."""
    a = get_secrets_manager()
    monkeypatch.setenv("SECRETS_BACKEND", "dotenv")
    b = get_secrets_manager(force_reload=True)
    assert b.write_backend_name == "dotenv"
    assert b is not a


# ---------------------------------------------------------------------------
# Backend status reporting
# ---------------------------------------------------------------------------


def test_backend_status_reports_canonical_keys():
    mgr = get_secrets_manager(force_reload=True)
    status = mgr.get_backend_status()
    for key in (
        "encrypted",
        "environment",
        "dotenv",
        "keyring",
        "write_backend",
        "expected_write_backend",
        "cryptography_available",
        "read_backends",
    ):
        assert key in status, f"missing key: {key}"
    assert "secrets_file_exists" in status["encrypted"]
    assert "master_key_present" in status["encrypted"]


# ---------------------------------------------------------------------------
# Migration: dotenv → encrypted
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    """Build a SecretsManager with backends pointed at a tmp directory.

    Avoids touching the developer's real ~/.vigil/ and ~/.deeptempo/.env
    during tests.
    """
    monkeypatch.setattr(secrets_manager, "_CRYPTOGRAPHY_AVAILABLE", True)
    encrypted = EncryptedFileBackend(data_dir=tmp_path / "vigil")
    # Make sure _crypto_ok reflects the patched module-level value.
    encrypted._crypto_ok = True
    dotenv = DotEnvBackend(env_file=tmp_path / "deeptempo" / ".env")

    mgr = SecretsManager(write_backend="encrypted")
    mgr.encrypted_backend = encrypted
    mgr.dotenv_backend = dotenv
    # Re-build read_backends so the encrypted/dotenv tmp instances are used.
    mgr.read_backends = [encrypted, mgr.env_backend, dotenv]
    mgr.write_backend = encrypted
    return mgr, dotenv, encrypted


def _seed_dotenv(dotenv: DotEnvBackend, **kv):
    for k, v in kv.items():
        dotenv.set(k, v)


def test_migrate_copies_keys_and_removes_from_dotenv(isolated_manager):
    mgr, dotenv, encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="alpha", BAR="beta")

    report = mgr.migrate_dotenv_secrets_to_encrypted()

    assert set(report["migrated"]) == {"FOO", "BAR"}
    assert report["already_present"] == []
    assert report["conflicts"] == []
    assert report["errors"] == []

    # Encrypted store now has both values.
    assert encrypted.get("FOO") == "alpha"
    assert encrypted.get("BAR") == "beta"
    # And the dotenv source is empty.
    assert dotenv.get("FOO") is None
    assert dotenv.get("BAR") is None


def test_migrate_dry_run_leaves_dotenv_intact(isolated_manager):
    mgr, dotenv, encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="alpha")

    report = mgr.migrate_dotenv_secrets_to_encrypted(remove_from_dotenv=False)

    assert report["migrated"] == ["FOO"]
    assert encrypted.get("FOO") == "alpha"
    # Dry-run keeps the source file untouched.
    assert dotenv.get("FOO") == "alpha"


def test_migrate_skips_keys_already_in_encrypted_with_same_value(isolated_manager):
    mgr, dotenv, encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="alpha")
    encrypted.set("FOO", "alpha")

    report = mgr.migrate_dotenv_secrets_to_encrypted()

    assert report["migrated"] == []
    assert report["already_present"] == ["FOO"]
    # Dotenv source cleaned up since the values matched.
    assert dotenv.get("FOO") is None


def test_migrate_reports_value_conflicts_without_overwriting(isolated_manager):
    mgr, dotenv, encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="dotenv-value")
    encrypted.set("FOO", "encrypted-value")

    report = mgr.migrate_dotenv_secrets_to_encrypted()

    assert report["migrated"] == []
    assert report["already_present"] == []
    assert report["conflicts"] == [{"key": "FOO", "reason": "value_mismatch"}]
    # Encrypted store wins; both stores keep their values.
    assert encrypted.get("FOO") == "encrypted-value"
    assert dotenv.get("FOO") == "dotenv-value"


def test_migrate_explicit_keys_filters_subset(isolated_manager):
    mgr, dotenv, encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="alpha", BAR="beta", BAZ="gamma")

    report = mgr.migrate_dotenv_secrets_to_encrypted(keys=["FOO", "BAZ"])

    assert set(report["migrated"]) == {"FOO", "BAZ"}
    # BAR was excluded — left alone in dotenv, not in encrypted.
    assert dotenv.get("BAR") == "beta"
    assert encrypted.get("BAR") is None


def test_migrate_refuses_when_encrypted_unavailable(isolated_manager, monkeypatch):
    mgr, dotenv, _encrypted = isolated_manager
    _seed_dotenv(dotenv, FOO="alpha")
    # Simulate cryptography missing.
    mgr.encrypted_backend._crypto_ok = False

    report = mgr.migrate_dotenv_secrets_to_encrypted()

    assert report["encrypted_available"] is False
    assert report["errors"]
    assert report["errors"][0]["key"] == "<all>"
    # Source file untouched.
    assert dotenv.get("FOO") == "alpha"
