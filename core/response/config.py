"""The confidence band for autonomous response, in one place (#916).

Every comparison against a confidence — the approval gate, the correlator's
recommendation ladder, the daemon's response queue and severity floors, and the
band text the agents are prompted with — reads a field here, so raising a
threshold in env moves every branch rather than one. Defaults are the literals
the code carried before, so a default install behaves as it did.

Lives in ``core/`` because ``core`` must not import ``services``;
``services.daemon.config`` re-exports it as part of ``DaemonConfig``.
"""

from dataclasses import dataclass
from typing import Any, Optional

from core.config import Settings, get_settings


def decision_rule(field: str, value: Any, observed: Optional[float] = None) -> str:
    """Render the rule a decision fired on, keyed on the config field (#917).

    Numeric thresholds render as ``response.confidence_threshold=0.90 met (0.92)``
    — the field, the value it held when the decision was made, whether the
    observed confidence reached it, and that confidence. Branches with no
    comparison (``reversibility=irreversible``, ``approval.force_manual_approval=True``)
    render as ``field=value``. Every decision site calls this; none assembles
    the string itself, so a later slice can parse one shape.
    """
    if observed is None:
        return f"{field}={value}"
    verdict = "met" if observed >= value else "not met"
    return f"{field}={value:.2f} {verdict} ({observed:.2f})"


@dataclass
class ResponseConfig:
    auto_response_enabled: bool = True
    # Reversible actions at or above this auto-approve; below it they wait for
    # an analyst. The one knob DAEMON_CONFIDENCE_THRESHOLD has always named.
    confidence_threshold: float = 0.90
    # The "quick review" line: the correlator recommends isolation with
    # approval, investigate_and_respond acts, and the processor queues a finding
    # for response at or above it.
    review_threshold: float = 0.85
    # Below this the recommendation is to keep monitoring rather than act.
    monitor_threshold: float = 0.70
    # Severity-conditioned floors the daemon responder applies even below
    # confidence_threshold: a critical finding is isolated, and a high one
    # investigated (or rated high by the correlator when indicators back it),
    # at or above these.
    critical_action_floor: float = 0.70
    high_action_floor: float = 0.80
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
            critical_action_floor=s.daemon_critical_action_floor,
            high_action_floor=s.daemon_high_action_floor,
            force_manual_approval=s.daemon_force_approval,
            dry_run=s.daemon_dry_run,
        )


def response_action_decision(
    severity: str,
    confidence: float,
    recommended: str,
    config: ResponseConfig,
) -> Optional[tuple[str, str]]:
    """The finding-side response, or None when nothing would be acted on.

    Returns ``(action, rule)``. Auto-response off is a decision, not a
    precondition: a manifest that disables it reports every finding that
    would have acted as losing its action.
    """
    if not config.auto_response_enabled:
        return None
    if confidence >= config.confidence_threshold and recommended in (
        "isolate",
        "block",
    ):
        return recommended, decision_rule(
            "response.confidence_threshold", config.confidence_threshold, confidence
        )
    if severity == "critical" and confidence >= config.critical_action_floor:
        return "isolate", decision_rule(
            "response.critical_action_floor", config.critical_action_floor, confidence
        )
    if severity == "high" and confidence >= config.high_action_floor:
        return "investigate", decision_rule(
            "response.high_action_floor", config.high_action_floor, confidence
        )
    return None


def approval_requirement(
    force_manual_approval: bool,
    reversibility: Any,
    confidence: float,
    config: ResponseConfig,
) -> tuple[bool, str]:
    """Whether an approval row waits for a human, and the rule that decided it.

    ``reversibility`` is the enum the live path passes. Compared by ``.value``
    so this module does not import the service that calls it. An unknown
    value raises, unless force-manual already decided.
    """
    if force_manual_approval:
        return True, decision_rule("approval.force_manual_approval", True)
    value = getattr(reversibility, "value", None)
    if value == "irreversible":
        return True, decision_rule("reversibility", value)
    if value == "reversible":
        return confidence < config.confidence_threshold, decision_rule(
            "response.confidence_threshold", config.confidence_threshold, confidence
        )
    raise ValueError(f"Unknown reversibility: {reversibility}")
