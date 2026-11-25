"""Restart soc-daemon on Compose, from one click or on its own. On Helm a
click clears a hang by deleting the daemon pod (see `_helm_restart`).

On its own (level L2, levels.py) Medic's loop hands a routed death incident to a
thread that takes this lock and runs the same checks and the same restart, plus
three more: the level is L2, the stored would-have L2 is `run_runbook` (so
`runtime_untrusted` stays at the click), and fewer than 3 restarts by Medic in
24 hours. A person's click is never capped.

One function: the checks, then a POST of the container id those checks just
read (on Helm a DELETE of the pod UID they read), then a verify after the
runbook's wait. No executor class and no second
action. A refused check is a fix_run that runs nothing. On start, a fix_run
with no fix_verified is closed: verified if its deadline has passed, otherwise
"not verified: Medic stopped", so a later click is not blocked forever.

`death_cleared` is the verify. A later hang clear can pass `passes` (the
counters that must have advanced) without a second function.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from services.medic.api.decisions import Problem
from services.medic.engine.suppress import selects
from services.medic.levels import DAILY_MAX, FIX_TYPES, MEDIC, RULE_ID, Levels
from services.medic.sensors import kube
from services.medic.sensors.containers import Containers
from services.medic.sensors.daemon import DaemonHealth
from services.medic.store import StoreError, open_reader
from services.medic.store.writer import utc_now

log = logging.getLogger("services.medic")

SIBLING_RULE = "pipeline.daemon-component-failed"
HUNG_RULE = "pipeline.daemon-component-hung-compose"
HUNG_SIBLING = "pipeline.daemon-component-hung"
_COVERED_RULES = frozenset({RULE_ID, SIBLING_RULE, HUNG_RULE, HUNG_SIBLING})
_HANG_RULES = frozenset({HUNG_RULE, HUNG_SIBLING})
# services/daemon/metrics.py _PASS_COMPONENTS. kafka has no counter.
_PASS_NAMES = ("poller", "processor", "scheduler", "orchestrator")
# services/daemon/scheduler.py counts a pass, then sleeps 60s. 90s covers that
# sleep and the work before the next pass, so a healthy loop has moved.
_SCHEDULER_GAP_S = 90.0
RUNBOOK_ID = "restart-soc-daemon"
SERVICE = "soc-daemon"
# Compose stop_grace_period. The proxy refuses a shorter `t`.
GRACE_S = 50
_OPEN = ("firing", "resolving")
_CHECKS = ("open", "firing", "runbook", "upgrade", "container", "idle")
_SERVICE_LABEL = "com.docker.compose.service"
_PROXY_S = 15.0
_RESTART_S = 90.0
_HEALTH_S = 5.0
# Helm: the API server, and the restart ServiceAccount's token. Its Role can delete
# the daemon pod and nothing else; the read token (kube._TOKEN_PATH) never deletes.
_KUBE = "https://kubernetes.default.svc:443"
_RESTART_TOKEN = Path("/var/run/secrets/vigil-medic-restart/token")
_POD_WAIT_S = 80.0

_FIXES = (
    "SELECT record FROM records"
    " WHERE json_extract(record, '$.type') IN ('fix_run', 'fix_verified')"
    " ORDER BY seq"
)
_OPENED = (
    "SELECT record FROM records"
    " WHERE json_extract(record, '$.type') = 'incident_opened'"
    " AND json_extract(record, '$.body.incident_id') = ?"
)


def death_cleared(
    status: int | None,
    body: Mapping[str, Any] | None,
    *,
    passes: Mapping[str, int] | None = None,
) -> str:
    """`cleared`, `not_cleared`, or `not_back`.

    Cleared: /health is 200 and every component is running or disabled (a
    disabled orchestrator is not a death). `passes`, when a later hang clear
    supplies it, must each have advanced. No body is `not_back`.
    """
    components = body.get("components") if isinstance(body, dict) else None
    if not isinstance(components, dict) or not components:
        return "not_back"
    alive = all(
        isinstance(v, str) and v in ("running", "disabled") for v in components.values()
    )
    if status != 200 or not alive:
        return "not_cleared"
    if passes is not None:
        got = body.get("passes") if isinstance(body, dict) else None
        if not isinstance(got, dict) or any(
            not isinstance(got.get(name), int) or got[name] <= previous
            for name, previous in passes.items()
        ):
            return "not_cleared"
    return "cleared"


def _stamp(moment: datetime) -> str:
    text = (
        moment.astimezone(UTC)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")
        .rstrip("0")
        .removesuffix(".")
    )
    return text + "Z"


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Docker sends nanoseconds; fromisoformat takes microseconds.
    if "." in text:
        head, rest = text.split(".", 1)
        frac, zone = rest, ""
        for i, ch in enumerate(rest):
            if ch in "+-":
                frac, zone = rest[:i], rest[i:]
                break
        text = head + "." + frac[:6] + zone
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


class Fixer:
    """The click, and the verifies that follow it. One restart at a time."""

    def __init__(self, medic, *, persistence: bool = True) -> None:
        self.medic = medic
        self.levels = Levels(medic, persistence=persistence)
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        # seq -> (monotonic time, passes from the first /health of the new process).
        # Not a fix_run field: that record's additionalProperties is false.
        self._hang_base: dict[int, tuple[float, dict]] = {}
        self._hang_tries: dict[int, int] = {}
        self.daemon, self.proxy = _addrs(medic)

    def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def settle(self) -> None:
        """Close fix_runs the last process left unverified."""
        with self._lock:
            self._settle()

    def run(self, incident_id: str, who: str) -> dict | Problem:
        """Checks, then the restart. 201 is the fix_run; a refusal is 409."""
        with self._lock:
            return self._run(incident_id, who)

    def on_stored(self, stored: list[dict]) -> None:
        """The loop's hook: a death incident stored as routed runs on its own when
        its fix type is at L2. Off the loop's thread, which the restart POST (up to
        90 s) would stall until the watchdog ended Medic."""
        try:
            for rec in stored:
                body = rec["body"]
                if rec["type"] != "incident_opened" or body.get("route") != "routed":
                    continue
                if (body.get("rule") or {}).get("id") != RULE_ID:
                    continue
                if self.levels.level(RULE_ID) == "L2":
                    threading.Thread(
                        target=self._on_its_own,
                        args=(body["incident_id"],),
                        name="medic-fix",
                        daemon=True,
                    ).start()
        except Exception as exc:  # noqa: BLE001 (never the loop's problem)
            log.error("fix on its own not started: %s", type(exc).__name__)

    def _on_its_own(self, incident_id: str) -> None:
        try:
            with self._lock:
                self._run(incident_id, MEDIC, auto=True)
        except Exception as exc:  # noqa: BLE001 (a thread's failure is only logged)
            log.error("fix on its own failed: %s", type(exc).__name__)

    def reconcile(self) -> None:
        try:
            self.levels.reconcile()
        except Exception as exc:  # noqa: BLE001 (never the loop's problem)
            log.error("levels not reconciled: %s", type(exc).__name__)

    def fix_types(self) -> list[dict]:
        """Each fix type's level and track record, for GET /v1/pack."""
        if self.medic.shape != "compose":
            return []
        return [self.levels.view(fix_type) for fix_type in FIX_TYPES]

    def _settle(self) -> None:
        now = time.time()
        records = _records(self.medic.writer.root)
        for rec in records:
            body = rec["body"]
            if rec["type"] != "fix_run" or body["checks"]["result"] != "passed":
                continue
            if _verified_after(rec, records):
                continue
            self._excuse(rec)
            when = _parse_time(body.get("verify_at"))
            if when is not None and when.timestamp() <= now:
                self._verify(rec)
            else:
                self._not_verified(rec)

    def _run(self, incident_id: str, who: str, *, auto: bool = False) -> dict | Problem:
        helm = self.medic.shape == "helm"
        if self.medic.shape != "compose" and not helm:
            return self._refuse(incident_id, who, "not_compose", [], None, None)
        row = _status_row(self.medic.engine, incident_id)
        if row is None or row.get("state") not in _OPEN:
            if _opened(self.medic.writer.root, incident_id) is None:
                return Problem(404, "not_found")
            return self._refuse(incident_id, who, "not_open", [], None, None)
        passed = ["open"]
        if row.get("state") != "firing" or row.get("eval") != "true":
            return self._refuse(incident_id, who, "not_firing", passed, None, None)
        passed.append("firing")
        opened = _opened(self.medic.writer.root, incident_id)
        runbook = (opened or {}).get("body", {}).get("runbook")
        if not isinstance(runbook, dict) or runbook.get("id") != RUNBOOK_ID:
            return self._refuse(incident_id, who, "no_runbook", passed, opened, None)
        passed.append("runbook")
        refused = self._level_check(incident_id, who, passed, opened, auto)
        if refused is not None:
            return refused
        if self.medic.engine.upgrade.active(time.time()):
            return self._refuse(
                incident_id, who, "upgrade_window", passed, opened, None
            )
        passed.append("upgrade")
        if helm:
            return self._helm_restart(incident_id, who, passed, opened, runbook)
        if self.proxy is None:
            return self._refuse(incident_id, who, "no_container", passed, opened, None)
        try:
            found = _candidates(self.proxy)
        except httpx.HTTPError:
            return self._refuse(incident_id, who, "no_container", passed, opened, None)
        if not found:
            return self._refuse(incident_id, who, "no_container", passed, opened, None)
        records = _records(self.medic.writer.root)
        opened_at = _machine_opened(self.medic.engine, incident_id)
        ready = [
            (cid, started)
            for cid, started in found
            if _start_ok(started, opened_at, records, cid)
        ]
        passed.append("container")
        if not ready:
            return self._refuse(
                incident_id,
                who,
                "container_restarted",
                passed,
                opened,
                found[0][0],
            )
        if _awaiting(records):
            return self._refuse(
                incident_id, who, "fix_running", passed, opened, ready[0][0]
            )
        passed.append("idle")
        if auto and self.levels.level(RULE_ID) != "L2":  # lowered while we checked
            return self._refuse(
                incident_id, who, "not_on_its_own", passed, opened, ready[0][0]
            )
        # Before the POST, so a sensor that sees the new start is not a marker.
        # A POST that does not restart puts the previous excuse back.
        previous = self.medic.engine.upgrade.excused.get(SERVICE)
        self.medic.engine.upgrade.note_recorded(SERVICE, time.time())
        container_id = None
        for cid, _started in ready:
            if _restart(self.proxy, cid) == 200:
                container_id = cid
                break
        if container_id is None:
            self.medic.engine.upgrade.restore_excuse(SERVICE, previous)
            return self._refuse(
                incident_id, who, "restart_failed", passed, opened, ready[0][0]
            )
        new_started = _started_after(self.proxy, container_id)
        covered = _covered(self.medic.engine, incident_id)
        record = self._write(
            incident_id,
            who,
            "passed",
            passed,
            opened,
            container_id,
            covered,
            started_at=new_started,
            verify_after_s=int(runbook["verify_after_s"]),
        )
        if isinstance(record, Problem):
            return record
        self._arm(record)
        if auto:
            self.levels.restarted(RULE_ID)
        return {"record": record}

    def _helm_restart(
        self, incident_id: str, who: str, passed: list[str], opened: dict, runbook: dict
    ) -> dict | Problem:
        """The Helm counterpart of the container checks and the restart: read the
        daemon pod, refuse mid-rollout or a pod that restarted since the incident
        opened, then DELETE that pod with its UID as a precondition."""
        sts = self.daemon[0].split(".")[0] if self.daemon else None
        read = asyncio.run(_helm_read(sts)) if sts else None
        if read is None or read[1] is None:
            return self._refuse(incident_id, who, "no_container", passed, opened, None)
        workload, pod = read
        name, uid, started = _pod_state(pod)
        pod_id = f"{name}:{uid}"
        if _mid_rollout(workload):
            return self._refuse(incident_id, who, "rollout", passed, opened, pod_id)
        records = _records(self.medic.writer.root)
        opened_at = _machine_opened(self.medic.engine, incident_id)
        ok = _start_ok(started, opened_at, records, pod_id)
        passed.append("container")
        if not ok:
            return self._refuse(
                incident_id, who, "container_restarted", passed, opened, pod_id
            )
        if _awaiting(records):
            return self._refuse(incident_id, who, "fix_running", passed, opened, pod_id)
        passed.append("idle")
        previous = self.medic.engine.upgrade.excused.get(SERVICE)
        self.medic.engine.upgrade.note_recorded(SERVICE, time.time())
        deleted_at = datetime.now(UTC)
        status = asyncio.run(_helm_delete(name, uid))
        if status not in (200, 202):
            self.medic.engine.upgrade.restore_excuse(SERVICE, previous)
            if status == 409:  # the UID moved on: nothing was deleted
                log.warning("fix %s: pod uid changed since the checks", incident_id)
            return self._refuse(
                incident_id, who, "restart_failed", passed, opened, pod_id
            )
        fresh = asyncio.run(_helm_new_pod(sts, uid))
        if fresh is not None:
            pod_id, started = f"{fresh[0]}:{fresh[1]}", fresh[2]
        record = self._write(
            incident_id,
            who,
            "passed",
            passed,
            opened,
            pod_id,
            _covered(self.medic.engine, incident_id),
            # Without the new pod's start, any process that began after the delete.
            started_at=started if fresh is not None else deleted_at,
            verify_after_s=int(runbook["verify_after_s"]),
        )
        if isinstance(record, Problem):
            return record
        self._arm(record)
        return {"record": record}

    def _level_check(
        self, incident_id: str, who: str, passed: list[str], opened: dict, auto: bool
    ) -> dict | Problem | None:
        """L0 refuses a click. On its own also needs L2, a stored L2 that runs the
        runbook, and room under the daily limit (reaching it lowers the level)."""
        fix_type = _rule_id(opened)
        level = self.levels.level(fix_type)
        why = None
        if level == "L0":
            why = "observe"
        elif auto and level != "L2":
            why = "not_on_its_own"
        elif auto and _would_l2(opened) != "run_runbook":
            why = "not_eligible"
        elif auto and self.levels.restarts_today(fix_type) >= DAILY_MAX:
            self.levels.limit_reached(fix_type)
            why = "daily_limit"
        if why is None:
            return None
        return self._refuse(incident_id, who, why, passed, opened, None)

    def _refuse(
        self,
        incident_id: str,
        who: str,
        result: str,
        passed: list[str],
        opened: dict | None,
        container_id: str | None,
    ) -> dict | Problem:
        covered = [incident_id]
        written = self._write(
            incident_id, who, result, passed, opened, container_id, covered
        )
        if isinstance(written, Problem):
            return written
        return Problem(409, "fix_refused", reason=result)

    def _write(
        self,
        incident_id: str,
        who: str,
        result: str,
        passed: list[str],
        opened: dict | None,
        container_id: str | None,
        covered: list[str],
        *,
        started_at: datetime | None = None,
        verify_after_s: int | None = None,
    ) -> dict | Problem:
        would = "notify_admin"
        if isinstance(opened, dict):
            level = opened.get("body", {}).get("would_have", {}).get("L1")
            if isinstance(level, str):
                would = level
        body: dict[str, Any] = {
            "incident_id": incident_id,
            "fix_type": "restart_service",
            "would_have": would,
            "action": {"type": "restart_service", "service": SERVICE},
            "level": self.levels.level(_rule_id(opened)),
            "who": who,
            "checks": {"result": result, "passed": passed},
            "covered": covered,
            "dependencies": _dependencies(self.medic.engine, opened),
            "queue_depth": _queue_depth(self.medic.engine),
        }
        if container_id is not None:
            body["container_id"] = container_id
        if started_at is not None:
            body["started_at"] = _stamp(started_at)
        if verify_after_s is not None:
            body["verify_at"] = _stamp(
                datetime.now(UTC) + timedelta(seconds=verify_after_s)
            )
        try:
            record = self.medic.writer.append(
                {"v": 1, "at": utc_now(), "type": "fix_run", "body": body}
            )
        except (StoreError, sqlite3.Error) as exc:
            log.warning("fix_run not stored: %s", type(exc).__name__)
            return Problem(503, "busy")
        log.info("fix %s %s", incident_id, result)
        return record

    def _arm(self, record: dict, *, delay: float | None = None) -> None:
        if delay is None:
            when = _parse_time(record["body"].get("verify_at"))
            if when is None:
                return
            delay = max(0.0, when.timestamp() - time.time())
        timer = threading.Timer(delay, self._fire, (record["seq"],))
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _fire(self, seq: int) -> None:
        with self._lock:
            records = _records(self.medic.writer.root)
            rec = next((r for r in records if r.get("seq") == seq), None)
            if rec is None or _verified_after(rec, records):
                return
            self._verify(rec)

    def _verify(self, rec: dict) -> None:
        status, body = None, None
        if self.daemon is not None:
            try:
                status, body = _health(self.daemon)
            except httpx.HTTPError:
                status, body = None, None
        passes = None
        if _covers_hang(self.medic.writer.root, rec):
            kind, passes = self._hang_passes(rec, status, body)
            if kind == "wait":
                return
            if kind in ("not_back", "not_cleared"):
                self._verified(rec, kind, None)
                return
        result = (
            "not_back"
            if self.daemon is None
            else death_cleared(status, body, passes=passes)
        )
        self._verified(rec, result, None)

    def _hang_passes(
        self, rec: dict, status: int | None, body: Any
    ) -> tuple[str, dict[str, int] | None]:
        """('wait', None) when the second read is still armed, else the baseline
        for every component that is running now. 'not_back' when the new process
        never answered."""
        seq = rec["seq"]
        started = _parse_time(rec["body"].get("started_at"))
        fresh = status == 200 and _new_process(body, started)
        saved = self._hang_base.get(seq)
        if saved is None:
            if (
                fresh
                and isinstance(body, dict)
                and isinstance(body.get("passes"), dict)
            ):
                self._hang_base[seq] = (time.monotonic(), body["passes"])
                self._arm(rec, delay=_SCHEDULER_GAP_S)
                return "wait", None
            tries = self._hang_tries.get(seq, 0) + 1
            self._hang_tries[seq] = tries
            if tries < 6:
                self._arm(rec, delay=10.0)
                return "wait", None
            return "not_back", None
        at, baseline = saved
        remain = _SCHEDULER_GAP_S - (time.monotonic() - at)
        if remain > 1:
            self._arm(rec, delay=remain)
            return "wait", None
        if not fresh:
            return "not_back", None
        built = _running_baseline(baseline, body)
        if built is None:
            return "not_cleared", None
        return "passes", built

    def _not_verified(self, rec: dict) -> None:
        self._verified(rec, "not_verified", "medic_stopped")

    def _verified(self, rec: dict, result: str, detail: str | None) -> None:
        for incident_id in rec["body"]["covered"]:
            body: dict[str, Any] = {"incident_id": incident_id, "result": result}
            if detail is not None:
                body["detail"] = detail
            if not self._append_verified(body):
                # Still awaiting: try again shortly, or the next click is blocked.
                self._arm(rec, delay=5.0)
                return
        self._hang_base.pop(rec["seq"], None)
        self._hang_tries.pop(rec["seq"], None)
        log.info("fix verified %s", result)
        if rec["body"]["who"] == MEDIC:
            opened = _opened(self.medic.writer.root, rec["body"]["incident_id"])
            self.levels.verified(_rule_id(opened), result)

    def _append_verified(self, body: dict[str, Any]) -> bool:
        for _ in range(3):
            try:
                self.medic.writer.append(
                    {"v": 1, "at": utc_now(), "type": "fix_verified", "body": body}
                )
                return True
            except (StoreError, sqlite3.Error) as exc:
                log.warning("fix_verified not stored: %s", type(exc).__name__)
                time.sleep(0.2)
        return False

    def _excuse(self, rec: dict) -> None:
        when = _parse_time(rec.get("at"))
        if when is not None:
            self.medic.engine.upgrade.note_recorded(SERVICE, when.timestamp())


