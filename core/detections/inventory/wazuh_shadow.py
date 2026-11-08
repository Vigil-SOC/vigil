"""Run a Wazuh overlay rule in shadow as a search over ``wazuh-archives-*``.

Wazuh matches a rule on the manager as each event is decoded; Vigil can only
search what the indexer stored. The archive holds every event after decoding
(it exists only when the customer sets ``logall_json``), so a rule's matchable
conditions on decoded fields become an OpenSearch query over it. The indexer's
template (wazuh-template.json) maps these fields as keyword; ``full_log`` is
analysed text:

====================  ==========================================
``<decoded_as>``      ``decoder.name`` (exact term)
``<program_name>``    ``predecoder.program_name``
``<location>``        ``location``
``<srcip>/<dstip>``   ``data.srcip`` / ``data.dstip`` (one address)
``<user>``            ``data.dstuser``
``<id>`` ``<status>`` ``<action>`` ``<protocol>`` ``<url>``
``<srcport>`` ``<dstport>``   ``data.<name>``
``<field name=x>``    ``data.x``
====================  ==========================================

A value is OSMatch: a literal, ``^`` / ``$`` anchoring its ends, ``|`` between
alternatives, searched anywhere in the value. On a keyword field that is a term,
prefix or wildcard query, case-sensitive as Wazuh is. All conditions are ANDed.

Anything that cannot be reproduced faithfully is refused with its reason, and a
refused rule is not validated: it has no passing runs and never promotes itself.
That is every ``if_*`` / ``same_*`` / ``different_*`` link (they need another
rule to have matched), ``frequency`` / ``timeframe`` (a count needs an
aggregation per correlated key), ``<list>``, geoip, ``<hostname>`` (the syslog
host or the agent name, the archive keeps both), ``<match>`` / ``<regex>`` /
``<pcre2>`` on ``full_log`` (analysed text: a substring, anchor or regex cannot
be reproduced exactly), negation (Wazuh's result when the field is missing is
not known here), CIDR addresses, escapes in a value, and any element not listed.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.detections import wazuh_xml

ARCHIVES = "wazuh-archives-*"
CANT = "can't validate: "
ARCHIVES_OFF = f"{CANT}archives off ({ARCHIVES} has no events in the last 7 days)"

_DECODER = "decoder.name"
# Wildcards on a keyword are case-sensitive by default; stated so it stays so.
_CASE = {"case_insensitive": False}
_KEYWORD = {
    "program_name": "predecoder.program_name",
    "location": "location",
    "user": "data.dstuser",
    **{t: f"data.{t}" for t in ("id", "status", "action", "protocol", "url")},
    **{t: f"data.{t}" for t in ("srcport", "dstport")},
}
_ADDRESS = {"srcip": "data.srcip", "dstip": "data.dstip"}
# Elements that say nothing about which events match.
_INERT = {"description", "group", "mitre", "info", "options", "example"}
_LINKS = re.compile(r"if_(sid|group|level|matched_\w+)|(same|different)_\w+")
_FIELD_NAME = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*")


@dataclass
class Translation:
    query: Optional[Dict[str, Any]] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def why(self) -> Optional[str]:
        """The stored reason when the rule cannot be validated, else None."""
        return CANT + "; ".join(self.reasons) if self.reasons else None


def _osmatch(path: str, value: str) -> Dict[str, Any]:
    """One OSMatch value on a keyword field; ValueError with the reason."""
    if value.startswith("!"):
        raise ValueError("negation isn't reproduced")
    if "\\" in value:
        raise ValueError("an escape in a value isn't reproduced")
    clauses = []
    for alt in value.split("|"):
        head, tail = alt.startswith("^"), alt.endswith("$") and len(alt) > 1
        body = alt[head : len(alt) - tail]
        if not body or any(c in body for c in "^$"):
            raise ValueError("an anchor inside a value isn't reproduced")
        if head and tail:
            clauses.append({"term": {path: body}})
        elif head:
            clauses.append({"prefix": {path: body}})
        else:
            safe = re.sub(r"([\\*?])", r"\\\1", body)
            value = f"*{safe}" if tail else f"*{safe}*"
            clauses.append({"wildcard": {path: {"value": value, **_CASE}}})
    return (
        clauses[0]
        if len(clauses) == 1
        else {"bool": {"should": clauses, "minimum_should_match": 1}}
    )


def _address(path: str, value: str) -> Dict[str, Any]:
    if value.startswith("!"):
        raise ValueError("negation isn't reproduced")
    if "/" in value:
        raise ValueError("a CIDR range isn't exact on a keyword field")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(f"{value!r} is not one address") from None
    return {"term": {path: value}}


def translate(text: str) -> Translation:
    """The archive query for the one Wazuh rule in ``text``, or why it has none."""
    try:
        rule = wazuh_xml.parse(text)
    except wazuh_xml.NotARule as exc:
        return Translation(reasons=[str(exc)])
    reasons: List[str] = []

    def refuse(why: str) -> None:
        if why not in reasons:
            reasons.append(why)

    for attr in ("frequency", "timeframe"):
        if rule.get(attr) is not None:
            refuse(f"{attr} needs an aggregation per correlated key")
    if rule.get("ignore") is not None:
        refuse("ignore suppresses by time after an earlier match")
    if (rule.get("overwrite") or "").strip().lower() == "yes":
        refuse("overwrite inherits the overwritten rule's parents")
    filters: List[Dict[str, Any]] = []
    for node in rule:
        tag = node.tag
        if tag in _INERT:
            continue
        value = (node.text or "").strip()
        if (node.get("negate") or "").strip().lower() == "yes":
            refuse(f"negate on <{tag}> isn't reproduced")
            continue
        if _LINKS.fullmatch(tag):
            refuse(f"<{tag}> depends on another rule having matched")
            continue
        if tag in ("match", "regex", "pcre2"):
            refuse(f"<{tag}> on full_log isn't exact")
            continue
        if tag == "list":
            refuse("<list> lookups aren't in the archive")
            continue
        if tag == "hostname":
            refuse("<hostname> is the syslog host or the agent name: two fields")
            continue
        if tag not in (*_KEYWORD, *_ADDRESS, "decoded_as", "field"):
            refuse(f"<{tag}> isn't translated")
            continue
        if not value:
            refuse(f"<{tag}> is empty")
            continue
        try:
            if tag == "decoded_as":
                filters.append({"term": {_DECODER: value}})
            elif tag in _ADDRESS:
                filters.append(_address(_ADDRESS[tag], value))
            else:
                if tag == "field":
                    name = (node.get("name") or "").strip()
                    kind = (node.get("type") or "osmatch").strip().lower()
                    if not _FIELD_NAME.fullmatch(name):
                        raise ValueError(f"field name {name!r} isn't translated")
                    if kind != "osmatch":
                        raise ValueError(f"a {kind} <field> isn't reproduced")
                    path = f"data.{name}"
                else:
                    path = _KEYWORD[tag]
                filters.append(_osmatch(path, value))
        except ValueError as exc:
            refuse(f"<{tag}>: {exc}")
    if not filters and not reasons:
        refuse("no condition on a decoded field: it would match every event")
    if reasons:
        return Translation(reasons=reasons)
    return Translation(query={"bool": {"filter": filters}})
