import httpx
import pytest

from services import local_ai_recovery as recovery


def test_local_recovery_requires_dev_mode_and_loopback_gateway(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("BIFROST_URL", "http://localhost:8080")
    monkeypatch.setattr(recovery, "get_ai_operations_setting", lambda key, default: True)
    assert recovery.local_bifrost_recovery_enabled() is True

    monkeypatch.setenv("BIFROST_URL", "http://bifrost:8080")
    assert recovery.local_bifrost_recovery_enabled() is False

    monkeypatch.setenv("BIFROST_URL", "http://localhost:8080")
    monkeypatch.setenv("DEV_MODE", "false")
    assert recovery.local_bifrost_recovery_enabled() is False


def test_retry_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(recovery, "get_ai_operations_setting", lambda key, default: 9)
    assert recovery.local_bifrost_recovery_retry_limit() == 3

    monkeypatch.setattr(recovery, "get_ai_operations_setting", lambda key, default: -5)
    assert recovery.local_bifrost_recovery_retry_limit() == 0

    monkeypatch.setattr(recovery, "get_ai_operations_setting", lambda key, default: "x")
    assert recovery.local_bifrost_recovery_retry_limit() == 1


def test_connection_error_classifier_does_not_retry_provider_errors():
    assert recovery.is_gateway_connection_error(recovery.httpx.ConnectError("offline"))
    assert recovery.is_gateway_connection_error(RuntimeError("offline")) is False


def test_connection_error_classifier_recognizes_openai_timeouts():
    openai = pytest.importorskip("openai")
    request = httpx.Request("POST", "http://localhost:8080/v1/chat/completions")
    assert recovery.is_gateway_connection_error(
        openai.APIConnectionError(request=request)
    )
    assert recovery.is_gateway_connection_error(
        openai.APITimeoutError(request=request)
    )


@pytest.mark.asyncio
async def test_recovery_retries_without_restart_when_gateway_is_already_healthy(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("BIFROST_URL", "http://localhost:8080")
    monkeypatch.setattr(recovery, "get_ai_operations_setting", lambda key, default: True)

    async def healthy():
        return True

    async def should_not_restart():
        raise AssertionError("healthy Bifrost must not be restarted")

    monkeypatch.setattr(recovery, "_bifrost_healthy", healthy)
    monkeypatch.setattr(recovery, "_restart_bifrost", should_not_restart)

    result = await recovery.recover_local_bifrost()

    assert result.ready is True
    assert result.restarted is False
