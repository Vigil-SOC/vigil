"""Mint short-lived, user-scoped session tokens for page-extension connectors.

Exchanges Vigil's copy of the connector's HMAC mint secret for a token by calling
the connector BFF's ``POST /session`` server-to-server, so the secret never
reaches the browser.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from backend.secrets_manager import get_secret
from database.config_service import get_config_service
from services.integration_secrets import secret_fields_for
from services.extension_trust import is_trusted_connector_url

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10.0


class ExtensionSessionError(Exception):
    """Raised when a token can't be minted. Carries an HTTP status the API layer
    surfaces verbatim."""

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
    if not is_trusted_connector_url(url):
        raise ExtensionSessionError(
            f"Integration '{integration_id}' connectorUrl is not a trusted origin "
            "(https required; must match EXTENSION_CONNECTOR_ALLOWLIST when set)",
            status_code=400,
        )
    return url


def _mint_secret(integration_id: str) -> str | None:
    """Session signing secret from the store, or ``None`` — the caller then
    degrades to a session-less context rather than failing the panel."""
    env_key = secret_fields_for(integration_id).get("mint_secret")
    return get_secret(env_key) if env_key else None


async def mint_session_token(integration_id: str, username: str) -> Dict[str, Any]:
    """Exchange the mint secret for a token via the connector BFF's POST /session.

    With no mint secret configured the connector is taken to be open, so a
    session-less context (``token=None``) is returned rather than failing — the
    element still mounts and any auth requirement surfaces on its own data call.
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
