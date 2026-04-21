"""Unit tests for SkillService — the Skill Builder service (Issue #82).

These tests focus on the pure logic: ID generation, response parsing, and
the clarification-detection heuristic. Persistence is covered via the API
test (tests/test_skills_api.py) with a mocked service.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load the service module directly so we don't pull in database.connection's
# heavy model graph during unit tests.
_spec = importlib.util.spec_from_file_location(
    "skill_service_under_test",
    _REPO_ROOT / "services" / "skill_service.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["skill_service_under_test"] = _mod
_spec.loader.exec_module(_mod)
SkillService = _mod.SkillService


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

SKILL_ID_RE = re.compile(r"^s-\d{8}-[A-F0-9]{8}$")


@pytest.mark.unit
def test_skill_id_format():
    from database.models import Skill

    for _ in range(5):
        assert SKILL_ID_RE.match(Skill.generate_skill_id())


# ---------------------------------------------------------------------------
# _is_asking_questions
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        "I have some questions:\n1. Which data source?\n2. What time window?",
        "Could you clarify which SIEM you use?",
        "I need more information about your environment.",
    ],
)
def test_is_asking_questions_detects_clarifications(response):
    svc = SkillService()
    assert svc._is_asking_questions(response) is True


@pytest.mark.unit
def test_is_asking_questions_ignores_json_even_if_question_words_present():
    svc = SkillService()
    response = 'I have some questions: resolved.\n```json\n{"required_tools": []}\n```'
    # JSON presence overrides the indicator — do NOT treat this as clarification.
    assert svc._is_asking_questions(response) is False


@pytest.mark.unit
def test_is_asking_questions_false_on_plain_prose():
    svc = SkillService()
    assert svc._is_asking_questions("Here is the design you requested.") is False


# ---------------------------------------------------------------------------
# _parse_claude_response
# ---------------------------------------------------------------------------

VALID_SKILL_JSON = """
Sure — here's the skill:

```json
{
  "name": "Detect Lateral RDP",
  "description": "Spots unusual RDP sessions in the last 24h.",
  "category": "detection",
  "input_schema": {"type": "object", "properties": {"hours": {"type": "integer"}}},
  "output_schema": {"type": "object"},
  "required_tools": ["splunk.search"],
  "prompt_template": "Investigate RDP in last {{hours}}h.",
  "execution_steps": [
    {"step_id": "1", "type": "mcp_tool_call", "tool": "splunk.search"}
  ]
}
```
"""


@pytest.mark.unit
def test_parse_claude_response_valid_fenced():
    svc = SkillService()
    parsed = svc._parse_claude_response(VALID_SKILL_JSON)
    assert parsed is not None
    assert parsed["name"] == "Detect Lateral RDP"
    assert parsed["category"] == "detection"
    assert parsed["required_tools"] == ["splunk.search"]
    assert parsed["prompt_template"].startswith("Investigate")
    # Optional fields get sensible defaults
    assert parsed["execution_steps"][0]["step_id"] == "1"


@pytest.mark.unit
def test_parse_claude_response_rejects_missing_required():
    svc = SkillService()
    # No required_tools / prompt_template
    bad = """```json
    {"name": "x", "category": "custom"}
    ```"""
    assert svc._parse_claude_response(bad) is None


@pytest.mark.unit
def test_parse_claude_response_handles_malformed_json():
    svc = SkillService()
    bad = "```json\n{not valid json\n```"
    assert svc._parse_claude_response(bad) is None


@pytest.mark.unit
def test_parse_claude_response_unfenced_object():
    svc = SkillService()
    unfenced = (
        'Here you go: {"name": "X", "category": "custom", '
        '"required_tools": [], "prompt_template": "do X"}'
    )
    parsed = svc._parse_claude_response(unfenced)
    assert parsed is not None
    assert parsed["name"] == "X"


# ---------------------------------------------------------------------------
# Prompt construction sanity checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_system_prompt_includes_required_sections():
    svc = SkillService()
    prompt = svc._system_prompt()
    assert "AVAILABLE MCP TOOLS" in prompt
    assert "MITRE ATT&CK TACTICS" in prompt
    assert "EXISTING SKILLS" in prompt
    assert "OUTPUT CONTRACT" in prompt
    assert "CLARIFICATION RULE" in prompt
    assert "Lateral Movement" in prompt  # MITRE tactic name
