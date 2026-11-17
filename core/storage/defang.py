"""The one Python rule for undoing defanged indicators, and for trimming their edges.

Mirrors ``services/agent/plugins/hunt/entities.ts``. Lives in storage so both
the shared-IOC index and memory's Entity Keys normalise by it: the tier
contract forbids storage from importing memory, and two copies would drift.
"""

from __future__ import annotations

import re

# Threat intel writes addresses defanged, and threat_intel is a worker whose
# output feeds this, so normalising first is cheaper than carrying defanged
# variants of every key.
_DEFANG: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[\.\]|\(\.\)|\{\.\}"), "."),
    # ASCII word boundary, as in JS: `éhxxp` un-defangs in the harness, so here too.
    (re.compile(r"\bh(?:xx)p", re.IGNORECASE | re.ASCII), "http"),
    (re.compile(r"\[:\]"), ":"),
    (re.compile(r"\[at\]", re.IGNORECASE), "@"),
)


def defang(text: str) -> str:
    for pattern, replacement in _DEFANG:
        text = pattern.sub(replacement, text)
    return text


# Every character either half trims at a value's edge: Python's `str.strip` set (the
# C0 separators U+001C-U+001F and NEL included) plus U+FEFF, which JS `trim` also takes.
_EDGE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    + "".join(map(chr, range(0x2000, 0x200B)))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def trim_edges(text: str) -> str:
    """The one edge trim for an indicator; ``trimEdges`` in entities.ts is its twin."""
    return text.strip(_EDGE)
