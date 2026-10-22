"""Helm pods and version through a fake API server and harness.Rig."""

from __future__ import annotations

import logging
import socket
import ssl

import httpx
import pytest

from services.medic.sensors import SensorContext
from services.medic.sensors.kube import Kube
from services.medic.sensors.scheduler import TICK_S
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="helm")
CANARY_ENV = "canaryKubeEnv77aa"
CANARY_ANN = "canaryKubeAnn77aa"
STARTED = "2026-10-07T08:12:01Z"
CREATED = "2026-10-07T08:00:00Z"


def _no_canary(payload: object) -> None:
    text = repr(payload)
    assert CANARY_ENV not in text
    assert CANARY_ANN not in text


def _pod(
    name: str,
    component: str,
    *,
    phase: str = "Running",
    ready: str = "True",
    restarts: int = 0,
    init_restarts: int = 0,
    started: str = STARTED,
    init_started: str = "2026-10-07T08:00:01Z",
    created: str = CREATED,
    waiting: str | None = None,
) -> dict:
    state: dict = {"running": {"startedAt": started}}
    last: dict = {}
    if waiting is not None:
        state = {"waiting": {"reason": waiting}}
        last = {"terminated": {"reason": waiting, "startedAt": started}}
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"app.kubernetes.io/component": component},
            "annotations": {"note": CANARY_ANN},
        },
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "image": "ghcr.io/vigil/backend:9.9.9",
                    "env": [{"name": "TOKEN", "value": CANARY_ENV}],
                }
            ]
        },
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": ready}],
            "initContainerStatuses": [
                {
                    "name": "init",
                    "restartCount": init_restarts,
                    "state": {"terminated": {"startedAt": init_started}},
                }
            ],
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": restarts,
                    "state": state,
                    "lastState": last,
                }
            ],
        },
    }


def _workload(
    name: str,
    component: str,
    *,
    version: str | None = None,
    image: str = "ghcr.io/vigil/backend:9.9.9",
) -> dict:
    labels = {"app.kubernetes.io/component": component}
    if version is not None:
        labels["app.kubernetes.io/version"] = version
    return {
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {"note": CANARY_ANN},
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "env": [{"name": "TOKEN", "value": CANARY_ENV}],
                        }
                    ]
                }
            }
        },
    }


class Api:
    def __init__(self, tmp_path) -> None:
        self.pods: list[dict] = []
        self.deployments: list[dict] = []
        self.statefulsets: list[dict] = []
        self.pod_pages: dict[str, tuple[list[dict], str]] | None = None
        self.status = 200
        self.token = tmp_path / "token"
        self.token.write_text("token-a\n")
        self.namespace = tmp_path / "namespace"
        self.namespace.write_text("vigil\n")
        self.urls: list[str] = []
        self.auths: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(request.url.path)
        self.auths.append(request.headers.get("authorization", ""))
        if self.status != 200:
            return httpx.Response(self.status, json={"message": CANARY_ENV})
        path = request.url.path
        if path.endswith(("/events", "/log")) or "/pods/" in path:
            return httpx.Response(200, json={"message": CANARY_ENV})
        if path.endswith("/pods"):
            if self.pod_pages is not None:
                items, nxt = self.pod_pages[request.url.params.get("continue", "")]
                meta = {"continue": nxt} if nxt else {}
                return httpx.Response(200, json={"items": items, "metadata": meta})
            return httpx.Response(200, json={"items": self.pods})
        if path.endswith("/deployments"):
            return httpx.Response(200, json={"items": self.deployments})
        if path.endswith("/statefulsets"):
            return httpx.Response(200, json={"items": self.statefulsets})
        return httpx.Response(404, json={"message": CANARY_ENV})

    def sensor(self) -> Kube:
        return Kube(
            token_path=self.token,
            namespace_path=self.namespace,
            api="https://kubernetes.default.svc",
            transport=self.transport,
        )


def _values(obs: dict) -> dict:
    return {v["key"]: v["value"] for v in obs["values"]}


async def _drive(rig: Rig, advance: float = 0) -> None:
    """Run one tick and let collect() finish.

    Token reads go through `asyncio.to_thread`. The rig's own `sleep(0)` loop
    returns before that thread does, and the next fake-clock jump would then
    cancel the still-running call.
    """
    await rig.tick(advance)
    task = rig.scheduler._states["kube"].task
    if task is not None and not task.done():
        await task
    rig.pipeline.drain()


