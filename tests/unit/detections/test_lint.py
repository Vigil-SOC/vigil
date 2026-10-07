"""Sigma environment-literal lint (#826)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.detections.lint import lint_sigma
from core.detections.tools import SecurityDetectionsTools

pytestmark = pytest.mark.unit

HOST_IP_USER_RULE = """
title: Host-keyed successful logon
description: >
  Mentions 203.0.113.50 and helpdesk.internal and mjones only in prose.
falsepositives:
  - Traffic to 198.51.100.10 on ws01.lan
logsource:
  product: windows
  service: security
detection:
  selection:
    DestinationIp:
      - 10.1.2.3
      - 10.1.0.0/16
    ComputerName: dc01.corp.local
    TargetUserName: jsmith
  condition: selection
"""

BEHAVIOURAL_RULE = """
title: Encoded PowerShell command line
logsource:
  product: windows
  category: process_creation
detection:
  selection:
    Image|endswith:
      - '\\powershell.exe'
      - '\\pwsh.exe'
    CommandLine|contains:
      - '-EncodedCommand'
      - '-enc '
    User: SYSTEM
    DestinationIp: 127.0.0.1
  filter:
    TargetUserName:
      - Administrator
      - root
      - krbtgt
      - '*'
    DestinationIp: 255.255.255.255
  condition: selection and not filter
"""


def _values(result: dict) -> set[str]:
    return {item["value"] for item in result.get("findings") or []}


def test_host_ip_user_keyed_rule_is_rejected_with_guidance():
    result = lint_sigma(rule_yaml=HOST_IP_USER_RULE)
    assert result["passed"] is False
    kinds = {item["kind"] for item in result["findings"]}
    assert {"ip", "cidr", "hostname", "user"} <= kinds
    assert "10.1.2.3" in _values(result)
    assert "10.1.0.0/16" in _values(result)
    assert "dc01.corp.local" in _values(result)
    assert "jsmith" in _values(result)
    guidance = result["rewrite_guidance"] or ""
    assert "10.1.2.3" in guidance
    assert "behaviour" in guidance.lower()


def test_literals_in_title_description_and_falsepositives_are_ignored():
    result = lint_sigma(rule_yaml=HOST_IP_USER_RULE)
    values = _values(result)
    assert "203.0.113.50" not in values
    assert "helpdesk.internal" not in values
    assert "mjones" not in values
    assert "198.51.100.10" not in values
    assert "ws01.lan" not in values


def test_behavioural_process_cmdline_rule_passes():
    result = lint_sigma(rule_yaml=BEHAVIOURAL_RULE)
    assert result["passed"] is True
    assert result["findings"] == []
    assert result["rewrite_guidance"] is None


def test_environment_tld_suffix_and_glob_are_rejected():
    rule = """
title: Domain-suffixed workstation
logsource:
  product: windows
detection:
  selection:
    ComputerName|endswith: '.corp.local'
    HostName: '*.local'
  condition: selection
"""
    result = lint_sigma(rule_yaml=rule)
    assert result["passed"] is False
    assert {item["kind"] for item in result["findings"]} == {"hostname"}
    assert result["rewrite_guidance"]


@pytest.mark.asyncio
async def test_tool_lints_yaml_string_and_source_path(tmp_path: Path):
    tools = SecurityDetectionsTools()

    rejected = await tools.lint_detections(rule_yaml=HOST_IP_USER_RULE)
    assert rejected["passed"] is False
    assert rejected["rewrite_guidance"]

    passed = await tools.lint_detections(rule_yaml=BEHAVIOURAL_RULE)
    assert passed["passed"] is True

    source = tmp_path / "sigma-rules"
    source.mkdir()
    (source / "bad.yml").write_text(HOST_IP_USER_RULE)
    (source / "good.yml").write_text(BEHAVIOURAL_RULE)
    (source / "notes.yaml").write_text(HOST_IP_USER_RULE)
    (source / "readme.txt").write_text("10.1.2.3 dc01.corp.local jsmith")

    walked = await tools.lint_detections(source_path=str(source))
    assert walked["rules_scanned"] == 2
    assert walked["rules_rejected"] == 1
    assert walked["passed"] is False
    assert walked["results"][0]["file"].endswith("bad.yml")


@pytest.mark.asyncio
async def test_backend_tool_dispatches_lint_detections():
    from core.agents.tool_registry import execute_backend_tool
    from core.llm.tool_schemas import ALL_TOOLS

    assert any(tool["name"] == "lint_detections" for tool in ALL_TOOLS)

    result, handled = await execute_backend_tool(
        "lint_detections", {"rule_yaml": HOST_IP_USER_RULE}
    )
    assert handled is True
    assert result["passed"] is False
    assert result["rewrite_guidance"]

    result, handled = await execute_backend_tool(
        "lint_detections", {"rule_yaml": BEHAVIOURAL_RULE}
    )
    assert handled is True
    assert result["passed"] is True
