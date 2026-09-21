"""The `ioc-enrichment` skill carries the procedure the threat_intel profile
used to hold (epic #882 decision 8, #930)."""

from __future__ import annotations

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.prompts import prompt_for_row
from core.skills.skill_library import LIBRARY_ROOT, read_skill

pytestmark = pytest.mark.unit

THREAT_INTEL = {r["id"]: r for r in BUILTIN_AGENTS}["threat_intel"]


def test_read_skill_returns_the_bundled_procedure():
    result = read_skill("ioc-enrichment", roots=[LIBRARY_ROOT])
    body = result["content"]
    assert result["skill"] == "ioc-enrichment" and not body.startswith("---")
    for phrase in ("recall_entity", "cf_lookup_ip_threat", "lookup_indicators"):
        assert phrase in body
    assert "cloudforce_one" in body and "cloudy_summary" in body


def test_threat_intel_profile_no_longer_duplicates_the_procedure():
    assert "methodology" not in THREAT_INTEL
    assert "cloudforce_one" not in THREAT_INTEL["extra_principles"]
    prompt = prompt_for_row(THREAT_INTEL)
    assert "State confidence in attribution" in prompt
    assert "<methodology>" not in prompt
    # The grant is what puts the skill in the prompt's index.
    assert "read_skill" in THREAT_INTEL["recommended_tools"]
    assert "- ioc-enrichment:" in prompt
