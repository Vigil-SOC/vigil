"""The settings the supervisory model watches: effective value, source, and who can change it.

Read-only. Phase 1 covers the settings that change triage, investigation and
response results: thresholds, investigation limits, model and effort, and the
autonomy flags. Plumbing (ports, URLs, tokens, retention) is left out.

A source is where the effective value comes from: ``env``, ``database`` (a value
saved from the console or an applied INTENT.md revision), ``default`` (code), or
``INTENT.md`` (a key the declared file manages). ``change`` says whether Vigil can
write the value itself, and if not, why.
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import get_settings
from core.daemon_config import DaemonConfig
from core.intent import INTENT_FIELDS
from core.intent_report import effective_daemon_config
from core.response.ceiling import environment_locks_act, try_read_stored_ceiling
from core.response.config import applied_intent
from core.workflows.playbook_resolver import DEFAULT_RUNTIME

logger = logging.getLogger(__name__)

# DaemonConfig.sources words, as the page names them.
_SOURCE = {
    "env": "env",
    "db": "database",
    "default": "default",
    "declared": "INTENT.md",
}

_ENV_ONLY = "Set in the environment or INTENT.md; Vigil has no write for it"
_APPLIED = "Set by an earlier INTENT.md revision; Vigil has no write for it"
CEILING_UNREAD = "The autonomy ceiling could not be read"
_CEILING_UNREAD = CEILING_UNREAD
STOPPED = "Autonomy is stopped: Vigil can only tighten it until it resumes"
_STOPPED = STOPPED
_MANAGED = "Managed by INTENT.md: Vigil can only tighten it"
_RESTART = "Saved at once; the daemon reads it when it starts"
ACT_LOCKED = "The environment forces it off"
_ACT_LOCKED = ACT_LOCKED
_MODEL = "Changed in AI model settings, which needs platform.write"

# Intent keys Vigil can write: the orchestrator settings and the autonomy ceiling.
_WRITABLE = {
    "investigate.enabled",
    "investigate.max_cost_per_investigation",
    "investigate.max_total_hourly_cost",
    "investigate.max_runtime_per_investigation",
    "act.reversible",
    "act.irreversible",
    "act.four_eyes",
}

# Orchestrator settings outside INTENT.md: (key, attribute, env setting).
_STARTUP_LIMITS = (
    (
        "investigate.max_iterations_per_agent",
        "max_iterations_per_agent",
        "orchestrator_max_iterations",
    ),
    (
        "investigate.max_concurrent_agents",
        "max_concurrent_agents",
        "orchestrator_max_agents",
    ),
)

# Components whose model and effort change phase 1 results.
_COMPONENTS = ("triage", "investigation")
_EFFORTS = ("low", "medium", "high")


def settings(
    applied: dict[str, Any] | None = None, config: DaemonConfig | None = None
) -> list[dict[str, Any]]:
    """Every watched setting with its effective value, source and change state.

    ``applied`` is the applied INTENT.md row; read here when not given, so a
    caller that checks writes against it can read it once and pass it.
    """
    config = effective_daemon_config(config or DaemonConfig.from_env())
    env_set = get_settings().model_fields_set
    locked = environment_locks_act()
    loaded = try_read_stored_ceiling()
    ceiling = None if loaded is None else loaded[0]
    managed = _managed_keys(applied_intent(fresh=True) if applied is None else applied)
    rows = [_intent_row(f, config, locked, ceiling, managed) for f in INTENT_FIELDS]
    for key, attr, setting in _STARTUP_LIMITS:
        path = f"orchestrator.{attr}"
        source = config.sources.get(path) or (
            "env" if setting in env_set else "default"
        )
        rows.append(
            _row(key, getattr(config.orchestrator, attr), source, True, _RESTART)
        )
    rows.append(
        _row(
            "triage.timeout_seconds",
            config.processing.triage_timeout,
            "env" if "daemon_triage_timeout" in env_set else "default",
            False,
            "Set in the environment only; read when the daemon starts",
        )
    )
    rows.append(
        _row(
            "investigate.max_turns",
            DEFAULT_RUNTIME["max_turns"],
            "default",
            False,
            "The investigation workflows' default, fixed in code; only a workflow"
            " definition can set its own",
        )
    )
    rows.extend(_model_rows())
    return rows


def _intent_row(
    field: Any, config: DaemonConfig, locked: bool, ceiling: Any, managed: set[str]
) -> dict[str, Any]:
    """``ceiling`` is the stored autonomy row, None when it could not be read.

    ``managed`` is every key the declared INTENT.md directory manages, pending
    ones included: a local write that widens any of them is refused.
    """
    target: Any = config
    for part in field.path.split("."):
        target = getattr(target, part)
    source = config.sources.get(field.path, "default")
    if field.key not in _WRITABLE:
        reason = _APPLIED if source == "db" else _ENV_ONLY
        return _row(field.key, target, source, False, reason)
    if field.key.startswith("act.") and ceiling is None:
        return _row(field.key, target, source, False, _CEILING_UNREAD)
    # The environment lock forces the flags off whatever the stop allows.
    if locked and field.key in ("act.reversible", "act.irreversible"):
        return _row(field.key, target, source, False, _ACT_LOCKED)
    if field.key.startswith("act.") and ceiling.stopped:
        return _row(field.key, target, source, True, _STOPPED)
    if source == "declared" or field.key in managed:
        return _row(field.key, target, source, True, _MANAGED)
    return _row(field.key, target, source, True, None)


def _managed_keys(applied: dict[str, Any]) -> set[str]:
    """Keys the declared INTENT.md directory manages, as the write paths read them."""
    if not applied.get("directory"):
        return set()
    return set(applied.get("keys") or {})


def _model_rows() -> list[dict[str, Any]]:
    """Model and effort per component: its own assignment, else chat_default's,
    else the default provider's model. One read; a failed read drops the rows."""
    try:
        from core.llm.providers.registry import get_registry
        from core.llm.router.router import get_default_provider_spec

        assignments = get_registry().get_all_assignments()
        spec = None
        needs_default = not all(c in assignments for c in _COMPONENTS)
        if needs_default and "chat_default" not in assignments:
            spec = get_default_provider_spec()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model assignments not read: %s", exc)
        return []
    rows = []
    for component in _COMPONENTS:
        row = assignments.get(component) or assignments.get("chat_default")
        if row is not None:
            model, model_source = row.model_id, "db"
        else:
            model, model_source = (spec.default_model if spec else None), "default"
        effort = (row.settings or {}).get("effort") if row is not None else None
        # Only a level the agent layer accepts reaches the model.
        if effort not in _EFFORTS:
            effort = None
        effort_source = "db" if effort else "default"
        rows.append(_row(f"model.{component}", model, model_source, True, _MODEL))
        rows.append(_row(f"effort.{component}", effort, effort_source, True, _MODEL))
    return rows


def _row(
    key: str, value: Any, source: str, possible: bool, reason: str | None
) -> dict[str, Any]:
    change: dict[str, Any] = {"possible": possible}
    if reason:
        change["reason"] = reason
    return {
        "key": key,
        "value": _text(value),
        "source": _SOURCE.get(source, "default"),
        "change": change,
    }


def _text(value: Any) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)
