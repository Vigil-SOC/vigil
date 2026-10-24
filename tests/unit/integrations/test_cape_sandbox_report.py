"""Unit tests for core/integrations/cape_sandbox/report.py."""

import pytest

from core.integrations.cape_sandbox.report import extract_iocs


@pytest.mark.unit
class TestExtractIocs:
    def test_empty_report(self):
        assert extract_iocs({}) == {
            "ip": [],
            "domain": [],
            "url": [],
            "hash": [],
            "mutex": [],
        }

    def test_extracts_deduped_sorted_iocs(self):
        report = {
            "network": {
                "hosts": [{"ip": "9.9.9.9"}, "8.8.8.8", {"ip": "9.9.9.9"}, 123],
                "dns": [{"request": "bad.example"}, {"request": "bad.example"}],
                "http": [{"uri": "http://bad.example/p"}],
            },
            "dropped": [{"sha256": "z" * 64}, {"md5": "a" * 32}],
            "behavior": {"summary": {"mutexes": ["m1"], "mutex": ["m2"]}},
        }
        assert extract_iocs(report) == {
            "ip": ["8.8.8.8", "9.9.9.9"],
            "domain": ["bad.example"],
            "url": ["http://bad.example/p"],
            "hash": ["a" * 32, "z" * 64],
            "mutex": ["m1", "m2"],
        }

    @pytest.mark.parametrize(
        "report",
        [
            {"behavior": None},
            {"behavior": {"summary": None}},
            {"behavior": {"summary": {"mutexes": None}}},
            {"network": None, "dropped": None},
            {"network": {"hosts": None, "dns": None, "http": None}},
        ],
    )
    def test_null_sections_yield_no_iocs(self, report):
        assert all(v == [] for v in extract_iocs(report).values())
