"""Enforced intent: apply a declared directory's INTENT.md (#5068).

With ``VIGIL_INTENT_DIR`` set, the daemon (a singleton, the only applier)
polls ``<dir>/INTENT.md`` and ``<dir>/REVISION`` on a fixed interval. When
their content hash changes it applies the revision:

- a parse failure, unknown key or wrong-typed value rejects it whole;
- only keys whose declared value changed since the last applied revision
  are considered, each labeled against the current effective value;
  ``tighten``/``same`` are written, ``loosen`` is recorded as pending;
- ``act.*`` go to ``autonomy.ceiling``, ``investigate.*`` to
  ``orchestrator.settings``, the rest to the ``intent.applied`` row, which
  also records revision, per-key file/line, pending keys and state.

A poll with an unchanged hash writes nothing, so a local narrowing survives
until a commit changes that key. A missing directory keeps the last applied
revision with state ``degraded``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.config import get_settings
from core.daemon_config import DaemonConfig
from core.intent import (
    FIELDS_BY_KEY,
    INTENT_FILE_NAME,
    REVISION_FILE_NAME,
    IntentRejected,
    effective_values,
    label,
    parse_intent_strict,
)
from core.intent_report import effective_daemon_config
from core.response.ceiling import (
    Ceiling,
    CeilingRefusal,
    _locked_update,
    environment_locks_act,
)
from core.response.config import INTENT_APPLIED_KEY, applied_intent
from core.storage.config_service import get_config_service
from core.time import utcnow

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
ACTOR = "system"
ORCHESTRATOR_KEY = "orchestrator.settings"
_CEILING_ATTR = {
    "act.reversible": "act_reversible",
    "act.irreversible": "act_irreversible",
    "act.four_eyes": "four_eyes",
}


class StoreWriteFailed(Exception):
    """A store was not written; the revision is retried on the next poll."""


def intent_dir() -> Optional[Path]:
    raw = get_settings().vigil_intent_dir.strip()
    return Path(raw) if raw else None


def _snapshot(directory: Path) -> tuple[str, str, str]:
    """``(hash, revision, INTENT.md text)``; raises ``OSError`` when unreadable."""
    content = (directory / INTENT_FILE_NAME).read_bytes()
    try:
        marker = (directory / REVISION_FILE_NAME).read_bytes()
    except FileNotFoundError:
        marker = b""
    digest = hashlib.sha256(content + b"\0" + marker).hexdigest()
    lines = marker.decode("utf-8", "replace").splitlines()
    revision = (lines[0].strip()[:200] if lines else "") or "unknown"
    return digest, revision, content.decode("utf-8", "replace")


def _write_row(row: Dict[str, Any], reason: str, actor: str = ACTOR) -> None:
    ok = get_config_service(actor).set_system_config(
        key=INTENT_APPLIED_KEY,
        value=row,
        description="Declared INTENT.md revision applied by the daemon",
        config_type="intent",
        change_reason=reason,
    )
    if not ok:
        raise StoreWriteFailed(INTENT_APPLIED_KEY)
    applied_intent(fresh=True)


def _effective() -> Dict[str, Any]:
    return effective_values(effective_daemon_config(DaemonConfig.from_env()))


def _apply_act(wants: Dict[str, Any], revision: str, pending: Set[str]) -> None:
    """Write the act keys that do not loosen, decided under the row lock."""
    locked = environment_locks_act()

    def change(current: Ceiling) -> tuple[Ceiling, str]:
        edits: Dict[str, Any] = {}
        for key, want in wants.items():
            attr = _CEILING_ATTR[key]
            have = getattr(current, attr)
            if locked and attr != "four_eyes":
                have = False  # the effective flag; the lock still wins
            if label(FIELDS_BY_KEY[key].rule, want, have) == "loosen":
                pending.add(key)
            else:
                pending.discard(key)
                edits[attr] = want
        summary = ", ".join(f"{k}={v}" for k, v in sorted(edits.items()))
        return (
            replace(current, **edits),
            f"INTENT.md revision {revision}: {summary or 'no change'}",
        )

    # Only ever lowers autonomy, so it lands during a restore's write pause.
    _locked_update(change, ACTOR, gated=False)


def _seed_orchestrator(effective: Dict[str, Any]) -> Dict[str, Any]:
    # No row yet: seed it with what runs now, or a row holding only a budget
    # would read as enabled=False to every reader.
    return {
        key.split(".", 1)[1]: effective[key]
        for key in FIELDS_BY_KEY
        if key.startswith("investigate.")
    }


def _apply_investigate(
    wants: Dict[str, Any],
    revision: str,
    effective: Dict[str, Any],
    pending: Set[str],
) -> None:
    service = get_config_service(ACTOR)
    stored = service.read_system_config(ORCHESTRATOR_KEY)
    current = stored or _seed_orchestrator(effective)
    names: Dict[str, Any] = {}
    for key, want in wants.items():
        name = key.split(".", 1)[1]
        # Re-decided on the row as read now: a narrowing since the label holds.
        have = current.get(name, effective[key])
        if label(FIELDS_BY_KEY[key].rule, want, have) == "loosen":
            pending.add(key)
        else:
            names[name] = want
    if not names or all(
        stored is not None and current.get(n) == v for n, v in names.items()
    ):
        return
    summary = ", ".join(f"{n}={v}" for n, v in sorted(names.items()))
    if not service.set_system_config(
        key=ORCHESTRATOR_KEY,
        value={**current, **names},
        description="Autonomous orchestrator settings",
        config_type="orchestrator",
        change_reason=f"INTENT.md revision {revision}: {summary}",
    ):
        raise StoreWriteFailed(ORCHESTRATOR_KEY)


def apply_once(directory: Path) -> None:
    """One poll. Writes only when the directory's content hash changed."""
    row: Dict[str, Any] = (
        get_config_service(ACTOR).read_system_config(INTENT_APPLIED_KEY) or {}
    )
    base = {**row, "directory": str(directory)}
    try:
        digest, revision, content = _snapshot(directory)
    except OSError as exc:
        reason = f"{directory} not readable ({exc.strerror or exc}); kept revision {row.get('revision') or 'none'}"
        if row.get("state") != "degraded" or row.get("reason") != reason:
            logger.warning("intent: %s", reason)
            _write_row({**base, "state": "degraded", "reason": reason}, reason)
        return

    if digest == row.get("hash"):
        if row.get("state") == "degraded":  # back, unchanged: nothing to apply
            rejected = row.get("rejected")
            state = "rejected" if rejected else "ok"
            reason = (
                rejected["reason"]
                if rejected
                else f"revision {row.get('revision')} applied"
            )
            _write_row({**base, "state": state, "reason": reason}, reason)
        return

    try:
        declared, lines = parse_intent_strict(content)
    except IntentRejected as exc:
        reason = f"revision {revision} rejected: {exc}"
        logger.warning("intent: %s", reason)
        rejected = {
            "revision": revision,
            "file": exc.file,
            "line": exc.line,
            "reason": reason,
        }
        _write_row(
            {
                **base,
                "hash": digest,
                "state": "rejected",
                "reason": reason,
                "rejected": rejected,
            },
            reason,
        )
        return

    previous: Dict[str, Any] = row.get("keys") or {}
    pending: Set[str] = set(row.get("pending") or []) & set(declared)
    values: Dict[str, Any] = dict(row.get("values") or {})
    changed = {
        key: value
        for key, value in declared.items()
        if key not in previous or previous[key].get("declared") != value
    }

    applied_intent(fresh=True)
    effective = _effective()
    act: Dict[str, Any] = {}
    investigate: Dict[str, Any] = {}
    for key, want in changed.items():
        group = key.split(".", 1)[0]
        if group == "act":
            act[key] = want  # decided under the ceiling's row lock
            continue
        if label(FIELDS_BY_KEY[key].rule, want, effective[key]) == "loosen":
            pending.add(key)
            continue
        pending.discard(key)
        if group == "investigate":
            investigate[key] = want
        else:
            values[key] = want

    if act:
        _apply_act(act, revision, pending)
    if investigate:
        _apply_investigate(investigate, revision, effective, pending)

    keys = {
        key: {
            "declared": value,
            "file": INTENT_FILE_NAME,
            "line": lines[key],
            # The revision that last changed this key's declared value.
            "revision": revision if key in changed else previous[key].get("revision"),
        }
        for key, value in declared.items()
    }
    # A key removed from the file is unmanaged; its value stays where it is.
    reason = f"revision {revision} applied"
    if pending:
        reason += f", {len(pending)} key(s) pending: {', '.join(sorted(pending))}"
    logger.info("intent: %s (%d key(s) changed)", reason, len(changed))
    _write_row(
        {
            **base,
            "revision": revision,
            "hash": digest,
            "state": "ok",
            "reason": reason,
            "applied_at": utcnow().isoformat(),
            "keys": keys,
            "values": values,
            "pending": sorted(pending),
            "rejected": None,
        },
        f"INTENT.md {reason}",
    )


