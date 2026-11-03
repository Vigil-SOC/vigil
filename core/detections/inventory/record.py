"""The one shape every reader returns, and the fingerprint of a rule's logic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

TECHNIQUE = re.compile(r"\bT\d{4}(?:\.\d{3})?\b", re.IGNORECASE)
# SPL's comment syntax: ```anything```.
_SPL_COMMENT = re.compile(r"```.*?```", re.DOTALL)
# A KQL (Microsoft) line comment. Only at line start: '//' also opens URLs.
_KQL_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


@dataclass
class RuleRecord:
    """A rule as one source lists it.

    ``logic`` holds the fields besides the query that change what the rule
    fires on or how loud it is (severity, threshold, log source, index). It is
    fingerprinted with the query. ``raw`` is kept as read and never compared.
    """

    native_key: str
    name: str
    query: str
    language: str
    enabled: bool = True
    techniques: List[str] = field(default_factory=list)
    logic: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    # Where this copy was read: an app, a file, a commit.
    seen_in: str = ""

    def content(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "query": self.query,
            "language": self.language,
            "enabled": self.enabled,
            "techniques": self.techniques,
            "logic": self.logic,
            "raw": self.raw,
        }


def techniques_in(values: Iterable[Any]) -> List[str]:
    """Every ATT&CK technique id named in ``values``, upper-cased and sorted."""
    found = set()
    for value in values:
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        found.update(m.upper() for m in TECHNIQUE.findall(text))
    return sorted(found)


def normalize_query(query: str, language: str) -> str:
    """The query with comments dropped and every whitespace run made one space."""
    text = query or ""
    if language == "spl":
        text = _SPL_COMMENT.sub(" ", text)
    elif language == "kql-ms":
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
    """sha256 of the rule's meaningful fields.

    Name, description, author, comments and whitespace do not count, and
    neither does whether it is enabled: an edit to any of those is not a new
    version.
    """
    payload: Dict[str, Optional[Any]] = {
        "language": record.language,
        "query": normalize_query(record.query, record.language),
        "logic": _canonical(record.logic),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
