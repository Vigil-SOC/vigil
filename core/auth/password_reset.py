"""
Password reset links (#4227).

A reset link is an ``auth_links`` row of kind ``reset`` (``core/auth/invites.py``
owns the table): only the SHA-256 of its random value is stored, it lives for
``RESET_TTL_SECONDS``, and it is used up by the confirm that sets the password.
An administrator revokes it, and deactivating or deleting the account, or a
change of its password or email, closes every open one.
"""

import os

# A reset link's lifetime. Fixed: PASSWORD_RESET_TTL_SECONDS is retired.
RESET_TTL_SECONDS = 3600


def default_base_url(settings) -> str:
    """Console URL for reset links: VIGIL_FRONTEND_URL, else this host's console.
    Never derived from the request, whose Host header the client controls."""
    if settings.vigil_frontend_url:
        return settings.vigil_frontend_url
    port = 6987 if os.path.exists("/.dockerenv") else 6988
    return f"http://localhost:{port}{settings.vigil_context_path.rstrip('/')}"


def reset_link(base_url: str, token: str) -> str:
    """Console URL that opens the reset screen for ``token``."""
    return f"{base_url.rstrip('/')}/reset-password?token={token}"
