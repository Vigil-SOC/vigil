"""Bounded projection of external scenario events, separate from Findings."""

from datetime import datetime, timezone
from ipaddress import IPv4Address
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:2000]
    return ""


def _time(value: Any) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def project_faults(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allowlisted fields only; preserve zero values and unknown event times."""
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = _text(event.get("eventId") or event.get("event_id"))
        if not event_id:
            continue
        properties = event.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        measurement = properties.get("rawMeasurement")
        measurement = measurement if isinstance(measurement, dict) else {}
        ips = event.get("ip4s")
        addresses: list[str] = []
        for value in ips[:50] if isinstance(ips, list) else []:
            try:
                if isinstance(value, str):
                    addresses.append(str(IPv4Address(value)))
            except ValueError:
                pass
        fault = {
            "event_id": event_id,
            "occurred_at": _time(event.get("date", event.get("timestamp"))),
            "label": _text(event.get("label") or properties.get("label")),
            "description": _text(
                event.get("description") or properties.get("description")
            ),
            "source_level": _text(event.get("level")),
            "ip4s": list(dict.fromkeys(addresses)),
            "measurement": {
                key: _text(measurement.get(key))
                for key in ("type", "initiator", "target", "key", "value")
                if measurement.get(key) is not None
            },
        }
        previous = unique.get(event_id)
        if previous is None or (fault["occurred_at"] or "") > (
            previous["occurred_at"] or ""
        ):
            unique[event_id] = fault
    return sorted(
        unique.values(), key=lambda fault: fault["occurred_at"] or "", reverse=True
    )
