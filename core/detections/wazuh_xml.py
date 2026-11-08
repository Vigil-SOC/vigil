"""One Wazuh rule written as XML: read it, list what it matches, set its id.

A Vigil-written Wazuh rule is one ``<rule>`` in its own file, optionally inside
a ``<group>``, as the manager reads rule files. Its id must sit in Wazuh's
range for custom rules (``CUSTOM_IDS``). Nothing here talks to a manager.
"""

from __future__ import annotations

import re

# Only _root() feeds it text, and it refuses a DOCTYPE or ENTITY first.
import xml.etree.ElementTree as ET  # nosec B405
from typing import Iterable, List, Optional, Tuple

# Wazuh keeps 100000-120000 for a customer's own rules.
CUSTOM_IDS = range(100000, 120001)
# A custom id as the manager reads it: six ASCII digits, no leading zero.
_CUSTOM_ID = re.compile(r"[1-9][0-9]{5}")
# A document type can declare entities; a rule file has no use for one.
_DECLARATION = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
# An XML declaration (<?xml version="1.0"?>) may open the file, after a BOM.
_XML_DECLARATION = re.compile(r"\A\ufeff?\s*<\?xml\b[^>]*\?>")
# A comment may hold "<rule ...>" text that is no rule.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# The opening tag of the rule, to set its id in place and keep the text as written.
_RULE_TAG = re.compile(r"<rule\b([^>]*)>")
_ID_ATTR = re.compile(r"""\sid\s*=\s*(["'])[^"']*\1""")
# The elements that match on a value, and the field each value is read as for lint.
_VALUE_FIELDS = {
    "match": "match",
    "regex": "regex",
    "srcip": "srcip",
    "dstip": "dstip",
    "hostname": "hostname",
    "system_name": "hostname",
    "user": "user",
    "url": "url",
    "location": "location",
    "program_name": "program_name",
    "extra_data": "extra_data",
    "data": "data",
    "id": "id",
    "status": "status",
    "action": "action",
    "protocol": "protocol",
    "srcport": "srcport",
    "dstport": "dstport",
    "srcgeoip": "srcgeoip",
    "dstgeoip": "dstgeoip",
}
# A regex escapes the dots of an address or name (10\.20\.30\.40).
_ESCAPED = re.compile(r"\\([.\-/:@])")


class NotARule(ValueError):
    """The text is not exactly one readable Wazuh rule."""


def without_declaration(text: str) -> str:
    """``text`` without a leading XML declaration, which nothing may precede."""
    return _XML_DECLARATION.sub("", text, count=1)


def custom_id(value: Optional[str]) -> Optional[int]:
    """``value`` as a custom rule id (six ASCII digits, no leading zero, in
    ``CUSTOM_IDS``); None for anything else."""
    value = (value or "").strip()
    if not _CUSTOM_ID.fullmatch(value) or int(value) not in CUSTOM_IDS:
        return None
    return int(value)


def number(value: Optional[str]) -> Optional[int]:
    """An id as the number the manager reads it as (ASCII digits only), or None."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


def _root(text: str) -> ET.Element:
    """``text`` parsed, wrapped (a rule file may hold a <group> and a comment
    side by side); NotARule when it declares a DTD or is not well-formed."""
    if _DECLARATION.search(text):
        raise NotARule("a Wazuh rule file must not declare a DOCTYPE or ENTITY")
    wrapped = f"<vigil>{without_declaration(text)}</vigil>"
    try:
        # No DTD reaches here (refused above), so no entity can expand.
        return ET.fromstring(wrapped)  # nosec B314
    except ET.ParseError as exc:
        raise NotARule(f"not well-formed XML: {exc}") from exc


def parse(text: str) -> ET.Element:
    """The one ``<rule>`` element in ``text``; NotARule otherwise."""
    rules = _root(text).findall(".//rule")
    if len(rules) != 1:
        raise NotARule(f"a Wazuh copy holds exactly one <rule>, not {len(rules)}")
    return rules[0]


def parse_all(text: str) -> List[Tuple[ET.Element, List[str]]]:
    """Each ``<rule>`` in a rule file, in order, with its enclosing groups' names."""
    root = _root(text)
    found: List[Tuple[ET.Element, List[str]]] = []

    def walk(node: ET.Element, names: List[str]) -> None:
        for child in node:
            if child.tag == "rule":
                found.append((child, names))
            elif child.tag == "group":
                walk(child, names + (child.get("name") or "").split(","))
            else:
                walk(child, names)

    walk(root, [])
    return found


def rule_id(rule: ET.Element) -> Optional[str]:
    value = (rule.get("id") or "").strip()
    return value or None


def description(rule: ET.Element) -> Optional[str]:
    text = (rule.findtext("description") or "").strip()
    return text or None


def techniques(rule: ET.Element) -> List[str]:
    """The technique ids under ``<mitre>``, in order, once each."""
    found = [
        (node.text or "").strip().upper()
        for node in rule.findall("./mitre/id")
        if (node.text or "").strip()
    ]
    return list(dict.fromkeys(found))


def groups(rule: ET.Element, enclosing: List[str]) -> List[str]:
    """The rule's groups: its enclosing <group>s' names, then its own <group>'s."""
    names = enclosing + (rule.findtext("group") or "").split(",")
    return [n.strip() for n in dict.fromkeys(n.strip() for n in names) if n.strip()]


def conditions(rule: ET.Element) -> Iterable[Tuple[str, str, bool]]:
    """(field, value, negated) for each value the rule matches on.

    ``<field name="x">`` is read as field x; the other matching elements as the
    field they name (``<hostname>`` and ``<system_name>`` as hostname). A value
    the rule must not hold (``negate="yes"``, or a leading ``!`` on an address
    list) is negated; regex escapes before dots are dropped.
    """
    for node in rule:
        tag = node.tag
        if tag == "field":
            field = (node.get("name") or "").strip()
        else:
            field = _VALUE_FIELDS.get(tag, "")
        if not field:
            continue
        value = (node.text or "").strip()
        if not value:
            continue
        negated = (node.get("negate") or "").strip().lower() == "yes"
        if tag in ("srcip", "dstip") and value.startswith("!"):
            negated, value = True, value[1:]
        yield field, _ESCAPED.sub(r"\1", value), negated


def with_id(text: str, new_id: int) -> str:
    """``text`` with the rule's id set to ``new_id``, the rest as written."""
    comments = [m.span() for m in _COMMENT.finditer(text)]
    for match in _RULE_TAG.finditer(text):
        if any(start <= match.start() < end for start, end in comments):
            continue
        attrs = _ID_ATTR.sub("", match.group(1))
        return f'{text[: match.start()]}<rule id="{new_id}"{attrs}>{text[match.end():]}'
    return text
