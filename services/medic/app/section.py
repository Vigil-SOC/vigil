"""`section`: one public-safe JSON document of prepare_support_bundle incidents.

A lane-1 daemon death or hang (`pipeline.daemon-component-failed-compose`,
`pipeline.daemon-component-hung-compose`) is in it too, even though Medic
restarted it: `fix` lists each attempt (action, who, level, result).

Reads the store through `open_reader` only. The field list below is the only
copy; the document repeats it as `fields`. No schema file and no version field.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from services.medic.api.incidents import _Fold
from services.medic.app import config
from services.medic.app.wiring import BUNDLED_DIR, VERSION_FILE
from services.medic.contracts.rule_check import ENGINE_API
from services.medic.engine.core import BLIND, BUILTINS
from services.medic.packs.loader import read_active
from services.medic.store import StoreError, open_reader

# Same spelling as core/platform/medic_incidents.py `_SENSOR`. Medic does not
# import core.platform (or core.config).
_SENSOR = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_TS = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
)
_INCIDENT = re.compile(r"^inc_[0-9A-Za-z]{8,32}$")
_SIGNAL = re.compile(r"^[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_]{0,63})?$")
_ID = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")
_RULE = re.compile(r"^(ingest|llm|pipeline|watcher)\.[a-z0-9][a-z0-9-]{1,62}$")
_MODE = re.compile(r"^[ILP]-[0-9]{1,2}$")
_CAUSE = re.compile(r"^[ILP]-[0-9]{1,2}(\.[a-z])?$")
_API = re.compile(r"^1\.(0|[1-9][0-9]{0,2})$")

_CAP = 200  # newest kept; the incidents API limit
_DEADLINE_S = 50  # before the support script's 60s source limit
_BUNDLED = BUNDLED_DIR / "medic-core.json"
_SITE = "site pack"
_SCAN = "SELECT seq, record FROM records ORDER BY seq"

# One allow-list. `sensor` is set only for watcher.sensor-blind.
FIELDS = (
    "incident_id",
    "subject",
    "install_shape",
    "detection_type",
    "lane",
    "rule",
    "fault",
    "active_since",
    "opened_at",
    "routed_at",
    "resolved_at",
    "resolved_how",
    "reopen_count",
    "suppressed_by",
    "runbook",
    "would_have",
    "evidence",
    "sensor",
    "fix",
)
_SUBJECTS = ("vigil", "watcher")
_SHAPES = ("start_sh", "compose", "helm")
_DETECTIONS = ("event", "absence")
_REASONS = ("rule", "lane1_gate_failed", "unknown_signature")
_ACTIONS = (
    "record",
    "notify_admin",
    "propose_runbook",
    "run_runbook",
    "prepare_support_bundle",
)
# Lane-1 rules whose restart stays in the section. The hung rule is not yet in the pack.
_DAEMON_RULES = (
    "pipeline.daemon-component-failed-compose",
    "pipeline.daemon-component-hung-compose",
)
_LEVELS = ("L0", "L1", "L2")
_RESULTS = ("cleared", "not_cleared", "not_back", "not_verified")
_INELIGIBLE = ("not_lane1", "no_runbook", "runtime_untrusted")
_HOW = ("cleared", "rule_retired", "group_retired", "closed_quietly")
_CLASSES = ("ingest", "llm", "pipeline")
_CHANNELS = ("stable", "beta", "dev")
_GAP_REASONS = ("stalled", "off")
_PURGE_REASONS = ("retention", "early_purge")
_FRESH = "A store started fresh holds nothing from before it started."
_ASIDE = "History moved aside after a store reset is not read."
_NARROW = "A shorter window narrows the list."
_UNKNOWN = "lane and membership unknown"


class _Bad(Exception):
    """A record is not JSON, or a matched incident will not fit the allow-list."""


class _Unreadable(Exception):
    """The database file cannot be read. Moved-aside files are never opened."""


class _Timeout(Exception):
    """The 50s deadline passed. Nothing is written to stdout."""


@dataclass
class _Kept:
    fold: _Fold
    evidence: list[Any]
    bad: bool = False
    fix: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Scan:
    folds: dict[str, _Kept] = field(default_factory=dict)
    opened: set[str] = field(default_factory=set)
    # incident id -> latest in-window `at`, when no incident_opened remains.
    purged: dict[str, datetime] = field(default_factory=dict)
    packs: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    gaps: list[dict[str, str]] = field(default_factory=list)
    anchors: list[dict[str, str]] = field(default_factory=list)
    resets: list[dict[str, str]] = field(default_factory=list)
    oldest: tuple[datetime, str] | None = None


def section_main(argv: Sequence[str], env: Mapping[str, str], usage: str) -> int:
    try:  # host-native has no cgroup budget; a refusal is not a failure
        os.nice(10)
    except OSError:
        pass
    since = _since(argv)
    if since is None:
        print(usage, file=sys.stderr)
        return 2
    try:
        text = _render(env, since, time.monotonic() + _DEADLINE_S)
    except _Unreadable:
        print("store unreadable", file=sys.stderr)
        return 1
    except _Bad:
        print("malformed", file=sys.stderr)
        return 1
    except _Timeout:
        print("out of time", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


def _since(argv: Sequence[str]) -> int | None:
    if not argv:
        return 7
    if len(argv) != 2 or argv[0] != "--since-days":
        return None
    raw = argv[1]
    # isdigit() is true for non-ASCII digits such as ², which int() rejects.
    if not raw.isascii() or not raw.isdigit() or raw[0] == "0":
        return None
    days = int(raw)
    return days if days <= 100_000 else None


def _render(env: Mapping[str, str], since: int, deadline: float) -> str:
    def tick() -> None:
        if time.monotonic() >= deadline:
            raise _Timeout

    try:
        shape = config.install_shape(env)
        vigil = VERSION_FILE.read_text().strip()
    except (config.ConfigError, OSError):
        raise _Bad from None
    if not vigil or not vigil.isascii() or any(ord(c) < 32 for c in vigil):
        raise _Bad
    try:
        bundled = _BUNDLED.read_bytes()
    except OSError:
        bundled = None
    bundled_sha = hashlib.sha256(bundled).hexdigest() if bundled else ""
    data_dir = config.data_dir(env, sys.platform)
    active = _active_pack(data_dir, bundled)
    end = datetime.now(UTC)
    start = end - timedelta(days=since)
    try:
        with open_reader(data_dir) as conn:
            scan = _walk(conn, start, end, bundled_sha, tick)
    except (StoreError, sqlite3.Error, OSError):
        raise _Unreadable from None
    tick()
    incidents = _incidents(scan, start, end)
    tick()
    shown, omitted = incidents[:_CAP], max(0, len(incidents) - _CAP)
    purged_ids, purged_omitted = _purged(scan.purged)
    doc = {
        "fields": list(FIELDS),
        "window": {"since_days": since, "from": _stamp(start), "to": _stamp(end)},
        "count": len(incidents),
        "omitted": omitted,
        "omitted_note": _NARROW,
        "vigil_version": vigil,
        "engine_api": f"{ENGINE_API[0]}.{ENGINE_API[1]}",
        "active_pack": active,
        "install_shape": shape,
        "oldest_at": None if scan.oldest is None else scan.oldest[1],
        "started_fresh": _FRESH,
        "moved_aside": _ASIDE,
        "gaps": scan.gaps,
        "anchors": scan.anchors,
        "store_resets": scan.resets,
        "purged_openings": {
            "note": _UNKNOWN,
            "count": len(scan.purged),
            "omitted": purged_omitted,
            "ids": purged_ids,
        },
        "incidents": shown,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if not text.isascii():
        raise _Bad
    tick()
    return text


def _active_pack(data_dir: Any, bundled: bytes | None) -> str | dict[str, str]:
    """The active file's bytes against the bundled file, as lifecycle._provenance."""
    if not bundled:
        return _SITE
    active = read_active(data_dir)
    if active is None or active.data != bundled:
        return _SITE
    lin = active.lineage
    if lin.channel not in _CHANNELS:
        return _SITE
    if _ID.fullmatch(lin.id) is None or _ID.fullmatch(lin.version) is None:
        return _SITE
    return {"id": lin.id, "channel": lin.channel, "version": lin.version}


