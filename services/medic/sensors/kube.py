"""Helm pod state and deployed version, read from the in-cluster API.

GET list only: core/v1 pods, apps/v1 deployments and statefulsets, in Medic's
namespace, following `metadata.continue`. No watch, events, logs, or probes.
A pod that leaves the list emits no series; the engine already treats that as
stale. 401/403, and any failure to reach the API, are errors on both signals
with service `medic` (blind after three cycles). CrashLoopBackOff and Failed
are results: the waiting reason is never stored.

The service-account token is read on every request, and not at registration,
so a missing mount fails `collect()` and a rotated token is picked up without
a restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from services.medic.sensors.base import (
    CONTRACTS,
    ReadError,
    Reading,
    SensorContext,
    Value,
)

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_MAX_BODY = 8 * 1024 * 1024
# Chart component `daemon` is the observation service `soc-daemon`.
_ALIAS = {"daemon": "soc-daemon"}
_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Unknown"})
_NS = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_HOST = re.compile(r"[A-Za-z0-9.:-]{1,253}")
_ZULU = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$"
)
_PODS = "/api/v1/namespaces/{ns}/pods"
_DEPLOYS = "/apis/apps/v1/namespaces/{ns}/deployments"
_SETS = "/apis/apps/v1/namespaces/{ns}/statefulsets"


def _services() -> frozenset[str]:
    doc = json.loads((CONTRACTS / "observation.schema.json").read_text())
    return frozenset(doc["$defs"]["service"]["enum"])


_SERVICES = _services()


class _Fail(Exception):
    def __init__(self, error: ReadError) -> None:
        self.error = error


def _both(error: ReadError) -> list[Reading]:
    return [
        Reading("pods", "medic", error=error),
        Reading("version", "medic", error=error),
    ]


def _cause(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _connect_error(exc: httpx.ConnectError) -> ReadError:
    # Reaching the API at all failed: an error, never a pod result.
    if _cause(exc, ConnectionRefusedError):
        return ReadError("refused")
    if _cause(exc, socket.gaierror):
        return ReadError("dns")
    if _cause(exc, ssl.SSLError):
        return ReadError("tls")
    return ReadError("other", detail=type(exc).__name__)


def service_of(obj: dict) -> str | None:
    """Map `app.kubernetes.io/component` onto the observation service enum."""
    meta = obj.get("metadata")
    labels = meta.get("labels") if isinstance(meta, dict) else None
    component = (
        labels.get("app.kubernetes.io/component") if isinstance(labels, dict) else None
    )
    if not isinstance(component, str):
        return None
    name = _ALIAS.get(component, component)
    return name if name in _SERVICES else None


def _meta_name(obj: dict) -> str:
    meta = obj.get("metadata")
    if not isinstance(meta, dict):
        return ""
    name = meta.get("name")
    return name if isinstance(name, str) else ""


def _stamp(raw: object) -> tuple[datetime, str] | None:
    """Parse an API timestamp. Offsets become Zulu so the epoch pattern holds."""
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    if _ZULU.fullmatch(text) and len("container:" + text) <= 64:
        return parsed, text
    frac = f".{parsed.microsecond:06d}" if parsed.microsecond else ""
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"
    if len("container:" + canonical) > 64:
        return None
    return parsed, canonical


def _walk_statuses(status: dict):
    for key in ("containerStatuses", "initContainerStatuses"):
        statuses = status.get(key) or []
        if isinstance(statuses, list):
            yield from (item for item in statuses if isinstance(item, dict))


def _restart_count(status: dict) -> int:
    total = 0
    for container in _walk_statuses(status):
        count = container.get("restartCount", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        total += count
    return total


def _ready(status: dict) -> bool:
    conditions = status.get("conditions") or []
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _phase(status: dict) -> str | None:
    phase = status.get("phase")
    return phase if phase in _PHASES else None


def _epoch(status: dict, created: object) -> str | None:
    """Latest container `startedAt`, else the pod's creation. `container:<rfc3339>`."""
    best: tuple[datetime, str] | None = None
    for container in _walk_statuses(status):
        for side in ("state", "lastState"):
            blob = container.get(side)
            if not isinstance(blob, dict):
                continue
            for kind in ("running", "terminated"):
                run = blob.get(kind)
                if not isinstance(run, dict):
                    continue
                stamp = _stamp(run.get("startedAt"))
                if stamp is not None and (best is None or stamp[0] > best[0]):
                    best = stamp
    if best is None:
        best = _stamp(created)
    if best is None:
        return None
    return "container:" + best[1]


