"""Per-integration egress proxy: the ``proxy=`` value for an integration's httpx client.

The wizard's proxy block saves ``proxy_*`` fields for the integration. Env vars
set on a tool server's spawn would miss the API process (Test) and the daemon,
so each client asks here for its own proxy where it builds its httpx client,
as it does for ``tls_verify``.
"""

from __future__ import annotations

from typing import Optional

from core.integrations._base.config import resolve_fields
from core.storage.db_proxy import ProxyConfig, http_proxy_url

_PROXY_FIELDS = (
    "proxy_type",
    "proxy_host",
    "proxy_port",
    "proxy_username",
    "proxy_password",
)


def integration_proxy(integration_id: str) -> Optional[str]:
    """The proxy URL for an integration, or ``None`` to keep httpx's env defaults.

    Only ``http`` and ``socks5`` with a host and port yield a URL; ``none``,
    blank and legacy ``pgbouncer`` / ``ssh_tunnel`` rows yield ``None``. The
    password lives in the secrets store, never in the stored config dict.
    """
    config = ProxyConfig.from_dict(resolve_fields(integration_id, _PROXY_FIELDS))
    if config.proxy_type not in ("http", "socks5"):
        return None
    if not config.proxy_host or not config.proxy_port:
        return None
    return http_proxy_url(config)
