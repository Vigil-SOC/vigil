"""Authenticated passthrough to Bifrost's config API.

Bifrost's own config store is the source of truth for providers, keys, model
allow-lists, pricing and governance. This router is the console's only door to
it: Bifrost itself runs with admin auth disabled on a private network, so the
gate has to live here.

Two things it does beyond forwarding, both because Bifrost's key API cannot be
driven honestly without them (see ``core.llm.bifrost.admin`` for how these were
learned):

* **Secrets stay ours.** A key's plaintext is mirrored into the secrets store
  under ``llm_key_<key_id>`` on write and dropped on delete, so credential
  rotation and backup keep working against ``~/.vigil/secrets.enc`` rather than
  a container volume.
* **Masked values are never round-tripped.** Reads come back masked
  (``sk-a****key``) and Bifrost accepts a write that echoes the mask, storing
  the mask as the credential — every call then 401s with nothing to say why. A
  write that carries no new secret has the stored one substituted in.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth.auth_service import AuthService
from core.config import get_settings
from core.routing import Auth, RouterMeta
from core.secrets import delete_secret, get_secret, set_secret
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/bifrost",
    tags=["bifrost-config"],
    auth=Auth.REQUIRED,
)

_TIMEOUT = 10.0
_MAX_BODY_BYTES = 256 * 1024

# What the console is allowed to reach. Anchored full-match, so nothing outside
# these five resource families is proxied — notably not ``/api/config`` (gateway
# internals), ``/api/plugins``, or ``/api/logs`` (which has its own read-side
# client in ``core.llm.bifrost.costs``).
_ALLOWED_PATHS = re.compile(
    r"""^(
        providers(/[^/]+(/keys(/[^/]+)?)?)?
      | keys
      | models(/(base|details|parameters))?
      | governance/(virtual-keys|budgets|rate-limits)(/[^/]+)?
    )$""",
    re.VERBOSE,
)

_METHODS = ("GET", "POST", "PUT", "DELETE")

# ``providers/{name}/keys`` and ``providers/{name}/keys/{key_id}`` — the only
# paths carrying a credential, and so the only ones needing secret handling.
_KEYS_PATH = re.compile(r"^providers/(?P<provider>[^/]+)/keys(?:/(?P<key_id>[^/]+))?$")


def _secret_ref(key_id: str) -> str:
    return f"llm_key_{key_id}"


def _is_masked(value: Any) -> bool:
    """True when ``value`` is a read-back rather than a new secret.

    Bifrost masks on read as ``sk-a****key`` and wraps it as
    ``{"value": ..., "env_var": ..., "from_env": ...}``. Either shape means the
    caller is echoing what it was shown, not setting a credential.
    """
    if isinstance(value, dict):
        return True
    return isinstance(value, str) and "*" in value


def _require_settings_admin(current_user: User) -> None:
    if not AuthService.check_permission(current_user.user_id, "settings.write"):
        raise HTTPException(
            status_code=403, detail="Permission denied: settings.write required"
        )


def _resolve_key_value(body: Dict[str, Any], key_id: Optional[str]) -> None:
    """Put a usable credential on ``body``, in place.

    A write that echoes the mask, or omits ``value`` entirely, is the console
    editing a key's weight or allow-list without retyping the secret. Bifrost
    has no models-only update, so the stored plaintext is substituted.
    """
    if not _is_masked(body.get("value")) and body.get("value"):
        return
    stored = get_secret(_secret_ref(key_id)) if key_id else None
    if not stored:
        raise HTTPException(
            status_code=400,
            detail=(
                "No stored credential for this key — provide the API key. "
                "Bifrost has no models-only update, so every key write needs one."
            ),
        )
    body["value"] = stored


@router.api_route("/{path:path}", methods=list(_METHODS))
async def proxy(
    path: str,
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> Response:
    """Forward one allow-listed request to Bifrost and return its answer verbatim.

    Bifrost's status codes and error bodies pass through unchanged so the console
    can surface what the gateway actually said.
    """
    _require_settings_admin(current_user)

    path = path.strip("/")
    if not _ALLOWED_PATHS.match(path):
        raise HTTPException(
            status_code=404, detail=f"Not a proxied Bifrost path: {path}"
        )

    body: Optional[Dict[str, Any]] = None
    if request.method in ("POST", "PUT"):
        raw = await request.body()
        if len(raw) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        if raw:
            try:
                body = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="Body must be JSON")
        if not isinstance(body, dict):
            body = {} if body is None else body

    keys_match = _KEYS_PATH.match(path)
    key_id = keys_match.group("key_id") if keys_match else None
    if keys_match and isinstance(body, dict):
        _resolve_key_value(body, key_id)

    url = f"{get_settings().bifrost_url.rstrip('/')}/api/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                url,
                params=dict(request.query_params),
                json=body if body is not None else None,
            )
    except httpx.HTTPError as exc:
        logger.warning("Bifrost proxy %s %s failed: %s", request.method, url, exc)
        raise HTTPException(status_code=502, detail=f"Bifrost unreachable: {exc}")

    if keys_match and upstream.status_code < 400:
        _persist_key_secret(request.method, key_id, body, upstream)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def _persist_key_secret(
    method: str,
    key_id: Optional[str],
    body: Optional[Dict[str, Any]],
    upstream: httpx.Response,
) -> None:
    """Mirror an accepted key write into the secrets store.

    On create, the ref comes from the ``key_id`` Bifrost minted in its response.
    Best-effort: a secrets failure must not undo a write Bifrost has accepted,
    but it does need a log line, because the next edit-without-retyping will
    fail on the missing ref.
    """
    try:
        if method == "DELETE":
            if key_id:
                delete_secret(_secret_ref(key_id))
            return
        value = (body or {}).get("value")
        if not value or _is_masked(value):
            return
        ref_id = key_id
        if not ref_id:
            # The keys subresource returns the UUID as ``id``; ``/api/keys``
            # spells the same value ``key_id``. Accept either.
            try:
                created = upstream.json()
                ref_id = created.get("id") or created.get("key_id")
            except Exception:
                ref_id = None
        if not ref_id:
            logger.warning(
                "Bifrost proxy: key write returned no key_id; secret not mirrored"
            )
            return
        set_secret(_secret_ref(ref_id), value)
    except Exception as exc:  # noqa: BLE001 - never fail an accepted write
        logger.warning("Bifrost proxy: could not mirror key secret: %s", exc)
