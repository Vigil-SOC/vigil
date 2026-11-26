"""The one YAML-frontmatter reader for ``WORKFLOW.md``, ``SKILL.md`` and ``INTENT.md``.

Callers decide what a miss means: a workflow without frontmatter is a workflow
with no metadata, a skill without one is a 400, an intent file without one is a
warning. This module only splits and parses.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import yaml

# Trailing ``\s*`` rather than a required newline: the skill importer accepted a
# body starting on the closing ``---`` line, and that leniency is kept.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*", re.DOTALL)


class FrontmatterError(ValueError):
    """The ``---`` block is present but is not a YAML mapping.

    ``body_offset`` still says where the body starts, so a caller that wants to
    keep the document despite the bad header can.
    """

    def __init__(self, message: str, body_offset: int, line: Optional[int] = None):
        super().__init__(message)
        self.body_offset = body_offset
        self.line = line  # 1-based line in the document, when YAML named one


def split_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], int]:
    """Return ``(mapping, body_offset)``.

    ``mapping`` is ``None`` when the document has no leading ``---`` block, in
    which case ``body_offset`` is 0. Raises :class:`FrontmatterError` when the
    block exists but is invalid YAML or parses to something other than a mapping.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None, 0
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        # The construct being parsed names the culprit line; where the
        # parser gave up can be several lines on.
        mark = getattr(exc, "context_mark", None) or getattr(exc, "problem_mark", None)
        line = _first_line(content, match) + mark.line if mark else None
        raise FrontmatterError(f"invalid YAML: {exc}", match.end(), line) from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise FrontmatterError(
            "frontmatter must be a YAML mapping",
            match.end(),
            _first_line(content, match),
        )
    return parsed, match.end()


def _first_line(content: str, match: "re.Match[str]") -> int:
    """1-based document line of the first frontmatter line."""
    return content.count("\n", 0, match.start(1)) + 1


def frontmatter_node(content: str) -> Tuple[Optional[Any], int]:
    """``(composed YAML node, first frontmatter line)``, for per-node line marks.

    Call after :func:`split_frontmatter` succeeded; ``(None, 1)`` otherwise.
    A node's document line is ``first + node.start_mark.line``.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None, 1
    try:
        return yaml.compose(match.group(1)), _first_line(content, match)
    except yaml.YAMLError:
        return None, 1


def frontmatter_alias_line(content: str) -> Optional[int]:
    """Document line of the first YAML alias (``*name``, ``<<: *name``), or None."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    try:
        for event in yaml.parse(match.group(1)):
            if isinstance(event, yaml.AliasEvent):
                return _first_line(content, match) + event.start_mark.line
    except yaml.YAMLError:
        return None
    return None


def frontmatter_key_lines(content: str) -> Dict[str, int]:
    """Dotted key path -> 1-based document line, for nested mapping keys.

    Call after :func:`split_frontmatter` succeeded; returns ``{}`` otherwise.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    try:
        root = yaml.compose(match.group(1))
    except yaml.YAMLError:
        return {}
    first = _first_line(content, match)
    lines: Dict[str, int] = {}

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, yaml.MappingNode):
            return
        for key_node, value_node in node.value:
            key = f"{prefix}{key_node.value}"
            lines[key] = first + key_node.start_mark.line
            walk(value_node, f"{key}.")

    walk(root, "")
    return lines
