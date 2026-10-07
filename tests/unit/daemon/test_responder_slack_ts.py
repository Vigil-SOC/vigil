"""The Slack escalation footer timestamp is a true epoch, whatever the host TZ."""

import time
from unittest.mock import Mock, patch

import pytest

from services.daemon.config import EscalationConfig, ResponseConfig
from services.daemon.responder import AutonomousResponder


@pytest.fixture
def non_utc_tz(monkeypatch):
    """Run under a host timezone well away from UTC, then restore it."""
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.asyncio
async def test_slack_attachment_ts_is_epoch_on_non_utc_host(non_utc_tz):
    # Guard: the fixture really did move the host off UTC.
    assert time.timezone != 0

    responder = AutonomousResponder(
        ResponseConfig(),
        EscalationConfig(),
        response_service=Mock(),
        approvals=Mock(),
    )
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True}

    with (
        patch(
            "core.config.get_integration_config",
            return_value={"bot_token": "xoxb-test"},
        ),
        patch("httpx.post", return_value=response) as post,
    ):
        await responder._send_slack_alert("message", "high")

    ts = post.call_args.kwargs["json"]["attachments"][0]["ts"]
    assert abs(ts - time.time()) < 5
