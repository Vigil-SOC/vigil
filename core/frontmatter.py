"""The one YAML-frontmatter reader for ``WORKFLOW.md``, ``SKILL.md`` and ``INTENT.md``.

Callers decide what a miss means: a workflow without frontmatter is a workflow
with no metadata, a skill without one is a 400, an intent file without one is a
warning. This module only splits and parses.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


class FrontmatterError(ValueError):
    """The ``---`` block is present but is not a YAML mapping.

    ``body_offset`` still says where the body starts, so a caller that wants to
    keep the document despite the bad header can.
    """

    def __init__(self, message: str, body_offset: int):
        super().__init__(message)
        self.body_offset = body_offset


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
        raise FrontmatterError(f"invalid YAML: {exc}", match.end()) from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise FrontmatterError("frontmatter must be a YAML mapping", match.end())
    return parsed, match.end()
