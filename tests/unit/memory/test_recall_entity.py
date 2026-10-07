"""Reading episodic memory (#732), against real rows and a real Postgres.

The query is the thing under test and it is not portable: the per-key cap is a
LATERAL, the Verdict join is array containment against a GIN index, and the total
order exists because ``LIMIT`` over a partial one is nondeterministic. A fake
would agree with whatever this module happened to do.

Rows are seeded the way the Distil writes them rather than by running a hunt: a
hunt exercises the fold, and what is being asserted here is the read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.memory.recall import expire_read_log, recall, recall_entity
from core.memory.recall_contract import (
    RECALL_OVERALL_CAP,
    RECALL_PER_KEY_CAP,
    RECALL_RESULT_KEYS,
    RECALL_TOOL,
    recall_ranking,
)
from core.storage.connection import get_db_session
from core.storage.models import (
    EpisodicGap,
    EpisodicReadLog,
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
)

pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]

EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)


# The clean-table fixture lives in tests/unit/conftest.py: #735's grant test
# seeds the same rows, and two copies drift.
@pytest.fixture
def session(episodic_session):
    return episodic_session


def sighting(db, key: str, *, investigation: str, concluded: datetime, source="splunk"):
    row = EpisodicSighting(
        entity_key=key,
        investigation_kind="hunt",
        investigation_id=investigation,
        source_system=source,
        hit_count=3,
        attacker_influenceable=True,
        first_seen=concluded - timedelta(hours=1),
        last_seen=concluded,
        concluded_at=concluded,
    )
    db.add(row)
    return row


def verdict(db, keys, *, investigation: str, concluded: datetime, sources=()):
    row = EpisodicVerdict(
        investigation_kind="hunt",
        investigation_id=investigation,
        hypothesis_id=f"h-{investigation}",
        statement="beaconing to a known-bad host",
        outcome="proven",
        rationale="regular interval, low jitter",
        subject_entities=list(keys),
        attacker_influenceable_only=False,
        trust="agent",
        first_seen=concluded - timedelta(hours=2),
        last_seen=concluded,
        window_source="observed",
        concluded_at=concluded,
    )
    db.add(row)
    db.flush()
    for system, stance in sources:
        db.add(
            EpisodicVerdictSource(
                verdict_id=row.id,
                source_system=system,
                stance=stance,
                source_tier="telemetry",
            )
        )
    return row


def gap(db, keys, *, investigation: str, concluded: datetime):
    row = EpisodicGap(
        investigation_kind="hunt",
        investigation_id=investigation,
        hypothesis_id=f"g-{investigation}",
        statement="did anything else talk to it",
        disposition="budget_exhausted",
        reason="the hunt ran out of turns",
        subject_entities=list(keys),
        concluded_at=concluded,
    )
    db.add(row)
    return row


@pytest.fixture
def invoke(monkeypatch):
    """The router's own endpoint, which is the seam an agent actually calls."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.agents import internal_auth, tools_router
    from core.integrations.mcp.registry import MCPRegistry

    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "shhh")
    app = FastAPI()
    app.state.mcp_registry = MCPRegistry()
    app.include_router(tools_router.router, prefix=tools_router.ROUTER_META.prefix)
    client = TestClient(app)

    def post(tool, args):
        return client.post(
            "/internal/tools/invoke",
            json={
                "tool": tool,
                "args": args,
                "bounds": {"max_rows": 5, "timeout_ms": 10_000},
            },
            headers={"Authorization": "Bearer shhh"},
        )

    return post


def read_log(db):
    from sqlalchemy import text

    return list(
        db.execute(
            text(
                "SELECT caller_kind, caller_id, keys, as_of, row_counts, dropped, "
                "ranking, ts FROM episodic_read_log ORDER BY id ASC"
            )
        ).mappings()
    )


