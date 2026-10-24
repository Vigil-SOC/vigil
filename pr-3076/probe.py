import asyncio, sys
sys.path.insert(0, sys.argv[1])
from core.integrations.mcp.service import MCPService
from core.integrations.mcp.client import MCPClient
svc = MCPService()
c = MCPClient(svc)
names = list(svc.servers)
print("servers:", len(names))
for n in names:
    r = c.setup_required(n)
    assert r is None or (isinstance(r, str) and r), (n, r)
    c.was_dialled(n); c.held_for_setup(n); c.get_missing_credentials(n); c.get_last_error(n)
    if r: print("held:", n, "->", r[:60])
print("retry on fresh client:", asyncio.run(c.retry_dormant_if_ready()))
print("OK")
svc._detection_rules.saved_mcp_env_vars = lambda: {}
from unittest.mock import patch
with patch("core.integrations.mcp.client.MCP_AVAILABLE", True):
    ok = asyncio.run(c.connect_to_server("security-detections", skip_enabled_check=True))
print("no-rules connect:", ok, "held:", c.held_for_setup("security-detections"), "reason:", c.setup_required("security-detections"))
assert ok is False and c.held_for_setup("security-detections")
print("retry while held:", asyncio.run(c.retry_dormant_if_ready()))
print("OK2")
