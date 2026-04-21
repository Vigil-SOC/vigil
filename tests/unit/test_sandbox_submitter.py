"""Unit tests for daemon.sandbox_submitter — safety gating and dispatch."""

import pytest
from unittest.mock import patch

from daemon.sandbox_submitter import SandboxSettings, SandboxSubmitter


@pytest.mark.unit
class TestSandboxSettings:
    def test_defaults(self, monkeypatch):
        for key in (
            "SANDBOX_AUTO_SUBMIT",
            "SANDBOX_MAX_FILE_SIZE_MB",
            "SANDBOX_ALLOWED_FILE_TYPES",
            "SANDBOX_ANALYSIS_TIMEOUT",
            "JOE_SANDBOX_ENABLED",
            "CAPE_SANDBOX_ENABLED",
            "HYBRID_ANALYSIS_ENABLED",
            "ANYRUN_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)

        s = SandboxSettings.from_env()
        assert s.auto_submit is False
        assert s.max_file_size_mb == 100
        assert "exe" in s.allowed_types
        assert s.cape_enabled is False

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_AUTO_SUBMIT", "true")
        monkeypatch.setenv("SANDBOX_MAX_FILE_SIZE_MB", "25")
        monkeypatch.setenv("SANDBOX_ALLOWED_FILE_TYPES", "exe,dll")
        monkeypatch.setenv("CAPE_SANDBOX_ENABLED", "1")

        s = SandboxSettings.from_env()
        assert s.auto_submit is True
        assert s.max_file_size_mb == 25
        assert s.allowed_types == ["exe", "dll"]
        assert s.cape_enabled is True


@pytest.mark.unit
class TestSafetyGate:
    def _make(self, **overrides):
        defaults = dict(
            auto_submit=True,
            max_file_size_mb=10,
            allowed_types=["exe", "dll"],
            timeout_seconds=300,
            joe_enabled=False,
            cape_enabled=True,
            hybrid_enabled=False,
            anyrun_enabled=False,
        )
        defaults.update(overrides)
        return SandboxSubmitter(SandboxSettings(**defaults))

    def test_enabled_requires_auto_submit_and_at_least_one_sandbox(self):
        off = self._make(auto_submit=False)
        assert off.enabled() is False

        nothing = self._make(cape_enabled=False)
        assert nothing.enabled() is False

        good = self._make()
        assert good.enabled() is True

    def test_bad_hash_rejected(self):
        s = self._make()
        assert s.is_hash_safe_to_submit("not-a-hash") is False
        assert s.is_hash_safe_to_submit("") is False
        assert s.is_hash_safe_to_submit("g" * 64) is False  # wrong alphabet

    def test_valid_hashes_accepted(self):
        s = self._make()
        assert s.is_hash_safe_to_submit("a" * 32) is True  # md5
        assert s.is_hash_safe_to_submit("b" * 40) is True  # sha1
        assert s.is_hash_safe_to_submit("c" * 64) is True  # sha256

    def test_size_cap_rejects_large_file(self):
        s = self._make()
        hint = {"file_size": 100 * 1024 * 1024}  # 100 MB, cap is 10
        assert s.is_hash_safe_to_submit("a" * 64, hint) is False

    def test_size_cap_accepts_under_limit(self):
        s = self._make()
        hint = {"file_size": 5 * 1024 * 1024}
        assert s.is_hash_safe_to_submit("a" * 64, hint) is True

    def test_extension_allowlist_rejects(self):
        s = self._make()
        assert s.is_hash_safe_to_submit("a" * 64, {"file_name": "song.mp3"}) is False

    def test_extension_allowlist_accepts(self):
        s = self._make()
        assert s.is_hash_safe_to_submit("a" * 64, {"file_name": "payload.exe"}) is True

    def test_hint_without_extension_is_permissive(self):
        s = self._make()
        assert s.is_hash_safe_to_submit("a" * 64, {"file_name": "noext"}) is True


@pytest.mark.unit
class TestSubmitDispatch:
    @pytest.mark.asyncio
    async def test_disabled_short_circuits(self):
        settings = SandboxSettings(
            auto_submit=False,
            max_file_size_mb=100,
            allowed_types=["exe"],
            timeout_seconds=300,
            joe_enabled=False,
            cape_enabled=False,
            hybrid_enabled=False,
            anyrun_enabled=False,
        )
        s = SandboxSubmitter(settings)
        out = await s.submit_hash("a" * 64)
        assert out == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_rejected_when_hash_bad(self):
        settings = SandboxSettings(
            auto_submit=True,
            max_file_size_mb=100,
            allowed_types=["exe"],
            timeout_seconds=300,
            joe_enabled=False,
            cape_enabled=True,
            hybrid_enabled=False,
            anyrun_enabled=False,
        )
        s = SandboxSubmitter(settings)
        out = await s.submit_hash("not-a-hash")
        assert out["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_dispatches_only_enabled_sandboxes(self):
        settings = SandboxSettings(
            auto_submit=True,
            max_file_size_mb=100,
            allowed_types=["exe"],
            timeout_seconds=300,
            joe_enabled=False,
            cape_enabled=True,
            hybrid_enabled=True,
            anyrun_enabled=False,
        )
        s = SandboxSubmitter(settings)

        async def fake_cape(self_arg, h):
            return {"status": "cached", "task_id": "c1"}

        async def fake_hybrid(self_arg, h):
            return {"status": "unknown"}

        async def fail(*a, **kw):
            raise AssertionError("disabled sandbox should not be called")

        with patch.object(SandboxSubmitter, "_submit_cape", fake_cape), patch.object(
            SandboxSubmitter, "_submit_hybrid", fake_hybrid
        ), patch.object(SandboxSubmitter, "_submit_anyrun", fail), patch.object(
            SandboxSubmitter, "_submit_joe", fail
        ):
            out = await s.submit_hash("a" * 64)

        assert "cape" in out and out["cape"]["task_id"] == "c1"
        assert "hybrid_analysis" in out
        assert "anyrun" not in out
        assert "joe_sandbox" not in out
        assert "submitted_at" in out["cape"]
