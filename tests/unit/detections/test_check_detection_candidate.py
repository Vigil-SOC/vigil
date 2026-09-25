"""Lint and replay a candidate Sigma rule (#1203)."""

from __future__ import annotations

import pytest

from core.agents.tool_registry import execute_backend_tool
from core.detections.reconstruction import steps_from_dispatch_results
from core.detections.tools import SecurityDetectionsTools
from core.llm.tool_schemas import ALL_TOOLS
from tests.unit.detections.test_lint import HOST_IP_USER_RULE

pytestmark = pytest.mark.unit

CANDIDATE_RULE = """
title: Whoami spawned from cmd
logsource:
  product: windows
  category: process_creation
detection:
  selection:
    Image: C:\\Windows\\System32\\cmd.exe
    CommandLine|contains: whoami
  condition: selection
"""

RE_RULE = """
title: Regex command line
logsource:
  product: windows
detection:
  selection:
    CommandLine|re: whoami
  condition: selection
"""

LIST_RULE = """
title: Either image
logsource:
  product: windows
detection:
  selection:
    Image:
      - C:\\Windows\\System32\\cmd.exe
      - C:\\Windows\\System32\\powershell.exe
  condition: selection
"""

MAPS_RULE = """
title: Either image as maps
logsource:
  product: windows
detection:
  selection:
    - Image: C:\\Windows\\System32\\cmd.exe
    - Image: C:\\Windows\\System32\\powershell.exe
  condition: selection
"""

ALL_RULE = """
title: All contains
logsource:
  product: windows
detection:
  selection:
    CommandLine|contains|all:
      - whoami
      - /c
  condition: selection
"""

BOOL_RULE = """
title: Initiated connection
logsource:
  product: windows
detection:
  selection:
    Initiated: true
  condition: selection
"""

FILTER_RULE = """
title: Selection and filter
logsource:
  product: windows
detection:
  selection:
    Image: C:\\Windows\\System32\\cmd.exe
  filter:
    User: SYSTEM
  condition: selection and not filter
"""

STEP = {
    "technique_id": "T1059.003",
    "hostname": "ws01",
    "started_at": "2026-09-25T18:00:00Z",
    "ended_at": "2026-09-25T18:05:00Z",
}
MATCHING_EVENT = {
    "Image": r"C:\Windows\System32\cmd.exe",
    "CommandLine": "cmd.exe /c whoami",
}


def _keys(result: dict) -> set[str]:
    return set(result)


@pytest.mark.asyncio
async def test_matching_event_returns_a_candidate_that_is_not_a_step():
    tools = SecurityDetectionsTools()
    result = await tools.check_detection_candidate(
        rule_yaml=CANDIDATE_RULE,
        events=[MATCHING_EVENT],
        **STEP,
    )

    assert _keys(result) == {"lint", "replay", "candidate"}
    assert result["lint"]["passed"] is True
    assert result["replay"]["evaluated"] is True
    assert result["replay"]["matched"] is True
    assert result["candidate"] == {**STEP, "rule_yaml": CANDIDATE_RULE}
    assert steps_from_dispatch_results(result) == []

    missed = await tools.check_detection_candidate(
        rule_yaml=CANDIDATE_RULE,
        events=[{"Image": r"C:\Windows\System32\cmd.exe", "CommandLine": "cmd.exe /c ipconfig"}],
        **STEP,
    )
    assert missed["replay"]["evaluated"] is True
    assert missed["replay"]["matched"] is False
    assert missed["candidate"] is None
    assert missed["replay"]["reason"]


@pytest.mark.asyncio
async def test_environment_keyed_rule_has_no_candidate():
    result = await SecurityDetectionsTools().check_detection_candidate(
        rule_yaml=HOST_IP_USER_RULE,
        events=[{"DestinationIp": "10.1.2.3", "ComputerName": "dc01.corp.local", "TargetUserName": "jsmith"}],
        **STEP,
    )
    assert result["lint"]["passed"] is False
    assert result["candidate"] is None
    assert result["lint"]["findings"]
    assert steps_from_dispatch_results(result) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [RE_RULE, LIST_RULE, MAPS_RULE, ALL_RULE, FILTER_RULE])
async def test_unsupported_sigma_is_not_a_match(rule: str):
    result = await SecurityDetectionsTools().check_detection_candidate(
        rule_yaml=rule,
        events=[MATCHING_EVENT],
        **STEP,
    )
    assert result["replay"]["evaluated"] is False
    assert result["replay"]["matched"] is False
    assert result["candidate"] is None
    assert result["replay"]["reason"]


@pytest.mark.asyncio
async def test_bool_does_not_match_a_number():
    result = await SecurityDetectionsTools().check_detection_candidate(
        rule_yaml=BOOL_RULE,
        events=[{"Initiated": 1}],
        **STEP,
    )
    assert result["replay"]["evaluated"] is True
    assert result["replay"]["matched"] is False
    assert result["candidate"] is None

    matched = await SecurityDetectionsTools().check_detection_candidate(
        rule_yaml=BOOL_RULE,
        events=[{"Initiated": True}],
        **STEP,
    )
    assert matched["candidate"]["technique_id"] == "T1059.003"


@pytest.mark.asyncio
async def test_backend_tool_dispatches_check_detection_candidate():
    assert any(tool["name"] == "check_detection_candidate" for tool in ALL_TOOLS)
    result, handled = await execute_backend_tool(
        "check_detection_candidate",
        {"rule_yaml": CANDIDATE_RULE, "events": [MATCHING_EVENT], **STEP},
    )
    assert handled is True
    assert result["candidate"]["technique_id"] == "T1059.003"
    assert steps_from_dispatch_results(result) == []
