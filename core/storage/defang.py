"""The one Python rule for undoing defanged indicators.

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
    (re.compile(r"\bh(?:xx)p", re.IGNORECASE), "http"),
    (re.compile(r"\[:\]"), ":"),
    (re.compile(r"\[at\]", re.IGNORECASE), "@"),
)


def defang(text: str) -> str:
    for pattern, replacement in _DEFANG:
        text = pattern.sub(replacement, text)
    return text
