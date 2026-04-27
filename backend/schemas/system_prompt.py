"""Shared validator for user-supplied ``system_prompt`` fields (issue #87).

Used by:
* ``backend/api/claude.py`` — ``ChatRequest.system_prompt``,
  ``AgentTaskRequest.system_prompt``
* ``backend/api/custom_agents.py`` — ``CustomAgentCreate.system_prompt_override``,
  ``CustomAgentUpdate.system_prompt_override``

Behaviour is **validate + audit, allow** in v1: shape checks reject
oversized or control-char-laced input with 422; injection-pattern hits
are logged but the value still passes through to the LLM. Promotion to
block-mode is a follow-up config decision, not a code change.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from services.prompt_security import (
    MAX_SYSTEM_PROMPT_BYTES,
    has_disallowed_control_chars,
    scan_for_injection,
)

logger = logging.getLogger(__name__)


def _audit(*, source: str, value: str) -> None:
    """Emit a structured audit-log record for a non-empty system_prompt.

    We log a sha256 of the value, not the value itself — the prompt may be
    sensitive customer content. Detections (pattern names) and length are
    safe to log because the pattern names are fixed strings and length
    isn't PII.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    scan = scan_for_injection(value)
    logger.info(
        "system_prompt audit",
        extra={
            "event": "system_prompt.audit",
            "source": source,
            "byte_length": len(value.encode("utf-8")),
            "sha256": digest,
            "injection_patterns": scan.patterns,
        },
    )


def validate_system_prompt(value: Optional[str], *, source: str) -> Optional[str]:
    """Pydantic-style validator for user-supplied system prompts.

    * Empty / None → returned as-is, no audit emitted.
    * > ``MAX_SYSTEM_PROMPT_BYTES`` (UTF-8) → ``ValueError``.
    * Disallowed control chars → ``ValueError``.
    * Otherwise → emit audit log entry and return value unchanged.

    ``source`` is a stable tag identifying the call site (``chat``,
    ``agent_task``, ``custom_agent_create``, ``custom_agent_update``).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("system_prompt must be a string")
    if value == "":
        return value

    byte_length = len(value.encode("utf-8"))
    if byte_length > MAX_SYSTEM_PROMPT_BYTES:
        raise ValueError(
            f"system_prompt exceeds {MAX_SYSTEM_PROMPT_BYTES}-byte limit "
            f"(got {byte_length} bytes)"
        )

    if has_disallowed_control_chars(value):
        raise ValueError("system_prompt contains disallowed control characters")

    _audit(source=source, value=value)
    return value
