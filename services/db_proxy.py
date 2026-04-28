"""Database / integration proxy runtime helper.

Wraps three optional hop types operators may want between Vigil and either
its own metadata Postgres or a DB-shaped integration target:

* ``pgbouncer`` — connection pooler in front of Postgres. Just an endpoint
  rewrite; the wire protocol is unchanged.
* ``http`` / ``socks5`` — a generic egress proxy. Honored by HTTP-based
  integration clients (we propagate ``HTTPS_PROXY`` / ``ALL_PROXY`` to
  child processes that read them). Not applicable to Postgres directly.
* ``ssh_tunnel`` — open a local SSH forward via ``sshtunnel`` and rewrite
  the effective ``(host, port)`` to point at ``127.0.0.1:<local_port>``.

The runtime resolves a :class:`ProxyConfig` against the encrypted secrets
manager so the caller never has to assemble credentials from env. The
non-secret half of the config (type, host, port, ssh user) lives in either
``SystemConfig`` (platform DB) or the integration's stored config dict;
the secret half (proxy_password, ssh_key_passphrase) is fetched from the
secrets manager just-in-time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from backend.secrets_manager import get_secret

logger = logging.getLogger(__name__)


# Set of proxy_type values understood by this module. Anything else is
# treated as "no proxy" (a no-op) so a stray value can't break boot.
PROXY_TYPES_ALL = ("pgbouncer", "http", "socks5", "ssh_tunnel")
PROXY_TYPES_NONE = ("", "none", None)

# Subset offered for the platform metadata DB. http/socks5 are excluded
# because the Postgres wire protocol isn't proxy-aware in
# psycopg2/asyncpg — using a SOCKS proxy on the platform DB would be a
# silent footgun.
PROXY_TYPES_PLATFORM_DB = ("none", "pgbouncer", "ssh_tunnel")

# Form-field names that are sensitive. Used by integration_secrets to
# route the values through the encrypted store rather than the DB row.
PROXY_SECRET_FIELDS: Tuple[str, ...] = ("proxy_password", "ssh_key_passphrase")


@dataclass
class ProxyConfig:
    """Resolved proxy configuration for one connection target.

    All credential lookups happen at construction time via
    :meth:`from_dict`, which pulls secrets out of the encrypted secrets
    manager. Callers receive a fully-populated value object and don't
    need to know where any individual field came from.
    """

    proxy_type: str = "none"
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    proxy_password: str = ""
    ssh_private_key_path: str = ""
    ssh_key_passphrase: str = ""
    verify_proxy_tls: bool = True

    @classmethod
    def from_dict(
        cls,
        config: Mapping[str, Any],
        *,
        password_secret_key: Optional[str] = None,
        passphrase_secret_key: Optional[str] = None,
    ) -> "ProxyConfig":
        """Build a config from a non-secret dict, hydrating secrets.

        ``password_secret_key`` / ``passphrase_secret_key`` are the keys
        under which the encrypted secrets manager holds the proxy
        password and SSH key passphrase respectively. When omitted, the
        method falls back to the value present in ``config`` (used by
        unit tests and by the platform-DB path where the secrets
        manager already round-trips both).
        """

        proxy_type = (config.get("proxy_type") or "none").strip().lower()
        if proxy_type in PROXY_TYPES_NONE:
            return cls()

        password = config.get("proxy_password") or ""
        if password_secret_key:
            password = get_secret(password_secret_key) or password

        passphrase = config.get("ssh_key_passphrase") or ""
        if passphrase_secret_key:
            passphrase = get_secret(passphrase_secret_key) or passphrase

        try:
            port = int(config.get("proxy_port") or 0)
        except (TypeError, ValueError):
            port = 0

        return cls(
            proxy_type=proxy_type,
            proxy_host=str(config.get("proxy_host") or ""),
            proxy_port=port,
            proxy_username=str(config.get("proxy_username") or ""),
            proxy_password=str(password),
            ssh_private_key_path=str(config.get("ssh_private_key_path") or ""),
            ssh_key_passphrase=str(passphrase),
            verify_proxy_tls=bool(config.get("verify_proxy_tls", True)),
        )

    @property
    def enabled(self) -> bool:
        return (
            self.proxy_type not in PROXY_TYPES_NONE
            and self.proxy_type in PROXY_TYPES_ALL
        )


@dataclass
class AppliedProxy:
    """Result of applying a proxy to a target ``(host, port)``.

    * ``host`` / ``port`` — the *effective* endpoint a client should
      connect to. For ``pgbouncer`` and ``ssh_tunnel`` modes these are
      rewritten; for HTTP/SOCKS they are unchanged.
    * ``http_proxy_url`` — populated for ``http`` / ``socks5`` so HTTP
      clients (or child processes via env vars) can route through the
      egress proxy.
    * ``ssl_disabled`` — set when the operator chose ``verify_proxy_tls
      = False`` and the underlying client supports a flag for it.
    * ``tunnel_handle`` — opaque object that owns the SSH tunnel
      lifetime. The caller must keep this alive (and call
      ``handle.close()`` on shutdown) for as long as the rewritten
      endpoint is in use.
    """

    host: str
    port: int
    http_proxy_url: Optional[str] = None
    ssl_disabled: bool = False
    tunnel_handle: Optional[Any] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        """Tear down any underlying tunnel. Idempotent."""

        handle = self.tunnel_handle
        self.tunnel_handle = None
        if handle is None:
            return
        try:
            stop = getattr(handle, "stop", None)
            if callable(stop):
                stop()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to stop proxy tunnel cleanly: %s", exc)


def apply(
    target_host: str,
    target_port: int,
    proxy: ProxyConfig,
) -> AppliedProxy:
    """Apply ``proxy`` to ``(target_host, target_port)`` and return the
    effective endpoint plus any side-effects (open tunnels, env vars).

    Safe to call with a disabled :class:`ProxyConfig`; in that case it
    returns an :class:`AppliedProxy` that's a pass-through of the
    original target.
    """

    if not proxy.enabled:
        return AppliedProxy(host=target_host, port=target_port)

    proxy_type = proxy.proxy_type
    if proxy_type == "pgbouncer":
        if not proxy.proxy_host or not proxy.proxy_port:
            raise ValueError("pgbouncer proxy requires proxy_host and proxy_port")
        return AppliedProxy(
            host=proxy.proxy_host,
            port=proxy.proxy_port,
            ssl_disabled=not proxy.verify_proxy_tls,
        )

    if proxy_type in ("http", "socks5"):
        if not proxy.proxy_host or not proxy.proxy_port:
            raise ValueError(f"{proxy_type} proxy requires proxy_host and proxy_port")
        url = _http_proxy_url(proxy)
        return AppliedProxy(
            host=target_host,
            port=target_port,
            http_proxy_url=url,
            ssl_disabled=not proxy.verify_proxy_tls,
        )

    if proxy_type == "ssh_tunnel":
        if not proxy.proxy_host or not proxy.proxy_port:
            raise ValueError("ssh_tunnel requires proxy_host and proxy_port")
        handle, local_host, local_port = _open_ssh_tunnel(
            proxy, target_host, target_port
        )
        return AppliedProxy(
            host=local_host,
            port=local_port,
            tunnel_handle=handle,
        )

    # Defensive: unknown type — pretend it's disabled.
    logger.warning("Unknown proxy_type=%r; ignoring", proxy_type)
    return AppliedProxy(host=target_host, port=target_port)


def _http_proxy_url(proxy: ProxyConfig) -> str:
    """Build an ``http://``/``socks5://`` proxy URL for client libraries."""

    scheme = "http" if proxy.proxy_type == "http" else "socks5"
    if proxy.proxy_username:
        from urllib.parse import quote

        creds = quote(proxy.proxy_username, safe="")
        if proxy.proxy_password:
            creds = f"{creds}:{quote(proxy.proxy_password, safe='')}"
        return f"{scheme}://{creds}@{proxy.proxy_host}:{proxy.proxy_port}"
    return f"{scheme}://{proxy.proxy_host}:{proxy.proxy_port}"