async def _once(sensor: Kube) -> list[dict]:
    rig = Rig([sensor])
    await _drive(rig)
    rig.assert_all_valid()
    _no_canary(rig.sink.observations)
    return [o for o in rig.sink.of("sample")]


def _signal(samples: list[dict], signal: str) -> list[dict]:
    return [o for o in samples if o["signal"] == signal]


async def test_running_ready_pod_and_two_replicas(tmp_path) -> None:
    api = Api(tmp_path)
    api.pods = [
        _pod("backend-a", "backend", restarts=3, init_restarts=2),
        _pod("backend-b", "backend", restarts=1, init_restarts=0),
    ]
    samples = _signal(await _once(api.sensor()), "pods")
    assert [o["target"]["instance"] for o in samples] == ["backend-a", "backend-b"]
    assert samples[0]["target"]["service"] == "backend"
    assert samples[0]["outcome"] == "ok"
    assert samples[0]["epoch"] == "container:" + STARTED
    assert _values(samples[0]) == {
        "phase": "Running",
        "ready": True,
        "restart_count": 5,
    }
    assert samples[0]["values"][0]["labels"] == {
        "service": "backend",
        "pod": "backend-a",
    }
    assert _values(samples[1])["restart_count"] == 1
    assert all(u.endswith(("/pods", "/deployments", "/statefulsets")) for u in api.urls)
    assert not any("events" in u for u in api.urls)


async def test_pending_pod_epochs_from_creation(tmp_path) -> None:
    api = Api(tmp_path)
    created = "2026-10-07T08:00:00+00:00"
    api.pods = [
        _pod(
            "backend-pending",
            "backend",
            phase="Pending",
            ready="False",
            started="",
            created=created,
        )
    ]
    # No container has started: drop the statuses so creation is the epoch.
    api.pods[0]["status"]["containerStatuses"] = []
    api.pods[0]["status"]["initContainerStatuses"] = []
    (sample,) = _signal(await _once(api.sensor()), "pods")
    assert _values(sample) == {"phase": "Pending", "ready": False, "restart_count": 0}
    assert sample["epoch"] == "container:2026-10-07T08:00:00Z"
    assert sample["outcome"] == "ok"


async def test_crashloop_and_failed_are_results_and_restarts_rise(tmp_path) -> None:
    api = Api(tmp_path)
    api.pods = [
        _pod(
            "backend-crash",
            "backend",
            ready="False",
            restarts=3,
            waiting="CrashLoopBackOff",
        ),
        _pod("backend-failed", "backend", phase="Failed", ready="False", restarts=1),
    ]
    sensor = api.sensor()
    rig = Rig([sensor])
    await _drive(rig)
    api.pods[0] = _pod(
        "backend-crash",
        "backend",
        ready="False",
        restarts=4,
        waiting="CrashLoopBackOff",
        started="2026-10-07T08:13:01Z",
    )
    await _drive(rig, 30)
    rig.assert_all_valid()
    _no_canary(rig.sink.observations)
    assert "CrashLoopBackOff" not in repr(rig.sink.observations)
    pods = _signal(rig.sink.of("sample"), "pods")
    crash = [o for o in pods if o["target"]["instance"] == "backend-crash"]
    assert [o["outcome"] for o in crash] == ["ok", "ok"]
    assert [_values(o)["restart_count"] for o in crash] == [3, 4]
    assert _values(crash[0])["phase"] == "Running"
    assert _values(crash[0])["ready"] is False
    assert crash[1]["epoch"] == "container:2026-10-07T08:13:01Z"
    failed = next(o for o in pods if o["target"]["instance"] == "backend-failed")
    assert failed["outcome"] == "ok"
    assert _values(failed)["phase"] == "Failed"


async def test_a_deleted_pod_emits_no_series_and_no_tombstone(tmp_path) -> None:
    api = Api(tmp_path)
    api.pods = [_pod("backend-a", "backend"), _pod("backend-b", "backend")]
    sensor = api.sensor()
    rig = Rig([sensor])
    await _drive(rig)
    api.pods = [_pod("backend-b", "backend")]
    await _drive(rig, 30)
    rig.assert_all_valid()
    cycles = []
    current: list[dict] = []
    for obs in rig.sink.observations:
        if obs["kind"] == "sensor_health" and current:
            cycles.append(current)
            current = []
        elif obs["kind"] == "sample":
            current.append(obs)
    if current:
        cycles.append(current)
    first, second = ([o for o in cycle if o["signal"] == "pods"] for cycle in cycles)
    assert {o["target"]["instance"] for o in first} == {"backend-a", "backend-b"}
    assert {o["target"]["instance"] for o in second} == {"backend-b"}
    assert "tombstone" not in repr(rig.sink.observations)


