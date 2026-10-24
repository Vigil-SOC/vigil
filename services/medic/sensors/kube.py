"""Helm pod state and deployed version, read from the Kubernetes API.

One `pods` reading per pod whose `app.kubernetes.io/component` maps into the
closed service list (`daemon` is `soc-daemon`), and one `version` reading per
such service from Deployments and StatefulSets. GET list only, following
`continue`. The service-account token is read on every request; a missing
mount or an unfinished list is an error for both signals and publishes no pod
readings. Pod and workload bodies are never logged or stored.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from services.medic.contracts.fingerprint_ref import SERVICES
from services.medic.sensors.base import ReadError, Reading, SensorContext, Value

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TOKEN_PATH = _SA / "token"
_CA_PATH = _SA / "ca.crt"
_NAMESPACE_PATH = _SA / "namespace"
_COMPONENT = "app.kubernetes.io/component"
_VERSION = "app.kubernetes.io/version"
# Kubernetes namespace: a DNS label, so it can go in the path.
_NS = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Unknown"})
# Same trim as containers._timestamp. Not imported: this reader shares no code
# with the Compose sensor.
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_MAX_PAGES = 32
_BODY_CAP = 8 << 20
_ERROR_CAP = 1024
_TOKEN_MAX = 16_384
_CONTINUE_MAX = 4096


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


def _timestamp(raw: object) -> tuple[datetime, str] | None:
    """RFC3339 trimmed to the observation timestamp pattern, with its instant."""
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
    return parsed, out + "Z"


def _tag(image: object) -> str | None:
    """Tag of the last path segment. A registry port and a digest are not it."""
    if not isinstance(image, str) or not image:
        return None
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" not in last:
        return None
    tag = last.split(":", 1)[1]
    return tag or None


def _service(labels: object) -> str | None:
    if not isinstance(labels, dict):
        return None
    component = labels.get(_COMPONENT)
    if not isinstance(component, str) or not component:
        return None
    name = "soc-daemon" if component == "daemon" else component
    return name if name in SERVICES else None


def _meta(obj: Mapping[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _labels(obj: Mapping[str, Any]) -> dict[str, Any]:
    labels = _meta(obj).get("labels")
    return labels if isinstance(labels, dict) else {}


def _name(obj: Mapping[str, Any]) -> str | None:
    name = _meta(obj).get("name")
    return name if isinstance(name, str) and name else None


def _phase(status: Mapping[str, Any]) -> str | None:
    phase = status.get("phase")
    if isinstance(phase, str) and phase in _PHASES:
        return phase
    return None


def _ready(status: Mapping[str, Any]) -> bool:
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
        for item in conditions
    )


def _restarts(status: Mapping[str, Any]) -> int:
    total = 0
    for key in ("initContainerStatuses", "containerStatuses"):
        statuses = status.get(key)
        if not isinstance(statuses, list):
            continue
        for item in statuses:
            if not isinstance(item, dict):
                continue
            count = item.get("restartCount")
            if type(count) is int and count > 0:
                total += count
    return total


def _starts(status: Mapping[str, Any]) -> list[tuple[datetime, str]]:
    found: list[tuple[datetime, str]] = []
    for key in ("initContainerStatuses", "containerStatuses"):
        statuses = status.get(key)
        if not isinstance(statuses, list):
            continue
        for item in statuses:
            if not isinstance(item, dict):
                continue
            for state_key in ("state", "lastState"):
                state = item.get(state_key)
                if not isinstance(state, dict):
                    continue
                for kind in ("running", "terminated"):
                    block = state.get(kind)
                    if isinstance(block, dict):
                        stamped = _timestamp(block.get("startedAt"))
                        if stamped is not None:
                            found.append(stamped)
    return found


def _epoch(status: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    starts = _starts(status)
    if not starts:
        created = _timestamp(meta.get("creationTimestamp"))
        if created is not None:
            starts = [created]
    if not starts:
        return "container:unknown"
    return "container:" + max(starts)[1]


def _workload_tag(obj: Mapping[str, Any]) -> str | None:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return None
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    pod_spec = template.get("spec")
    if not isinstance(pod_spec, dict):
        return None
    containers = pod_spec.get("containers")
    if not isinstance(containers, list):
        return None
    for container in containers:
        if isinstance(container, dict) and (tag := _tag(container.get("image"))):
            return tag
    return None


def _version_value(obj: Mapping[str, Any]) -> str | None:
    version = _labels(obj).get(_VERSION)
    if isinstance(version, str) and version:
        return version
    return _workload_tag(obj)


def _pod_readings(pods: list[dict[str, Any]]) -> list[Reading]:
    readings: list[Reading] = []
    for pod in sorted(pods, key=lambda item: _name(item) or ""):
        name = _name(pod)
        service = _service(_labels(pod))
        if name is None or service is None:
            continue
        status = pod.get("status")
        if not isinstance(status, dict):
            status = {}
        labels = {"service": service, "pod": name}
        readings.append(
            Reading(
                "pods",
                service,
                values=[
                    Value.enum("phase", _phase(status), labels),
                    Value.flag("ready", _ready(status), labels),
                    Value.counter("restart_count", _restarts(status), labels),
                ],
                instance=name,
                epoch=_epoch(status, _meta(pod)),
            )
        )
    return readings


def _version_readings(workloads: list[dict[str, Any]]) -> list[Reading]:
    # One series per service. Labels key the upgrade watch, so two workloads
    # for one service must not publish two values. The first name wins; a
    # later one fills in only when the first had no version. Never `diverged`.
    chosen: dict[str, str | None] = {}
    for obj in sorted(workloads, key=lambda item: _name(item) or ""):
        service = _service(_labels(obj))
        if service is None:
            continue
        value = _version_value(obj)
        if service not in chosen or (chosen[service] is None and value is not None):
            chosen[service] = value
    return [
        Reading(
            "version",
            service,
            values=[Value.enum("version", chosen[service], {"service": service})],
        )
        for service in sorted(chosen)
    ]


def _page(body: object) -> tuple[list[dict[str, Any]], str | None, ReadError | None]:
    if not isinstance(body, dict):
        return [], None, ReadError("parse")
    items = body.get("items")
    if not isinstance(items, list):
        return [], None, ReadError("parse")
    rows = [item for item in items if isinstance(item, dict)]
    meta = body.get("metadata")
    if not isinstance(meta, dict) or "continue" not in meta:
        return rows, None, None
    cont = meta.get("continue")
    if cont is None or cont == "":
        return rows, None, None
    if (
        not isinstance(cont, str)
        or len(cont) > _CONTINUE_MAX
        or any(char.isspace() for char in cont)
    ):
        return [], None, ReadError("parse")
    return rows, cont, None


async def _text(path: Path) -> tuple[str | None, ReadError | None]:
    try:
        raw = await asyncio.to_thread(path.read_text)
    except (OSError, UnicodeError) as exc:
        return None, ReadError("other", detail=type(exc).__name__)
    return raw, None


async def _read_namespace(path: Path) -> tuple[str | None, ReadError | None]:
    raw, error = await _text(path)
    if error is not None or raw is None:
        return None, error
    namespace = raw.strip()
    if not _NS.fullmatch(namespace):
        return None, ReadError("parse", detail="namespace")
    return namespace, None


async def _read_token(path: Path) -> tuple[str | None, ReadError | None]:
    raw, error = await _text(path)
    if error is not None or raw is None:
        return None, error
    token = raw.strip()
    if not token or len(token) > _TOKEN_MAX or any(char.isspace() for char in token):
        return None, ReadError("other", detail="token")
    return token, None


async def _stat(path: Path) -> ReadError | None:
    try:
        await asyncio.to_thread(path.stat)
    except OSError as exc:
        return ReadError("other", detail=type(exc).__name__)
    return None


async def _decode(response: httpx.Response) -> tuple[Any, ReadError | None]:
    status = response.status_code
    try:
        raw = await _read_capped(response, _BODY_CAP if status == 200 else _ERROR_CAP)
    except _TooLarge:
        if status == 200:
            return None, ReadError("too_large")
        if status in (401, 403):
            return None, ReadError("auth", http_status=status)
        return None, ReadError("http_status", http_status=status)
    # The capped bytes are dropped on an error: a body can echo a pod.
    if status in (401, 403):
        return None, ReadError("auth", http_status=status)
    if status != 200:
        return None, ReadError("http_status", http_status=status)
    try:
        return json.loads(raw), None
    except ValueError:
        return None, ReadError("parse")


@dataclass
class KubePods:
    id: str = "kube.pods"
    service: str = "medic"
    interval_s: int = 30  # C7 §3
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("pods", "version")
    uses_vigil_api: bool = False
    host: str = "kubernetes.default.svc"
    port: int = 443
    scheme: str = "https"
    token_path: Path = field(default=_TOKEN_PATH)
    ca_path: Path = field(default=_CA_PATH)
    namespace_path: Path = field(default=_NAMESPACE_PATH)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def _down(self, error: ReadError) -> list[Reading]:
        """The list did not finish: only errors, so the cycle counts as failed."""
        return [Reading(signal, "medic", error=error) for signal in self.covers]

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        deadline = time.monotonic() + self.timeout_s * 0.8
        namespace, error = await _read_namespace(self.namespace_path)
        if error is not None or namespace is None:
            return self._down(error or ReadError("parse", detail="namespace"))
        if self.scheme == "https":
            missing = await _stat(self.ca_path)
            if missing is not None:
                return self._down(missing)
        verify: bool | str = str(self.ca_path) if self.scheme == "https" else False
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(self.timeout_s * 0.8),
            trust_env=False,
            follow_redirects=False,
            verify=verify,
        ) as client:
            pods = await self._list(
                client, f"/api/v1/namespaces/{namespace}/pods", deadline
            )
            if isinstance(pods, ReadError):
                return self._down(pods)
            deploys = await self._list(
                client,
                f"/apis/apps/v1/namespaces/{namespace}/deployments",
                deadline,
            )
            if isinstance(deploys, ReadError):
                return self._down(deploys)
            sets = await self._list(
                client,
                f"/apis/apps/v1/namespaces/{namespace}/statefulsets",
                deadline,
            )
            if isinstance(sets, ReadError):
                return self._down(sets)
        return [*_pod_readings(pods), *_version_readings([*deploys, *sets])]

    async def _list(
        self, client: httpx.AsyncClient, path: str, deadline: float
    ) -> list[dict[str, Any]] | ReadError:
        items: list[dict[str, Any]] = []
        params: dict[str, str] | None = None
        for _ in range(_MAX_PAGES):
            token, error = await _read_token(self.token_path)
            if error is not None:
                return error
            body, error = await self._get(client, path, params, token, deadline)
            if error is not None:
                return error
            page, cont, error = _page(body)
            if error is not None:
                return error
            items.extend(page)
            if cont is None:
                return items
            params = {"continue": cont}
        return ReadError("other", detail="page cap")

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str] | None,
        token: str | None,
        deadline: float,
    ) -> tuple[Any, ReadError | None]:
        left = deadline - time.monotonic()
        if token is None:
            return None, ReadError("other", detail="token")
        if left <= 0:
            return None, ReadError("timeout", detail="timed out")
        url = f"{self.scheme}://{self.host}:{self.port}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with client.stream(
                "GET",
                url,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(left),
            ) as response:
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
            # Type only: the message can quote a response body or the token.
            return None, ReadError("other", detail=type(exc).__name__)


def kube_pods() -> KubePods:
    return KubePods()