def _addrs(medic) -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
    daemon = proxy = None
    for sensor in medic.sensors:
        if isinstance(sensor, DaemonHealth):
            daemon = (sensor.host, sensor.port)
        elif isinstance(sensor, Containers):
            proxy = (sensor.host, sensor.port)
    return daemon, proxy


def _rule_id(opened: dict | None) -> str | None:
    rule = ((opened or {}).get("body") or {}).get("rule") or {}
    return rule.get("id")


def _would_l2(opened: dict | None) -> str | None:
    would = ((opened or {}).get("body") or {}).get("would_have") or {}
    return (would.get("L2") or {}).get("action")


def _status_row(engine, incident_id: str) -> dict | None:
    for row in engine.status():
        if row.get("incident_id") == incident_id:
            return row
    return None


def _machine_opened(engine, incident_id: str) -> float | None:
    for slots in engine.machines.values():
        for machine in slots.values():
            if machine.get("incident_id") == incident_id:
                return machine.get("opened_at")
    return None


def _covered(engine, incident_id: str) -> list[str]:
    """This incident, plus other open soc-daemon death or hang incidents.
    One restart writes fix_verified for each of them."""
    found = {incident_id}
    for row in engine.status():
        if (
            row.get("rule") in _COVERED_RULES
            and row.get("state") in _OPEN
            and row.get("incident_id")
        ):
            found.add(row["incident_id"])
    return sorted(found)


