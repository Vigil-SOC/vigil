"""The text of one Sigma rule: read it, fill in what a reviewer would add, add an exclusion."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

_LEVELS = {"informational", "low", "medium", "high", "critical"}
_TECHNIQUE_TAG = re.compile(r"attack\.(t\d{4}(?:\.\d{3})?)", re.IGNORECASE)
TECHNIQUE_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
EXCLUSION_MATCHES = ("contains", "startswith", "endswith", "equals")
MAX_YAML_DEPTH = 32
_OPENS = (
    yaml.BlockMappingStartToken,
    yaml.BlockSequenceStartToken,
    yaml.FlowMappingStartToken,
    yaml.FlowSequenceStartToken,
)
_CLOSES = (yaml.BlockEndToken, yaml.FlowMappingEndToken, yaml.FlowSequenceEndToken)


class NotARule(ValueError):
    """The text cannot be changed as a Sigma rule; the message says why."""


@dataclass(frozen=True)
class Exclusion:
    """A person's "not this": rows where ``field`` matches any of ``values``."""

    field: str
    match: str
    values: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not _FIELD_NAME.match(self.field):
            raise NotARule(f"{self.field!r} is not a field name.")
        if self.match not in EXCLUSION_MATCHES:
            raise NotARule(f"{self.match!r} is not one of {EXCLUSION_MATCHES}.")
        if not self.values or not all(v.strip() for v in self.values):
            raise NotARule("An exclusion needs at least one value.")


def unsafe_because(text: str) -> Optional[str]:
    """Why loading ``text`` could cost far more than its size, read from its tokens alone.

    An alias or merge key lets a few hundred bytes expand without bound, and loading recurses
    once per level of nesting. Scanning does neither. Text that does not scan is left for the
    loader to refuse.
    """
    depth = 0
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) or (
                isinstance(token, yaml.ScalarToken)
                and token.plain
                and token.value == "<<"
            ):
                return (
                    "This text uses a YAML alias or merge key, and Vigil does not expand "
                    "or nest those in a rule. Write the values out in full."
                )
            depth += isinstance(token, _OPENS)
            depth -= isinstance(token, _CLOSES)
            if depth > MAX_YAML_DEPTH:
                return (
                    f"This text is nested more than {MAX_YAML_DEPTH} levels deep, and Vigil "
                    "does not expand or nest a rule that far."
                )
    except yaml.YAMLError:
        return None
    return None


def parse(text: str) -> Optional[Dict[str, Any]]:
    """The one YAML mapping ``text`` holds, or None."""
    if unsafe_because(text):
        return None
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return None
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return None
    return docs[0]


def techniques(doc: Dict[str, Any]) -> List[str]:
    tags = doc.get("tags")
    if not isinstance(tags, list):
        return []
    found = (_TECHNIQUE_TAG.fullmatch(str(tag)) for tag in tags)
    return list(dict.fromkeys(m.group(1).upper() for m in found if m))


def names_every_field(doc: Dict[str, Any]) -> bool:
    """Whether every selection is field maps. A keyword selection reads the whole event."""
    detection = doc.get("detection")
    if not isinstance(detection, dict):
        return False
    return all(
        all(
            isinstance(item, dict)
            for item in (body if isinstance(body, list) else [body])
        )
        for key, body in detection.items()
        if key != "condition"
    )


def fields_read(doc: Dict[str, Any]) -> Set[str]:
    """Every event field the rule's detection names, without modifiers."""
    detection = doc.get("detection")
    if not isinstance(detection, dict):
        return set()
    names: Set[str] = set()
    for key, body in detection.items():
        if key == "condition":
            continue
        for item in body if isinstance(body, list) else [body]:
            if isinstance(item, dict):
                names.update(str(k).split("|", 1)[0] for k in item)
    names.discard("")
    return names


def fill_in(text: str, technique: Optional[str]) -> Tuple[str, List[str]]:
    """``text`` with an id, level, status and technique tag, and what was added.

    Text that is not one mapping comes back as it is, for lint to say so. A rule keeps its
    id through every later edit, so the overlay lands a change on the same rule.
    """
    doc = parse(text)
    if doc is None:
        return text, []
    fixes: List[str] = []
    given = doc.get("id")
    if not given:
        doc = {"id": str(uuid.uuid4()), **doc}
        fixes.append("added id")
    else:
        try:
            uuid.UUID(str(given))
        except ValueError:
            doc["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sigma-id:{given}"))
            fixes.append(f"id {given!r} is not a UUID; replaced with {doc['id']}")
    if doc.get("level") not in _LEVELS:
        fixes.append(f"level {doc.get('level')!r} became medium")
        doc["level"] = "medium"
    if "status" not in doc:
        doc["status"] = "experimental"
        fixes.append("status set to experimental")
    if technique:
        tag = f"attack.{technique.lower()}"
        tags = doc.get("tags")
        tags = [str(t) for t in tags] if isinstance(tags, list) else []
        if tag not in [t.lower() for t in tags]:
            doc["tags"] = [*tags, tag]
            fixes.append(f"added tag {tag}")
    if not fixes:
        return text, []
    return dump(doc), fixes


def dump(doc: Dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def add_exclusion(text: str, exclusion: Exclusion) -> str:
    """The rule with a new filter selection, and ``and not`` it on every condition."""
    doc = parse(text)
    detection = doc.get("detection") if doc else None
    if doc is None or not isinstance(detection, dict):
        raise NotARule("An exclusion needs a rule with a detection mapping.")
    condition = detection.get("condition")
    number = 1
    while f"filter_person_{number}" in detection:
        number += 1
    name = f"filter_person_{number}"
    key = (
        exclusion.field
        if exclusion.match == "equals"
        else f"{exclusion.field}|{exclusion.match}"
    )

    def narrowed(one: Any) -> str:
        return f"({one}) and not {name}"

    if isinstance(condition, list) and condition:
        new_condition: Any = [narrowed(c) for c in condition]
    elif isinstance(condition, str) and condition.strip():
        new_condition = narrowed(condition)
    else:
        raise NotARule("An exclusion needs a rule with a condition.")
    selections = {k: v for k, v in detection.items() if k != "condition"}
    doc["detection"] = {
        **selections,
        name: {key: list(exclusion.values)},
        "condition": new_condition,
    }
    return dump(doc)