def pod_reading(pod: dict) -> Reading | None:
    """One series per pod. Unknown components and pods with no epoch are skipped."""
    service = service_of(pod)
    name = _meta_name(pod)
    if service is None or not name:
        return None
    status = pod.get("status")
    if not isinstance(status, dict):
        status = {}
    meta = pod.get("metadata")
    created = meta.get("creationTimestamp") if isinstance(meta, dict) else None
    epoch = _epoch(status, created)
    if epoch is None:
        return None
    labels = {"service": service, "pod": name}
    return Reading(
        "pods",
        service,
        values=[
            Value.enum("phase", _phase(status), labels),
            Value.flag("ready", _ready(status), labels),
            Value.counter("restart_count", _restart_count(status), labels),
        ],
        instance=name,
        epoch=epoch,
    )


def _image_tag(image: object) -> str | None:
    # The tag is the last path segment's `:tag`, so a registry port is not one.
    if not isinstance(image, str) or not image:
        return None
    leaf = image.split("@", 1)[0].rsplit("/", 1)[-1]
    tag = leaf.split(":", 1)[1] if ":" in leaf else ""
    return tag or None


def _workload_image(workload: dict) -> str | None:
    spec = workload.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if (
        not isinstance(containers, list)
        or not containers
        or not isinstance(containers[0], dict)
    ):
        return None
    image = containers[0].get("image")
    return image if isinstance(image, str) else None


def _version_value(workload: dict) -> str | None:
    meta = workload.get("metadata")
    labels = meta.get("labels") if isinstance(meta, dict) else None
    version = (
        labels.get("app.kubernetes.io/version") if isinstance(labels, dict) else None
    )
    if isinstance(version, str) and version:
        return version
    return _image_tag(_workload_image(workload))


def version_readings(
    deployments: list[dict], statefulsets: list[dict]
) -> list[Reading]:
    """One series per service. Deployments win over a statefulset of the same service.

    The upgrade window keys a version series on the value's labels, not on
    `target.service`, so the service label is what keeps two workloads apart.
    The image tag is used only when `app.kubernetes.io/version` is absent.
    """
    chosen: dict[str, Reading] = {}
    ordered = sorted(deployments, key=_meta_name) + sorted(statefulsets, key=_meta_name)
    for workload in ordered:
        service = service_of(workload)
        if service is None or service in chosen:
            continue
        chosen[service] = Reading(
            "version",
            service,
            values=[
                Value.enum("version", _version_value(workload), {"service": service})
            ],
            instance=service,
        )
    return list(chosen.values())


async def _read_text(path: Path, detail: str) -> str:
    try:
        return await asyncio.to_thread(path.read_text)
    except (OSError, UnicodeError):
        raise _Fail(ReadError("other", detail=detail)) from None


async def _token(path: Path) -> str:
    text = (await _read_text(path, "service account token unreadable")).strip()
    if not text or len(text) > 16_384 or any(char.isspace() for char in text):
        raise _Fail(ReadError("other", detail="service account token unreadable"))
    return text


async def _namespace(path: Path) -> str:
    text = (await _read_text(path, "namespace unreadable")).strip()
    if not _NS.fullmatch(text) or len(text) > 63:
        raise _Fail(ReadError("other", detail="namespace unreadable"))
    return text


async def _discard(response: httpx.Response) -> None:
    read = 0
    async for chunk in response.aiter_bytes():
        read += len(chunk)
        if read > 65_536:
            return


