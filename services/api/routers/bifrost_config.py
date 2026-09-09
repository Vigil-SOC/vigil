"""Authenticated passthrough to Bifrost's config API.

Bifrost's own config store is the source of truth for providers, keys, model
allow-lists, pricing and governance. This router is the console's only door to
it: Bifrost itself runs with admin auth disabled on a private network, so the
gate has to live here.

Three things it does beyond forwarding. The first two are because Bifrost's key
API cannot be driven honestly without them (see ``core.llm.bifrost.admin`` for
how those were learned); the third is because the rest of Vigil does not read
Bifrost:

* **Secrets stay ours.** A key's plaintext is mirrored into the secrets store
  under ``llm_key_<key_id>`` on write and dropped on delete, so credential
  rotation and backup keep working against ``~/.vigil/secrets.enc`` rather than
  a container volume.
* **Masked values are never round-tripped.** Reads come back masked
  (``sk-a****key``) and Bifrost accepts a write that echoes the mask, storing
  the mask as the credential — every call then 401s with nothing to say why. A
  write that carries no new secret has the stored one substituted in.
* **A key write mirrors a provider row.** Vigil indexes providers by
  ``llm_provider_configs``, so a key that exists only in Bifrost is unreachable
  to the model picker, to assignments, and to dispatch — see
  ``core.llm.bifrost.mirror``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Dict, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.auth.auth_service import AuthService
from core.config import get_settings
from core.llm.bifrost import mirror
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
_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE"})

# ``providers/{name}/keys`` and ``providers/{name}/keys/{key_id}`` — the only
# paths carrying a credential, and so the only ones needing secret handling.
_KEYS_PATH = re.compile(r"^providers/(?P<provider>[^/]+)/keys(?:/(?P<key_id>[^/]+))?$")


def _secret_ref(key_id: str) -> str:
    return f"llm_key_{key_id}"


def _scope_ref(key_id: str) -> str:
    """Where a Vertex key's project/region are kept.

    Not secrets, but Bifrost masks them like one and offers no unmasked read —
    ``?unmasked``/``?reveal`` all come back ``prod****oglm`` — so the real
    values cannot be recovered from the gateway. Keeping our own copy is what
    lets an edit that only changes the weight leave the scope intact.
    """
    return f"llm_key_{key_id}_vertex_scope"


def _ollama_ref(key_id: str) -> str:
    """Where an Ollama key's endpoint URL is kept.

    Same reason as ``_scope_ref``: not a secret, but Bifrost masks it on read
    and offers no way to get it back, so an edit that only changes the weight
    would otherwise hand the gateway a mask in place of the endpoint.
    """
    return f"llm_key_{key_id}_ollama_url"


def _stored_scope(key_id: Optional[str]) -> Dict[str, str]:
    if not key_id:
        return {}
    raw = get_secret(_scope_ref(key_id))
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Bifrost proxy: stored vertex scope for %s is not JSON", key_id)
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def _resolve_vertex_scope(vertex: Dict[str, Any], key_id: Optional[str]) -> None:
    """Fill in ``project_id``/``region`` the write left out, in place.

    The console omits a field it isn't changing rather than echoing the mask it
    was shown, so an edit that only touches the weight arrives with no scope at
    all. Bifrost would take that literally and blank it, pointing the key at no
    project. A key configured outside Vigil has no stored copy to fall back on,
    and the gateway will not reveal one — so that case asks rather than guesses.
    """
    stored = _stored_scope(key_id)
    missing = []
    for field in ("project_id", "region"):
        supplied = vertex.get(field)
        if supplied and not _is_masked(supplied):
            continue
        if stored.get(field):
            vertex[field] = stored[field]
        else:
            missing.append(field)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "This Vertex key needs its "
                + " and ".join(f.replace("_", " ") for f in missing)
                + ". Bifrost masks both on read and will not reveal them, so a "
                "key first configured outside Vigil has to have them retyped once."
            ),
        )


def _resolve_ollama_url(ollama: Dict[str, Any], key_id: Optional[str]) -> None:
    """Fill in the endpoint an Ollama write left out, in place.

    ``env.OLLAMA_URL`` and friends pass straight through — a reference is not a
    mask, and the seeded key is only editable at all because Bifrost returns
    ``env_var`` unmasked. Anything else masked or absent falls back to our own
    copy, and a key first configured outside Vigil has none, so that case asks
    rather than pointing the gateway at nothing.
    """
    supplied = ollama.get("url")
    if supplied and not _is_masked(supplied):
        return
    stored = get_secret(_ollama_ref(key_id)) if key_id else None
    if not stored:
        raise HTTPException(
            status_code=400,
            detail=(
                "This Ollama key needs its server URL. Bifrost masks it on read "
                "and will not reveal it, so a key first configured outside Vigil "
                "has to have it retyped once."
            ),
        )
    ollama["url"] = stored


def _resolve_optional_token(body: Dict[str, Any], key_id: Optional[str]) -> None:
    """Carry a bearer token through a write that didn't retype it, in place.

    Ollama's own server takes no credential, but the gateway does send
    ``value`` as ``Authorization: Bearer`` when one is set — which is what
    reaches an Ollama put behind an authenticating proxy. So the token is
    optional here, unlike every provider handled by ``_resolve_key_value``:
    absent with nothing stored means "no auth", not "you forgot the key".

    The console states the mode, so the two empty cases are not the same and
    are told apart by whether the field is there at all. An *omitted* value is
    "keep what is stored", so a weight edit does not silently strip the token
    off a key that needs one. An *empty* value is the operator choosing No
    auth, and has to survive — substituting the stored token there would make
    the switch back impossible.
    """
    if "value" not in body:
        stored = get_secret(_secret_ref(key_id)) if key_id else None
        if stored:
            body["value"] = stored
        return
    supplied = body["value"]
    if not supplied:
        return
    if not _is_masked(supplied):
        return
    # A mask is the console echoing what it was shown, which it does not do for
    # this field — but a third-party client might, and storing the mask as a
    # token would send the gateway off with a credential of asterisks.
    stored = get_secret(_secret_ref(key_id)) if key_id else None
    if stored:
        body["value"] = stored
    else:
        body.pop("value", None)


def _resolve_key_value(
    body: Dict[str, Any], key_id: Optional[str], provider: Optional[str] = None
) -> None:
    """Put a usable credential on ``body``, in place.

    A write that echoes the mask, or omits ``value`` entirely, is the console
    editing a key's weight or allow-list without retyping the secret. Bifrost
    has no models-only update, so the stored plaintext is substituted.

    Two providers break the plain ``value`` shape:

    * **Vertex** authenticates one of two ways. A *service account* is scoped
      by ``project_id``/``region`` under ``vertex_key_config`` and carries its
      JSON in ``vertex_key_config.auth_credentials``; an *API key* carries a
      bare ``value`` like any other provider and sends no ``vertex_key_config``
      at all. The presence of that block is therefore what marks the mode.
      Either credential is mirrored to ``value`` so a single ``llm_key_<id>``
      ref backs the key, and an edit that leaves the credential blank
      substitutes the stored copy back in.
    * **Ollama** carries its endpoint under ``ollama_key_config`` and a bearer
      token, when the endpoint wants one, under ``value``. Both read back
      masked, so both are substituted from our own copies.

    ``provider`` comes from the request path. Keying Ollama on its block
    instead sent an edit that did not retype the URL -- which omits the block
    -- down the API-key path, where it was refused for want of a credential
    Ollama does not have. Vertex still keys on its block, because there the
    block *is* the statement: a service account sends one and an express-mode
    API key does not.
    """
    ollama = body.get("ollama_key_config")
    if provider == "ollama" or (provider is None and isinstance(ollama, dict)):
        # Bifrost takes an absent block literally and blanks the endpoint.
        if not isinstance(ollama, dict):
            ollama = {}
            body["ollama_key_config"] = ollama
        _resolve_ollama_url(ollama, key_id)
        _resolve_optional_token(body, key_id)
        return

    vertex = body.get("vertex_key_config")
    if isinstance(vertex, dict):
        _resolve_vertex_scope(vertex, key_id)
        # Service-account mode: the JSON is the credential, mirrored to value.
        # Keyed on the block, not on ``auth_credentials`` within it: editing
        # project/region without retyping the JSON omits that field entirely
        # (AiProvidersPanel), and keying on it sent exactly that edit down the
        # API-key path — ``value`` was restored but ``auth_credentials`` was
        # left unset, handing Bifrost a scoped key with no credential.
        sa = vertex.get("auth_credentials")
        if _is_masked(sa) or not sa:
            stored = get_secret(_secret_ref(key_id)) if key_id else None
            if not stored:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No stored service-account credential for this Vertex key — "
                        "paste the service-account JSON. Bifrost has no "
                        "credential-only update, so every key write needs one."
                    ),
                )
            vertex["auth_credentials"] = stored
        # `value` is deliberately NOT set from the service account. Bifrost
        # picks a vertex key's auth mode by whether `value` is populated, so
        # mirroring the JSON there made it authenticate as an API key and the
        # request came back "API keys are not supported by this API. Expected
        # OAuth2 access token" — a 401 that says nothing about the real cause.
        # A service-account key carries its credential in `auth_credentials`
        # alone; the secrets-store copy is keyed off that field instead.
        body.pop("value", None)
        return

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


# Declared before the catch-all below, which FastAPI would otherwise match
# first. Not a proxied path: this is Vigil's own verdict about Bifrost's keys,
# computed in core.llm.bifrost.mirror so the console, the setup gate and
# scripts/reset-setup.sh all read one implementation instead of restating it in
# three languages.
class KeyVerdict(BaseModel):
    """Whether one Bifrost key can route, and how the console should badge it."""

    provider: str
    routable: bool
    health: Literal["healthy", "unverified", "unverifiable", "rejected"]
    description: Optional[str] = None


class Routability(BaseModel):
    """Declared rather than a bare dict so the generated client carries the
    shape: hand-maintaining it in TypeScript is the drift this route's own
    verdict exists to end."""

    providers: Dict[str, bool]
    keys: Dict[str, KeyVerdict]


@router.get("/routability", response_model=Routability)
async def routability(
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> Dict[str, Any]:
    """Per-key and per-provider "can this actually route?".

    One call answers what used to take 1 + N proxy round trips — the setup gate
    listed providers, then every provider's keys, on its critical path.
    """
    _require_settings_admin(current_user)
    try:
        return await mirror.routability()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError: a gateway that answers 200 with something other than JSON
        # is still an upstream failure, not a bug in this handler.
        logger.warning("Routability check failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Bifrost unreachable: {exc}")


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
        _resolve_key_value(body, key_id, keys_match.group("provider"))

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

    if keys_match and request.method in _WRITE_METHODS and upstream.status_code < 400:
        provider = keys_match.group("provider")
        _persist_key_secret(request.method, key_id, body, upstream, provider)
        await mirror.sync_provider(provider)

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
    provider: Optional[str] = None,
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
                delete_secret(_scope_ref(key_id))
                delete_secret(_ollama_ref(key_id))
            return
        # An ollama key carries no credential at all — its endpoint takes the
        # place of one, and is kept for the same reason the vertex scope is.
        ollama = (body or {}).get("ollama_key_config")
        if provider == "ollama" and isinstance(ollama, dict):
            ref_id = _written_key_id(key_id, upstream)
            url = ollama.get("url")
            if ref_id and url and not _is_masked(url):
                set_secret(_ollama_ref(ref_id), url)
            # The bearer token, when the deployment has one, is a real secret
            # and is kept like any other. Its absence is normal here, and an
            # explicit empty one is No auth — drop the copy so a later edit
            # does not resurrect a token the operator turned off.
            token = (body or {}).get("value")
            if ref_id and token and not _is_masked(token):
                set_secret(_secret_ref(ref_id), token)
            elif ref_id and "value" in (body or {}) and not token:
                delete_secret(_secret_ref(ref_id))
            return
        # A vertex service-account key sends no `value` at all (see
        # _resolve_key_value), so its credential is read off the vertex block.
        vertex = (body or {}).get("vertex_key_config")
        if isinstance(vertex, dict):
            value = vertex.get("auth_credentials")
        else:
            value = (body or {}).get("value")
        if not value or _is_masked(value):
            return
        ref_id = _written_key_id(key_id, upstream)
        if not ref_id:
            logger.warning(
                "Bifrost proxy: key write returned no key_id; secret not mirrored"
            )
            return
        set_secret(_secret_ref(ref_id), value)
        _persist_vertex_scope(ref_id, body)
    except Exception as exc:  # noqa: BLE001 - never fail an accepted write
        logger.warning("Bifrost proxy: could not mirror key secret: %s", exc)


def _written_key_id(key_id: Optional[str], upstream: httpx.Response) -> Optional[str]:
    """The id to file a key's stored fields under, on a create or an update."""
    if key_id:
        return key_id
    # The keys subresource returns the UUID as ``id``; ``/api/keys`` spells the
    # same value ``key_id``. Accept either.
    try:
        created = upstream.json()
        return created.get("id") or created.get("key_id")
    except Exception:
        return None


def _persist_vertex_scope(key_id: str, body: Optional[Dict[str, Any]]) -> None:
    """Keep our own copy of a Vertex key's project/region.

    Written after ``_resolve_vertex_scope`` has already filled in whatever the
    request omitted, so this stores the complete scope rather than only the
    fields that happened to be retyped.
    """
    vertex = (body or {}).get("vertex_key_config")
    if not isinstance(vertex, dict):
        return
    scope = {
        field: vertex[field]
        for field in ("project_id", "region")
        if vertex.get(field) and not _is_masked(vertex[field])
    }
    if scope:
        set_secret(_scope_ref(key_id), json.dumps(scope))
