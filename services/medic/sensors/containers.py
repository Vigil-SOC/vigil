"""Compose container state, read through medic-dockerproxy.

One `containers` reading per container whose `com.docker.compose.service` label
is in the closed service list, plus one `version` reading (the shared image
tag, no labels, `target.service` backend) for the upgrade watch. List rows
have no Health, so each id is inspected. A proxy failure is an error: three
failed cycles go blind. A stopped container is a result, not an error.

The proxy JSON is never logged or stored: only the typed fields below.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from services.medic.contracts.fingerprint_ref import SERVICES
from services.medic.sensors.base import ReadError, Reading, SensorContext, Value

# Docker's container status set. Anything else is `unknown` (outcome stays ok).
_STATES = frozenset(
    {"created", "restarting", "running", "removing", "paused", "exited", "dead"}
)
_HEALTH = frozenset({"none", "starting", "healthy", "unhealthy"})
_SERVICE_LABEL = "com.docker.compose.service"
# Same charset the proxy allows on `{id}` (policy.ID_RE). Not imported: Medic
# shares no code with the proxy.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
# `server.refuse` writes this and nothing else. The code is a short token;
# the rest of the body is dropped.
_PROXY_MESSAGE = "medic-dockerproxy: "
_CODE = re.compile(r"[a-z_]{1,64}")
# The proxy caps a projected reply at 8 MiB. Past that it isn't a reply we can use.
_BODY_CAP = 8 << 20
_ERROR_CAP = 1024
# Not a real tag, so a mixed set differs from the last uniform one (upgrade watch).
_DIVERGED = "diverged"
_DIVERGED_ALT = "diverged.2"


def _cause(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class _TooLarge(Exception):
    pass


async def _read_capped(response: httpx.Response, cap: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > cap:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _proxy_code(raw: bytes) -> str | None:
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    message = body.get("message")
    if not isinstance(message, str) or not message.startswith(_PROXY_MESSAGE):
        return None
    code = message[len(_PROXY_MESSAGE) :]
    return code if _CODE.fullmatch(code) else None


def _timestamp(raw: object) -> str | None:
    """Docker's RFC3339 start time, trimmed to the observation timestamp pattern."""
    if not isinstance(raw, str) or not (match := _TS.fullmatch(raw.strip())):
        return None
    frac = ""
    if match.group(2):
        digits = match.group(2)[1:7].rstrip("0")
        if digits:
            frac = "." + digits
    text = f"{match.group(1)}{frac}{match.group(3)}"
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    out = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    if parsed.microsecond:
        out += "." + f"{parsed.microsecond:06d}".rstrip("0")
    return out + "Z"


def _tag(image: object) -> str | None:
    """The tag of `Config.Image`, not the registry path and not a digest."""
    if not isinstance(image, str) or not image:
        return None
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else "latest"
    return tag or None


def _enum(value: object, known: frozenset[str]) -> str:
    if isinstance(value, str) and value.lower() in known:
        return value.lower()
    return "unknown"


def _instance(names: object, inspect_name: object = None) -> str | None:
    if isinstance(inspect_name, str) and inspect_name.strip("/"):
        return inspect_name.lstrip("/")
    if isinstance(names, list):
        for name in names:
            if isinstance(name, str) and name.strip("/"):
                return name.lstrip("/")
    return None


def _service_of(row: dict[str, Any]) -> str | None:
    labels = row.get("Labels")
    if not isinstance(labels, dict):
        return None
    service = labels.get(_SERVICE_LABEL)
    if isinstance(service, str) and service in SERVICES:
        return service
    return None


