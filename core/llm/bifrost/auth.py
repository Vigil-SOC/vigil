"""HTTP Basic for Bifrost's management API.

Inference (/v1, /anthropic, /health) does not use this. On v2.2.1 the
inference middleware never checks the admin password, and
``client.enforce_auth_on_inference`` is what would require a credential
there. It is set false in config.json.
"""

from __future__ import annotations

from core.config import get_settings

# Matches governance.auth_config.admin_username in the Bifrost config.
BIFROST_ADMIN_USERNAME = "vigil"


def management_auth() -> tuple[str, str]:
    """Username and password for an ``/api/*`` request.

    An empty password is still sent. A blank password on Bifrost answers 401
    rather than leaving the API open. start.sh and dc() mint one before a
    normal boot.
    """
    return (BIFROST_ADMIN_USERNAME, get_settings().bifrost_admin_password)