def _walk(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    bundled_sha: str,
    tick,
) -> _Scan:
    scan = _Scan()
    tick()
    for seq, raw in conn.execute(_SCAN):
        tick()
        rec = _record(raw, seq)
        at = _when(rec.get("at"))
        if at is None:
            raise _Bad
        if scan.oldest is None or at < scan.oldest[0]:
            scan.oldest = (at, rec["at"])
        kind = rec.get("type")
        body = rec.get("body")
        if kind == "pack_event":
            _note_pack(scan, body, bundled_sha)
        elif kind == "incident_opened":
            _opened(scan, rec)
        elif kind in ("incident_updated", "incident_resolved", "feedback"):
            _later(scan, rec, at, start, end)
        elif kind == "fix_run":
            _fix_run(scan, body)
        elif kind == "fix_verified":
            _fix_verified(scan, body)
        elif kind in ("gap", "anchor", "store_reset") and start <= at <= end:
            _coverage(scan, kind, rec, body)
    return scan


def _record(raw: Any, seq: int) -> dict[str, Any]:
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError, TypeError):
        raise _Bad from None
    if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
        raise _Bad
    rec["seq"] = seq
    return rec


def _opened(scan: _Scan, rec: dict[str, Any]) -> None:
    body = rec.get("body")
    iid = _incident_id(body)
    if iid is None or not isinstance(body, dict):
        raise _Bad
    scan.opened.add(iid)
    scan.purged.pop(iid, None)
    # would_have is fixed at open. Routing may arrive on a later record.
    if not _prepares_bundle(body):
        scan.folds.pop(iid, None)
        return
    evidence = body.get("evidence")
    bad = not isinstance(evidence, list)
    fold = _Fold(opened=rec, last_seq=rec["seq"])
    if body.get("route") == "routed":
        fold.routed_at = rec["at"]
    scan.folds[iid] = _Kept(fold, [] if bad else list(evidence), bad)