def _covers_hang(data_dir, rec: dict) -> bool:
    for incident_id in rec["body"]["covered"]:
        opened = _opened(data_dir, incident_id)
        rule = ((opened or {}).get("body") or {}).get("rule") or {}
        if rule.get("id") in _HANG_RULES:
            return True
    return False


def _new_process(body: Any, started: datetime | None) -> bool:
    """True when /health is the process that began at or after `started`
    (Docker State.StartedAt). A naive timestamp is UTC, as the daemon writes it."""
    if started is None or not isinstance(body, dict):
        return False
    raw = body.get("started_at")
    if not isinstance(raw, str):
        return False
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        begun = datetime.fromisoformat(text)
    except ValueError:
        return False
    if begun.tzinfo is None:
        begun = begun.replace(tzinfo=UTC)
    return begun.astimezone(UTC) + timedelta(seconds=5) >= started


def _running_baseline(baseline: dict, body: Any) -> dict[str, int] | None:
    """Baseline passes for components that are running now. None if one of
    those has no integer baseline, so the clear cannot succeed on /health alone."""
    components = body.get("components") if isinstance(body, dict) else None
    if not isinstance(components, dict) or not isinstance(baseline, dict):
        return None
    out: dict[str, int] = {}
    for name in _PASS_NAMES:
        if components.get(name) != "running":
            continue
        previous = baseline.get(name)
        if type(previous) is not int:
            return None
        out[name] = previous
    return out


