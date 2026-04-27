"""Prompt-injection defenses for the Vigil LLM call path (issue #87).

Three responsibilities, all small:

* ``scan_for_injection`` — pattern-based detection, used at the API boundary
  (validating user-supplied ``system_prompt`` values), inside the LLM router
  pre-dispatch hook, and at daemon ingestion. Detect-only in v1; the caller
  decides whether to log, flag, or block.

* ``wrap_tool_result`` — wraps untrusted tool-result text in a delimiter
  block the model can recognize. Internal ``<`` is escaped so an attacker
  can't smuggle a fake closing tag in the content.

* ``sanitize_system_prompt`` — convenience wrapper that returns the original
  value plus detections (we don't mutate the prompt — silently editing user
  input is worse than logging it).

Constants like ``MAX_SYSTEM_PROMPT_BYTES`` are exported for use in Pydantic
validators so the API boundary and the library agree on shape.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — also imported by backend.schemas.system_prompt
# ---------------------------------------------------------------------------

MAX_SYSTEM_PROMPT_BYTES: int = 8192

# Control characters allowed in user-supplied prompts. Everything else in
# 0x00–0x1F plus 0x7F (DEL) is rejected by the API validator.
_ALLOWED_CONTROL_CHARS: frozenset[str] = frozenset({"\n", "\t"})


# ---------------------------------------------------------------------------
# Injection patterns
# ---------------------------------------------------------------------------

# Pattern names double as the structured-log tag, so keep them stable.
_INJECTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|all)\b[^.\n]{0,40}\b"
            r"(instruction|prompt|rule|message|directive)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_manipulation",
        re.compile(
            r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b|"
            r"\bbehave\s+as\b|\bnew\s+persona\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_leak",
        re.compile(
            r"\b(reveal|print|repeat|show|output)\b[^.\n]{0,30}\b"
            r"(system|initial|original)\b[^.\n]{0,20}\b(prompt|instruction)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delimiter_injection",
        re.compile(
            r"</?\s*(system|user|assistant|s|im_start|im_end)\s*>"
            r"|<\s*\|?\s*(system|user|assistant|im_start|im_end)\s*\|?\s*>",
            re.IGNORECASE,
        ),
    ),
    (
        "developer_mode",
        re.compile(
            r"\b(dev(eloper)?\s+mode|jailbreak|do\s+anything\s+now|DAN\s+mode|"
            r"unrestricted\s+mode)\b",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True)
class InjectionMatch:
    """One pattern hit. ``span`` is the matched substring (capped) for logs."""

    pattern: str
    span: str
    start: int
    end: int


@dataclass
class ScanResult:
    """Aggregate outcome of a scan; truthy iff any pattern matched."""

    matches: List[InjectionMatch] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.matches)

    @property
    def patterns(self) -> List[str]:
        return [m.pattern for m in self.matches]


def scan_for_injection(text: Optional[str]) -> ScanResult:
    """Run the full pattern set against *text* and return all hits.

    Returns an empty ``ScanResult`` on None / empty input. Per-match span
    is truncated to 80 chars so audit logs don't carry the full payload.
    """
    if not text:
        return ScanResult()
    matches: List[InjectionMatch] = []
    for name, pattern in _INJECTION_PATTERNS:
        for m in pattern.finditer(text):
            span = m.group(0)
            if len(span) > 80:
                span = span[:77] + "..."
            matches.append(
                InjectionMatch(pattern=name, span=span, start=m.start(), end=m.end())
            )
    return ScanResult(matches=matches)


def has_disallowed_control_chars(text: str) -> bool:
    """Return True if *text* contains a control char not in the allow-list."""
    for ch in text:
        codepoint = ord(ch)
        if codepoint < 0x20 and ch not in _ALLOWED_CONTROL_CHARS:
            return True
        if codepoint == 0x7F:  # DEL
            return True
    return False


# ---------------------------------------------------------------------------
# Tool-result wrapping
# ---------------------------------------------------------------------------

_TOOL_RESULT_OPEN = '<vigil:tool_result source="{source}" tool="{tool}">'
_TOOL_RESULT_CLOSE = "</vigil:tool_result>"


def _escape_for_wrapper(text: str) -> str:
    """Neutralise attacker-supplied ``<`` so they can't forge a close tag.

    We only escape ``<``; ``&`` and ``>`` aren't load-bearing here. The model
    treats the wrapped block as a delimited region of untrusted text, not as
    HTML/XML — full entity-encoding would just bloat the context.
    """
    return text.replace("<", "&lt;")


def _slug(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(value))
    return cleaned[:64] or fallback


def wrap_tool_result(
    content: str, *, source: Optional[str], tool: Optional[str]
) -> str:
    """Wrap *content* in a `<vigil:tool_result>` block.

    Already-wrapped content is returned unchanged so wrapping is idempotent
    (the router applies it defensively to historical messages, and the
    construction sites in claude_service.py also wrap fresh results — both
    paths must be safe).
    """
    if not isinstance(content, str):
        content = str(content)
    if content.startswith("<vigil:tool_result"):
        return content
    src = _slug(source, "unknown")
    tl = _slug(tool, "unknown")
    open_tag = _TOOL_RESULT_OPEN.format(source=src, tool=tl)
    return f"{open_tag}\n{_escape_for_wrapper(content)}\n{_TOOL_RESULT_CLOSE}"


# ---------------------------------------------------------------------------
# Convenience for API validator audit logging
# ---------------------------------------------------------------------------


def sanitize_system_prompt(value: Optional[str]) -> Tuple[Optional[str], ScanResult]:
    """Return the prompt unchanged plus the scan result.

    Mutating user-supplied prompts silently is worse than logging them —
    callers (the API validator, the route handler) decide what to do with
    the detections. v1 logs and allows; future versions may block via env.
    """
    return value, scan_for_injection(value)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PromptInjectionBlocked(Exception):
    """Raised by the LLM router when ``PROMPT_INJECTION_BLOCK=true``."""

    def __init__(self, patterns: List[str]):
        super().__init__(f"prompt injection blocked: {', '.join(patterns)}")
        self.patterns = patterns
