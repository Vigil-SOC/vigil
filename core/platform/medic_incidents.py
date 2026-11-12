"""Incident notices for holders of medic.admin (#4366).

The status watch commits first. This pass then reads Medic, and under the
same watch lock writes the open count, the sent rows and the bell rows.
Mail goes out after that commit. A failure here does not roll back a status
notice already committed. The lock is not held across HTTP or SMTP.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import text

from core.config import Settings, get_settings
from core.platform import medic_last_seen, notices
from core.platform.medic_status import medic_enabled
from core.storage.connection import get_db_session
from core.storage.models import (
    CaseNotification,
    MedicIncidentNotice,
    MedicLastSeen,
    MedicWatchState,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

# Decision 12: wait out the pack's longest `for`, and never past seventy-five
# minutes from the start marker.
_AFTER = timedelta(minutes=15)
_CAP = timedelta(minutes=75)
_KIND = "medic_incident"
_MEDIC = "/medic"
_BUNDLE = "sh scripts/vigil-support/vigil-support.sh"
_NO_FIX = "No fix text for this incident; open it in Medic"
_OPEN_CONSOLE = "Open Medic in the Vigil console"
_INCIDENT = re.compile(r"inc_[0-9A-Za-z]{8,32}")
_SENSOR = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z"
)
_PAGES = 100
_STATES = frozenset({"firing", "resolving", "held", "suppressed", "resolved"})
_REASONS = frozenset({"rule", "lane1_gate_failed", "unknown_signature"})
_SUBJECTS = frozenset({"vigil", "watcher"})
_FIXED = frozenset({"lane1_gate_failed", "unknown_signature"})
_ROUTED = frozenset({"firing", "resolving"})
_SENSOR_BLIND = "watcher.sensor-blind"
_RECORDING = "watcher.recording-stopped"
_STORE_RESET = "watcher.store-reset"
_POLICY = "watcher.policy-unenforced"
_HOW = {
    "cleared": "The fault cleared.",
    "rule_retired": "The rule was retired.",
    "group_retired": "The group was retired.",
}
# The status watch announces a red or watching word after this many polls.
_CONFIRM = 2
_GATE = "A fix rule didn't pass Medic's safety check, so no fix was offered."
_UNKNOWN = "Medic saw a signature it does not recognise, so no fix was offered."

Fetch = Any  # (path) -> (status or None, body)


@dataclass
class _Item:
    id: str
    reopen: int
    state: str
    lane: int
    reason: str
    subject: str
    rule: Optional[str]
    group: list
    opened_at: str


@dataclass
class _Note:
    event: str
    title: str
    message: str
    mail_body: Optional[str]
    link: Optional[str]
    incident_id: Optional[str] = None
    reopen: Optional[int] = None


@dataclass
class _Row:
    incident_id: str
    reopen: int
    event: str
    summary: Optional[str]
    fix: Optional[str]


@dataclass
class IncidentPlan:
    count: int
    summary_started_at: Optional[str]
    rows: list[_Row] = field(default_factory=list)
    notices: list[_Note] = field(default_factory=list)


def _clip(value: Any, limit: int, *, one_line: bool = False) -> str:
    """Pack text is untrusted: drop control characters and cap the length."""
    if not isinstance(value, str):
        return ""
    chars: list[str] = []
    for ch in value:
        if ch in "\n\t" and not one_line:
            chars.append(ch)
            continue
        if one_line and ch in "\n\r\t":
            chars.append(" ")
            continue
        if unicodedata.category(ch)[0] == "C":
            continue
        chars.append(ch)
    text_ = "".join(chars).strip()
    if one_line:
        text_ = " ".join(text_.split())
    return text_[:limit]


def _when(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not _TS.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _link(incident_id: str) -> Optional[str]:
    if _INCIDENT.fullmatch(incident_id):
        return f"{_MEDIC}?incident={incident_id}"
    return None


def _mail(message: str, link: Optional[str], frontend: str) -> Optional[str]:
    """The absolute link only when the console address is set. The bell keeps the relative one."""
    if not link:
        return None
    base = _clip(frontend, 500, one_line=True).rstrip("/")
    absolute = f"{base}{link}" if base else None
    for line in (f"Open it at {link}", f"Open Medic at {link}"):
        if line not in message:
            continue
        replacement = (
            f"{line.rsplit(link, 1)[0]}{absolute}" if absolute else _OPEN_CONSOLE
        )
        return message.replace(line, replacement, 1)
    return message.replace(link, absolute, 1) if absolute else _OPEN_CONSOLE


def _sensor(group: Any) -> Optional[str]:
    """A recognised sensor id, or None. No other group value is told."""
    if not isinstance(group, list):
        return None
    for item in group:
        if not isinstance(item, dict) or item.get("name") != "sensor":
            continue
        value = item.get("value")
        if isinstance(value, str) and _SENSOR.fullmatch(value):
            return value
        return None
    return None


def _parse(raw: Any) -> Optional[_Item]:
    if not isinstance(raw, dict):
        return None
    iid = raw.get("incident_id")
    if not isinstance(iid, str) or not _INCIDENT.fullmatch(iid):
        return None
    reopen = raw.get("reopen_count")
    lane = raw.get("lane")
    rule = raw.get("rule")
    if type(reopen) is not int or not 0 <= reopen < 2**31:
        return None
    if not isinstance(lane, dict) or not isinstance(rule, dict):
        return None
    value, reason = lane.get("value"), lane.get("reason")
    if value not in (1, 2, 3) or reason not in _REASONS:
        return None
    subject, state = raw.get("subject"), raw.get("state")
    if subject not in _SUBJECTS or state not in _STATES:
        return None
    rule_id = rule.get("id")
    if rule_id is not None and not isinstance(rule_id, str):
        return None
    opened = raw.get("opened_at")
    if not isinstance(opened, str):
        return None
    return _Item(
        id=iid,
        reopen=reopen,
        state=state,
        lane=value,
        reason=reason,
        subject=subject,
        rule=rule_id,
        group=raw.get("group") if isinstance(raw.get("group"), list) else [],
        opened_at=opened,
    )


def _body(detail: Any) -> dict:
    if not isinstance(detail, dict):
        return {}
    opened = detail.get("opened")
    if not isinstance(opened, dict):
        return {}
    body = opened.get("body")
    return body if isinstance(body, dict) else {}


def _l1(detail: Any) -> Optional[str]:
    would = _body(detail).get("would_have")
    if not isinstance(would, dict):
        return None
    action = would.get("L1")
    return action if isinstance(action, str) else None


def _advice(detail: Any) -> tuple[Optional[str], Optional[str]]:
    advice = _body(detail).get("advice")
    if not isinstance(advice, dict):
        return None, None
    summary = _clip(advice.get("summary"), 200) or None
    fix = _clip(advice.get("fix"), 1000) or None
    return summary, fix


def _needs_text(item: _Item) -> bool:
    """Pack summary and fix. Watchers and the two fixed reasons do not."""
    return item.subject != "watcher" and item.reason not in _FIXED


def _in_scope(item: _Item, detail: Any, sent: set[tuple[str, int, str]]) -> bool:
    if item.lane in (2, 3):
        return True
    if item.lane != 1:
        return False
    if (item.id, item.reopen, "opened") in sent or (
        item.id,
        item.reopen,
        "summary",
    ) in sent:
        return True
    return _l1(detail) == "notify_admin"


def _told(item: _Item, sent: set[tuple[str, int, str]]) -> bool:
    return (item.id, item.reopen, "opened") in sent or (
        item.id,
        item.reopen,
        "summary",
    ) in sent


def _held_back(item: _Item, announced: Optional[str]) -> bool:
    """Decision 15: no opened while the status notice covers it.

    The opened goes when the word moves to degraded and the incident is still
    routed. A full recovery (running) sends nothing further for these two.
    """
    if item.rule == _SENSOR_BLIND:
        return announced in ("blind", "running")
    if item.rule == _RECORDING:
        return announced in ("not_recording", "running")
    return False


def _finish(
    message: str, link: Optional[str], frontend: str
) -> tuple[str, Optional[str]]:
    return message, _mail(message, link, frontend)


def _fixed_line(item: _Item) -> Optional[str]:
    if item.reason == "lane1_gate_failed":
        return _GATE
    if item.reason == "unknown_signature":
        return _UNKNOWN
    if item.subject != "watcher":
        return None
    if item.rule == _SENSOR_BLIND:
        sensor = _sensor(item.group)
        if sensor:
            return f"Medic can't see sensor {sensor}."
        return "Medic can't see a sensor Vigil does not recognise."
    if item.rule == _RECORDING:
        return "Medic stopped recording. Free disk space or raise Medic's store cap."
    if item.rule == _STORE_RESET:
        return "Medic's store was reset."
    if item.rule == _POLICY:
        return "Medic's network policy is not enforced. The cluster's network plugin has to enforce it."
    return "Medic reported a watcher incident."


def _opened_note(
    item: _Item, detail: Any, frontend: str
) -> Optional[tuple[_Note, Optional[str], Optional[str]]]:
    """The notice, and the summary and fix to store. None if the detail is still owed."""
    fixed = _fixed_line(item)
    if fixed is not None:
        summary, fix = _clip(fixed, 200) or None, None
        lines = [fixed]
    else:
        if detail is None:
            return None
        summary, fix = _advice(detail)
        title_src = summary or "Medic incident"
        lines = [title_src, "", fix or _NO_FIX]
        if item.lane == 3 and item.subject == "vigil" and item.reason == "rule":
            lines = [
                "Medic classes this as a Vigil defect.",
                "",
                title_src,
                "",
                fix or _NO_FIX,
                "",
                f"Build a support bundle with {_BUNDLE}",
            ]
    if item.reopen > 0:
        lines += ["", "This incident is flapping."]
    link = _link(item.id)
    if link:
        lines += ["", f"Open it at {link}"]
    message, mail = _finish("\n".join(lines), link, frontend)
    title = _clip(summary or "Medic incident", 200, one_line=True) or "Medic incident"
    return (
        _Note("opened", title, message, mail, link, item.id, item.reopen),
        summary,
        fix,
    )


def _clear_note(
    incident_id: str,
    reopen: int,
    how: str,
    summary: Optional[str],
    fix: Optional[str],
    frontend: str,
) -> _Note:
    lines = [_HOW[how]]
    if summary:
        lines += ["", summary]
    if fix:
        lines += ["", fix]
    link = _link(incident_id)
    if link:
        lines += ["", f"Open it at {link}"]
    message, mail = _finish("\n".join(lines), link, frontend)
    title = _clip(summary or "Medic incident cleared", 200, one_line=True)
    return _Note(
        "cleared",
        title or "Medic incident cleared",
        message,
        mail,
        link,
        incident_id,
        reopen,
    )


def _summary_note(
    count: int,
    defects: int,
    start: datetime,
    end: datetime,
    *,
    watching: bool,
    reset: bool,
    frontend: str,
) -> _Note:
    phrase = "while Medic was starting up" if watching else "while notices were off"
    noun = "problem" if count == 1 else "problems"
    title = f"{count} Medic {noun} found {phrase}"
    lines = [f"{title}, from {_stamp(start)} to {_stamp(end)}."]
    if defects == 0:
        lines.append("None are Vigil defects to report.")
    elif defects == 1:
        lines.append("1 is a Vigil defect to report.")
    else:
        lines.append(f"{defects} are Vigil defects to report.")
    if defects:
        lines.append(f"Build a support bundle with {_BUNDLE}")
    if reset:
        lines.append("This follows a store reset.")
    if not watching:
        lines.append("Medic isn't watching yet.")
    lines.append(f"Open Medic at {_MEDIC}")
    message, mail = _finish("\n".join(lines), _MEDIC, frontend)
    return _Note("summary", _clip(title, 200, one_line=True), message, mail, _MEDIC)


def _reset_of(items: list[_Item]) -> Optional[_Item]:
    resets = [i for i in items if i.rule == _STORE_RESET and _when(i.opened_at)]
    if not resets:
        return None
    return max(resets, key=lambda i: i.opened_at)


def _detail_summary(detail: Any) -> Optional[dict]:
    if not isinstance(detail, dict):
        return None
    summary = detail.get("summary")
    return summary if isinstance(summary, dict) else None


def plan_incident_notices(
    items: list[_Item],
    details: dict[str, Any],
    sent_text: dict[tuple[str, int, str], tuple[Optional[str], Optional[str]]],
    snapshot: Optional[dict],
    *,
    now: datetime,
    confirming_polls: int,
    confirming_word: Optional[str],
    announced_word: Optional[str],
    summary_started_at: Optional[str],
    frontend_url: str,
) -> IncidentPlan:
    """What this pass writes. ``details`` values are bodies, or None if that fetch did not finish."""
    sent = set(sent_text)
    count = sum(
        1
        for item in items
        if item.state in _ROUTED and _in_scope(item, details.get(item.id), sent)
    )
    open_ids = {item.id for item in items}
    reset = _reset_of(items)
    started = _when((snapshot or {}).get("started_at"))
    start_key = (snapshot or {}).get("started_at") if started is not None else None
    reset_at = _when(reset.opened_at) if reset is not None else None
    # A reset still open from before this process start is not this start.
    # The clock is the later of the two, so a restart is not timed from the old reset.
    reset_marker = (
        reset
        if reset is not None
        and reset_at is not None
        and (started is None or reset_at >= started)
        else None
    )
    if summary_started_at is None:
        fresh = True
    elif started is None and reset_marker is None:
        fresh = False
    else:
        covered_start, _, covered_reset = summary_started_at.partition("|")
        same_start = started is None or covered_start == start_key
        same_reset = reset_marker is None or reset_marker.id == covered_reset
        fresh = not (same_start and same_reset)
    marker_at = reset_at if reset_marker is not None else started
    watching = (
        confirming_polls >= _CONFIRM and confirming_word in medic_last_seen._WATCHING
    )
    hold = _when((snapshot or {}).get("upgrade_hold_until"))
    hold_clear = (snapshot or {}).get("upgrade_hold_until") is None or (
        hold is not None and hold <= now
    )
    elapsed = now - marker_at if marker_at is not None else None
    due = elapsed is not None and (
        elapsed >= _CAP or (elapsed >= _AFTER and hold_clear and watching)
    )
    # No start marker yet: hold per-incident opens. The count and clears still go.
    if marker_at is None:
        fresh, due = True, False
    plan = IncidentPlan(count=count, summary_started_at=None)
    if fresh and due and marker_at is not None:
        if start_key is not None and reset_marker is not None:
            plan.summary_started_at = f"{start_key}|{reset_marker.id}"
        elif start_key is not None:
            plan.summary_started_at = start_key
        elif reset_marker is not None:
            plan.summary_started_at = f"|{reset_marker.id}"

    def add_opened(item: _Item) -> None:
        built = _opened_note(item, details.get(item.id), frontend_url)
        if built is None:
            return
        note, summary, fix = built
        plan.notices.append(note)
        plan.rows.append(_Row(item.id, item.reopen, "opened", summary, fix))

    covered: list[_Item] = []
    skipped = False
    for item in items:
        if item.state not in _ROUTED or _told(item, sent):
            continue
        if not _in_scope(item, details.get(item.id), sent):
            continue
        if item.rule == _STORE_RESET:
            add_opened(item)
            continue
        if fresh and not due:
            continue
        if fresh and due:
            # A detail that did not finish is told on a later kick, not in this summary.
            if _needs_text(item) and details.get(item.id) is None:
                skipped = True
                continue
            covered.append(item)
            continue
        if _held_back(item, announced_word):
            continue
        add_opened(item)

    # A store reset with nothing else open still gets the summary that says so.
    # A summary of zero while details are still owed would undercount, so that waits.
    if fresh and due and (covered or (reset_marker is not None and not skipped)):
        for item in covered:
            if _needs_text(item):
                summary, fix = _advice(details.get(item.id))
            else:
                summary, fix = _clip(_fixed_line(item), 200) or None, None
            plan.rows.append(_Row(item.id, item.reopen, "summary", summary, fix))
        defects = sum(
            1
            for item in covered
            if item.lane == 3 and item.subject == "vigil" and item.reason == "rule"
        )
        plan.notices.append(
            _summary_note(
                len(covered),
                defects,
                marker_at,
                now,
                watching=watching,
                reset=reset_marker is not None,
                frontend=frontend_url,
            ),
        )

    for (iid, reopen, event), (summary, fix) in sent_text.items():
        if event != "opened":
            continue
        if (iid, reopen, "cleared") in sent or (iid, reopen, "summary") in sent:
            continue
        if iid in open_ids:
            continue
        detail = details.get(iid)
        summary_body = _detail_summary(detail)
        if summary_body is None:
            continue
        if summary_body.get("state") != "resolved":
            continue
        if summary_body.get("reopen_count") != reopen:
            continue
        how = summary_body.get("resolved_how")
        if how not in _HOW:
            continue
        plan.notices.append(_clear_note(iid, reopen, how, summary, fix, frontend_url))
        plan.rows.append(_Row(iid, reopen, "cleared", summary, fix))
    return plan


def _pages(client: httpx.Client, url: str, key: str) -> Optional[list]:
    items: list = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    for _ in range(_PAGES):
        path = "/v1/incidents?state=open&limit=200"
        if cursor:
            path += "&cursor=" + quote(cursor, safe="")
        status, body = _one(client, url, key, path)
        if status != 200 or not isinstance(body, dict):
            return None
        page, nxt = body.get("items"), body.get("next_cursor")
        if not isinstance(page, list) or "next_cursor" not in body:
            return None
        items.extend(page)
        if nxt is None:
            return items
        if not isinstance(nxt, str) or nxt in seen or len(nxt) > 512:
            return None
        seen.add(nxt)
        cursor = nxt
    return None


def _one(
    client: httpx.Client, url: str, key: str, path: str
) -> tuple[Optional[int], Any]:
    try:
        response = client.get(url.rstrip("/") + path, headers={"X-Medic-Key": key})
    except httpx.TimeoutException:
        return None, None
    except httpx.TransportError:
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        return 200, response.json()
    except ValueError:
        return 200, None


def _wanted(items: list[_Item], sent: set[tuple[str, int, str]]) -> list[str]:
    open_ids = {item.id for item in items}
    ids: list[str] = []
    for item in items:
        if item.state not in _ROUTED or _told(item, sent):
            continue
        if item.lane == 1 or (item.lane in (2, 3) and _needs_text(item)):
            ids.append(item.id)
    for iid, reopen, event in sent:
        if event != "opened":
            continue
        if (iid, reopen, "cleared") in sent or (iid, reopen, "summary") in sent:
            continue
        if iid not in open_ids:
            ids.append(iid)
    return list(dict.fromkeys(ids))


def _read_sent() -> tuple[
    dict[tuple[str, int, str], tuple[Optional[str], Optional[str]]],
    Optional[dict],
    Optional[str],
    int,
    Optional[str],
    Optional[str],
]:
    with get_db_session() as session, session.begin():
        sent = {
            (row.incident_id, row.reopen_count, row.event): (row.summary, row.fix)
            for row in session.query(MedicIncidentNotice).all()
        }
        seen = session.get(MedicLastSeen, 1)
        state = session.get(MedicWatchState, 1)
        snap = (
            dict(seen.status_snapshot)
            if seen and isinstance(seen.status_snapshot, dict)
            else None
        )
        return (
            sent,
            snap,
            state.summary_started_at if state else None,
            state.confirming_polls if state else 0,
            state.confirming_word if state else None,
            state.announced_word if state else None,
        )


def _fetch(url: str, key: str) -> Optional[tuple[list[_Item], dict[str, Any]]]:
    """Open pages, then the details this pass may tell. None if a page did not come back 200."""
    timeout = httpx.Timeout(medic_last_seen.REQUEST_TIMEOUT_S)
    with httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        raw = _pages(client, url, key)
        if raw is None:
            return None
        by_id: dict[str, _Item] = {}
        for item in (_parse(row) for row in raw):
            if item is not None:
                by_id.setdefault(item.id, item)
        items = list(by_id.values())
        sent, *_ = _read_sent()
        details: dict[str, Any] = {}
        for iid in _wanted(items, set(sent)):
            status, body = _one(
                client, url, key, f"/v1/incidents/{quote(iid, safe='')}"
            )
            details[iid] = body if status == 200 and isinstance(body, dict) else None
        return items, details


def _mark_opened(session, principal: str, incident_id: str, reopen: int) -> None:
    session.query(CaseNotification).filter(
        CaseNotification.user_id == principal,
        CaseNotification.case_id.is_(None),
        CaseNotification.is_read.is_(False),
        CaseNotification.notification_metadata["event"].astext == "opened",
        CaseNotification.notification_metadata["incident_id"].astext == incident_id,
        CaseNotification.notification_metadata["reopen_count"].astext == str(reopen),
    ).update({"is_read": True, "read_at": utcnow()}, synchronize_session=False)


def watch_incidents(settings: Optional[Settings] = None) -> list[int]:
    """Read Medic, then under the watch lock write the count, the sent rows and the bell rows.

    Does not send mail. A page that is not 200 writes nothing. A detail that
    does not finish is left for the next kick; the count still updates.
    """
    settings = settings or get_settings()
    if not medic_enabled(settings):
        return []
    url, key = settings.vigil_medic_api_url, medic_last_seen.read_key(settings)
    if not url or key is None:
        return []
    fetched = _fetch(url, key)
    if fetched is None:
        return []
    items, details = fetched
    return _commit(settings, items, details)


def _commit(
    settings: Settings, items: list[_Item], details: dict[str, Any]
) -> list[int]:
    with get_db_session() as session, session.begin():
        locked = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": medic_last_seen._WATCH_LOCK},
        ).scalar()
        if not locked:
            return []
        now = medic_last_seen._db_now(session)
        seen = session.get(MedicLastSeen, 1)
        state = session.get(MedicWatchState, 1)
        if state is None:
            state = MedicWatchState(id=1, confirming_polls=0)
            session.add(state)
            session.flush()
        sent_text = {
            (row.incident_id, row.reopen_count, row.event): (row.summary, row.fix)
            for row in session.query(MedicIncidentNotice).all()
        }
        snap = (
            dict(seen.status_snapshot)
            if seen and isinstance(seen.status_snapshot, dict)
            else None
        )
        plan = plan_incident_notices(
            items,
            details,
            sent_text,
            snap,
            now=now,
            confirming_polls=state.confirming_polls,
            confirming_word=state.confirming_word,
            announced_word=state.announced_word,
            summary_started_at=state.summary_started_at,
            frontend_url=settings.vigil_frontend_url,
        )
        state.open_count = plan.count
        state.open_count_at = now
        holders = medic_last_seen._medic_admin_holders()
        if not holders:
            logger.info("Medic incident count %s; no medic.admin holders", plan.count)
            return []
        if plan.summary_started_at:
            state.summary_started_at = plan.summary_started_at
        for row in plan.rows:
            session.add(
                MedicIncidentNotice(
                    incident_id=row.incident_id,
                    reopen_count=row.reopen,
                    event=row.event,
                    summary=row.summary,
                    fix=row.fix,
                )
            )
        ids: list[int] = []
        for note in plan.notices:
            meta: dict[str, Any] = {"event": note.event}
            if note.incident_id is not None:
                meta["incident_id"] = note.incident_id
                meta["reopen_count"] = note.reopen
            for principal in holders:
                ids.append(
                    notices.notify(
                        principal,
                        _KIND,
                        note.title,
                        note.message,
                        link=note.link,
                        metadata=meta,
                        mail_body=note.mail_body,
                        session=session,
                    )
                )
                if note.event == "cleared" and note.incident_id is not None:
                    _mark_opened(session, principal, note.incident_id, note.reopen or 0)
        logger.info(
            "Medic incident notices: %s to %s holder(s); %s open",
            len(plan.notices),
            len(holders),
            plan.count,
        )
        return ids


def incident_told(incident_id: str, reopen_count: int) -> str:
    """``none``, ``opened`` or ``summary`` for this reopen. The latter two mean a bell row was written."""
    if not isinstance(incident_id, str) or type(reopen_count) is not int:
        return "none"
    with get_db_session() as session, session.begin():
        events = {
            row.event
            for row in session.query(MedicIncidentNotice).filter(
                MedicIncidentNotice.incident_id == incident_id,
                MedicIncidentNotice.reopen_count == reopen_count,
                MedicIncidentNotice.event.in_(("opened", "summary")),
            )
        }
    if "opened" in events:
        return "opened"
    if "summary" in events:
        return "summary"
    return "none"