def _dependencies(engine, opened: dict | None) -> list[str]:
    if not isinstance(opened, dict):
        return []
    rule = (opened.get("body") or {}).get("rule") or {}
    rule_id = rule.get("id")
    if not isinstance(rule_id, str):
        return []
    loaded = engine.by_id.get(rule_id)
    fault = None if loaded is None else loaded.rule.get("fault")
    out: set[str] = set()
    for entry in engine.suppression:
        if not selects(entry["children"], rule_id, fault):
            continue
        for row in engine.status():
            if row.get("state") not in _OPEN or not row.get("incident_id"):
                continue
            parent = engine.by_id.get(row["rule"])
            pfault = None if parent is None else parent.rule.get("fault")
            if selects(entry["parent"], row["rule"], pfault):
                out.add(row["incident_id"])
    return sorted(out)


def _queue_depth(engine) -> int | str:
    rows = engine.history.samples.get("daemon_status.processor") or []
    if not rows or rows[-1][1].get("outcome") != "ok":
        return "unknown"
    for value in rows[-1][1].get("values", []):
        if value.get("key") == "queue_depth" and value.get("state") == "present":
            depth = value.get("value")
            if type(depth) is int and depth >= 0:
                return depth
    return "unknown"


def _records(data_dir) -> list[dict]:
    try:
        with open_reader(data_dir) as conn:
            rows = conn.execute(_FIXES).fetchall()
    except (StoreError, sqlite3.Error, json.JSONDecodeError):
        return []
    return [json.loads(row[0]) for row in rows]


