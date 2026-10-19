"""Every built-in that can list findings can also search them (#2311)."""

from __future__ import annotations

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.llm.chat_layers import granted_ids

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "agent",
    [a for a in BUILTIN_AGENTS if "list_findings" in a["recommended_tools"]],
    ids=lambda a: a["id"],
)
def test_list_findings_grant_comes_with_search_findings(agent):
    assert "search_findings" in granted_ids(agent["recommended_tools"])
