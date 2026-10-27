"""Read a finding's anomaly score without turning a real zero into "missing"."""

from typing import Any, Mapping, Optional

# The console says "Not provided". This wording tells the model why it is absent.
NOT_SCORED_BY_SOURCE = "not scored by the source"


def stored_anomaly_score(finding: Mapping[str, Any]) -> Optional[float]:
    """None when the key is absent or the value is None. A real 0.0 stays 0.0."""
    score = finding.get("anomaly_score")
    if score is None:
        return None
    return float(score)


def format_anomaly_score(score: Any) -> str:
    """Prompt text: ``0.00`` for a real zero, otherwise that the source sent none."""
    if score is None:
        return NOT_SCORED_BY_SOURCE
    return f"{float(score):.2f}"
