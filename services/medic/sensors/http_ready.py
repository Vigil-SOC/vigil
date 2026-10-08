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
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value

_RESULTS = {200: "ready", 503: "not_ready"}


def _cause(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


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
        instance = self.instance
        # httpx's own limit sits inside the framework's hard cap, so a slow target
        # is classified here (connect vs read) rather than cut by the scheduler.
        limit = httpx.Timeout(self.timeout_s * 0.8)
        started = time.perf_counter()
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport, timeout=limit, trust_env=False
                ) as client,
                client.stream("GET", self.url) as response,  # the body is never read
            ):
                status = response.status_code
        except httpx.ConnectTimeout:
            return [
                self._error(instance, ReadError("timeout", detail="connect timed out"))
            ]
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return [self._result(instance, "timeout")]
        except httpx.ConnectError as exc:
            if _cause(exc, ConnectionRefusedError):
                return [self._result(instance, "refused")]
            if _cause(exc, socket.gaierror):
                return [self._error(instance, ReadError("dns"))]
            if _cause(exc, ssl.SSLError):
                return [self._error(instance, ReadError("tls"))]
            return [
                self._error(instance, ReadError("other", detail=type(exc).__name__))
            ]
        except (httpx.RemoteProtocolError, httpx.ReadError):
            return [self._result(instance, "no_response")]
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if status in (401, 403):
            return [self._error(instance, ReadError("auth", http_status=status))]
        if status == 200 or status >= 500:
            result = _RESULTS.get(status, f"http_{status}")
            return [self._result(instance, result, status, latency_ms)]
        return [self._error(instance, ReadError("http_status", http_status=status))]

    def _result(
        self,
        instance: str,
        result: str,
        status: int | None = None,
        latency_ms: float | None = None,
    ) -> Reading:
        values = [
            Value.flag("ready", result == "ready"),
            Value.enum("result", result),
            Value.gauge("status_code", status),
            Value.gauge("latency_ms", latency_ms),
        ]
        return Reading(self.signal, self.service, values=values, instance=instance)

    def _error(self, instance: str, error: ReadError) -> Reading:
        return Reading(self.signal, self.service, error=error, instance=instance)


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
