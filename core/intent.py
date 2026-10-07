"""Declared daemon intent (``INTENT.md``) diffed against what the daemon runs with.

The manifest is Markdown with YAML frontmatter, the same shape as ``WORKFLOW.md``.
Its frontmatter carries the autonomy knobs ``DaemonConfig`` already has, grouped
by action class; the body is prose for the operator and is not parsed. Observe
mode only: nothing here enforces or writes. The daemon and the CLI log one line
per key whose declared value differs from the effective one, with where the
effective value came from and whether applying the declared value would
``tighten`` or ``loosen`` autonomy.

Effective values come from ``DaemonConfig.from_env()`` in ``services/daemon``;
this module is pure so ``core`` never imports the deployables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.config import REPO_ROOT, get_settings
from core.frontmatter import FrontmatterError, split_frontmatter

logger = logging.getLogger(__name__)

DEFAULT_INTENT_FILE = REPO_ROOT / "INTENT.md"

# Tightening rules, one per field type. The label describes what applying the
# declared value over the effective one would do to the daemon's autonomy.
HIGHER_TIGHTER = "higher_tighter"  # confidence thresholds, force_manual_approval
LOWER_TIGHTER = "lower_tighter"  # budgets, auto_* / enabled booleans
SHORTER_TIGHTER = "shorter_tighter"  # severity lists


@dataclass(frozen=True)
class IntentField:
    key: str  # manifest key, "<action class>.<knob>"
    path: str  # attribute path on DaemonConfig
    setting: str  # Settings field that supplies the env value
    rule: str


# One key per existing knob and no key without a reader. Grouped by the daemon's
# action classes; the attribute paths are the fields ``DaemonConfig`` carries today.
INTENT_FIELDS: Tuple[IntentField, ...] = (
    IntentField(
        "triage.auto_triage",
        "processing.auto_triage_enabled",
        "daemon_auto_triage",
        LOWER_TIGHTER,
    ),
    IntentField(
        "enrich.auto_enrich",
        "processing.auto_enrich_enabled",
        "daemon_auto_enrich",
        LOWER_TIGHTER,
    ),
    IntentField(
        "respond.auto_response",
        "response.auto_response_enabled",
        "daemon_auto_response",
        LOWER_TIGHTER,
    ),
    IntentField(
        "respond.confidence_threshold",
        "response.confidence_threshold",
        "daemon_confidence_threshold",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "respond.review_threshold",
        "response.review_threshold",
        "daemon_review_threshold",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "respond.monitor_threshold",
        "response.monitor_threshold",
        "daemon_monitor_threshold",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "respond.critical_action_floor",
        "response.critical_action_floor",
        "daemon_critical_action_floor",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "respond.high_action_floor",
        "response.high_action_floor",
        "daemon_high_action_floor",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "respond.force_manual_approval",
        "response.force_manual_approval",
        "daemon_force_approval",
        HIGHER_TIGHTER,
    ),
    IntentField(
        "escalate.severities",
        "escalation.escalate_severities",
        "daemon_escalate_severities",
        SHORTER_TIGHTER,
    ),
    IntentField(
        "investigate.enabled",
        "orchestrator.enabled",
        "orchestrator_enabled",
        LOWER_TIGHTER,
    ),
    IntentField(
        "investigate.max_cost_per_investigation",
        "orchestrator.max_cost_per_investigation",
        "orchestrator_max_cost",
        LOWER_TIGHTER,
    ),
    IntentField(
        "investigate.max_total_hourly_cost",
        "orchestrator.max_total_hourly_cost",
        "orchestrator_max_hourly_cost",
        LOWER_TIGHTER,
    ),
    IntentField(
        "investigate.max_runtime_per_investigation",
        "orchestrator.max_runtime_per_investigation",
        "orchestrator_max_runtime",
        LOWER_TIGHTER,
    ),
)

FIELDS_BY_KEY: Dict[str, IntentField] = {f.key: f for f in INTENT_FIELDS}


@dataclass(frozen=True)
class IntentDiff:
    key: str
    declared: Any
    effective: Any
    source: str  # env | db | default
    label: str  # tighten | loosen | same


def intent_file() -> Path:
    override = get_settings().vigil_intent_path
    return Path(override) if override else DEFAULT_INTENT_FILE


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for name, value in mapping.items():
        key = f"{prefix}{name}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{key}."))
        else:
            flat[key] = value
    return flat


def read_intent(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Declared values keyed by manifest key, or ``None`` after one warning.

    Keys the daemon has no reader for are dropped with a warning each; they
    would otherwise claim to declare something nothing enforces.
    """
    path = path or intent_file()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("INTENT.md not read (%s): %s", path, exc)
        return None
    try:
        front, _ = split_frontmatter(content)
    except FrontmatterError as exc:
        logger.warning("INTENT.md frontmatter unreadable (%s): %s", path, exc)
        return None
    if front is None:
        logger.warning("INTENT.md has no YAML frontmatter (%s)", path)
        return None
    declared = _flatten(front)
    for key in sorted(set(declared) - set(FIELDS_BY_KEY)):
        logger.warning("INTENT.md key %r has no reader in the daemon; ignored", key)
        declared.pop(key)
    if not declared:
        logger.warning("INTENT.md declares no known key (%s)", path)
        return None
    return declared


