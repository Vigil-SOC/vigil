"""Read Splunk's scheduled and correlation saved searches, and whether each runs.

Credentials are the ``splunk`` integration's. Listing is REST reads; the Runs
result costs exactly one search per sync, against ``index=_internal``, spent
against the integration's ``max_searches_per_hour`` (Sees, in ``splunk_sees``,
spends the same budget). Nothing is written to Splunk.

A scheduled run's errors are read from two sourcetypes joined on sid: the
scheduler logs a runtime failure as ``success`` (Splunk 10.4) or not at all
(10.6), and only ``splunk_search_messages`` carries the error. A message has
no search name, but its sid does: ``..__RMD5<md5(name)[16:]>_at_<epoch>_<n>``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Set

from core.detections.inventory.readers import _techniques
from core.detections.inventory.record import RuleRecord

logger = logging.getLogger(__name__)

SOURCE = "splunk"
# Searches Vigil may send Splunk an hour when max_searches_per_hour is unset.
DEFAULT_PER_HOUR = 30
# Scheduled searches and correlation searches; Splunk counts with this filter too.
LISTED = "is_scheduled=1 OR action.correlationsearch.enabled=1"
# A run due less than GRACE ago may not be logged and indexed yet, so the window
# is the WINDOW before that.
WINDOW = timedelta(hours=24)
GRACE = timedelta(minutes=10)
# Splunk runs a cron in a timezone Vigil doesn't know: every offset, half-hourly.
_OFFSETS = [timedelta(minutes=30 * n) for n in range(-24, 29)]

# Besides the search, the fields that change what fires or how loud it is.
LOGIC_KEYS = (
    "cron_schedule",
    "dispatch.earliest_time",
    "dispatch.latest_time",
    "dispatch.index_earliest",
    "dispatch.index_latest",
    "counttype",
    "relation",
    "quantity",
    "alert_condition",
    "alert_type",
    "alert_comparator",
    "alert_threshold",
    "alert.severity",
    "alert.suppress",
    "alert.suppress.fields",
    "alert.suppress.period",
    "action.correlationsearch.enabled",
    "action.notable",
    "action.risk",
    "action.notable.param.severity",
    "action.risk.param._risk",
)
# Run state, not the rule: never stored, so it can't look like an edit.
_VOLATILE = ("next_scheduled_time", "triggered_alert_count", "embed.enabled")

# One search: every scheduled run in the window, latest per search and owner,
# with the errors its sid logged. A skipped run has no sid; it is keyed by name
# and time. A run only messages logged has no user: ``who`` is its sid up to the
# hash, which starts ``scheduler__<owner>_``.
RUNS_SPL = r"""search index=_internal
  ((sourcetype=scheduler savedsearch_name=* NOT status IN ("continued", "deferred", "delegated_remote"))
   OR (sourcetype=splunk_search_messages log_level=ERROR sid="scheduler__*"))
| eval run=coalesce(sid, savedsearch_name."@".scheduled_time),
  h=if(sourcetype="scheduler", substr(md5(savedsearch_name), 17, 16),
       replace(sid, "^.*__RMD5([0-9a-f]{16})_at_.*$", "\1")),
  t=coalesce(scheduled_time, tonumber(replace(sid, "^.*_at_(\d+)_.*$", "\1"))),
  err=if(sourcetype="splunk_search_messages", message, null())
| stats max(t) as t values(savedsearch_name) as name values(user) as user
  values(status) as status values(reason) as reason values(err) as errors
  count(eval(sourcetype="scheduler")) as logged by run h app
| eval who=coalesce(user, replace(run, "__RMD5.*$", ""))
| eventstats sum(logged) as scheduler_events
| sort 0 - t
| dedup app h who
| fields app h t name user who status reason errors scheduler_events"""


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _when(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _hours() -> int:
    return int(WINDOW.total_seconds() // 3600)


def name_hash(name: str) -> str:
    """The RMD5 part of a scheduled run's sid."""
    return hashlib.md5(name.encode("utf-8"), usedforsecurity=False).hexdigest()[16:]


