# Stopping a run. Cancel used to write workflow_runs.status and nothing else, so
# the worker kept spending and later overwrote the row with its own terminal --
# an operator's only working stop was killing the process.

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from core.agents.directives import RunAlreadyEnded, UnknownRun
from core.workflows import run_cancel

pytestmark = pytest.mark.unit

RUN = "2906b7c5-1574-4d78-a406-e4ab362a3a94"


class _Session:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestAskingTheRunToStop:
    def _queue(self, monkeypatch, raises=None):
        seen = {}

        def _enqueue(session, run_id, kind, body, actor, fields=None):
            if raises is not None:
                raise raises
            seen.update(run_id=run_id, kind=kind, body=body, actor=actor)
            return {}

        monkeypatch.setattr(run_cancel, "enqueue_directive", _enqueue)
        monkeypatch.setattr(run_cancel, "get_db_session", lambda: _Session())
        return seen

    def test_queues_an_abort_the_run_can_act_on(self, monkeypatch):
        seen = self._queue(monkeypatch)

        assert run_cancel.request_stop(RUN, "stop it", "matt") is True
        assert seen["kind"] == "abort"
        assert seen["run_id"] == RUN
        assert seen["actor"] == "matt"

    # The zombie-row case, which is exactly where cancel already worked: there is
    # no ledger to abort, and the row still wants finalising by the caller.
    def test_reports_no_abort_for_a_run_with_no_ledger(self, monkeypatch):
        self._queue(monkeypatch, raises=UnknownRun("no such run"))
        assert run_cancel.request_stop(RUN, "stop it", "matt") is False

    def test_reports_no_abort_for_a_run_that_already_ended(self, monkeypatch):
        self._queue(monkeypatch, raises=RunAlreadyEnded("ended"))
        assert run_cancel.request_stop(RUN, "stop it", "matt") is False

    def test_refuses_something_that_is_not_a_run_id(self, monkeypatch):
        self._queue(monkeypatch)
        assert run_cancel.request_stop("not-a-uuid", "stop", "matt") is False


class TestTheBackstop:
    # worker.ts short-circuits when a terminal exists, so writing one stops the
    # next attempt and stops the sweeper re-enqueuing a failing run forever.
    def _rows(self, monkeypatch, written):
        statements = []

        class _S(_Session):
            def execute(self, statement, params=None):
                statements.append(params)

                class _R:
                    def one_or_none(_self):
                        return written

                return _R()

        monkeypatch.setattr(run_cancel, "get_db_session", lambda: _S())
        return statements

    def test_writes_a_terminal_when_the_run_did_not_stop_itself(self, monkeypatch):
        statements = self._rows(monkeypatch, written=(7,))

        assert run_cancel.force_terminal(RUN, "stopped from the console") is True
        assert '"outcome": "aborted"' in statements[0]["payload"]

    # The SQL refuses the insert when a terminal is already there, so a run that
    # settled itself keeps its own outcome and its report.
    def test_writes_nothing_when_the_run_settled_first(self, monkeypatch):
        self._rows(monkeypatch, written=None)
        assert run_cancel.force_terminal(RUN, "stopped") is False

    def test_survives_a_database_that_will_not_answer(self, monkeypatch):
        def _boom():
            raise RuntimeError("no database")

        monkeypatch.setattr(run_cancel, "get_db_session", _boom)
        assert run_cancel.force_terminal(RUN, "stopped") is False


class TestTheWholeStop:
    @pytest.mark.asyncio
    async def test_escalates_behind_a_queued_abort(self, monkeypatch):
        monkeypatch.setattr(run_cancel, "request_stop", lambda *a: True)
        forced = []
        monkeypatch.setattr(
            run_cancel, "force_terminal", lambda run_id, reason: forced.append(run_id)
        )

        with patch.object(run_cancel, "ESCALATE_AFTER_S", 0.0):
            answer = run_cancel.stop_run(RUN, "stop it", "matt")
            task = run_cancel.escalate_later(RUN, "stop it", delay=0.0)
            if task is not None:
                await task

        assert answer == {"abort_queued": True}
        assert forced == [RUN]

    # Nothing to abort means nothing to escalate: the caller finalises the row
    # and a terminal written for a run with no ledger would be a second bug.
    @pytest.mark.asyncio
    async def test_does_not_escalate_when_there_was_nothing_to_abort(self, monkeypatch):
        monkeypatch.setattr(run_cancel, "request_stop", lambda *a: False)
        scheduled = []
        monkeypatch.setattr(
            run_cancel, "escalate_later", lambda *a, **k: scheduled.append(a)
        )

        assert run_cancel.stop_run(RUN, "stop", "matt") == {"abort_queued": False}
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_holds_a_reference_so_the_escalation_is_not_collected(
        self, monkeypatch
    ):
        monkeypatch.setattr(run_cancel, "force_terminal", lambda *a: None)
        task = run_cancel.escalate_later(RUN, "stop", delay=0.01)

        assert task in run_cancel._pending
        await asyncio.sleep(0.05)
        assert task not in run_cancel._pending
