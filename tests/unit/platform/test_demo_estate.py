"""The sample estate hangs together, and nothing in it points at a real host."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone

import pytest

from core.platform.demo_estate import HOSTS, USERS, build_estate

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
# RFC 5737
DOCUMENTATION_NETS = [
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
DOMAIN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.example\b")


@pytest.fixture(scope="module")
def estate():
    return build_estate(NOW)


def _values(finding: dict, *keys: str) -> set:
    context = finding["entity_context"]
    return {value for key in keys for value in context.get(key, [])}


def _entities(finding: dict) -> set:
    return _values(finding, "hostnames", "usernames", "src_ips", "dest_ips", "domains")


def test_ids_are_unique_and_stable(estate):
    findings, cases = estate
    finding_ids = [f["finding_id"] for f in findings]
    case_ids = [c["case_id"] for c in cases]
    assert len(finding_ids) == len(set(finding_ids))
    assert len(case_ids) == len(set(case_ids))
    assert len(cases) == 5

    later_findings, later_cases = build_estate(NOW + timedelta(days=3))
    assert [f["finding_id"] for f in later_findings] == finding_ids
    assert [c["case_id"] for c in later_cases] == case_ids


def test_every_case_names_findings_that_exist_and_none_is_shared(estate):
    findings, cases = estate
    known = {f["finding_id"] for f in findings}
    linked = [fid for case in cases for fid in case["finding_ids"]]
    assert set(linked) <= known
    assert len(linked) == len(set(linked))
    # some findings are noise that belongs to no case
    assert known - set(linked)


def test_each_case_is_one_connected_story(estate):
    """Every finding in a case shares a host, user, address or domain with another."""
    findings, cases = estate
    by_id = {f["finding_id"]: f for f in findings}
    for case in cases:
        members = [by_id[fid] for fid in case["finding_ids"]]
        reached, frontier = {0}, [0]
        while frontier:
            here = _entities(members[frontier.pop()])
            for index, other in enumerate(members):
                if index not in reached and here & _entities(other):
                    reached.add(index)
                    frontier.append(index)
        assert len(reached) == len(members), case["case_id"]


def test_what_a_case_names_is_in_its_findings(estate):
    findings, cases = estate
    by_id = {f["finding_id"]: f for f in findings}
    for case in cases:
        seen = set().union(*(_entities(by_id[fid]) for fid in case["finding_ids"]))
        text = f"{case['title']} {case['description']}"
        named = {host for host in HOSTS if host in text}
        named |= {user for user in USERS if user in text}
        named |= set(IP.findall(text)) | set(DOMAIN.findall(text))
        assert named, case["case_id"]
        assert named <= seen, (case["case_id"], named - seen)


def test_entities_are_the_estates_own_and_reserved(estate):
    findings, _ = estate
    for finding in findings:
        assert _values(finding, "hostnames") <= set(HOSTS)
        assert _values(finding, "usernames") <= set(USERS)
        for address in _values(finding, "src_ips", "dest_ips"):
            ip = ipaddress.ip_address(address)
            assert any(ip in net for net in DOCUMENTATION_NETS), address
        for domain in _values(finding, "domains"):
            assert domain.endswith(".example"), domain
        assert finding["demo"] is True
    for address in HOSTS.values():
        assert ipaddress.ip_address(address) in DOCUMENTATION_NETS[1]


def test_the_estate_is_the_last_day_and_cases_follow_their_findings(estate):
    findings, cases = estate
    by_id = {f["finding_id"]: f for f in findings}
    stamps = [datetime.fromisoformat(f["timestamp"]) for f in findings]
    assert all(NOW - timedelta(days=1) < stamp < NOW for stamp in stamps)
    assert stamps == sorted(stamps, reverse=True)  # newest first, like the API default
    for case in cases:
        opened = datetime.fromisoformat(case["created_at"])
        first = min(
            datetime.fromisoformat(by_id[fid]["timestamp"])
            for fid in case["finding_ids"]
        )
        assert first <= opened <= datetime.fromisoformat(case["updated_at"]) < NOW


def test_each_build_is_a_fresh_copy(estate):
    """DemoDataService edits these dicts in place; a reset must not inherit the edits."""
    _, cases = build_estate(NOW)
    cases[0]["finding_ids"].append("f-extra")
    assert "f-extra" not in build_estate(NOW)[1][0]["finding_ids"]
    assert build_estate(NOW) == estate