def _later(
    scan: _Scan, rec: dict[str, Any], at: datetime, start: datetime, end: datetime
) -> None:
    iid = _incident_id(rec.get("body"))
    if iid is None:
        raise _Bad
    kept = scan.folds.get(iid)
    if iid not in scan.opened:
        if start <= at <= end:
            prev = scan.purged.get(iid)
            if prev is None or at > prev:
                scan.purged[iid] = at
        return
    if kept is None:
        return
    body = rec["body"]
    if (
        rec["type"] == "incident_updated"
        and isinstance(body, dict)
        and body.get("change") == "evidence_added"
    ):
        added = body.get("evidence")
        if not isinstance(added, list):
            kept.bad = True
        else:
            kept.evidence.extend(added)
    try:
        kept.fold.apply(rec)
    except (KeyError, TypeError, AttributeError):
        kept.bad = True


def _fix_run(scan: _Scan, body: Any) -> None:
    if not isinstance(body, dict):
        raise _Bad
    covered = body.get("covered")
    if not isinstance(covered, list):
        raise _Bad
    for iid in covered:
        if not isinstance(iid, str) or _INCIDENT.fullmatch(iid) is None:
            raise _Bad
    kept = [scan.folds[i] for i in covered if i in scan.folds]
    if not kept:
        return
    who = body.get("who")
    if not isinstance(who, str):
        raise _Bad
    if who.startswith("user:"):
        who = "admin"  # the session id is never published
    elif who != "medic":
        raise _Bad
    attempt = {
        "action": _one(body.get("would_have"), _ACTIONS),
        "who": who,
        "level": _one(body.get("level"), _LEVELS),
        "result": "",
    }
    for item in kept:
        item.fix.append(dict(attempt))


def _fix_verified(scan: _Scan, body: Any) -> None:
    iid = _incident_id(body)
    if iid is None:
        raise _Bad
    result = _one(body.get("result"), _RESULTS)
    kept = scan.folds.get(iid)
    if kept is None:
        return
    # The latest attempt with no outcome yet.
    for attempt in reversed(kept.fix):
        if attempt["result"] == "":
            attempt["result"] = result
            return


def _note_pack(scan: _Scan, body: Any, bundled_sha: str) -> None:
    if not isinstance(body, dict):
        raise _Bad
    pack = body.get("pack")
    if not isinstance(pack, dict):
        raise _Bad
    key = (pack.get("id"), pack.get("channel"), pack.get("version"))
    if not all(isinstance(part, str) for part in key):
        raise _Bad
    sha = body.get("sha256")
    # rule_skipped has no sha256, so that pack cannot be the bundled file.
    ok = isinstance(sha, str) and sha == bundled_sha
    scan.packs[key] = scan.packs.get(key, True) and ok


def _coverage(scan: _Scan, kind: str, rec: dict[str, Any], body: Any) -> None:
    if not isinstance(body, dict):
        raise _Bad
    if kind == "gap":
        scan.gaps.append(_times(body, "from", "to", _GAP_REASONS))
    elif kind == "anchor":
        scan.anchors.append(_times(body, "deleted_from", "deleted_to", _PURGE_REASONS))
    else:
        reason = body.get("reason")
        if reason != "corruption":
            raise _Bad
        scan.resets.append({"at": rec["at"], "reason": reason})


def _times(
    body: dict, start: str, end: str, reasons: tuple[str, ...]
) -> dict[str, str]:
    a, b, reason = body.get(start), body.get(end), body.get("reason")
    if _when(a) is None or _when(b) is None or reason not in reasons:
        raise _Bad
    return {start: a, end: b, "reason": reason}


