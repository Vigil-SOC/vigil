"""Triage facts held to the alert fields they cite.

The daemon's triage answers in lines; its KNOWN line lists the facts a verdict
rests on, each followed by the alert fields it came from in brackets:
``beacons every 60 s to 203.0.113.77 [Description]``. A fact must cite a field
the prompt showed, or say it came from general knowledge, and every address
or host name it states must appear in the fields it cites. Anything else is
an error the model is asked once to correct; an answer still wrong after that
is not one a verdict may stand on.

Pure: parsing and checking only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping

#: Cited without a field to check against: what the model knows, not the alert.
GENERAL_KNOWLEDGE = "general knowledge"

# A defanged dot, as alerts and analysts write an address so it can't be clicked:
# 203.0.113[.]77. Not a citation, and the same address as 203.0.113.77.
DEFANGED = "[.]"
_HELD = "\x00"

_FACT = re.compile(r"([^\[\];]+?)\s*\[([^\]]*)\]")
# Four dotted numbers, not inside a longer run of them; a sentence's full stop
# after the address is not part of it.
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
# A dotted name with a letter in its last label: a host or domain, not a number.
_HOST = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9](?![\w-])",
    re.IGNORECASE,
)


@dataclass
class Fact:
    claim: str
    fields: List[str] = field(default_factory=list)
    general: bool = False
    unknown: List[str] = field(default_factory=list)


def parse_facts(known: str, field_names: List[str]) -> List[Fact]:
    """Each ``claim [source, source]`` in the KNOWN line, separated by ";"."""
    by_name = {name.lower(): name for name in field_names}
    facts = []
    held = (known or "").replace(_HELD, "").replace(DEFANGED, _HELD)
    for claim, cited in _FACT.findall(held):
        claim = claim.replace(_HELD, DEFANGED)
        fact = Fact(claim=claim.strip(" ;").strip())
        # "," or ";" between fields: models write either inside one bracket.
        for token in (t.strip() for t in re.split(r"[,;]", cited)):
            if not token:
                continue
            if token.lower() == GENERAL_KNOWLEDGE:
                fact.general = True
            elif token.lower() in by_name:
                fact.fields.append(by_name[token.lower()])
            else:
                fact.unknown.append(token)
        if fact.claim:
            facts.append(fact)
    return facts


def _refang(text: str) -> str:
    return text.replace(DEFANGED, ".")


def _names(claim: str) -> List[str]:
    """The addresses and host names a claim states, the parts that can be checked."""
    claim = _refang(claim)
    ips = _IPV4.findall(claim)
    hosts = [h for h in _HOST.findall(claim) if not _IPV4.fullmatch(h)]
    return ips + hosts


def _appears(name: str, text: str) -> bool:
    """``name`` in ``text`` as a whole address or host, not inside a longer one:
    10.0.0.1 is not in 10.0.0.15, nor evil.com in notevil.com or evil.com.au. A
    host is in its subdomains: evil.com is in www.evil.com."""
    if _IPV4.fullmatch(name):
        pattern = rf"(?<!\d)(?<!\d\.){re.escape(name)}(?!\.?\d)"
    else:
        pattern = rf"(?<![\w-]){re.escape(name)}(?!\.?[\w-])"
    return re.search(pattern, text) is not None


def check(facts: List[Fact], fields: Mapping[str, str]) -> List[str]:
    """What is wrong with the facts against the alert ``fields`` the prompt showed."""
    allowed = ", ".join(f"[{name}]" for name in fields) + f", [{GENERAL_KNOWLEDGE}]"
    if not facts:
        return [
            "KNOWN lists no fact with its sources in brackets, e.g. "
            "'the host ran an encoded PowerShell command [Description]'"
        ]
    errors = []
    for n, fact in enumerate(facts, 1):
        label = f"fact {n} ({fact.claim[:60]})"
        for token in fact.unknown:
            errors.append(f"{label} cites '{token}': cite one of {allowed}")
        if not (fact.fields or fact.general or fact.unknown):
            errors.append(f"{label} names no source")
        if not fact.fields:
            continue
        shown = _refang(" ".join(fields[name] for name in fact.fields)).lower()
        for name in _names(fact.claim):
            if not _appears(name.lower(), shown):
                errors.append(
                    f"{label} states {name}, which is not in "
                    f"{', '.join(fact.fields)}"
                )
    return errors


def stored_facts(facts: List[Fact]) -> List[Dict[str, object]]:
    """The facts as kept on the triage result."""
    out: List[Dict[str, object]] = []
    for fact in facts:
        sources = list(fact.fields) + ([GENERAL_KNOWLEDGE] if fact.general else [])
        out.append({"claim": fact.claim, "sources": sources})
    return out


__all__ = ["GENERAL_KNOWLEDGE", "Fact", "check", "parse_facts", "stored_facts"]
