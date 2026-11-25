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
from core.response.ceiling import environment_locks_act
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
_MANAGED = "Managed by INTENT.md: Vigil can only tighten it"
_RESTART = "Saved at once; the daemon reads it when it starts"
_ACT_LOCKED = "The environment forces it off"
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


def settings() -> list[dict[str, Any]]:
    """Every watched setting with its effective value, source and change state."""
    config = effective_daemon_config(DaemonConfig.from_env())
    env_set = get_settings().model_fields_set
    locked = environment_locks_act()
    rows = [_intent_row(f, config, locked) for f in INTENT_FIELDS]
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
            "Fixed in code; there is no setting for it",
        )
    )
    rows.extend(_model_rows())
    return rows


def _intent_row(field: Any, config: DaemonConfig, locked: bool) -> dict[str, Any]:
    target: Any = config
    for part in field.path.split("."):
        target = getattr(target, part)
    source = config.sources.get(field.path, "default")
    if field.key not in _WRITABLE:
        return _row(field.key, target, source, False, _ENV_ONLY)
    if locked and field.key in ("act.reversible", "act.irreversible"):
        return _row(field.key, target, source, False, _ACT_LOCKED)
    if source == "declared":
        return _row(field.key, target, source, True, _MANAGED)
    return _row(field.key, target, source, True, None)


def _model_rows() -> list[dict[str, Any]]:
    """Model and effort per component, from its own assignment, else chat_default's."""
    try:
        from core.llm.providers.registry import get_registry

        registry = get_registry()
        assignments = registry.get_all_assignments()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model assignments not read: %s", exc)
        return []
    rows = []
    for component in _COMPONENTS:
        resolved = registry.resolve_model_for_component(component)
        saved = component in assignments or "chat_default" in assignments
        source = "db" if saved else "default"
        rows.append(
            _row(
                f"model.{component}",
                resolved[1] if resolved else None,
                source,
                True,
                _MODEL,
            )
        )
        rows.append(
            _row(
                f"effort.{component}",
                registry.effort_for_component(component),
                source,
                True,
                _MODEL,
            )
        )
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
