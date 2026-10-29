"""The SOC daemon, Kafka and the orchestrator (D6-6).

Two reads go straight to the daemon's health port on Medic's network
(`services/daemon/metrics.py`), every 30 s (epic decision 7):

- `daemon_health` from `/health`: `ok`, `components.<name>` (closed enum,
  `failed: <Exc>` folded to `failed`), `uptime_seconds`. A 503 is the daemon
  saying a component died: a result, `ok: false`, with the same values.
- `daemon_status.*` from `/status`: the counters a rule reads, one reading per
  component. Their epoch is the daemon's start time, so a restart isn't read
  as counters going backwards. The Kafka consumer is re-created when it is
  toggled, which resets its counters inside one epoch (E3 adds max(0, Δ)).

Failing to connect follows `http_ready`: refused, a read timeout or a dropped
reply is the daemon being down (`daemon_health` `ok: false`, `result`). On Compose
the name doesn't resolve without the `daemon` profile: `not_installed`, which
never reads blind. DNS elsewhere is an error (Helm registers no daemon sensor
when the daemon is off).

Two reads go through the gateway: `kafka_status` (`enabled`, `daemon_reachable`;
`config` holds the broker and SASL settings and is never read) and
`orchestrator_status` (`enabled`, `queued`, `cost.hourly_paused`) every 300 s,
because that route loads every investigation twice. The orchestrator's
counters come from the daemon's `/status` only: the route's `stats` are the API
process's own orchestrator.

`federation`, `llm_queue_depth` and `agent_runs_depth` aren't emitted: no rule
reads them. Where the daemon reports `false`/`0` for a component it doesn't
have (`auto_triage_enabled`, `free_slots`), the value is absent instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value
from services.medic.sensors.gateway import GatewayCall, get
from services.medic.sensors.http_ready import failure

SERVICE = "soc-daemon"
COMPONENTS = ("poller", "kafka", "processor", "executor", "scheduler", "orchestrator")
STATES = frozenset({"running", "disabled", "stopped", "not_initialized", "failed"})
STATUS_SIGNALS = tuple(f"daemon_status.{part}" for part in (*COMPONENTS, "vendors"))
KAFKA_CALL = GatewayCall("/api/kafka/status")
ORCHESTRATOR_CALL = GatewayCall("/api/orchestrator/status")
CALLS = (KAFKA_CALL, ORCHESTRATOR_CALL)
_BODY_CAP = 1 << 20  # /status is a few KiB
_EXECUTOR = ("executed", "failed", "errors")


def _int(raw: object) -> int | None:
    return raw if type(raw) is int and raw >= 0 else None


def _num(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw < 0:
        return None
    return raw


def _flag(raw: object) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _part(body: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    part = body.get(name)
    return part if isinstance(part, dict) else {}


def _state(raw: object) -> str | None:
    """The closed enum. `failed: <Exc>` → `failed`: the class name isn't label-safe."""
    if not isinstance(raw, str):
        return None
    state = "failed" if raw.startswith("failed") else raw
    return state if state in STATES else None


def _epoch(raw: object) -> str | None:
    """`uptime:<start time, UTC>`: the schema's epoch pattern has no `+`."""
    if not isinstance(raw, str):
        return None
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return None
    started = (started if started.tzinfo else started.replace(tzinfo=UTC)).astimezone(
        UTC
    )
    return "uptime:" + started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _fetch(
    url: str, timeout: float, transport: httpx.AsyncBaseTransport | None
) -> tuple[int, Any] | str | ReadError:
    """(status, JSON body or None), a down result name, or a read error."""
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream("GET", url) as response,
        ):
            raw = b""
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) > _BODY_CAP:
                    return ReadError("too_large")
            status = response.status_code
    except httpx.HTTPError as exc:
        return failure(exc)
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _installed(error: ReadError, ctx: SensorContext) -> ReadError:
    """On Compose an unresolvable daemon is the `daemon` profile left off: not
    installed, which never reads blind. Elsewhere DNS is a real error."""
    if error.cls == "dns" and ctx.shape == "compose":
        return ReadError("not_installed")
    return error


