"""How a finding's entity context is spelled, and the role an IP plays in it.

The one map behind memory's Entity Keys, the ``shared_iocs`` index and the
matching of an alert to a Case, so a new ingest field reaches all three at once.
Alternatives within a tuple are aliases: the first one present wins, because
sources disagree about `dest` and `dst` and mean the same thing.

Each entry's last element is the type the entity is indexed under in
``shared_iocs``: ``src_ip``/``dst_ip`` carry an IP's role, ``None`` is the kind.
"""

from typing import List, Optional, Tuple

# `host`, not `hostname`: this is memory's spelling. The shared-IOC minter aliases
# it back on the way in, so the keys that subsystem writes are unchanged.
CONTEXT_LISTS: Tuple[Tuple[str, Tuple[str, ...], Optional[str]], ...] = (
    ("ip", ("src_ips",), "src_ip"),
    ("ip", ("dest_ips", "dst_ips"), "dst_ip"),
    ("host", ("hostnames",), None),
    ("user", ("usernames", "users"), None),
    ("hash", ("file_hashes",), None),
    ("domain", ("domains",), None),
    ("process", ("processes",), None),
    ("url", ("urls",), None),
    ("email", ("emails",), None),
)

CONTEXT_SCALARS: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("ip", "src_ip", "src_ip"),
    ("ip", "dst_ip", "dst_ip"),
    ("host", "hostname", None),
    ("user", "user", None),
)


def context_candidates(finding: object) -> List[Tuple[str, str, str]]:
    """A finding's entity context as ``(kind, value, indexed type)`` candidates.

    Empty for a finding carrying no entity context, which is what keeps "nothing
    was asked" apart from "nothing is known".
    """
    context = finding.get("entity_context") if isinstance(finding, dict) else None
    if not isinstance(context, dict):
        return []

    candidates: List[Tuple[str, str, str]] = []
    for kind, names, typed in CONTEXT_LISTS:
        values = next((context[n] for n in names if context.get(n)), None) or []
        if not isinstance(values, (list, tuple)):
            continue
        candidates.extend((kind, str(value), typed or kind) for value in values)

    for kind, name, typed in CONTEXT_SCALARS:
        value = context.get(name)
        if value:
            candidates.append((kind, str(value), typed or kind))

    return candidates
