"""Tests for the Board Brief report type (Issue #8).

Tests cover:
1. Reporter agent delegates report writing to the executive-summary skill (#929)
2. The skill body carries the report-type methodology and loads from the library
3. Board brief template exists as the skill's asset and has required sections
"""

import json

from core.skills.skill_library import LIBRARY_ROOT, load_skills, read_skill

# ---------------------------------------------------------------------------
# Tests — Agent configuration
# ---------------------------------------------------------------------------


class TestReporterAgentConfig:
    """The reporter delegates report writing to the executive-summary skill (#929)."""

    def test_reporter_agent_exists(self):
        """Reporter agent must exist in AGENT_CONFIGS."""
        from core.agents.builtins import BUILTIN_AGENTS

        assert any(r["id"] == "reporter" for r in BUILTIN_AGENTS)

    def test_reporter_methodology_points_at_the_skill_not_the_procedure(self):
        """The procedure lives once, in the skill; the profile only refers to it."""
        from core.agents.builtins import BUILTIN_AGENTS

        reporter = {r["id"]: r for r in BUILTIN_AGENTS}["reporter"]
        assert "read_skill" in reporter["recommended_tools"]
        methodology = reporter["methodology"]
        assert 'read_skill("executive-summary")' in methodology
        for ported in (
            "BOARD BRIEF",
            "30/60/90",
            "Key Metrics",
            "templates/board-brief",
        ):
            assert ported not in methodology

    def test_reporter_base_prompt_renders_with_the_skill_listed(self):
        from core.agents.builtins import BUILTIN_AGENTS
        from core.agents.prompts import prompt_for_row

        prompt = prompt_for_row({r["id"]: r for r in BUILTIN_AGENTS}["reporter"])
        assert "<available_skills>" in prompt
        assert "- executive-summary:" in prompt

    def test_reporter_description_updated(self):
        """Reporter description should mention executive briefs."""
        from core.agents.builtins import BUILTIN_AGENTS

        desc = {r["id"]: r for r in BUILTIN_AGENTS}["reporter"]["description"].lower()
        assert "executive briefs" in desc


# ---------------------------------------------------------------------------
# Tests — executive-summary skill body (the ported methodology)
# ---------------------------------------------------------------------------

SKILL_DIR = LIBRARY_ROOT / "executive-summary"
TEMPLATE_PATH = SKILL_DIR / "assets" / "board-brief.md"


def _skill_body():
    return read_skill("executive-summary", roots=[LIBRARY_ROOT])["content"]


class TestExecutiveSummarySkill:
    """The report-type methodology moved from the reporter profile into the skill."""

    def test_skill_loads_from_the_library_and_serves_its_asset(self):
        skills = {s.name: s for s in load_skills([LIBRARY_ROOT])}
        assert skills["executive-summary"].path == SKILL_DIR
        asset = read_skill(
            "executive-summary", "assets/board-brief.md", roots=[LIBRARY_ROOT]
        )
        assert asset["file"] == "assets/board-brief.md"
        assert asset["content"] == TEMPLATE_PATH.read_text()
        assert "## Risk posture: {{POSTURE_INDICATOR}}" in asset["content"]

    def test_skill_methodology_includes_board_brief(self):
        body = _skill_body()
        assert "BOARD BRIEF" in body
        assert 'read_skill("executive-summary", "assets/board-brief.md")' in body
        assert "core/agents/templates" not in body

    def test_skill_methodology_mentions_risk_posture(self):
        body = _skill_body()
        assert "RED" in body
        assert "YELLOW" in body
        assert "GREEN" in body

    def test_skill_methodology_mentions_key_metrics(self):
        body = _skill_body().lower()
        assert "kill chain" in body
        assert "detection coverage" in body
        assert "remediation" in body
        assert "open critical" in body

    def test_skill_methodology_mentions_trend_and_no_cve(self):
        body = _skill_body()
        assert "30/60/90" in body
        assert "no cve" in body.lower()

    def test_skill_carries_the_report_types(self):
        body = _skill_body()
        for section in (
            "TECHNICAL REPORT",
            "EXECUTIVE SUMMARY",
            "Timeline",
            "Recommendations",
        ):
            assert section in body

    def test_eval_cases_follow_the_epic_schema(self):
        cases = json.loads((SKILL_DIR / "evals" / "cases.json").read_text())
        assert len(cases) >= 3
        for case in cases:
            assert set(case) == {"name", "input", "expect"}
            assert case["name"] and case["input"]
            assert isinstance(case["expect"], list) and case["expect"]
            assert all(isinstance(s, str) and s for s in case["expect"])


# ---------------------------------------------------------------------------
# Tests — Board brief template (now the skill's asset)
# ---------------------------------------------------------------------------


class TestBoardBriefTemplate:
    """Verify the board brief template exists and has required sections."""

    TEMPLATE_PATH = TEMPLATE_PATH

    def test_template_file_exists(self):
        """Template lives under the skill's assets/ directory."""
        assert (
            self.TEMPLATE_PATH.exists()
        ), f"Template not found at {self.TEMPLATE_PATH}"

    def test_template_has_risk_posture_section(self):
        """Template must contain a risk posture section."""
        content = self.TEMPLATE_PATH.read_text()
        assert "risk posture" in content.lower()
        assert "POSTURE_INDICATOR" in content

    def test_template_has_key_metrics_section(self):
        """Template must contain a key metrics table."""
        content = self.TEMPLATE_PATH.read_text()
        assert "Key metrics" in content
        assert "kill chain" in content.lower()
        assert "Detection coverage" in content or "detection_coverage" in content
        assert "remediation" in content.lower()

    def test_template_has_action_items_section(self):
        """Template must contain top 3 action items."""
        content = self.TEMPLATE_PATH.read_text()
        assert "Top 3 action items" in content
        assert "budget" in content.lower()
        assert "policy" in content.lower()
        assert "technical" in content.lower()

    def test_template_has_trend_section(self):
        """Template must contain a 30/60/90 day trend section."""
        content = self.TEMPLATE_PATH.read_text()
        assert "Trend" in content
        assert "30" in content and "60" in content and "90" in content

    def test_template_has_no_cve_instruction(self):
        """Template comments must instruct against CVE numbers in main body."""
        content = self.TEMPLATE_PATH.read_text().lower()
        assert "no cve" in content or "no cve numbers" in content
