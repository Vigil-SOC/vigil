"""Each LLM-path outage keeps its own entry, reminder, and recovery templates."""

from __future__ import annotations

import logging

import pytest

from core.llm import outage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state():
    outage._failures.clear()
    yield
    outage._failures.clear()


def _args(record: logging.LogRecord) -> tuple:
    if isinstance(record.args, tuple):
        return record.args
    if record.args:
        return (record.args,)
    return ()


def test_two_outages_log_distinct_templates_and_keep_the_message_out_of_args(caplog):
    log = logging.getLogger("outage.templates")
    gateway = ConnectionError("redis down")
    mirror = OSError("bifrost down")
    with caplog.at_level(logging.INFO, logger="outage.templates"):
        for _ in range(outage.REMINDER_EVERY):
            outage.report_outage(log, "gateway", "gateway down: %s", gateway)
            outage.report_outage(log, "mirror", "mirror down: %s", mirror)
        assert outage.report_recovered(log, "gateway", "gateway up: %s", gateway)
        assert outage.report_recovered(log, "mirror", "mirror up: %s", mirror)
        assert outage.report_recovered(log, "absent", "gateway up: %s", gateway) is False

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(errors) == 4
    assert len(infos) == 2

    phases = {
        "entry": ("gateway down: %s", "mirror down: %s"),
        "reminder": (
            "gateway down: %s (still failing, %d failures)",
            "mirror down: %s (still failing, %d failures)",
        ),
        "recovery": (
            "gateway up: %s; recovered after %d failures",
            "mirror up: %s; recovered after %d failures",
        ),
    }
    by_msg = {r.msg: r for r in errors + infos}
    for left, right in phases.values():
        assert left != right
        assert left in by_msg and right in by_msg

    gateway_reminder = by_msg[phases["reminder"][0]]
    assert gateway_reminder.args == (gateway, outage.REMINDER_EVERY)
    assert gateway_reminder.getMessage() == (
        f"gateway down: {gateway} (still failing, {outage.REMINDER_EVERY} failures)"
    )
    assert by_msg[phases["recovery"][0]].args == (gateway, outage.REMINDER_EVERY)
    assert by_msg[phases["entry"][0]].args == (gateway,)

    for record in errors + infos:
        assert all(arg not in (record.msg, record.getMessage()) for arg in _args(record))
        assert "gateway down:" not in _args(record)
        assert "mirror down:" not in _args(record)
        assert "gateway up:" not in _args(record)
        assert "mirror up:" not in _args(record)
