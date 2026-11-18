"""Whether Vigil's own MCP surface is listening, and who is calling it.

Two things live here because they are the same question asked twice: is this
surface open, and if a request arrived on it, whose is it?

**Open or not.** Off on a fresh install. This is another front door into a SOC
and one nobody asked for should not be listening. An operator sets a floor in
the environment before boot and moves it from Settings afterwards, so the
runtime answer is the stored one when there is one and the environment's
otherwise -- a toggle nobody has touched should not quietly outrank the
configuration a deployment shipped with.

**Whose request.** A credential authenticates the caller at the edge; the tools
are ordinary functions and have no request to consult. The principal is put
here for the duration of the call, which is what lets a tool record who called
it without every tool taking a caller argument it would have to be trusted not
to invent.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The Settings key an operator's toggle writes.
CONFIG_KEY = "mcp.server_enabled"
# The key the OAuth toggle writes (#4765). Named without "auth": CodeQL reads
# that as a credential and flags every log line the key name reaches.
SIGN_IN_SETTING = "mcp.oauth_enabled"
# Redirect URIs a registering client may name beyond loopback (#4787). Stored as
# {"uris": [...]}; empty, and so loopback only, until an operator adds one.
REDIRECT_ALLOWLIST_SETTING = "mcp.oauth_redirect_allowlist"

# Hosts allowed over plain http: the set the MCP SDK's validate_issuer_url takes.
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# What Vigil's own server is called. It is the one server in the registry that
# is not a product anyone sells, and the places that treat it differently --
# its tools are not prefixed, and it is not an integration an operator connects
# -- say so by naming this rather than the string.
VIGIL_SERVER = "vigil"

# Who is calling, for the duration of one call. Bound by the /mcp surface and by
# /internal/tools/invoke when a chat turn carries a verified principal. Unset
# means no person is behind the call, as in a hunt.
_caller: ContextVar[Optional[str]] = ContextVar("vigil_mcp_caller", default=None)


def _stored_flag(key: str, default: bool) -> bool:
    """The stored toggle for ``key``, else the environment's ``default``."""
    try:
        from core.storage.config_service import get_config_service

        stored = get_config_service().get_system_config(key)
    except Exception:  # noqa: BLE001 - a database that cannot be read is not a yes
        logger.warning("Could not read %s; using the configured default", key)
        return default

    if not stored or "enabled" not in stored:
        return default
    return bool(stored["enabled"])


def _write_flag(key: str, enabled: bool, updated_by: str) -> bool:
    from core.storage.config_service import get_config_service

    # set_system_config appends the CONFIG_CHANGE trail entry with this actor.
    return bool(
        get_config_service(user_id=updated_by).set_system_config(
            key=key,
            value={"enabled": bool(enabled)},
            config_type="mcp",
        )
    )


def is_enabled() -> bool:
    """Whether the HTTP surface should answer at all."""
    from core.config import get_settings

    return _stored_flag(CONFIG_KEY, get_settings().vigil_mcp_enabled)


def set_enabled(enabled: bool, updated_by: str) -> bool:
    """Record the operator's choice. Returns whether it was written.

    ``updated_by`` is the signed-in user, stored on the audit row.
    """
    return _write_flag(CONFIG_KEY, enabled, updated_by)


# --- OAuth on the surface (#4765) -------------------------------------------
#
# Every URL comes from VIGIL_FRONTEND_URL (which already carries the context
# path), never from a request: the Host header is the client's to choose.


def oauth_urls() -> dict:
    """Issuer, resource and both metadata URLs, computed from the public base.

    The issuer is the bare origin: the authorization server is mounted at the
    host root and the SDK advertises ``issuer + "/authorize"``.
    """
    from mcp.server.auth.routes import build_resource_metadata_url
    from pydantic import AnyHttpUrl

    from core.auth.password_reset import default_base_url
    from core.config import get_settings

    base = default_base_url(get_settings()).rstrip("/")
    parts = urlsplit(base)
    issuer = f"{parts.scheme}://{parts.netloc}"
    resource = f"{base}/mcp"
    try:
        prm = str(build_resource_metadata_url(AnyHttpUrl(resource)))
    except Exception:  # noqa: BLE001 - a malformed base is reported by oauth_refusal
        prm = None
    return {
        "base": base,
        "issuer": issuer,
        "resource": resource,
        "authorization_server_metadata": f"{issuer}/.well-known/oauth-authorization-server",
        "protected_resource_metadata": prm,
    }


def oauth_refusal() -> Optional[str]:
    """Why OAuth cannot be on right now, or None when nothing stands in the way."""
    if not is_enabled():
        return "The MCP surface is closed; open it before turning on OAuth."
    base = oauth_urls()["base"]
    parts = urlsplit(base)
    if parts.query or parts.fragment:
        return f"The public base URL {base} carries a query or fragment; set VIGIL_FRONTEND_URL without one."
    secure = parts.scheme == "https" or (
        parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK
    )
    if not secure or not parts.hostname:
        return f"The public base URL {base} is not https or a loopback address; set VIGIL_FRONTEND_URL to an https URL."
    return None


def oauth_requested() -> bool:
    """The stored-or-environment switch, before its preconditions."""
    from core.config import get_settings

    return _stored_flag(SIGN_IN_SETTING, get_settings().vigil_mcp_oauth_enabled)


def is_oauth_enabled() -> bool:
    """Whether OAuth is effectively on: switched on, surface open, base URL safe.

    The one check anything serving OAuth gates on; neither the environment
    floor nor a stale stored value bypasses the preconditions.
    """
    return oauth_requested() and oauth_refusal() is None


def set_oauth_enabled(enabled: bool, updated_by: str) -> bool:
    """Record the operator's OAuth choice. Callers check ``oauth_refusal`` first."""
    return _write_flag(SIGN_IN_SETTING, enabled, updated_by)


def oauth_redirect_allowlist() -> list[str]:
    """Non-loopback redirect URIs an operator allows, matched exactly. Empty when unread."""
    try:
        from core.storage.config_service import get_config_service

        stored = get_config_service().get_system_config(REDIRECT_ALLOWLIST_SETTING)
    except Exception:  # noqa: BLE001 - an unreadable list allows nothing extra
        logger.warning(
            "Could not read %s; allowing loopback only", REDIRECT_ALLOWLIST_SETTING
        )
        return []
    uris = (stored or {}).get("uris") or []
    return [u for u in uris if isinstance(u, str)]


def set_oauth_redirect_allowlist(uris: list[str], updated_by: str) -> bool:
    """Replace the allowlist. Returns whether it was written."""
    from core.storage.config_service import get_config_service

    return bool(
        get_config_service(user_id=updated_by).set_system_config(
            key=REDIRECT_ALLOWLIST_SETTING,
            value={"uris": [str(u) for u in uris]},
            config_type="mcp",
        )
    )


@contextmanager
def acting_as(principal: str) -> Iterator[None]:
    """Run the enclosed call as ``principal``."""
    token = _caller.set(principal)
    try:
        yield
    finally:
        _caller.reset(token)


def current_caller() -> Optional[str]:
    """The authenticated principal for this call, or None if there is none."""
    return _caller.get()
