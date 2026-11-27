"""The intent report: declared INTENT.md keys beside the effective values.

One builder, :func:`intent_body`, is ``GET /api/config/intent`` and the /mcp
``get_intent_report`` tool. With a declared directory applied
(``VIGIL_INTENT_DIR``) it also says, per key, that it is managed, the file,
line and revision governing it, whether it drifted, whether a loosening is
pending a person, and whether an env value is ignored. The daemon logs
:func:`report_intent` once at startup.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.daemon_config import CeilingConfig, DaemonConfig
from core.intent import (
    FIELDS_BY_KEY,
    INTENT_FILE_NAME,
    IntentDiff,
    _normalize,
    diff_intent,
    effective_values,
    format_rows,
    intent_file,
    is_workflow_key,
    read_intent,
    workflow_label,
)
from core.response.ceiling import (
    CONFIDENCE_SWITCHES,
    environment_locks_act,
    try_read_stored_ceiling,
)
from core.response.config import applied_intent

logger = logging.getLogger(__name__)

# Delivery and signatures belong to Flux, Argo CD or Git, not to Vigil.
NOT_VERIFIED = (
    "Vigil did not verify this revision; verifying it is the job of the tool "
    "that delivers the files."
)


def _overlay_ceiling(config: DaemonConfig) -> DaemonConfig:
    """Effective act flags from the ceiling row, forced off by the environment.

    A missing row is the seeded off state. A failed read stays off and does
    not report a grant. The file still does not enforce.
    """
    sources = dict(config.sources)
    loaded = try_read_stored_ceiling()
    if loaded is None:
        return dataclasses.replace(config, sources=sources)
    stored, found = loaded
    locked = environment_locks_act()
    found_origin = "db" if found else "default"

    def origin(path: str) -> str:
        return "declared" if sources.get(path) == "declared" else found_origin

    sources["ceiling.act_reversible"] = (
        "env" if locked and stored.act_reversible else origin("ceiling.act_reversible")
    )
    sources["ceiling.act_irreversible"] = (
        "env"
        if locked and stored.act_irreversible
        else origin("ceiling.act_irreversible")
    )
    sources["ceiling.four_eyes"] = origin("ceiling.four_eyes")
    sources["ceiling.hold_crown_jewels"] = origin("ceiling.hold_crown_jewels")
    for attr in CONFIDENCE_SWITCHES:
        sources[f"ceiling.{attr}"] = origin(f"ceiling.{attr}")
    ceiling = CeilingConfig(
        act_reversible=False if locked else stored.act_reversible,
        act_irreversible=False if locked else stored.act_irreversible,
        four_eyes=stored.four_eyes,
        hold_crown_jewels=stored.hold_crown_jewels,
        **{attr: getattr(stored, attr) for attr in CONFIDENCE_SWITCHES},
    )
    return dataclasses.replace(config, ceiling=ceiling, sources=sources)


def applied_directory() -> Optional[Dict[str, Any]]:
    """The ``intent.applied`` row when a declared directory is enforced, else None.

    Every process reads it, so a backend replica without the directory mounted
    still reports what the daemon applied.
    """
    applied = applied_intent()
    return applied if applied.get("directory") else None


def report_path() -> Path:
    applied = applied_directory()
    if applied is not None:
        return Path(applied["directory"]) / INTENT_FILE_NAME
    return intent_file()


def intent_report(
    config: Optional[DaemonConfig] = None,
    path: Optional[Path] = None,
    *,
    include_same: bool = False,
) -> Optional[List[IntentDiff]]:
    """Rows for declared keys, or ``None`` when the manifest is unreadable.

    Differing rows only, unless ``include_same`` (the Settings card). With a
    declared directory applied, the declared values are the applied row's.
    """
    applied = applied_directory() if path is None else None
    if applied is not None:
        declared = {
            key: meta.get("declared")
            for key, meta in (applied.get("keys") or {}).items()
        }
    else:
        declared = read_intent(path)
    if declared is None:
        return None
    config = _overlay_ceiling(config or DaemonConfig.from_env())
    return diff_intent(
        declared,
        effective_values(config),
        config.sources,
        include_same=include_same,
    )


def report_intent(config: Optional[DaemonConfig] = None) -> None:
    """Log the report. Never raises: a broken manifest must not stop the daemon."""
    try:
        rows = intent_report(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intent report failed (non-fatal): %s", exc)
        return
    if rows is None:
        return
    if not rows:
        logger.info(
            "INTENT.md: no declared key differs from effective daemon config (%s)",
            report_path(),
        )
        return
    for line in format_rows(rows):
        logger.info(line)


def effective_daemon_config(config: DaemonConfig) -> DaemonConfig:
    """Effective config with the ceiling overlay applied.

    Env already lives on ``config``. The ceiling row is the other input.
    """
    return _overlay_ceiling(config)


def _env_ignored(key: str, declared: Any) -> Optional[Any]:
    """The env value a declared key overrides, when one is set and differs.

    The act flags have no env value, only locks, which count as drift.
    """
    setting = FIELDS_BY_KEY[key].setting
    if key.startswith("act.") or setting is None:
        return None
    settings = get_settings()
    if setting not in settings.model_fields_set:
        return None
    env = getattr(settings, setting)
    return env if _normalize(env) != _normalize(declared) else None


def _workflow_row(
    key: str,
    meta: Dict[str, Any],
    loaded: Optional[tuple],
    listed: set,
) -> Dict[str, Any]:
    """One managed ``workflows.<id>``: the declared entry beside the stored one."""
    declared = meta.get("declared") or {}
    stored = loaded[0].workflows.get(key.split(".", 1)[1]) if loaded else None
    if loaded is None:  # ceiling unread: say nothing about it
        effective: Any = None
        verdict = "unknown"
    elif stored is None:
        effective, verdict = "fleet", workflow_label(declared, None)
    else:
        effective = {"tier": stored.get("tier")}
        if stored.get("tier") == "act":
            effective["floor"] = stored.get("floor")
        if stored.get("bar") is not None:
            effective["bar"] = stored.get("bar")
        verdict = workflow_label(declared, stored)
    # Unread ceiling: a listed key stays pending, so the accept still offers it.
    pending = key in listed and verdict != "same"
    # Not as declared and not pending: Vigil's own lowering or a local change
    # (a narrowing, or following the fleet, which the guard let through).
    drift = verdict not in ("same", "unknown") and not pending
    return {
        "key": key,
        "declared": declared,
        "effective": effective,
        "source": "db" if stored else "default",
        "label": verdict,
        "managed": True,
        "file": meta.get("file"),
        "line": meta.get("line"),
        "revision": meta.get("revision"),
        "drift": drift,
        "drift_reason": (
            (
                # Vigil lowers a workflow only by dropping a declared Act to Assist; a
                # person's bar-only write keeps the entry's ``by``, the applier's included.
                "lowered by Vigil"
                if (stored or {}).get("by") == "system"
                and (stored or {}).get("tier") == "assist"
                and declared.get("tier") == "act"
                else "narrowed locally"
            )
            if drift
            else None
        ),
        "pending": pending,
        "env_ignored": False,
        "env_value": None,
    }


def intent_body() -> Dict[str, Any]:
    """The report body. Observe mode (no directory) leaves the managed fields empty."""
    applied = applied_intent(fresh=True)
    managed = applied if applied.get("directory") else {}
    rows = intent_report(include_same=True)
    keys: Dict[str, Any] = managed.get("keys") or {}
    listed = set(managed.get("pending") or [])
    flows = {k: m for k, m in keys.items() if is_workflow_key(k)}
    loaded = (
        try_read_stored_ceiling()
        if flows or any(k.startswith("act.") for k in listed)
        else None
    )

    def still_pending(r: IntentDiff) -> bool:
        """Listed, and its store still differs from the declared value.

        An act flag compares the stored flag: one an env lock forces off is drift.
        """
        if r.key not in listed:
            return False
        if r.key.startswith("act.") and loaded is not None:
            have = getattr(loaded[0], FIELDS_BY_KEY[r.key].path.split(".")[1])
            return _normalize(have) != _normalize(r.declared)
        return r.label != "same"

    flow_rows = [_workflow_row(k, m, loaded, listed) for k, m in flows.items()]
    flow_rows.sort(key=lambda r: r["line"] or 0)
    pending = [r.key for r in rows or [] if still_pending(r)] + [
        r["key"] for r in flow_rows if r["pending"]
    ]
    state = managed.get("state")
    rejected = managed.get("rejected") or None
    if not managed:
        on_disk = None
    elif state == "degraded":
        on_disk = "unknown"
    elif state == "rejected" and rejected:
        on_disk = rejected.get("revision")
    else:
        on_disk = managed.get("revision")

    def row(r: IntentDiff) -> Dict[str, Any]:
        meta = keys.get(r.key) or {}
        env = _env_ignored(r.key, r.declared) if r.source == "declared" else None
        return {
            "key": r.key,
            "declared": r.declared,
            "effective": r.effective,
            "source": r.source,
            "label": r.label,
            "managed": bool(meta),
            "file": meta.get("file"),
            "line": meta.get("line"),
            "revision": meta.get("revision"),
            # Tighter than declared: a local narrowing or an env lock.
            "drift": bool(meta) and r.label == "loosen" and r.key not in pending,
            "pending": r.key in pending,
            "env_ignored": env is not None,
            "env_value": env,
        }

    return {
        "path": str(report_path()),
        "readable": rows is not None,
        "rows": [row(r) for r in rows or []] + (flow_rows if rows is not None else []),
        "revision": managed.get("revision"),
        "state": state,
        "reason": managed.get("reason"),
        "pending": pending,
        "skipped": managed.get("skipped") or [] if managed else [],
        "directory": managed.get("directory"),
        "revision_on_disk": on_disk,
        "in_sync": (state == "ok" and not pending) if managed else None,
        "hash": managed.get("hash"),
        "applied_at": managed.get("applied_at"),
        "rejected": rejected,
        "verification": NOT_VERIFIED if managed else None,
    }
