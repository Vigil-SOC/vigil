"""What starts each workflow. The one place the ids are written down.

The daemon imports these names instead of its own literals, and the catalog
derives each row's ``triggers`` from them, so the listing cannot drift from the
code that actually starts a run.
"""

from typing import Dict, List, Optional

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


# Workflows nothing can turn off, each with the reason the console shows.
ALWAYS_ON: Dict[str, str] = {
    FALLBACK_WORKFLOW: "Alerts land here when nothing else fits, so it stays on.",
    SCHEDULED_WORKFLOW: "Hunts run on a schedule and on new threat intel, so it stays on.",
}


def can_disable(workflow_id: str) -> bool:
    return workflow_id not in ALWAYS_ON


def always_on_reason(workflow_id: str) -> Optional[str]:
    """Why this workflow cannot be turned off; None when it can."""
    return ALWAYS_ON.get(workflow_id)