def _opened(data_dir, incident_id: str) -> dict | None:
    try:
        with open_reader(data_dir) as conn:
            row = conn.execute(_OPENED, (incident_id,)).fetchone()
    except (StoreError, sqlite3.Error, json.JSONDecodeError):
        return None
    return json.loads(row[0]) if row else None


def _awaiting(records: list[dict]) -> bool:
    return any(
        rec["type"] == "fix_run"
        and rec["body"]["checks"]["result"] == "passed"
        and not _verified_after(rec, records)
        for rec in records
    )


def _verified_after(rec: dict, records: list[dict]) -> bool:
    seq = rec["seq"]
    done = {
        other["body"]["incident_id"]
        for other in records
        if other["type"] == "fix_verified" and other["seq"] > seq
    }
    return all(incident_id in done for incident_id in rec["body"]["covered"])


def _start_ok(
    started: datetime | None,
    opened_at: float | None,
    records: list[dict],
    container_id: str,
) -> bool:
    """True when this container has not restarted since the incident opened,
    except by a fix_run Medic recorded."""
    if started is None or opened_at is None:
        return False
    if started.timestamp() <= opened_at + 2:
        return True
    for rec in records:
        body = rec["body"]
        if rec["type"] != "fix_run" or body["checks"]["result"] != "passed":
            continue
        if body.get("container_id") != container_id:
            continue
        recorded = _parse_time(body.get("started_at"))
        if recorded is None or abs((recorded - started).total_seconds()) <= 2:
            return True
    return False


