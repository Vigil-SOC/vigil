"""How a Case closes: the categories, and who did it.

One module rather than a constant in each writer, because three of them close a
Case — the HTTP close, the MCP tool, and the status edit the console actually
uses — and episodic memory reads what all three wrote. A vocabulary spelled in
four places is a vocabulary that disagrees with itself the first time one is
edited.
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence


class ClosureCategory(str, Enum):
    """What closing the Case determined.

    ``UNSPECIFIED`` is the one that is not a determination. It exists because
    the console closes a Case by setting its status and asks for no category,
    and the alternative to recording that plainly is either dropping the close
    from memory or picking a determination on the analyst's behalf. Recorded, it
    reads as what it is — closed, no reason stated — and becomes an inconclusive
    Verdict rather than a claim nobody made.
    """

    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    UNABLE_TO_RESOLVE = "unable_to_resolve"
    UNSPECIFIED = "unspecified"


class ClosedByKind(str, Enum):
    """The Trust word a reader shows, and the kind a noise mark stores.

    A closure does not store this word. It stores how the caller authenticated,
    and Trust is derived from that: ``analyst`` only for a person on the
    console cookie. Noise marking still writes the word itself; that path has
    no session to derive from.
    """

    ANALYST = "analyst"
    AGENT = "agent"


# ``close_channel`` values. Trust is derived; the client cannot send either.
CHANNEL_COOKIE = "cookie"
CHANNEL_AGENT = "agent"


def trust_word(channel: str | None) -> str:
    """``analyst`` only when the close was a person's console cookie."""
    if channel == CHANNEL_COOKIE:
        return ClosedByKind.ANALYST.value
    return ClosedByKind.AGENT.value


__all__: Sequence[str] = (
    "CHANNEL_AGENT",
    "CHANNEL_COOKIE",
    "ClosedByKind",
    "ClosureCategory",
    "trust_word",
)