def effective_values(config: Any) -> Dict[str, Any]:
    """Walk each field's attribute path on ``config`` (a ``DaemonConfig``)."""
    values: Dict[str, Any] = {}
    for f in INTENT_FIELDS:
        target: Any = config
        for part in f.path.split("."):
            target = getattr(target, part)
        values[f.key] = target
    return values


def _normalize(value: Any) -> Any:
    """Comparable form: severity lists as lower-cased sets, numbers as floats.

    Returns ``None`` for a value of the wrong shape (a quoted number, ``1`` for
    a boolean, a bare string for a list): YAML is loose and a typo must not
    become a crash or a silent "no difference".
    """
    if isinstance(value, (list, tuple)):
        return frozenset(str(v).lower() for v in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return None


def label(rule: str, declared: Any, effective: Any) -> str:
    declared, effective = _normalize(declared), _normalize(effective)
    if declared == effective:
        return "same"
    if rule == SHORTER_TIGHTER:
        # Length, as the issue states the rule; two lists of equal length with
        # different members are neither direction.
        if len(declared) == len(effective):
            return "same"
        return "tighten" if len(declared) < len(effective) else "loosen"
    if rule == HIGHER_TIGHTER:
        return "tighten" if declared > effective else "loosen"
    if rule == LOWER_TIGHTER:
        return "tighten" if declared < effective else "loosen"
    raise ValueError(f"unknown tightening rule {rule!r}")


def diff_intent(
    declared: Mapping[str, Any],
    effective: Mapping[str, Any],
    sources: Mapping[str, str],
    *,
    include_same: bool = False,
) -> List[IntentDiff]:
    """One row per declared key whose value differs from the effective one.

    A declared value of the wrong type for its knob is one warning and no row.
    ``include_same`` also emits the equal keys, labeled ``same``. The daemon
    log and CLI leave it off.
    """
    rows: List[IntentDiff] = []
    for f in INTENT_FIELDS:
        if f.key not in declared:
            continue
        want, have = declared[f.key], effective[f.key]
        norm_want, norm_have = _normalize(want), _normalize(have)
        if norm_want is None or type(norm_want) is not type(norm_have):
            logger.warning(
                "INTENT.md key %r: declared %r is not a %s; ignored",
                f.key,
                want,
                type(have).__name__,
            )
            continue
        if norm_want == norm_have and not include_same:
            continue
        rows.append(
            IntentDiff(
                key=f.key,
                declared=want,
                effective=have,
                source=sources.get(f.path, "default"),
                label=label(f.rule, want, have),
            )
        )
    return rows


def format_rows(rows: Sequence[IntentDiff]) -> List[str]:
    return [
        f"intent {r.key}: declared={r.declared!r} effective={r.effective!r} "
        f"source={r.source} label={r.label}"
        for r in rows
    ]
