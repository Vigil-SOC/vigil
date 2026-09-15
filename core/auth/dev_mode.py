"""The DEV_MODE bypass: what it opens, and the noise it makes doing it.

``DEV_MODE=true`` turns authentication off across the whole service. Two gates
honour it, and they are named individually below because "auth is off" is too
vague to act on when you are staring at a log:

* session auth — ``get_current_user`` returns an admin without reading a token
* the vstrike inbound API-key check

The ``/internal`` path the agent worker uses is deliberately absent: it is
shared-secret only (``core.agents.internal_auth``) and has no dev-mode branch,
so naming it here would report a door open that is not.

The bypass never blocks a startup. Someone turning it on is doing it
deliberately, including in places we would not choose for them — a throwaway
cluster, a demo box, a container someone wants to poke at — and a service that
refuses to boot is a service they cannot use to find out why. What it does
instead is refuse to be quiet: every startup announces it, and a bypass on an
address the network can reach announces that too, in its own right, because
that is the case where the setting stops being a local convenience.
"""

import logging
import sys

from core.config import get_settings

logger = logging.getLogger(__name__)

# Addresses that reach only this machine.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

# The gates DEV_MODE opens, in the order the banner lists them.
BYPASSED_GATES = (
    "session authentication (every request is a full-permission admin)",
    "vstrike inbound API key",
)

_BANNER_WIDTH = 74


def is_exposed(bind_host: str) -> bool:
    """Is this address reachable from somewhere other than this machine?"""
    return (bind_host or "").strip().lower() not in LOOPBACK_HOSTS


def _banner(bind_host: str) -> str:
    rule = "!" * _BANNER_WIDTH
    exposed = is_exposed(bind_host)

    lines = ["", rule, "  DEV_MODE IS ON — THIS SERVICE IS NOT AUTHENTICATED", ""]

    if exposed:
        lines += [
            f"  AND IT IS LISTENING ON {bind_host}, WHICH IS NOT LOOPBACK.",
            "  Anyone who can route to this host has full administrative access",
            "  to it. No password, no token, no audit trail worth the name.",
            "",
        ]

    lines += ["  Open to anyone who can reach it:"]
    lines += [f"    - {gate}" for gate in BYPASSED_GATES]
    lines += [""]

    if exposed:
        lines += [
            "  If this is not a throwaway machine, stop it now: set DEV_MODE=false",
            "  and provide JWT_SECRET_KEY.",
        ]
    else:
        lines += [
            f"  Bound to {bind_host or '127.0.0.1'} (loopback — this machine only).",
            "  Set DEV_MODE=false in .env to require a login.",
        ]

    lines += [rule, ""]
    return "\n".join(lines)


def announce_dev_mode() -> None:
    """Say, loudly and on every startup, that authentication is off.

    Called at import of the API entrypoint, next to ``validate_settings_or_exit``,
    so it lands before the first request rather than somewhere in the noise after
    it.
    """
    settings = get_settings()
    if not settings.dev_mode:
        return

    exposed = is_exposed(settings.bind_host)

    # Auditable in whatever collects this service's logs: one event per startup,
    # under a stable name, carrying what was opened and whether it is exposed.
    logger.warning(
        "security.dev_mode_enabled: authentication bypassed on %s%s",
        settings.bind_host,
        " (REACHABLE FROM THE NETWORK)" if exposed else "",
        extra={
            "event": "security.dev_mode_enabled",
            "bind_host": settings.bind_host,
            "exposed": exposed,
            "bypassed_gates": list(BYPASSED_GATES),
        },
    )

    # stderr as well as the log: a container's logs are the only surface some
    # deployments have, and a WARNING can be filtered out by a log level nobody
    # remembers setting.
    print(_banner(settings.bind_host), file=sys.stderr)