def _incidents(scan: _Scan, start: datetime, end: datetime) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    for kept in scan.folds.values():
        if not _open_in(kept, start, end):
            continue
        if kept.bad or kept.fold.routed_at is None:
            if kept.bad:
                raise _Bad
            continue
        chosen.append(_project(kept, scan.packs))
    chosen.sort(key=lambda inc: inc["incident_id"])
    chosen.sort(key=lambda inc: inc["opened_at"], reverse=True)
    return chosen


def _open_in(kept: _Kept, start: datetime, end: datetime) -> bool:
    """Open interval is incident_opened.at through resolved_at, or now."""
    opened = _when(kept.fold.opened.get("at"))
    if opened is None:
        raise _Bad
    resolved = kept.fold.resolved
    if resolved is None:
        close = end
    else:
        close = _when(resolved.get("at"))
        if close is None:
            raise _Bad
    return opened <= end and close >= start


def _project(kept: _Kept, packs: Mapping[tuple[str, str, str], bool]) -> dict[str, Any]:
    body = kept.fold.opened.get("body")
    if not isinstance(body, dict):
        raise _Bad
    iid = body.get("incident_id")
    if not isinstance(iid, str) or _INCIDENT.fullmatch(iid) is None:
        raise _Bad
    rule, site = _rule(body.get("rule"), packs)
    out = {
        "incident_id": iid,
        "subject": _one(body.get("subject"), _SUBJECTS),
        "install_shape": _one(body.get("install_shape"), _SHAPES),
        "detection_type": _one(body.get("detection_type"), _DETECTIONS),
        "lane": _lane(body),
        "rule": rule,
        "fault": _fault(body.get("fault")),
        "active_since": _ts(body.get("active_since")),
        "opened_at": _ts(kept.fold.opened.get("at")),
        "routed_at": _ts(kept.fold.routed_at),
        "resolved_at": (
            None if kept.fold.resolved is None else _ts(kept.fold.resolved.get("at"))
        ),
        "resolved_how": _how(kept),
        "reopen_count": _count(kept.fold.reopen_count),
        "suppressed_by": _parent(kept.fold.suppressed_by),
        "runbook": None if site else _runbook(body.get("runbook")),
        "would_have": _would(body.get("would_have")),
        "evidence": [_item(item, site) for item in kept.evidence],
        "fix": kept.fix,
    }
    sensor = _sensor(body.get("rule"), body.get("group"))
    if sensor is not None:
        out["sensor"] = sensor
    return out


def _rule(rule: Any, packs: Mapping[tuple[str, str, str], bool]) -> tuple[Any, bool]:
    if rule is None:
        return None, False
    if not isinstance(rule, dict):
        raise _Bad
    rid = rule.get("id")
    if rid in BUILTINS:
        return _builtin(rule), False
    key = (rule.get("pack_id"), rule.get("pack_channel"), rule.get("pack_version"))
    if all(isinstance(part, str) for part in key) and packs.get(key) is True:
        return _pack_rule(rule), False
    return {"pack": _SITE}, True


def _builtin(rule: dict) -> dict[str, Any]:
    revision, api = rule.get("revision"), rule.get("engine_api")
    if type(revision) is not int or revision < 1 or not isinstance(api, str):
        raise _Bad
    if _API.fullmatch(api) is None or not isinstance(rule.get("id"), str):
        raise _Bad
    return {"id": rule["id"], "revision": revision, "engine_api": api}


def _pack_rule(rule: dict) -> dict[str, Any]:
    channel, version, pack_id = (
        rule.get("pack_channel"),
        rule.get("pack_version"),
        rule.get("pack_id"),
    )
    if channel not in _CHANNELS or not isinstance(version, str):
        raise _Bad
    if not isinstance(pack_id, str) or _ID.fullmatch(pack_id) is None:
        raise _Bad
    rid = rule.get("id")
    if not isinstance(rid, str) or _RULE.fullmatch(rid) is None:
        raise _Bad
    if _ID.fullmatch(version) is None:
        raise _Bad
    revision, api = rule.get("revision"), rule.get("engine_api")
    if type(revision) is not int or revision < 1 or not isinstance(api, str):
        raise _Bad
    if _API.fullmatch(api) is None:
        raise _Bad
    return {
        "id": rid,
        "revision": revision,
        "pack_id": pack_id,
        "channel": channel,
        "version": version,
        "engine_api": api,
    }


