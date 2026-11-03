"""/api/medic/*: the console's way to Medic's API (H3).

One plain route per Medic operation (``services/medic/contracts/medic-api.openapi.yaml``),
each behind its own permission: ``medic.read`` for reads, ``medic.admin`` for the
feedback POST. Not a generic proxy: a path or query parameter the contract does not
declare never reaches Medic, and no client header is forwarded. Medic gets only
``X-Medic-Key``, plus ``X-Medic-Admin`` (the signed-in user) on the feedback POST.

Records, incidents and decisions are hash-covered, so Medic's body bytes pass through
untouched. Only the status snapshot is parsed and cached (15 s, per process).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse, Response

from core.auth.permissions import (
    MEDIC_ADMIN_PERMISSION,
    MEDIC_READ_PERMISSION,
    permission_gate,
    require_person_permission,
)
from core.config import get_settings
from core.platform.medic_last_seen import read_key
from core.platform.medic_status import MedicStatus, medic_enabled
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/medic",
    tags=["medic"],
    auth=Auth.REQUIRED,
)

_TIMEOUT_S = 5.0
_MAX_BODY = 4 * 1024 * 1024  # a page of 100 records of up to 16 KB, with room
_MAX_FEEDBACK = 4 * 1024  # the contract's request limit
_STATUS_TTL_S = 15.0
_MAX_PARAM_LEN = 300

_INCIDENT_ID = r"^inc_[0-9A-Za-z]{8,32}$"
_ADMIN_LABEL = re.compile(r"[A-Za-z0-9_.:/@+-]{1,128}")
# The closed set of Medic error codes (the contract's Error schema).
_CODES = frozenset(
    {
        "unauthorized",
        "not_found",
        "method_not_allowed",
        "invalid_parameter",
        "invalid_cursor",
        "invalid_body",
        "missing_admin_identity",
        "not_rateable",
        "payload_too_large",
        "unsupported_media_type",
        "busy",
        "store_not_recording",
        "internal",
    }
)

# The query parameters the contract declares, per operation.
_INCIDENT_QUERY = frozenset(
    {
        "state",
        "lane",
        "subject",
        "unrated",
        "include_suppressed",
        "opened_after",
        "opened_before",
        "cursor",
        "limit",
    }
)
_DECISION_QUERY = frozenset({"incident_id", "type", "from_seq", "cursor", "limit"})
_REPEATABLE = frozenset({"type"})

_read = permission_gate(MEDIC_READ_PERMISSION)


def _problem(status: int, code: str) -> Response:
    """A fixed error: a code and nothing of Medic's internals."""
    return JSONResponse(
        {"status": status, "code": code},
        status_code=status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
    )


def _medic_code(response: httpx.Response) -> Optional[str]:
    """Medic's problem+json ``code`` if it is one of the contract's."""
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return None
    return code if code in _CODES else None


def _target() -> tuple[Optional[str], Optional[str], Optional[Response]]:
    """(base URL, key, None), or an error when Medic is off or not reachable by config."""
    settings = get_settings()
    if not medic_enabled(settings):
        return None, None, _problem(503, "medic_off")
    key = read_key(settings)
    if not settings.vigil_medic_api_url or key is None:
        return None, None, _problem(503, "medic_unavailable")
    return settings.vigil_medic_api_url.rstrip("/"), key, None


async def _call(
    method: str,
    path: str,
    *,
    params: Optional[list[tuple[str, str]]] = None,
    extra_headers: Optional[dict[str, str]] = None,
    content: Optional[bytes] = None,
) -> tuple[Optional[httpx.Response], Optional[Response]]:
    """One request to Medic: (its response, None) or (None, the error to return).

    Never retried. A refusal, timeout, 401 or 5xx becomes a fixed 503; a 404 the
    fixed not-found; any other Medic error keeps its status and ``code``.
    """
    base, key, err = _target()
    if err is not None:
        return None, err
    headers = {"X-Medic-Key": key, **(extra_headers or {})}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S, follow_redirects=False, trust_env=False
        ) as client:
            r = await client.request(
                method, base + path, params=params, headers=headers, content=content
            )
    except httpx.HTTPError:
        return None, _problem(503, "medic_unavailable")
    if r.status_code == 404:
        return None, _problem(404, "not_found")
    if r.status_code >= 500 or r.status_code in (401, 403):
        code = _medic_code(r) if r.status_code >= 500 else None
        return None, _problem(503, code or "medic_unavailable")
    if r.status_code >= 400:
        return None, _problem(r.status_code, _medic_code(r) or "internal")
    if r.status_code not in (200, 201) or len(r.content) > _MAX_BODY:
        return None, _problem(503, "medic_unavailable")
    return r, None


