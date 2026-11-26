"""Declared daemon intent (``INTENT.md``) diffed against what the daemon runs with.

The manifest is Markdown with YAML frontmatter, the same shape as ``WORKFLOW.md``.
Its frontmatter carries the autonomy knobs ``DaemonConfig`` already has, grouped
by action class; the body is prose for the operator and is not parsed. Observe
mode only: nothing here enforces or writes. The daemon and the CLI log one line
per key whose declared value differs from the effective one, with where the
effective value came from and whether applying the declared value would
``tighten`` or ``loosen`` autonomy.

Effective values come from ``DaemonConfig.from_env()`` in ``services/daemon``;
this module is pure so ``core`` never imports the deployables. Enforced mode
(``VIGIL_INTENT_DIR``) parses with :func:`parse_intent_strict` and is applied
by ``services/daemon/intent_apply.py``; ``VIGIL_INTENT_PATH`` stays observe-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.config import REPO_ROOT, get_settings
from core.frontmatter import (
    FrontmatterError,
    frontmatter_key_lines,
    split_frontmatter,
)

logger = logging.getLogger(__name__)

DEFAULT_INTENT_FILE = REPO_ROOT / "INTENT.md"

# Tightening rules, one per field type. The label describes what applying the
# declared value over the effective one would do to the daemon's autonomy.
HIGHER_TIGHTER = "higher_tighter"  # confidence thresholds, four_eyes, hold_crown_jewels
LOWER_TIGHTER = "lower_tighter"  # budgets, auto_* / enabled booleans


@dataclass(frozen=True)
class IntentField:
    key: str  # manifest key, "<action class>.<knob>"
    path: str  # attribute path on DaemonConfig
    setting: Optional[str]  # Settings field that supplies the env value; None: no env
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
    # Act flags: off is tighter. The named settings are the env floor, which
    # can only force them off. four_eyes on is tighter.
    IntentField(
        "act.reversible",
        "ceiling.act_reversible",
        "daemon_auto_response",
        LOWER_TIGHTER,
    ),
    IntentField(
        "act.irreversible",
        "ceiling.act_irreversible",
        "daemon_force_approval",
        LOWER_TIGHTER,
    ),
    IntentField(
        "act.four_eyes",
        "ceiling.four_eyes",
        "daemon_force_approval",
        HIGHER_TIGHTER,
    ),
    # On is tighter; no env setting and exempt from the environment locks.
    IntentField(
        "act.hold_crown_jewels",
        "ceiling.hold_crown_jewels",
        None,
        HIGHER_TIGHTER,
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

# Keys whose value is a number; every other key is a boolean.
NUMERIC_KEYS = frozenset(
    {
        "respond.confidence_threshold",
        "respond.review_threshold",
        "respond.monitor_threshold",
        "investigate.max_cost_per_investigation",
        "investigate.max_total_hourly_cost",
        "investigate.max_runtime_per_investigation",
    }
)

# Enforced mode rejects a number outside these; the budgets match the bounds
# the Settings card enforces (OrchestratorSettingsConfig), so an applied value
# never makes the console's next save invalid.
NUMERIC_BOUNDS: Dict[str, Tuple[float, float]] = {
    "respond.confidence_threshold": (0.0, 1.0),
    "respond.review_threshold": (0.0, 1.0),
    "respond.monitor_threshold": (0.0, 1.0),
    "investigate.max_cost_per_investigation": (0.5, 100.0),
    "investigate.max_total_hourly_cost": (1.0, 500.0),
    "investigate.max_runtime_per_investigation": (60.0, 86400.0),
}
INTEGER_KEYS = frozenset({"investigate.max_runtime_per_investigation"})

# Per-workflow autonomy (#5272): ``workflows: {<id>: {tier, floor}}``, one
# managed key ``workflows.<id>`` per entry whose value is the whole entry.
WORKFLOWS = "workflows"
WORKFLOW_TIERS = ("act", "assist")
WORKFLOW_ENTRY_KEYS = ("tier", "floor")

# Enforced mode (VIGIL_INTENT_DIR): the directory holds INTENT.md and this
# marker, whose first line names the revision.
INTENT_FILE_NAME = "INTENT.md"
REVISION_FILE_NAME = "REVISION"


def declared_dir(settings: Any = None) -> Optional[Path]:
    """``VIGIL_INTENT_DIR``, or None when unset."""
    raw = (settings or get_settings()).vigil_intent_dir.strip()
    return Path(raw) if raw else None


def read_revision(directory: Path) -> Tuple[bytes, str]:
    """``<directory>/REVISION``'s bytes and the revision its first line names.

    A missing or empty marker names ``unknown``. The daemon applier hashes the
    bytes; the skills API reports the revision (#5070).
    """
    try:
        marker = (directory / REVISION_FILE_NAME).read_bytes()
    except FileNotFoundError:
        marker = b""
    lines = marker.decode("utf-8", "replace").splitlines()
    return marker, (lines[0].strip()[:200] if lines else "") or "unknown"


class IntentRejected(ValueError):
    """A declared revision that is not applied at all, and where it went wrong."""

    def __init__(self, file: str, line: Optional[int], reason: str):
        super().__init__(f"{file}:{line or 1}: {reason}")
        self.file = file
        self.line = line or 1
        self.reason = reason


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
    front = dict(front)
    front.pop(WORKFLOWS, None)  # enforced mode only (VIGIL_INTENT_DIR)
    declared = _flatten(front)
    for key in sorted(set(declared) - set(FIELDS_BY_KEY)):
        logger.warning("INTENT.md key %r has no reader in the daemon; ignored", key)
        declared.pop(key)
    if not declared:
        logger.warning("INTENT.md declares no known key (%s)", path)
        return None
    return declared


def parse_intent_strict(
    content: str, name: str = INTENT_FILE_NAME
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Declared values and the line each key is on, or :class:`IntentRejected`.

    Enforced mode: unlike :func:`read_intent`, an unknown key or a value of the
    wrong type rejects the whole file instead of being dropped.
    """
    try:
        front, _ = split_frontmatter(content)
    except FrontmatterError as exc:
        # The YAML problem alone; its own line numbers count from the block.
        problem = getattr(exc.__cause__, "problem", None)
        reason = f"invalid YAML: {problem}" if problem else str(exc)
        raise IntentRejected(name, exc.line, reason) from exc
    if front is None:
        raise IntentRejected(name, 1, "no YAML frontmatter")
    lines = frontmatter_key_lines(content)
    front = dict(front)
    workflows = _parse_workflows(front.pop(WORKFLOWS, None), lines, name)
    declared = _flatten(front)
    for key in sorted(declared, key=lambda k: lines.get(k, 0)):
        value = declared[key]
        if key not in FIELDS_BY_KEY:
            raise IntentRejected(name, lines.get(key), f"unknown key {key!r}")
        norm = _normalize(value)
        want = float if key in NUMERIC_KEYS else bool
        if norm is None or type(norm) is not want:
            raise IntentRejected(
                name,
                lines.get(key),
                f"{key}: {value!r} is not a {'number' if want is float else 'boolean'}",
            )
        if key in INTEGER_KEYS and not float(norm).is_integer():
            raise IntentRejected(name, lines.get(key), f"{key}: {value!r} is not whole")
        low, high = NUMERIC_BOUNDS.get(key, (norm, norm))
        if not low <= norm <= high:
            raise IntentRejected(
                name, lines.get(key), f"{key}: {value!r} is outside {low:g}..{high:g}"
            )
    declared.update(workflows)
    return declared, {k: lines.get(k, 1) for k in declared}


def _parse_workflows(
    section: Any, lines: Dict[str, int], name: str
) -> Dict[str, Dict[str, Any]]:
    """``{"workflows.<id>": {"tier", "floor"?}}``, or :class:`IntentRejected`.

    A bad inner key or value is reported at its own line, a missing one at the
    entry's. ``floor`` is a whole number 1..100, required with ``act`` and
    refused with ``assist``.
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise IntentRejected(name, lines.get(WORKFLOWS), "workflows: not a mapping")
    out: Dict[str, Dict[str, Any]] = {}
    for wid, entry in section.items():
        key = f"{WORKFLOWS}.{wid}"
        at = lines.get(key)
        if not isinstance(entry, dict):
            raise IntentRejected(name, at, f"{key}: not a mapping of tier and floor")
        for inner in entry:
            if inner not in WORKFLOW_ENTRY_KEYS:
                raise IntentRejected(
                    name, lines.get(f"{key}.{inner}", at), f"unknown key {key}.{inner}"
                )
        tier, floor = entry.get("tier"), entry.get("floor")
        if "tier" not in entry:
            raise IntentRejected(name, at, f"{key}: tier is required")
        if tier not in WORKFLOW_TIERS:
            raise IntentRejected(
                name,
                lines.get(f"{key}.tier"),
                f"{key}.tier: {tier!r} is not act or assist",
            )
        if tier == "assist":
            if "floor" in entry:
                raise IntentRejected(
                    name,
                    lines.get(f"{key}.floor"),
                    f"{key}.floor: assist takes no floor",
                )
            out[key] = {"tier": tier}
            continue
        if "floor" not in entry:
            raise IntentRejected(name, at, f"{key}: act needs a floor")
        # bool is an int in Python; a floor is never true or false.
        if (
            isinstance(floor, bool)
            or not isinstance(floor, int)
            or not 1 <= floor <= 100
        ):
            raise IntentRejected(
                name,
                lines.get(f"{key}.floor"),
                f"{key}.floor: {floor!r} is not a whole number from 1 to 100",
            )
        out[key] = {"tier": tier, "floor": floor}
    return out


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
    """Comparable form: numbers as floats.

    Returns ``None`` for a value of the wrong shape (a quoted number, ``1`` for
    a boolean): YAML is loose and a typo must not become a crash or a silent
    "no difference".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return None


def label(rule: str, declared: Any, effective: Any) -> str:
    declared, effective = _normalize(declared), _normalize(effective)
    if declared == effective:
        return "same"
    if rule == HIGHER_TIGHTER:
        return "tighten" if declared > effective else "loosen"
    if rule == LOWER_TIGHTER:
        return "tighten" if declared < effective else "loosen"
    raise ValueError(f"unknown tightening rule {rule!r}")


def is_workflow_key(key: str) -> bool:
    return key.startswith(f"{WORKFLOWS}.")


def workflow_label(
    declared: Mapping[str, Any], stored: Optional[Mapping[str, Any]]
) -> str:
    """The one comparison of a workflow entry over the stored one (None: no entry).

    Act over anything not Act loosens; Act at a lower floor loosens and at a
    higher one tightens; Assist over anything not Assist tightens. A stored
    Assist entry's floor (kept by the floor drop) is not compared.
    """
    have = stored.get("tier") if stored else None
    if declared.get("tier") != "act":
        return "same" if have == "assist" else "tighten"
    if have != "act":
        return "loosen"
    want_floor = declared.get("floor") or 0
    have_floor = stored.get("floor") if stored else None
    have_floor = have_floor if isinstance(have_floor, int) else 0
    if want_floor == have_floor:
        return "same"
    return "tighten" if want_floor > have_floor else "loosen"


def key_label(key: str, declared: Any, effective: Any) -> str:
    """:func:`label` for a scalar key, :func:`workflow_label` for ``workflows.<id>``."""
    if is_workflow_key(key):
        return workflow_label(declared, effective)
    return label(FIELDS_BY_KEY[key].rule, declared, effective)


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
