import asyncio, inspect, sys
sys.path.insert(0, ".")
from core.federation import registry
from core.federation.runner import FederationRunner
from services.daemon.poller import DataPoller, PollingConfig

async def main():
    adapters = registry.list_adapters()
    print("adapters:", sorted(a.name for a in adapters))
    for a in adapters:
        params = list(inspect.signature(a.fetch).parameters)
        assert "since" not in params, (a.name, params)
        res = await a.fetch(cursor={}, max_items=1)
        print(f"{a.name}: params={params} configured={a.is_configured()} -> findings={len(res.findings)} cursor_keys={sorted(res.cursor)}")
    r = FederationRunner(output_queue=None)
    print("runner attrs:", sorted(k for k in vars(r)))
    p = DataPoller(PollingConfig())
    for src in DataPoller._INGESTION_SOURCES:
        print(f"poller _{src}_state:", type(getattr(p, f"_{src}_state")).__name__,
              f"_{src}_service:", getattr(p, f"_{src}_service"))
    print("poller.stats keys:", sorted(p.stats))

asyncio.run(main())
