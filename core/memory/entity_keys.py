"""Entity Keys, and the one rule for making one.

Only Python writes an Entity Key. The harness extracts candidates and hands over
a type and a value; the key they join on is minted here, so a stored key and a
queried key cannot be normalised by two rules that drift.

The rule is ``defang`` (``core.storage.defang``) then case-fold, mirroring
``services/agent/plugins/hunt/entities.ts``. ``user`` and ``email`` are
case-folded on purpose, including keys a person types; the reason, and what
that costs, is the Entity Key entry in CONTEXT.md. The exception is the half
that breaks silently: an ARN's resource part and an AWS key id are
case-significant, so folding them makes two principals one key — and the join
still returns rows, just the wrong ones. Two types keep case in part (#4542):
a ``process`` that is an absolute POSIX path keeps it whole, and a ``url``
case-folds only its scheme and authority, since its path, query and fragment
are case-significant.

``scripts/check_entity_key_agreement.py`` runs this and the harness on the same
inputs in CI and fails when they disagree; change a rule in both halves.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable, List, Tuple

from core.memory.recall_contract import ENTITY_KEY_TYPES, KEY_CASE_SENSITIVE_TYPES
from core.memory.tlds import TLDS
from core.storage.defang import defang, trim_edges
from core.storage.entity_context import (
    CONTEXT_LISTS,
    CONTEXT_SCALARS,
    context_candidates,
)


def _fold(kind: str, text: str) -> str:
    """Case-fold ``text`` as an Entity Key value of ``kind``.

    Plain string operations, as in entities.ts: a URL parser re-serialises
    (trailing slash, percent-encoding, port) and the two languages would drift.
    ``user`` and ``email`` are case-folded on purpose. Keeping their case here,
    in both halves, passes the parity check and splits account history. Why,
    and what that costs, is the Entity Key entry in CONTEXT.md.
    """
    if kind in KEY_CASE_SENSITIVE_TYPES:
        return text
    if kind == "process" and text.startswith("/"):
        return text  # an absolute POSIX path: case-significant on Linux
    if kind == "url":
        # Scheme and authority fold; from the first / ? # after them, case stays.
        sep = text.find("://")
        start = sep + 3 if sep >= 0 else 0
        end = next((i for i in range(start, len(text)) if text[i] in "/?#"), len(text))
        return text[:end].lower() + text[end:]
    return text.lower()


def entity_key(entity_type: str, value: str) -> str:
    """Return ``type:value``, or ``""`` when either half is absent.

    An empty key is dropped by the caller rather than raising: one unusable
    candidate in a payload must not fail the investigation it arrived in.
    """
    kind = trim_edges(entity_type or "").lower()
    text = defang(trim_edges(value or ""))
    if not kind or not text:
        return ""
    return f"{kind}:{_fold(kind, text)}"


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
    kind, _, value = trim_edges(key or "").partition(":")
    return entity_key(kind, value)


def normalise_keys(keys: object) -> List[str]:
    """Keys for a list of ``type:value`` strings, deduped in order."""
    return _deduped(
        normalise_key(str(key))
        for key in (keys if isinstance(keys, (list, tuple)) else [])
    )


# Free-text extraction, ported from `fromText` in
# services/agent/plugins/hunt/entities.ts. Same patterns, same well-formed
# checks, so a pasted report and a hunt's evidence agree on what is an entity.
# re.ASCII because JS `\b`, `\d` and `[a-z]` are ASCII-only; Python's default
# Unicode classes would match wider and the two would drift. JS `\s` is the
# exception — it is Unicode even without the `u` flag — so it is spelled out.
_FLAGS = re.IGNORECASE | re.ASCII
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_TEXT_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("arn", re.compile(rf"\barn:aws:[a-z0-9-]*:[^\"',{_JS_WS}]*", _FLAGS)),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b", re.ASCII)),
    ("url", re.compile(rf"\bhttps?://[^\"'<>{_JS_WS}]+", _FLAGS)),
    ("email", re.compile(r"\b[a-z0-9._%+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24}\b", _FLAGS)),
    ("hash", re.compile(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32})\b", _FLAGS)),
    ("ip", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", re.ASCII)),
    ("ip", re.compile(r"\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b", _FLAGS)),
    (
        "domain",
        re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}\b", _FLAGS),
    ),
)
_VERSION_WORD = re.compile(rf"\b(?:v|ver|version|release|build)[.:={_JS_WS}]*$", _FLAGS)
_TRAILING_PUNCT = re.compile(r"[.,;)]+$")
_HASH = re.compile(r"^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _has_known_tld(value: str) -> bool:
    return value.lower().rpartition(".")[2] in TLDS


def well_formed(entity_type: str, value: str) -> bool:
    """Matched loosely then validated, as the hunt extractor does.

    A pattern tight enough to accept only real values is unreadable and still
    wrong on IPv6 and on TLDs, so the regexes over-match and this decides.
    """
    if value in ("", "-", "null"):
        return False
    if entity_type == "ip":
        return _is_ip(value)
    if entity_type in ("domain", "email"):
        return "." in value and _has_known_tld(value)
    if entity_type == "hash":
        return _HASH.match(value) is not None
    return True


def text_entity_keys(raw: str) -> List[str]:
    """Entity Keys for the well-formed candidates in free text, deduped in order."""
    text = defang(raw or "")
    minted: List[str] = []
    for kind, pattern in _TEXT_PATTERNS:
        for match in pattern.finditer(text):
            value = _fold(kind, _TRAILING_PUNCT.sub("", match.group(0)))
            # A dotted quad after a version word is a release, not a host.
            before = text[max(0, match.start() - 12) : match.start()]
            if kind == "ip" and _VERSION_WORD.search(before):
                continue
            if well_formed(kind, value):
                minted.append(entity_key(kind, value))
    return _deduped(minted)


# How a finding's entity context is spelled is `core.storage.entity_context`'s one
# map, shared with cross-investigation correlation: a new ingest field reaches
# it and memory recall at once.
_CONTEXT_TYPES = {kind for kind, _, _ in CONTEXT_LISTS} | {
    kind for kind, _, _ in CONTEXT_SCALARS
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
    return [(kind, value) for kind, value, _ in context_candidates(finding)]


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
