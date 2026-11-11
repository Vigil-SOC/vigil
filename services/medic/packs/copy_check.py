"""copy_check: refuse vendor-directed copy in the Medic content Vigil ships.

    python -m services.medic.packs.copy_check [path ...]

Fails (exit 1) on advice that tells the operator to send, report, escalate or attach
something to DeepTempo or its support, or that mentions an "evidence report", and
prints each file and the matched text. Paths default to what Vigil ships: the
medic-core source, the bundled pack and the contract fixtures. CI only; it never runs
at engine load or pack import, so operators' own packs are untouched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SHIPPED = [
    "services/medic/packs/medic-core",
    "services/medic/packs/bundled/medic-core.json",
    "services/medic/contracts/fixtures",
]

# Who the copy must not send the operator to. A bare "vendor" is a third-party ingest
# vendor and a bare "support" is usually the support bundle, so neither counts alone.
_VENDOR = (
    r"(?:deeptempo(?:'s)?(?:\s+(?:support|team|engineers?))?"
    r"|(?:the\s+|your\s+)?vendor(?:'s)?\s+support"
    r"|(?:the\s+)?support\s+team|(?:customer|technical)\s+support"
    r"|support@\S+)"
)
_ACTION = (
    r"\b(?:send|report|escalat|attach|forward|submit|upload|share|e-?mail|contact"
    r"|notify|tell|ask|reach\s+out|raise|open|file|call|page)\w*"
)
PATTERNS = [
    re.compile(r"\bevidence\s+report", re.IGNORECASE),
    # "send this incident to DeepTempo Support", "escalate to the vendor's support".
    re.compile(
        _ACTION + r"\b[^.]{0,120}?\b(?:to|with|for)\s+" + _VENDOR + r"\b", re.IGNORECASE
    ),
    # "contact DeepTempo", "open a ticket with support@deeptempo.ai".
    re.compile(_ACTION + r"\s+(?:\w+\s+){0,3}?" + _VENDOR + r"\b", re.IGNORECASE),
    # Naming the vendor's support desk at all is the vendor-directed part.
    re.compile(
        r"\b(?:deeptempo|vendor)(?:'s)?\s+support\b(?!\s+bundle)", re.IGNORECASE
    ),
]


def _normalise(text: str) -> str:
    # JSON escapes from the bundled pack, then YAML folding: one space per run.
    text = re.sub(r"\\[nrt]", " ", text).replace('\\"', '"')
    return re.sub(r"\s+", " ", text)


def _files(paths: list[str]):
    for p in map(Path, paths):
        yield from sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]


def main(argv: list[str]) -> int:
    hits = 0
    for f in _files(argv or SHIPPED):
        text = _normalise(f.read_text(encoding="utf-8", errors="replace"))
        spans = sorted(
            (m.start(), -m.end()) for pat in PATTERNS for m in pat.finditer(text)
        )
        reach = -1
        for start, neg_end in spans:  # one line per phrase: skip matches inside another
            if -neg_end > reach:
                hits += 1
                reach = -neg_end
                print(f"{f}: {text[start:reach]!r}")
    if hits:
        print(
            f"{hits} vendor-directed or evidence-report mention(s) in shipped Medic content"
        )
        return 1
    print("no vendor-directed copy in shipped Medic content")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
