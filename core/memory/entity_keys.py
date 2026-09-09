"""Entity Keys, and the one rule for making one.

Only Python writes an Entity Key. The harness extracts candidates and hands over
a type and a value; the key they join on is minted here, so a stored key and a
queried key cannot be normalised by two rules that drift.

The rule is ``defang`` then case-fold, mirroring
``services/agent/workflows/hunt/entities.ts``. The exception is the half that
breaks silently: an ARN's resource part and an AWS key id are case-significant,
so folding them makes two principals one key — and the join still returns rows,
just the wrong ones.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from core.memory.recall_contract import ENTITY_KEY_TYPES, KEY_CASE_SENSITIVE_TYPES

# Threat intel writes addresses defanged, and threat_intel is a worker whose
# output feeds this, so normalising first is cheaper than carrying defanged
# variants of every key.
_DEFANG: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[\.\]|\(\.\)|\{\.\}"), "."),
    (re.compile(r"\bh(?:xx)p", re.IGNORECASE), "http"),
    (re.compile(r"\[:\]"), ":"),
    (re.compile(r"\[at\]", re.IGNORECASE), "@"),
)


def defang(text: str) -> str:
    for pattern, replacement in _DEFANG:
        text = pattern.sub(replacement, text)
    return text


def entity_key(entity_type: str, value: str) -> str:
    """Return ``type:value``, or ``""`` when either half is absent.

    An empty key is dropped by the caller rather than raising: one unusable
    candidate in a payload must not fail the investigation it arrived in.
    """
    kind = (entity_type or "").strip().lower()
    text = defang((value or "").strip())
    if not kind or not text:
        return ""
    if kind not in KEY_CASE_SENSITIVE_TYPES:
        text = text.lower()
    return f"{kind}:{text}"


def _deduped(minted: Iterable[str]) -> List[str]:
    """Keys in the order they were minted, without repeats or empties.

    Order is kept because a Verdict's subjects read back to a human. An unusable
    candidate is dropped rather than raising: one bad key among ten is a read of
    nine, and one unusable candidate in a payload must not fail the investigation
    it arrived in.
    """
    keys: List[str] = []
    seen = set()
    for key in minted:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def entity_keys(entities: object) -> List[str]:
    """Keys for a list of ``{"type", "value"}`` candidates, deduped in order."""
    return _deduped(
        entity_key(str(entity.get("type", "")), str(entity.get("value", "")))
        for entity in (entities if isinstance(entities, list) else [])
        if isinstance(entity, dict)
    )


def normalise_key(key: str) -> str:
    """Normalise a stored-form ``type:value`` key the way the writer minted it.

    The reader is handed keys as one string, where the writer was handed a type
    and a value. Splitting on the *first* colon is what keeps a URL or an IPv6
    address whole: everything after the type belongs to the value.
    """
    kind, _, value = (key or "").strip().partition(":")
    return entity_key(kind, value)


def normalise_keys(keys: object) -> List[str]:
    """Keys for a list of ``type:value`` strings, deduped in order."""
    return _deduped(
        normalise_key(str(key))
        for key in (keys if isinstance(keys, (list, tuple)) else [])
    )


# How a finding's entity context is spelled, in memory's vocabulary. One map, so
# a new ingest field reaches cross-investigation correlation and memory recall at
# once. Alternatives within a tuple are aliases: the first one present wins,
# because sources disagree about `dest` and `dst` and mean the same thing.
#
# `host`, not `hostname`: this is memory's spelling. The shared-IOC minter aliases
# it back on the way in, so the keys that subsystem writes are unchanged.
_CONTEXT_LISTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ip", ("src_ips",)),
    ("ip", ("dest_ips", "dst_ips")),
    ("host", ("hostnames",)),
    ("user", ("usernames", "users")),
    ("hash", ("file_hashes",)),
    ("domain", ("domains",)),
)

_CONTEXT_SCALARS: Tuple[Tuple[str, str], ...] = (
    ("ip", "src_ip"),
    ("ip", "dst_ip"),
    ("host", "hostname"),
    ("user", "user"),
)


_CONTEXT_TYPES = {kind for kind, _ in _CONTEXT_LISTS} | {
    kind for kind, _ in _CONTEXT_SCALARS
}
_OFF_VOCABULARY = _CONTEXT_TYPES - set(ENTITY_KEY_TYPES)
if _OFF_VOCABULARY:
    raise ImportError(
        f"entity context maps {sorted(_OFF_VOCABULARY)} onto no Entity Key type; "
        "a key minted outside ENTITY_KEY_TYPES is one no reader will ever query"
    )


def entity_context_candidates(finding: object) -> List[Tuple[str, str]]:
    """A finding's entity context as ``(type, value)`` candidates.

    Typed but not minted: the caller decides which vocabulary the key is spelled
    in, and there are two. Empty for a finding carrying no entity context, which
    is what keeps "nothing was asked" apart from "nothing is known".
    """
    context = finding.get("entity_context") if isinstance(finding, dict) else None
    if not isinstance(context, dict):
        return []

    candidates: List[Tuple[str, str]] = []
    for kind, names in _CONTEXT_LISTS:
        values = next((context[n] for n in names if context.get(n)), None) or []
        if not isinstance(values, (list, tuple)):
            continue
        candidates.extend((kind, str(value)) for value in values)

    for kind, name in _CONTEXT_SCALARS:
        value = context.get(name)
        if value:
            candidates.append((kind, str(value)))

    return candidates


def finding_entity_keys(findings: object) -> List[str]:
    """Entity Keys for the findings an investigation was opened on.

    The deduped union across every finding, not the first one's: an investigation
    opened over several findings is about all of them. Recall bounds itself per
    key, per kind and overall and reports what it dropped, so a wider union does
    not mean an unbounded prefix.
    """
    return _deduped(
        entity_key(kind, value)
        for finding in (findings if isinstance(findings, list) else [])
        for kind, value in entity_context_candidates(finding)
    )
