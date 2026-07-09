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
from typing import Any, Dict

import httpx

from backend.secrets_manager import get_secret
from database.config_service import get_config_service
from services.integration_secrets import secret_fields_for

logger = logging.getLogger(__name__)

# how long to wait on the connector BFF before giving up
_HTTP_TIMEOUT_SECONDS = 10.0


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
    return str(url).rstrip("/")


def _mint_secret(integration_id: str) -> str:
    """Read the integration's session signing secret from the secrets store.

    Resolves the secrets-store key via the shared registry (loglm ->
    ``LOGLM_MINT_SECRET``) rather than hardcoding it here.
    """
    env_key = secret_fields_for(integration_id).get("mint_secret")
    secret = get_secret(env_key) if env_key else None
    if not secret:
        raise ExtensionSessionError(
            f"Integration '{integration_id}' has no session signing secret configured",
            status_code=400,
        )
    return secret


async def mint_session_token(integration_id: str, username: str) -> Dict[str, Any]:
    """Exchange the shared mint secret for a short-lived, user-scoped token.

    Calls the connector BFF ``POST {connectorUrl}/session`` with the mint
    secret as a bearer and the Vigil user identity as the subject. Returns
    ``{token, expires_in, user}`` for the frontend to hand to the element.
    """
    connector_url = _connector_url(integration_id)
    secret = _mint_secret(integration_id)

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
