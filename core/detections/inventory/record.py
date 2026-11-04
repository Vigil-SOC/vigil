"""The one shape every reader returns, and the fingerprint of a rule's logic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# SPL's comment syntax: ```anything```.
_SPL_COMMENT = re.compile(r"```.*?```", re.DOTALL)
# A KQL line comment. Only at line start: '//' also opens URLs.
_KQL_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


@dataclass
class RuleRecord:
    """A rule as one source file or SIEM holds it.

    ``logic`` holds the fields besides the query that change what the rule fires
    on or how loud it is; it is fingerprinted with the query. ``content`` is the
    parsed rule, stored on the version and searched, never compared.

    ``status`` is 'disabled' for a SIEM rule switched off. ``runs`` is the SIEM's
    word on whether the rule runs, ``{result, reason}``, or None for no new word.
    """

    native_key: str
    name: str
    language: str
    path: Optional[str]
    query: str = ""
    techniques: List[str] = field(default_factory=list)
    tactics: List[str] = field(default_factory=list)
    logic: Dict[str, Any] = field(default_factory=dict)
    content: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    runs: Optional[Dict[str, Optional[str]]] = None


def normalize_query(query: str, language: str) -> str:
    """The query with comments dropped and every whitespace run made one space."""
    text = query or ""
    if language == "splunk":
        text = _SPL_COMMENT.sub(" ", text)
    elif language == "kql":
        text = _KQL_LINE_COMMENT.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _canonical(value: Any) -> Any:
    """Strings trimmed and whitespace-collapsed; empty values dropped."""
    if isinstance(value, str):
        return _WHITESPACE.sub(" ", value).strip()
    if isinstance(value, dict):
        out = {k: _canonical(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def fingerprint(record: RuleRecord) -> str:
    """sha256 of the rule's query and logic.

    Name, description, author, comments, whitespace and enabled do not count:
    an edit to any of those is not a new version.
    """
    payload = {
        "language": record.language,
        "query": normalize_query(record.query, record.language),
        "logic": _canonical(record.logic),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
