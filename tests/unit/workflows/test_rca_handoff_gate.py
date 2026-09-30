"""The RCA-on-handoff gate: a proven threat hunt tees up a root-cause-analysis run
that parks for an operator to permit the trace; an RCA's own handoff spawns nothing
(no loop)."""

from unittest.mock import AsyncMock, Mock, patch

from core.workflows import run_bridge_router as rbr
from core.workflows.run_bridge_router import (
    TerminalHandoff,
    _source_is_hunt,
    _start_root_cause,
)
from core.workflows.workflows_service import WorkflowsService, _nothing_to_run

HANDOFF = TerminalHandoff(
    case_id="case-abc",
    title="IR case case-abc — threat-hunt",
    markdown="The internal host FYODOR-L (192.168.70.186) is beaconing to 45.77.53.176:443.",
)


def _run_service(existing=None):
    """``existing`` is the run row a prior tee-up left, or None when there is none."""
    svc = Mock()
    svc.find_run_by_trigger.return_value = existing
    return svc


def _with_ledger_kind(run_kind, raises=None):
    """Patch the lazily-imported ledger read so it reports run_kind for any run."""
    reader = Mock(side_effect=raises) if raises else Mock(return_value=run_kind)
    return patch("core.workflows.run_resume.run_kind_of", reader)


class TestTheSourceGuard:
    """Answered off the ledger's first event, which is the value the worker itself
    decided to push the handoff early from. The run's workflow row is a different
    question: a hunt started from file paths is filed under 'hunt' rather than
    'threat-hunt', so resolving a definition from it finds nothing."""

    def test_a_threat_hunt_source_is_a_hunt(self):
        with _with_ledger_kind("hunt"):
            assert _source_is_hunt("run-1") is True

    def test_a_root_cause_source_is_not_a_hunt(self):
        # An RCA's own handoff must not spawn another RCA.
        with _with_ledger_kind("root_cause"):
            assert _source_is_hunt("run-2") is False

    def test_a_run_with_no_ledger_is_not_a_hunt(self):
        with _with_ledger_kind(None):
            assert _source_is_hunt("run-3") is False

    def test_an_unreadable_ledger_is_not_a_hunt(self):
        with _with_ledger_kind(None, raises=RuntimeError("db gone")):
            assert _source_is_hunt("run-4") is False


class TestTeeingUpTheRootCause:
    def test_a_hunt_handoff_enqueues_exactly_one_rca_on_the_confirmed_finding(self):
        # _process_handoff owns the dedup guard, so _start_root_cause is the sole
        # tee-up and takes no run_service of its own.
        enqueue = AsyncMock(return_value={"success": True, "run_id": "r-1"})
        with patch.object(rbr, "_enqueue_root_cause", enqueue):
            _start_root_cause("run-1", HANDOFF, "case-opened")

        enqueue.assert_awaited_once()
        params, triggered_by = enqueue.await_args.args
        assert triggered_by == "handoff:run-1:case-abc"
        # Only what the run reads: the finding to trace and the case to report onto.
        # No synthesized hypothesis -- a trace has none to state, and triggered_by
        # above already carries which run this traces back from.
        assert set(params) == {"context", "case_id"}
        # The finding travels verbatim in context, where the run reads it on turn 0.
        assert "FYODOR-L" in params["context"]
        # No approve_hypotheses pinned, so the workflow's rca_permit policy governs.
        assert "approve_hypotheses" not in params
        # Files back onto the IR case the hunt opened.
        assert params["case_id"] == "case-opened"


class TestProcessHandoff:
    """A handoff is filed the moment it lands and again on the terminal that carries
    it, so processing one must open its case and tee its RCA exactly once."""

    def test_a_hunt_handoff_opens_a_case_and_tees_the_rca(self):
        svc = _run_service()
        with patch.object(
            rbr, "_open_case", return_value="case-opened"
        ) as open_case, patch.object(rbr, "_start_root_cause") as start_rca:
            rbr._process_handoff("run-1", HANDOFF, "", True, svc)
        open_case.assert_called_once()
        start_rca.assert_called_once()
        # The RCA is teed onto the case that was just opened, not the agent-side id.
        assert start_rca.call_args.args[2] == "case-opened"

    def test_a_second_arrival_tees_no_second_rca(self):
        # The /handoff push already teed the RCA; the terminal re-carries the same
        # handoff and must tee nothing further. _open_case is still called -- it
        # keys on the handoff and finds the case the first arrival opened, which is
        # its own gate rather than this one's.
        svc = _run_service(existing={"run_id": "r-1", "status": "running"})
        with patch.object(rbr, "_open_case", return_value="case-opened"), patch.object(
            rbr, "_start_root_cause"
        ) as start_rca:
            rbr._process_handoff("run-1", HANDOFF, "", True, svc)
        start_rca.assert_not_called()

    def test_a_non_hunt_handoff_opens_a_case_but_tees_nothing(self):
        # A root-cause run's own handoff opens its case and spawns no further RCA.
        svc = _run_service()
        with patch.object(
            rbr, "_open_case", return_value="case-opened"
        ) as open_case, patch.object(rbr, "_start_root_cause") as start_rca:
            rbr._process_handoff("run-2", HANDOFF, "", False, svc)
        open_case.assert_called_once()
        start_rca.assert_not_called()


def test_what_the_tee_up_sends_is_something_a_trace_can_start_from():
    # The run is refused unless it names a finding, a case, or describes one.
    params = {"context": rbr._rca_context(HANDOFF), "case_id": "case-opened"}
    rca = WorkflowsService().get_workflow("root-cause-analysis")
    assert _nothing_to_run(rca, params) == ""
    assert _nothing_to_run(rca, {}) == "subject"
    assert _nothing_to_run(rca, {"hypothesis": "anything at all"}) == "subject"
