"""Intake joins an alert to the open Case for its incident (#3292), against the throwaway Postgres."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import (
    Case,
    CaseRelationship,
    Finding,
    Investigation,
    SharedIOC,
    case_findings,
)
from core.storage.shared_ioc_repository import SharedIOCRepository
from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator, _matching_cases
from services.daemon.shared_intel import SharedIntelligence

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

TITLE = "sshd: brute force"


class World:
    """Unique entities per test: the database outlives the test."""

    def __init__(self):
        self.tag = uuid.uuid4().hex[:10]
        self.host = f"host-{self.tag}.example"

    def ip(self, n: int) -> str:
        return f"10.{int(self.tag[:2], 16)}.{int(self.tag[2:4], 16)}.{n}"

    def finding(self, n: int, **kw) -> dict:
        """A finding as intake sees it, and its row in the database."""
        finding_id = f"f-{self.tag}-{n}"
        entity = kw.pop("entity_context", {})
        title = kw.pop("title", TITLE)
        severity = kw.pop("severity", "high")
        with get_db_manager().session_scope() as session:
            session.add(
                Finding(
                    finding_id=finding_id,
                    data_source=kw.pop("data_source", "wazuh"),
                    severity=severity,
                    title=title,
                    entity_context=entity,
                )
            )
        return {
            "finding_id": finding_id,
            "title": title,
            "severity": severity,
            "data_source": "wazuh",
            "entity_context": entity,
        }

    def case(
        self, n: int, *, keys, status="open", priority="medium", age_days=0, on=None
    ):
        """A Case with one investigation indexed under ``keys`` and optionally holding finding ``on``."""
        case_id = f"case-{self.tag}-{n}"
        with get_db_manager().session_scope() as session:
            session.add(
                Case(
                    case_id=case_id,
                    title="t",
                    status=status,
                    priority=priority,
                    updated_at=utcnow() - timedelta(days=age_days),
                )
            )
            session.flush()
            inv = Investigation(
                investigation_id=f"inv-{self.tag}-{n}",
                case_id=case_id,
                workflow_id="incident-response",
                trigger_type="finding",
                status="completed",
                workdir="/tmp/inv",
            )
            session.add(inv)
            session.flush()
            SharedIOCRepository(session).record(inv.investigation_id, keys)
            if on:
                session.execute(
                    case_findings.insert().values(
                        case_id=case_id, finding_id=on["finding_id"]
                    )
                )
        return case_id


@pytest.fixture
def w():
    return World()


def _match(finding, *, open=True):
    return _matching_cases(finding, open=open)


def test_the_title_rule_joins_the_same_alert_on_the_same_host(w):
    held = w.finding(1)
    case = w.case(1, keys=[f"ip:{w.ip(1)}", f"hostname:{w.host}"], on=held)

    new = w.finding(
        2,
        title=f"  {TITLE.upper()} ",
        entity_context={"hostnames": [w.host], "src_ips": [w.ip(2)]},
    )
    [m] = _match(new)
    assert (m.case_id, m.key, m.same_alert) == (case, f"hostname:{w.host}", True)

    # Same host, other title: a different problem on that server.
    other = w.finding(3, title="malware found", entity_context={"hostnames": [w.host]})
    assert _match(other) == []
    # Same title and host under another data source is not the same alert either.
    elsewhere = {**new, "data_source": "elastic"}
    assert _match(elsewhere) == []


def test_an_attacker_ip_or_a_hash_alone_joins_on_any_host(w):
    case = w.case(1, keys=[f"ip:{w.ip(1)}", f"hash:{w.tag}abc", f"hostname:{w.host}"])

    by_ip = {"entity_context": {"src_ip": w.ip(1), "hostnames": ["other-box"]}}
    [m] = _match(by_ip)
    assert (m.case_id, m.key, m.same_alert) == (case, f"ip:{w.ip(1)}", False)

    by_hash = {"entity_context": {"file_hashes": [f"{w.tag.upper()}ABC"]}}
    [m] = _match(by_hash)
    assert (m.case_id, m.key) == (case, f"hash:{w.tag}abc")


def test_a_username_domain_destination_ip_or_bare_host_never_joins(w):
    w.case(
        1,
        keys=[
            f"ip:{w.ip(1)}",
            f"user:root-{w.tag}",
            f"domain:{w.tag}.example.com",
            f"hostname:{w.host}",
        ],
        on=w.finding(1),
    )

    shared_only = {
        "title": TITLE,
        "data_source": "wazuh",
        "entity_context": {
            "usernames": [f"root-{w.tag}"],
            "domains": [f"{w.tag}.example.com"],
            "dst_ip": w.ip(1),  # the Case's attacker, seen here as a destination
            "dest_ips": [w.ip(1)],
        },
    }
    assert _match(shared_only) == []
    # A host with no title to compare is a host alone.
    assert (
        _match({"entity_context": {"hostnames": [w.host]}, "data_source": "wazuh"})
        == []
    )
    assert _match({}) == []


def test_open_and_closed_are_split_and_the_newest_updated_comes_first(w):
    keys = [f"ip:{w.ip(1)}"]
    closed = [w.case(n, keys=keys, status="closed", age_days=n) for n in range(1, 7)]
    open_old = w.case(7, keys=keys, age_days=90)  # weeks untouched still matches
    finding = {"entity_context": {"src_ip": w.ip(1)}}

    assert [m.case_id for m in _match(finding, open=True)] == [open_old]
    assert [m.case_id for m in _match(finding, open=False)] == closed


def test_the_open_case_read_never_swallows_an_error(monkeypatch, w):
    monkeypatch.setattr(
        SharedIOCRepository,
        "matching_cases",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    with pytest.raises(RuntimeError):
        _match({"entity_context": {"src_ip": w.ip(1)}})


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = SharedIntelligence()
    orch.stats = {"dedup_prevented": 0}
    orch._decide_trigger = MagicMock()
    return orch


def _case(case_id: str) -> dict:
    with get_db_manager().session_scope() as session:
        case = session.get(Case, case_id)
        return {
            "priority": case.priority,
            "activities": list(case.activities or []),
            "findings": sorted(f.finding_id for f in case.findings),
        }


def test_a_merge_attaches_notes_indexes_and_chains(w):
    held = w.finding(1)
    case = w.case(1, keys=[f"ip:{w.ip(1)}", f"hostname:{w.host}"], on=held)
    orch = _orchestrator()

    # Same alert, same host, a new IP: attached though the run finished.
    second = w.finding(2, entity_context={"hostnames": [w.host], "src_ips": [w.ip(2)]})
    assert orch._merge_if_overlaps(second, 5) is True
    orch._decide_trigger.assert_called_once_with(
        5, state="merged", reason="entity_on_open_case", merged_into=case
    )
    assert orch.stats["dedup_prevented"] == 1
    after = _case(case)
    assert second["finding_id"] in after["findings"]
    assert after["priority"] == "high"  # raised from medium
    [activity] = after["activities"]
    assert activity["description"] == (
        f"Alert `{TITLE}` added, same alert on `hostname:{w.host}`"
    )
    with get_db_manager().session_scope() as session:
        indexed = session.query(SharedIOC.ioc_type, SharedIOC.value).filter_by(
            investigation_id=f"inv-{w.tag}-1"
        )
        assert ("ip", w.ip(2)) in indexed.all()

    # Chain: the IP alert 2 brought in now joins alone, on a different title.
    third = w.finding(
        3, title="other rule", entity_context={"src_ip": w.ip(2)}, severity="low"
    )
    assert orch._merge_if_overlaps(third, 6) is True
    assert orch._decide_trigger.call_args.kwargs["merged_into"] == case
    after = _case(case)
    assert after["priority"] == "high"  # a lower alert never lowers it
    assert after["activities"][-1]["description"] == (
        f"Alert `other rule` added, matched on `ip:{w.ip(2)}`"
    )
    assert len(after["activities"]) == 2


def test_an_unrelated_alert_is_left_to_launch(w):
    w.case(1, keys=[f"ip:{w.ip(1)}"])
    orch = _orchestrator()

    assert (
        orch._merge_if_overlaps(w.finding(2, entity_context={"src_ip": w.ip(9)}), 5)
        is False
    )
    orch._decide_trigger.assert_not_called()


def _links(case_id: str) -> list:
    with get_db_manager().session_scope() as session:
        rows = session.query(CaseRelationship).filter_by(case_id=case_id).all()
        return sorted((r.related_case_id, r.relationship_type, r.notes) for r in rows)


def test_a_new_case_links_to_the_five_newest_closed_matches_both_ways(w):
    keys = [f"ip:{w.ip(1)}"]
    closed = [w.case(n, keys=keys, status="closed", age_days=n) for n in range(1, 7)]
    new = w.case(9, keys=keys)
    finding = {"entity_context": {"src_ip": w.ip(1)}}

    asyncio.run(_orchestrator()._link_closed_cases(new, finding))

    note = f"same attacker {w.ip(1)}"
    assert _links(new) == sorted((c, "related_to", note) for c in closed[:5])
    for c in closed[:5]:
        assert _links(c) == [(new, "related_to", note)]
    assert _links(closed[5]) == []


def test_a_failed_closed_case_lookup_still_leaves_the_case(monkeypatch, w):
    new = w.case(1, keys=[f"ip:{w.ip(1)}"])
    monkeypatch.setattr(
        "services.daemon.orchestrator._matching_cases",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    asyncio.run(_orchestrator()._link_closed_cases(new, {}))

    assert _links(new) == []
    assert _case(new)["priority"] == "medium"


def test_replaying_a_brute_force_makes_one_case(w):
    """Dozens of sshd findings on one host from a few IPs join the first one's Case."""
    orch = _orchestrator()
    first = w.finding(0, entity_context={"hostnames": [w.host], "src_ips": [w.ip(0)]})
    case = w.case(0, keys=[f"ip:{w.ip(0)}", f"hostname:{w.host}"], on=first)

    launched = 0
    for n in range(1, 41):
        finding = w.finding(
            n,
            entity_context={"hostnames": [w.host], "src_ips": [w.ip(n % 5)]},
        )
        if not orch._merge_if_overlaps(finding, n):
            launched += 1

    assert launched == 0
    assert len(_case(case)["findings"]) == 41
    with get_db_manager().session_scope() as session:
        n_cases = (
            session.query(Case)
            .join(Investigation, Investigation.case_id == Case.case_id)
            .join(
                SharedIOC, SharedIOC.investigation_id == Investigation.investigation_id
            )
            .filter(SharedIOC.value == w.host)
            .count()
        )
    assert n_cases == 1
