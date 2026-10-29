"""D6-6: the daemon's /health and /status, Kafka and orchestrator status.

Fixtures are the shapes `services/daemon/metrics.py` returns on main.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from services.medic.sensors.base import SensorContext
from services.medic.sensors.daemon import (
    CALLS,
    STATUS_SIGNALS,
    DaemonHealth,
    DaemonStatus,
    KafkaStatus,
    OrchestratorStatus,
    backend_status_sensors,
)
from services.medic.sensors.gateway import assert_path_subset
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="compose")
START = "2026-10-28T08:12:03.123456+00:00"
EPOCH = "uptime:2026-10-28T08:12:03.123456Z"

HEALTH = {
    "status": "unhealthy",
    "timestamp": "2026-10-28T10:00:00+00:00",
    "uptime_seconds": 6477.5,
    "components": {
        "poller": "running",
        "kafka": "running",
        "processor": "failed: RuntimeError",
        "executor": "running",
        "scheduler": "stopped",
        "orchestrator": "disabled",
    },
}


def _status(**over: Any) -> dict[str, Any]:
    body = {
        "daemon": {"start_time": START, "uptime_seconds": 6477.5},
        "poller": {
            "splunk_polls": 12,
            "splunk_findings": 3,
            "crowdstrike_polls": 0,
            "crowdstrike_findings": 0,
            "webhook_findings": 7,
            "dropped": 0,
            "errors": 1,
            "webhook_rejections": {"crowdstrike": {"bad_signature": 2, "stale": 1}},
            "sources": {"splunk": {"last_poll_time": "2026-10-28T09:59:00"}},
        },
        "kafka": {
            "connected": True,
            "messages_consumed": 10,
            "decode_errors": 4,
            "missing_id_errors": 0,
            "last_error": "password=hunter2kafka broker said no",
            "topics": ["findings"],
            "partitions": [],
        },
        "processor": {
            "processed": 40,
            "triaged": 30,
            "enriched": 20,
            "errors": 2,
            "queue_depth": 0,
            "queue_max": 1000,
            "auto_triage_enabled": True,
        },
        "executor": {"executed": 1, "failed": 0, "errors": 0},
        "scheduler": {"tasks_run": 9, "threat_hunts": 1, "errors": 0},
        "orchestrator": {
            "investigations_created": 6,
            "investigations_completed": 4,
            "investigations_failed": 1,
            "total_cost_usd": 1.25,
            "active_agents": 1,
            "enabled": True,
            "max_concurrent_agents": 3,
            "free_slots": 2,
        },
        "federation": {"polls": 3, "findings": 1, "errors": 0, "dropped": 0},
        "llm_queue_depth": 4,
        "agent_runs_depth": 0,
        "vendors": {"splunk": {"total": 3, "by_status": {"401": 2, "429": 1}}},
    }
    return body | over


class Target:
    """The daemon or the gateway; `status`/`body` can change between ticks."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status, self.body, self.paths = status, body, []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            return httpx.Response(
                self.status,
                content=json.dumps(self.body).encode(),
                headers={"content-type": "application/json"},
            )

        return httpx.MockTransport(handler)


def _refusing() -> httpx.MockTransport:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused") from ConnectionRefusedError()

    return httpx.MockTransport(refuse)


def _samples(obs: list[dict[str, Any]], signal: str) -> list[dict[str, Any]]:
    return [o for o in obs if o["kind"] == "sample" and o["signal"] == signal]


