"""The one ranking for an ATT&CK confidence.

Storage orders drill-down rows, and the readers order the same way. The
functions live here so the storage tier does not import the threat-intel
domain to get them. ``core.threat_intel.mitre_lookup`` re-exports them.
"""

from typing import Any, Callable, Iterable, Optional, TypeVar

T = TypeVar("T")


def restore_bool_confidences(raw: Any, validated: Any) -> Any:
    """Put bools back after float validation so ``true`` is not stored as 1.0.

    Pydantic accepts a bool as a float. The storage write then treats that
    bool as unknown; it has to still be a bool when it gets there.
    """
    if not isinstance(raw, dict) or not isinstance(validated, dict):
        return validated
    restored = dict(validated)
    for key, value in raw.items():
        if isinstance(value, bool):
            restored[str(key)] = value
    return restored


def is_score(value: Any) -> bool:
    """A real confidence: an int or float, not a bool (an int subclass)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def meets_threshold(confidence: Any, min_confidence: float) -> bool:
    """The ATT&CK threshold: a score at or above it; an unknown only at 0."""
    if is_score(confidence):
        return confidence >= min_confidence
    return min_confidence <= 0


def rank_by_confidence(
    items: Iterable[T],
    confidence: Callable[[T], Any] = lambda t: t.get("confidence"),
) -> list[T]:
    """Scored items highest first, then unknowns; ties and unknowns keep source order.

    An unknown is never ranked against a score: below a 0.3 it would read as
    less likely than 0.3, above it as more certain than anything scored.
    """
    items = list(items)
    scored = [i for i in items if is_score(confidence(i))]
    unknown = [i for i in items if not is_score(confidence(i))]
    # sorted() is stable, so equal scores stay in source order.
    return sorted(scored, key=confidence, reverse=True) + unknown


def top_technique(techniques: Iterable[dict]) -> Optional[dict]:
    """The highest-scored technique, or the first source-named one when none is."""
    ranked = rank_by_confidence(techniques)
    return ranked[0] if ranked else None
