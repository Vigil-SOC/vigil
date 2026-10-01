"""Demo rows carry demo: true without a database column."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


@pytest.fixture
def demo_service():
    from core.platform.demo_data_service import DemoDataService

    saved_random = random.getstate()
    saved = (
        DemoDataService._instance,
        list(DemoDataService._findings),
        list(DemoDataService._cases),
        DemoDataService._initialized,
    )
    DemoDataService._instance = None
    DemoDataService._findings = []
    DemoDataService._cases = []
    DemoDataService._initialized = False
    service = DemoDataService()
    yield service
    (
        DemoDataService._instance,
        DemoDataService._findings,
        DemoDataService._cases,
        DemoDataService._initialized,
    ) = saved
    random.setstate(saved_random)


def test_generated_and_created_rows_are_marked_demo(demo_service):
    findings = demo_service.get_findings()
    cases = demo_service.get_cases()
    assert findings and cases
    assert all(row.get("demo") is True for row in findings)
    assert all(row.get("demo") is True for row in cases)
    assert all("demo" in row.get("tags", []) for row in cases)

    created_finding = demo_service.create_finding({"finding_id": "f-extra"})
    created_case = demo_service.create_case("Extra", ["f-extra"])
    assert created_finding["demo"] is True
    assert created_case["demo"] is True
    assert created_case["tags"] == ["demo"]
    assert demo_service.get_finding("f-extra")["demo"] is True
    assert demo_service.get_case(created_case["case_id"])["demo"] is True
