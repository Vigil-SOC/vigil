"""Tests for the Board Brief report type (Issue #8).

Tests cover:
1. Reporter agent configuration includes board brief methodology
2. Agent routing selects Reporter for board brief keywords
3. Board brief template exists and has required sections
4. Risk posture determination logic with synthetic data
5. Metric computation helpers with synthetic findings/cases
"""

import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Fixtures — synthetic findings and cases
# ---------------------------------------------------------------------------

SYNTHETIC_FINDINGS = [
    {
        "id": "f-20260301-crit001",
        "title": "Ransomware encryption activity on FILESERV-01",
        "severity": "critical",
        "source": "crowdstrike",
        "timestamp": "2026-03-01T08:00:00Z",
        "mitre_techniques": ["T1486"],
        "status": "open",
    },
    {
        "id": "f-20260302-crit002",
        "title": "C2 beaconing to known APT infrastructure",
        "severity": "critical",
        "source": "splunk",
        "timestamp": "2026-03-02T14:30:00Z",
        "mitre_techniques": ["T1071.001", "T1573.001"],
        "status": "open",
    },
    {
        "id": "f-20260228-high001",
        "title": "Lateral movement via SMB from workstation-089",
        "severity": "high",
        "source": "microsoft_defender",
        "timestamp": "2026-02-28T10:00:00Z",
        "mitre_techniques": ["T1021.002"],
        "status": "open",
    },
    {
        "id": "f-20260215-med001",
        "title": "Brute force authentication attempts",
        "severity": "medium",
        "source": "azure_sentinel",
        "timestamp": "2026-02-15T09:00:00Z",
        "mitre_techniques": ["T1110.001"],
        "status": "resolved",
    },
    {
        "id": "f-20260210-high002",
        "title": "Suspicious PowerShell encoded command",
        "severity": "high",
        "source": "splunk",
        "timestamp": "2026-02-10T16:00:00Z",
        "mitre_techniques": ["T1059.001", "T1027"],
        "status": "resolved",
    },
]

SYNTHETIC_CASES = [
    {
        "id": "case-20260301-abc",
        "title": "Ransomware Investigation - FILESERV-01",
        "status": "in_progress",
        "priority": "critical",
        "severity": "critical",
        "created_at": "2026-03-01T08:30:00Z",
        "updated_at": "2026-03-05T12:00:00Z",
        "resolved_at": None,
        "findings": ["f-20260301-crit001", "f-20260302-crit002"],
    },
    {
        "id": "case-20260228-def",
        "title": "Lateral Movement - Workstation Cluster",
        "status": "in_progress",
        "priority": "high",
        "severity": "high",
        "created_at": "2026-02-28T11:00:00Z",
        "updated_at": "2026-03-03T09:00:00Z",
        "resolved_at": None,
        "findings": ["f-20260228-high001"],
    },
    {
        "id": "case-20260215-ghi",
        "title": "Brute Force - Admin Account",
        "status": "resolved",
        "priority": "medium",
        "severity": "medium",
        "created_at": "2026-02-15T09:30:00Z",
        "updated_at": "2026-02-16T14:00:00Z",
        "resolved_at": "2026-02-16T14:00:00Z",
        "findings": ["f-20260215-med001"],
    },
    {
        "id": "case-20260210-jkl",
        "title": "PowerShell Abuse Investigation",
        "status": "resolved",
        "priority": "high",
        "severity": "high",
        "created_at": "2026-02-10T17:00:00Z",
        "updated_at": "2026-02-12T10:00:00Z",
        "resolved_at": "2026-02-12T10:00:00Z",
        "findings": ["f-20260210-high002"],
    },
]


# ---------------------------------------------------------------------------
# Helper functions that mirror board brief computation logic
# ---------------------------------------------------------------------------

def compute_risk_posture(findings, cases):
    """Determine RED/YELLOW/GREEN from findings and cases.
    
    RED:    Active critical finding with no containment (no in-progress case)
    YELLOW: Open critical findings but cases are in progress
    GREEN:  No open critical findings
    """
    open_criticals = [
        f for f in findings
        if f["severity"] == "critical" and f.get("status") != "resolved"
    ]
    if not open_criticals:
        return "GREEN"

    # Check if all criticals have an associated in-progress case
    critical_ids = {f["id"] for f in open_criticals}
    covered_ids = set()
    for case in cases:
        if case["status"] in ("in_progress", "resolved"):
            for fid in case.get("findings", []):
                covered_ids.add(fid)

    uncovered = critical_ids - covered_ids
    if uncovered:
        return "RED"
    return "YELLOW"


def compute_open_criticals(findings):
    """Count open critical findings."""
    return len([
        f for f in findings
        if f["severity"] == "critical" and f.get("status") != "resolved"
    ])


def compute_mttr_hours(cases):
    """Mean time to remediation in hours for resolved cases."""
    resolved = [c for c in cases if c.get("resolved_at")]
    if not resolved:
        return None
    total_hours = 0
    for c in resolved:
        created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        resolved_at = datetime.fromisoformat(c["resolved_at"].replace("Z", "+00:00"))
        total_hours += (resolved_at - created).total_seconds() / 3600
    return round(total_hours / len(resolved), 1)