async def test_version_is_one_series_per_service(tmp_path) -> None:
    api = Api(tmp_path)
    api.deployments = [
        _workload(
            "backend", "backend", version="1.4.0", image="ghcr.io/vigil/backend:9.9.9"
        ),
        _workload(
            "agent-worker",
            "agent-worker",
            image="registry.example:5000/vigil/agent-worker:2.0.1",
        ),
        _workload("mcp", "mcp-runner", version="0.1.0"),
    ]
    api.statefulsets = [
        _workload("backend-sts", "backend", version="8.8.8"),
        _workload("daemon", "daemon", version="3.1.0"),
        _workload("splunk", "splunk", version="1"),
    ]
    samples = _signal(await _once(api.sensor()), "version")
    got = {o["target"]["service"]: _values(o)["version"] for o in samples}
    assert got == {"backend": "1.4.0", "agent-worker": "2.0.1", "soc-daemon": "3.1.0"}
    assert all(o["target"]["instance"] == o["target"]["service"] for o in samples)
    assert all(
        o["values"][0]["labels"] == {"service": o["target"]["service"]} for o in samples
    )
    text = repr(samples)
    assert "9.9.9" not in text and "8.8.8" not in text and "mcp-runner" not in text


async def test_components_outside_the_enum_are_skipped(tmp_path) -> None:
    api = Api(tmp_path)
    api.pods = [
        _pod("mcp-0", "mcp-runner"),
        _pod("db-init-abc", "db-init"),
        _pod("pgadmin-0", "pgadmin"),
        _pod("splunk-0", "splunk"),
        _pod("loose", "frontend"),
        _pod("daemon-0", "daemon"),
    ]
    samples = await _once(api.sensor())
    pods = _signal(samples, "pods")
    assert [o["target"]["service"] for o in pods] == ["soc-daemon"]
    assert pods[0]["target"]["instance"] == "daemon-0"
    assert pods[0]["values"][0]["labels"]["service"] == "soc-daemon"
    assert _signal(samples, "version") == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_is_an_error_on_both_signals(tmp_path, status: int) -> None:
    api = Api(tmp_path)
    api.status = status
    readings = await api.sensor().collect(CTX)
    assert [
        (r.signal, r.service, r.error.cls, r.error.http_status, r.values)
        for r in readings
    ] == [
        ("pods", "medic", "auth", status, ()),
        ("version", "medic", "auth", status, ()),
    ]
    _no_canary(readings)
    assert api.urls == ["/api/v1/namespaces/vigil/pods"]
    assert not any("events" in u for u in api.urls)


async def test_a_missing_token_fails_the_collect(tmp_path) -> None:
    api = Api(tmp_path)
    api.token.unlink()
    readings = await api.sensor().collect(CTX)
    assert [(r.signal, r.service, r.error.cls) for r in readings] == [
        ("pods", "medic", "other"),
        ("version", "medic", "other"),
    ]
    assert all(r.values == () for r in readings)
    assert api.urls == []


