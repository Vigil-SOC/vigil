# A root-cause run is its own loop: it traces one confirmed finding back through
# causal steps, so it resolves without hypotheses and binds only the log search.

from __future__ import annotations

import pytest
import yaml

from core.workflows.playbook_resolver import (
    ROOT_CAUSE_CAPABILITIES,
    resolve_hunt,
    resolve_root_cause,
)
from core.workflows.playbooks_router import _resolver_for
from core.workflows.workflows_service import WorkflowsService, is_hunt_like

pytestmark = pytest.mark.unit


@pytest.fixture()
def resolved():
    playbook, config = resolve_root_cause("root-cause-analysis")
    return yaml.safe_load(playbook), yaml.safe_load(config)


def test_is_routed_to_its_own_resolver_and_is_not_hunt_like():
    workflows = WorkflowsService()
    assert _resolver_for(workflows, "root-cause-analysis") is resolve_root_cause
    assert _resolver_for(workflows, "threat-hunt") is resolve_hunt
    assert not is_hunt_like("root_cause")


# Only the layers the agent's plain loaders accept: a hunt's sections would be
# refused there, since the rca entry owns none of them.
def test_the_playbook_states_no_hypotheses_or_hunt_sections(resolved):
    playbook, _ = resolved
    assert set(playbook) <= {
        "name",
        "description",
        "use_case",
        "trigger_examples",
        "narrative",
    }
    assert playbook["narrative"]


def test_binds_only_the_log_search(resolved):
    _, config = resolved
    provided = {tool.get("provides") for tool in config["tools"]}
    assert provided <= set(ROOT_CAUSE_CAPABILITIES)
    assert "hypothesis_loop" not in config


# The gate is the trace's own class. It permits tracing one finding; there are no
# hypotheses to approve.
def test_carries_the_permit_policy_the_definition_declares(resolved):
    _, config = resolved
    assert config["checkpoints"] == {"rca_permit": "ask"}