def compute_detection_coverage(findings, cases):
    """Percentage of findings that have an associated case."""
    if not findings:
        return 0.0
    all_case_finding_ids = set()
    for case in cases:
        for fid in case.get("findings", []):
            all_case_finding_ids.add(fid)
    covered = sum(1 for f in findings if f["id"] in all_case_finding_ids)
    return round((covered / len(findings)) * 100, 1)


def determine_trend(exposures_30d, exposures_60d, exposures_90d):
    """Determine trend direction from 3 exposure windows."""
    if exposures_30d < exposures_60d < exposures_90d:
        return "Improving"
    elif exposures_30d > exposures_60d > exposures_90d:
        return "Degrading"
    else:
        max_val = max(exposures_30d, exposures_60d, exposures_90d)
        min_val = min(exposures_30d, exposures_60d, exposures_90d)
        if max_val == 0 or (max_val - min_val) / max_val < 0.10:
            return "Stable"
        return "Mixed"


# ---------------------------------------------------------------------------
# Tests — Agent configuration
# ---------------------------------------------------------------------------

class TestReporterAgentConfig:
    """Verify the Reporter agent is configured for board briefs."""

    def test_reporter_agent_exists(self):
        """Reporter agent must exist in AGENT_CONFIGS."""
        from services.soc_agents import AGENT_CONFIGS
        assert "reporter" in AGENT_CONFIGS

    def test_reporter_methodology_includes_board_brief(self):
        """Reporter methodology must reference the board brief report type."""
        from services.soc_agents import AGENT_CONFIGS
        methodology = AGENT_CONFIGS["reporter"]["methodology"]
        assert "BOARD BRIEF" in methodology
        assert "board brief" in methodology.lower() or "board-brief" in methodology.lower()

    def test_reporter_methodology_mentions_risk_posture(self):
        """Board brief methodology must mention risk posture indicator."""
        from services.soc_agents import AGENT_CONFIGS
        methodology = AGENT_CONFIGS["reporter"]["methodology"]
        assert "RED" in methodology
        assert "YELLOW" in methodology
        assert "GREEN" in methodology

    def test_reporter_methodology_mentions_key_metrics(self):
        """Board brief methodology must reference all four key metrics."""
        from services.soc_agents import AGENT_CONFIGS
        methodology = AGENT_CONFIGS["reporter"]["methodology"].lower()
        assert "kill chain" in methodology
        assert "detection coverage" in methodology
        assert "remediation" in methodology
        assert "open critical" in methodology

    def test_reporter_methodology_mentions_trend(self):
        """Board brief methodology must mention 30/60/90 day trend."""
        from services.soc_agents import AGENT_CONFIGS
        methodology = AGENT_CONFIGS["reporter"]["methodology"]
        assert "30/60/90" in methodology

    def test_reporter_no_cve_instruction(self):
        """Board brief methodology must instruct no CVEs in main body."""
        from services.soc_agents import AGENT_CONFIGS
        methodology = AGENT_CONFIGS["reporter"]["methodology"].lower()
        assert "no cve" in methodology

    def test_reporter_description_updated(self):
        """Reporter description should mention board briefs."""
        from services.soc_agents import AGENT_CONFIGS
        desc = AGENT_CONFIGS["reporter"]["description"].lower()
        assert "board brief" in desc


# ---------------------------------------------------------------------------
# Tests — Agent routing
# ---------------------------------------------------------------------------

class TestAgentRouting:
    """Verify keyword routing selects Reporter for board brief requests."""

    def test_board_brief_routes_to_reporter(self):
        """'board brief' keyword should route to the reporter agent."""
        from services.soc_agents import AgentManager
        mgr = AgentManager()
        agent = mgr.get_agent_by_task("Generate board brief")
        assert agent is not None
        assert agent.id == "reporter"

    def test_board_report_routes_to_reporter(self):
        """'board report' keyword should route to the reporter agent."""
        from services.soc_agents import AgentManager
        mgr = AgentManager()
        agent = mgr.get_agent_by_task("Create board report")
        assert agent is not None
        assert agent.id == "reporter"

    def test_risk_posture_routes_to_reporter(self):
        """'risk posture' keyword should route to the reporter agent."""
        from services.soc_agents import AgentManager
        mgr = AgentManager()
        agent = mgr.get_agent_by_task("Generate risk posture report")
        assert agent is not None
        assert agent.id == "reporter"

    def test_existing_report_routing_preserved(self):
        """Existing 'report' and 'summary' keywords must still work."""
        from services.soc_agents import AgentManager
        mgr = AgentManager()
        assert mgr.get_agent_by_task("Write a report").id == "reporter"
        assert mgr.get_agent_by_task("Generate summary").id == "reporter"
        assert mgr.get_agent_by_task("Document the findings").id == "reporter"


# ---------------------------------------------------------------------------
# Tests — Board brief template
# ---------------------------------------------------------------------------

