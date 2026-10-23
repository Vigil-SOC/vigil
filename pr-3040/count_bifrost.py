import asyncio, sys, httpx
from unittest.mock import MagicMock
sys.path.insert(0, ".")
from core.reporting import analytics_service as svc
calls = []
real = httpx.Client
transport = httpx.MockTransport(lambda r: (calls.append(str(r.url)), httpx.Response(200, json={"buckets": []}))[1])
httpx.Client = lambda **kw: real(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
for name in ["_cost_totals", "_cost_group_by_agent", "_cost_group_by_model", "_cost_top_investigations"]:
    if hasattr(svc, name):
        setattr(svc, name, lambda db, f, *a, **k: [])
out = asyncio.run(svc.get_cost_breakdown(MagicMock(), "24h"))
print("keys:", sorted(out))
print("bifrost calls:", calls)
