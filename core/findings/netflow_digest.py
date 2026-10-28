"""Counts over a NetFlow finding's flows, and their sum across a case (#3275).

A finding stores only a preview of its flows (``SOURCE_EVIDENCE_PREVIEW_LIMIT``),
so the digest is taken at ingest, over every flow the sequence carried, and
kept in ``entity_context["netflow_digest"]``.  ``case_flow_summary`` sums the
digests of a case's findings so an investigate run can tell discovery from a
connection in one read.

The recorded source is the client and the destination the server; nothing is
inferred.  A flow missing a byte or packet field is counted as unknown, never
as zero.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

NETFLOW_DIGEST_VERSION = 1
# Per finding: host pairs kept, ports named per pair, port ranges stored.
DIGEST_PAIR_LIMIT = 20
DIGEST_TOP_PORTS = 10
DIGEST_PORT_RANGE_LIMIT = 256
DIGEST_PROTOCOL_LIMIT = 20
# Per case: host pairs, servers per pair and finding ids named.
CASE_PAIR_LIMIT = 12
CASE_TOP_SERVERS = 5
CASE_ID_LIMIT = 20

VOLUME = {
    "bytes_out": "forward_bytes",
    "bytes_back": "backward_bytes",
    "packets_out": "forward_packets",
    "packets_back": "backward_packets",
}


def _count(value: Any) -> Optional[int]:
    """A non-negative byte/packet count, or None when absent or unreadable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _port(value: Any) -> Optional[int]:
    port = _count(value)
    return port if port is not None and port <= 65535 else None


