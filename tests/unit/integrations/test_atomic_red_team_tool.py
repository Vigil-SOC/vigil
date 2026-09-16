"""Stubbed Atomic Red Team execute: trace shape and refusals before the runner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.integrations.atomic_red_team.tool as art

CONFIG = {"runner_path": "/opt/art-runner", "atomics_path": "/opt/atomics"}


def _refusing_runner(calls: list):
    def boom(*_a, **_kw):
        calls.append(True)
        raise AssertionError("runner must not be invoked")

    return boom


@pytest.mark.unit
class TestExecuteAtomic:
    def test_missing_environment_id_refuses_before_the_runner(self):
        calls: list = []
        for args in (
            {"technique": "T1003.001", "hostname": "dc01"},
            {"technique": "T1003.001", "hostname": "dc01", "environment_id": "   "},
        ):
            out = art.execute_atomic(args, CONFIG, run=_refusing_runner(calls))
            assert out == {"error": "environment_id required"}
        assert calls == []

    def test_missing_hostname_refuses_before_the_runner(self):
        calls: list = []
        for args in (
            {"technique": "T1003.001", "environment_id": "range-1"},
            {"technique": "T1003.001", "environment_id": "range-1", "hostname": " "},
        ):
            out = art.execute_atomic(args, CONFIG, run=_refusing_runner(calls))
            assert out == {"error": "hostname required"}
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
            {
                "technique": "T1059.001",
                "environment_id": "prod-range",
                "hostname": "ws01.corp.local",
            },
            CONFIG,
            run=fake_run,
        )
        assert recorded, "stub runner was not invoked"
        argv, kwargs = recorded[0]
        assert argv == [
            "/opt/art-runner",
            "--technique",
            "T1059.001",
            "--hostname",
            "ws01.corp.local",
            "--atomics-path",
            "/opt/atomics",
        ]
        assert kwargs.get("capture_output") is True
        # Reconstruction spelling: the trace is a step, not a tool-private shape.
        assert out["technique_id"] == "T1059.001"
        assert out["hostname"] == "ws01.corp.local"
        assert out["environment_id"] == "prod-range"
        assert out["command"] == argv
        assert out["exit"] == 0
        assert out["stdout"] == "[+] T1059.001 executed\n"
        assert out["stderr"] == ""
        assert out["started_at"]
        assert out["ended_at"]
        for old_key in ("technique", "finished_at", "error", "events", "telemetry"):
            assert old_key not in out

    def test_timeout_keeps_the_step_shape(self):
        def slow_run(argv, **_kwargs):
            raise art.subprocess.TimeoutExpired(argv, art.RUNNER_TIMEOUT)

        out = art.execute_atomic(
            {"technique": "T1047", "environment_id": "range-1", "hostname": "ws02"},
            CONFIG,
            run=slow_run,
        )
        assert out["error"] == "runner timed out"
        assert out["technique_id"] == "T1047"
        assert out["hostname"] == "ws02"
        assert out["exit"] is None
        assert out["started_at"] and out["ended_at"]
        assert "finished_at" not in out

    @pytest.mark.asyncio
    async def test_schema_requires_hostname(self):
        (tool,) = await art.handle_list_tools()
        assert tool.name == "atomic_red_team_execute"
        schema = tool.model_dump(by_alias=True)["inputSchema"]
        assert set(schema["required"]) == {"technique", "environment_id", "hostname"}
        assert "hostname" in schema["properties"]
