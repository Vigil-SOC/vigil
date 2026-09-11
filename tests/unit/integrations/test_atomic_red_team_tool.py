"""Stubbed Atomic Red Team execute: trace shape and missing environment_id."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.integrations.atomic_red_team.tool as art


@pytest.mark.unit
class TestExecuteAtomic:
    def test_missing_environment_id_refuses_before_the_runner(self):
        calls: list = []

        def boom(*_a, **_kw):
            calls.append(True)
            raise AssertionError("runner must not be invoked")

        out = art.execute_atomic(
            {"technique": "T1003.001"},
            {"runner_path": "/opt/art", "atomics_path": "/opt/atomics"},
            run=boom,
        )
        assert out == {"error": "environment_id required"}
        assert calls == []

        out = art.execute_atomic(
            {"technique": "T1003.001", "environment_id": "   "},
            {"runner_path": "/opt/art", "atomics_path": "/opt/atomics"},
            run=boom,
        )
        assert out == {"error": "environment_id required"}
        assert calls == []

    def test_stubbed_run_returns_the_action_trace(self):
        recorded = []

        def fake_run(argv, **kwargs):
            recorded.append((list(argv), kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout="[+] T1059.001 executed\n",
                stderr="",
            )

        # A prod-looking id must still run — we do not substring-match "prod".
        out = art.execute_atomic(
            {"technique": "T1059.001", "environment_id": "prod-range"},
            {"runner_path": "/opt/art-runner", "atomics_path": "/opt/atomics"},
            run=fake_run,
        )
        assert recorded, "stub runner was not invoked"
        argv, kwargs = recorded[0]
        assert argv == [
            "/opt/art-runner",
            "--technique",
            "T1059.001",
            "--atomics-path",
            "/opt/atomics",
        ]
        assert kwargs.get("capture_output") is True
        assert out["technique"] == "T1059.001"
        assert out["environment_id"] == "prod-range"
        assert out["command"] == argv
        assert out["exit"] == 0
        assert out["stdout"] == "[+] T1059.001 executed\n"
        assert out["stderr"] == ""
        assert out["started_at"]
        assert out["finished_at"]
        assert "error" not in out
        assert "events" not in out
        assert "telemetry" not in out
