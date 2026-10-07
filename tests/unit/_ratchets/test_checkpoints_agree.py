# The hunt engine owns the checkpoint classes and their defaults; Python mirrors
# them so the reader can show where a run may pause. Held to it here, and the
# leads the reader names are held to the arches that grant them.

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.workflows.hunt_preflight import LEAD_TOOLS
from core.workflows.playbook_resolver import CHECKPOINT_CLASSES, DEFAULT_CHECKPOINTS
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
CHECKPOINTS_TS = ROOT / "services/agent/workflows/hunt/checkpoints.ts"
ARCH = ROOT / "services/agent/arch"


def _ts_classes() -> list[str]:
    body = re.search(
        r"CHECKPOINT_CLASSES = \[(.*?)\] as const", CHECKPOINTS_TS.read_text(), re.S
    )
    assert body is not None, "checkpoints.ts no longer declares CHECKPOINT_CLASSES"
    return re.findall(r'"([a-z_]+)"', body.group(1))


def _ts_defaults() -> dict[str, str]:
    body = re.search(
        r"DEFAULT_CHECKPOINTS: Checkpoints = \{(.*?)\};",
        CHECKPOINTS_TS.read_text(),
        re.S,
    )
    assert body is not None, "checkpoints.ts no longer declares DEFAULT_CHECKPOINTS"
    return dict(re.findall(r'([a-z_]+): "([a-z]+)"', body.group(1)))


def test_the_class_lists_agree():
    assert list(CHECKPOINT_CLASSES) == _ts_classes()


def test_the_defaults_agree():
    assert DEFAULT_CHECKPOINTS == _ts_defaults()


# Every class must have a default, or the reader would show a hole.
def test_every_class_has_a_default():
    assert set(DEFAULT_CHECKPOINTS) == set(CHECKPOINT_CLASSES)


@pytest.mark.parametrize(
    ("kind", "arch"),
    [
        ("hunt", "threathunt.yaml"),
        ("adjudicate", "adjudicate.yaml"),
        ("root_cause", "rootcause.yaml"),
    ],
)
def test_the_lead_the_reader_names_holds_what_the_arch_grants(kind, arch):
    lead = yaml.safe_load((ARCH / arch).read_text())["roles"]["lead"]

    assert set(LEAD_TOOLS[kind]) == set(lead.get("tools") or []) | set(
        lead.get("needs") or []
    )


def test_the_investigate_lead_is_the_arch_lead():
    from core.workflows.playbook_resolver import INVESTIGATE_TOOLS

    lead = yaml.safe_load((ARCH / "investigate.yaml").read_text())["roles"]["lead"]

    assert set(INVESTIGATE_TOOLS) == set(lead["tools"])


# The reader shows the roster as the helpers; a name the arch lacks would read as
# a worker the lead can ask for and never gets.
@pytest.mark.parametrize(
    ("workflow_id", "arch"),
    [("threat-hunt", "threathunt.yaml"), ("shadow-adjudication", "adjudicate.yaml")],
)
def test_the_rostered_helpers_are_arch_workers(workflow_id, arch):
    workers = yaml.safe_load((ARCH / arch).read_text())["roles"]["workers"]
    phases = WorkflowsService().get_workflow(workflow_id).phases

    assert {p["agent"] for p in phases} <= set(workers)
