"""The Vigil case a hunt's handoff is filed on, from either side of the bridge.

The hunt names each handoff with a key it mints itself (``case-xxxxxxxx``); it holds
no case table, so it cannot know where the escalation lands. The bridge files it on
the case the hunt was started from, or opens one named for the handoff. Anything
that shows a reader the hunt's handoffs has to say the same case the bridge used,
or it names a case that does not exist.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional


# Named for the handoff rather than for the moment it arrived. Derived rather than
# looked up because there is nothing to look it up by: create_case mints its own id,
# so a handoff arriving twice -- pushed the moment the hunt journals it, then again
# on the terminal that re-carries it -- would open two cases with nothing able to
# tell they are the same escalation. No date in the key, so two arrivals either side
# of midnight still land on one case.
#
# 16 hex characters, not 8. A collision here does not merely reuse an id: the second
# escalation finds the first one's case already open and returns, so its case file is
# discarded with nothing said. 32 bits makes that a coin flip at seventy-odd thousand
# escalations; 64 makes it unreachable, and cases.case_id is String(50) against the
# 29 this spends, so the width is free.
def handoff_case_id(source_run_id: str, handoff_key: str) -> str:
    digest = hashlib.sha256(f"{source_run_id}:{handoff_key}".encode()).hexdigest()
    return f"case-handoff-{digest[:16]}"


def filed_case(source_run_id: str, handoff_key: str, origin: Optional[str]) -> str:
    """The case a handoff is filed on: the hunt's own case, else the one it opens."""
    return origin or handoff_case_id(source_run_id, handoff_key)


def stamp_handoff_cases(
    folded: Optional[Dict[str, Any]], run_id: str, origin: Optional[str]
) -> None:
    """Point a hunt projection's handoffs at the cases they were filed on.

    The hunt's own key stays beside it as ``handoff_key``: it is what the hunt's
    report and ledger say.
    """
    if not isinstance(folded, dict):
        return
    for item in folded.get("handoffs") or []:
        key = item.get("case_id") if isinstance(item, dict) else None
        if isinstance(key, str) and key:
            item["handoff_key"] = key
            item["case_id"] = filed_case(run_id, key, origin)
