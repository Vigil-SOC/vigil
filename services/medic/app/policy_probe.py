"""Is NetworkPolicy enforced here? (A3-3, C3 §4.4, check 14).

Medic tries one TCP connection its own egress policy must block. Most
policy-enforcing network plugins drop a denied packet (a timeout); some can be
set to REJECT it (Calico `Reject`), which connect() reports as refused (S7-2).
Both show the policy at work, but only next to a control connection that works
to the same pods: the caller checks that. A connection shows the plugin ignores
NetworkPolicy (flannel, a bare CNI). Anything else (no route, an unresolvable
name) shows nothing either way, so the caller refuses on it: fail closed.
"""

from __future__ import annotations

import enum
import socket
from collections.abc import Callable

TIMEOUT_S = 3.0


class Verdict(enum.Enum):
    BLOCKED = "blocked"  # timed out: the policy dropped it
    REFUSED = "refused"  # refused: the policy rejected it (or no listener/endpoint)
    CONNECTED = "connected"  # reached a target the policy must block
    INCONCLUSIVE = "inconclusive"  # unreachable, reset, ...: proves nothing
    UNRESOLVED = "unresolved"  # the target's name didn't resolve

    @property
    def enforced(self) -> bool:
        """What a policy would produce. Only evidence with a working control,
        and a refusal only once it has held (see cli._policy_enforced)."""
        return self in (Verdict.BLOCKED, Verdict.REFUSED)


def _resolve(host: str, port: int) -> list[tuple[str, int]]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][:2] for info in infos]


def probe(
    target: tuple[str, int],
    *,
    timeout: float = TIMEOUT_S,
    connect: Callable = socket.create_connection,
    resolve: Callable[[str, int], list[tuple[str, int]]] = _resolve,
) -> Verdict:
    """One attempt to the first resolved address; never retried, never logged here."""
    try:
        addrs = resolve(*target)
    except OSError:  # gaierror is an OSError
        return Verdict.UNRESOLVED
    if not addrs:
        return Verdict.UNRESOLVED
    try:
        conn = connect(addrs[0], timeout)
    except TimeoutError:  # socket.timeout is TimeoutError on 3.10+
        return Verdict.BLOCKED
    except ConnectionRefusedError:
        return Verdict.REFUSED
    except OSError:
        return Verdict.INCONCLUSIVE
    conn.close()
    return Verdict.CONNECTED