async def _body(response: httpx.Response) -> dict:
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        if len(buf) + len(chunk) > _MAX_BODY:
            raise _Fail(ReadError("too_large", detail="list response over the cap"))
        buf.extend(chunk)
    try:
        parsed = json.loads(buf)
    except json.JSONDecodeError:
        raise _Fail(ReadError("parse", detail="list response was not json")) from None
    if not isinstance(parsed, dict):
        raise _Fail(ReadError("parse", detail="list response was not json"))
    return parsed


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    params: dict[str, str] | None,
    timeout_s: float,
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with client.stream(
        "GET",
        url,
        headers=headers,
        params=params,
        timeout=httpx.Timeout(timeout_s),
    ) as response:
        status = response.status_code
        if status != 200:
            await _discard(response)
            if status in (401, 403):
                raise _Fail(ReadError("auth", http_status=status))
            raise _Fail(ReadError("http_status", http_status=status))
        return await _body(response)


async def _list(
    client: httpx.AsyncClient, url: str, token_path: Path, deadline: float
) -> list[dict]:
    items: list[dict] = []
    params: dict[str, str] | None = None
    while True:
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise _Fail(ReadError("timeout", detail="list timed out"))
        page = await _get_json(client, url, await _token(token_path), params, left)
        batch = page.get("items") or []
        if not isinstance(batch, list):
            raise _Fail(ReadError("parse", detail="list response was not json"))
        items.extend(item for item in batch if isinstance(item, dict))
        meta = page.get("metadata") or {}
        if not isinstance(meta, dict):
            raise _Fail(ReadError("parse", detail="list response was not json"))
        cont = meta.get("continue")
        if not cont:
            return items
        if not isinstance(cont, str):
            raise _Fail(ReadError("parse", detail="list response was not json"))
        params = {"continue": cont}


def _base_url(api: str | None) -> str:
    if api is not None:
        return api.rstrip("/")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    if not _HOST.fullmatch(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise _Fail(ReadError("other", detail="api server address unusable"))
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"
    return f"https://{host}:{port}"


@dataclass
class Kube:
    id: str = "kube"
    service: str = "medic"
    interval_s: int = 30
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("pods", "version")
    uses_vigil_api: bool = False
    token_path: Path = _SA / "token"
    namespace_path: Path = _SA / "namespace"
    ca_path: Path = _SA / "ca.crt"
    api: str | None = None
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, _ctx: SensorContext) -> Sequence[Reading]:
        # Helm-only by registration. The mount is read here, not at construction.
        try:
            return await self._collect()
        except _Fail as fail:
            return _both(fail.error)
        except httpx.TimeoutException:
            return _both(ReadError("timeout", detail="api timed out"))
        except httpx.ConnectError as exc:
            return _both(_connect_error(exc))
        except httpx.HTTPError as exc:
            return _both(ReadError("other", detail=type(exc).__name__))
        except OSError:
            return _both(ReadError("tls", detail="api server ca unreadable"))

    async def _collect(self) -> list[Reading]:
        namespace = await _namespace(self.namespace_path)
        base = _base_url(self.api)
        deadline = time.monotonic() + self.timeout_s * 0.8
        verify: bool | str = False if self.transport is not None else str(self.ca_path)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(self.timeout_s),
            trust_env=False,
            follow_redirects=False,
            verify=verify,
        ) as client:
            pods = await _list(
                client, f"{base}{_PODS.format(ns=namespace)}", self.token_path, deadline
            )
            deployments = await _list(
                client,
                f"{base}{_DEPLOYS.format(ns=namespace)}",
                self.token_path,
                deadline,
            )
            statefulsets = await _list(
                client, f"{base}{_SETS.format(ns=namespace)}", self.token_path, deadline
            )
        readings: list[Reading] = []
        for pod in pods:
            reading = pod_reading(pod)
            if reading is not None:
                readings.append(reading)
        readings.extend(version_readings(deployments, statefulsets))
        return readings


def kube_sensor() -> Kube:
    """Helm registration. Does not touch the service-account mount."""
    return Kube()
