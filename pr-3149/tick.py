import asyncio, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, ".")
from services.daemon.config import SchedulerConfig
from services.daemon import scheduler as m

async def main():
    s = m.TaskScheduler(SchedulerConfig())
    names = [t.name for t in s._tasks]
    print("tasks", names)
    print("stats", sorted(s.stats))
    ran = []
    for t in s._tasks:
        t.func = AsyncMock(side_effect=lambda n=t.name: ran.append(n))
    stop = asyncio.Event()
    with patch.object(s, "_init_services"), patch.object(m, "refresh_gateway_rates", AsyncMock()), \
         patch.object(m, "run_gateway_rates_refresher", AsyncMock()), patch.object(m, "_last_scheduled_hunt_at", lambda: None):
        task = asyncio.create_task(s.run(stop))
        await asyncio.sleep(0.5); stop.set(); await task
    print("ran", ran, "tasks_run", s.stats["tasks_run"], "errors", s.stats["errors"])
    assert {"threat_hunt", "cleanup", "probe_sweep"} <= set(ran), ran
    assert "health_check" not in names and "weekly_report" not in names
    assert ran == names and s.stats["errors"] == 0
    assert hasattr(s, "_get_top_techniques") and not hasattr(s, "_claude_service")
    print("OK")
asyncio.run(main())
