"""The one gateway client (D6-7). Plain functions; sensors call them.

`GET /_gw/status` is the gateway's own route (`server.py` `status_read`),
answered before the allowlist. It is not on `policy.OUTBOUND`, and this module
does not import `services.medic_gateway` (the import fence forbids it).

What a reply means (`server.py` `_relay`):

| Gateway answered                         | Sensor sees                                      |
|------------------------------------------|--------------------------------------------------|
| 200, or a backend 5xx it relayed         | a result (`GatewayReply`)                        |
| 502 `auth_rejected` or `auth_<state>`    | error `auth`, http_status 502, code in detail    |
| its own `upstream_*` 502, 504, 503 busy  | an error                                         |
| 401 / 403, including one it should not   | error `auth` (never outcome ok)                  |

Failing to connect is an error, with the same classes `http_ready` uses for a
failed connect (`timeout`, `refused`, `dns`, `tls`).
"""

from __future__ import annotations

import ast
import json
import re
import socket
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from services.medic.app import config
from services.medic.sensors.base import ReadError, Reading, SensorContext, Value

# ViewerSession.state (session.py). A 200 whose state isn't one of these is a
# parse error: the enum is closed.
SESSION_STATES = (
    "init",
    "ok",
    "bad_password",
    "bad_password_stopped",
    "locked",
    "rate_limited",
    "rejected_after_login",
    "backend_unreachable",
    "login_error",
    "no_password",
)
_STATES = frozenset(SESSION_STATES)
_STATUS_PATH = "/_gw/status"
# The gateway drops a reply over 2 MiB (Upstream.max_body). Past that it isn't one.
_BODY_CAP = 2 << 20
_POLICY_PY = Path(__file__).resolve().parents[2] / "medic_gateway" / "policy.py"
_CODE = re.compile(r"[A-Za-z0-9_]{1,64}")
_PROBLEM = "application/problem+json"


def address(env: Mapping[str, str], shape: str) -> tuple[str, int] | None:
    """`VIGIL_MEDIC_GATEWAY_ADDR` as host:port. None when this shape has no gateway."""
    return config.gateway_addr(env, shape)


@dataclass(frozen=True)
class GatewayReply:
    """A read that worked. `status` >= 500 here is a backend 5xx the gateway relayed."""

    status: int
    body: Any


@dataclass(frozen=True)
class GatewayCall:
    """One GET a sensor declares. `query` is every parameter name it sends."""

    path: str
    query: tuple[str, ...] = ()


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


def _connect_error(exc: httpx.ConnectError) -> ReadError:
    if _cause(exc, ConnectionRefusedError):
        return ReadError("refused")
    if _cause(exc, socket.gaierror):
        return ReadError("dns")
    if _cause(exc, ssl.SSLError):
        return ReadError("tls")
    return ReadError("other", detail=type(exc).__name__)


