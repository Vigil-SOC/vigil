"""A NetFlow finding's title and description, in plain words, from its flows.

Built from the digest over every flow (``netflow_digest.py``), never from the
detector's verdict or score, and with no model call:

    title        192.168.155.1 → 3.218.110.90:443
    description  192.168.155.1 sent 170 TCP flows to 3.218.110.90 on port 443
                 between 17:45 and 17:50 UTC on 21 Oct. It sent 203.0 KB and
                 received 1.1 MB.

The recorded source is the client and the destination the server, as the
digest records them; nothing is inferred. Pure: no I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Mapping, Optional

# Ports named in a title before the rest are counted as "+n".
TITLE_PORTS = 3


def _size(n: int) -> str:
    for unit, scale in (("GB", 1_000_000_000), ("MB", 1_000_000), ("KB", 1_000)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _clock(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(digest: Mapping[str, Any]) -> str:
    first, last = _clock(digest.get("first")), _clock(digest.get("last"))
    if not first or not last:
        return ""
    day = f"{first.day} {first:%b}"
    if (first.hour, first.minute) == (last.hour, last.minute):
        return f" at {first:%H:%M} UTC on {day}"
    if first.date() != last.date():
        return (
            f" between {first:%H:%M} UTC on {day} and "
            f"{last:%H:%M} UTC on {last.day} {last:%b}"
        )
    return f" between {first:%H:%M} and {last:%H:%M} UTC on {day}"


def _only_pair(digest: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    pairs = digest.get("pairs") or []
    if len(pairs) == 1 and not digest.get("pairs_omitted"):
        return pairs[0]
    return None


def _ports(pair: Mapping[str, Any]) -> List[int]:
    return [int(p) for p, _n in pair.get("top_ports") or []]


def _port_text(pair: Mapping[str, Any]) -> str:
    """'443', '80/443', or '80/443/8080 +4' past TITLE_PORTS."""
    ports = _ports(pair)
    if not ports:
        return ""
    shown = "/".join(str(p) for p in sorted(ports[:TITLE_PORTS]))
    more = int(pair.get("port_count") or len(ports)) - min(len(ports), TITLE_PORTS)
    return f"{shown} +{more}" if more > 0 else shown


def _protocol(digest: Mapping[str, Any]) -> str:
    protocols = [
        p for p in (digest.get("protocols") or {}) if p and p.lower() != "unknown"
    ]
    return f"{protocols[0]} " if len(protocols) == 1 else ""


def title(
    digest: Optional[Mapping[str, Any]], focal: Any, engaged: Any
) -> Optional[str]:
    """'client → server:port' for one conversation, else the two hosts.

    None when neither the flows nor the row name both hosts.
    """
    pair = _only_pair(digest or {})
    if pair and pair.get("src") and pair.get("dst"):
        ports = _port_text(pair)
        return f"{pair['src']} → {pair['dst']}{':' + ports if ports else ''}"
    if not focal or not engaged:
        return None
    return f"NetFlow: {focal} ↔ {engaged}"


def description(
    digest: Optional[Mapping[str, Any]], focal: Any, engaged: Any
) -> Optional[str]:
    """Who sent how many flows to what, when; then the bytes each way.

    None when there are no flows and the row doesn't name both hosts.
    """
    if not digest or not digest.get("flows"):
        if not focal or not engaged:
            return None
        return f"NetFlow conversation between {focal} and {engaged}."
    flows = int(digest["flows"])
    total = int(digest.get("total_flows") or flows)
    plural = "s" if flows != 1 else ""
    kind = _protocol(digest)
    pair = _only_pair(digest)
    if pair and pair.get("src") and pair.get("dst"):
        ports = _ports(pair)
        on = ""
        if len(ports) == 1:
            on = f" on port {ports[0]}"
        elif ports:
            on = f" on {int(pair.get('port_count') or len(ports))} ports"
        sentence = f"{pair['src']} sent {flows} {kind}flow{plural} to {pair['dst']}{on}"
    else:
        pairs = digest.get("pairs") or []
        count = len(pairs) + int(digest.get("pairs_omitted") or 0)
        sentence = f"{flows} {kind}flow{plural} across {count} host pairs"
        # Pairs are stored busiest first.
        if pairs and pairs[0].get("src") and pairs[0].get("dst"):
            top = pairs[0]
            sentence += f", most from {top['src']} to {top['dst']} ({int(top.get('flows') or 0)})"
    sentence += _span(digest) + "."
    if total > flows:
        sentence += f" These counts cover {flows} of the sequence's {total} flows."
    unknown = digest.get("unknown") or {}
    if unknown.get("bytes_out") or unknown.get("bytes_back"):
        return sentence
    sent, back = int(digest.get("bytes_out") or 0), int(digest.get("bytes_back") or 0)
    if pair:
        return f"{sentence} It sent {_size(sent)} and received {_size(back)}."
    return f"{sentence} Sources sent {_size(sent)} and destinations {_size(back)}."


__all__ = ["description", "title"]
