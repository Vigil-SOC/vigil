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
    """Which kind of actor closed it, asked at the write.

    Not derived from ``closed_by`` afterwards: an agent closing as
    "soc-automation" and a person closing as "nestor" are the same shape of
    string, and a lookup against users would grade a departed analyst's close as
    an agent's. The HTTP close has a person behind it and the MCP tool has a
    program holding someone's credential, so each writer already knows the
    answer.
    """

    ANALYST = "analyst"
    AGENT = "agent"


__all__: Sequence[str] = ("ClosedByKind", "ClosureCategory")
