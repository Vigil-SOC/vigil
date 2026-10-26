"""Pod state through a local stub of the Kubernetes API.

A Pending, Failed, or CrashLoopBackOff pod is a result. An API that can't be
read is an error for both signals, and three of those go blind. Nothing from
the pod body is stored except the typed fields, and events are not requested.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlsplit

import pytest

from services.medic.sensors.kube import KubePods, kube_pods
from services.medic.tests.sensors.harness import Rig

CANARY = "CANARY9f3c7a"
STARTED = "2026-10-07T08:12:01.123456789Z"
EARLIER = "2026-10-07T08:00:00Z"
CREATED = "2026-10-07T07:00:00Z"
NS = "vigil"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pod(
    name: str,
    component: str,
    *,
    phase: str = "Running",
    ready: str = "True",
    restarts: int = 0,
    init_restarts: int = 0,
    started: str | None = STARTED,
    earlier: str | None = None,
    created: str = CREATED,
    waiting: str | None = None,
) -> dict:
    statuses: list[dict] = []
    if earlier is not None:
        statuses.append(
            {
                "name": "init-like",
                "restartCount": 0,
                "state": {"running": {"startedAt": earlier}},
            }
        )
    if started is not None or waiting is not None:
        state: dict = (
            {"waiting": {"reason": waiting, "message": CANARY}}
            if waiting
            else {"running": {"startedAt": started}}
        )
        item: dict = {"name": "app", "restartCount": restarts, "state": state}
        if waiting:
            item["lastState"] = {
                "terminated": {"startedAt": started, "message": CANARY}
            }
        statuses.append(item)
    init = []
    if init_restarts:
        init.append(
            {
                "name": "init",
                "restartCount": init_restarts,
                "state": {"terminated": {"startedAt": EARLIER, "reason": "Completed"}},
            }
        )
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "creationTimestamp": created,
            "labels": {"app.kubernetes.io/component": component},
            "annotations": {"note": CANARY},
        },
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "image": "ghcr.io/vigil/backend:9.9.9",
                    "env": [{"name": "PASSWORD", "value": CANARY}],
                }
            ]
        },
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": ready}],
            "containerStatuses": statuses,
            "initContainerStatuses": init,
        },
    }


def _workload(
    kind: str,
    name: str,
    component: str,
    image: str,
    *,
    version: str | None = None,
) -> dict:
    labels = {"app.kubernetes.io/component": component}
    if version is not None:
        labels["app.kubernetes.io/version"] = version
    return {
        "kind": kind,
        "metadata": {"name": name, "labels": labels, "annotations": {"note": CANARY}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "env": [{"name": "PASSWORD", "value": CANARY}],
                        }
                    ]
                }
            }
        },
    }


class _Api:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.seen: list[str] = []
        self.auths: list[str] = []
        self.on_request: Callable[[str], None] | None = None
        self._server: asyncio.Server | None = None
        self.port = 0

    def set(
        self, path: str, body: object, *, status: int = 200, cont: str = ""
    ) -> None:
        self.routes[(path, cont)] = (status, body)

    async def __aenter__(self) -> Self:
        api = self

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                request = await reader.readuntil(b"\r\n")
                parts = request.split()
                target = parts[1].decode() if len(parts) > 1 else ""
                auth = ""
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b""):
                        break
                    if line.lower().startswith(b"authorization:"):
                        auth = line.split(b":", 1)[1].strip().decode()
                api.seen.append(target)
                api.auths.append(auth)
                if api.on_request is not None:
                    api.on_request(target)
                split = urlsplit(target)
                cont = (parse_qs(split.query).get("continue") or [""])[0]
                status, body = api.routes.get((split.path, cont), (404, {"items": []}))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                head = (
                    f"HTTP/1.1 {status} X\r\nContent-Length: {len(raw)}\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(head.encode() + raw)
                await writer.drain()
            finally:
                writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _mount(tmp_path: Path, token: str = "token-one") -> tuple[Path, Path]:
    token_path = tmp_path / "token"
    namespace_path = tmp_path / "namespace"
    token_path.write_text(token + "\n")
    namespace_path.write_text(NS + "\n")
    return token_path, namespace_path


def _sensor(tmp_path: Path, port: int, token: str = "token-one") -> KubePods:
    token_path, namespace_path = _mount(tmp_path, token)
    return KubePods(
        host="127.0.0.1",
        port=port,
        scheme="http",
        token_path=token_path,
        namespace_path=namespace_path,
    )


def _list(items: list[dict], cont: str | None = None) -> dict:
    meta: dict[str, str] = {}
    if cont:
        meta["continue"] = cont
    return {"kind": "List", "metadata": meta, "items": items}


@asynccontextmanager
async def _up(tmp_path: Path) -> AsyncIterator[tuple[_Api, KubePods]]:
    async with _Api() as api:
        yield api, _sensor(tmp_path, api.port)


async def _cycle(rig: Rig, advance: float = 0) -> list[dict]:
    before = len(rig.sink.observations)
    rig.clock.advance(advance)
    await rig.scheduler.tick()
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        pending = [
            state.task
            for state in rig.scheduler._states.values()
            if state.task is not None and not state.task.done()
        ]
        if not pending:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    rig.pipeline.drain()
    return rig.sink.observations[before:]


def _samples(observations: list[dict], signal: str) -> list[dict]:
    return [
        obs
        for obs in observations
        if obs["kind"] == "sample" and obs["signal"] == signal
    ]


def _value(obs: dict, key: str) -> dict:
    return next(value for value in obs["values"] if value["key"] == key)


def _paths(api: _Api) -> list[str]:
    return [urlsplit(target).path for target in api.seen]


def _queries(api: _Api) -> list[dict[str, list[str]]]:
    return [parse_qs(urlsplit(target).query) for target in api.seen]


def test_defaults_point_at_the_in_cluster_api() -> None:
    sensor = kube_pods()
    assert (sensor.host, sensor.port, sensor.scheme) == (
        "kubernetes.default.svc",
        443,
        "https",
    )
    root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    assert sensor.token_path == root / "token"
    assert sensor.ca_path == root / "ca.crt"
    assert sensor.namespace_path == root / "namespace"
    assert sensor.interval_s == 30
    assert 0 < sensor.timeout_s < sensor.interval_s
    assert sensor.covers == ("pods", "version")
    assert getattr(sensor, "url", None) is None


async def test_pod_states_versions_and_a_skipped_component(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    running = _pod(
        "vigil-backend-a",
        "backend",
        restarts=0,
        earlier=EARLIER,
    )
    replica = _pod("vigil-backend-b", "backend", restarts=1)
    crash = _pod(
        "vigil-backend-crash",
        "backend",
        ready="False",
        restarts=4,
        init_restarts=1,
        waiting="CrashLoopBackOff",
    )
    pending = _pod(
        "vigil-backend-pending",
        "backend",
        phase="Pending",
        ready="False",
        started=None,
    )
    failed = _pod(
        "vigil-backend-failed",
        "backend",
        phase="Failed",
        ready="False",
        restarts=2,
    )
    daemon = _pod("vigil-daemon-0", "daemon")
    skipped = [
        _pod(name, component)
        for name, component in (
            ("vigil-pgadmin-0", "pgadmin"),
            ("vigil-mcp-0", "mcp-runner"),
            ("vigil-db-init-0", "db-init"),
            ("vigil-splunk-0", "splunk"),
        )
    ]
    backend = _workload(
        "Deployment",
        "vigil-backend",
        "backend",
        "ghcr.io/vigil/backend:9.9.9",
        version="1.8.0",
    )
    daemon_set = _workload(
        "StatefulSet",
        "vigil-daemon",
        "daemon",
        "registry.example:5000/vigil/daemon:2.4.1",
    )
    redis = _workload(
        "StatefulSet",
        "vigil-redis",
        "redis",
        "ghcr.io/vigil/redis@sha256:deadbeef",
    )
    pgadmin = _workload(
        "Deployment", "vigil-pgadmin", "pgadmin", "pgadmin:4", version="4.0.0"
    )
    pods = [running, replica, crash, pending, failed, daemon, *skipped]
    caplog.set_level(logging.DEBUG)
    async with _up(tmp_path) as (api, sensor):
        api.set(f"/api/v1/namespaces/{NS}/pods", _list(pods))
        api.set(f"/apis/apps/v1/namespaces/{NS}/deployments", _list([backend, pgadmin]))
        api.set(
            f"/apis/apps/v1/namespaces/{NS}/statefulsets",
            _list([daemon_set, redis]),
        )
        rig = Rig([sensor])
        first = await _cycle(rig)
        crash["status"]["containerStatuses"][0]["restartCount"] = 6
        second = await _cycle(rig, sensor.interval_s)
    rig.assert_all_valid()
    assert all("/events" not in path and "/log" not in path for path in _paths(api))
    assert all("watch" not in query for query in _queries(api))

    pods_out = _samples(first, "pods")
    by_instance = {obs["target"]["instance"]: obs for obs in pods_out}
    assert set(by_instance) == {
        "vigil-backend-a",
        "vigil-backend-b",
        "vigil-backend-crash",
        "vigil-backend-pending",
        "vigil-backend-failed",
        "vigil-daemon-0",
    }
    ready = by_instance["vigil-backend-a"]
    assert ready["outcome"] == "ok"
    assert ready["target"]["service"] == "backend"
    assert ready["epoch"] == "pod:uid-vigil-backend-a"
    assert _value(ready, "phase")["value"] == "Running"
    assert _value(ready, "ready")["value"] is True
    assert _value(ready, "restart_count")["value"] == 0
    for value in ready["values"]:
        assert value["labels"] == {"service": "backend", "pod": "vigil-backend-a"}
    other = by_instance["vigil-backend-b"]
    assert other["outcome"] == "ok"
    assert other["target"]["service"] == "backend"
    assert _value(other, "restart_count")["value"] == 1
    looping = by_instance["vigil-backend-crash"]
    assert looping["outcome"] == "ok"
    assert _value(looping, "phase")["value"] == "Running"
    assert _value(looping, "ready")["value"] is False
    assert _value(looping, "restart_count")["value"] == 5
    assert looping["epoch"] == "pod:uid-vigil-backend-crash"
    waiting = by_instance["vigil-backend-pending"]
    assert waiting["outcome"] == "ok"
    assert _value(waiting, "phase")["value"] == "Pending"
    assert _value(waiting, "ready")["value"] is False
    assert _value(waiting, "restart_count")["value"] == 0
    assert waiting["epoch"] == "pod:uid-vigil-backend-pending"
    dead = by_instance["vigil-backend-failed"]
    assert dead["outcome"] == "ok"
    assert _value(dead, "phase")["value"] == "Failed"
    assert by_instance["vigil-daemon-0"]["target"]["service"] == "soc-daemon"

    versions = _samples(first, "version")
    by_service = {obs["target"]["service"]: obs for obs in versions}
    assert set(by_service) == {"backend", "soc-daemon", "redis"}
    assert _value(by_service["backend"], "version")["value"] == "1.8.0"
    assert _value(by_service["backend"], "version")["labels"] == {"service": "backend"}
    assert _value(by_service["soc-daemon"], "version")["value"] == "2.4.1"
    assert _value(by_service["soc-daemon"], "version")["labels"] == {
        "service": "soc-daemon"
    }
    assert _value(by_service["redis"], "version")["state"] == "absent"
    assert "diverged" not in json.dumps(versions)
    assert "9.9.9" not in json.dumps(by_service["backend"])

    again = {obs["target"]["instance"]: obs for obs in _samples(second, "pods")}
    assert _value(again["vigil-backend-crash"], "restart_count")["value"] == 7
    assert again["vigil-backend-crash"]["outcome"] == "ok"
    stored = json.dumps(rig.sink.observations)
    assert CANARY not in stored
    assert CANARY not in caplog.text
    assert "CrashLoopBackOff" not in stored
    assert "deadbeef" not in stored
    assert "9.9.9" not in stored


async def test_a_deleted_pod_emits_nothing(tmp_path: Path) -> None:
    kept = _pod("vigil-backend-a", "backend")
    gone = _pod("vigil-backend-b", "backend")
    async with _up(tmp_path) as (api, sensor):
        api.set(f"/api/v1/namespaces/{NS}/pods", _list([kept, gone]))
        api.set(f"/apis/apps/v1/namespaces/{NS}/deployments", _list([]))
        api.set(f"/apis/apps/v1/namespaces/{NS}/statefulsets", _list([]))
        rig = Rig([sensor])
        first = await _cycle(rig)
        api.set(f"/api/v1/namespaces/{NS}/pods", _list([kept]))
        second = await _cycle(rig, sensor.interval_s)
    rig.assert_all_valid()
    assert {obs["target"]["instance"] for obs in _samples(first, "pods")} == {
        "vigil-backend-a",
        "vigil-backend-b",
    }
    assert {obs["target"]["instance"] for obs in _samples(second, "pods")} == {
        "vigil-backend-a"
    }
    assert [obs["signal"] for obs in second if obs["kind"] == "sample"] == ["pods"]


async def test_a_later_page_failure_publishes_no_pod_readings(tmp_path: Path) -> None:
    async with _up(tmp_path) as (api, sensor):
        api.set(
            f"/api/v1/namespaces/{NS}/pods",
            _list([_pod("vigil-backend-a", "backend")], cont="next"),
        )
        api.set(
            f"/api/v1/namespaces/{NS}/pods",
            b'{"message":"' + CANARY.encode() + b'"}',
            status=500,
            cont="next",
        )
        api.set(f"/apis/apps/v1/namespaces/{NS}/deployments", _list([]))
        api.set(f"/apis/apps/v1/namespaces/{NS}/statefulsets", _list([]))
        rig = Rig([sensor])
        got = await _cycle(rig)
    rig.assert_all_valid()
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert [obs["signal"] for obs in samples] == ["pods", "version"]
    assert {obs["outcome"] for obs in samples} == {"error"}
    assert all(
        obs["error"] == {"class": "http_status", "http_status": 500} for obs in samples
    )
    assert all(obs["values"] == [] for obs in samples)
    assert {obs["target"]["service"] for obs in samples} == {"medic"}
    assert any(query.get("continue") == ["next"] for query in _queries(api))
    assert f"/apis/apps/v1/namespaces/{NS}/deployments" not in _paths(api)
    assert CANARY not in json.dumps(rig.sink.observations)
    assert rig.health(sensor.id)[-1]["state"] == "degraded"


async def test_a_deployment_failure_drops_the_pod_list(tmp_path: Path) -> None:
    async with _up(tmp_path) as (api, sensor):
        api.set(
            f"/api/v1/namespaces/{NS}/pods",
            _list([_pod("vigil-backend-a", "backend")]),
        )
        api.set(
            f"/apis/apps/v1/namespaces/{NS}/deployments",
            {"message": "no"},
            status=403,
        )
        rig = Rig([sensor])
        got = await _cycle(rig)
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert {obs["signal"] for obs in samples} == {"pods", "version"}
    assert {obs["outcome"] for obs in samples} == {"error"}
    assert all(
        obs["error"] == {"class": "auth", "http_status": 403} and obs["values"] == []
        for obs in samples
    )
    assert all(obs["target"]["service"] == "medic" for obs in samples)
    assert "vigil-backend-a" not in json.dumps(rig.sink.observations)


async def test_pagination_follows_continue_and_rereads_the_token(
    tmp_path: Path,
) -> None:
    token_path, namespace_path = _mount(tmp_path, "first-token")
    rotated = False

    def rotate(target: str) -> None:
        nonlocal rotated
        if not rotated and "continue" not in target:
            rotated = True
            token_path.write_text("second-token\n")

    async with _Api() as api:
        sensor = KubePods(
            host="127.0.0.1",
            port=api.port,
            scheme="http",
            token_path=token_path,
            namespace_path=namespace_path,
        )
        api.on_request = rotate
        api.set(
            f"/api/v1/namespaces/{NS}/pods",
            _list([_pod("vigil-backend-a", "backend")], cont="next"),
        )
        api.set(
            f"/api/v1/namespaces/{NS}/pods",
            _list([_pod("vigil-backend-b", "backend")]),
            cont="next",
        )
        api.set(f"/apis/apps/v1/namespaces/{NS}/deployments", _list([]))
        api.set(f"/apis/apps/v1/namespaces/{NS}/statefulsets", _list([]))
        rig = Rig([sensor])
        first = await _cycle(rig)
        token_path.write_text("third-token\n")
        await _cycle(rig, sensor.interval_s)
    rig.assert_all_valid()
    assert {obs["target"]["instance"] for obs in _samples(first, "pods")} == {
        "vigil-backend-a",
        "vigil-backend-b",
    }
    assert api.auths[0] == "Bearer first-token"
    assert api.auths[1:4] == ["Bearer second-token"] * 3
    assert api.auths[4:] == ["Bearer third-token"] * 4
    assert any(query.get("continue") == ["next"] for query in _queries(api))


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_an_error_for_both_signals(
    tmp_path: Path, status: int
) -> None:
    async with _up(tmp_path) as (api, sensor):
        body = {"message": CANARY}
        api.set(f"/api/v1/namespaces/{NS}/pods", body, status=status)
        api.set(f"/apis/apps/v1/namespaces/{NS}/deployments", body, status=status)
        api.set(f"/apis/apps/v1/namespaces/{NS}/statefulsets", body, status=status)
        rig = Rig([sensor])
        got = await _cycle(rig)
    rig.assert_all_valid()
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert [obs["signal"] for obs in samples] == ["pods", "version"]
    assert all(
        obs["error"] == {"class": "auth", "http_status": status} for obs in samples
    )
    assert {obs["target"]["service"] for obs in samples} == {"medic"}
    assert CANARY not in json.dumps(rig.sink.observations)


async def test_unreachable_api_is_an_error_then_blind(tmp_path: Path) -> None:
    sensor = _sensor(tmp_path, _closed_port())
    rig = Rig([sensor])
    first = await _cycle(rig)
    samples = [obs for obs in first if obs["kind"] == "sample"]
    assert [obs["signal"] for obs in samples] == ["pods", "version"]
    assert {obs["outcome"] for obs in samples} == {"error"}
    assert {obs["error"]["class"] for obs in samples} == {"refused"}
    assert all(
        obs["values"] == [] and obs["target"]["service"] == "medic" for obs in samples
    )
    await _cycle(rig, sensor.interval_s)
    assert rig.health(sensor.id)[-1]["state"] != "blind"
    third = await _cycle(rig, sensor.interval_s)
    assert rig.health(sensor.id)[-1]["state"] == "blind"
    assert any(obs["kind"] == "sensor_health" for obs in third)
    rig.assert_all_valid()


async def test_a_malformed_ca_is_a_tls_error(tmp_path: Path) -> None:
    sensor = _sensor(tmp_path, _closed_port())
    sensor.scheme = "https"
    ca_path = tmp_path / "ca.crt"
    ca_path.write_text("not a pem")
    sensor.ca_path = ca_path
    rig = Rig([sensor])
    samples = _samples(await _cycle(rig), "pods")
    assert [obs["error"]["class"] for obs in samples] == ["tls"]


async def test_a_list_that_is_not_a_list_is_a_parse_error(tmp_path: Path) -> None:
    async with _up(tmp_path) as (api, sensor):
        api.set(f"/api/v1/namespaces/{NS}/pods", {"message": "not a list"})
        rig = Rig([sensor])
        got = await _cycle(rig)
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert samples and {obs["error"]["class"] for obs in samples} == {"parse"}
    assert all(obs["values"] == [] for obs in samples)