def _lane(body: dict) -> dict[str, Any]:
    lane = body.get("lane")
    if not isinstance(lane, dict):
        raise _Bad
    value, reason = lane.get("value"), lane.get("reason")
    if value not in (1, 2, 3) or reason not in _REASONS:
        raise _Bad
    return {"value": value, "reason": reason}


def _fault(fault: Any) -> dict[str, Any] | None:
    if fault is None:
        return None
    if not isinstance(fault, dict):
        raise _Bad
    causes = fault.get("causes")
    mode = fault.get("mode")
    if fault.get("class") not in _CLASSES or not isinstance(mode, str):
        raise _Bad
    if _MODE.fullmatch(mode) is None or not isinstance(causes, list) or not causes:
        raise _Bad
    if len(causes) > 16 or any(
        not isinstance(c, str) or _CAUSE.fullmatch(c) is None for c in causes
    ):
        raise _Bad
    return {"class": fault["class"], "mode": mode, "causes": list(causes)}


def _would(wh: Any) -> dict[str, Any]:
    if not isinstance(wh, dict):
        raise _Bad
    level2 = wh.get("L2")
    if (
        wh.get("L0") != "record"
        or wh.get("L1") not in _ACTIONS
        or not isinstance(level2, dict)
    ):
        raise _Bad
    action = level2.get("action")
    if action not in _ACTIONS:
        raise _Bad
    shown: dict[str, Any] = {"action": action}
    if "not_eligible" in level2:
        if level2["not_eligible"] not in _INELIGIBLE:
            raise _Bad
        shown["not_eligible"] = level2["not_eligible"]
    return {"L0": "record", "L1": wh["L1"], "L2": shown}


def _item(item: Any, site: bool) -> dict[str, str]:
    if not isinstance(item, dict):
        raise _Bad
    when = _ts(item.get("t"))
    if site:
        return {"signal": _SITE, "t": when}
    signal = item.get("signal")
    if not isinstance(signal, str) or _SIGNAL.fullmatch(signal) is None:
        raise _Bad
    return {"signal": signal, "t": when}


def _sensor(rule: Any, group: Any) -> str | None:
    if (
        not isinstance(rule, dict)
        or rule.get("id") != BLIND
        or not isinstance(group, list)
    ):
        return None
    for item in group:
        if not isinstance(item, dict) or item.get("name") != "sensor":
            continue
        value = item.get("value")
        if isinstance(value, str) and _SENSOR.fullmatch(value):
            return value
        return None
    return None


def _runbook(runbook: Any) -> str | None:
    if runbook is None:
        return None
    if not isinstance(runbook, dict):
        raise _Bad
    rid = runbook.get("id")
    if not isinstance(rid, str) or _ID.fullmatch(rid) is None:
        raise _Bad
    return rid


def _how(kept: _Kept) -> str | None:
    if kept.fold.resolved is None:
        return None
    body = kept.fold.resolved.get("body")
    how = body.get("how") if isinstance(body, dict) else None
    if how not in _HOW:
        raise _Bad
    return how


def _parent(iid: str | None) -> str | None:
    if iid is None:
        return None
    if not isinstance(iid, str) or _INCIDENT.fullmatch(iid) is None:
        raise _Bad
    return iid


def _count(value: int) -> int:
    if type(value) is not int or value < 0:
        raise _Bad
    return value


def _one(value: Any, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise _Bad
    return value


def _ts(value: Any) -> str:
    if _when(value) is None or not isinstance(value, str):
        raise _Bad
    return value


def _purged(found: dict[str, datetime]) -> tuple[list[str], int]:
    # Newest latest-at first. Equal times stay in incident_id order.
    ordered = sorted(found, key=lambda iid: iid)
    ordered.sort(key=lambda iid: found[iid], reverse=True)
    shown = ordered[:_CAP]
    return shown, len(found) - len(shown)


def _prepares_bundle(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    wh = body.get("would_have")
    rule = body.get("rule")
    if isinstance(rule, dict) and rule.get("id") in _DAEMON_RULES:
        return True
    if not isinstance(wh, dict):
        return False
    if wh.get("L1") == "prepare_support_bundle":
        return True
    level2 = wh.get("L2")
    return isinstance(level2, dict) and level2.get("action") == "prepare_support_bundle"


def _incident_id(body: Any) -> str | None:
    if not isinstance(body, dict) or "incident_id" not in body:
        return None
    iid = body.get("incident_id")
    if not isinstance(iid, str) or _INCIDENT.fullmatch(iid) is None:
        raise _Bad
    return iid


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or _TS.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _stamp(moment: datetime) -> str:
    text = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    text = text.rstrip("0").removesuffix(".")
    return text + "Z"