def _passthrough(r: httpx.Response) -> Response:
    """Medic's body bytes and content type, unchanged."""
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers={
            "Content-Type": r.headers.get("content-type", "application/json"),
            "Cache-Control": "no-store",
        },
    )


def _query(request: Request, allowed: frozenset) -> tuple[list, Optional[Response]]:
    """The declared query parameters only; anything else is refused, not forwarded."""
    pairs = request.query_params.multi_items()
    seen: set[str] = set()
    for k, v in pairs:
        if k not in allowed or len(v) > _MAX_PARAM_LEN:
            return [], _problem(400, "invalid_parameter")
        if k in seen and k not in _REPEATABLE:
            return [], _problem(400, "invalid_parameter")
        seen.add(k)
    return pairs, None


async def _get(path: str, params: Optional[list] = None) -> Response:
    r, err = await _call("GET", path, params=params)
    return err or _passthrough(r)


# Status: current_status() answers alone unless Medic is Running; then Medic's own
# snapshot rides along, shared by every poll for up to 15 s.
_status_cache: Optional[tuple[float, dict]] = None
_status_lock = asyncio.Lock()


def _last_seen_at() -> Optional[str]:
    """When the backend last got an answer from Medic (UTC), or None."""
    from core.platform.medic_last_seen import read_last_seen

    try:
        seen, _ = read_last_seen()
    except Exception:  # noqa: BLE001 - Postgres down: nothing to report
        return None
    at = seen.last_seen_at if seen else None
    return at.strftime("%Y-%m-%dT%H:%M:%SZ") if at else None


@router.get("/status", dependencies=[_read])
async def medic_status_route() -> Response:
    """Medic's state word, when it was last seen, and its own snapshot when Running."""
    global _status_cache
    from core.platform.medic_last_seen import current_status

    state = await asyncio.to_thread(current_status)
    # Off has no history worth showing; the other states say when Medic last answered.
    last_seen = (
        None if state is MedicStatus.OFF else await asyncio.to_thread(_last_seen_at)
    )
    if state is not MedicStatus.RUNNING:
        body = {"state": state.value}
        if last_seen:
            body["last_seen_at"] = last_seen
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
    async with _status_lock:  # one fetch per process when polls overlap
        cached = _status_cache
        if cached is None or time.monotonic() - cached[0] >= _STATUS_TTL_S:
            r, err = await _call("GET", "/v1/status")
            if err is not None:
                return err
            try:
                snapshot = r.json()
            except ValueError:
                return _problem(503, "medic_unavailable")
            cached = _status_cache = (time.monotonic(), snapshot)
    body = {"state": state.value, "medic": cached[1]}
    if last_seen:
        body["last_seen_at"] = last_seen
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/incidents", dependencies=[_read])
async def list_incidents(request: Request) -> Response:
    params, err = _query(request, _INCIDENT_QUERY)
    return err or await _get("/v1/incidents", params)


@router.get("/incidents/{incident_id}", dependencies=[_read])
async def get_incident(incident_id: str = Path(pattern=_INCIDENT_ID)) -> Response:
    return await _get(f"/v1/incidents/{incident_id}")


@router.post("/incidents/{incident_id}/feedback")
async def post_feedback(
    request: Request,
    incident_id: str = Path(pattern=_INCIDENT_ID),
    user: User = Depends(require_person_permission(MEDIC_ADMIN_PERMISSION)),
) -> Response:
    """Record the signed-in admin's rating. The identity is the session's, never the body's."""
    admin = f"user:{user.user_id}"
    if not _ADMIN_LABEL.fullmatch(admin):
        return _problem(422, "missing_admin_identity")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != (
        "application/json"
    ):
        return _problem(415, "unsupported_media_type")
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        return _problem(400, "invalid_parameter")
    if declared > _MAX_FEEDBACK:
        return _problem(413, "payload_too_large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_FEEDBACK:
            return _problem(413, "payload_too_large")
    r, err = await _call(
        "POST",
        f"/v1/incidents/{incident_id}/feedback",
        extra_headers={"X-Medic-Admin": admin, "Content-Type": "application/json"},
        content=body,
    )
    return err or _passthrough(r)


@router.get("/decisions", dependencies=[_read])
async def list_decisions(request: Request) -> Response:
    params, err = _query(request, _DECISION_QUERY)
    return err or await _get("/v1/decisions", params)


@router.get("/decisions/{seq}", dependencies=[_read])
async def get_decision(seq: int = Path(ge=0, lt=2**53)) -> Response:
    return await _get(f"/v1/decisions/{seq}")


@router.get("/sensors", dependencies=[_read])
async def list_sensors() -> Response:
    return await _get("/v1/sensors")


@router.get("/pack", dependencies=[_read])
async def get_pack() -> Response:
    return await _get("/v1/pack")