def test_an_entity_with_history_returns_its_sightings_verdicts_and_gaps(session):
    key = "ip:10.0.0.7"
    sighting(session, key, investigation="hunt-a", concluded=EPOCH)
    verdict(
        session,
        [key],
        investigation="hunt-a",
        concluded=EPOCH,
        sources=[("splunk", "supports"), ("crowdstrike", "weakens")],
    )
    gap(session, [key], investigation="hunt-a", concluded=EPOCH)
    session.commit()

    result = recall([key])

    assert tuple(result) == RECALL_RESULT_KEYS
    assert result["keys"] == [key]
    assert len(result["sightings"]) == 1
    assert len(result["verdicts"]) == 1
    assert len(result["gaps"]) == 1
    assert result["ranking"] == recall_ranking()

    seen = result["sightings"][0]
    assert seen["entity_key"] == key
    assert seen["source_system"] == "splunk"
    assert seen["hit_count"] == 3
    assert seen["window"]["last_seen"].startswith("2026-08-01")

    # A stance per source, and direction with it: a flat corroborated list could
    # not say that CrowdStrike argued against the claim.
    stances = {
        s["source_system"]: s["stance"] for s in result["verdicts"][0]["sources"]
    }
    assert stances == {"splunk": "supports", "crowdstrike": "weakens"}
    assert result["gaps"][0]["disposition"] == "budget_exhausted"


def test_an_entity_with_no_history_is_an_empty_result_rather_than_an_error(session):
    session.commit()

    result = recall(["ip:203.0.113.9"])

    assert result["keys"] == ["ip:203.0.113.9"]
    assert result["sightings"] == result["verdicts"] == result["gaps"] == []
    # Zeros rather than absent keys: an entity nobody has looked at and an entity
    # whose history was truncated must not read the same.
    assert all(
        counts == {"per_key_cap": 0, "overall_cap": 0}
        for counts in result["dropped"].values()
    )


