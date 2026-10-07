"""Client-credentials token exchange against Microsoft Entra ID (Azure AD)."""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_AADSTS = re.compile(r"AADSTS\d+")
_REJECTED_STATUSES = (400, 401, 403)


class TokenError(Exception):
    """The token exchange failed; ``str()`` is safe to show to a tool caller."""


def fetch_client_credentials_token(
    label: str, tenant: str, client_id: str, client_secret: str, scope: str
) -> str:
    """Exchange a client secret for an access token, or raise ``TokenError``.

    Logs the HTTP status and the AADSTS code / description from the response
    body. Never logs the secret or the token.
    """
    try:
        resp = httpx.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        logger.error("%s token request failed: %s", label, type(exc).__name__)
        raise TokenError(
            f"{label} token request failed ({type(exc).__name__}); "
            "Entra ID could not be reached"
        ) from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        description = str(body.get("error_description") or body.get("error") or "")
        code = _AADSTS.search(description)
        detail = code.group(0) if code else None
        logger.error(
            "%s token exchange returned HTTP %s: %s",
            label,
            resp.status_code,
            description[:300] or "<no error body>",
        )
        suffix = f", {detail}" if detail else ""
        if resp.status_code in _REJECTED_STATUSES:
            raise TokenError(
                f"{label} credential rejected by Entra ID (HTTP {resp.status_code}{suffix})"
            )
        raise TokenError(
            f"{label} token request failed (HTTP {resp.status_code}{suffix})"
        )

    try:
        token = resp.json().get("access_token")
    except ValueError:
        token = None
    if not token:
        logger.error("%s token response carried no access_token", label)
        raise TokenError(f"{label} token response carried no access_token")
    return token
