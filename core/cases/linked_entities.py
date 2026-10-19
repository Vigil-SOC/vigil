"""What a case's linked alerts name, read from their ``entity_context``.

A hunt links the alerts its evidence cites (#1916), so the case holds them by
reference. Their hosts and users are the case's entities and their addresses,
domains, URLs and hashes its IOCs. Read when asked and never stored: unlinking
an alert takes its entities with it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# entity_context keys, as the sources write them, by what they name.
ENTITY_KEYS = ("hostnames", "hosts", "usernames", "users")
IOC_KEYS = {
    "src_ips": "ip",
    "dest_ips": "ip",
    "source_ips": "ip",
    "ips": "ip",
    "ip_addresses": "ip",
    "domains": "domain",
    "urls": "url",
    "emails": "email",
    "file_hashes": "hash",
    "hashes": "hash",
}

LINKED_SOURCE = "Linked alert"

# What one alert names, by kind, as a model reads it in a page of findings (#2277).
# The processes and files an EDR alert names are here too: a hunt ties a chain
# together on them as much as on a host.
SUMMARY_KEYS = {
    "hosts": ("hostnames", "hosts"),
    "users": ("usernames", "users"),
    "ips": ("src_ips", "dest_ips", "source_ips", "ips", "ip_addresses"),
    "domains": ("domains",),
    "urls": ("urls",),
    "emails": ("emails",),
    "hashes": ("file_hashes", "hashes"),
    "processes": ("processes",),
    "files": ("files",),
}


def _values(context: Any, key: str) -> list[str]:
    if not isinstance(context, Mapping):
        return []
    raw = context.get(key)
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(item).strip() for item in items if isinstance(item, (str, int))]


def finding_entities(context: Any) -> list[str]:
    """Hosts and users one alert names, in the order it names them, once each."""
    found = [v for key in ENTITY_KEYS for v in _values(context, key) if v]
    return list(dict.fromkeys(found))


def finding_entity_summary(
    context: Any, per_kind: int = 5, chars: int = 128
) -> dict[str, Any]:
    """What one alert names, by kind, bounded so twenty of them stay small.

    Each kind keeps its first ``per_kind`` values, each cut to ``chars``; a kind
    that lost values is named under ``truncated``. The raw record under
    ``source_evidence`` is left out: that is what ``get_finding`` is for.
    """
    summary: dict[str, Any] = {}
    truncated: list[str] = []
    for kind, keys in SUMMARY_KEYS.items():
        found = [v for key in keys for v in _values(context, key) if v]
        values = list(dict.fromkeys(found))
        if not values:
            continue
        if len(values) > per_kind:
            truncated.append(kind)
        summary[kind] = [value[:chars] for value in values[:per_kind]]
    if truncated:
        summary["truncated"] = truncated
    return summary


def finding_iocs(context: Any) -> list[tuple[str, str]]:
    """``(ioc_type, value)`` pairs one alert names, once each."""
    found = [
        (kind, value)
        for key, kind in IOC_KEYS.items()
        for value in _values(context, key)
        if value
    ]
    return list(dict.fromkeys(found))


def linked_iocs(
    contexts: Iterable[Any], held: Iterable[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """The IOCs of every alert, without any ``held`` already on the case."""
    seen = {(kind.lower(), value) for kind, value in held}
    out: list[tuple[str, str]] = []
    for context in contexts:
        for pair in finding_iocs(context):
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out
