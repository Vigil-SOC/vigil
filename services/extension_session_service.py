"""Mint short-lived session tokens for page-extension connectors.

A page extension (see ``frontend/src/redesign/extensions``) renders a
connector-supplied web component that calls the connector's BFF directly
from the browser. The BFF only trusts requests bearing a short-lived,
user-scoped session token it signs with a shared HMAC secret.

Vigil keeps its copy of that mint secret in the encrypted secrets store
(saved via Settings -> Integrations; see ``services.integration_secrets``).
This service exchanges the mint secret for a user-scoped token by calling
the connector BFF's ``POST /session`` endpoint server-to-server, so the
secret never reaches the browser.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict
from urllib.parse import urlsplit

import httpx

from backend.secrets_manager import get_secret
from database.config_service import get_config_service
from services.integration_secrets import secret_fields_for

logger = logging.getLogger(__name__)

# how long to wait on the connector BFF before giving up
_HTTP_TIMEOUT_SECONDS = 10.0

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _connector_allowlist() -> list[str]:
    """Optional runtime allowlist of connector origins (comma-separated
    ``EXTENSION_CONNECTOR_ALLOWLIST``). Empty → only the scheme rule applies."""
    raw = os.getenv("EXTENSION_CONNECTOR_ALLOWLIST", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def _is_trusted_connector_url(url: str) -> bool:
    """Backend twin of the frontend ``isTrustedConnectorUrl``. The backend calls
    this admin-set URL server-side (``POST /session``), so an untrusted value is
    an SSRF vector: require https (http only for loopback dev) and, when an
    allowlist is configured, membership in it."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    loopback = host in _LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        return False
    allow = _connector_allowlist()
    return not allow or f"{parts.scheme}://{parts.netloc}" in allow


class ExtensionSessionError(Exception):
    """Raised when a token can't be minted (mis-config or connector down).

    Carries an HTTP status the API layer surfaces verbatim.
    """

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _connector_url(integration_id: str) -> str:
    """Resolve the enabled integration's connector base URL, or raise."""
    cfg = get_config_service().get_integration_config(integration_id)
    if not cfg or not cfg.get("enabled"):
        raise ExtensionSessionError(
            f"Integration '{integration_id}' is not enabled", status_code=404
        )
    url = (cfg.get("config") or {}).get("connectorUrl")
    if not url:
        raise ExtensionSessionError(
            f"Integration '{integration_id}' has no connectorUrl configured",
            status_code=400,
        )
    url = str(url).rstrip("/")
    if not _is_trusted_connector_url(url):
        raise ExtensionSessionError(
            f"Integration '{integration_id}' connectorUrl is not a trusted origin "
            "(https required; must match EXTENSION_CONNECTOR_ALLOWLIST when set)",
            status_code=400,
        )
    return url


def _mint_secret(integration_id: str) -> str | None:
    """Read the integration's session signing secret from the secrets store.

    Resolves the secrets-store key via the shared registry (loglm ->
    ``LOGLM_MINT_SECRET``) rather than hardcoding it here. Returns ``None``
    when none is configured; the caller degrades to a session-less context
    instead of failing the panel (see ``mint_session_token``).
    """
    env_key = secret_fields_for(integration_id).get("mint_secret")
    return get_secret(env_key) if env_key else None


async def mint_session_token(integration_id: str, username: str) -> Dict[str, Any]:
    """Exchange the shared mint secret for a short-lived, user-scoped token.

    Calls the connector BFF ``POST {connectorUrl}/session`` with the mint
    secret as a bearer and the Vigil user identity as the subject. Returns
    ``{token, expires_in, user}`` for the frontend to hand to the element.

    When no mint secret is configured the connector's data endpoints are
    taken to be open (dev / restricted-network), so a session-less context
    (``token=None``) is returned rather than failing: the element still
    mounts, and if the connector *does* require auth its own data call
    surfaces a 401 locally instead of blanking the whole panel.
    """
    connector_url = _connector_url(integration_id)
    secret = _mint_secret(integration_id)
    if not secret:
        logger.info(
            "no mint secret for '%s'; issuing a session-less extension context",
            integration_id,
        )
        return {"token": None, "expires_in": None, "user": username}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{connector_url}/session",
                headers={"Authorization": f"Bearer {secret}"},
                json={"subject": username, "user": username},
            )
    except httpx.HTTPError as e:
        logger.warning("extension session mint failed for %s: %s", integration_id, e)
        raise ExtensionSessionError(
            f"Could not reach the '{integration_id}' connector", status_code=502
        ) from e

    if resp.status_code >= 400:
        logger.warning(
            "connector '%s' rejected session mint: %s %s",
            integration_id,
            resp.status_code,
            resp.text[:200],
        )
        raise ExtensionSessionError(
            f"Connector rejected the session request ({resp.status_code})",
            status_code=502,
        )

    data = resp.json()
    token = data.get("token")
    if not token:
        raise ExtensionSessionError("Connector returned no token", status_code=502)
    return {
        "token": token,
        "expires_in": data.get("expires_in"),
        "user": username,
    }
