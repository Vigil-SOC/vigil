#!/usr/bin/env python3
"""
Test backend tool integration with Claude function calling.

`test_tool_availability` is an offline unit test (no API key required).
`test_security_detections` and `test_backend_integration` exercise live
Claude tool-calling and are marked `external_service` so they run only in
the dedicated CI lane; they skip when no API key is configured.
"""
import sys
from pathlib import Path

import pytest

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.claude_service import ClaudeService


@pytest.mark.external_service
def test_security_detections():
    """Smoke-test security-detection tool calls end to end (needs a live API key)."""
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False,
    )

    if not claude.has_api_key():
        pytest.skip("No API key configured (set ANTHROPIC_API_KEY or CLAUDE_API_KEY)")

    # Analyze coverage for PowerShell techniques
    response = claude.chat(
        message=(
            "What's our detection coverage for PowerShell-related MITRE "
            "techniques T1059.001 and T1059.003?"
        ),
        max_tokens=2048,
    )
    assert response, "Expected a non-empty response for coverage analysis"

    # Search detections
    response = claude.chat(
        message=(
            "Search our detection rules for anything related to 'mimikatz' "
            "credential dumping. Show me the top 5 results."
        ),
        max_tokens=2048,
    )
    assert response, "Expected a non-empty response for detection search"

    # Get detection counts
    response = claude.chat(
        message=(
            "How many detection rules do we have in total? Break it down by "
            "source format (Sigma, Splunk, Elastic, KQL)."
        ),
        max_tokens=2048,
    )
    assert response, "Expected a non-empty response for detection counts"


@pytest.mark.external_service
def test_backend_integration():
    """Smoke-test a multi-tool backend query end to end (needs a live API key)."""
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
        enable_thinking=False,
    )

    if not claude.has_api_key():
        pytest.skip("No API key configured (set ANTHROPIC_API_KEY or CLAUDE_API_KEY)")

    # Complex query that may use multiple tools
    response = claude.chat(
        message=(
            "I need to understand our detection gaps for ransomware attacks. "
            "Can you analyze our coverage and identify what techniques we're "
            "missing detections for? Focus on the most critical ransomware "
            "techniques."
        ),
        max_tokens=4096,
    )
    assert response, "Expected a non-empty response for the gap-analysis query"


def test_tool_availability():
    """Backend tools load into ClaudeService (offline; no API key required)."""
    claude = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=False,
    )

    assert claude.use_backend_tools
    assert len(claude.backend_tools) > 0
    # Every loaded tool must be a usable definition.
    for tool in claude.backend_tools:
        assert tool.get("name")
        assert tool.get("description")