def _candidates(proxy: tuple[str, int]) -> list[tuple[str, datetime | None]]:
    """soc-daemon containers the proxy lists, running ones first."""
    host, port = proxy
    listed = _proxy(
        host,
        port,
        "GET",
        "/containers/json?all=true&filters="
        + urllib.parse.quote(
            json.dumps(
                {"label": [f"{_SERVICE_LABEL}={SERVICE}"]}, separators=(",", ":")
            )
        ),
        _PROXY_S,
    )
    if listed.status_code != 200 or not isinstance(listed.json(), list):
        return []
    rows = sorted(listed.json(), key=lambda row: row.get("State") != "running")
    out = []
    for row in rows:
        cid = row.get("Id")
        if isinstance(cid, str) and (one := _one(proxy, cid)) is not None:
            out.append(one)
    return out


def _one(
    proxy: tuple[str, int], container_id: str
) -> tuple[str, datetime | None] | None:
    host, port = proxy
    inspected = _proxy(host, port, "GET", f"/containers/{container_id}/json", _PROXY_S)
    if inspected.status_code != 200:
        return None
    doc = inspected.json()
    state = doc.get("State") if isinstance(doc, dict) else None
    started = _parse_time(state.get("StartedAt") if isinstance(state, dict) else None)
    full = doc.get("Id") if isinstance(doc, dict) else None
    if isinstance(full, str):
        container_id = full
    return container_id, started


