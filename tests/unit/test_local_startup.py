from backend.main import _mcp_auto_connect_enabled


def test_mcp_auto_connect_is_off_by_default_in_dev(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("MCP_AUTO_CONNECT_ON_STARTUP", raising=False)

    assert _mcp_auto_connect_enabled() is False


def test_mcp_auto_connect_can_be_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("MCP_AUTO_CONNECT_ON_STARTUP", "true")

    assert _mcp_auto_connect_enabled() is True
