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

logger = logging.getLogger(__name__)

# The Settings key an operator's toggle writes.
CONFIG_KEY = "mcp.server_enabled"

# What Vigil's own server is called. It is the one server in the registry that
# is not a product anyone sells, and the places that treat it differently --
# its tools are not prefixed, and it is not an integration an operator connects
# -- say so by naming this rather than the string.
VIGIL_SERVER = "vigil"

# Who is calling, for the duration of one call. Bound by the /mcp surface and by
# /internal/tools/invoke when a chat turn carries a verified principal. Unset
# means no person is behind the call, as in a hunt.
_caller: ContextVar[Optional[str]] = ContextVar("vigil_mcp_caller", default=None)


def is_enabled() -> bool:
    """Whether the HTTP surface should answer at all."""
    from core.config import get_settings

    default = get_settings().vigil_mcp_enabled
    try:
        from core.storage.config_service import get_config_service

        stored = get_config_service().get_system_config(CONFIG_KEY)
    except Exception:  # noqa: BLE001 - a database that cannot be read is not a yes
        logger.warning(
            "Could not read the MCP surface setting; using the configured default"
        )
        return default

    if not stored or "enabled" not in stored:
        return default
    return bool(stored["enabled"])


def set_enabled(enabled: bool) -> bool:
    """Record the operator's choice. Returns whether it was written."""
    from core.storage.config_service import get_config_service

    return bool(
        get_config_service().set_system_config(
            key=CONFIG_KEY,
            value={"enabled": bool(enabled)},
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