def test_keys_differing_by_case_or_defanging_find_the_same_rows(session):
    stored = "url:http://evil.com/a"
    sighting(session, stored, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    asked = recall(["URL:hxxp://Evil[.]com/A"])

    assert asked["keys"] == [stored]
    assert len(asked["sightings"]) == 1


def test_a_case_significant_type_is_not_folded(session):
    # Folding an ARN's resource part makes two principals one key, and the join
    # still returns rows -- just the wrong ones.
    stored = "arn:arn:aws:iam::1:role/Admin"
    sighting(session, stored, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    assert len(recall([stored])["sightings"]) == 1
    assert recall(["arn:arn:aws:iam::1:role/admin"])["sightings"] == []


def test_more_sightings_than_the_per_key_cap_returns_the_cap_and_says_what_went(
    session,
):
    key = "ip:10.0.0.8"
    over = 4
    for index in range(RECALL_PER_KEY_CAP + over):
        sighting(
            session,
            key,
            investigation=f"hunt-{index:03d}",
            concluded=EPOCH - timedelta(minutes=index),
        )
    session.commit()

    result = recall([key])

    assert len(result["sightings"]) == RECALL_PER_KEY_CAP
    assert result["dropped"]["sightings"]["per_key_cap"] == over
    # Newest first, which is what makes the cap a choice rather than a coin toss.
    assert result["sightings"][0]["investigation_id"] == "hunt-000"


def test_a_broad_read_stays_inside_the_overall_budget_and_names_what_it_dropped(
    session,
):
    # One key under its own cap, many keys over the overall one: the two caps
    # are different failures and the result has to tell them apart.
    keys = [f"ip:10.1.0.{index}" for index in range(12)]
    for key_index, key in enumerate(keys):
        for row_index in range(10):
            sighting(
                session,
                key,
                investigation=f"hunt-{key_index:02d}-{row_index:02d}",
                concluded=EPOCH - timedelta(minutes=key_index * 10 + row_index),
            )
    session.commit()

    result = recall(keys)

    total = sum(len(result[kind]) for kind in ("sightings", "verdicts", "gaps"))
    assert total == RECALL_OVERALL_CAP
    assert result["dropped"]["sightings"]["overall_cap"] == 120 - RECALL_OVERALL_CAP
    assert result["dropped"]["sightings"]["per_key_cap"] == 0


def test_the_overall_cap_sorts_on_the_order_it_reports_rather_than_a_copy(
    session, monkeypatch
):
    """Flip RECALL_ORDER's direction and the budget must keep the other rows.

    The laterals derive their ORDER BY from the constant, so a budget that
    restated the directions would go on keeping the newest rows while the result
    reported the constant as the basis for a set chosen some other way. That is
    the one failure the derivation exists to prevent, and only reversing the
    constant can show it is prevented.
    """
    from core.memory import recall as module

    key = "ip:10.12.0.1"
    for index in range(3):
        sighting(
            session,
            key,
            investigation=f"hunt-{index}",
            concluded=EPOCH - timedelta(days=index),
        )
    session.commit()

    newest_first = [row["investigation_id"] for row in recall([key])["sightings"]]
    assert newest_first == ["hunt-0", "hunt-1", "hunt-2"]

    # Only the budget's terms are flipped; the SQL keeps its own order, so what
    # changes below can only be the step under test.
    monkeypatch.setattr(module, "_ORDER_TERMS", (("concluded_at", False), ("id", True)))
    monkeypatch.setattr(module, "RECALL_OVERALL_CAP", 1)

    kept = recall([key])["sightings"]

    assert [row["investigation_id"] for row in kept] == ["hunt-2"]


def test_two_identical_reads_over_unchanged_data_return_the_same_rows_in_order(session):
    # Every row shares a concluded_at, so nothing but the primary-key tiebreak
    # decides the order or which rows the cap keeps.
    keys = [f"ip:10.2.0.{index}" for index in range(8)]
    for key_index, key in enumerate(keys):
        for row_index in range(20):
            sighting(
                session,
                key,
                investigation=f"hunt-{key_index:02d}-{row_index:02d}",
                concluded=EPOCH,
            )
    session.commit()

    # as_of is pinned because it defaults to now and would differ between the
    # two calls; what is under test is the set and its order, not the clock.
    first = recall(keys, as_of=EPOCH)
    second = recall(keys, as_of=EPOCH)

    assert first == second
    assert len(first["sightings"]) == RECALL_OVERALL_CAP


def test_a_shared_verdict_is_counted_once_when_the_cap_reports_what_it_withheld(
    session,
):
    # Both keys name every Verdict, so each key's page is capped and the two
    # pages hold the same rows. Counting the overshoot per key would report twice
    # what was actually withheld -- the caller would be told 8 conclusions were
    # held back when 4 were.
    keys = ["ip:10.11.0.1", "host:dc01"]
    over = 4
    for index in range(RECALL_PER_KEY_CAP + over):
        verdict(
            session,
            keys,
            investigation=f"hunt-{index:03d}",
            concluded=EPOCH - timedelta(minutes=index),
        )
    session.commit()

    result = recall(keys)

    assert len(result["verdicts"]) == RECALL_PER_KEY_CAP
    assert result["dropped"]["verdicts"]["per_key_cap"] == over


def test_a_verdict_naming_two_queried_keys_is_returned_once(session):
    keys = ["ip:10.3.0.1", "host:web01"]
    verdict(session, keys, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    result = recall(keys)

    assert len(result["verdicts"]) == 1


def test_the_freshness_filter_excludes_what_concluded_after_as_of(session):
    key = "ip:10.4.0.1"
    sighting(session, key, investigation="hunt-old", concluded=EPOCH)
    sighting(
        session, key, investigation="hunt-new", concluded=EPOCH + timedelta(days=2)
    )
    session.commit()

    result = recall([key], as_of=EPOCH + timedelta(days=1))

    assert [row["investigation_id"] for row in result["sightings"]] == ["hunt-old"]
    assert result["as_of"] == "2026-08-02T00:00:00Z"


def test_every_read_writes_a_log_row_naming_the_caller_the_keys_and_the_answer(
    session,
):
    key = "ip:10.5.0.1"
    sighting(session, key, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    recall([key], caller_kind="worker", caller_id="threat_hunter", as_of=EPOCH)

    rows = read_log(session)
    assert len(rows) == 1
    logged = rows[0]
    assert logged["caller_kind"] == "worker"
    assert logged["caller_id"] == "threat_hunter"
    assert list(logged["keys"]) == [key]
    assert logged["as_of"] == EPOCH
    assert logged["row_counts"] == {"sightings": 1, "verdicts": 0, "gaps": 0}
    assert logged["ranking"] == recall_ranking()


def test_a_read_that_found_nothing_is_logged_too(session):
    session.commit()

    recall(["ip:198.51.100.4"])

    rows = read_log(session)
    assert len(rows) == 1
    # An unattributed read is still worth logging; refusing would make this a log
    # of the well-behaved callers only.
    assert rows[0]["caller_kind"] == "unknown"
    assert rows[0]["row_counts"] == {"sightings": 0, "verdicts": 0, "gaps": 0}


def test_the_read_log_is_swept_at_the_retention_boundary(session):
    from sqlalchemy import text

    session.execute(text("DELETE FROM episodic_read_log"))
    session.commit()

    recall(["ip:10.6.0.1"])
    session.execute(
        text("UPDATE episodic_read_log SET ts = now() - interval '91 days'")
    )
    session.commit()
    recall(["ip:10.6.0.2"])

    removed = expire_read_log(datetime.now(timezone.utc) - timedelta(days=90))

    assert removed == 1
    assert len(read_log(session)) == 1


def test_the_singular_argument_is_wrapped(session):
    key = "ip:10.7.0.1"
    sighting(session, key, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    result = recall_entity({"entity_key": key})

    assert result["keys"] == [key]
    assert len(result["sightings"]) == 1


def test_the_row_bound_the_router_injects_is_ignored(session):
    # tools_router._bounded puts `limit` on every backend call. The caps here are
    # memory's own, and a budget meant for a page of findings is not one for an
    # entity's history.
    key = "ip:10.8.0.1"
    for index in range(5):
        sighting(
            session,
            key,
            investigation=f"hunt-{index}",
            concluded=EPOCH - timedelta(minutes=index),
        )
    session.commit()

    assert len(recall_entity({"entity_key": key, "limit": 1})["sightings"]) == 5


@pytest.mark.parametrize(
    "args",
    [
        {"caller_kind": "worker"},
        {"entity_key": "ip:10.9.0.1", "as_of": "last tuesday"},
    ],
    ids=["no key named", "unreadable as_of"],
)
def test_a_caller_mistake_is_invalid_args_however_it_is_spelled(session, args):
    # Both are the caller's to fix, so both must come back the same way. The
    # router answers invalid_args only for Python's own wording for a call that
    # did not fit its signature; anything else is a backend_error, which tells
    # the model the tool broke rather than that it should call again.
    from core.agents.tools_router import _is_bad_arguments

    with pytest.raises(TypeError) as raised:
        recall_entity(args)

    assert _is_bad_arguments(raised.value)


def test_the_bridge_resolves_recall_entity_without_reaching_mcp(
    invoke, monkeypatch, session
):
    """The read seam, end to end: an agent's call, answered by the backend.

    Through the router's own endpoint rather than the registry alone, because
    the fall-through to MCP is the router's: a name the registry declines is
    exactly what reaches a server next. One call exercises the registry wiring,
    the row bound the router injects, the envelope and the read log.
    """
    from core.agents import tools_router
    from core.agents.tool_registry import MANIFEST

    def refuse(name, args, timeout_s, registry):
        raise AssertionError(f"{RECALL_TOOL} was routed to MCP")

    monkeypatch.setattr(tools_router, "execute_mcp_tool", refuse)

    key = "ip:10.10.0.1"
    sighting(session, key, investigation="hunt-a", concluded=EPOCH)
    session.commit()

    assert RECALL_TOOL in MANIFEST, "the tool must be registered in ALL_TOOLS"
    response = invoke(
        RECALL_TOOL,
        {"entity_keys": [key], "caller_kind": "worker", "caller_id": "threat_hunter"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # One row holding the whole mapping, and not capped: _rows must not slice the
    # Sightings, Verdicts and Gaps into rows of their own and lose which is which.
    assert body["rowCount"] == 1
    assert body["capped"] is False
    assert tuple(body["rows"][0]) == RECALL_RESULT_KEYS
    assert len(body["rows"][0]["sightings"]) == 1

    # Attributed even though it arrived over HTTP, which is what the log is for.
    assert read_log(session)[0]["caller_id"] == "threat_hunter"
