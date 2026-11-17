"""The level of each fix type, and the counts that lower it.

A fix type is a lane-1 rule id (one rule, one action, one shape). L0 observe: a
click is refused. L1 one-click (the default). L2 on its own: Medic runs the same
restart when the incident routes (fix.py). An admin raises one step, with a
reason, or lowers; Medic only ever lowers, and each change is a `level_changed`
record in the hash chain.

The level lives in one JSON file beside packs/state.json, outside the purgeable
log, written the way lifecycle.py writes (temp, fsync, replace, fsync-dir; 0600).
Per fix type it keeps the level, the rule revision and pack it was raised on, and
the timestamps (not counters, so they age out) of Medic's failed attempts in the
last 21 minutes and of Medic's restarts in the last 24 hours.

    No file, no earlier level_changed in the log   L1
    No usable file, a level_changed in the log     L0 (reported as `unreadable`)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

from services.medic.api.decisions import Problem
from services.medic.packs import lifecycle
from services.medic.packs.loader import packs_dir
from services.medic.redact import REDACTION_VERSION
from services.medic.store import RecordHeld, StoreError, open_reader
from services.medic.store.writer import utc_now

log = logging.getLogger("services.medic")

FILE = "levels.json"
RULE_ID = "pipeline.daemon-component-failed-compose"
FIX_TYPES = (RULE_ID,)
MEDIC = "medic"
LEVELS = ("L0", "L1", "L2")
FAIL_WINDOW_S = 21 * 60
FAILS_MAX = 2
DAY_S = 24 * 3600
DAILY_MAX = 3

_TEXT = {
    "two_failures": "two failed restarts in 21 minutes",
    "not_back": "the container did not come back",
    "daily_limit": "3 restarts in 24 hours",
    "pack_changed": "the rule or its pack changed",
    "store_reset": "the decision store was reset",
}
_RECORDS = (
    "SELECT record FROM records WHERE json_extract(record, '$.type') IN"
    " ('fix_run', 'fix_verified', 'level_changed', 'store_reset', 'anchor')"
    " ORDER BY seq"
)
_FIRST = "SELECT json_extract(record, '$.at') FROM records ORDER BY seq LIMIT 1"
_OPENED = (
    "SELECT json_extract(record, '$.body.incident_id'),"
    " json_extract(record, '$.body.rule.id'),"
    " json_extract(record, '$.body.would_have.L1') FROM records"
    " WHERE json_extract(record, '$.type') = 'incident_opened'"
)
_EVER = (
    "SELECT 1 FROM records WHERE json_extract(record, '$.type') = 'level_changed'"
    " LIMIT 1"
)


def _epoch(raw: object) -> float | None:
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _floats(raw: object) -> bool:
    return isinstance(raw, list) and all(
        isinstance(t, int | float) and not isinstance(t, bool) for t in raw
    )


def _entry_ok(entry: object) -> bool:
    if not isinstance(entry, dict) or entry.get("level") not in LEVELS:
        return False
    raised = entry.get("raised")
    if raised is not None and not (
        isinstance(raised, dict)
        and isinstance(raised.get("at"), int | float)
        and isinstance(raised.get("rule"), dict)
    ):
        return False
    return _floats(entry.get("failures")) and _floats(entry.get("restarts"))


class Levels:
    def __init__(self, medic, *, persistence: bool = True) -> None:
        self.medic, self.persistence = medic, persistence
        self.path = packs_dir(medic.writer.root) / FILE
        # Reentrant: a drop is called from inside a count. Taken after Fixer's lock,
        # never before it, and never held across the restart.
        self._lock = threading.RLock()
        self._ever: bool | None = None  # a level_changed is in the log

    # -- the file ----------------------------------------------------------------

    def _load(self) -> dict[str, dict] | None:
        """The fix types in the file; None when it is missing or unusable."""
        try:
            doc = json.loads(self.path.read_bytes())
        except (OSError, ValueError, RecursionError):
            return None
        types = doc.get("types") if isinstance(doc, dict) else None
        if (
            not isinstance(types, dict)
            or doc.get("v") != 1
            or not all(_entry_ok(e) for e in types.values())
        ):
            return None
        return types

    def _save(self, types: dict[str, dict], now: float) -> None:
        for entry in types.values():
            entry["failures"] = [
                t for t in entry["failures"] if now - t < FAIL_WINDOW_S
            ]
            entry["restarts"] = [t for t in entry["restarts"] if now - t < DAY_S]
        data = json.dumps({"v": 1, "types": types}, indent=2, sort_keys=True)
        lifecycle._replace(lifecycle._dir(self.medic.writer.root), FILE, data.encode())

    def _ever_set(self) -> bool:
        """Has any level been set in the log? Only this process writes level_changed,
        so the answer is read once and kept."""
        if self._ever is None:
            try:
                with open_reader(self.medic.writer.root) as conn:
                    self._ever = conn.execute(_EVER).fetchone() is not None
            except (StoreError, sqlite3.Error):
                return True  # can't tell: fail toward observe
        return self._ever

    def _state(self) -> tuple[dict[str, dict], bool]:
        """(fix types, unreadable). Unreadable: no usable file after a level was set."""
        types = self._load()
        if types is not None:
            return types, False
        return {}, self._ever_set()

    def level(self, fix_type: str | None) -> str:
        """The level in force. A fix type that cannot be set is always L1."""
        if fix_type not in FIX_TYPES:
            return "L1"
        with self._lock:
            types, unreadable = self._state()
        if unreadable:
            return "L0"
        return types.get(fix_type, {}).get("level", "L1")

    def rule_of(self, fix_type: str) -> dict[str, Any] | None:
        """The rule as the pack has it now: what a raise is tied to."""
        loaded = self.medic.engine.by_id.get(fix_type)
        if loaded is None:
            return None
        return {
            "id": fix_type,
            "revision": loaded.rule["revision"],
            "pack_id": loaded.pack[0],
        }

    # -- changes -----------------------------------------------------------------

    def _change(
        self,
        types: dict[str, dict],
        fix_type: str,
        before: str,
        to: str,
        who: str,
        reason: dict | None,
        cause: str | None = None,
    ) -> Problem | None:
        """The record first, then the file."""
        body: dict[str, Any] = {
            "fix_type": fix_type, "from": before, "to": to, "who": who,
        }  # fmt: skip
        if reason is not None:
            body["reason"] = reason
        if cause is not None:
            body["cause"] = cause
        try:
            self.medic.writer.append(
                {"v": 1, "at": utc_now(), "type": "level_changed", "body": body}
            )
        except (StoreError, sqlite3.Error) as exc:
            if isinstance(exc, RecordHeld):
                return Problem(503, "store_not_recording")
            log.warning("level_changed not stored: %s", type(exc).__name__)
            return Problem(503, "busy")
        self._ever = True
        try:
            self._save(types, time.time())
        except OSError as exc:
            log.error("levels file not written: %s", type(exc).__name__)
            return Problem(503, "busy")
        log.info("level %s %s -> %s by %s", fix_type, before, to, who)
        return None

    def set(
        self, fix_type: str, to: str, who: str, reason: dict | None
    ) -> dict | Problem:
        """An admin's change. A raise is one step and needs a reason; to L2 it also
        needs persistence on. A lower is any number of steps."""
        with self._lock:
            types, unreadable = self._state()
            now = "L0" if unreadable else types.get(fix_type, {}).get("level", "L1")
            if to == now:
                return Problem(409, "fix_refused", reason="no_change")
            step = LEVELS.index(to) - LEVELS.index(now)
            raised = None
            if step > 0:
                if step != 1:
                    return Problem(422, "invalid_body", "/level")
                if reason is None:
                    return Problem(422, "invalid_body", "/reason")
                if to == "L2":
                    if not self.persistence:
                        log.warning("level raise to L2 refused: persistence is off")
                        return Problem(409, "fix_refused", reason="persistence_off")
                    rule = self.rule_of(fix_type)
                    if rule is None:
                        return Problem(409, "fix_refused", reason="no_rule")
                    raised = {"at": time.time(), "rule": rule}
            entry = types.setdefault(
                fix_type, {"level": now, "raised": None, "failures": [], "restarts": []}
            )
            entry.update(level=to, raised=raised, failures=[])
            if (bad := self._change(types, fix_type, now, to, who, reason)) is not None:
                return bad
        return self.view(fix_type)

    def _drop(self, fix_type: str, cause: str) -> None:
        """Medic lowers L2 to L1 and says why."""
        types, unreadable = self._state()
        entry = types.get(fix_type)
        if unreadable or entry is None or entry["level"] != "L2":
            return
        entry.update(level="L1", raised=None, failures=[])
        text = _TEXT[cause]
        reason = {"text": text, "redaction_version": REDACTION_VERSION}
        if bad := self._change(types, fix_type, "L2", "L1", MEDIC, reason, cause):
            # Safer lowered than recorded: the level comes down even when the record
            # could not be stored.
            log.error("level %s lowered without a record: %s", fix_type, bad.code)
            self._keep(types)

    def _keep(self, types: dict[str, dict]) -> None:
        """Write the file; a failure is logged and never reaches the caller (a verify
        timer or the fix thread has nothing to do with it)."""
        try:
            self._save(types, time.time())
        except OSError as exc:
            log.error("levels file not written: %s", type(exc).__name__)

    def restarted(self, fix_type: str) -> None:
        """Medic restarted this fix type: counts toward the daily limit."""
        with self._lock:
            types, unreadable = self._state()
            entry = types.get(fix_type)
            if unreadable or entry is None:
                return
            now = time.time()
            entry["restarts"].append(now)
            self._keep(types)
            if sum(now - t < DAY_S for t in entry["restarts"]) >= DAILY_MAX:
                self._drop(fix_type, "daily_limit")

    def restarts_today(self, fix_type: str) -> int:
        with self._lock:
            types, _ = self._state()
        now = time.time()
        return sum(now - t < DAY_S for t in types.get(fix_type, {}).get("restarts", []))

    def limit_reached(self, fix_type: str) -> None:
        with self._lock:
            self._drop(fix_type, "daily_limit")

    def verified(self, fix_type: str | None, result: str) -> None:
        """The verify of one of Medic's own restarts. Not cleared counts as a failed
        attempt; not back lowers at once. Not verified counts for nothing more."""
        if fix_type not in FIX_TYPES or result not in ("not_cleared", "not_back"):
            return
        with self._lock:
            types, unreadable = self._state()
            entry = types.get(fix_type)
            if unreadable or entry is None:
                return
            if result == "not_back":
                return self._drop(fix_type, "not_back")
            now = time.time()
            entry["failures"].append(now)
            self._keep(types)
            if sum(now - t < FAIL_WINDOW_S for t in entry["failures"]) >= FAILS_MAX:
                self._drop(fix_type, "two_failures")

    def reconcile(self) -> None:
        """Drop L2 to L1 when the rule or its pack is not what it was raised on, or
        the store was reset since. Run at start and every cycle, so a pack change
        shows in GET /v1/pack at once."""
        with self._lock:
            types, unreadable = self._state()
            if unreadable:
                return
            for fix_type, entry in list(types.items()):
                if entry["level"] != "L2":
                    continue
                if self.rule_of(fix_type) != entry["raised"]["rule"]:
                    self._drop(fix_type, "pack_changed")
                elif self._reset_after(entry["raised"]["at"]):
                    self._drop(fix_type, "store_reset")

    def _reset_after(self, at: float) -> bool:
        try:
            with open_reader(self.medic.writer.root) as conn:
                # A reset is the first record of a fresh store, so no scan.
                rows = conn.execute(
                    "SELECT json_extract(record, '$.type'), json_extract(record, '$.at')"
                    " FROM records ORDER BY seq LIMIT 3"
                ).fetchall()
        except (StoreError, sqlite3.Error):
            return False
        return any(t == "store_reset" and (_epoch(a) or 0) > at for t, a in rows)

    # -- what GET /v1/pack serves --------------------------------------------------

    def view(self, fix_type: str) -> dict[str, Any]:
        with self._lock:
            types, unreadable = self._state()
        entry = types.get(fix_type)
        level = "L0" if unreadable else (entry or {}).get("level", "L1")
        raised = (entry or {}).get("raised")
        try:
            track = self._track(fix_type)
        except (StoreError, sqlite3.Error):
            track = None  # the level still stands when the log can't be read
        return {
            "fix_type": fix_type,
            "level": level,
            "unreadable": unreadable,
            "raised_on": (
                None
                if level != "L2" or raised is None
                else {
                    "at": _iso(raised["at"]),
                    "revision": raised["rule"].get("revision"),
                    "pack_id": raised["rule"].get("pack_id"),
                }
            ),
            "track_record": track,
        }

    def _track(self, fix_type: str) -> dict[str, Any]:
        """Counted from the log: Medic's runs and outcomes, and what could have
        thinned it (a reset or an early purge inside the window)."""
        with open_reader(self.medic.writer.root) as conn:
            opened = conn.execute(_OPENED).fetchall()
            rows = [json.loads(r[0]) for r in conn.execute(_RECORDS)]
            first = conn.execute(_FIRST).fetchone()
        rule_of = {iid: rule for iid, rule, _ in opened}
        out: dict[str, Any] = {
            "since": first[0] if first else None,
            "proposed": sum(
                1 for _, rule, l1 in opened if rule == fix_type and l1 == "propose_runbook"
            ),
            "ran": {"person": 0, "medic": 0},
            "cleared": 0, "failed": 0, "not_back": 0, "not_verified": 0,
            "escalations": 0, "lowered_by_medic": 0, "resets": [],
        }  # fmt: skip
        waiting: dict[str, str] = {}  # incident -> who ran, until its fix_verified
        names = {
            "cleared": "cleared", "not_cleared": "failed",
            "not_back": "not_back", "not_verified": "not_verified",
        }  # fmt: skip
        for rec in rows:
            body, kind = rec["body"], rec["type"]
            if kind == "store_reset" or (
                kind == "anchor" and body.get("reason") == "early_purge"
            ):
                out["resets"].append(
                    {"kind": "store_reset" if kind == "store_reset" else "early_purge",
                     "at": rec["at"]}
                )  # fmt: skip
            elif kind == "level_changed" and body["fix_type"] == fix_type:
                if body["who"] == MEDIC:
                    out["lowered_by_medic"] += 1
                    out["escalations"] += body.get("cause") == "two_failures"
            elif kind == "fix_run" and rule_of.get(body["incident_id"]) == fix_type:
                if body["checks"]["result"] == "passed":
                    who = "medic" if body["who"] == MEDIC else "person"
                    out["ran"][who] += 1
                    waiting[body["incident_id"]] = who
            elif kind == "fix_verified" and body["incident_id"] in waiting:
                del waiting[body["incident_id"]]
                out[names[body["result"]]] += 1
        return out
