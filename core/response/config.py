"""The confidence band for autonomous response, in one place (#916).

Every comparison against a confidence — the approval gate, the daemon's intake
line, and the band text the agents are prompted with — reads a field here, so raising a
threshold in env moves every branch rather than one. Defaults are the literals
the code carried before, so a default install behaves as it did.

Lives in ``core/`` because ``core`` must not import ``services``;
``core.daemon_config`` re-exports it as part of ``DaemonConfig``.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.config import Settings, get_settings
from core.intent import is_workflow_key, key_label

logger = logging.getLogger(__name__)

# The row the daemon writes when it applies a declared INTENT.md revision
# (VIGIL_INTENT_DIR). ``values`` holds the applied triage/enrich/respond keys,
# which have no other store; act.*, confidence.* and investigate.* live in their own rows.
INTENT_APPLIED_KEY = "intent.applied"
# Every process re-reads the row at most this often, so an applied revision
# reaches the daemon and each backend replica without a restart.
_APPLIED_TTL_SECONDS = 10.0
_applied_cache: tuple[float, Dict[str, Any]] = (float("-inf"), {})


def applied_intent(*, fresh: bool = False) -> Dict[str, Any]:
    """The ``intent.applied`` row through a short cache; ``{}`` when absent.

    A failed read keeps the last value read, so a database blip does not
    flip a declared tightening back to env.
    """
    global _applied_cache
    now = time.monotonic()
    at, value = _applied_cache
    if not fresh and now - at < _APPLIED_TTL_SECONDS:
        return value
    # The read is outside the lock: a slow database stalls only its caller.
    try:
        from core.storage.config_service import get_config_service

        row = get_config_service().read_system_config(INTENT_APPLIED_KEY)
        value = row if isinstance(row, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("intent.applied not read; keeping last: %s", exc)
    _applied_cache = (now, value)
    return value


def managed_widening(
    key: str,
    requested: Any,
    applied: Optional[Dict[str, Any]] = None,
    *,
    ceiling: Any = None,
) -> Optional[str]:
    """Why a local write of ``requested`` to a managed key is refused, or None.

    Refused when it is looser than the declared value. Compared with the
    declared value, not the current one, so setting it back is the reset.
    For ``workflows.<id>``, ``requested`` is the entry to store, or None to
    follow the fleet: that is refused when, against the declared entry on
    ``ceiling`` (the stored :class:`Ceiling`), it would let the workflow act.
    """
    applied = applied_intent(fresh=True) if applied is None else applied
    meta = (applied.get("keys") or {}).get(key) if applied.get("directory") else None
    if not meta:
        return None
    declared = meta.get("declared")
    if is_workflow_key(key) and requested is None:
        if ceiling is None or not _fleet_raises(
            ceiling, key.split(".", 1)[1], declared
        ):
            return None
    elif requested is None or key_label(key, requested, declared) != "loosen":
        return None
    return (
        f"{key} is managed by {meta.get('file')}:{meta.get('line')} at revision "
        f"{meta.get('revision') or 'unknown'} (declared {json.dumps(declared)}); a local "
        f"change may only narrow it. Change it in the declared directory "
        f"{applied['directory']}."
    )


def _fleet_raises(ceiling: Any, workflow_id: str, declared: Dict[str, Any]) -> bool:
    """Whether following the fleet lets the workflow act where ``declared`` would not."""
    from dataclasses import replace
    from types import MappingProxyType

    from core.response.ceiling import raises_workflow

    others = {k: v for k, v in ceiling.workflows.items() if k != workflow_id}
    held = replace(
        ceiling, workflows=MappingProxyType({**others, workflow_id: dict(declared)})
    )
    fleet = replace(ceiling, workflows=MappingProxyType(others))
    return raises_workflow(held, workflow_id, fleet)


def settle_pending(values: Dict[str, Any], user_id: str) -> None:
    """Drop from the row's pending list each key a person just set to its declared value.

    That write is the accept for that key; left listed, a later narrowing of
    it would read as pending rather than drift. Best-effort.
    """
    try:
        from core.storage.config_service import get_config_service

        service = get_config_service(user_id)
        row = service.read_system_config(INTENT_APPLIED_KEY)
        if not isinstance(row, dict) or not row.get("pending"):
            return
        keys = row.get("keys") or {}
        settled = [
            k
            for k in row["pending"]
            if values.get(k) is not None
            and k in keys
            and key_label(k, values[k], keys[k].get("declared")) == "same"
        ]
        if not settled:
            return
        note = (
            f"INTENT.md revision {row.get('revision')}: "
            f"{', '.join(settled)} set to the declared value"
        )
        pending = [k for k in row["pending"] if k not in settled]
        service.set_system_config(
            key=INTENT_APPLIED_KEY,
            value={**row, "pending": pending, "reason": note},
            description="Declared INTENT.md revision applied by the daemon",
            config_type="intent",
            change_reason=note,
        )
        applied_intent(fresh=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent.applied pending not settled: %s", exc)


def applied_values() -> Dict[str, Any]:
    """Applied triage/enrich/respond values keyed by manifest key."""
    values = applied_intent().get("values")
    return values if isinstance(values, dict) else {}


def decision_rule(field: str, value: Any, observed: Optional[float] = None) -> str:
    """Render the rule a decision fired on, keyed on the config field (#917).

    Numeric thresholds render as ``response.review_threshold=0.85 met (0.92)``
    — the field, the value it held when the decision was made, whether the
    observed confidence reached it, and that confidence. Branches with no
    comparison (``effect_class=irreversible``, ``approval.force_manual_approval=True``)
    render as ``field=value``. Every decision site calls this; none assembles
    the string itself, so a later slice can parse one shape.
    """
    if observed is None:
        return f"{field}={value}"
    verdict = "met" if observed >= value else "not met"
    return f"{field}={value:.2f} {verdict} ({observed:.2f})"


# The shapes decision_rule emits: ``field=value`` or ``field=0.90 met (0.92)``.
_RULE = re.compile(r"[a-z_][a-z0-9_.]*=[^\s;=]+(?: (?:not )?met \([0-9.]+\))?")


def split_decision_rule(reason: Optional[str]) -> tuple[str, Optional[str]]:
    """Inverse of ``decision_rule`` on a stored reason: ``(narrative, rule)``.

    The rule is the tail after the last ``"; "`` (or the whole string when
    ``_put_action`` stored a bare rule) if it has a ``decision_rule`` shape;
    otherwise the reason is all narrative and the rule is ``None``.
    """
    if not reason:
        return "", None
    narrative, sep, tail = reason.rpartition("; ")
    if not sep:
        narrative, tail = "", reason
    if _RULE.fullmatch(tail):
        return narrative, tail
    return reason, None


@dataclass
class ResponseConfig:
    auto_response_enabled: bool = True
    # The "quick review" line: the processor offers a finding to intake at or
    # above it.
    review_threshold: float = 0.85
    # Below this the recommendation is to keep monitoring rather than act.
    monitor_threshold: float = 0.70
    # Process copy of DAEMON_FORCE_APPROVAL. With auto-response off, it forces
    # the ceiling's effective act flags off. It is not a grant, and it does
    # not release a row.
    force_manual_approval: bool = False
    dry_run: bool = False  # Log actions without executing

    @classmethod
    def from_settings(cls, settings: Optional[Settings] = None) -> "ResponseConfig":
        """Env, with an applied INTENT.md revision's respond.* keys over it."""
        s = settings or get_settings()
        applied = applied_values()
        return cls(
            auto_response_enabled=applied.get(
                "respond.auto_response", s.daemon_auto_response
            ),
            review_threshold=applied.get(
                "respond.review_threshold", s.daemon_review_threshold
            ),
            monitor_threshold=applied.get(
                "respond.monitor_threshold", s.daemon_monitor_threshold
            ),
            force_manual_approval=s.daemon_force_approval,
            dry_run=s.daemon_dry_run,
        )


def approval_requirement(
    forced: bool,
    effect_class: Optional[str],
    config: ResponseConfig,
) -> tuple[bool, str]:
    """Whether an approval row waits for a human, and the rule that decided it.

    Every class waits; no confidence is an input (#5278). ``config`` stays
    in the signature so callers keep one function. An act flag does not
    release a row either (#2931).

    ``forced`` is ``environment_locks_act()`` on the live path.
    ``effect_class`` is the tool's stored class (#3304), None when it is
    unclassified. An unknown class raises, unless forced already decided.
    """
    if forced:
        return True, decision_rule("approval.force_manual_approval", True)
    if effect_class is None or effect_class == "irreversible":
        return True, decision_rule("effect_class", effect_class or "unclassified")
    if effect_class in ("read", "read_unverified", "reversible"):
        return True, decision_rule("effect_class", effect_class)
    raise ValueError(f"Unknown effect class: {effect_class}")
