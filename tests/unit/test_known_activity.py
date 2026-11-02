"""Known activity's rules: what is in force, and what a new rule must carry."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.audit.words import sentence
from core.memory.known_activity import (
    KnownActivityError,
    active_for,
    create_rule,
    state_of,
)
from core.memory.recall_contract import ENTITY_KEY_TYPES
from core.storage.models.known_activity import KnownActivity
from core.time import utcnow

pytestmark = pytest.mark.unit

NOW = datetime(2030, 1, 15, 12, 0, 0)


def _rule(kind="host", value="web-01", expires=NOW + timedelta(days=1), removed=None):
    return KnownActivity(
        entity_kind=kind,
        entity_value=value,
        reason="Patch window",
        expires_at=expires,
        removed_at=removed,
        created_by="analyst",
    )


def _create(**overrides):
    fields = {
        "entity_kind": "host",
        "entity_value": "WEB-01",
        "reason": "Patch window",
        "created_by": "analyst",
        "principal": "u-1",
    }
    return create_rule(MagicMock(), **{**fields, **overrides})


def test_a_rule_in_force_for_a_named_entity_is_returned():
    rule = _rule()
    assert active_for(["host:web-01"], NOW, [rule]) == [rule]


def test_a_rule_for_another_entity_is_not_returned():
    assert active_for(["host:web-02", "user:web-01"], NOW, [_rule()]) == []


def test_entities_are_matched_as_an_entity_key_is_spelled():
    rule = _rule(kind="domain", value="example.org")
    assert active_for(["domain:EXAMPLE[.]ORG"], NOW, [rule]) == [rule]


@pytest.mark.parametrize(
    "expires, state",
    [
        (NOW + timedelta(seconds=1), "active"),
        (NOW, "expired"),
        (NOW - timedelta(seconds=1), "expired"),
    ],
)
def test_a_rule_stops_at_its_expiry(expires, state):
    rule = _rule(expires=expires)
    assert state_of(rule, NOW) == state
    assert active_for(["host:web-01"], NOW, [rule]) == (
        [rule] if state == "active" else []
    )


def test_a_removed_rule_is_not_returned_before_its_expiry():
    rule = _rule(removed=NOW - timedelta(hours=1))
    assert state_of(rule, NOW) == "removed"
    assert active_for(["host:web-01"], NOW, [rule]) == []


def test_a_new_rule_takes_the_servers_default_duration(monkeypatch):
    monkeypatch.setenv("KNOWN_ACTIVITY_DEFAULT_DAYS", "7")
    before = utcnow()
    rule = _create()
    assert before + timedelta(days=7) <= rule.expires_at
    assert rule.expires_at <= utcnow() + timedelta(days=7)


def test_a_new_rule_is_keyed_as_an_entity_key_is_spelled():
    rule = _create(entity_kind="ip", entity_value="2001:DB8:0::1")
    assert (rule.entity_kind, rule.entity_value) == ("ip", "2001:db8::1")
    assert _create().entity_value == "web-01"


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "   "},
        {"reason": "x" * 2001},
        {"entity_kind": "mutex"},
        {"entity_value": "  "},
        {"entity_kind": "ip", "entity_value": "203.0.113.0/24"},
        {"origin": "somewhere"},
        {"expires_at": utcnow() - timedelta(minutes=1)},
    ],
)
def test_a_rule_that_cannot_explain_itself_is_refused(overrides):
    with pytest.raises(KnownActivityError):
        _create(**overrides)


def test_the_tables_check_constraint_holds_every_entity_key_type():
    checks = [
        c.sqltext.text
        for c in KnownActivity.__table__.constraints
        if getattr(c, "name", "") == "ck_known_activity_kind"
    ]
    assert len(checks) == 1
    for kind in ENTITY_KEY_TYPES:
        assert f"'{kind}'" in checks[0]


def test_the_trail_says_what_happened_in_words():
    assert (
        sentence({"action": "known_activity.create", "entity_kind": "host"})
        == "Recorded known activity (host)"
    )
    assert (
        sentence({"action": "known_activity.remove", "entity_kind": "aws_key"})
        == "Removed known activity (aws key)"
    )
