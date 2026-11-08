"""Run a Wazuh overlay rule in shadow as a search over ``wazuh-archives-*``.

Wazuh matches a rule on the manager as each event is decoded; Vigil can only
search what the indexer stored. The archive holds every event after decoding
(it exists only when the customer sets ``logall_json``), so a rule's matchable
conditions on decoded fields become an OpenSearch query over it. The indexer's
template (wazuh-template.json) maps these fields as keyword; ``full_log`` is
analysed text. Types and case rules are read from Wazuh v4.9.0
(analysisd/rules.c, os_regex), checked with wazuh-logtest on 4.9.2:

==================  =========  ==========  ====================================
element             Wazuh type case        archive field and query
==================  =========  ==========  ====================================
``decoded_as``      name       sensitive   ``decoder.name`` term
``program_name``    osmatch    insensitive ``predecoder.program_name``
``location``        osmatch    insensitive ``location``
``id`` ``status``   osmatch    insensitive ``data.<name>``
``url`` ``protocol``
``srcport``
``dstport``
``user``            osmatch    insensitive ``data.dstuser`` when the event has
                                           one, else ``data.srcuser``
``action``          string     sensitive   ``data.action`` term, exact
``srcip`` ``dstip`` ip list    n/a         ``data.srcip`` / ``data.dstip``
                                           term, one IPv4 address
``field name=x``    osregex    insensitive ``data.x``; only a value osregex and
                                           osmatch read alike (see below)
``field type=       osmatch    insensitive ``data.x``
osmatch``
==================  =========  ==========  ====================================

An osmatch value is a literal, ``^`` / ``$`` anchoring its ends, ``|`` between
alternatives, found anywhere in the value, case-folded over ASCII only. On a
keyword field that is a term, prefix or wildcard query with ``case_insensitive``
(OpenSearch folds Unicode, so a pattern with a non-ASCII character is refused).
An untyped ``<field>`` is osregex: only ``^literal$``, ``^literal``,
``literal$`` or ``literal`` with no regex metacharacter is read, as osmatch.
All conditions are ANDed.

Anything that cannot be reproduced faithfully is refused with its reason, and a
refused rule is not validated: it has no passing runs and never promotes itself.
That is every ``if_*`` / ``same_*`` / ``different_*`` link (they need another
rule to have matched), ``frequency`` / ``timeframe`` (a count needs an
aggregation per correlated key), ``<list>``, geoip, ``<hostname>`` (the syslog
host or the agent name, the archive keeps both), ``<match>`` / ``<regex>`` /
``<pcre2>`` on ``full_log`` (analysed text: a substring, anchor or regex cannot
be reproduced exactly), a ``type`` other than the element's own, osregex with
metacharacters, negation, CIDR and IPv6 addresses (the archive keeps the
decoded text), escapes in a value, and any element not listed.
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
# Wazuh folds ASCII case in osmatch; OpenSearch's keyword queries are exact unless told.
_FOLD = {"case_insensitive": True}
_OSMATCH = {
    "program_name": "predecoder.program_name",
    "location": "location",
    **{
        t: f"data.{t}"
        for t in ("id", "status", "protocol", "url", "srcport", "dstport")
    },
}
_ADDRESS = {"srcip": "data.srcip", "dstip": "data.dstip"}
# What osregex reads as itself: none of its operators, nor ``.`` and ``<`` it also knows.
_PLAIN = re.compile(r"\^?[^\\()|^$+*?\[\]{}.&<]+\$?")
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
    """One osmatch value on a keyword field; ValueError with the reason."""
    if value.startswith("!"):
        raise ValueError("negation isn't reproduced")
    if "\\" in value:
        raise ValueError("an escape in a value isn't reproduced")
    if not value.isascii():
        raise ValueError("a non-ASCII value isn't folded as Wazuh folds it")
    clauses = []
    for alt in value.split("|"):
        head, tail = alt.startswith("^"), alt.endswith("$") and len(alt) > 1
        body = alt[head : len(alt) - tail]
        if not body or any(c in body for c in "^$"):
            raise ValueError("an anchor inside a value isn't reproduced")
        if head and tail:
            clauses.append({"term": {path: {"value": body, **_FOLD}}})
        elif head:
            clauses.append({"prefix": {path: {"value": body, **_FOLD}}})
        else:
            safe = re.sub(r"([\\*?])", r"\\\1", body)
            value = f"*{safe}" if tail else f"*{safe}*"
            clauses.append({"wildcard": {path: {"value": value, **_FOLD}}})
    if len(clauses) == 1:
        return clauses[0]
    return {"bool": {"should": clauses, "minimum_should_match": 1}}


def _user(value: str) -> Dict[str, Any]:
    """Wazuh matches ``<user>`` on the event's dstuser, else its srcuser."""
    return {
        "bool": {
            "should": [
                {"bool": {"filter": [_osmatch("data.dstuser", value)]}},
                {
                    "bool": {
                        "filter": [_osmatch("data.srcuser", value)],
                        "must_not": [{"exists": {"field": "data.dstuser"}}],
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    }


def _address(path: str, value: str) -> Dict[str, Any]:
    if value.startswith("!"):
        raise ValueError("negation isn't reproduced")
    if "/" in value:
        raise ValueError("a CIDR range isn't exact on a keyword field")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(f"{value!r} is not one address") from None
    if ip.version != 4:
        raise ValueError("an IPv6 address isn't normalised in the archive")
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
        if tag not in (*_OSMATCH, *_ADDRESS, "user", "action", "decoded_as", "field"):
            refuse(f"<{tag}> isn't translated")
            continue
        if not value:
            refuse(f"<{tag}> is empty")
            continue
        kind = (node.get("type") or "").strip().lower()
        try:
            if tag == "decoded_as":
                filters.append({"term": {_DECODER: value}})
            elif tag in _ADDRESS:
                filters.append(_address(_ADDRESS[tag], value))
            elif tag == "action":
                if kind:
                    raise ValueError(f"a {kind} <action> isn't reproduced")
                filters.append({"term": {"data.action": value}})  # a string compare
            elif tag == "field":
                name = (node.get("name") or "").strip()
                if not _FIELD_NAME.fullmatch(name):
                    raise ValueError(f"field name {name!r} isn't translated")
                if kind not in ("", "osmatch"):
                    raise ValueError(f"a {kind} <field> isn't reproduced")
                if not kind and not _PLAIN.fullmatch(value):
                    raise ValueError("osregex isn't translated faithfully")
                filters.append(_osmatch(f"data.{name}", value))
            else:
                if kind not in ("", "osmatch"):
                    raise ValueError(f"a {kind} <{tag}> isn't reproduced")
                filters.append(
                    _user(value) if tag == "user" else _osmatch(_OSMATCH[tag], value)
                )
        except ValueError as exc:
            refuse(f"<{tag}>: {exc}")
    if not filters and not reasons:
        refuse("no condition on a decoded field: it would match every event")
    if reasons:
        return Translation(reasons=reasons)
    return Translation(query={"bool": {"filter": filters}})
