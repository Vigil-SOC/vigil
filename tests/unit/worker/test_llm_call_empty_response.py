import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from services.worker.jobs import llm_call

pytestmark = pytest.mark.unit


def test_empty_response_is_logged_and_still_returns_error_dict(caplog):
    # Callers (daemon processor, AI insights) depend on the error-dict contract.
    claude = MagicMock()
    claude.chat.return_value = None
    ctx = {"in_flight": asyncio.Semaphore(1), "claude_service": claude}

    with caplog.at_level(logging.ERROR, logger="services.worker.jobs"):
        result = asyncio.run(llm_call(ctx, "hi", "some-model", 10, None))

    assert result == {"content": "", "type": "error", "error": "Empty response"}
    assert "Empty response" in caplog.text
    assert "API key" in caplog.text
