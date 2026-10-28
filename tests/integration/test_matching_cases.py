"""``matching_cases`` and the intake merge it drives, against Postgres (#3292).

A finding matches a Case on a source IP, a file hash, or a hostname plus the
same alert (``data_source`` and trimmed, case-insensitive ``title``) already on
that Case. The replay at the end runs real intake: a burst of brute-force
alerts against one server opens one Case.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text

from core.time import utcnow

pytestmark = [pytest.mark.integration, pytest.mark.database]

HOST = "wazuh.webhosting.coop"
TITLE = "sshd: brute force"


@pytest.fixture(autouse=True)
def _database():
    """Skip only when Postgres is unreachable; fail on anything else."""
    from core.storage.connection import (
        get_db_manager,
        init_database,
        seed_reference_tables,
        stamp_schema_head,
    )

    manager = get_db_manager()
    try:
        manager.initialize()
        session = manager.get_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            "requires a local PostgreSQL (docker compose up -d postgres): " f"{exc}"
        )

    manager.create_tables()
    seed_reference_tables(manager)
    stamp_schema_head(manager.engine)
    init_database()


@pytest.fixture
def world():
    """Writes Cases, findings and investigations; deletes everything it made."""
    from core.storage.connection import get_db_manager
    from core.storage.models import Case, Finding, Investigation, SharedIOC

    tag = uuid.uuid4().hex[:8]
    made = {"cases": [], "findings": []}

    class World:
        tag_ = tag

        def finding(self, *, title=TITLE, source="wazuh", severity="medium", **ctx):
            fid = f"f-{tag}-{len(made['findings'])}"
            with get_db_manager().session_scope() as s:
                s.add(
                    Finding(
                        finding_id=fid,
                        title=title,
                        data_source=source,
                        severity=severity,
                        entity_context=ctx,
                    )
                )
            made["findings"].append(fid)
            return {
                "finding_id": fid,
                "title": title,
                "data_source": source,
                "severity": severity,
                "entity_context": ctx,
            }

        def case(self, keys, *, findings=(), status="open", age_days=0, prio="medium"):
            from services.daemon.shared_intel import SharedIntelligence

            cid = f"case-{tag}-{len(made['cases'])}"
            inv_id = f"inv-{tag}-{len(made['cases'])}"
            stamp = utcnow() - timedelta(days=age_days)
            with get_db_manager().session_scope() as s:
                case = Case(
                    case_id=cid,
                    title=cid,
                    status=status,
                    priority=prio,
                    timeline=[],
                    updated_at=stamp,
                )
                s.add(case)
                s.flush()
                rows = s.execute(
                    select(Finding).where(
                        Finding.finding_id.in_([f["finding_id"] for f in findings])
                    )
                ).scalars()
                case.findings.extend(rows)
                s.add(
                    Investigation(
                        investigation_id=inv_id,
                        case_id=cid,
                        workflow_id="full-investigation",
                        trigger_type="finding",
                        trigger_ids=[],
                        status="completed",
                        workdir="/tmp",
                    )
                )
            made["cases"].append(cid)
            SharedIntelligence().register_investigation(inv_id, findings)
            if keys:
                with get_db_manager().session_scope() as s:
                    from core.storage.shared_ioc_repository import SharedIOCRepository

                    SharedIOCRepository(s).record(inv_id, keys)
            # The attach and the shared_iocs write bump updated_at; pin it last.
            with get_db_manager().session_scope() as s:
                s.get(Case, cid).updated_at = stamp
            return cid

        def track_cases_of(self, finding_ids):
            from core.storage.models import case_findings

            with get_db_manager().session_scope() as s:
                ids = s.execute(
                    select(case_findings.c.case_id).where(
                        case_findings.c.finding_id.in_(finding_ids)
                    )
                ).scalars()
                made["cases"].extend(set(ids) - set(made["cases"]))

    yield World()

    with get_db_manager().session_scope() as s:
        invs = select(Investigation.investigation_id).where(
            Investigation.case_id.in_(made["cases"])
        )
        s.execute(delete(SharedIOC).where(SharedIOC.investigation_id.in_(invs)))
        s.execute(delete(Investigation).where(Investigation.case_id.in_(made["cases"])))
        s.execute(delete(Case).where(Case.case_id.in_(made["cases"])))
        s.execute(delete(Finding).where(Finding.finding_id.in_(made["findings"])))


def _match(finding, *, open=True, **kw):
    from services.daemon.shared_intel import SharedIntelligence

    return SharedIntelligence().matching_cases(finding, open=open, **kw)


def _ids(matches):
    return [m.case_id for m in matches]


# ---- the rule -------------------------------------------------------------


def test_same_alert_on_the_same_host_matches(world):
    held = world.finding(hostname=HOST, src_ip="27.102.116.43")
    a = world.case(set(), findings=[held])

    # Title compared trimmed and case-insensitive; a new attacker IP.
    new = world.finding(title="  SSHD: Brute Force ", hostname=HOST, src_ip="1.2.3.4")
    [m] = _match(new)
    assert (m.case_id, m.key, m.same_alert) == (a, f"hostname:{HOST}", True)


def test_host_without_the_same_alert_does_not_match(world):
    held = world.finding(hostname=HOST)
    world.case(set(), findings=[held])

    assert _match(world.finding(title="malware dropped", hostname=HOST)) == []
    assert _match(world.finding(source="elastic", hostname=HOST)) == []


def test_source_ip_and_hash_match_on_their_own(world):
    a = world.case({"ip:27.102.116.43"})
    b = world.case({"hash:abc123"})

    [m] = _match(world.finding(src_ips=["27.102.116.43"], hostname="other"))
    assert (m.case_id, m.key) == (a, "ip:27.102.116.43")
    [m] = _match(world.finding(file_hashes=["ABC123"]))
    assert (m.case_id, m.key) == (b, "hash:abc123")


def test_user_domain_and_destination_ip_never_match(world):
    world.case({"user:root", "domain:evil.test", "ip:10.0.0.5", "hostname:solo"})

    assert _match(world.finding(user="root")) == []
    assert _match(world.finding(domains=["evil.test"])) == []
    assert _match(world.finding(dst_ip="10.0.0.5")) == []
    assert _match(world.finding(hostname="solo")) == []


def test_open_and_closed_cases_are_kept_apart(world):
    opened = world.case({"ip:9.9.9.9"})
    closed = world.case({"ip:9.9.9.9"}, status="closed")
    finding = world.finding(src_ip="9.9.9.9")

    assert _ids(_match(finding, open=True)) == [opened]
    assert _ids(_match(finding, open=False)) == [closed]


def test_newest_updated_first_and_limited(world):
    cases = [world.case({"ip:8.8.8.8"}, status="closed", age_days=d) for d in range(6)]
    finding = world.finding(src_ip="8.8.8.8")

    assert _ids(_match(finding, open=False, limit=5)) == cases[:5]
    assert _ids(_match(finding, open=False, exclude_case_id=cases[0])) == cases[1:]


# ---- intake ---------------------------------------------------------------


def _orchestrator(tmp_path):
    from services.daemon.config import OrchestratorConfig
    from services.daemon.orchestrator import Orchestrator

    return Orchestrator(OrchestratorConfig(dry_run=True, workdir_base=str(tmp_path)))


def _case(case_id):
    from core.storage.connection import get_db_manager
    from core.storage.models import Case

    with get_db_manager().session_scope() as s:
        case = s.get(Case, case_id)
        return {
            "priority": case.priority,
            "activities": list(case.activities or []),
            "findings": {f.finding_id for f in case.findings},
        }


@pytest.mark.asyncio
async def test_an_alert_joins_a_finished_case_and_chains(world, tmp_path):
    held = world.finding(hostname=HOST, src_ip="27.102.116.43")
    a = world.case(set(), findings=[held], age_days=30)
    orch = _orchestrator(tmp_path)

    # Same alert, same host, new IP: joins A and brings ip:5.5.5.5 with it.
    second = world.finding(severity="high", hostname=HOST, src_ip="5.5.5.5")
    assert orch._merge_if_overlaps(second, None) is True
    # A different server, from the IP alert 2 brought in.
    third = world.finding(title="other rule", hostname="web-2", src_ip="5.5.5.5")
    assert orch._merge_if_overlaps(third, None) is True

    case = _case(a)
    assert case["findings"] == {
        held["finding_id"],
        second["finding_id"],
        third["finding_id"],
    }
    assert case["priority"] == "high"
    merges = [x for x in case["activities"] if x["activity_type"] == "intake_merge"]
    assert [x["description"] for x in merges] == [
        f"Alert `{TITLE}` added, same alert on `hostname:{HOST}`; "
        "priority raised from medium to high",
        "Alert `other rule` added, matched on `ip:5.5.5.5`",
    ]


@pytest.mark.asyncio
async def test_a_lower_alert_leaves_the_priority(world, tmp_path):
    a = world.case({"ip:7.7.7.7"})
    orch = _orchestrator(tmp_path)

    assert orch._merge_if_overlaps(
        world.finding(severity="low", src_ip="7.7.7.7"), None
    )
    assert _case(a)["priority"] == "medium"


@pytest.mark.asyncio
async def test_a_new_case_links_to_the_closed_cases_it_matches(world, tmp_path):
    from core.cases.case_records_service import list_relationships
    from core.storage.connection import get_db_manager

    closed = [world.case({"ip:6.6.6.6"}, status="closed", age_days=d) for d in range(3)]
    finding = world.finding(src_ip="6.6.6.6")
    orch = _orchestrator(tmp_path)

    await orch._create_investigation_for_finding(finding, None)
    world.track_cases_of([finding["finding_id"]])

    [new] = _ids(_match(finding))
    with get_db_manager().session_scope() as s:
        rels = list_relationships(s, new)
        assert {r.related_case_id for r in rels} == set(closed)
        assert {r.relationship_type for r in rels} == {"related_to"}
        assert {r.notes for r in rels} == {"same attacker ip:6.6.6.6"}
        for old in closed:
            assert [r.related_case_id for r in list_relationships(s, old)] == [new]


@pytest.mark.asyncio
async def test_replaying_a_brute_force_burst_opens_one_case(world, tmp_path):
    orch = _orchestrator(tmp_path)
    ips = [f"203.0.113.{n}" for n in range(1, 6)]
    findings = [
        world.finding(hostname=HOST, src_ip=ips[n % len(ips)], user="root")
        for n in range(30)
    ]

    for finding in findings:
        await orch._create_investigation_for_finding(finding, None)
    world.track_cases_of([f["finding_id"] for f in findings])

    from core.storage.connection import get_db_manager
    from core.storage.models import case_findings

    with get_db_manager().session_scope() as s:
        cases = set(
            s.execute(
                select(case_findings.c.case_id).where(
                    case_findings.c.finding_id.in_([f["finding_id"] for f in findings])
                )
            ).scalars()
        )
    assert len(cases) == 1
    assert orch.stats["investigations_created"] == 1
