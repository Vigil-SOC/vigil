"""Is NetworkPolicy enforced here? (A3-3, C3 §4.4, check 14).

Medic tries one TCP connection its own egress policy must block. Policy-enforcing
network plugins (Calico, Cilium) drop a denied packet, so only a timeout shows
the policy at work. A connection shows the plugin ignores NetworkPolicy (kind's
kindnet, flannel). A refusal or an unresolvable name shows nothing either way,
so the caller treats every verdict but BLOCKED as "not enforced": fail closed.
"""

from __future__ import annotations

import enum
import socket
from collections.abc import Callable

TIMEOUT_S = 3.0


class Verdict(enum.Enum):
    BLOCKED = "blocked"  # timed out: the policy dropped it
    CONNECTED = "connected"  # reached a target the policy must block
    INCONCLUSIVE = "inconclusive"  # refused, unreachable: proves nothing
    UNRESOLVED = "unresolved"  # the target's name didn't resolve

    @property
    def enforced(self) -> bool:
        return self is Verdict.BLOCKED


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
    except OSError:
        return Verdict.INCONCLUSIVE
    conn.close()
    return Verdict.CONNECTED
