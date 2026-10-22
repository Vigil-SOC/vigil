import asyncio, sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from services.api.routers import claude

class Reg:
    def resolve_model_for_component(self, category, agent_override=None):
        return ("gemini", "gemini-flash-latest")
claude.get_registry = lambda: Reg()
claude.provider_for = lambda pid: SimpleNamespace(provider_type="gemini", pid=pid)
claude.model_for = lambda p, m: m
claude.live_mcp_tools = lambda r: []
saved = {}
claude._persist_chat_turn = lambda **kw: saved.update(kw)
claude._internal_headers = lambda: {}
sent = {}
import httpx
class Up:
    status_code = 200
    async def aiter_lines(self):
        yield 'data: {"text": "ok"}'
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
class C:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def stream(self, method, url, json=None, headers=None):
        sent.update(json); return Up()
httpx.AsyncClient = C

body = {"messages": [{"role": "user", "content": "hi"}],
        "model": "ollama-local::llama3.1:8b", "system_prompt": "EVIL OVERRIDE", "max_tokens": 1}
req = claude.ChatRequest.model_validate(body)
print("fields after parse:", sorted(req.model_dump()))
async def main():
    r = await claude.chat_stream(req, current_user=SimpleNamespace(username="u", user_id="u1"),
                                 registry=MagicMock(), workflows=MagicMock())
    async for _ in r.body_iterator: pass
asyncio.run(main())
print("config model line:", [l for l in sent["config"].splitlines() if "model" in l][:1] if isinstance(sent["config"], str) else sent["config"])
print("EVIL in system_prompt:", "EVIL" in sent["system_prompt"])
print("persisted model:", saved["model"])
assert "EVIL" not in sent["system_prompt"] and saved["model"] == "gemini-flash-latest"
print("OK")