def _started_after(proxy: tuple[str, int], container_id: str) -> datetime | None:
    """StartedAt after the restart. None if the read fails, so a later click is
    not compared against the time from before the restart."""
    for _ in range(3):
        try:
            found = _one(proxy, container_id)
        except httpx.HTTPError:
            found = None
        if found is not None and found[1] is not None:
            return found[1]
        time.sleep(0.2)
    return None


def _restart(proxy: tuple[str, int], container_id: str) -> int:
    host, port = proxy
    try:
        response = _proxy(
            host,
            port,
            "POST",
            f"/containers/{container_id}/restart?t={GRACE_S}",
            _RESTART_S,
        )
    except httpx.HTTPError:
        return 0
    return response.status_code


async def _kube_session(token_path: Path):
    """(client, namespace, token) for the API server; None if a file is unreadable."""
    ns, bad = await kube._read_namespace(kube._NAMESPACE_PATH)
    token, worse = await kube._read_token(token_path)
    if bad or worse or ns is None:
        return None
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False, verify=str(kube._CA_PATH)
    )
    return client, ns, token


async def _helm_read(sts: str) -> tuple[dict, dict | None] | None:
    """The daemon StatefulSet and its pod (None if absent), read with the read-only
    token. None when the StatefulSet cannot be read. replicas is 1, so the pod is -0."""
    try:
        session = await _kube_session(kube._TOKEN_PATH)
        if session is None:
            return None
        client, ns, token = session
        async with client:
            deadline = time.monotonic() + _PROXY_S
            got, error = await kube._kube_get(
                client,
                f"{_KUBE}/apis/apps/v1/namespaces/{ns}/statefulsets/{sts}",
                None,
                token,
                deadline,
            )
            if error is not None or not isinstance(got, dict):
                return None
            pod, error = await kube._kube_get(
                client,
                f"{_KUBE}/api/v1/namespaces/{ns}/pods/{sts}-0",
                None,
                token,
                deadline,
            )
            return got, pod if error is None and isinstance(pod, dict) else None
    except (httpx.HTTPError, OSError):
        return None


