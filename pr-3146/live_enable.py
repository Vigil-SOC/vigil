import asyncio, json, os, pathlib
from services.daemon.config import SchedulerConfig
from services.daemon.scheduler import TaskScheduler

cfg = pathlib.Path(os.environ["VIGIL_DIR"]) / "integrations_config.json"
cfg.write_text(json.dumps({"enabled_integrations": [], "integrations": {}}))
s = TaskScheduler(SchedulerConfig())
names = [t.name for t in s._tasks]
print("tasks at start (disabled):", names)
task = next((t for t in s._tasks if t.name == "threat_feed_poll"), None)
if task:
    print("tick while disabled:", asyncio.run(task.func()))
cfg.write_text(json.dumps({"enabled_integrations": ["cloudforce_one"], "integrations": {"cloudforce_one": {}}}))
print("operator enabled cloudforce_one in integrations_config.json")
if task is None:
    raise SystemExit("FAIL: threat_feed_poll not registered, enabling it cannot start polling")
print("tick after enable:", asyncio.run(task.func()))