def _cron_field(spec: str, low: int, high: int) -> Set[int]:
    out: Set[int] = set()
    for part in spec.split(","):
        body, _, step = part.partition("/")
        if body == "*":
            first, last = low, high
        elif "-" in body:
            first, last = (int(x) for x in body.split("-", 1))
        else:
            first = last = int(body)
            if step:
                last = high
        out.update(range(first, last + 1, int(step or 1)))
    return out


def _parse_cron(cron: str) -> Optional[tuple]:
    fields = cron.split()
    if len(fields) != 5:
        return None
    try:
        minutes, hours, doms, months, dows = (
            _cron_field(f, lo, hi)
            for f, lo, hi in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))
        )
    except ValueError:
        return None
    # cron: with both day fields set, either one matching is enough.
    either = fields[2] != "*" and fields[4] != "*"
    return minutes, hours, doms, months, {d % 7 for d in dows}, either


def _fires(parsed: tuple, start: datetime, end: datetime) -> bool:
    """Whether the parsed cron fires in (start, end], read as wall-clock times."""
    minutes, hours, doms, months, dows, either = parsed
    tick = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while tick <= end:
        dom_ok, dow_ok = tick.day in doms, (tick.weekday() + 1) % 7 in dows
        day_ok = (dom_ok or dow_ok) if either else (dom_ok and dow_ok)
        if tick.month in months and day_ok and tick.hour in hours:
            if tick.minute in minutes:
                return True
            tick += timedelta(minutes=1)
        else:
            tick = tick.replace(minute=0) + timedelta(hours=1)
    return False


def cron_fires(cron: str, start: datetime, end: datetime) -> Optional[bool]:
    """Whether a cron fires in (start, end] whatever splunkd's timezone is.

    True when it fires under every UTC offset, False under none, None when it
    depends on the offset or the cron is not readable.
    """
    parsed = _parse_cron(cron)
    if parsed is None:
        return None
    seen = {_fires(parsed, start + o, end + o) for o in _OFFSETS}
    return seen.pop() if len(seen) == 1 else None


