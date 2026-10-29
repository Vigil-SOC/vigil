"""The confidence band for autonomous response, in one place (#916).

Every comparison against a confidence — the approval gate, the daemon's intake
line, and the band text the agents are prompted with — reads a field here, so raising a
threshold in env moves every branch rather than one. Defaults are the literals
the code carried before, so a default install behaves as it did.

Lives in ``core/`` because ``core`` must not import ``services``;
``services.daemon.config`` re-exports it as part of ``DaemonConfig``.
"""

import re
from dataclasses import dataclass
from typing import Any, Optional

from core.config import Settings, get_settings


def decision_rule(field: str, value: Any, observed: Optional[float] = None) -> str:
    """Render the rule a decision fired on, keyed on the config field (#917).

    Numeric thresholds render as ``response.confidence_threshold=0.90 met (0.92)``
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
    # Routing and prompt band. A confidence at or above this used to
    # auto-approve a reversible row; the approval gate no longer does that
    # (#2918).
    confidence_threshold: float = 0.90
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
        s = settings or get_settings()
        return cls(
            auto_response_enabled=s.daemon_auto_response,
            confidence_threshold=s.daemon_confidence_threshold,
            review_threshold=s.daemon_review_threshold,
            monitor_threshold=s.daemon_monitor_threshold,
            force_manual_approval=s.daemon_force_approval,
            dry_run=s.daemon_dry_run,
        )


def approval_requirement(
    forced: bool,
    effect_class: Optional[str],
    confidence: float,
    config: ResponseConfig,
) -> tuple[bool, str]:
    """Whether an approval row waits for a human, and the rule that decided it.

    A confidence never marks a row approved: every class waits. ``config``
    stays in the signature so callers and the intent replay keep one
    function; the threshold it carries is a routing knob, not a release. An
    act flag does not release a row either (#2931).

    ``forced`` is ``environment_locks_act()`` on the live path.
    ``effect_class`` is the tool's stored class (#3304), None when it is
    unclassified. An unknown class raises, unless forced already decided.
    """
    if forced:
        return True, decision_rule("approval.force_manual_approval", True)
    if effect_class is None or effect_class == "irreversible":
        return True, decision_rule("effect_class", effect_class or "unclassified")
    if effect_class in ("read", "read_unverified", "reversible"):
        # A confidence is a probability. Anything else (5.0, NaN) is recorded
        # as out of range, and still waits.
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            return True, decision_rule("response.confidence_range", confidence)
        return True, decision_rule("effect_class", effect_class)
    raise ValueError(f"Unknown effect class: {effect_class}")
