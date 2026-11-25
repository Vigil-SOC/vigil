"""The close-time ceiling for a re-decision run's search tools (#5092).

One place, called from ``tools_router._run`` before the tool. The tools honor
the arguments this sets (Splunk ``latest``, Elastic and OpenSearch
``time_gte``/``time_lte``, Security Lake ``end``) and otherwise search as they
always have. Other query text is left alone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_TOOLS = (
    "splunk_execute",
    "elastic_search_logs",
    "opensearch_search_logs",
    "security_lake_query",
)
_SPAN = re.compile(r"^(\d{1,6})([smhdwMy])$")
_UNIT = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "M": timedelta(days=30),
    "y": timedelta(days=365),
}
_LATEST = re.compile(
    r"""(?<![A-Za-z0-9_])latest\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s|"']+))""",
    re.IGNORECASE,
)
_RELATIVE = re.compile(
    r"^(?P<sign>[-+])(?P<num>\d+)(?P<unit>s|min|m|h|d|w|mon|y)"
    r"(?:@(?P<snap>s|m|h|d|w|mon|y))?$",
    re.IGNORECASE,
)
_SPLUNK_ABS = (
    "%m/%d/%Y:%H:%M:%S.%f",
    "%m/%d/%Y:%H:%M:%S",
    "%m/%d/%Y",
)


class SearchAfterClose(Exception):
    """An inline Splunk ``latest`` resolves after the close."""


def _leaf(tool: str) -> str:
    for name in _TOOLS:
        if tool == name or tool.endswith("_" + name):
            return name
    return ""


def _duration(span: str) -> timedelta:
    match = _SPAN.match(span.strip())
    if match is None:
        raise SearchAfterClose(
            "time_range is not a duration this search can hold to the close"
        )
    count, unit = match.groups()
    return int(count) * _UNIT[unit]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _snap(moment: datetime, unit: str) -> datetime:
    unit = unit.lower()
    if unit == "s":
        return moment.replace(microsecond=0)
    if unit == "m":
        return moment.replace(second=0, microsecond=0)
    if unit == "h":
        return moment.replace(minute=0, second=0, microsecond=0)
    if unit == "d":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "w":
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=midnight.weekday())
    if unit == "mon":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "y":
        return moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return moment


def _relative_unit(unit: str) -> timedelta:
    unit = unit.lower()
    if unit == "min":
        unit = "m"
    if unit == "mon":
        return _UNIT["M"]
    return _UNIT[unit]


def _absolute(value: str, now: datetime) -> Optional[datetime]:
    text = value.strip()
    if not text:
        return None
    folded = text.lower()
    if folded in ("now", "now()"):
        return now
    if folded.startswith("@"):
        return _snap(now, folded[1:])
    relative = _RELATIVE.match(text)
    if relative is not None:
        sign = -1 if relative.group("sign") == "-" else 1
        moment = now + sign * int(relative.group("num")) * _relative_unit(
            relative.group("unit")
        )
        snap = relative.group("snap")
        return moment if snap is None else _snap(moment, snap)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        stamp = float(text)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    for pattern in _SPLUNK_ABS:
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _inline_latest_after(query: str, close: datetime) -> bool:
    """True when a ``latest=`` in the SPL is after the close, or cannot be read."""
    now = datetime.now(timezone.utc)
    for match in _LATEST.finditer(query or ""):
        raw = next(group for group in match.groups() if group is not None)
        moment = _absolute(raw, now)
        if moment is None or moment > close:
            return True
    return False


def _cap_end(value: Any, close: datetime) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return value
    moment = _absolute(str(value), datetime.now(timezone.utc))
    if moment is None or moment > close:
        return _iso(close)
    return value


def cap_search_args(tool: str, args: Dict[str, Any], close: datetime) -> Dict[str, Any]:
    """The arguments to send, held so the search ends at ``close``.

    Raises ``SearchAfterClose`` when a Splunk query names a ``latest`` after
    the close. Every other argument is unchanged.
    """
    name = _leaf(tool)
    if not name:
        return args
    capped = dict(args)
    if name == "splunk_execute":
        if _inline_latest_after(str(capped.get("spl_query") or ""), close):
            raise SearchAfterClose("latest is after the close")
        capped["latest"] = f"{close.timestamp():.3f}"
        return capped
    if name in ("elastic_search_logs", "opensearch_search_logs"):
        span = str(capped.get("time_range") or "24h")
        start = close - _duration(span)
        capped["time_gte"] = _iso(start)
        capped["time_lte"] = _iso(close)
        return capped
    capped["end"] = _cap_end(capped.get("end"), close)
    return capped