def accept_pending(
    keys: List[str], revision: str, digest: Optional[str], user_id: str, who: str
) -> None:
    """A person's accept of pending loosenings (#5069).

    Writes each key's declared value into its store as ``user_id``, so every
    ``config.change`` entry names the person and the revision, then clears
    them from the row's pending list. The caller has checked the person and
    the stop; the revision is checked again on the row read here.
    """
    row = get_config_service(ACTOR).read_system_config(INTENT_APPLIED_KEY) or {}
    if row.get("revision") != revision or (digest and row.get("hash") != digest):
        raise CeilingRefusal(
            409, f"The applied revision is now {row.get('revision')}; review it again"
        )
    declared = {
        key: row["keys"][key]["declared"]
        for key in keys
        if key in (row.get("keys") or {})
    }
    if not declared:
        return
    if environment_locks_act() and any(
        k in ("act.reversible", "act.irreversible") and v is True
        for k, v in declared.items()
    ):
        raise CeilingRefusal(409, "The environment wins; Act was not saved.")
    reason = f"INTENT.md revision {revision}: pending accepted by {who}"
    groups: Dict[str, Dict[str, Any]] = {}
    for key, value in declared.items():
        groups.setdefault(key.split(".", 1)[0], {})[key] = value

    def summary(items: Dict[str, Any]) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(items.items()))

    if "act" in groups:
        act = groups["act"]

        def change(current: Ceiling) -> tuple[Ceiling, str]:
            if current.stopped:
                raise CeilingRefusal(409, "Stopped: resume first")
            edits = {_CEILING_ATTR[k]: v for k, v in act.items()}
            return replace(current, **edits), f"{reason}: {summary(act)}"

        _locked_update(change, user_id)

    if "investigate" in groups:
        service = get_config_service(user_id)
        current = service.read_system_config(ORCHESTRATOR_KEY) or _seed_orchestrator(
            _effective()
        )
        names = {k.split(".", 1)[1]: v for k, v in groups["investigate"].items()}
        if not service.set_system_config(
            key=ORCHESTRATOR_KEY,
            value={**current, **names},
            description="Autonomous orchestrator settings",
            config_type="orchestrator",
            change_reason=f"{reason}: {summary(groups['investigate'])}",
        ):
            raise StoreWriteFailed(ORCHESTRATOR_KEY)

    others = {
        k: v
        for k, v in declared.items()
        if k.split(".", 1)[0] not in ("act", "investigate")
    }
    # Re-read just before writing, so a poll since the first read is kept.
    row = get_config_service(ACTOR).read_system_config(INTENT_APPLIED_KEY) or row
    pending = [k for k in row.get("pending") or [] if k not in declared]
    note = f"{reason}: {summary(declared)}"
    _write_row(
        {
            **row,
            "values": {**(row.get("values") or {}), **others},
            "pending": pending,
            "reason": f"revision {row.get('revision')} applied; {note}",
        },
        note,
        actor=user_id,
    )


async def run_applier(directory: Path, shutdown_event: asyncio.Event) -> None:
    """Poll until shutdown. A failed poll is logged and retried next time."""
    logger.info("intent: applying %s every %ds", directory, POLL_SECONDS)
    while not shutdown_event.is_set():
        try:
            await asyncio.to_thread(apply_once, directory)
        except Exception:  # noqa: BLE001 - never stops the daemon
            logger.exception("intent: poll of %s failed; retrying", directory)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