async def _helm_delete(name: str, uid: str) -> int:
    """DELETE the pod with the restart token, only if its UID is still `uid`.
    409 is a UID mismatch (nothing deleted); 0 is an error."""
    try:
        session = await _kube_session(_RESTART_TOKEN)
        if session is None:
            return 0
        client, ns, token = session
        async with client:
            response = await client.request(
                "DELETE",
                f"{_KUBE}/api/v1/namespaces/{ns}/pods/{name}",
                json={
                    "kind": "DeleteOptions",
                    "apiVersion": "v1",
                    "preconditions": {"uid": uid},
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=_PROXY_S,
            )
            return response.status_code
    except (httpx.HTTPError, OSError):
        return 0


async def _helm_new_pod(sts: str, old_uid: str) -> tuple[str, str, datetime] | None:
    """The replacement pod (name, uid, container start) once its container runs."""
    end = time.monotonic() + _POD_WAIT_S
    while time.monotonic() < end:
        await asyncio.sleep(2)
        read = await _helm_read(sts)
        if read is not None and read[1] is not None:
            name, uid, started = _pod_state(read[1])
            if uid != old_uid and started is not None:
                return name, uid, started
    return None


def _pod_state(pod: dict) -> tuple[str, str, datetime | None]:
    """Name, UID and the daemon container's start. No start while the pod is
    terminating or the container is not running."""
    meta = pod.get("metadata") or {}
    started = None
    if "deletionTimestamp" not in meta:
        for item in (pod.get("status") or {}).get("containerStatuses") or []:
            running = (item.get("state") or {}).get("running")
            if item.get("name") == "daemon" and isinstance(running, dict):
                started = _parse_time(running.get("startedAt"))
    return str(meta.get("name", "")), str(meta.get("uid", "")), started


def _mid_rollout(workload: dict) -> bool:
    """A StatefulSet that has not finished rolling out its latest spec."""
    status = workload.get("status") or {}
    want = (workload.get("spec") or {}).get("replicas", 1)
    return (
        status.get("observedGeneration")
        != (workload.get("metadata") or {}).get("generation")
        or status.get("currentRevision") != status.get("updateRevision")
        or (status.get("updatedReplicas") or 0) < want
        or (status.get("readyReplicas") or 0) < want
    )


def _health(addr: tuple[str, int]) -> tuple[int, Any]:
    host, port = addr
    response = httpx.get(
        f"http://{host}:{port}/health", timeout=_HEALTH_S, trust_env=False
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


def _proxy(
    host: str, port: int, method: str, path: str, timeout: float
) -> httpx.Response:
    return httpx.request(
        method,
        f"http://{host}:{port}{path}",
        content=b"" if method == "POST" else None,
        timeout=timeout,
        trust_env=False,
    )