def _epoch(value: Any) -> Optional[float]:
    """Seconds since the epoch for an epoch (s/ms/us) or ISO timestamp."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.replace(".", "", 1).isdigit():
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.timestamp()
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds):
        return None
    while seconds > 1e11:  # ms or us since the epoch
        seconds /= 1000
    return seconds


def _iso(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _span(first: Any, last: Any, moment: Any):
    if moment is None:
        return first, last
    return (
        moment if first is None or moment < first else first,
        moment if last is None or moment > last else last,
    )


def _ranges(ports: Iterable[int]) -> List[List[int]]:
    ranges: List[List[int]] = []
    for port in sorted(set(ports)):
        if ranges and port == ranges[-1][1] + 1:
            ranges[-1][1] = port
        else:
            ranges.append([port, port])
    return ranges


def _ip(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _new_pair(src: Optional[str], dst: Optional[str]) -> Dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "flows": 0,
        "first": None,
        "last": None,
        "answered": 0,
        "answer_unknown": 0,
        **{key: 0 for key in VOLUME},
        "unknown": Counter(),
        "ports": Counter(),
    }


def netflow_digest(
    records: Iterable[Any], *, total_records: Optional[int] = None
) -> Dict[str, Any]:
    """Count every flow in ``records``; ``total_records`` is what the source
    says the sequence held, so a digest over a preview says it is partial."""
    pairs: Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}
    protocols: Counter = Counter()
    flows = 0
    first = last = None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        flows += 1
        key = (_ip(record.get("source_ip")), _ip(record.get("destination_ip")))
        pair = pairs.get(key) or pairs.setdefault(key, _new_pair(*key))
        pair["flows"] += 1
        moment = _epoch(record.get("timestamp"))
        pair["first"], pair["last"] = _span(pair["first"], pair["last"], moment)
        first, last = _span(first, last, moment)
        protocol = record.get("protocol")
        protocols[str(protocol) if protocol not in (None, "") else "unknown"] += 1
        port = _port(record.get("destination_port"))
        if port is not None:
            pair["ports"][port] += 1
        back = []
        for key_out, field in VOLUME.items():
            value = _count(record.get(field))
            if value is None:
                pair["unknown"][key_out] += 1
            else:
                pair[key_out] += value
            if key_out in ("bytes_back", "packets_back"):
                back.append(value)
        if any(value for value in back):
            pair["answered"] += 1
        elif None in back:
            pair["answer_unknown"] += 1

    total = max(total_records or 0, flows)
    ranked = sorted(pairs.values(), key=lambda p: -p["flows"])
    kept = ranked[:DIGEST_PAIR_LIMIT]
    unknown: Counter = Counter()
    for pair in ranked:
        unknown.update(pair["unknown"])
    digest: Dict[str, Any] = {
        "version": NETFLOW_DIGEST_VERSION,
        "flows": flows,
        "total_flows": total,
        "complete": flows >= total,
        "first": _iso(first),
        "last": _iso(last),
        "protocols": dict(protocols.most_common(DIGEST_PROTOCOL_LIMIT)),
        # Over every pair, so a reply on a pair cut below is still counted.
        **{
            key: sum(pair[key] for pair in ranked)
            for key in ("answered", "answer_unknown", *VOLUME)
        },
        "pairs": [_stored_pair(pair) for pair in kept],
    }
    if unknown:
        digest["unknown"] = dict(unknown)
    if len(ranked) > len(kept):
        digest["pairs_omitted"] = len(ranked) - len(kept)
        digest["flows_in_omitted_pairs"] = sum(p["flows"] for p in ranked[len(kept) :])
    return digest


def _stored_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    ports: Counter = pair["ports"]
    stored = {
        key: value for key, value in pair.items() if key not in ("ports", "unknown")
    }
    stored["first"], stored["last"] = _iso(pair["first"]), _iso(pair["last"])
    if pair["unknown"]:
        stored["unknown"] = dict(pair["unknown"])
    stored["port_count"] = len(ports)
    ranges = _ranges(ports)
    if len(ranges) <= DIGEST_PORT_RANGE_LIMIT:
        stored["port_ranges"] = ranges
    stored["top_ports"] = [[port, n] for port, n in ports.most_common(DIGEST_TOP_PORTS)]
    return stored


# Readers of a stored digest: JSONB may hold anything, so a bad value reads
# as nothing rather than failing the whole case_records call.
def _n(value: Any) -> int:
    return _count(value) or 0


def _when(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _port_set(ranges: Any) -> Optional[set]:
    """The ports a stored range list names, or None when it is unreadable."""
    if not isinstance(ranges, list):
        return None
    ports: set = set()
    for bounds in ranges:
        if not isinstance(bounds, list) or len(bounds) != 2:
            return None
        lo, hi = _port(bounds[0]), _port(bounds[1])
        if lo is None or hi is None or lo > hi:
            return None
        ports.update(range(lo, hi + 1))
    return ports


def case_flow_summary(
    findings: Iterable[Tuple[str, Any]], *, without_digest: Iterable[str] = ()
) -> Dict[str, Any]:
    """Sum ``(finding_id, digest)`` per host pair; ``without_digest`` names the
    case's NetFlow findings that carry none, so their flows are not read as 0."""
    pairs: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    missing = sorted(set(without_digest))
    partial: List[str] = []
    totals: Counter = Counter()
    unknown: Counter = Counter()
    first = last = None
    for finding_id, digest in findings:
        if not isinstance(digest, Mapping) or not isinstance(digest.get("pairs"), list):
            missing.append(finding_id)
            continue
        totals["findings"] += 1
        for key in ("flows", "total_flows", "flows_in_omitted_pairs"):
            totals[key] += _n(digest.get(key))
        for key in ("answered", "answer_unknown", *VOLUME):
            totals[key] += _n(digest.get(key))
        if isinstance(digest.get("unknown"), Mapping):
            unknown.update({k: _n(digest["unknown"].get(k)) for k in VOLUME})
        if digest.get("complete") is False:
            partial.append(finding_id)
        first, last = _span(first, last, _when(digest.get("first")))
        first, last = _span(first, last, _when(digest.get("last")))
        for stored in digest["pairs"]:
            if not isinstance(stored, Mapping):
                continue
            key = (_ip(stored.get("src")), _ip(stored.get("dst")))
            pair = pairs.get(key)
            if pair is None:
                pair = pairs[key] = {
                    **_new_pair(*key),
                    "findings": 0,
                    "port_set": set(),
                    "ports_exact": True,
                    "port_floor": 0,
                }
            pair["findings"] += 1
            pair["flows"] += _n(stored.get("flows"))
            for when in (_when(stored.get("first")), _when(stored.get("last"))):
                pair["first"], pair["last"] = _span(pair["first"], pair["last"], when)
            for key_out in ("answered", "answer_unknown", *VOLUME):
                pair[key_out] += _n(stored.get(key_out))
            if isinstance(stored.get("unknown"), Mapping):
                pair["unknown"].update(
                    {k: _n(stored["unknown"].get(k)) for k in VOLUME}
                )
            ports = _port_set(stored.get("port_ranges"))
            if ports is None:
                pair["ports_exact"] = False
            else:
                pair["port_set"] |= ports
            pair["port_floor"] = max(pair["port_floor"], _n(stored.get("port_count")))
            for entry in stored.get("top_ports") or []:
                if isinstance(entry, list) and len(entry) == 2:
                    port = _port(entry[0])
                    if port is not None:
                        pair["ports"][port] += _n(entry[1])

    ranked = sorted(pairs.values(), key=lambda p: -p["flows"])
    shown = [_case_pair(pair) for pair in ranked[:CASE_PAIR_LIMIT]]
    all_ports: set = set().union(*(p["port_set"] for p in pairs.values()))
    ports_exact = all(p["ports_exact"] for p in pairs.values())
    block: Dict[str, Any] = {
        "summary": (
            f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''}, "
            f"{totals['findings']} findings, {totals['flows']} flows, "
            f"{'at least ' if not ports_exact else ''}{len(all_ports)} ports, "
            f"{totals['answered']} answered, {totals['bytes_back']} bytes back"
            + (f"; {len(missing)} NetFlow findings have no digest" if missing else "")
        ),
        "findings_digested": totals["findings"],
        "flows": totals["flows"],
        "total_flows": totals["total_flows"],
        "first": first,
        "last": last,
        "distinct_ports": len(all_ports),
        **{key: totals[key] for key in ("answered", "answer_unknown", *VOLUME)},
        "pairs": shown,
    }
    if not ports_exact:
        block["distinct_ports_at_least"] = True
    if +unknown:
        block["unknown"] = dict(+unknown)
    if totals["flows"] < totals["total_flows"]:
        block["partial"] = {
            "note": "these findings' digests cover only part of their flows",
            "count": len(partial),
            "finding_ids": partial[:CASE_ID_LIMIT],
        }
    if len(ranked) > CASE_PAIR_LIMIT:
        block["pairs_omitted"] = len(ranked) - CASE_PAIR_LIMIT
    if totals["flows_in_omitted_pairs"]:
        # Counted in the totals above, but not in any pair listed.
        block["flows_outside_pairs"] = totals["flows_in_omitted_pairs"]
    if missing:
        block["no_digest"] = {
            "note": "NetFlow findings whose flows are not counted here",
            "count": len(missing),
            "finding_ids": missing[:CASE_ID_LIMIT],
        }
    return block


def _case_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    exact = pair["ports_exact"]
    distinct = (
        len(pair["port_set"])
        if exact
        else max(len(pair["port_set"]), pair["port_floor"])
    )
    shown = {
        "src": pair["src"],
        "dst": pair["dst"],
        "findings": pair["findings"],
        "flows": pair["flows"],
        "first": pair["first"],
        "last": pair["last"],
        "distinct_ports": distinct,
        "answered": pair["answered"],
        "answer_unknown": pair["answer_unknown"],
        **{key: pair[key] for key in VOLUME},
        # Summed from each finding's busiest ports, so a floor, not a count.
        "top_servers": [
            f"{pair['dst']}:{port} ({n})"
            for port, n in pair["ports"].most_common(CASE_TOP_SERVERS)
        ],
    }
    if not exact:
        shown["distinct_ports_at_least"] = True
    if +pair["unknown"]:
        shown["unknown"] = dict(+pair["unknown"])
    return shown
