"""The restricted-case read rule, as the case loaders see it (#4354).

The rule is defined once in ``core.auth.permissions``, which installs it here
when it is imported: ``core.storage`` may not import ``core.auth``
(.importlinter tiers). With no rule installed a case read fails rather than
answer as if every case were open.
"""

from __future__ import annotations

from typing import Any

_rule: Any = None


def install(rule: Any) -> None:
    """``rule`` has ``may_read_case(case)`` and ``case_read_clause()``."""
    global _rule
    _rule = rule


def _installed() -> Any:
    if _rule is None:
        raise RuntimeError("the restricted-case read rule is not installed")
    return _rule


def may_read(case: Any) -> bool:
    """The single-record test on a ``Case`` row."""
    return _installed().may_read_case(case)


def read_clause() -> Any:
    """The same test as a SQL clause on ``cases``."""
    return _installed().case_read_clause()