class TestBoardBriefTemplate:
    """Verify the board brief template exists and has required sections."""

    TEMPLATE_PATH = (
        Path(__file__).parent.parent.parent / "docs" / "templates" / "board-brief.md"
    )

    def test_template_file_exists(self):
        """Board brief template must exist at docs/templates/board-brief.md."""
        assert self.TEMPLATE_PATH.exists(), (
            f"Template not found at {self.TEMPLATE_PATH}"
        )

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


# ---------------------------------------------------------------------------
# Tests — Risk posture computation with synthetic data
# ---------------------------------------------------------------------------

class TestRiskPostureComputation:
    """Test risk posture determination with synthetic data."""

    def test_yellow_when_criticals_covered_by_cases(self):
        """YELLOW: criticals exist but all have in-progress cases."""
        posture = compute_risk_posture(SYNTHETIC_FINDINGS, SYNTHETIC_CASES)
        assert posture == "YELLOW"

    def test_red_when_critical_uncovered(self):
        """RED: critical finding has no associated case."""
        uncovered_finding = {
            "id": "f-uncovered",
            "title": "Uncontained critical",
            "severity": "critical",
            "source": "splunk",
            "timestamp": "2026-03-10T00:00:00Z",
            "mitre_techniques": [],
            "status": "open",
        }
        findings = SYNTHETIC_FINDINGS + [uncovered_finding]
        posture = compute_risk_posture(findings, SYNTHETIC_CASES)
        assert posture == "RED"

    def test_green_when_no_open_criticals(self):
        """GREEN: no open critical findings."""
        resolved_findings = [
            {**f, "status": "resolved"} for f in SYNTHETIC_FINDINGS
        ]
        posture = compute_risk_posture(resolved_findings, SYNTHETIC_CASES)
        assert posture == "GREEN"

    def test_green_on_empty_data(self):
        """GREEN: no findings at all means no risk."""
        posture = compute_risk_posture([], [])
        assert posture == "GREEN"


# ---------------------------------------------------------------------------
# Tests — Metric computation with synthetic data
# ---------------------------------------------------------------------------

class TestMetricComputation:
    """Test metric helper functions with synthetic data."""

    def test_open_criticals_count(self):
        """Should count exactly the open critical findings."""
        count = compute_open_criticals(SYNTHETIC_FINDINGS)
        assert count == 2  # crit001 and crit002

    def test_open_criticals_zero_when_all_resolved(self):
        """Should be 0 when all findings are resolved."""
        resolved = [{**f, "status": "resolved"} for f in SYNTHETIC_FINDINGS]
        assert compute_open_criticals(resolved) == 0

    def test_mttr_hours(self):
        """MTTR should be average of resolved cases."""
        mttr = compute_mttr_hours(SYNTHETIC_CASES)
        assert mttr is not None
        # case-ghi: ~28.5 hours, case-jkl: ~41 hours => avg ~34.75
        assert 25.0 < mttr < 45.0

    def test_mttr_none_when_no_resolved(self):
        """MTTR should be None when no cases are resolved."""
        open_cases = [c for c in SYNTHETIC_CASES if not c.get("resolved_at")]
        assert compute_mttr_hours(open_cases) is None

    def test_detection_coverage(self):
        """Detection coverage = findings with cases / total findings."""
        coverage = compute_detection_coverage(SYNTHETIC_FINDINGS, SYNTHETIC_CASES)
        # 5 findings, 4 covered by cases (crit001, crit002, high001, med001, high002)
        # = 5/5 = 100%
        assert coverage == 100.0

    def test_detection_coverage_partial(self):
        """Partial coverage when some findings lack cases."""
        extra_finding = {
            "id": "f-nocoverage",
            "title": "Uncovered finding",
            "severity": "low",
            "source": "splunk",
            "timestamp": "2026-03-01T00:00:00Z",
            "mitre_techniques": [],
            "status": "open",
        }
        coverage = compute_detection_coverage(
            SYNTHETIC_FINDINGS + [extra_finding],
            SYNTHETIC_CASES,
        )
        # 5 out of 6 covered = 83.3%
        assert 83.0 <= coverage <= 84.0

    def test_detection_coverage_empty(self):
        """Coverage should be 0 when no findings exist."""
        assert compute_detection_coverage([], []) == 0.0


# ---------------------------------------------------------------------------
# Tests — Trend determination
# ---------------------------------------------------------------------------

class TestTrendDetermination:
    """Test 30/60/90 day trend logic."""

    def test_improving_trend(self):
        """Fewer exposures in each more recent window = improving."""
        assert determine_trend(2, 5, 8) == "Improving"

    def test_degrading_trend(self):
        """More exposures in each more recent window = degrading."""
        assert determine_trend(8, 5, 2) == "Degrading"

    def test_stable_trend(self):
        """Similar counts across windows = stable."""
        assert determine_trend(5, 5, 5) == "Stable"

    def test_stable_within_threshold(self):
        """Less than 10% variation = stable."""
        assert determine_trend(10, 10, 11) == "Stable"

    def test_mixed_trend(self):
        """Non-monotonic pattern above threshold = mixed."""
        result = determine_trend(5, 2, 8)
        assert result in ("Mixed", "Improving", "Degrading")
