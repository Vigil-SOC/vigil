"""The security-scan jobs are required checks, so they must be able to fail.

`cmd || true` and `continue-on-error: true` both turn a scan's exit code into
green, which is how Bandit and npm audit ran for a long time without gating
anything (#1709).
"""

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci-cd.yml"


def swallowed_failures(workflow: Dict[str, Any]) -> List[str]:
    """Describe every step in a `security-scan-*` job that cannot fail the job."""
    found = []
    for job_id, job in workflow["jobs"].items():
        if not job_id.startswith("security-scan-"):
            continue
        for i, step in enumerate(job.get("steps", [])):
            label = f"{job_id}: {step.get('name') or step.get('uses') or i}"
            if step.get("continue-on-error") in (True, "true"):
                found.append(f"{label} sets continue-on-error")
            if re.search(r"\|\|\s*true\b", step.get("run", "")):
                found.append(f"{label} uses '|| true'")
    return found


def test_security_scan_jobs_exist():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert {"security-scan-python", "security-scan-npm"} <= set(jobs)


def test_security_scan_steps_can_fail():
    assert swallowed_failures(yaml.safe_load(WORKFLOW.read_text())) == []


@pytest.mark.parametrize(
    "step",
    [
        {"run": "bandit -r core/ || true"},
        {"run": "npm audit\nnpm audit --audit-level=high || true"},
        {"run": "npm audit", "continue-on-error": True},
    ],
)
def test_lint_flags_swallowed_failures(step):
    workflow = {
        "jobs": {"security-scan-x": {"steps": [step]}, "other": {"steps": [step]}}
    }
    assert len(swallowed_failures(workflow)) >= 1