def _annotations(content: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        parsed = json.loads(content.get("action.correlationsearch.annotations") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scheduled(entry: Mapping[str, Any]) -> bool:
    content = entry.get("content") or {}
    return not _truthy(content.get("disabled")) and _truthy(content.get("is_scheduled"))


def runs_of(
    entry: Mapping[str, Any],
    run: Optional[Mapping[str, Any]],
    now: datetime,
    unreadable: Optional[str] = None,
    started: Optional[datetime] = None,
) -> Optional[Dict[str, Optional[str]]]:
    """``{result, reason}`` from the search's latest run in the window, or None.

    A run from before the search's last edit ran other logic, and says nothing.
    Nothing runs while Splunk is down, so the window starts no earlier than
    ``started`` (splunkd's start).
    """
    if not scheduled(entry):
        return None
    content = entry["content"]
    if str(content.get("dispatch.earliest_time") or "").startswith("rt"):
        # A real-time search runs continuously; the scheduler logs no runs of it.
        return {
            "result": "unknown",
            "reason": "real-time search: no scheduled runs to read",
        }
    if unreadable:
        return {"result": "unknown", "reason": unreadable}
    updated = _when(entry.get("updated")) or datetime.min.replace(tzinfo=timezone.utc)
    ran = (
        datetime.fromtimestamp(float(run["t"]), timezone.utc)
        if run and run.get("t")
        else None
    )
    if ran is None or ran < updated:
        end = now - GRACE
        start = max(end - WINDOW, updated, started or updated)
        fires = cron_fires(str(content.get("cron_schedule") or ""), start, end)
        if fires:
            return {"result": "fail", "reason": f"no run in the last {_hours()}h"}
        if fires is None or start > end - WINDOW or ran is not None:
            return None  # not due yet whatever the timezone, or edited/restarted since
        return {
            "result": "unknown",
            "reason": f"scheduled less often than the {_hours()}h window",
        }
    errors = _list(run.get("errors"))
    if errors:
        return {"result": "fail", "reason": "; ".join(errors)}
    statuses = _list(run.get("status"))
    if "success" in statuses or "delegated_remote_completion" in statuses:
        return {"result": "pass", "reason": None}
    reasons = "; ".join(_list(run.get("reason")))
    status = ", ".join(statuses) or "unknown"
    return {"result": "fail", "reason": f"{status}: {reasons}" if reasons else status}


def record_of(
    entry: Mapping[str, Any], runs: Optional[Dict[str, Optional[str]]]
) -> RuleRecord:
    from core.integrations.splunk.client import saved_search_key

    content = entry.get("content") or {}
    acl = entry.get("acl") or {}
    return RuleRecord(
        native_key=saved_search_key(entry),
        # Kept as Splunk has it: 'ESCU - <name> - Rule' is how it links.
        name=str(entry.get("name")),
        language="splunk",
        path=None,
        query=str(content.get("search") or ""),
        techniques=_techniques(_annotations(content).get("mitre_attack") or []),
        logic={k: content.get(k) for k in LOGIC_KEYS},
        content={
            "name": entry.get("name"),
            "app": acl.get("app"),
            "owner": acl.get("owner"),
            "sharing": acl.get("sharing"),
            "updated": entry.get("updated"),
            **{k: v for k, v in content.items() if k not in _VOLATILE},
        },
        status="disabled" if _truthy(content.get("disabled")) else "active",
        runs=runs,
    )


def latest_run(
    entry: Mapping[str, Any], rows: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The latest of the rows (one per app, name hash and owner) that are this search's."""
    acl = entry.get("acl") or {}
    owner, h = str(acl.get("owner")), name_hash(str(entry.get("name")))
    mine = [
        row
        for row in rows
        if row.get("app") == acl.get("app")
        and row.get("h") == h
        and (
            row.get("user") == owner
            if row.get("user")
            else str(row.get("who") or "").startswith(f"scheduler__{owner}_")
        )
    ]
    return max(mine, key=lambda r: float(r.get("t") or 0), default=None)


def service_of(config: Mapping[str, Any]) -> Any:
    from core.integrations.splunk.client import SplunkService

    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    return SplunkService(
        server_url=config["server_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )


def per_hour_of(config: Mapping[str, Any]) -> int:
    """The splunk integration's max_searches_per_hour: absent 30, never below 0."""
    per_hour = config.get("max_searches_per_hour")
    return DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)


def read_splunk(config: Mapping[str, Any]) -> List[RuleRecord]:
    """Every listed saved search with its Runs result, or raise on a short list.

    The Runs search spends one of the splunk budget's searches; when the hour's
    are spent every record has no Runs result, so the stored ones are kept.
    """
    from core.integrations._base import search_budget
    from core.integrations.splunk.client import SplunkSearchError

    service = service_of(config)
    entries = service.list_saved_searches(LISTED)
    now = datetime.now(timezone.utc)
    started = service.started_at()
    rows: List[Dict[str, Any]] = []
    unreadable: Optional[str] = None
    if any(scheduled(e) for e in entries):
        if service.can_search_index("_internal") is False:
            unreadable = f"{service.username} cannot search index=_internal"
        elif not search_budget.spend(SOURCE, per_hour_of(config)):
            logger.info("detection rules: Splunk Runs not searched, budget spent")
            return [record_of(e, None) for e in entries]
        else:
            try:
                rows = service.search(
                    RUNS_SPL,
                    earliest_time=f"-{int((WINDOW + GRACE).total_seconds() // 60)}m",
                    max_count=0,
                ).results
            except SplunkSearchError as exc:
                unreadable = f"scheduler search failed: {exc}"
            # Searches are scheduled, yet the window holds no scheduler event: the
            # user can't see _internal (Splunk returns no events, not an error).
            if not unreadable and not int(
                (rows or [{}])[0].get("scheduler_events") or 0
            ):
                unreadable = (
                    "no scheduler events in index=_internal; it may not be readable"
                )
    return [
        record_of(e, runs_of(e, latest_run(e, rows), now, unreadable, started))
        for e in entries
    ]
