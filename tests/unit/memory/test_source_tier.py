# The domains a hunt records under, and the vendors an alert names, are graded rather than
# logged as unknown (#2287). A domain missing here grades CrowdStrike process evidence a feed.

from __future__ import annotations

import logging

import pytest

from core.memory.source_tier import (
    _HUNT_TIERS,
    InvestigationKind,
    SourceTier,
    resolve_source_tier,
)
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

HUNT_LIKE = ("threat-hunt", "shadow-adjudication")


@pytest.mark.parametrize("workflow_id", HUNT_LIKE)
def test_every_domain_a_hunt_declares_has_a_tier(workflow_id):
    definition = WorkflowsService().get_workflow(workflow_id)
    declared = list(definition.metadata.get("data_domains") or [])

    assert declared, f"{workflow_id} declares no data_domains"
    missing = [domain for domain in declared if domain not in _HUNT_TIERS]
    assert not missing, f"{workflow_id} records under untiered domains: {missing}"


@pytest.mark.parametrize("domain", ["proxy", "process_lineage", "dns", "endpoint"])
def test_the_demo_domains_grade_telemetry_without_logging_unknown(domain, caplog):
    with caplog.at_level(logging.WARNING, logger="core.memory.source_tier"):
        tier = resolve_source_tier(domain, InvestigationKind.HUNT)

    assert tier is SourceTier.TELEMETRY
    assert "unknown source" not in caplog.text


@pytest.mark.parametrize(
    "source",
    ["crowdstrike", "zscaler", "proofpoint", "darktrace", "infoblox", "splunk"],
)
def test_an_alert_vendor_grades_telemetry_without_logging_unknown(source, caplog):
    with caplog.at_level(logging.WARNING, logger="core.memory.source_tier"):
        tier = resolve_source_tier(source, InvestigationKind.CASE)

    assert tier is SourceTier.TELEMETRY
    assert "unknown source" not in caplog.text