def _problem_code(content_type: str, raw: bytes) -> str | None:
    """The gateway's own `code`, or None when this is a relayed body.

    `""` means it said problem+json and the code isn't a short token: still its
    own response, and the body is not kept.
    """
    if content_type.split(";", 1)[0].strip().lower() != _PROBLEM:
        return None
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    code = body.get("code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return ""


def _gateway_owned(code: str) -> bool:
    """Codes `_problem` in the gateway writes. A relayed body can be problem+json too."""
    return code.startswith(("auth_", "upstream_", "internal_")) or code == "busy"


def _gateway_error(status: int, code: str | None) -> ReadError:
    if code and code.startswith("auth_"):
        return ReadError("auth", http_status=status, detail=code)
    if status == 504 or code == "upstream_timeout":
        return ReadError("timeout", http_status=status, detail=code)
    if status == 503 and code == "busy":
        return ReadError("other", http_status=status, detail=code)
    if code and code.startswith("internal_"):
        return ReadError("other", http_status=status, detail=code)
    return ReadError("http_status", http_status=status, detail=code)


def classify(status: int, content_type: str, raw: bytes) -> GatewayReply | ReadError:
    """A relayed backend 5xx is a result. The gateway's own failures are errors.

    The gateway relays `application/problem+json` when the backend sent it
    (`JSON_TYPES`). Only its own codes (`auth_*`, `upstream_*`, `internal_*`,
    `busy`) and its own 4xx are errors; a relayed 200 or 5xx is a result.
    """
    code = _problem_code(content_type, raw)
    if code is not None and (
        _gateway_owned(code) or not (status == 200 or status >= 500)
    ):
        return _gateway_error(status, code or None)
    if status in (401, 403):
        return ReadError("auth", http_status=status)
    if status == 200 or status >= 500:
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return ReadError("parse")
        return GatewayReply(status, body)
    return ReadError("http_status", http_status=status)


async def get(
    host: str,
    port: int,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    timeout: float = 4.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GatewayReply | ReadError:
    """GET `path` on the gateway. The client does not follow redirects or use env proxies."""
    url = f"http://{host}:{port}{path}"
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream("GET", url, params=params) as response,
        ):
            status = response.status_code
            content_type = response.headers.get("content-type", "")
            try:
                raw = await _read_capped(response, _BODY_CAP)
            except _TooLarge:
                return ReadError("too_large")
    except httpx.ConnectTimeout:
        return ReadError("timeout", detail="connect timed out")
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
        return ReadError("timeout", detail="timed out")
    except httpx.ConnectError as exc:
        return _connect_error(exc)
    except httpx.HTTPError as exc:
        # Type only: the message can quote a response body.
        return ReadError("other", detail=type(exc).__name__)
    return classify(status, content_type, raw)


@dataclass(frozen=True)
class _Route:
    path: str
    query: frozenset[str]
    required: frozenset[str]


def _strings(node: ast.AST, what: str) -> frozenset[str]:
    if not isinstance(node, ast.Tuple | ast.List):
        raise TypeError(f"OUTBOUND {what}: expected a list of string literals")
    names: list[str] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            raise TypeError(f"OUTBOUND {what}: expected a string literal")
        names.append(elt.value)
    return frozenset(names)


def _dict_keys(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, ast.Dict):
        raise TypeError("OUTBOUND query: expected a dict literal")
    names: list[str] = []
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise TypeError("OUTBOUND query: expected a string literal")
        names.append(key.value)
    return frozenset(names)


def _route(node: ast.AST) -> _Route:
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        raise AssertionError("OUTBOUND entry is not a Route(...) call")
    method, path = node.args[0], node.args[1]
    if not (
        isinstance(method, ast.Constant)
        and method.value == "GET"
        and isinstance(path, ast.Constant)
        and isinstance(path.value, str)
    ):
        raise AssertionError("OUTBOUND route is not a GET with a string path")
    query: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    if len(node.args) >= 3:
        query = _dict_keys(node.args[2])
    for kw in node.keywords:
        if kw.arg == "query":
            query = _dict_keys(kw.value)
        elif kw.arg == "required":
            required = _strings(kw.value, "required")
    return _Route(path.value, query, required)


def _outbound(source: str) -> dict[str, _Route]:
    """Routes in policy.py's OUTBOUND list, read as text. No import."""
    tree = ast.parse(source)
    found: ast.List | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "OUTBOUND" for t in node.targets
        ):
            if not isinstance(node.value, ast.List):
                raise AssertionError("OUTBOUND is not a list literal")
            found = node.value
    if found is None:
        raise AssertionError("OUTBOUND is not in policy.py")
    routes: dict[str, _Route] = {}
    for elt in found.elts:
        route = _route(elt)
        previous = routes.get(route.path)
        if previous is None:
            routes[route.path] = route
        else:
            routes[route.path] = _Route(
                route.path,
                previous.query | route.query,
                previous.required | route.required,
            )
    return routes


def assert_path_subset(
    calls: Sequence[GatewayCall], *, source: str | None = None
) -> None:
    """Fail if a declared GET, or a query name it sends, is missing from OUTBOUND.

    Also fails when the call omits a query name that route requires: the gateway
    would reject it. `/_gw/status` is not on the list; declaring it fails.
    """
    routes = _outbound(source if source is not None else _POLICY_PY.read_text())
    problems: list[str] = []
    for call in calls:
        if call.path == _STATUS_PATH:
            problems.append(f"{call.path} is the gateway's own route, not OUTBOUND")
            continue
        route = routes.get(call.path)
        if route is None:
            problems.append(f"{call.path} is not on OUTBOUND")
            continue
        missing = [name for name in call.query if name not in route.query]
        if missing:
            problems.append(f"{call.path} query not on OUTBOUND: {', '.join(missing)}")
        omitted = sorted(route.required - set(call.query))
        if omitted:
            problems.append(
                f"{call.path} omits query OUTBOUND requires: {', '.join(omitted)}"
            )
    if problems:
        raise AssertionError(
            "gateway calls missing from OUTBOUND: " + "; ".join(problems)
        )


def _reading(error: ReadError | None, state: str | None, instance: str) -> Reading:
    if error is not None:
        return Reading(
            "gateway_session", "medic-gateway", error=error, instance=instance
        )
    return Reading(
        "gateway_session",
        "medic-gateway",
        values=[Value.enum("state", state)],
        instance=instance,
    )


@dataclass
class GatewaySession:
    host: str
    port: int
    id: str = "gateway_session"
    service: str = "medic-gateway"
    interval_s: int = 30  # C7 §3, and this route never reaches the backend
    timeout_s: float = 5.0
    covers: tuple[str, ...] = ("gateway_session",)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        instance = f"{self.host}:{self.port}"
        got = await get(
            self.host,
            self.port,
            _STATUS_PATH,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [_reading(got, None, instance)]
        if got.status != 200 or not isinstance(got.body, dict):
            error = (
                ReadError("parse")
                if got.status == 200
                else ReadError("http_status", http_status=got.status)
            )
            return [_reading(error, None, instance)]
        state = got.body.get("state")
        if not isinstance(state, str) or state not in _STATES:
            return [_reading(ReadError("parse"), None, instance)]
        return [_reading(None, state, instance)]


def gateway_session(host: str, port: int) -> GatewaySession:
    return GatewaySession(host, port)