def _open_ssh_tunnel(
    proxy: ProxyConfig, target_host: str, target_port: int
) -> Tuple[Any, str, int]:
    """Open an SSH tunnel and return (handle, local_host, local_port).

    Imported lazily so the dep is only required when the feature is used.
    """

    try:
        from sshtunnel import SSHTunnelForwarder  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ssh_tunnel proxy requires the 'sshtunnel' package. "
            "Install it with: pip install sshtunnel"
        ) from exc

    kwargs: Dict[str, Any] = {
        "ssh_address_or_host": (proxy.proxy_host, proxy.proxy_port),
        "remote_bind_address": (target_host, target_port),
        "local_bind_address": ("127.0.0.1", 0),  # let OS pick a free port
    }
    if proxy.proxy_username:
        kwargs["ssh_username"] = proxy.proxy_username
    if proxy.ssh_private_key_path:
        kwargs["ssh_pkey"] = proxy.ssh_private_key_path
        if proxy.ssh_key_passphrase:
            kwargs["ssh_private_key_password"] = proxy.ssh_key_passphrase
    elif proxy.proxy_password:
        kwargs["ssh_password"] = proxy.proxy_password

    forwarder = SSHTunnelForwarder(**kwargs)
    forwarder.start()
    local_host, local_port = forwarder.local_bind_address
    logger.info(
        "Opened SSH tunnel %s:%s -> %s:%s via %s:%s",
        local_host,
        local_port,
        target_host,
        target_port,
        proxy.proxy_host,
        proxy.proxy_port,
    )
    return forwarder, local_host, local_port


def child_env_for_proxy(proxy: ProxyConfig) -> Dict[str, str]:
    """Return env-var overrides that should be merged into a child
    process so its HTTP client respects the proxy.

    Targets the conventional ``HTTPS_PROXY`` / ``HTTP_PROXY`` /
    ``ALL_PROXY`` variables, which ``requests``, ``httpx``, and ``urllib``
    all honor by default. Empty dict when proxy is disabled or non-HTTP.
    """

    if not proxy.enabled or proxy.proxy_type not in ("http", "socks5"):
        return {}
    url = _http_proxy_url(proxy)
    env = {
        "HTTPS_PROXY": url,
        "HTTP_PROXY": url,
        "ALL_PROXY": url,
        # lowercase aliases for tools that only check those
        "https_proxy": url,
        "http_proxy": url,
        "all_proxy": url,
    }
    return env


__all__ = [
    "AppliedProxy",
    "PROXY_SECRET_FIELDS",
    "PROXY_TYPES_ALL",
    "PROXY_TYPES_NONE",
    "PROXY_TYPES_PLATFORM_DB",
    "ProxyConfig",
    "apply",
    "child_env_for_proxy",
]
