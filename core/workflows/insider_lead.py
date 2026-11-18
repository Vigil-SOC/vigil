"""An insider lead: a finding that names an account, as a hunt on that account's
data movement (#4745).

The proposal is an execute body: one falsifiable hypothesis per ``user`` Entity
Key on the finding, that key its subject, on a restricted case. Nothing here
starts a hunt or asks the identity provider anything: the hunt does that.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.memory.entity_keys import finding_entity_keys
from core.memory.hunt_coverage import EXECUTE_PATH, build_proposal

NO_ACCOUNT = "names no account, so it is not an insider lead"


class LeadRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _claim(key: str, finding_id: str) -> str:
    # One line, whatever the key holds: subjects are keyed by the exact statement.
    return " ".join(
        f"Account {key} moved data beyond its own usual volume or destinations "
        f"(bulk download, external share, personal cloud or removable media) "
        f"in the days around finding {finding_id}".split()
    )


def account_keys(finding: Dict[str, Any]) -> List[str]:
    """The finding's ``user`` Entity Keys, in the order it names them."""
    return [key for key in finding_entity_keys([finding]) if key.startswith("user:")]


def lead_proposal(finding_id: str) -> Dict[str, Any]:
    """The execute body for ``finding_id``; ``LeadRefused`` when there is none."""
    from core.storage.database_data_service import get_database_data_service

    finding = get_database_data_service().get_finding(finding_id)
    if not finding:
        raise LeadRefused(404, f"Finding {finding_id} not found")
    keys = account_keys(finding)
    if not keys:
        raise LeadRefused(400, f"Finding {finding_id} {NO_ACCOUNT}")
    hypotheses: List[str] = []
    subjects: Dict[str, List[str]] = {}
    for key in keys:
        one = build_proposal([key], [], claim=_claim(key, finding_id))
        hypotheses.append(one["hypothesis"])
        subjects.update(one["hypothesis_subjects"])
    return {
        "execute_path": EXECUTE_PATH,
        "accounts": keys,
        "proposal": {
            "finding_id": finding_id,
            # execute_workflow reads each line as its own hypothesis.
            "hypothesis": "\n".join(hypotheses),
            "hypothesis_subjects": subjects,
            "approve_hypotheses": True,
            "restricted": True,
        },
    }
