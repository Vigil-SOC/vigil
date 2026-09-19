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

import ipaddress
import logging
import sys

from core.config import get_settings

logger = logging.getLogger(__name__)

# Names that reach only this machine. Numeric addresses are not listed: the
# loopback range is wider than 127.0.0.1 and ipaddress already knows it.
_LOOPBACK_NAMES = frozenset({"localhost"})

# The gates DEV_MODE opens, in the order the banner lists them.
BYPASSED_GATES = (
    "session authentication (every request is a full-permission admin)",
    "vstrike inbound API key",
)

_BANNER_WIDTH = 74


def is_exposed(bind_host: str) -> bool:
    """Is this address reachable from somewhere other than this machine?

    Anything not known to be local counts as exposed — an unset host (uvicorn's
    "every interface", not loopback) and a name this process cannot resolve
    alike. The two mistakes available here are not symmetrical: calling a
    loopback bind exposed prints a banner someone can dismiss in a second, and
    calling an exposed bind loopback is a silence that outlives the session.
    """
    host = (bind_host or "").strip().lower()
    if not host:
        return True
    if host in _LOOPBACK_NAMES:
        return False
    try:
        # strip("[]") for the bracketed IPv6 form a --host argument can carry.
        return not ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return True


def bind_host_from_argv() -> str | None:
    """The address on this process's own command line, if it carries one.

    BIND_HOST is what the launchers export and what ``Settings`` reads, but
    ``uvicorn --host 0.0.0.0`` typed by hand exports nothing. Without this the
    banner would fall back to the unset default and call that run loopback --
    wrong in the one direction where being wrong costs something.
    """
    args = list(sys.argv)
    for index, arg in enumerate(args):
        if arg == "--host" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return None


def resolve_bind_host() -> str:
    """The address this process will listen on, as closely as it can be known.

    The command line wins: uvicorn binds what it was passed, whatever the
    environment says. Otherwise BIND_HOST, which every launcher in the repo
    exports before starting the server.
    """
    from_argv = bind_host_from_argv()
    return from_argv if from_argv is not None else get_settings().bind_host


def _describe(bind_host: str) -> str:
    """The address as the banner should name it, never as an empty space."""
    return bind_host or "every interface (no host set)"


def _banner(bind_host: str) -> str:
    rule = "!" * _BANNER_WIDTH
    exposed = is_exposed(bind_host)

    lines = ["", rule, "  DEV_MODE IS ON — THIS SERVICE IS NOT AUTHENTICATED", ""]

    if exposed:
        lines += [
            f"  AND IT IS LISTENING ON {_describe(bind_host)}, WHICH IS NOT LOOPBACK.",
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
            f"  Bound to {bind_host} (loopback — this machine only).",
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

    bind_host = resolve_bind_host()
    exposed = is_exposed(bind_host)

    # Auditable in whatever collects this service's logs: one event per startup,
    # under a stable name, carrying what was opened and whether it is exposed.
    logger.warning(
        "security.dev_mode_enabled: authentication bypassed on %s%s",
        bind_host,
        " (REACHABLE FROM THE NETWORK)" if exposed else "",
        extra={
            "event": "security.dev_mode_enabled",
            "bind_host": bind_host,
            "exposed": exposed,
            "bypassed_gates": list(BYPASSED_GATES),
        },
    )

    # stderr as well as the log: a container's logs are the only surface some
    # deployments have, and a WARNING can be filtered out by a log level nobody
    # remembers setting.
    print(_banner(bind_host), file=sys.stderr)
