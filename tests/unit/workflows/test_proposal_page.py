"""A run that files proposed containment pages PagerDuty once (#3401)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.integrations.pagerduty import page as pagerduty
from core.workflows import run_bridge_router as rbr

pytestmark = pytest.mark.unit

PROPOSAL = {
    "action_type": "isolate_host",
    "target": "ws-7",
    "reason": "beaconing",
    "confidence": 0.9,
}


@pytest.fixture
def paged(monkeypatch):
    pager = Mock()
    monkeypatch.setattr(rbr, "page_proposed_containment", pager)
    monkeypatch.setattr(rbr, "_record_proposals", Mock())
    monkeypatch.setattr(rbr, "proposer_principal_id", lambda run_id: "agent:run")
    return pager


def _service(match=None):
    service = Mock()
    service.run_proposal_match.return_value = match
    service.create_action.return_value = SimpleNamespace(action_id="act-1")
    return service


def test_a_new_proposal_set_pages_once(paged):
    two = [PROPOSAL, {**PROPOSAL, "target": "ws-8"}]
    rbr._propose_actions("case-1", "run-1", two, _service())

    paged.assert_called_once_with(
        "case-1",
        "run-1",
        [("Isolate host ws-7", "ws-7"), ("Isolate host ws-8", "ws-8")],
    )


def test_a_re_sent_terminal_pages_nobody(paged):
    key = "run-proposal:run-1:isolate_host:ws-7"
    match = SimpleNamespace(idempotency_key=key, status="pending", action_id="act-1")
    service = _service(match)

    rbr._propose_actions("case-1", "run-1", [PROPOSAL], service)

    service.create_action.assert_not_called()
    paged.assert_not_called()


def test_joining_another_runs_ask_pages_nobody(paged, monkeypatch):
    other = SimpleNamespace(idempotency_key="run-proposal:run-0:x", status="pending")
    monkeypatch.setattr(rbr, "_already_asked", lambda *a: True)

    rbr._propose_actions("case-1", "run-1", [PROPOSAL], _service(other))

    paged.assert_not_called()


def _post(monkeypatch, *, enabled, key="rk"):
    post = Mock()
    post.return_value.json.return_value = {"status": "success"}
    monkeypatch.setattr(pagerduty.httpx, "post", post)
    monkeypatch.setattr(
        pagerduty,
        "get_settings",
        lambda: SimpleNamespace(daemon_pagerduty_enabled=enabled),
    )
    monkeypatch.setattr(
        pagerduty, "get_integration_config", lambda name: {"routing_key": key}
    )
    return post


def test_no_page_with_the_flag_off(monkeypatch):
    post = _post(monkeypatch, enabled=False)
    pagerduty.page_proposed_containment(
        "case-1", "run-1", [("Isolate host ws-7", "ws-7")]
    )
    post.assert_not_called()


def test_the_page_names_the_case_and_each_action(monkeypatch):
    post = _post(monkeypatch, enabled=True)
    pagerduty.page_proposed_containment(
        "case-1", "run-1", [("Isolate host ws-7", "ws-7")]
    )

    body = post.call_args.kwargs["json"]
    assert body["routing_key"] == "rk"
    assert body["dedup_key"] == "vigil-run-run-1"
    assert "case-1" in body["payload"]["summary"]
    assert body["payload"]["custom_details"]["proposed_actions"] == [
        "Isolate host ws-7: ws-7"
    ]
    assert post.call_args.kwargs["timeout"] == pagerduty.TIMEOUT_S


def test_a_pagerduty_failure_never_raises(monkeypatch):
    post = _post(monkeypatch, enabled=True)
    post.side_effect = RuntimeError("down")
    pagerduty.page_proposed_containment(
        "case-1", "run-1", [("Isolate host ws-7", "ws-7")]
    )
