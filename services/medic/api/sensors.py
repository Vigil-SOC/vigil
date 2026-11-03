"""The body of `GET /v1/sensors` (X2 `SensorList`), built once per cycle by the
main loop from the scheduler's per-sensor view and the engine's open
sensor-blind incidents. The listener only serves the published copy.
"""

from __future__ import annotations

from typing import Any

from services.medic.app.gap import iso

BLIND = "watcher.sensor-blind"  # engine.core.BLIND; not imported (engine stays out)
_OPEN = ("firing", "resolving")
MAX_ITEMS = 128


def build_sensors(
    states: list[dict[str, Any]], engine_status: list[dict[str, Any]], *, now: float
) -> dict[str, Any]:
    """`states` is `Scheduler.sensor_states()`; `engine_status` is `Engine.status()`."""
    watcher = {
        row["group"].get("sensor"): row["incident_id"]
        for row in engine_status
        if row["rule"] == BLIND and row["state"] in _OPEN and row["incident_id"]
    }
    items = [{**s, "watcher_incident_id": watcher.get(s["id"])} for s in states]
    return {"as_of": iso(now), "items": items[:MAX_ITEMS]}