async def test_unreachable_api_is_blind_after_three_cycles(tmp_path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    api = Api(tmp_path)
    sensor = Kube(
        token_path=api.token,
        namespace_path=api.namespace,
        api="https://kubernetes.default.svc",
        transport=httpx.MockTransport(boom),
    )
    rig = Rig([sensor])
    for _ in range(8):
        await _drive(rig, TICK_S)
    states = [h["state"] for h in rig.health("kube")]
    assert "blind" in states
    assert states.index("blind") >= 2
    errors = [o for o in rig.sink.of("sample") if o["outcome"] == "error"]
    assert errors
    assert {o["error"]["class"] for o in errors} == {"timeout"}
    assert {o["error"]["detail"] for o in errors} == {"api timed out"}
    assert {o["signal"] for o in errors} == {"pods", "version"}
    assert {o["target"]["service"] for o in errors} == {"medic"}
    rig.assert_all_valid()


@pytest.mark.parametrize(
    ("raised", "cls"),
    [
        (httpx.ConnectError("no route"), "other"),
        (httpx.ConnectError("refused"), "refused"),
    ],
)
async def test_failures_to_reach_the_api_are_errors(
    tmp_path, raised: Exception, cls: str
) -> None:
    if cls == "refused":
        raised = httpx.ConnectError("refused")
        raised.__cause__ = ConnectionRefusedError()

    def boom(request: httpx.Request) -> httpx.Response:
        raise raised

    api = Api(tmp_path)
    sensor = Kube(
        token_path=api.token,
        namespace_path=api.namespace,
        api="https://kubernetes.default.svc",
        transport=httpx.MockTransport(boom),
    )
    readings = await sensor.collect(CTX)
    assert [r.error.cls for r in readings] == [cls, cls]
    assert all(r.values == () for r in readings)


@pytest.mark.parametrize(
    ("cause", "cls"),
    [(socket.gaierror(8, "nodename"), "dns"), (ssl.SSLError("bad"), "tls")],
)
async def test_dns_and_tls_are_errors(tmp_path, cause: Exception, cls: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x") from cause

    api = Api(tmp_path)
    sensor = Kube(
        token_path=api.token,
        namespace_path=api.namespace,
        api="https://kubernetes.default.svc",
        transport=httpx.MockTransport(boom),
    )
    readings = await sensor.collect(CTX)
    assert [r.error.cls for r in readings] == [cls, cls]


async def test_a_bad_body_is_parse_and_stays_out_of_the_error(tmp_path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CANARY_ENV.encode())

    api = Api(tmp_path)
    sensor = Kube(
        token_path=api.token,
        namespace_path=api.namespace,
        api="https://kubernetes.default.svc",
        transport=httpx.MockTransport(boom),
    )
    readings = await sensor.collect(CTX)
    assert [r.error.cls for r in readings] == ["parse", "parse"]
    _no_canary(readings)


async def test_an_oversized_list_is_too_large(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("services.medic.sensors.kube._MAX_BODY", 32)

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"items":"' + CANARY_ENV.encode() + b'"' * 8 + b"}"
        )

    api = Api(tmp_path)
    sensor = Kube(
        token_path=api.token,
        namespace_path=api.namespace,
        api="https://kubernetes.default.svc",
        transport=httpx.MockTransport(boom),
    )
    readings = await sensor.collect(CTX)
    assert readings[0].error.cls == "too_large"
    _no_canary(readings)


async def test_list_follows_continue_and_rereads_the_token(tmp_path) -> None:
    api = Api(tmp_path)
    api.pod_pages = {
        "": ([_pod("backend-a", "backend")], "next-page"),
        "next-page": ([_pod("backend-b", "backend")], ""),
    }

    first = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal first
        if (
            first
            and request.url.path.endswith("/pods")
            and "continue" not in request.url.params
        ):
            first = False
            api.token.write_text("token-b\n")
        return api.handle(request)

    api.transport = httpx.MockTransport(handle)
    sensor = api.sensor()
    readings = await sensor.collect(CTX)
    names = {r.instance for r in readings if r.signal == "pods"}
    assert names == {"backend-a", "backend-b"}
    assert api.auths[0] == "Bearer token-a"
    assert api.auths[1] == "Bearer token-b"
    assert "next-page" not in repr(readings)
    _no_canary(readings)
    api.token.write_text("token-c\n")
    await sensor.collect(CTX)
    assert api.auths[-1] == "Bearer token-c"


async def test_canary_is_absent_from_the_sink_and_logs(tmp_path, caplog) -> None:
    api = Api(tmp_path)
    api.pods = [_pod("backend-a", "backend", restarts=1, init_restarts=1)]
    api.deployments = [_workload("backend", "backend", version="1.4.0")]
    api.statefulsets = [_workload("daemon", "daemon", version="3.1.0")]
    rig = Rig([api.sensor()])
    with caplog.at_level(logging.DEBUG):
        await _drive(rig)
    rig.assert_all_valid()
    blob = repr(rig.sink.observations) + caplog.text
    assert CANARY_ENV not in blob
    assert CANARY_ANN not in blob
    assert "token-a" not in blob
    assert not any(
        "events" in u or u.endswith("/log") or "/pods/" in u for u in api.urls
    )
    pods = _signal(rig.sink.of("sample"), "pods")
    assert len(pods) == 1 and pods[0]["outcome"] == "ok"
