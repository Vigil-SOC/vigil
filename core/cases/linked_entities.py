"""What a case's linked alerts name, read from their ``entity_context``.

A hunt links the alerts its evidence cites (#1916), so the case holds them by
reference. Their hosts and users are the case's entities (their indicators are
listed by ``CaseIOCService.list_iocs``). Read when asked and never stored:
unlinking an alert takes its entities with it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# entity_context keys, as the sources write them, by what they name.
ENTITY_KEYS = ("hostnames", "hosts", "usernames", "users")
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


# SUMMARY_KEYS' kinds as memory spells an Entity Key type; files have none.
_KEY_TYPES = {
    "hosts": "host",
    "users": "user",
    "ips": "ip",
    "domains": "domain",
    "urls": "url",
    "emails": "email",
    "hashes": "hash",
    "processes": "process",
}


def linked_entity_keys(contexts: Iterable[Any]) -> list[str]:
    """The Entity Keys every alert names, once each, for ``recall_entity``."""
    from core.memory.entity_keys import entity_key

    keys = (
        entity_key(_KEY_TYPES[kind], value)
        for context in contexts
        for kind, names in SUMMARY_KEYS.items()
        if kind in _KEY_TYPES  # files name no Entity Key type
        for name in names
        for value in _values(context, name)
    )
    return list(dict.fromkeys(key for key in keys if key))