@dataclass
class Containers:
    host: str
    port: int
    id: str = "docker.containers"
    service: str = "backend"
    interval_s: int = 30  # C7 §3
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("containers", "version")
    uses_vigil_api: bool = False
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    # Last tag every closed-list container shared. A mixed set must not repeat it.
    _uniform: str | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        limit = httpx.Timeout(self.timeout_s * 0.8)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=limit,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            listed, error = await self._get(
                client, "/containers/json", params={"all": "true"}
            )
            if error is not None:
                return self._down(error)
            if not isinstance(listed, list):
                return self._down(ReadError("parse"))
            rows = [row for row in listed if isinstance(row, dict) and _service_of(row)]
            rows.sort(key=lambda row: _instance(row.get("Names")) or "")
            readings: list[Reading] = []
            tags: list[str | None] = []
            failed = False
            for row in rows:
                reading, tag, inspect_failed = await self._container(client, row)
                readings.append(reading)
                tags.append(tag)
                failed = failed or inspect_failed
            # A partial inspect must not publish a tag from the containers that
            # answered: that would look like a version change.
            if rows and not failed:
                readings.append(self._version(tags))
            return readings

    def _down(self, error: ReadError) -> list[Reading]:
        """List failed: only errors, so the cycle counts as failed."""
        return [Reading(signal, "backend", error=error) for signal in self.covers]

    async def _container(
        self, client: httpx.AsyncClient, row: dict[str, Any]
    ) -> tuple[Reading, str | None, bool]:
        service = _service_of(row) or "backend"
        instance = _instance(row.get("Names"))
        cid = row.get("Id")
        if not isinstance(cid, str) or not _ID.fullmatch(cid):
            return (
                Reading(
                    "containers", service, error=ReadError("parse"), instance=instance
                ),
                None,
                True,
            )
        body, error = await self._get(client, f"/containers/{cid}/json")
        if error is not None:
            return (
                Reading("containers", service, error=error, instance=instance),
                None,
                True,
            )
        if not isinstance(body, dict):
            return (
                Reading(
                    "containers", service, error=ReadError("parse"), instance=instance
                ),
                None,
                True,
            )
        instance = _instance(row.get("Names"), body.get("Name")) or instance
        return self._reading(service, instance, body, row), _image_tag(body), False

    def _reading(
        self,
        service: str,
        instance: str | None,
        body: dict[str, Any],
        row: dict[str, Any],
    ) -> Reading:
        labels = {"service": service}
        state = body.get("State") if isinstance(body.get("State"), dict) else {}
        status = state.get("Status") if isinstance(state, dict) else None
        if not isinstance(status, str):
            status = row.get("State")
        health_block = state.get("Health") if isinstance(state, dict) else None
        if health_block is None:
            health: str | None = None
        elif isinstance(health_block, dict):
            health = _enum(health_block.get("Status"), _HEALTH)
        else:
            health = "unknown"
        restarts = body.get("RestartCount")
        count = restarts if type(restarts) is int and restarts >= 0 else None
        started = _timestamp(
            state.get("StartedAt") if isinstance(state, dict) else None
        )
        return Reading(
            "containers",
            service,
            values=[
                Value.enum("state", _enum(status, _STATES), labels),
                Value.enum("health", health, labels),
                Value.counter("restart_count", count, labels),
                Value.timestamp("started_at", started, labels),
            ],
            instance=instance,
            epoch=f"container:{started}" if started else "container:unknown",
        )

    def _version(self, tags: list[str | None]) -> Reading:
        return Reading(
            "version",
            "backend",
            values=[Value.enum("version", self._version_value(tags))],
        )

    def _version_value(self, tags: list[str | None]) -> str:
        tag = tags[0] if tags else None
        if tag is not None and all(item == tag for item in tags):
            self._uniform = tag
            return tag
        if self._uniform != _DIVERGED:
            return _DIVERGED
        return _DIVERGED_ALT

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[Any, ReadError | None]:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            async with client.stream("GET", url, params=params) as response:
                return await _decode(response)
        except httpx.ConnectTimeout:
            return None, ReadError("timeout", detail="connect timed out")
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            return None, ReadError("timeout", detail="timed out")
        except httpx.ConnectError as exc:
            if _cause(exc, ConnectionRefusedError):
                return None, ReadError("refused")
            if _cause(exc, socket.gaierror):
                return None, ReadError("dns")
            if _cause(exc, ssl.SSLError):
                return None, ReadError("tls")
            return None, ReadError("other", detail=type(exc).__name__)
        except httpx.HTTPError as exc:
            # Type only: the message can quote a response body.
            return None, ReadError("other", detail=type(exc).__name__)


async def _decode(response: httpx.Response) -> tuple[Any, ReadError | None]:
    status = response.status_code
    try:
        raw = await _read_capped(response, _BODY_CAP if status == 200 else _ERROR_CAP)
    except _TooLarge:
        if status == 200:
            return None, ReadError("too_large")
        return None, ReadError("http_status", http_status=status)
    if status != 200:
        return None, ReadError(
            "http_status", http_status=status, detail=_proxy_code(raw)
        )
    try:
        return json.loads(raw), None
    except ValueError:
        return None, ReadError("parse")


def _image_tag(body: dict[str, Any]) -> str | None:
    config = body.get("Config")
    if not isinstance(config, dict):
        return None
    return _tag(config.get("Image"))


def docker_containers(host: str = "medic-dockerproxy", port: int = 8472) -> Containers:
    return Containers(host, port)
