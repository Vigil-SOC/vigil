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
from typing import Optional

from core.config import Settings, get_settings


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