def _values(sample: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
    return {
        (v["key"], tuple(sorted(v.get("labels", {}).items()))): v
        for v in sample["values"]
    }


async def test_health_503_is_a_result_with_every_component_folded() -> None:
    rig = Rig(
        [DaemonHealth("soc-daemon", 9091, transport=Target(503, HEALTH).transport())]
    )
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = _samples(obs, "daemon_health")
    assert sample["outcome"] == "ok"
    assert sample["target"]["instance"] == "soc-daemon:9091"
    got = {k: v["value"] for (k, _), v in _values(sample).items()}
    assert got == {
        "ok": False,
        "result": "unhealthy",
        "components.poller": "running",
        "components.kafka": "running",
        "components.processor": "failed",
        "components.executor": "running",
        "components.scheduler": "stopped",
        "components.orchestrator": "disabled",
        "uptime_seconds": 6477.5,
    }


async def test_a_refused_daemon_reads_down_not_unread() -> None:
    rig = Rig(
        [
            DaemonHealth("soc-daemon", 9091, transport=_refusing()),
            DaemonStatus("soc-daemon", 9091, transport=_refusing()),
        ]
    )
    obs = await rig.tick()
    rig.assert_all_valid()
    (health,) = _samples(obs, "daemon_health")
    assert health["outcome"] == "ok"
    assert {v["key"]: v["value"] for v in health["values"]} == {
        "ok": False,
        "result": "refused",
    }
    for signal in STATUS_SIGNALS:
        (sample,) = _samples(obs, signal)
        assert sample["outcome"] == "error" and sample["error"]["class"] == "refused"


async def test_an_unresolvable_daemon_on_compose_is_not_installed() -> None:
    def no_name(request: httpx.Request) -> httpx.Response:
        import socket

        raise httpx.ConnectError("no name") from socket.gaierror()

    transport = httpx.MockTransport(no_name)
    rig = Rig(
        [
            DaemonHealth("soc-daemon", 9091, transport=transport),
            DaemonStatus("soc-daemon", 9091, transport=transport),
        ]
    )
    obs = []
    for _ in range(4):
        obs += await rig.tick(30)
    rig.assert_all_valid()
    # Compose without the daemon profile: not installed, never blind.
    sample = _samples(obs, "daemon_health")[0]
    assert sample["outcome"] == "error" and sample["error"]["class"] == "not_installed"
    assert rig.health("daemon.health")[-1]["state"] != "blind"
    assert rig.health("daemon.status")[-1]["state"] != "blind"
    # Off Compose, DNS stays an error.
    (reading,) = await DaemonHealth("d", 9091, transport=transport).collect(
        SensorContext(shape="start_sh")
    )
    assert reading.error is not None and reading.error.cls == "dns"


async def test_status_emits_the_rule_keys_with_types_and_the_uptime_epoch() -> None:
    daemon = Target(body=_status())
    rig = Rig(
        [DaemonStatus("soc-daemon", 9091, transport=daemon.transport())], choke=None
    )
    obs = await rig.tick()
    rig.assert_all_valid()
    assert daemon.paths == ["/status"]
    by_signal = {s: _values(_samples(obs, s)[0]) for s in STATUS_SIGNALS}
    for signal in STATUS_SIGNALS:
        assert _samples(obs, signal)[0]["epoch"] == EPOCH
    poller = by_signal["daemon_status.poller"]
    assert poller[("polls", (("source", "splunk"),))]["value"] == 12
    assert poller[("polls", (("source", "crowdstrike"),))]["value"] == 0
    rejected = (("endpoint", "crowdstrike"), ("reason", "bad_signature"))
    assert poller[("webhook_rejections", rejected)]["type"] == "counter"
    assert poller[("webhook_rejections", rejected)]["value"] == 2
    assert {k for k, _ in poller} == {"polls", "webhook_findings", "webhook_rejections"}
    assert {k: v["type"] for (k, _), v in by_signal["daemon_status.kafka"].items()} == {
        "connected": "bool",
        "decode_errors": "counter",
        "missing_id_errors": "counter",
    }
    processor = {
        k: v["value"] for (k, _), v in by_signal["daemon_status.processor"].items()
    }
    assert processor == {
        "processed": 40,
        "triaged": 30,
        "errors": 2,
        "auto_triage_enabled": True,
    }
    orch = {k: v for (k, _), v in by_signal["daemon_status.orchestrator"].items()}
    assert orch["investigations_created"]["type"] == "counter"
    assert orch["free_slots"] == {**orch["free_slots"], "type": "gauge", "value": 2}
    assert by_signal["daemon_status.scheduler"][("tasks_run", ())]["value"] == 9
    vendors = by_signal["daemon_status.vendors"]
    assert vendors[("errors", (("status", "401"), ("vendor", "splunk")))]["value"] == 2
    # Never read: free text, and blocks no rule reads.
    dumped = json.dumps(obs)
    assert "hunter2kafka" not in dumped
    assert "llm_queue_depth" not in dumped and "federation" not in dumped


async def test_a_missing_component_is_absent_not_false_or_zero() -> None:
    # What metrics.py sends with no processor or orchestrator (and an older daemon).
    body = _status(
        processor={"queue_depth": 0, "auto_triage_enabled": False},
        orchestrator={"max_concurrent_agents": 0, "free_slots": 0},
        kafka={},
    )
    rig = Rig([DaemonStatus("d", 9091, transport=Target(body=body).transport())])
    obs = await rig.tick()
    rig.assert_all_valid()
    processor = _values(_samples(obs, "daemon_status.processor")[0])
    orch = _values(_samples(obs, "daemon_status.orchestrator")[0])
    kafka = _values(_samples(obs, "daemon_status.kafka")[0])
    assert processor[("auto_triage_enabled", ())]["state"] == "absent"
    assert orch[("free_slots", ())]["state"] == "absent"
    assert all(v["state"] == "absent" for v in kafka.values())
    assert _samples(obs, "daemon_status.processor")[0]["outcome"] == "ok"


async def test_status_without_a_start_time_is_a_parse_error() -> None:
    body = _status(daemon={})
    readings = await DaemonStatus(
        "d", 9091, transport=Target(body=body).transport()
    ).collect(CTX)
    assert {r.error.cls for r in readings if r.error} == {"parse"}
    assert len(readings) == len(STATUS_SIGNALS)


def test_the_gateway_calls_are_on_outbound() -> None:
    assert_path_subset(CALLS)


async def test_kafka_status_reads_two_flags_and_never_config() -> None:
    body = {
        "enabled": True,
        "daemon_reachable": False,
        "config": {"bootstrap_servers": "broker:9092", "sasl_username": "canaryUser77"},
        "stats": {"connected": False},
    }
    rig = Rig([KafkaStatus("gw", 8471, transport=Target(body=body).transport())])
    obs = await rig.tick()
    rig.assert_all_valid()
    (sample,) = _samples(obs, "kafka_status")
    assert {v["key"]: v["value"] for v in sample["values"]} == {
        "enabled": True,
        "daemon_reachable": False,
    }
    assert "canaryUser77" not in json.dumps(obs) and "broker" not in json.dumps(obs)


async def test_orchestrator_status_reads_three_keys() -> None:
    body = {
        "enabled": True,
        "active_agents": 1,
        "queued": 5,
        "cost": {"hourly_paused": True, "hourly_spent": 3.0},
        "stats": {"investigations_created": 99},
    }
    rig = Rig([OrchestratorStatus("gw", 8471, transport=Target(body=body).transport())])
    (sample,) = _samples(await rig.tick(), "orchestrator_status")
    rig.assert_all_valid()
    assert {v["key"]: v["value"] for v in sample["values"]} == {
        "enabled": True,
        "queued": 5,
        "cost.hourly_paused": True,
    }


async def test_a_relayed_5xx_leaves_the_values_absent() -> None:
    for sensor in backend_status_sensors("gw", 8471):
        sensor.transport = Target(500, {"detail": "db down"}).transport()
        (reading,) = await sensor.collect(CTX)
        assert reading.error is None
        assert all(v.value is None for v in reading.values)


async def test_orchestrator_status_is_never_read_faster_than_300_s() -> None:
    gateway = Target(body={"enabled": False, "queued": 0, "cost": {}})
    rig = Rig([OrchestratorStatus("gw", 8471, transport=gateway.transport())])
    for _ in range(40):  # 20 minutes of 30 s ticks
        await rig.tick(30)
    assert 1 <= len(gateway.paths) <= 1200 // 300 + 1
    assert OrchestratorStatus("gw", 8471).interval_s >= 300
