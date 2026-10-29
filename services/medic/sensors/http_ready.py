"""Reference sensor: is an agent process ready to work, not just alive?

`/healthz` on the agent answers `ok` whenever the process exists (SW-CONTEXT §3);
`/readyz` checks the process's dependencies (`services/agent/core/health.ts`: 200
"ready" or 503 "not ready", bounded to 2 s, unauthenticated). So Medic reads
`/readyz` on the worker (6990) and serve (6989).

What the target said is a result, `outcome: ok` (C7 §4: a read that says Vigil is
down never backs off, and the engine needs a value, not unknown):

| Answer                       | ready | result      | status_code | latency_ms |
|------------------------------|-------|-------------|-------------|------------|
| 200                          | true  | ready       | 200         | yes        |
| 503 / other 5xx              | false | not_ready / http_5xx | code | yes        |
| connection refused           | false | refused     | absent      | absent     |
| connected, no answer in time | false | timeout     | absent      | absent     |
| connection dropped mid-reply | false | no_response | absent      | absent     |

Medic couldn't make the read: `outcome: error` (these back off after 3):

| 401 / 403      | `auth` + http_status. K1 T-09: while Redis is down Vigil answers 401 to every authenticated read; D3: the sensor records `auth`, rules decide it's a Redis fault |
| other 3xx/4xx  | `http_status` (wrong URL or path) |
| connect timeout| `timeout` (no route: a network policy drops, or the host is gone) |
| DNS / TLS      | `dns` / `tls` |
"""

from __future__ import annotations

import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value

_RESULTS = {200: "ready", 503: "not_ready"}


@dataclass(frozen=True)
class ReadyHit:
    """One `/readyz`: a result (`ready` is true only for `ready`) or a read error."""

    result: str = ""
    status: int | None = None
    latency_ms: float | None = None
    error: ReadError | None = None


def classify_status(status: int) -> str | ReadError:
    """The result table: 200 / 5xx are results; 401 / 403 and other statuses are errors."""
    if status in (401, 403):
        return ReadError("auth", http_status=status)
    if status == 200 or status >= 500:
        return _RESULTS.get(status, f"http_{status}")
    return ReadError("http_status", http_status=status)


async def read_ready(
    url: str,
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ReadyHit:
    """GET `url` and classify it. `timeout_s` is the sensor's cap; httpx stops at 80%."""
    limit = httpx.Timeout(timeout_s * 0.8)
    started = time.perf_counter()
    try:
        async with (
            httpx.AsyncClient(
                transport=transport, timeout=limit, trust_env=False
            ) as client,
            client.stream("GET", url) as response,  # the body is never read
        ):
            status = response.status_code
    except httpx.HTTPError as exc:
        failed = failure(exc)
        if isinstance(failed, ReadError):
            return ReadyHit(error=failed)
        return ReadyHit(result=failed)
    classified = classify_status(status)
    if isinstance(classified, ReadError):
        return ReadyHit(error=classified)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return ReadyHit(result=classified, status=status, latency_ms=latency_ms)


def ready_reading(
    signal: str,
    service: str,
    instance: str,
    hit: ReadyHit,
    labels: Mapping[str, str] | None = None,
) -> Reading:
    """The one reading `read_ready` becomes. Labels ride on every value."""
    if hit.error is not None:
        return Reading(signal, service, error=hit.error, instance=instance)
    marks = dict(labels or {})
    values = [
        Value.flag("ready", hit.result == "ready", marks),
        Value.enum("result", hit.result, marks),
        Value.gauge("status_code", hit.status, marks),
        Value.gauge("latency_ms", hit.latency_ms, marks),
    ]
    return Reading(signal, service, values=values, instance=instance)


def _cause(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def failure(exc: httpx.HTTPError) -> str | ReadError:
    """A failed GET: the result it says (the target is down) or the read error.

    Shared with the daemon sensors, so one failed connect means one thing."""
    if isinstance(exc, httpx.ConnectTimeout):
        return ReadError("timeout", detail="connect timed out")
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        if _cause(exc, ConnectionRefusedError):
            return "refused"
        if _cause(exc, socket.gaierror):
            return ReadError("dns")
        if _cause(exc, ssl.SSLError):
            return ReadError("tls")
        return ReadError("other", detail=type(exc).__name__)
    if isinstance(exc, httpx.RemoteProtocolError | httpx.ReadError):
        return "no_response"
    # Type only: the message can quote a response body.
    return ReadError("other", detail=type(exc).__name__)


@dataclass
class HttpReady:
    id: str
    service: str
    signal: str
    url: str
    interval_s: int = 30  # C7 §3: readiness 30 s / 5 s
    timeout_s: float = 5.0
    uses_vigil_api: bool = False  # /readyz is the agent's own port, not Vigil's API
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    @property
    def covers(self) -> tuple[str, ...]:
        return (self.signal,)

    @property
    def instance(self) -> str:
        # host:port only: netloc would carry any user:password@ in the URL.
        parts = urlsplit(self.url)
        return f"{parts.hostname}:{parts.port or 80}"

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        # httpx's own limit sits inside the framework's hard cap, so a slow target
        # is classified here (connect vs read) rather than cut by the scheduler.
        hit = await read_ready(
            self.url, timeout_s=self.timeout_s, transport=self.transport
        )
        return [ready_reading(self.signal, self.service, self.instance, hit)]


def agent_worker_ready(host: str = "agent-worker", port: int = 6990) -> HttpReady:
    return HttpReady(
        "http_ready.agent_worker",
        "agent-worker",
        "agent_worker_readyz",
        f"http://{host}:{port}/readyz",
    )


def agent_serve_ready(host: str = "agent-serve", port: int = 6989) -> HttpReady:
    return HttpReady(
        "http_ready.agent_serve",
        "agent-serve",
        "agent_serve_readyz",
        f"http://{host}:{port}/readyz",
    )
