"""Container state through a local stub of medic-dockerproxy.

A stopped container is a result. A proxy that can't be read is an error, and
three of those go blind. Nothing from the proxy JSON is stored except the
typed fields.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

import pytest

from services.medic.sensors.containers import docker_containers
from services.medic.tests.sensors.harness import Rig

CANARY = "CANARY9f3c7a"
STARTED = "2026-10-07T08:12:01.123456789Z"
STARTED_OUT = "2026-10-07T08:12:01.123456Z"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _row(
    service: str,
    name: str,
    cid: str,
    *,
    status: str = "running",
    image: str = "ghcr.io/vigil/backend:1.8.0",
    health: str | None = "healthy",
    restarts: int = 0,
    started: str = STARTED,
    labels: dict[str, str] | None = None,
    env: list[str] | None = None,
) -> tuple[dict, dict]:
    label_map = {"com.docker.compose.service": service, **(labels or {})}
    listed = {
        "Id": cid,
        "Names": [f"/{name}"],
        "Image": "sha256:deadbeef",
        "Labels": label_map,
        "State": status,
        "Status": "Up 2 hours",
        "Created": 1,
    }
    state: dict = {
        "Status": status,
        "StartedAt": started,
        "Error": f"PASSWORD={CANARY}",
    }
    if health is not None:
        state["Health"] = {"Status": health, "FailingStreak": 0, "Log": [CANARY]}
    inspected = {
        "Id": cid,
        "Name": f"/{name}",
        "RestartCount": restarts,
        "State": state,
        "Config": {
            "Image": image,
            "Labels": label_map,
            "Env": env or [f"PASSWORD={CANARY}"],
        },
        "Mounts": [{"Source": CANARY}],
    }
    return listed, inspected


class _Proxy:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.seen: list[str] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    def set(self, path: str, status: int, body: object) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routes[path] = (status, raw)

    async def __aenter__(self) -> Self:
        proxy = self

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                request = await reader.readuntil(b"\r\n")
                parts = request.split()
                target = parts[1].decode() if len(parts) > 1 else ""
                proxy.seen.append(target)
                while await reader.readline() not in (b"\r\n", b""):
                    pass
                status, body = proxy.routes.get(target.split("?", 1)[0], (404, b"{}"))
                head = (
                    f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                )
                writer.write(head.encode() + body)
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


def _load(proxy: _Proxy, rows: list[tuple[dict, dict]]) -> None:
    proxy.set("/containers/json", 200, [row for row, _ in rows])
    for row, inspected in rows:
        proxy.set(f"/containers/{row['Id']}/json", 200, inspected)


@asynccontextmanager
async def _up(rows: list[tuple[dict, dict]]) -> AsyncIterator[_Proxy]:
    async with _Proxy() as proxy:
        _load(proxy, rows)
        yield proxy


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


async def test_running_and_stopped_containers_and_the_shared_tag() -> None:
    rows = [
        _row("backend", "deeptempo-backend-1", "c-backend", restarts=0),
        _row(
            "postgres",
            "deeptempo-postgres-1",
            "c-postgres",
            status="exited",
            health=None,
            image="ghcr.io/vigil/postgres:1.8.0",
            restarts=2,
        ),
        _row("pgadmin", "deeptempo-pgadmin-1", "c-pgadmin", image="pgadmin:9"),
        _row(
            "medic-dockerproxy",
            "deeptempo-medic-dockerproxy-1",
            "c-proxy",
            image="proxy:0",
        ),
        _row("kafka", "deeptempo-kafka-1", "c-kafka", image="kafka:3"),
    ]
    async with _up(rows) as proxy:
        sensor = docker_containers("127.0.0.1", proxy.port)
        rig = Rig([sensor])
        got = await _cycle(rig)
    rig.assert_all_valid()
    assert any(
        target.startswith("/containers/json?") and "all=true" in target
        for target in proxy.seen
    )
    containers = _samples(got, "containers")
    by_instance = {obs["target"]["instance"]: obs for obs in containers}
    assert set(by_instance) == {"deeptempo-backend-1", "deeptempo-postgres-1"}
    backend = by_instance["deeptempo-backend-1"]
    assert backend["outcome"] == "ok"
    assert backend["target"]["service"] == "backend"
    assert backend["epoch"] == f"container:{STARTED_OUT}"
    assert _value(backend, "state")["value"] == "running"
    assert _value(backend, "health")["value"] == "healthy"
    assert _value(backend, "restart_count")["value"] == 0
    assert _value(backend, "started_at")["value"] == STARTED_OUT
    for value in backend["values"]:
        assert value["labels"] == {"service": "backend"}
    stopped = by_instance["deeptempo-postgres-1"]
    assert stopped["outcome"] == "ok"
    assert _value(stopped, "state")["value"] == "exited"
    assert _value(stopped, "health")["state"] == "absent"
    assert _value(stopped, "restart_count")["value"] == 2
    (version,) = _samples(got, "version")
    assert version["outcome"] == "ok"
    assert version["target"] == {"service": "backend", "shape": "compose"}
    assert version["values"] == [
        {
            "key": "version",
            "type": "enum",
            "state": "present",
            "value": "1.8.0",
            "trust": "trusted",
        }
    ]


async def test_a_status_outside_dockers_set_is_unknown() -> None:
    rows = [
        _row(
            "redis",
            "deeptempo-redis-1",
            "c-redis",
            status="melting",
            health="sideways",
            image="redis:1.8.0",
        )
    ]
    async with _up(rows) as proxy:
        rig = Rig([docker_containers("127.0.0.1", proxy.port)])
        got = await _cycle(rig)
    rig.assert_all_valid()
    (obs,) = _samples(got, "containers")
    assert obs["outcome"] == "ok"
    assert _value(obs, "state")["value"] == "unknown"
    assert _value(obs, "health")["value"] == "unknown"


async def test_mixed_tags_differ_from_the_last_uniform_tag_and_stay_put() -> None:
    backend, backend_body = _row("backend", "deeptempo-backend-1", "c-backend")
    postgres, postgres_body = _row(
        "postgres",
        "deeptempo-postgres-1",
        "c-postgres",
        image="ghcr.io/vigil/postgres:1.8.0",
    )
    async with _Proxy() as proxy:
        _load(proxy, [(backend, backend_body), (postgres, postgres_body)])
        sensor = docker_containers("127.0.0.1", proxy.port)
        rig = Rig([sensor])
        first = await _cycle(rig)
        postgres_body["Config"]["Image"] = "localhost:5000/vigil/postgres:1.9.0"
        proxy.set(f"/containers/{postgres['Id']}/json", 200, postgres_body)
        second = await _cycle(rig, sensor.interval_s)
        third = await _cycle(rig, sensor.interval_s)
        backend_body["Config"]["Image"] = "ghcr.io/vigil/backend:1.9.0"
        proxy.set("/containers/c-backend/json", 200, backend_body)
        fourth = await _cycle(rig, sensor.interval_s)
        # The last uniform tag can itself be the token a mixed set would use.
        odd = docker_containers()
        odd._uniform = "diverged"
        assert odd._version_value(["diverged", "other"]) != "diverged"
    rig.assert_all_valid()

    def version_of(observations: list[dict]) -> str:
        (obs,) = _samples(observations, "version")
        return _value(obs, "version")["value"]

    assert version_of(first) == "1.8.0"
    mixed = version_of(second)
    assert mixed != "1.8.0"
    assert version_of(third) == mixed
    assert version_of(fourth) == "1.9.0"

    # The uniform tag itself may be the token used for a mixed set.
    odd = docker_containers("127.0.0.1", proxy.port)
    odd._uniform = "diverged"
    assert odd._version_value(["diverged", "other"]) != "diverged"


async def test_one_failed_inspect_does_not_publish_a_partial_version() -> None:
    rows = [
        _row("backend", "deeptempo-backend-1", "c-backend"),
        _row("redis", "deeptempo-redis-1", "c-redis", image="redis:1.8.0"),
    ]
    async with _up(rows) as proxy:
        proxy.set(
            "/containers/c-redis/json",
            502,
            {"message": "medic-dockerproxy: upstream_reply"},
        )
        rig = Rig([docker_containers("127.0.0.1", proxy.port)])
        got = await _cycle(rig)
    rig.assert_all_valid()
    containers = _samples(got, "containers")
    assert {obs["outcome"] for obs in containers} == {"ok", "error"}
    failed = next(obs for obs in containers if obs["outcome"] == "error")
    assert failed["error"] == {
        "class": "http_status",
        "http_status": 502,
        "detail": "upstream_reply",
    }
    assert _samples(got, "version") == []
    assert rig.health("docker.containers")[-1]["state"] == "degraded"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (502, "upstream_reply"),
        (502, "upstream_unreachable"),
        (503, "busy"),
    ],
)
async def test_proxy_refusal_is_an_error_and_fails_the_cycle(
    status: int, code: str
) -> None:
    async with _Proxy() as proxy:
        proxy.set("/containers/json", status, {"message": f"medic-dockerproxy: {code}"})
        rig = Rig([docker_containers("127.0.0.1", proxy.port)])
        got = await _cycle(rig)
    rig.assert_all_valid()
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert {obs["signal"] for obs in samples} == {"containers", "version"}
    assert [obs["outcome"] for obs in samples] == ["error", "error"]
    assert [obs["values"] for obs in samples] == [[], []]
    assert {obs["error"]["http_status"] for obs in samples} == {status}
    assert {obs["error"]["detail"] for obs in samples} == {code}
    assert {obs["error"]["class"] for obs in samples} == {"http_status"}
    assert rig.health("docker.containers")[-1]["state"] == "degraded"


async def test_a_list_that_is_not_a_list_is_a_parse_error() -> None:
    async with _Proxy() as proxy:
        proxy.set("/containers/json", 200, {"message": "not a list"})
        rig = Rig([docker_containers("127.0.0.1", proxy.port)])
        got = await _cycle(rig)
    samples = [obs for obs in got if obs["kind"] == "sample"]
    assert samples and {obs["error"]["class"] for obs in samples} == {"parse"}
    assert all(obs["values"] == [] for obs in samples)


async def test_unreachable_proxy_is_an_error_then_blind_then_backs_off() -> None:
    sensor = docker_containers("127.0.0.1", _closed_port())
    calls: list[int] = []
    collect = sensor.collect

    async def counting(ctx: object) -> object:
        calls.append(1)
        return await collect(ctx)  # type: ignore[arg-type]

    sensor.collect = counting  # type: ignore[method-assign]
    rig = Rig([sensor])
    first = await _cycle(rig)
    samples = [obs for obs in first if obs["kind"] == "sample"]
    assert samples and {obs["outcome"] for obs in samples} == {"error"}
    assert {obs["error"]["class"] for obs in samples} == {"refused"}
    assert all(obs["values"] == [] for obs in samples)
    await _cycle(rig, sensor.interval_s)
    third = await _cycle(rig, sensor.interval_s)
    assert rig.health(sensor.id)[-1]["state"] == "blind"
    assert any(obs["kind"] == "sensor_health" for obs in third)
    await _cycle(rig, sensor.interval_s)
    assert len(calls) == 3  # backing off: not due again after one interval
    # Gap is 2 × interval, ±10%. A full extra interval clears the jitter.
    await _cycle(rig, sensor.interval_s + sensor.interval_s)
    assert len(calls) == 4
    rig.assert_all_valid()


async def test_a_canary_in_a_label_and_an_env_string_is_not_stored_or_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [
        _row(
            "backend",
            "deeptempo-backend-1",
            "c-backend",
            labels={"note": f"PASSWORD={CANARY}"},
        )
    ]
    caplog.set_level(logging.DEBUG)
    async with _up(rows) as proxy:
        rig = Rig([docker_containers("127.0.0.1", proxy.port)])
        await _cycle(rig)
    rig.assert_all_valid()
    stored = json.dumps(rig.sink.observations)
    assert CANARY not in stored
    assert CANARY not in caplog.text
    assert "sha256:deadbeef" not in stored
    assert "Mounts" not in stored


def test_defaults_point_at_the_proxy() -> None:
    sensor = docker_containers()
    assert (sensor.host, sensor.port) == ("medic-dockerproxy", 8472)
    assert sensor.interval_s == 30
    assert 0 < sensor.timeout_s < sensor.interval_s
    assert sensor.covers == ("containers", "version")