def _poller(part: Mapping[str, Any]) -> list[Value]:
    values = [
        Value.counter("polls", _int(n), {"source": key.removesuffix("_polls")})
        for key, n in sorted(part.items())
        if key.endswith("_polls") and len(key) > len("_polls")
    ]
    values.append(Value.counter("webhook_findings", _int(part.get("webhook_findings"))))
    rejections = part.get("webhook_rejections")
    if not isinstance(rejections, dict):
        rejections = {}
    for endpoint, reasons in sorted(rejections.items()):
        if not isinstance(reasons, dict):
            continue
        for reason, n in sorted(reasons.items()):
            labels = {"endpoint": str(endpoint), "reason": str(reason)}
            values.append(Value.counter("webhook_rejections", _int(n), labels))
    return values


def _kafka(part: Mapping[str, Any]) -> list[Value]:
    return [
        Value.flag("connected", _flag(part.get("connected"))),
        Value.counter("decode_errors", _int(part.get("decode_errors"))),
        Value.counter("missing_id_errors", _int(part.get("missing_id_errors"))),
    ]


def _processor(part: Mapping[str, Any]) -> list[Value]:
    # Without a processor the daemon still says auto_triage_enabled: false.
    has_own = "processed" in part
    return [
        *(
            Value.counter(k, _int(part.get(k)))
            for k in ("processed", "triaged", "errors")
        ),
        Value.flag(
            "auto_triage_enabled",
            _flag(part.get("auto_triage_enabled")) if has_own else None,
        ),
    ]


def _orchestrator(part: Mapping[str, Any]) -> list[Value]:
    # Without an orchestrator the daemon still says free_slots: 0.
    has_own = "enabled" in part
    return [
        Value.counter(
            "investigations_created", _int(part.get("investigations_created"))
        ),
        Value.counter(
            "investigations_completed", _int(part.get("investigations_completed"))
        ),
        Value.gauge("free_slots", _int(part.get("free_slots")) if has_own else None),
    ]


def _vendors(part: Mapping[str, Any]) -> list[Value]:
    values = []
    for vendor, row in sorted(part.items()):
        by_status = row.get("by_status") if isinstance(row, dict) else None
        if not isinstance(by_status, dict):
            continue
        for status, n in sorted(by_status.items()):
            labels = {"vendor": str(vendor), "status": str(status)}
            values.append(Value.counter("errors", _int(n), labels))
    return values


def _status(body: Mapping[str, Any]) -> dict[str, list[Value]]:
    return {
        "daemon_status.poller": _poller(_part(body, "poller")),
        "daemon_status.kafka": _kafka(_part(body, "kafka")),
        "daemon_status.processor": _processor(_part(body, "processor")),
        "daemon_status.executor": [
            Value.counter(k, _int(_part(body, "executor").get(k))) for k in _EXECUTOR
        ],
        "daemon_status.scheduler": [
            Value.counter("tasks_run", _int(_part(body, "scheduler").get("tasks_run")))
        ],
        "daemon_status.orchestrator": _orchestrator(_part(body, "orchestrator")),
        "daemon_status.vendors": _vendors(_part(body, "vendors")),
    }


@dataclass
class DaemonHealth:
    host: str
    port: int
    id: str = "daemon.health"
    service: str = SERVICE
    interval_s: int = 30
    timeout_s: float = 5.0
    covers: tuple[str, ...] = ("daemon_health",)
    uses_vigil_api: bool = False  # the daemon's own port, not Vigil's API
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        instance = f"{self.host}:{self.port}"
        got = await _fetch(
            f"http://{instance}/health", self.timeout_s * 0.8, self.transport
        )
        if isinstance(got, ReadError):
            got = _installed(got, ctx)
            return [Reading("daemon_health", SERVICE, error=got, instance=instance)]
        if isinstance(got, str):
            # Refused, timed out or dropped: the daemon is down, not unread.
            values = [Value.flag("ok", False), Value.enum("result", got)]
            return [Reading("daemon_health", SERVICE, values=values, instance=instance)]
        status, body = got
        if status in (401, 403):
            error = ReadError("auth", http_status=status)
        elif status not in (200, 503):
            error = ReadError("http_status", http_status=status)
        elif not isinstance(body, dict) or not isinstance(body.get("components"), dict):
            error = ReadError("parse")
        else:
            error = None
        if error is not None:
            return [Reading("daemon_health", SERVICE, error=error, instance=instance)]
        components = body["components"]
        values = [
            Value.flag("ok", status == 200),
            Value.enum("result", "healthy" if status == 200 else "unhealthy"),
            *(
                Value.enum(f"components.{c}", _state(components.get(c)))
                for c in COMPONENTS
            ),
            Value.gauge("uptime_seconds", _num(body.get("uptime_seconds"))),
        ]
        return [Reading("daemon_health", SERVICE, values=values, instance=instance)]


