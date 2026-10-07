"""What starts each workflow. The one place the ids are written down.

The daemon imports these names instead of its own literals, and the catalog
derives each row's ``triggers`` from them, so the listing cannot drift from the
code that actually starts a run.
"""

from typing import List

# Where triage lands when nothing else fits, or when the routed workflow is off.
FALLBACK_WORKFLOW = "incident-response"
# Everything ``select_workflow`` can return for a finding.
ROUTED_WORKFLOWS = frozenset(
    {FALLBACK_WORKFLOW, "forensic-analysis", "full-investigation"}
)
# The scheduler's hunt. The threat-feed poller queues the same workflow for
# uncovered indicators, so it shows ``schedule`` rather than a trigger of its own.
SCHEDULED_WORKFLOW = "threat-hunt"
# Run beside every admitted finding, executing nothing.
SHADOW_WORKFLOW_ID = "shadow-adjudication"


def triggers_for(workflow_id: str) -> List[str]:
    """What starts this workflow; empty means only a person does."""
    triggers = []
    if workflow_id in ROUTED_WORKFLOWS:
        triggers.append("alerts")
    if workflow_id == SCHEDULED_WORKFLOW:
        triggers.append("schedule")
    if workflow_id == SHADOW_WORKFLOW_ID:
        triggers.append("shadow")
    return triggers


def can_disable(workflow_id: str) -> bool:
    return workflow_id != FALLBACK_WORKFLOW