@dataclass
class DaemonStatus:
    host: str
    port: int
    id: str = "daemon.status"
    service: str = SERVICE
    interval_s: int = 30
    timeout_s: float = 5.0
    covers: tuple[str, ...] = STATUS_SIGNALS
    uses_vigil_api: bool = False
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def _errors(self, instance: str, error: ReadError) -> list[Reading]:
        return [
            Reading(signal, SERVICE, error=error, instance=instance)
            for signal in STATUS_SIGNALS
        ]

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        instance = f"{self.host}:{self.port}"
        got = await _fetch(
            f"http://{instance}/status", self.timeout_s * 0.8, self.transport
        )
        if isinstance(got, str):
            # daemon_health says the daemon is down; here there's nothing read.
            cls = {"refused": "refused", "timeout": "timeout"}.get(got, "other")
            return self._errors(instance, ReadError(cls, detail=got))
        if isinstance(got, ReadError):
            return self._errors(instance, _installed(got, ctx))
        status, body = got
        if status != 200:
            cls = "auth" if status in (401, 403) else "http_status"
            return self._errors(instance, ReadError(cls, http_status=status))
        epoch = (
            _epoch(_part(body, "daemon").get("start_time"))
            if isinstance(body, dict)
            else None
        )
        if epoch is None:
            return self._errors(instance, ReadError("parse"))
        return [
            Reading(signal, SERVICE, values=values, instance=instance, epoch=epoch)
            for signal, values in _status(body).items()
        ]


@dataclass
class KafkaStatus:
    host: str
    port: int
    id: str = "gateway.kafka_status"
    service: str = "backend"
    interval_s: int = 60
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("kafka_status",)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        got = await get(
            self.host,
            self.port,
            KAFKA_CALL.path,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [Reading("kafka_status", "backend", error=got)]
        body: Any = {}
        if got.status == 200:
            # A relayed 5xx leaves both absent: the read failed, not "off".
            if not isinstance(got.body, dict):
                return [Reading("kafka_status", "backend", error=ReadError("parse"))]
            body = got.body
        values = [
            Value.flag("enabled", _flag(body.get("enabled"))),
            Value.flag("daemon_reachable", _flag(body.get("daemon_reachable"))),
        ]
        return [Reading("kafka_status", "backend", values=values)]


@dataclass
class OrchestratorStatus:
    host: str
    port: int
    id: str = "gateway.orchestrator_status"
    service: str = "backend"
    interval_s: int = 300  # the route loads every investigation twice
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("orchestrator_status",)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        got = await get(
            self.host,
            self.port,
            ORCHESTRATOR_CALL.path,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [Reading("orchestrator_status", "backend", error=got)]
        body: Any = {}
        if got.status == 200:
            if not isinstance(got.body, dict):
                return [
                    Reading("orchestrator_status", "backend", error=ReadError("parse"))
                ]
            body = got.body
        cost = body.get("cost") if isinstance(body.get("cost"), dict) else {}
        values = [
            Value.flag("enabled", _flag(body.get("enabled"))),
            Value.gauge("queued", _int(body.get("queued"))),
            Value.flag("cost.hourly_paused", _flag(cost.get("hourly_paused"))),
        ]
        return [Reading("orchestrator_status", "backend", values=values)]


def daemon_sensors(host: str, port: int) -> list[DaemonHealth | DaemonStatus]:
    return [DaemonHealth(host, port), DaemonStatus(host, port)]


def backend_status_sensors(
    host: str, port: int
) -> list[KafkaStatus | OrchestratorStatus]:
    return [KafkaStatus(host, port), OrchestratorStatus(host, port)]
